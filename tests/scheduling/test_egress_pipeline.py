"""Egress FX funnel tests — the single _enqueue_with_egress chokepoint.

Covers the three audio-delivery scenarios for the egress path:
  - Normal: a produced music/voice segment is coloured by the broadcast chain.
  - Empty-fallback: a canned / silence-fallback rescue SKIPS the chain (no extra
    ffmpeg pass between the queue running dry and audio resuming).
  - Post-restart: a resume/idle bridge SKIPS the chain so audio is instant after a
    watchdog restart.
Plus the best-effort contract: a colouring failure never raises and never drops a
beat — the un-coloured audio airs instead of dead air.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mammamiradio.core.models import Segment, SegmentType, StationState
from mammamiradio.scheduling import producer
from mammamiradio.scheduling.producer import (
    _adjacent_music_source,
    _apply_egress,
    _enqueue_with_egress,
    _front_insert_queue_and_shadow,
    _is_rescue_fill,
)

PRODUCER_MODULE = "mammamiradio.scheduling.producer"


@pytest.fixture(autouse=True)
def _reset_producer_music_cache():
    producer._last_music_file = None
    yield
    producer._last_music_file = None


class _Cfg:
    """Minimal stand-in carrying the tmp_dir + cache_dir the egress pass needs."""

    def __init__(self, tmp_dir: Path, cache_dir: Path | None = None):
        self.tmp_dir = tmp_dir
        # Defaults to tmp_dir for the ephemeral-source tests, which never read it
        # (the bakeable branch short-circuits on ephemeral=True). Bake tests pass it.
        self.cache_dir = cache_dir if cache_dir is not None else tmp_dir


def _colour(in_path: Path, out_path: Path) -> bool:
    """Stand-in for apply_broadcast_chain: writes the coloured output, reports success."""
    out_path.write_bytes(b"COLOURED")
    return True


def _seg(tmp_path: Path, *, metadata: dict, ephemeral: bool = True, name: str = "seg.mp3") -> Segment:
    path = tmp_path / name
    path.write_bytes(b"AUDIO")
    return Segment(type=SegmentType.MUSIC, path=path, metadata=metadata, ephemeral=ephemeral)


# ── Scenario 2 & 3: rescue / bridge fills must skip the pass ───────────────────


# The real metadata shapes the producer stamps on each rescue/bridge fill. Every one
# carries the explicit ``rescue`` flag (the egress skip key); the semantic markers
# (queue_drain_recovery, idle_bridge, …) ride alongside it for other subsystems.
_RESCUE_METADATA_SHAPES = [
    {"queue_drain_recovery": True, "rescue": True},  # drain canned / norm-cache / tone bridge
    {"resume_bridge": True, "rescue": True},  # resume bridge
    {"warmup": True, "idle_bridge": True, "rescue": True},  # idle warm-up bridge
    {"silence_fallback": True, "rescue": True},  # quality-gate silence fallback
    {"recycled": True, "silence_fallback": True, "rescue": True},  # last-known-good recycle
    {"error_recovery": True, "rescue": True},  # error-recovery canned / norm-cache / recycled
    {"error_recovery": True, "rescue": True, "audio_source": "emergency_tone", "error": "boom"},  # error-recovery tone
    {"canned": True, "queue_drain_recovery": True, "rescue": True},  # canned bridge
]


@pytest.mark.parametrize("metadata", _RESCUE_METADATA_SHAPES)
async def test_apply_egress_skips_rescue_fills(tmp_path, metadata):
    """Every emergency / bridge / rescue fill carries the explicit ``rescue`` flag and
    bypasses the egress pass so a dead-air rescue is never delayed by an ffmpeg encode
    (INSTANT AUDIO)."""
    seg = _seg(tmp_path, metadata=metadata)
    with patch(f"{PRODUCER_MODULE}.apply_broadcast_chain") as m_chain:
        out = await _apply_egress(seg, _Cfg(tmp_path))
    m_chain.assert_not_called()
    assert out is seg  # returned untouched, same object


async def test_apply_egress_colours_rotation_canned_banter(tmp_path):
    """A canned clip used in NORMAL rotation (shareware gold clips / Demo mode) carries
    ``canned=True`` but NOT the ``rescue`` flag — it is content, not a dead-air rescue,
    so it MUST still be coloured. Guards the seam where Demo-mode host breaks aired
    studio-clean next to FM-coloured music (the regression this fix removes)."""
    seg = _seg(tmp_path, metadata={"type": "banter", "canned": True, "title": "Pre-recorded banter"})
    with patch(f"{PRODUCER_MODULE}.apply_broadcast_chain", side_effect=_colour) as m_chain:
        out = await _apply_egress(seg, _Cfg(tmp_path))
    m_chain.assert_called_once()  # rotation-canned banter went through the transmitter
    assert out.path.name.startswith("egress_")


# ── Scenario 1: a produced segment is coloured ────────────────────────────────


async def test_apply_egress_colours_normal_segment(tmp_path):
    """A normal segment is replaced by a fresh ephemeral egress render."""
    seg = _seg(tmp_path, metadata={"title": "Song"}, ephemeral=True, name="orig.mp3")
    with patch(f"{PRODUCER_MODULE}.apply_broadcast_chain", side_effect=_colour):
        out = await _apply_egress(seg, _Cfg(tmp_path))
    assert out.path != seg.path
    assert out.path.name.startswith("egress_")
    assert out.ephemeral is True
    assert out.path.read_bytes() == b"COLOURED"
    assert out.metadata == seg.metadata  # metadata carried through replace()


async def test_apply_egress_colours_live_generated_banter(tmp_path):
    """The dominant voice case: a normal LLM-generated banter segment stamps
    ``canned=False`` (it is NOT a canned clip). It MUST still be coloured — skipping
    it would air studio-clean voice next to FM music, the exact seam this removes.
    Guards against the 'skip on key presence' bug where ``canned=False`` wrongly
    bypassed the transmitter."""
    seg = Segment(
        type=SegmentType.BANTER,
        path=tmp_path / "banter.mp3",
        metadata={"title": "Host break", "canned": False},
        ephemeral=True,
    )
    seg.path.write_bytes(b"VOICE")
    with patch(f"{PRODUCER_MODULE}.apply_broadcast_chain", side_effect=_colour) as m_chain:
        out = await _apply_egress(seg, _Cfg(tmp_path))
    m_chain.assert_called_once()
    assert out.path.name.startswith("egress_")  # the host break went through the transmitter


async def test_apply_egress_unlinks_pre_egress_tmp(tmp_path):
    """A freshly-rendered (ephemeral, tmp) input is cleaned up after colouring so the
    pre-egress render does not leak."""
    seg = _seg(tmp_path, metadata={"title": "Song"}, ephemeral=True, name="orig.mp3")
    pre_path = seg.path
    with patch(f"{PRODUCER_MODULE}.apply_broadcast_chain", side_effect=_colour):
        await _apply_egress(seg, _Cfg(tmp_path))
    assert not pre_path.exists()  # the pre-egress tmp was removed


# ── Cache-bake: a norm-cache music hit is coloured once and reused ────────────


def _cache_setup(tmp_path: Path) -> tuple[_Cfg, Segment]:
    """A cache-file music segment (non-ephemeral, source under cache_dir) + its _Cfg.

    Tests derive the cache dir from ``cfg.cache_dir`` and the source from ``seg.path``.
    """
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    src = cache_dir / "norm_song.mp3"
    src.write_bytes(b"CACHED")
    seg = Segment(type=SegmentType.MUSIC, path=src, metadata={"title": "S"}, ephemeral=False)
    return _Cfg(tmp_dir, cache_dir), seg


async def test_apply_egress_bakes_cache_hit_and_preserves_source(tmp_path):
    """A norm-cache music hit is colour-baked into a persistent cache file (fm_*), the
    source is left intact, and the baked render is non-ephemeral (kept for reuse)."""
    cfg, seg = _cache_setup(tmp_path)
    with (
        patch(f"{PRODUCER_MODULE}.broadcast_chain_version", return_value="v1"),
        patch(f"{PRODUCER_MODULE}.apply_broadcast_chain", side_effect=_colour),
    ):
        out = await _apply_egress(seg, cfg)
    assert out.path.name.startswith("fm_") and out.path.parent == cfg.cache_dir
    assert out.ephemeral is False  # a cache file — not unlinked after play
    assert out.path.read_bytes() == b"COLOURED"
    assert seg.path.exists() and seg.path.read_bytes() == b"CACHED"  # source untouched
    assert not list(cfg.cache_dir.glob("*.staging_*"))  # staging atomically consumed


async def test_apply_egress_reuses_baked_render_without_reencode(tmp_path):
    """The win: the second play of a cached song reuses the baked render and does NOT
    re-run the FM encode (the per-replay re-encode that cost Pi CPU is eliminated)."""
    cfg, seg = _cache_setup(tmp_path)
    with (
        patch(f"{PRODUCER_MODULE}.broadcast_chain_version", return_value="v1"),
        patch(f"{PRODUCER_MODULE}.apply_broadcast_chain", side_effect=_colour) as m_chain,
    ):
        first = await _apply_egress(seg, cfg)
        second = await _apply_egress(seg, cfg)
    assert m_chain.call_count == 1  # baked once, reused on replay — no second encode
    assert first.path == second.path and second.ephemeral is False


async def test_apply_egress_rebakes_on_chain_version_change(tmp_path):
    """A filter/encoding change yields a new chain version, so the segment re-bakes under
    a new key instead of airing a stale colour (the old bake is left to LRU eviction)."""
    cfg, seg = _cache_setup(tmp_path)
    with (
        patch(f"{PRODUCER_MODULE}.broadcast_chain_version", side_effect=["v1", "v2"]),
        patch(f"{PRODUCER_MODULE}.apply_broadcast_chain", side_effect=_colour) as m_chain,
    ):
        r1 = await _apply_egress(seg, cfg)
        r2 = await _apply_egress(seg, cfg)
    assert m_chain.call_count == 2  # re-encoded for the new version
    assert r1.path != r2.path
    assert "v1" in r1.path.name and "v2" in r2.path.name


async def test_apply_egress_cache_source_noop_when_chain_disabled(tmp_path):
    """With the chain disabled (broadcast_chain_version is None — the autouse default),
    a cache-file source is returned untouched and never re-encoded."""
    cfg, seg = _cache_setup(tmp_path)
    with patch(f"{PRODUCER_MODULE}.apply_broadcast_chain") as m_chain:
        out = await _apply_egress(seg, cfg)
    m_chain.assert_not_called()
    assert out is seg


async def test_apply_egress_bake_failure_airs_source_clean(tmp_path):
    """apply_broadcast_chain returning False leaves the source aired un-coloured this
    play (never dead air) and publishes no baked/partial file."""
    cfg, seg = _cache_setup(tmp_path)
    with (
        patch(f"{PRODUCER_MODULE}.broadcast_chain_version", return_value="v1"),
        patch(f"{PRODUCER_MODULE}.apply_broadcast_chain", return_value=False),
    ):
        out = await _apply_egress(seg, cfg)
    assert out is seg
    assert not list(cfg.cache_dir.glob("fm_*"))  # nothing published, staging cleaned


async def test_apply_egress_bake_exception_cleans_staging(tmp_path):
    """A raised exception during the bake never escapes, airs the source un-coloured,
    and leaves no staging file behind in the cache."""
    cfg, seg = _cache_setup(tmp_path)
    with (
        patch(f"{PRODUCER_MODULE}.broadcast_chain_version", return_value="v1"),
        patch(f"{PRODUCER_MODULE}.apply_broadcast_chain", side_effect=RuntimeError("boom")),
    ):
        out = await _apply_egress(seg, cfg)
    assert out is seg
    assert not list(cfg.cache_dir.glob("fm_*"))


async def test_apply_egress_bake_publish_failure_airs_source_clean(tmp_path):
    """If the atomic publish (os.replace) fails after a successful encode, the source is
    aired un-coloured this play and no baked/staging file is left in the cache."""
    cfg, seg = _cache_setup(tmp_path)
    with (
        patch(f"{PRODUCER_MODULE}.broadcast_chain_version", return_value="v1"),
        patch(f"{PRODUCER_MODULE}.apply_broadcast_chain", side_effect=_colour),
        patch(f"{PRODUCER_MODULE}.os.replace", side_effect=OSError("ENOSPC")),
    ):
        out = await _apply_egress(seg, cfg)
    assert out is seg
    assert not list(cfg.cache_dir.glob("fm_*"))  # staging cleaned, nothing published


async def test_apply_egress_reuses_baked_render_from_prior_run(tmp_path):
    """Scenario 3 (post-restart): a baked render persisted on disk from a prior run is
    reused on the next play with no re-encode — instant, no FFmpeg, even after a restart."""
    cfg, seg = _cache_setup(tmp_path)
    st = seg.path.stat()
    baked = cfg.cache_dir / f"fm_{seg.path.stem}_v1_{st.st_mtime_ns}_{st.st_size}.mp3"
    baked.write_bytes(b"COLOURED-FROM-PRIOR-RUN")  # as if a prior run produced it
    with (
        patch(f"{PRODUCER_MODULE}.broadcast_chain_version", return_value="v1"),
        patch(f"{PRODUCER_MODULE}.apply_broadcast_chain") as m_chain,
    ):
        out = await _apply_egress(seg, cfg)
    m_chain.assert_not_called()  # no encode — reused the persisted bake
    assert out.path == baked and out.ephemeral is False


async def test_apply_egress_rebakes_when_source_rewritten(tmp_path):
    """A norm source rewritten in place (e.g. ``reconcile_cached_music`` re-levels it after
    a LUFS-target change, or it is evicted and regenerated) must NOT reuse the stale bake.
    The source mtime+size is in the bake key, so a content change re-bakes the new source
    instead of airing the old/quieter colour."""
    cfg, seg = _cache_setup(tmp_path)
    with (
        patch(f"{PRODUCER_MODULE}.broadcast_chain_version", return_value="v1"),
        patch(f"{PRODUCER_MODULE}.apply_broadcast_chain", side_effect=_colour) as m_chain,
    ):
        first = await _apply_egress(seg, cfg)
        # Source rewritten in place with different content (new size — and mtime) so the
        # bake key must change even though the path and chain version are identical.
        seg.path.write_bytes(b"RECONCILED-LOUDER-AND-LONGER-THAN-BEFORE")
        second = await _apply_egress(seg, cfg)
    assert m_chain.call_count == 2  # re-baked from the updated source, not reused
    assert first.path != second.path  # the source tag in the key changed


# ── Best-effort: a colouring failure never produces dead air ──────────────────


async def test_apply_egress_returns_original_when_chain_declines(tmp_path):
    """apply_broadcast_chain returning False (disabled / measure failure) leaves the
    original segment untouched and cleans up the unused tmp output."""
    seg = _seg(tmp_path, metadata={"title": "Song"})
    with patch(f"{PRODUCER_MODULE}.apply_broadcast_chain", return_value=False):
        out = await _apply_egress(seg, _Cfg(tmp_path))
    assert out is seg
    assert not list(tmp_path.glob("egress_*.mp3"))  # the unused tmp was removed


async def test_apply_egress_best_effort_on_exception(tmp_path):
    """A raised exception inside the pass never escapes — the un-coloured segment airs
    instead of dead air, and the tmp output is cleaned up."""
    seg = _seg(tmp_path, metadata={"title": "Song"})
    with patch(f"{PRODUCER_MODULE}.apply_broadcast_chain", side_effect=RuntimeError("boom")):
        out = await _apply_egress(seg, _Cfg(tmp_path))
    assert out is seg
    assert not list(tmp_path.glob("egress_*.mp3"))


async def test_apply_egress_cleans_up_tmp_on_cancellation(tmp_path):
    """A cancellation/shutdown landing mid-encode must NOT leak the half-written egress
    tmp, and must propagate (not swallow) — ``except Exception`` would miss it because
    CancelledError is a BaseException. Uses a BaseException to hit the same branch
    deterministically without asyncio cancellation internals."""

    class _Interrupt(BaseException):
        pass

    seg = _seg(tmp_path, metadata={"title": "Song"})

    def _partial_then_interrupt(in_path: Path, out_path: Path) -> bool:
        out_path.write_bytes(b"PARTIAL")  # ffmpeg left a half-written file behind
        raise _Interrupt

    with (
        patch(f"{PRODUCER_MODULE}.apply_broadcast_chain", side_effect=_partial_then_interrupt),
        pytest.raises(_Interrupt),
    ):
        await _apply_egress(seg, _Cfg(tmp_path))
    assert not list(tmp_path.glob("egress_*.mp3"))  # the half-written tmp was removed


# ── The funnel wires egress to both enqueue shapes ────────────────────────────


async def test_enqueue_with_egress_puts_coloured_segment(tmp_path):
    """The normal path colours then enqueues the processed segment."""
    seg = _seg(tmp_path, metadata={"title": "Song"})
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    state = StationState()
    with patch(f"{PRODUCER_MODULE}.apply_broadcast_chain", side_effect=_colour):
        assert await _enqueue_with_egress(queue, state, _Cfg(tmp_path), seg) is True
    queued = queue.get_nowait()
    assert queued.path.name.startswith("egress_")  # the COLOURED segment was queued
    assert state.last_enqueued_type == SegmentType.MUSIC
    # The bed source is the CLEAN pre-egress render, never the coloured output. Here the
    # ephemeral pre-egress render was consumed by the colour pass, so last_music_file is
    # left unset rather than pointed at the coloured file (the cache-hit test below
    # asserts the positive clean-source case).
    assert state.last_music_file is None
    assert producer._last_music_file is None


async def test_enqueue_with_egress_front_insert_colours_before_critical_section(tmp_path):
    """Operator air-next colours the segment BEFORE the synchronous front-insert
    critical section (which must stay a no-await drain→prepend→repush)."""
    seg = _seg(tmp_path, metadata={"title": "Song"})
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    state = StationState()
    m_front = MagicMock(return_value=True)
    with (
        patch(f"{PRODUCER_MODULE}.apply_broadcast_chain", side_effect=_colour) as m_chain,
        patch(f"{PRODUCER_MODULE}._front_insert_queue_and_shadow", m_front),
    ):
        ok = await _enqueue_with_egress(queue, state, _Cfg(tmp_path), seg, front_insert=True, shadow_entry={"id": "x"})
    assert ok is True
    m_chain.assert_called_once()
    inserted_segment = m_front.call_args[0][2]  # (queue, state, segment, shadow_entry)
    assert inserted_segment.path.name.startswith("egress_")  # coloured before insert
    assert state.last_enqueued_type is None
    assert state.last_music_file is None


async def test_enqueue_with_egress_front_insert_requires_shadow_entry(tmp_path):
    """front_insert without a shadow_entry is a programming error that corrupts the
    up-next shadow list — raise a clear ValueError rather than relying on an ``assert``
    that python -O would strip into a None inserted into the shadow."""
    seg = _seg(tmp_path, metadata={"title": "Song"})
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    state = StationState()
    with (
        patch(f"{PRODUCER_MODULE}.apply_broadcast_chain", side_effect=_colour),
        pytest.raises(ValueError),
    ):
        await _enqueue_with_egress(queue, state, _Cfg(tmp_path), seg, front_insert=True, shadow_entry=None)


async def test_enqueue_with_egress_rescue_skips_chain(tmp_path):
    """A rescue segment routed through the funnel is enqueued as-is, never coloured."""
    seg = _seg(tmp_path, metadata={"canned": True, "queue_drain_recovery": True, "rescue": True})
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    state = StationState()
    with patch(f"{PRODUCER_MODULE}.apply_broadcast_chain") as m_chain:
        await _enqueue_with_egress(queue, state, _Cfg(tmp_path), seg)
    m_chain.assert_not_called()
    assert queue.get_nowait() is seg  # the original bridge audio, undelayed
    assert state.last_enqueued_type == SegmentType.MUSIC
    assert state.last_music_file == seg.path


async def test_enqueue_with_egress_emergency_tone_clears_music_adjacency(tmp_path):
    """The synthetic continuity tone is MUSIC-shaped audio but a continuity BREAK: it
    must never let a song the beep gapped out bleed under the next speech."""
    previous_song = tmp_path / "previous_song.mp3"
    previous_song.write_bytes(b"MUSIC")
    tone = _seg(
        tmp_path,
        metadata={"audio_source": "emergency_tone", "queue_drain_recovery": True, "rescue": True},
        name="tone.mp3",
    )
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    # Seed a real song as the prior tail — the tone must sever it, not inherit it.
    state = StationState(last_music_file=previous_song, last_enqueued_type=SegmentType.MUSIC)

    with patch(f"{PRODUCER_MODULE}.apply_broadcast_chain") as m_chain:
        assert await _enqueue_with_egress(queue, state, _Cfg(tmp_path), tone) is True

    m_chain.assert_not_called()
    assert queue.get_nowait() is tone
    assert state.last_enqueued_type is None
    # The next speech beds nothing — no stale song bleeds across the beep gap.
    assert _adjacent_music_source(state) is None


async def test_enqueue_with_egress_recycled_music_preserves_music_adjacency(tmp_path):
    """Recycled last-known-good music is still a real queued song for the next speech bed."""
    recycled = _seg(tmp_path, metadata={"recycled": True, "silence_fallback": True, "rescue": True})
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    state = StationState(last_music_file=recycled.path, last_enqueued_type=SegmentType.AD)

    assert await _enqueue_with_egress(queue, state, _Cfg(tmp_path), recycled) is True

    assert queue.get_nowait() is recycled
    assert state.last_enqueued_type == SegmentType.MUSIC
    assert state.last_music_file == recycled.path
    assert _adjacent_music_source(state) == recycled.path


async def test_enqueue_with_egress_error_segment_clears_music_adjacency(tmp_path):
    """A failed render aired as brief silence is a continuity break: it must sever song
    adjacency so the pre-error song never bleeds under the next speech segment."""
    previous_song = tmp_path / "previous_song.mp3"
    previous_song.write_bytes(b"MUSIC")
    errored = _seg(tmp_path, metadata={"error": "boom", "rescue": True}, name="error.mp3")
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    # Seed a real song as the prior tail — the errored silence must sever it.
    state = StationState(last_music_file=previous_song, last_enqueued_type=SegmentType.MUSIC)

    assert await _enqueue_with_egress(queue, state, _Cfg(tmp_path), errored) is True

    assert queue.get_nowait() is errored
    assert state.last_enqueued_type is None
    assert _adjacent_music_source(state) is None


async def test_enqueue_with_egress_front_insert_does_not_skew_tail_adjacency(tmp_path):
    """Air-next inserts at the head; the later tail-generated speech still sees the tail song.
    Use a rescue fill as the tail so the funnel records its clean bed source (rendered music's
    bed source is owned by _remember_rendered_music, not exercised in this funnel-only test)."""
    song = _seg(
        tmp_path,
        metadata={"title": "Tail Song", "rescue": True, "audio_source": "norm_cache"},
        ephemeral=False,
        name="tail_song.mp3",
    )
    front_banter = Segment(
        type=SegmentType.BANTER,
        path=tmp_path / "front_banter.mp3",
        metadata={"title": "Operator banter"},
        ephemeral=False,
    )
    front_banter.path.write_bytes(b"VOICE")
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    state = StationState()

    with patch(f"{PRODUCER_MODULE}._apply_egress", return_value=song):
        assert await _enqueue_with_egress(queue, state, _Cfg(tmp_path), song) is True
    with (
        patch(f"{PRODUCER_MODULE}._apply_egress", return_value=front_banter),
        patch(f"{PRODUCER_MODULE}._front_insert_queue_and_shadow", return_value=True),
    ):
        assert await _enqueue_with_egress(
            queue,
            state,
            _Cfg(tmp_path),
            front_banter,
            front_insert=True,
            shadow_entry={"id": "front"},
        )

    assert state.last_enqueued_type == SegmentType.MUSIC
    assert state.last_music_file == song.path
    assert _adjacent_music_source(state) == song.path


def test_seed_adjacency_type_applies_continuity_break_on_both_paths(tmp_path):
    """Producer-start seed must honour the continuity-break rule for the queued tail AND the
    now-streaming inference — a tone/errored now-playing on an empty-queue restart must not seed
    MUSIC and let a stale last_music_file bleed under the first speech segment (#641)."""
    from mammamiradio.scheduling.producer import _seed_adjacency_type

    # Empty queue + emergency-tone now-playing → cleared.
    state = StationState()
    state.now_streaming = {"type": "music", "metadata": {"audio_source": "emergency_tone"}}
    empty: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    assert _seed_adjacency_type(empty, state, SegmentType.MUSIC) is None

    # Empty queue + errored now-playing → cleared.
    state.now_streaming = {"type": "music", "metadata": {"error": "boom"}}
    assert _seed_adjacency_type(empty, state, SegmentType.MUSIC) is None

    # Empty queue + a real song now-playing (resume mid-song) → MUSIC preserved.
    state.now_streaming = {"type": "music", "metadata": {"title": "Real Song"}}
    assert _seed_adjacency_type(empty, state, SegmentType.MUSIC) == SegmentType.MUSIC

    # Queued tone tail → cleared regardless of the inference argument.
    q: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    q.put_nowait(_seg(tmp_path, metadata={"audio_source": "emergency_tone", "rescue": True}, name="t.mp3"))
    assert _seed_adjacency_type(q, state, SegmentType.MUSIC) is None


def test_adjacency_type_for_treats_continuity_breaks_as_non_song():
    """The single shared rule: errored silence and the emergency tone are NOT adjacent songs."""
    from mammamiradio.scheduling.producer import _adjacency_type_for

    music = Segment(type=SegmentType.MUSIC, path=Path("x.mp3"), metadata={"title": "Song"})
    tone = Segment(
        type=SegmentType.MUSIC, path=Path("t.mp3"), metadata={"audio_source": "emergency_tone", "rescue": True}
    )
    errored = Segment(type=SegmentType.MUSIC, path=Path("e.mp3"), metadata={"error": "boom"})
    banter = Segment(type=SegmentType.BANTER, path=Path("b.mp3"), metadata={})

    assert _adjacency_type_for(music) == SegmentType.MUSIC
    assert _adjacency_type_for(tone) is None
    assert _adjacency_type_for(errored) is None
    assert _adjacency_type_for(banter) == SegmentType.BANTER


def test_front_insert_tone_tail_not_reclassified_as_music(tmp_path):
    """Air-next behind a buffered emergency-tone tail must not reclassify the tone as an
    adjacent song — the funnel already cleared adjacency when the tone was enqueued, and the
    tail recompute must honour the same continuity-break rule (#641)."""
    tone = Segment(
        type=SegmentType.MUSIC,
        path=tmp_path / "tone.mp3",
        metadata={"audio_source": "emergency_tone", "rescue": True},
        ephemeral=True,
    )
    tone.path.write_bytes(b"BEEP")
    prior_song = tmp_path / "prior_song.mp3"
    prior_song.write_bytes(b"MUSIC")
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    queue.put_nowait(tone)  # buffered tail is the tone
    state = StationState(last_music_file=prior_song, last_enqueued_type=None)
    state.queued_segments = [{"id": "tone", "type": "music"}]

    front_banter = Segment(type=SegmentType.BANTER, path=tmp_path / "fb.mp3", metadata={"title": "Op"}, ephemeral=False)
    front_banter.path.write_bytes(b"VOICE")

    assert _front_insert_queue_and_shadow(queue, state, front_banter, {"id": "front", "type": "banter"}) is True

    assert state.last_enqueued_type is None  # tone tail is a continuity break, not a song
    assert _adjacent_music_source(state) is None


def test_front_insert_full_queue_drop_clears_stale_music_adjacency(tmp_path):
    """Air-next on a FULL queue drops the furthest-future tail. That dropped segment is
    exactly what last_enqueued_type describes, so its adjacency basis must be cleared —
    otherwise a later generated speech segment would bed a dropped (never-aired) song.
    A cache-backed dropped song stays on disk, so the existence check alone won't save us."""
    # Cache-backed tail song (ephemeral=False) — survives the drop on disk.
    tail_song = tmp_path / "tail_song.mp3"
    tail_song.write_bytes(b"MUSIC")
    queued_song = Segment(type=SegmentType.MUSIC, path=tail_song, metadata={"title": "Tail"}, ephemeral=False)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=1)
    queue.put_nowait(queued_song)
    # Adjacency currently points at the song that is the queue tail.
    state = StationState(last_music_file=tail_song, last_enqueued_type=SegmentType.MUSIC)
    state.queued_segments = [{"id": "tail", "type": "music"}]

    front_banter = Segment(
        type=SegmentType.BANTER,
        path=tmp_path / "front_banter.mp3",
        metadata={"title": "Operator banter"},
        ephemeral=False,
    )
    front_banter.path.write_bytes(b"VOICE")

    # maxsize=1: inserting the banter forces the tail song to be dropped, leaving the banter
    # as the actual new tail.
    assert _front_insert_queue_and_shadow(queue, state, front_banter, {"id": "front", "type": "banter"}) is True

    assert state.last_enqueued_type == SegmentType.BANTER  # inserted banter is the new tail
    # No stale song bleeds under the next generated speech even though tail_song persists on disk.
    assert _adjacent_music_source(state) is None


