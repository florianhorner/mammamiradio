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
    """Minimal stand-in carrying just the tmp_dir the egress pass needs."""

    def __init__(self, tmp_dir: Path):
        self.tmp_dir = tmp_dir


def _colour(in_path: Path, out_path: Path) -> bool:
    """Stand-in for apply_broadcast_chain: writes the coloured output, reports success."""
    out_path.write_bytes(b"COLOURED")
    return True


def _seg(tmp_path: Path, *, metadata: dict, ephemeral: bool = True, name: str = "seg.mp3") -> Segment:
    path = tmp_path / name
    path.write_bytes(b"AUDIO")
    return Segment(type=SegmentType.MUSIC, path=path, metadata=metadata, ephemeral=ephemeral)


# ── Scenario 2 & 3: rescue / bridge / canned fills must skip the pass ──────────


@pytest.mark.parametrize(
    "skip_key",
    [
        "error",
        "queue_drain_recovery",
        "silence_fallback",
        "error_recovery",
        "resume_bridge",
        "idle_bridge",
        "warmup",
        "canned",
        "recycled",
    ],
)
async def test_apply_egress_skips_rescue_and_canned_fills(tmp_path, skip_key):
    """Every emergency / bridge / rescue / canned marker bypasses the egress pass so a
    dead-air rescue is never delayed by an ffmpeg encode (INSTANT AUDIO)."""
    seg = _seg(tmp_path, metadata={skip_key: True})
    with patch(f"{PRODUCER_MODULE}.apply_broadcast_chain") as m_chain:
        out = await _apply_egress(seg, _Cfg(tmp_path))
    m_chain.assert_not_called()
    assert out is seg  # returned untouched, same object


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


async def test_apply_egress_preserves_cache_file(tmp_path):
    """A norm-cache hit (non-ephemeral, outside tmp_dir) is colour-copied but the
    CACHE file itself is left intact — the colouring pass never corrupts the cache."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    cache_file = cache_dir / "norm_song.mp3"
    cache_file.write_bytes(b"CACHED")
    seg = Segment(type=SegmentType.MUSIC, path=cache_file, metadata={"title": "S"}, ephemeral=False)
    with patch(f"{PRODUCER_MODULE}.apply_broadcast_chain", side_effect=_colour):
        out = await _apply_egress(seg, _Cfg(tmp_dir))
    assert cache_file.exists()  # cache preserved
    assert cache_file.read_bytes() == b"CACHED"
    assert out.path != cache_file and out.ephemeral is True


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
        ok = await _enqueue_with_egress(
            queue, state, _Cfg(tmp_path), seg, front_insert=True, shadow_entry={"id": "x"}
        )
    assert ok is True
    m_chain.assert_called_once()
    inserted_segment = m_front.call_args[0][2]  # (queue, state, segment, shadow_entry)
    assert inserted_segment.path.name.startswith("egress_")  # coloured before insert


async def test_enqueue_with_egress_rescue_skips_chain(tmp_path):
    """A rescue segment routed through the funnel is enqueued as-is, never coloured."""
    seg = _seg(tmp_path, metadata={"canned": True, "queue_drain_recovery": True})
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    state = StationState()
    with patch(f"{PRODUCER_MODULE}.apply_broadcast_chain") as m_chain:
        await _enqueue_with_egress(queue, state, _Cfg(tmp_path), seg)
    m_chain.assert_not_called()
    assert queue.get_nowait() is seg  # the original bridge audio, undelayed


def test_egress_skip_keys_cover_known_rescue_markers():
    """Floor: the skip set must include every rescue/bridge/canned marker the producer
    stamps. A bridge type whose marker is dropped from the set would get an FX delay on
    the dead-air rescue path."""
    assert producer._EGRESS_SKIP_KEYS.issuperset(
        {
            "error",
            "queue_drain_recovery",
            "silence_fallback",
            "error_recovery",
            "resume_bridge",
            "idle_bridge",
            "warmup",
            "canned",
            "recycled",
        }
    )


def test_is_rescue_fill_uses_truthiness_not_presence():
    """The skip decision is truthiness, not key presence — the contract that keeps
    ``canned=False`` banter out of the skip path while ``canned=True`` fills skip."""
    banter = Segment(type=SegmentType.BANTER, path=Path("/x.mp3"), metadata={"canned": False})
    canned = Segment(type=SegmentType.BANTER, path=Path("/x.mp3"), metadata={"canned": True})
    error = Segment(type=SegmentType.MUSIC, path=Path("/x.mp3"), metadata={"error": "boom"})
    normal = Segment(type=SegmentType.MUSIC, path=Path("/x.mp3"), metadata={"title": "Song"})
    assert _is_rescue_fill(banter) is False  # live banter is NOT a fill — must be coloured
    assert _is_rescue_fill(canned) is True
    assert _is_rescue_fill(error) is True
    assert _is_rescue_fill(normal) is False
