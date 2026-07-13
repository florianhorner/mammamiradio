"""Control-plane cleanup for committed exact-once music/speech pairs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.core.config import load_config
from mammamiradio.core.models import GenerationWasteReason, Segment, SegmentType, StationState
from mammamiradio.scheduling.handoff import (
    PreparedMusicHandoff,
    commit_music_handoff,
    finish_handoff_segment,
    mark_handoff_segment_selected,
)
from mammamiradio.scheduling.producer import _front_insert_queue_and_shadow
from mammamiradio.web.mp3_frames import Mp3HandoffSplit
from mammamiradio.web.streamer import (
    LiveStreamHub,
    _cancel_active_handoff_from_queue,
    _purge_blocklisted_from_queue,
    _purge_home_fact_banter_from_queue,
    _purge_queue_and_shadow,
    _request_skip,
    _reserve_continuity_runway,
    router,
)

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def _queue_items(queue: asyncio.Queue[Segment]) -> list[Segment]:
    """Inspect the synchronous backing deque used by queue-rewrite tests."""

    return list(queue._queue)  # type: ignore[attr-defined]


def _pair(
    tmp_path: Path,
    *,
    maxsize: int = 4,
    original_name: str = "song.mp3",
) -> tuple[asyncio.Queue[Segment], StationState, Segment, Segment, Path, Path]:
    original = tmp_path / original_name
    head = tmp_path / "song_head.mp3"
    tail = tmp_path / "song_tail.mp3"
    voice = tmp_path / "voice.mp3"
    for path, payload in ((original, b"song"), (head, b"head"), (tail, b"tail"), (voice, b"voice")):
        path.write_bytes(payload)
    music = Segment(
        type=SegmentType.MUSIC,
        path=original,
        duration_sec=180.0,
        metadata={"queue_id": "music", "artist": "artist", "title_only": "song"},
    )
    successor = Segment(
        type=SegmentType.BANTER,
        path=voice,
        duration_sec=10.0,
        metadata={"queue_id": "speech", "title": "Host break"},
    )
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
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=maxsize)
    queue.put_nowait(music)
    state = StationState()
    state.queued_segments = [{"id": "music", "duration_sec": 180.0}]
    assert commit_music_handoff(
        queue,
        state,
        prepared,
        successor,
        {"id": "speech", "label": "Host break", "duration_sec": 10.0},
    )
    return queue, state, music, successor, original, head


def test_front_insert_discards_tail_successor_and_restores_unstarted_music(tmp_path: Path) -> None:
    queue, state, music, successor, original, head = _pair(tmp_path, maxsize=2)
    forced = Segment(type=SegmentType.BANTER, path=tmp_path / "forced.mp3", metadata={"title": "Air next"})
    forced.path.write_bytes(b"forced")

    assert _front_insert_queue_and_shadow(queue, state, forced, {"id": "forced", "duration_sec": 1.0})

    assert _queue_items(queue) == [forced, music]
    assert music.path == original
    assert not head.exists()
    assert successor.path.exists() is False
    assert not state.handoff_reservations


def test_air_next_after_clean_head_keeps_due_successor_first(tmp_path: Path) -> None:
    queue, state, music, successor, _original, _head = _pair(tmp_path, maxsize=3)
    successor.metadata["transition_track_ref"] = "youtube|song"
    forced = Segment(
        type=SegmentType.BANTER,
        path=tmp_path / "forced.mp3",
        metadata={"title": "Air next"},
    )
    forced.path.write_bytes(b"forced")

    assert queue.get_nowait() is music
    queue.task_done()
    state.queued_segments.pop(0)
    state.active_playback_segment = music
    mark_handoff_segment_selected(state, music)
    finish_handoff_segment(state, music, completed_cleanly=True)
    state.active_playback_segment = None

    assert _front_insert_queue_and_shadow(queue, state, forced, {"id": "forced", "duration_sec": 1.0})

    assert _queue_items(queue) == [successor, forced]
    assert successor.path.exists()
    assert state.handoff_reservations


def test_full_queue_air_next_never_evicts_due_successor(tmp_path: Path) -> None:
    """Capacity planning treats a committed outro as ownership, not ordinary speech."""

    queue, state, music, successor, _original, _head = _pair(tmp_path, maxsize=3)
    assert queue.get_nowait() is music
    queue.task_done()
    state.queued_segments.pop(0)
    state.active_playback_segment = music
    mark_handoff_segment_selected(state, music)
    finish_handoff_segment(state, music, completed_cleanly=True)
    state.active_playback_segment = None
    # Retain the legacy adjacency marker to prove its generic stale-head rule
    # cannot override the stronger committed-handoff ownership signal.
    successor.metadata["transition_track_ref"] = "youtube|song"

    continuity = Segment(
        type=SegmentType.BANTER,
        path=tmp_path / "continuity.mp3",
        duration_sec=30.0,
        metadata={"queue_id": "continuity", "continuity_reservation": True},
    )
    old_air_next = Segment(
        type=SegmentType.BANTER,
        path=tmp_path / "old-air-next.mp3",
        duration_sec=2.0,
        metadata={"queue_id": "old-air-next", "air_next": True},
    )
    forced = Segment(
        type=SegmentType.BANTER,
        path=tmp_path / "new-air-next.mp3",
        duration_sec=2.0,
        metadata={"title": "New Air Next"},
    )
    for segment in (continuity, old_air_next, forced):
        segment.path.write_bytes(segment.path.stem.encode())
    for segment in (continuity, old_air_next):
        queue.put_nowait(segment)
        state.queued_segments.append({"id": segment.metadata["queue_id"], "duration_sec": segment.duration_sec})

    assert _front_insert_queue_and_shadow(
        queue,
        state,
        forced,
        {"id": "new-air-next", "duration_sec": forced.duration_sec},
    )

    assert _queue_items(queue) == [successor, forced, old_air_next]
    assert state.continuity_slot is continuity
    assert successor.path.exists()
    assert state.handoff_reservations


def test_full_queue_continuity_reservation_never_evicts_due_successor(tmp_path: Path) -> None:
    queue, state, music, successor, _original, _head = _pair(tmp_path, maxsize=2)
    assert queue.get_nowait() is music
    queue.task_done()
    state.queued_segments.pop(0)
    state.active_playback_segment = music
    mark_handoff_segment_selected(state, music)
    finish_handoff_segment(state, music, completed_cleanly=True)
    state.active_playback_segment = None

    old_air_next = Segment(
        type=SegmentType.BANTER,
        path=tmp_path / "old-air-next.mp3",
        duration_sec=2.0,
        metadata={"queue_id": "old-air-next", "air_next": True},
    )
    runway = Segment(
        type=SegmentType.BANTER,
        path=tmp_path / "runway.mp3",
        duration_sec=120.0,
        metadata={"queue_id": "runway", "continuity_reservation": True},
    )
    for segment in (old_air_next, runway):
        segment.path.write_bytes(segment.path.stem.encode())
    queue.put_nowait(old_air_next)
    state.queued_segments.append({"id": "old-air-next", "duration_sec": old_air_next.duration_sec})

    with (
        patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 240.0),
        patch("mammamiradio.web.streamer._continuity_reservation_segments", return_value=[runway]),
    ):
        assert _reserve_continuity_runway(SimpleNamespace(queue=queue), state, SimpleNamespace()) == 0

    assert _queue_items(queue) == [successor, old_air_next]
    assert state.continuity_slot is runway
    assert successor.path.exists()
    assert state.handoff_reservations


def test_live_handoff_original_and_canonical_copy_are_excluded_from_ordinary_runway(tmp_path: Path) -> None:
    queue, state, music, successor, original, _head = _pair(
        tmp_path,
        maxsize=6,
        original_name="norm_original.mp3",
    )
    assert queue.get_nowait() is music
    queue.task_done()
    state.queued_segments.pop(0)
    state.active_playback_segment = music
    mark_handoff_segment_selected(state, music)

    later_path = tmp_path / "norm_later.mp3"
    duplicate_original = tmp_path / "norm_original_copy.mp3"
    later_path.write_bytes(b"later")
    duplicate_original.write_bytes(b"same song, different cache path")
    (tmp_path / "norm_original_copy.mp3.json").write_text(
        '{"artist": "artist", "title": "song"}',
        encoding="utf-8",
    )
    later = Segment(
        type=SegmentType.MUSIC,
        path=later_path,
        duration_sec=100.0,
        metadata={"queue_id": "later", "artist": "other artist", "title_only": "later song"},
        ephemeral=False,
    )
    queue.put_nowait(later)
    state.queued_segments.append({"id": "later", "duration_sec": 100.0})
    state.last_music_file = later_path
    state.immediate_audio_index = {original: 180.0, duplicate_original: 180.0, later_path: 100.0}

    config = load_config(TOML_PATH)
    config.cache_dir = tmp_path
    with patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 240.0):
        _reserve_continuity_runway(SimpleNamespace(queue=queue), state, config)

    queued_paths = [segment.path for segment in _queue_items(queue)]
    assert queued_paths[:2] == [successor.path, later_path]
    assert original not in queued_paths
    assert duplicate_original not in queued_paths
    assert state.handoff_reservations


@pytest.mark.asyncio
async def test_skip_carries_handoff_original_exclusions_across_cancellation(tmp_path: Path) -> None:
    queue, state, music, successor, original, _head = _pair(tmp_path, maxsize=4)
    assert queue.get_nowait() is music
    queue.task_done()
    state.queued_segments.pop(0)
    state.active_playback_segment = music
    mark_handoff_segment_selected(state, music)
    state.now_streaming = {
        "type": "music",
        "label": "Artist - Song",
        "started": 0.0,
        "metadata": {"artist": "artist", "title_only": "song"},
    }
    app_state = SimpleNamespace(queue=queue, skip_event=asyncio.Event())
    reserve = patch("mammamiradio.web.streamer._reserve_continuity_runway", return_value=0)
    persist = patch("mammamiradio.web.streamer._persist_skipped_music", new_callable=AsyncMock)

    with reserve as reserve_mock, persist:
        await _request_skip(app_state, state, load_config(TOML_PATH), source="test")

    assert successor not in _queue_items(queue)
    assert not state.handoff_reservations
    assert reserve_mock.call_args.kwargs["excluded_paths"] == {original}
    assert reserve_mock.call_args.kwargs["excluded_track_keys"] == {("artist", "song")}


def test_ban_removes_orphaned_successor_when_its_music_head_is_dropped(tmp_path: Path) -> None:
    queue, state, _music, successor, _original, _head = _pair(tmp_path)

    assert _purge_blocklisted_from_queue(queue, state, {("artist", "song")}) == 2

    assert queue.empty()
    assert not successor.path.exists()
    assert not state.handoff_reservations


def test_home_fact_queue_removal_restores_unstarted_music_and_removes_tail_successor(tmp_path: Path) -> None:
    queue, state, music, successor, original, head = _pair(tmp_path)
    successor.metadata["home_fact_entity_id"] = "sensor.kitchen"

    assert _purge_home_fact_banter_from_queue(queue, state, "sensor.kitchen") == 1

    assert _queue_items(queue) == [music]
    assert music.path == original
    assert not head.exists()
    assert not successor.path.exists()
    assert not state.handoff_reservations


def test_skip_of_active_head_cleans_queued_successor(tmp_path: Path) -> None:
    queue, state, music, successor, _original, _head = _pair(tmp_path)
    assert queue.get_nowait() is music
    queue.task_done()
    state.queued_segments.pop(0)
    state.active_playback_segment = music

    assert _cancel_active_handoff_from_queue(queue, state, reason=GenerationWasteReason.OPERATOR_PURGE) == 1

    assert queue.empty()
    assert not successor.path.exists()
    assert not state.handoff_reservations


def test_purge_releases_pair_when_current_head_is_already_airing(tmp_path: Path) -> None:
    queue, state, music, successor, _original, _head = _pair(tmp_path)
    assert queue.get_nowait() is music
    queue.task_done()
    state.active_playback_segment = music

    assert _purge_queue_and_shadow(queue, state, reason=GenerationWasteReason.OPERATOR_PURGE) == 1

    assert queue.empty()
    assert not successor.path.exists()
    assert not state.handoff_reservations


def test_source_or_chaos_replacement_releases_active_pair_before_new_runway(tmp_path: Path) -> None:
    queue, state, music, successor, _original, _head = _pair(tmp_path)
    assert queue.get_nowait() is music
    queue.task_done()
    state.queued_segments.pop(0)
    state.active_playback_segment = music
    runway = Segment(type=SegmentType.BANTER, path=tmp_path / "runway.mp3", duration_sec=4.0)
    runway.path.write_bytes(b"runway")
    app_state = SimpleNamespace(queue=queue)

    with patch("mammamiradio.web.streamer._continuity_reservation_segments", return_value=[runway]):
        assert (
            _reserve_continuity_runway(
                app_state,
                state,
                SimpleNamespace(),
                replace_queue=True,
                discard_reason=GenerationWasteReason.SOURCE_SWITCH,
            )
            == 1
        )

    assert _queue_items(queue) == [runway]
    assert not successor.path.exists()
    assert not state.handoff_reservations


@pytest.mark.asyncio
async def test_queue_remove_endpoint_restores_unstarted_predecessor(tmp_path: Path) -> None:
    queue, state, music, successor, original, head = _pair(tmp_path)
    config = load_config(TOML_PATH)
    config.cache_dir = tmp_path
    app = FastAPI()
    app.include_router(router)
    app.state.queue = queue
    app.state.skip_event = asyncio.Event()
    app.state.station_state = state
    app.state.config = config
    app.state.stream_hub = LiveStreamHub()
    app.state.start_time = 0.0

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer._reserve_continuity_runway", return_value=0):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/queue/remove", json={"id": "speech"})

    assert response.status_code == 200
    assert response.json()["removed"] == "Host break"
    assert _queue_items(queue) == [music]
    assert music.path == original
    assert not head.exists()
    assert not successor.path.exists()
    assert not state.handoff_reservations


@pytest.mark.asyncio
async def test_queue_remove_head_excludes_full_original_after_last_music_advances(tmp_path: Path) -> None:
    queue, state, music, successor, original, _head = _pair(
        tmp_path,
        maxsize=6,
        original_name="norm_original.mp3",
    )
    later_path = tmp_path / "norm_later.mp3"
    later_path.write_bytes(b"later")
    duplicate_original = tmp_path / "norm_original_copy.mp3"
    duplicate_original.write_bytes(b"same song, different cache path")
    (tmp_path / "norm_original_copy.mp3.json").write_text(
        '{"artist": "artist", "title": "song"}',
        encoding="utf-8",
    )
    later = Segment(
        type=SegmentType.MUSIC,
        path=later_path,
        duration_sec=100.0,
        metadata={
            "queue_id": "later",
            "artist": "other artist",
            "title_only": "later song",
        },
        ephemeral=False,
    )
    queue.put_nowait(later)
    state.queued_segments.append({"id": "later", "duration_sec": 100.0})
    state.last_music_file = later_path
    state.immediate_audio_index = {
        original: 180.0,
        duplicate_original: 180.0,
        later_path: 100.0,
    }

    config = load_config(TOML_PATH)
    config.cache_dir = tmp_path
    app = FastAPI()
    app.include_router(router)
    app.state.queue = queue
    app.state.skip_event = asyncio.Event()
    app.state.station_state = state
    app.state.config = config
    app.state.stream_hub = LiveStreamHub()
    app.state.start_time = 0.0

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/queue/remove", json={"id": "music"})

    assert response.status_code == 200
    queued_paths = [segment.path for segment in _queue_items(queue)]
    assert music not in _queue_items(queue)
    assert successor not in _queue_items(queue)
    assert original not in queued_paths
    assert duplicate_original not in queued_paths
    assert queued_paths.count(later_path) == 1
    assert not state.handoff_reservations
