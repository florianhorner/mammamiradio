"""Air-next front-insert: an operator-triggered segment airs at the next boundary.

Covers the three mandatory audio-delivery scenarios for the queue mutation:
  1. Normal — front-insert past a buffered queue, segment airs next.
  2. Empty fallback — front-insert into an empty queue (no crash, airs next).
  3. Post-restart/stopped — a stop mid-build drops the segment, never airs it.
Plus the bounded-queue tail-drop (the dead-air landmine) and shadow consistency.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from mammamiradio.core.models import GenerationWasteReason, Segment, SegmentType, StationState
from mammamiradio.scheduling import producer
from mammamiradio.scheduling.producer import _front_insert_queue_and_shadow


def _seg(label: str, *, ephemeral: bool = False, seg_type: SegmentType = SegmentType.MUSIC) -> Segment:
    return Segment(type=seg_type, path=Path(f"/tmp/{label}.mp3"), metadata={"title": label}, ephemeral=ephemeral)


def _shadow(label: str, seg_type: str = "banter") -> dict:
    return {"id": label, "type": seg_type, "label": label, "duration_sec": 10.0}


def test_front_insert_airs_next_past_buffered_queue():
    """Scenario 1: with songs buffered, the forced segment becomes the NEXT get
    (FIFO front) and the next shadow entry — not minutes behind the queue."""
    q: asyncio.Queue = asyncio.Queue(maxsize=5)
    state = StationState()
    song1, song2 = _seg("song1"), _seg("song2")
    q.put_nowait(song1)
    q.put_nowait(song2)
    state.queued_segments = [_shadow("song1", "music"), _shadow("song2", "music")]

    banter = _seg("banter", seg_type=SegmentType.BANTER)
    state.operator_force_pending = SegmentType.BANTER  # an operator trigger is in flight
    assert _front_insert_queue_and_shadow(q, state, banter, _shadow("banter")) is True

    # Queuing the pick fulfils the trigger, so the in-flight guard clears here (not at
    # render-start) — that is what keeps a second tap rejected for the whole render so
    # it can't be front-inserted ahead of this pick.
    assert state.operator_force_pending is None
    assert q.qsize() == 3
    assert len(state.queued_segments) == 3
    assert state.queued_segments[0]["id"] == "banter"  # shows as next in the panel
    assert q.get_nowait() is banter  # FIFO front => airs next
    assert q.get_nowait() is song1  # original order preserved behind it


def test_front_insert_into_empty_queue():
    """Scenario 2 (empty fallback): front-insert into an empty queue just queues
    the segment — no crash, airs next."""
    q: asyncio.Queue = asyncio.Queue(maxsize=5)
    state = StationState()
    banter = _seg("banter", seg_type=SegmentType.BANTER)

    assert _front_insert_queue_and_shadow(q, state, banter, _shadow("banter")) is True
    assert q.qsize() == 1
    assert q.get_nowait() is banter
    assert state.queued_segments == [_shadow("banter")]


def test_front_insert_dropped_when_session_stopped(tmp_path):
    """Scenario 3 (post-restart/stopped): a stop mid-build drops the forced segment
    — it never airs, the queue/shadow are untouched, and its temp is cleaned up. The
    one-at-a-time guard is also released so the operator can retry after resume instead
    of being locked out until restart."""
    q: asyncio.Queue = asyncio.Queue(maxsize=5)
    state = StationState()
    state.session_stopped = True
    state.operator_force_pending = SegmentType.BANTER  # a trigger was in flight when the stop landed
    f = tmp_path / "banter.mp3"
    f.write_bytes(b"x")
    banter = Segment(type=SegmentType.BANTER, path=f, metadata={}, ephemeral=True)

    assert _front_insert_queue_and_shadow(q, state, banter, _shadow("banter")) is False
    assert q.qsize() == 0
    assert state.queued_segments == []
    assert not f.exists()  # ephemeral temp unlinked, no leak
    assert state.operator_force_pending is None  # guard released — operator can retry after resume
    assert state.discarded_segments_total == 1
    assert state.discard_by_reason == {GenerationWasteReason.SESSION_STOPPED: 1}


def test_front_insert_stopped_keeps_packaged_asset_even_if_ephemeral(tmp_path):
    """The stopped-discard path must not delete packaged demo assets."""
    demo_root = tmp_path / "assets" / "demo"
    packaged = demo_root / "recovery" / "continuity_1.mp3"
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(b"\x00" * 2048)
    q: asyncio.Queue = asyncio.Queue(maxsize=5)
    state = StationState(session_stopped=True, operator_force_pending=SegmentType.BANTER)
    banter = Segment(type=SegmentType.BANTER, path=packaged, metadata={}, ephemeral=True)

    with patch.object(producer, "_DEMO_ASSETS_DIR", demo_root):
        assert _front_insert_queue_and_shadow(q, state, banter, _shadow("banter")) is False

    assert packaged.exists()


def test_front_insert_drops_furthest_future_tail_on_full_queue():
    """The dead-air landmine: on a full bounded queue a front-insert would push
    N+1 and raise QueueFull. Instead the furthest-future tail is dropped so the
    queue stays at maxsize, the forced segment airs next, and the shadow stays
    <= the real queue (so the one-directional drift guard never false-alarms)."""
    q: asyncio.Queue = asyncio.Queue(maxsize=3)
    state = StationState()
    songs = [_seg(f"s{i}") for i in range(3)]
    for s in songs:
        q.put_nowait(s)
    state.queued_segments = [_shadow(f"s{i}", "music") for i in range(3)]

    banter = _seg("banter", seg_type=SegmentType.BANTER)
    assert _front_insert_queue_and_shadow(q, state, banter, _shadow("banter")) is True

    assert q.qsize() == 3  # never exceeds maxsize -> no QueueFull
    got = [q.get_nowait() for _ in range(3)]
    assert got == [banter, songs[0], songs[1]]  # banter next; furthest-future s2 dropped
    assert len(state.queued_segments) <= q.maxsize  # shadow never exceeds the queue
    assert state.queued_segments[0]["id"] == "banter"


def test_front_insert_unlinks_dropped_ephemeral_tail(tmp_path):
    """A dropped tail render that is ephemeral is unlinked (no temp leak); a
    non-ephemeral one is left on disk to be re-queued later."""
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    state = StationState()
    f0 = tmp_path / "s0.mp3"
    f0.write_bytes(b"x")
    f1 = tmp_path / "s1.mp3"
    f1.write_bytes(b"x")
    s0 = Segment(type=SegmentType.MUSIC, path=f0, metadata={}, ephemeral=False)
    s1 = Segment(type=SegmentType.MUSIC, path=f1, metadata={}, ephemeral=True)
    q.put_nowait(s0)
    q.put_nowait(s1)
    state.queued_segments = [_shadow("s0", "music"), _shadow("s1", "music")]

    banter = _seg("banter", seg_type=SegmentType.BANTER)
    _front_insert_queue_and_shadow(q, state, banter, _shadow("banter"))

    assert not f1.exists()  # dropped ephemeral tail unlinked
    assert f0.exists()  # non-ephemeral survivor kept
    assert state.discarded_segments_total == 1
    assert state.discard_by_reason == {GenerationWasteReason.AIR_NEXT_OVERFLOW: 1}


def test_front_insert_overflow_keeps_packaged_asset_even_if_ephemeral(tmp_path):
    """Overflow tail cleanup also respects the packaged asset guard."""
    demo_root = tmp_path / "assets" / "demo"
    packaged = demo_root / "recovery" / "continuity_1.mp3"
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(b"\x00" * 2048)
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    state = StationState()
    tail = Segment(type=SegmentType.BANTER, path=packaged, metadata={}, ephemeral=True)
    q.put_nowait(tail)
    state.queued_segments = [_shadow("tail")]

    front = _seg("front", seg_type=SegmentType.BANTER)
    with patch.object(producer, "_DEMO_ASSETS_DIR", demo_root):
        assert _front_insert_queue_and_shadow(q, state, front, _shadow("front")) is True

    assert packaged.exists()