def test_front_insert_into_empty_queue_clears_stale_music_adjacency(tmp_path):
    """Air-next into an EMPTY queue makes the inserted (speech) segment the real tail. A prior
    last_enqueued_type=MUSIC must not survive, or the next generated speech would bed a song the
    operator's banter aired in front of (#641). Recompute adjacency from the actual new tail."""
    prior_song = tmp_path / "prior_song.mp3"
    prior_song.write_bytes(b"MUSIC")
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)  # empty
    state = StationState(last_music_file=prior_song, last_enqueued_type=SegmentType.MUSIC)
    state.queued_segments = []

    front_banter = Segment(
        type=SegmentType.BANTER,
        path=tmp_path / "front_banter.mp3",
        metadata={"title": "Operator banter"},
        ephemeral=False,
    )
    front_banter.path.write_bytes(b"VOICE")

    assert _front_insert_queue_and_shadow(queue, state, front_banter, {"id": "front", "type": "banter"}) is True

    assert state.last_enqueued_type == SegmentType.BANTER  # the inserted segment is the new tail
    assert _adjacent_music_source(state) is None  # no stale song bleeds behind the operator banter


async def test_enqueue_with_egress_rescue_records_clean_pre_egress_source(tmp_path):
    """For a RESCUE fill (the only music the funnel records as the bed source), the
    bed/crossfade source must be the CLEAN pre-egress render, never the egress output.
    With broadcast_chain on, egress returns an FM-baked path; recording that would make a
    later speech crossfade embed already-coloured audio that egress colours a second time.
    The funnel captures the clean source before egress, so last_music_file stays clean."""
    clean = _seg(
        tmp_path,
        metadata={"title": "Rescue Song", "rescue": True, "audio_source": "norm_cache"},
        ephemeral=False,
        name="norm_song.mp3",
    )
    baked = tmp_path / "fm_song.mp3"
    baked.write_bytes(b"FM-COLOURED")
    baked_segment = Segment(type=SegmentType.MUSIC, path=baked, metadata=clean.metadata, ephemeral=False)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    state = StationState(last_enqueued_type=SegmentType.AD)

    # Egress swaps the path to the baked render (what airs), but the funnel captured
    # the clean source beforehand.
    with patch(f"{PRODUCER_MODULE}._apply_egress", return_value=baked_segment):
        assert await _enqueue_with_egress(queue, state, _Cfg(tmp_path), clean) is True

    assert queue.get_nowait() is baked_segment  # the coloured render is what airs
    assert state.last_enqueued_type == SegmentType.MUSIC
    assert state.last_music_file == clean.path  # …but the bed source stays clean
    assert _adjacent_music_source(state) == clean.path


