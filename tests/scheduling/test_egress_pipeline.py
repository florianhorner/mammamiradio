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
from mammamiradio.scheduling.producer import _apply_egress, _enqueue_with_egress, _is_rescue_fill

PRODUCER_MODULE = "mammamiradio.scheduling.producer"


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
    {"error_recovery": True, "rescue": True},  # error-recovery canned
    {"error": "boom", "rescue": True},  # brief-silence error segment
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
    baked = cfg.cache_dir / f"fm_{seg.path.stem}_v1.mp3"
    baked.write_bytes(b"COLOURED-FROM-PRIOR-RUN")  # as if a prior run produced it
    with (
        patch(f"{PRODUCER_MODULE}.broadcast_chain_version", return_value="v1"),
        patch(f"{PRODUCER_MODULE}.apply_broadcast_chain") as m_chain,
    ):
        out = await _apply_egress(seg, cfg)
    m_chain.assert_not_called()  # no encode — reused the persisted bake
    assert out.path == baked and out.ephemeral is False


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
