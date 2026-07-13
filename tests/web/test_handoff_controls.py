"""Control-plane cleanup for committed exact-once music/speech pairs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.core.config import load_config
from mammamiradio.core.models import GenerationWasteReason, Segment, SegmentType, StationState
from mammamiradio.scheduling.handoff import PreparedMusicHandoff, commit_music_handoff
from mammamiradio.scheduling.producer import _front_insert_queue_and_shadow
from mammamiradio.web.mp3_frames import Mp3HandoffSplit
from mammamiradio.web.streamer import (
    LiveStreamHub,
    _cancel_active_handoff_from_queue,
    _purge_blocklisted_from_queue,
    _purge_home_fact_banter_from_queue,
    _purge_queue_and_shadow,
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
) -> tuple[asyncio.Queue[Segment], StationState, Segment, Segment, Path, Path]:
    original = tmp_path / "song.mp3"
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