async def test_enqueue_with_egress_rendered_music_does_not_overwrite_clean_bed_source(tmp_path):
    """Normally-rendered music (no rescue/recycled flag) already had its CLEAN source
    recorded by _remember_rendered_music BEFORE the transition-sting prepend and egress.
    The funnel must NOT overwrite last_music_file for it — by enqueue time segment.path may
    be a sting-merged or FM-baked render, and bedding that under a later announcer would
    smear a stinger/colour into the bed. last_enqueued_type still flips to MUSIC."""
    prior_clean = tmp_path / "prior_clean_song.mp3"
    prior_clean.write_bytes(b"CLEAN")
    # A rendered segment whose path is a sting-merged temp render (what the main loop builds).
    sting_merged = tmp_path / "segment_with_sting_abc.mp3"
    sting_merged.write_bytes(b"STING+MUSIC")
    rendered = Segment(type=SegmentType.MUSIC, path=sting_merged, metadata={"title": "Rendered"}, ephemeral=True)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    state = StationState(last_music_file=prior_clean, last_enqueued_type=SegmentType.AD)

    with patch(f"{PRODUCER_MODULE}._apply_egress", return_value=rendered):
        assert await _enqueue_with_egress(queue, state, _Cfg(tmp_path), rendered) is True

    assert state.last_enqueued_type == SegmentType.MUSIC
    # The clean source recorded earlier by _remember_rendered_music is preserved.
    assert state.last_music_file == prior_clean


