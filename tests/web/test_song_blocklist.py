"""Endpoint + engine tests for the operator song blocklist (Phase 1).

Scenarios (CLAUDE.md audio-delivery rule):
  * Normal      — ban a song -> dropped from rotation + persisted + listed.
  * Empty/edge  — bulk ban that would starve the pool is refused with a warm message.
  * Post-restart — covered at the data layer in tests/playlist/test_blocklist.py.
Plus: durable ✕ removal, unban, the 4th ingest doorway (_commit_external_download),
on-air queue purge, and pinned-track clear.
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
from mammamiradio.core.models import Segment, SegmentType, StationState, Track
from mammamiradio.web import streamer
from mammamiradio.web.streamer import LiveStreamHub, _apply_ban, router

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def _track(title: str, artist: str, spotify_id: str = "") -> Track:
    return Track(title=title, artist=artist, duration_ms=180_000, spotify_id=spotify_id)


def _make_app(tmp_path, tracks=None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    config = load_config(TOML_PATH)
    config.admin_password = ""
    config.admin_token = ""
    config.is_addon = False
    config.cache_dir = Path(tmp_path)
    state = StationState(playlist=list(tracks if tracks is not None else [_track("Volare", "Modugno", "t1")]))
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
async def test_ban_by_keys_drops_and_persists(tmp_path):
    app = _make_app(tmp_path, [_track("Volare", "Modugno"), _track("Felicità", "Al Bano")])
    state = app.state.station_state
    async with _client(app) as c:
        r = await c.post("/api/track/ban", json={"keys": [["Modugno", "Volare"]]})
        body = r.json()
        assert body["ok"] is True and body["removed"] == 1
        # Dropped from the live pool...
        assert [t.title for t in state.playlist] == ["Felicità"]
        # ...persisted to disk...
        assert ("modugno", "volare") in state.blocklist
        # ...and surfaced in the banlist view.
        bl = (await c.get("/api/track/banlist")).json()
        assert bl["count"] == 1 and bl["banned"][0]["title"] == "volare"


@pytest.mark.asyncio
async def test_ban_by_indices(tmp_path):
    app = _make_app(tmp_path, [_track("A", "X"), _track("B", "Y"), _track("C", "Z")])
    state = app.state.station_state
    async with _client(app) as c:
        r = await c.post("/api/track/ban", json={"indices": [0, 2]})
        assert r.json()["removed"] == 2
    assert [t.title for t in state.playlist] == ["B"]


@pytest.mark.asyncio
async def test_bulk_ban_starvation_rejected_with_warm_message(tmp_path):
    pool = [_track(f"S{i}", "A", f"id{i}") for i in range(6)]
    app = _make_app(tmp_path, pool)
    state = app.state.station_state
    async with _client(app) as c:
        # Banning 5 of 6 would leave 1, below the floor -> refuse, change nothing.
        r = await c.post("/api/track/ban", json={"indices": [0, 1, 2, 3, 4]})
        body = r.json()
        assert body["ok"] is False
        assert "too few songs" in body["error"].lower()
        assert "429" not in body["error"] and "starv" not in body["error"].lower()
    assert len(state.playlist) == 6
    assert state.blocklist == {}


@pytest.mark.asyncio
async def test_remove_endpoint_is_a_durable_ban(tmp_path):
    app = _make_app(tmp_path, [_track("Volare", "Modugno"), _track("Felicità", "Al Bano")])
    state = app.state.station_state
    async with _client(app) as c:
        r = await c.post("/api/playlist/remove", json={"index": 0})
        assert r.json()["banned"] is True
    assert ("modugno", "volare") in state.blocklist
    assert [t.title for t in state.playlist] == ["Felicità"]


@pytest.mark.asyncio
async def test_unban_lifts_the_ban(tmp_path):
    app = _make_app(tmp_path, [_track("Volare", "Modugno")])
    state = app.state.station_state
    async with _client(app) as c:
        await c.post("/api/track/ban", json={"keys": [["Modugno", "Volare"]]})
        assert ("modugno", "volare") in state.blocklist
        r = await c.post("/api/track/unban", json={"keys": [["Modugno", "Volare"]]})
        assert r.json()["unbanned"] == 1
    assert ("modugno", "volare") not in state.blocklist


@pytest.mark.asyncio
async def test_ban_clears_matching_pin(tmp_path):
    pinned = _track("Volare", "Modugno")
    app = _make_app(tmp_path, [pinned, _track("Felicità", "Al Bano")])
    state = app.state.station_state
    state.pinned_track = pinned
    _apply_ban(state, app.state.config, [pinned], queue=app.state.queue)
    assert state.pinned_track is None


@pytest.mark.asyncio
async def test_ban_purges_not_yet_started_queued_music_segment(tmp_path):
    app = _make_app(tmp_path, [_track("Volare", "Modugno")])
    state = app.state.station_state
    q = app.state.queue
    # A pre-produced music segment of the banned song + an innocent one.
    seg_banned = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/volare.mp3"),
        ephemeral=False,
        metadata={"artist": "Modugno", "title_only": "Volare", "queue_id": "q-ban"},
    )
    seg_keep = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/keep.mp3"),
        ephemeral=False,
        metadata={"artist": "Al Bano", "title_only": "Felicità", "queue_id": "q-keep"},
    )
    q.put_nowait(seg_banned)
    q.put_nowait(seg_keep)
    state.queued_segments = [{"id": "q-ban", "label": "Volare"}, {"id": "q-keep", "label": "Felicità"}]

    result = _apply_ban(state, app.state.config, [_track("Volare", "Modugno")], queue=q)
    assert result["purged"] == 1
    # Shadow + real queue both keep only the innocent segment.
    assert [s["id"] for s in state.queued_segments] == ["q-keep"]
    survivors = []
    while not q.empty():
        survivors.append(q.get_nowait())
    assert [s.metadata["queue_id"] for s in survivors] == ["q-keep"]


@pytest.mark.asyncio
async def test_commit_external_download_drops_banned_song(tmp_path):
    """4th ingest doorway: an admin queue-from-search / listener request for a
    banned song must be dropped, not committed to rotation."""
    app = _make_app(tmp_path, [])
    state = app.state.station_state
    state.blocklist = {
        ("modugno", "volare"): {"display": "Modugno - Volare", "banned_by": "operator", "banned_at": 0.0}
    }
    banned = _track("Volare", "Modugno", "yt1")

    async def _no_download(track, cache_dir, music_dir=None):
        return None

    with (
        patch("mammamiradio.playlist.downloader.download_external_track", _no_download),
        patch("mammamiradio.playlist.cover_art.needs_resolve", return_value=False),
    ):
        status = await streamer._commit_external_download(
            banned,
            app.state,
            state.source_revision,
            should_commit=lambda: True,
            should_pin=lambda: True,
        )
    assert status == "dropped"
    assert banned not in state.playlist
    assert state.playlist == []
