"""Behavioral contract for synchronous playback-queue mutations."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mammamiradio.core.models import GenerationWasteReason, Segment, SegmentType, StationState
from mammamiradio.home.moment_receipts import MomentStore
from mammamiradio.scheduling import queue_mutations
from mammamiradio.scheduling.queue_mutations import drop_matching_segments


def _segment(
    queue_id: str,
    *,
    segment_type: SegmentType = SegmentType.MUSIC,
    path: Path | None = None,
    ephemeral: bool = False,
    metadata: dict | None = None,
) -> Segment:
    segment_metadata = {"queue_id": queue_id, "title": queue_id}
    segment_metadata.update(metadata or {})
    return Segment(
        type=segment_type,
        path=path or Path(f"/tmp/{queue_id}.mp3"),
        duration_sec=30.0,
        ephemeral=ephemeral,
        metadata=segment_metadata,
    )


def _fill(queue: asyncio.Queue[Segment], state: StationState, segments: list[Segment]) -> None:
    for segment in segments:
        queue.put_nowait(segment)
        state.queued_segments.append({"id": segment.metadata["queue_id"], "label": segment.metadata["title"]})


def _queue_ids(queue: asyncio.Queue[Segment]) -> list[str]:
    queued: list[Segment] = []
    while not queue.empty():
        segment = queue.get_nowait()
        queue.task_done()
        queued.append(segment)
    for segment in queued:
        queue.put_nowait(segment)
    return [segment.metadata["queue_id"] for segment in queued]


@pytest.mark.parametrize("drop_id", ["head", "middle", "tail"])
def test_drop_at_each_position_preserves_unrelated_order_and_shadow(drop_id: str) -> None:
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=3)
    state = StationState()
    _fill(queue, state, [_segment("head"), _segment("middle"), _segment("tail")])

    dropped = drop_matching_segments(
        queue,
        state,
        should_drop=lambda segment: segment.metadata["queue_id"] == drop_id,
        reason=GenerationWasteReason.OPERATOR_BAN,
    )

    expected = [queue_id for queue_id in ("head", "middle", "tail") if queue_id != drop_id]
    assert dropped == 1
    assert _queue_ids(queue) == expected
    assert [entry["id"] for entry in state.queued_segments] == expected
    assert state.discard_by_reason == {GenerationWasteReason.OPERATOR_BAN: 1}


@pytest.mark.asyncio
async def test_full_queue_rebuild_balances_join_accounting_and_leaves_airing_work_untouched() -> None:
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=3)
    state = StationState()
    airing = _segment("airing")
    queue.put_nowait(airing)
    assert queue.get_nowait() is airing  # Playback owns it; it is no longer mutable queue work.
    state.now_streaming = {"type": "music", "metadata": {"queue_id": "airing"}}

    queued = [_segment("keep-a"), _segment("drop"), _segment("keep-b")]
    _fill(queue, state, queued)
    before_now = dict(state.now_streaming)

    assert (
        drop_matching_segments(
            queue,
            state,
            should_drop=lambda segment: segment.metadata["queue_id"] == "drop",
            reason=GenerationWasteReason.OPERATOR_BAN,
        )
        == 1
    )
    assert _queue_ids(queue) == ["keep-a", "keep-b"]
    assert state.now_streaming == before_now

    join = asyncio.create_task(queue.join())
    await asyncio.sleep(0)
    assert not join.done()  # The airing item and both survivors are still unfinished.
    queue.task_done()  # airing
    for _ in range(2):
        queue.get_nowait()
        queue.task_done()
    await asyncio.wait_for(join, timeout=0.1)


def test_drop_settles_receipts_and_removes_only_discarded_ephemeral_render(tmp_path: Path) -> None:
    queue: asyncio.Queue[Segment] = asyncio.Queue()
    state = StationState()
    store = MomentStore()
    state.moment_store = store
    moment_id = store.record(lane="directive", family="morning_launch", public_label="Morning launch")
    dropped_path = tmp_path / "drop.mp3"
    dropped_path.write_bytes(b"drop")
    survivor_path = tmp_path / "keep.mp3"
    survivor_path.write_bytes(b"keep")
    dropped_segment = _segment(
        "drop",
        segment_type=SegmentType.BANTER,
        path=dropped_path,
        ephemeral=True,
        metadata={"ritual_moment_id": moment_id},
    )
    survivor = _segment("keep", path=survivor_path, ephemeral=True)
    _fill(queue, state, [dropped_segment, survivor])

    drop_matching_segments(
        queue,
        state,
        should_drop=lambda segment: segment.metadata["queue_id"] == "drop",
        reason=GenerationWasteReason.OPERATOR_PURGE,
    )

    assert not dropped_path.exists()
    assert survivor_path.exists()
    assert store.rows[0].status == "dropped"
    assert store.rows[0].drop_reason == GenerationWasteReason.OPERATOR_PURGE


def test_drop_preserves_packaged_asset_even_when_marked_ephemeral(tmp_path: Path, monkeypatch) -> None:
    demo_root = tmp_path / "assets" / "demo"
    packaged = demo_root / "recovery" / "continuity_1.mp3"
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(b"package data")
    monkeypatch.setattr(queue_mutations, "_DEMO_ASSETS_DIR", demo_root)
    queue: asyncio.Queue[Segment] = asyncio.Queue()
    state = StationState()
    _fill(queue, state, [_segment("drop", path=packaged, ephemeral=True)])

    assert (
        drop_matching_segments(
            queue,
            state,
            should_drop=lambda _segment: True,
            reason=GenerationWasteReason.OPERATOR_BAN,
        )
        == 1
    )
    assert packaged.exists()


def test_drop_preserves_protected_runway_and_out_of_band_continuity_slot() -> None:
    """The generic queue seam must not weaken #829's continuity reservation."""
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=3)
    state = StationState()
    protected = _segment("protected", metadata={"continuity_reservation": True})
    dropped = _segment("drop")
    survivor = _segment("survivor")
    continuity_slot = _segment("slot", metadata={"continuity_reservation": True})
    state.continuity_slot = continuity_slot
    _fill(queue, state, [protected, dropped, survivor])

    assert (
        drop_matching_segments(
            queue,
            state,
            should_drop=lambda segment: segment.metadata["queue_id"] == "drop",
            reason=GenerationWasteReason.OPERATOR_BAN,
        )
        == 1
    )

    assert _queue_ids(queue) == ["protected", "survivor"]
    assert [entry["id"] for entry in state.queued_segments] == ["protected", "survivor"]
    assert state.continuity_slot is continuity_slot


@pytest.mark.asyncio
async def test_predicate_failure_restores_original_queue_and_accounting() -> None:
    queue: asyncio.Queue[Segment] = asyncio.Queue()
    state = StationState()
    segments = [_segment("one"), _segment("two")]
    _fill(queue, state, segments)

    def _fail_on_second(segment: Segment) -> bool:
        if segment.metadata["queue_id"] == "two":
            raise RuntimeError("broken predicate")
        return True

    with pytest.raises(RuntimeError, match="broken predicate"):
        drop_matching_segments(
            queue,
            state,
            should_drop=_fail_on_second,
            reason=GenerationWasteReason.OPERATOR_BAN,
        )

    assert _queue_ids(queue) == ["one", "two"]
    assert [entry["id"] for entry in state.queued_segments] == ["one", "two"]
    assert state.discarded_segments_total == 0
    for _ in segments:
        queue.get_nowait()
        queue.task_done()
    await asyncio.wait_for(queue.join(), timeout=0.1)