def test_norm_cache_bridge_payload_stamps_rescue_flag():
    """Non-tautological floor: the shared norm-cache bridge builder (used by the drain,
    resume, and idle bridges) stamps the explicit ``rescue`` flag, so every norm-cache
    rescue skips the egress pass. Exercises the real constructor, not a mirror list."""
    from pathlib import Path as _Path

    with patch(f"{PRODUCER_MODULE}.load_track_metadata", return_value=None):
        metadata, _label = producer._norm_cache_bridge_payload(
            _Path("/cache/norm_song.mp3"), "queue_drain_recovery", "Mamma Mi Radio"
        )
    assert metadata.get("rescue") is True
    assert metadata.get("queue_drain_recovery") is True
    assert _is_rescue_fill(Segment(type=SegmentType.MUSIC, path=_Path("/x.mp3"), metadata=metadata))


def test_every_rescue_marker_dict_in_producer_stamps_rescue_flag():
    """Drift guard, stronger than the old allowlist: scan the producer SOURCE and assert
    every metadata dict literal carrying an unambiguous bridge/rescue marker also carries
    the explicit ``rescue`` flag. A new rescue site that forgets the flag would get an
    FFmpeg delay on the dead-air path (INSTANT AUDIO) — this fails CI at the construction
    site instead of trusting a hand-maintained mirror list to be kept in sync."""
    import ast
    from pathlib import Path as _Path

    # Markers that ONLY ever appear on rescue/bridge fills (not overloaded like
    # ``canned``/``error``, which also ride on normal content).
    markers = {
        "queue_drain_recovery",
        "resume_bridge",
        "idle_bridge",
        "warmup",
        "silence_fallback",
        "recycled",
        "error_recovery",
    }
    tree = ast.parse(_Path(producer.__file__).read_text())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if (keys & markers) and "rescue" not in keys:
            offenders.append((node.lineno, sorted(keys & markers)))
    assert not offenders, f"producer rescue dicts missing 'rescue': True at {offenders}"


