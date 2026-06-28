"""Endpoint tests for the rotation-pool purge ("Svuota tutto" → /api/playlist/purge).

The admin "Svuota tutto" button POSTed to a route that did not exist, so every
click 404'd and the operator got the way-out error toast. This pins the new
route and its contract.

Scenarios (CLAUDE.md audio-delivery rule — purge touches the lookahead queue):
  * Normal       — pool + queued segments present -> pool emptied, queue drained,
                   pin cleared, revision bumped, {ok, purged} returned.
  * Empty/edge   — already-empty pool + empty queue -> still 200, purged 0, no error.
  * Post-restart — session_stopped set -> purge still clears the pool cleanly and
                   never raises into the audio path.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.core.config import load_config
from mammamiradio.core.models import PlaylistSource, Segment, SegmentType, StationState, Track
from mammamiradio.playlist.playlist import PERSISTED_SOURCE_FILENAME, write_persisted_source
from mammamiradio.web.streamer import LiveStreamHub, router

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def _track(title: str, artist: str) -> Track:
    return Track(title=title, artist=artist, duration_ms=180_000)


def _make_app(tmp_path, tracks=None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    config = load_config(TOML_PATH)
    config.admin_password = ""
    config.admin_token = ""
    config.is_addon = False
    config.cache_dir = Path(tmp_path)
    state = StationState(playlist=list(tracks if tracks is not None else []))
    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    app.state.source_switch_lock = asyncio.Lock()
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    hub = LiveStreamHub()
    hub.bind_state(state)
    app.state.stream_hub = hub
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://testserver"
    )


@pytest.mark.asyncio
async def test_purge_empties_pool_and_drains_queue(tmp_path):
    app = _make_app(tmp_path, [_track("Volare", "Modugno"), _track("Felicità", "Al Bano")])
    state = app.state.station_state
    source = PlaylistSource(kind="charts", source_id="apple_music_it_top_100", label="Italian charts")
    write_persisted_source(tmp_path, source)
    assert (tmp_path / PERSISTED_SOURCE_FILENAME).exists()
    state.playlist_source = source
    state.pinned_track = state.playlist[0]
    # Two pre-produced segments buffered in the lookahead (real queue + shadow).
    seg_a = Segment(type=SegmentType.MUSIC, path=tmp_path / "a.mp3", duration_sec=5.0)
    seg_b = Segment(type=SegmentType.BANTER, path=tmp_path / "b.mp3", duration_sec=5.0)
    app.state.queue.put_nowait(seg_a)
    app.state.queue.put_nowait(seg_b)
    state.queued_segments = [{"id": "a", "type": "music"}, {"id": "b", "type": "banter"}]
    rev_before = state.playlist_revision

    async with _client(app) as c:
        r = await c.post("/api/playlist/purge", json={})
        body = r.json()

    assert r.status_code == 200
    assert body["ok"] is True
    assert body["purged"] == 2
    assert body["persisted"] is True
    assert state.playlist == []
    assert state.playlist_source is None
    assert state.pinned_track is None
    assert state.queued_segments == []
    assert app.state.queue.empty()
    assert state.playlist_revision > rev_before
    assert not app.state.skip_event.is_set()
    assert not (tmp_path / PERSISTED_SOURCE_FILENAME).exists()


@pytest.mark.asyncio
async def test_purge_reports_not_persisted_but_still_clears_pool_and_queue(tmp_path):
    app = _make_app(tmp_path, [_track("Volare", "Modugno")])
    state = app.state.station_state
    state.playlist_source = PlaylistSource(kind="charts", source_id="apple_music_it_top_100", label="Italian charts")
    app.state.queue.put_nowait(Segment(type=SegmentType.MUSIC, path=tmp_path / "a.mp3", duration_sec=5.0))
    state.queued_segments = [{"id": "a", "type": "music"}]

    with patch("mammamiradio.web.streamer._delete_persisted_source", return_value=False):
        async with _client(app) as c:
            r = await c.post("/api/playlist/purge", json={})
            body = r.json()

    assert r.status_code == 200
    assert body["ok"] is True
    assert body["persisted"] is False
    assert body["purged"] == 1
    assert state.playlist == []
    assert state.playlist_source is None
    assert state.queued_segments == []
    assert app.state.queue.empty()
    assert not app.state.skip_event.is_set()


@pytest.mark.asyncio
async def test_purge_on_empty_pool_is_a_clean_noop(tmp_path):
    # Empty fallback: no tracks, no buffered segments. Purge must still succeed.
    app = _make_app(tmp_path, [])
    state = app.state.station_state
    async with _client(app) as c:
        r = await c.post("/api/playlist/purge", json={})
        body = r.json()
    assert r.status_code == 200
    assert body["ok"] is True
    assert body["purged"] == 0
    assert state.playlist == []
    assert not app.state.skip_event.is_set()


@pytest.mark.asyncio
async def test_purge_after_restart_with_session_stopped(tmp_path):
    # Post-restart: a stopped session must not make purge raise; it just clears.
    app = _make_app(tmp_path, [_track("Volare", "Modugno")])
    state = app.state.station_state
    state.session_stopped = True
    async with _client(app) as c:
        r = await c.post("/api/playlist/purge", json={})
        body = r.json()
    assert r.status_code == 200
    assert body["ok"] is True
    assert state.playlist == []


@pytest.mark.asyncio
async def test_purge_waits_for_in_flight_source_switch(tmp_path):
    app = _make_app(tmp_path, [_track("Volare", "Modugno")])

    await app.state.source_switch_lock.acquire()
    async with _client(app) as c:
        task = asyncio.create_task(c.post("/api/playlist/purge", json={}))
        await asyncio.sleep(0.05)
        assert not task.done(), "purge must wait behind an active source mutation"
        app.state.source_switch_lock.release()
        r = await task

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert app.state.station_state.playlist == []
