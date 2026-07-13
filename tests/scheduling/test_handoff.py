"""Private queue-pair ownership for exact-once music-to-speech handoffs."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mammamiradio.core.models import Segment, SegmentType, StationState
from mammamiradio.scheduling.handoff import (
    HandoffPhase,
    PreparedMusicHandoff,
    cancel_active_music_handoff,
    commit_music_handoff,
    finish_handoff_segment,
    mark_handoff_segment_selected,
    peek_music_handoff_candidate,
    reconcile_handoff_queue_items,
)
from mammamiradio.web.mp3_frames import Mp3HandoffSplit


def _queue_items(queue: asyncio.Queue[Segment]) -> list[Segment]:
    """Inspect the synchronous backing deque used by queue-rewrite tests."""

    return list(queue._queue)  # type: ignore[attr-defined]


def _committed_pair(
    tmp_path: Path,
    *,
    original_ephemeral: bool = False,
    successor_type: SegmentType = SegmentType.BANTER,
) -> tuple[asyncio.Queue[Segment], StationState, Segment, Segment, Path, Path, Path]:
    """Return a committed music-head/speech-successor pair with real artifacts."""

    original = tmp_path / "song.mp3"
    head = tmp_path / "song_head.mp3"
    tail = tmp_path / "song_tail.mp3"
    original.write_bytes(b"full-song")
    head.write_bytes(b"head")
    tail.write_bytes(b"tail")
    music = Segment(
        type=SegmentType.MUSIC,
        path=original,
        duration_sec=180.0,
        metadata={"queue_id": "music", "artist": "Artista", "title_only": "Canzone"},
        ephemeral=original_ephemeral,
    )
    successor = Segment(
        type=successor_type,
        path=tmp_path / "host.mp3",
        duration_sec=12.0,
        metadata={"queue_id": "speech", "title": "Marco"},
    )
    successor.path.write_bytes(b"host")
    prepared = PreparedMusicHandoff(
        music_segment=music,
        source_path=original,
        split=Mp3HandoffSplit(
            head_path=head,
            tail_path=tail,
            playable_start_byte=0,
            head_end_byte=4,
            playable_end_byte=8,
            head_duration_sec=172.0,
            tail_duration_sec=8.0,
            source_duration_sec=180.0,
            frame_count=10,
            head_frame_count=9,
            tail_frame_count=1,
        ),
    )
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    queue.put_nowait(music)
    state = StationState()
    state.queued_segments = [{"id": "music", "duration_sec": 180.0}]

    assert commit_music_handoff(
        queue,
        state,
        prepared,
        successor,
        {"id": "speech", "duration_sec": 12.0},
    )
    return queue, state, music, successor, original, head, tail


@pytest.mark.parametrize(
    "successor_type",
    [
        pytest.param(SegmentType.BANTER, id="generated-banter"),
        pytest.param(SegmentType.BANTER, id="impossible-banter"),
        pytest.param(SegmentType.NEWS_FLASH, id="news-flash"),
        pytest.param(SegmentType.AD, id="ad-intro"),
    ],
)
def test_commit_replaces_only_the_queued_head_and_appends_tail_successor(
    tmp_path: Path,
    successor_type: SegmentType,
) -> None:
    """Every generated speech lane uses the same atomic music-tail pair."""

    queue, state, music, successor, original, head, _tail = _committed_pair(
        tmp_path,
        successor_type=successor_type,
    )

    assert _queue_items(queue) == [music, successor]
    assert music.path == head
    assert music.duration_sec == 172.0
    assert music.ephemeral is True
    assert successor.type is successor_type
    assert successor.metadata["has_music_tail"] is True
    assert music.handoff_id and music.handoff_id == successor.handoff_id
    assert original.exists(), "the complete egress source remains available until the pair retires"
    assert state.queued_segments == [
        {"id": "music", "duration_sec": 172.0},
        {"id": "speech", "duration_sec": 12.0},
    ]


def test_reconcile_restores_unstarted_music_when_successor_is_removed(tmp_path: Path) -> None:
    _queue, state, music, successor, original, head, tail = _committed_pair(tmp_path)
    tail.unlink()  # The renderer consumes this before the queue pair can be changed.
    items = [music]

    assert reconcile_handoff_queue_items(state, items) == []
    assert items == [music]
    assert music.path == original
    assert music.duration_sec == 180.0
    assert music.handoff_id is None
    assert successor.handoff_id is None
    assert not head.exists()
    assert not state.handoff_reservations


def test_reconcile_drops_successor_and_restores_music_when_pair_is_reordered(tmp_path: Path) -> None:
    _queue, state, music, successor, original, head, _tail = _committed_pair(tmp_path)
    unrelated = Segment(type=SegmentType.AD, path=tmp_path / "ad.mp3")
    items = [music, unrelated, successor]

    assert reconcile_handoff_queue_items(state, items) == [successor]
    assert items == [music, unrelated]
    assert music.path == original
    assert music.handoff_id is None
    assert not head.exists()


def test_reconcile_returns_successor_when_predecessor_is_removed(tmp_path: Path) -> None:
    _queue, state, music, successor, original, head, _tail = _committed_pair(tmp_path)
    items = [successor]

    assert reconcile_handoff_queue_items(state, items) == [successor]
    assert items == []
    assert music.path == head, "explicitly removed music is not silently reinserted"
    assert original.exists(), "a non-ephemeral source remains cache-owned"
    assert music.handoff_id is None
    assert successor.handoff_id is None
    assert not state.handoff_reservations


def test_clean_head_eof_protects_successor_across_front_rewrite(tmp_path: Path) -> None:
    _queue, state, music, successor, _original, _head, _tail = _committed_pair(tmp_path)
    reservation = next(iter(state.handoff_reservations.values()))
    forced = Segment(type=SegmentType.BANTER, path=tmp_path / "forced.mp3")

    assert reservation.phase is HandoffPhase.QUEUED
    mark_handoff_segment_selected(state, music)
    assert reservation.phase is HandoffPhase.HEAD_ACTIVE
    finish_handoff_segment(state, music, completed_cleanly=True)
    assert reservation.phase is HandoffPhase.SUCCESSOR_DUE

    items = [forced, successor]
    assert reconcile_handoff_queue_items(state, items) == []
    assert items == [successor, forced]
    assert reservation.phase is HandoffPhase.SUCCESSOR_DUE

    mark_handoff_segment_selected(state, successor)
    assert reservation.phase is HandoffPhase.SUCCESSOR_ACTIVE
    finish_handoff_segment(state, successor, completed_cleanly=True)
    assert not state.handoff_reservations


def test_explicit_removal_of_due_successor_retires_without_music_restore(tmp_path: Path) -> None:
    _queue, state, music, successor, original, head, _tail = _committed_pair(tmp_path)
    mark_handoff_segment_selected(state, music)
    finish_handoff_segment(state, music, completed_cleanly=True)

    items: list[Segment] = []
    assert reconcile_handoff_queue_items(state, items) == []
    assert music.path == head
    assert original.exists()
    assert successor.handoff_id is None
    assert not state.handoff_reservations


def test_skip_of_active_music_head_discards_only_its_successor(tmp_path: Path) -> None:
    _queue, state, music, successor, original, _head, _tail = _committed_pair(
        tmp_path,
        original_ephemeral=True,
    )
    state.active_playback_segment = music
    items = [successor]

    assert cancel_active_music_handoff(state, items) == [successor]
    assert items == []
    assert music.path.name == "song_head.mp3", "an already-airing head can never be restored"
    assert not original.exists(), "the abandoned ephemeral full source is released"
    assert not state.handoff_reservations


def test_only_normal_unstarted_music_can_supply_a_handoff_tail(tmp_path: Path) -> None:
    queue: asyncio.Queue[Segment] = asyncio.Queue()
    rescue = Segment(
        type=SegmentType.MUSIC,
        path=tmp_path / "rescue.mp3",
        metadata={"rescue": True},
    )
    rescue.path.write_bytes(b"rescue")
    queue.put_nowait(rescue)
    assert peek_music_handoff_candidate(queue) is None

    song = Segment(type=SegmentType.MUSIC, path=tmp_path / "song.mp3")
    song.path.write_bytes(b"song")
    queue.get_nowait()
    queue.task_done()
    queue.put_nowait(song)
    assert peek_music_handoff_candidate(queue) is song


def test_commit_fails_closed_when_queued_egress_path_changes_after_split(tmp_path: Path) -> None:
    source = tmp_path / "song.mp3"
    head = tmp_path / "head.mp3"
    tail = tmp_path / "tail.mp3"
    successor_path = tmp_path / "host.mp3"
    source.write_bytes(b"original-egress")
    head.write_bytes(b"head")
    tail.write_bytes(b"tail")
    successor_path.write_bytes(b"host")
    music = Segment(type=SegmentType.MUSIC, path=source, duration_sec=180.0)
    successor = Segment(type=SegmentType.BANTER, path=successor_path)
    before = source.stat()
    prepared = PreparedMusicHandoff(
        music_segment=music,
        source_path=source,
        split=Mp3HandoffSplit(
            head_path=head,
            tail_path=tail,
            playable_start_byte=0,
            head_end_byte=4,
            playable_end_byte=8,
            head_duration_sec=172.0,
            tail_duration_sec=8.0,
            source_duration_sec=180.0,
            frame_count=10,
            head_frame_count=9,
            tail_frame_count=1,
        ),
        source_size=before.st_size,
        source_mtime_ns=before.st_mtime_ns,
    )
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=2)
    queue.put_nowait(music)
    source.write_bytes(b"new-egress-bytes-at-the-same-path")

    assert commit_music_handoff(queue, StationState(), prepared, successor, {}) is False
    assert _queue_items(queue) == [music]
    assert music.path == source
    assert successor.handoff_id is None