def test_is_rescue_fill_keys_off_explicit_rescue_flag():
    """The skip decision is the explicit ``rescue`` flag, NOT an overloaded key like
    ``canned``: rotation-canned banter (``canned=True``, no rescue flag) must be
    coloured, while a flagged fill skips. This is the contract that closed the
    Demo-mode studio-clean-voice seam."""
    rotation_canned = Segment(type=SegmentType.BANTER, path=Path("/x.mp3"), metadata={"canned": True})
    canned_false = Segment(type=SegmentType.BANTER, path=Path("/x.mp3"), metadata={"canned": False})
    bare_error_key = Segment(type=SegmentType.MUSIC, path=Path("/x.mp3"), metadata={"error": "boom"})
    flagged_fill = Segment(type=SegmentType.BANTER, path=Path("/x.mp3"), metadata={"rescue": True})
    normal = Segment(type=SegmentType.MUSIC, path=Path("/x.mp3"), metadata={"title": "Song"})
    assert _is_rescue_fill(rotation_canned) is False  # canned rotation banter MUST be coloured
    assert _is_rescue_fill(canned_false) is False
    assert _is_rescue_fill(bare_error_key) is False  # a marker without the rescue flag is content
    assert _is_rescue_fill(flagged_fill) is True
    assert _is_rescue_fill(normal) is False
