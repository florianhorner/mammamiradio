"""Admin heading endpoint tests for Seed Station Phase 2A."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.core.config import load_config
from mammamiradio.core.models import Heading, PlaylistSource, StationState, Track
from mammamiradio.playlist.playlist import read_persisted_heading, write_persisted_heading
from mammamiradio.web.streamer import LiveStreamHub, router

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def _track(title: str, artist: str = "Artist", spotify_id: str = "", youtube_id: str = "") -> Track:
    return Track(
        title=title,
        artist=artist,
        duration_ms=180_000,
        spotify_id=spotify_id or title,
        youtube_id=youtube_id,
    )


def _source(seed: str = "classic://italian/80s") -> PlaylistSource:
    return PlaylistSource(kind="classic", source_id=seed.rsplit("/", 1)[-1], url=seed, label="Classici")


def _make_app(tmp_path, tracks: list[Track] | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    config = load_config(TOML_PATH)
    config.admin_password = ""
    config.admin_token = ""
    config.is_addon = False
    config.cache_dir = Path(tmp_path)
    state = StationState(playlist=list(tracks if tracks is not None else [_track("Base", "Base Artist", "base")]))
    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    app.state.source_switch_lock = asyncio.Lock()
    app.state.stream_hub = LiveStreamHub()
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url="http://testserver",
    )


@pytest.mark.asyncio
async def test_heading_set_adds_tags_and_persists(tmp_path):
    app = _make_app(tmp_path)
    heading_tracks = [_track("Blue Jeans", "Franco", "h1"), _track("Notte", "Gianna", "h2")]
    start_revision = app.state.station_state.playlist_revision

    with patch("mammamiradio.web.streamer.load_explicit_source", return_value=(heading_tracks, _source())):
        async with _client(app) as client:
            resp = await client.post("/api/heading", json={"seed": "classic://italian/80s"})
            status = await client.get("/status")

    body = resp.json()
    state = app.state.station_state
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["added"] == 2
    assert state.playlist_revision == start_revision + 1
    assert state.heading is not None
    assert {track.heading_id for track in state.playlist[:2]} == {state.heading.id}
    assert read_persisted_heading(tmp_path) == state.heading
    assert status.json()["heading"]["active"] is True
    assert status.json()["heading"]["label"] == "Anni '80"


@pytest.mark.asyncio
async def test_heading_empty_fallback_arms_nothing(tmp_path):
    app = _make_app(tmp_path)
    start_revision = app.state.station_state.playlist_revision

    with patch("mammamiradio.web.streamer.load_explicit_source", return_value=([], _source())):
        async with _client(app) as client:
            resp = await client.post("/api/heading", json={"seed": "classic://italian/80s"})

    body = resp.json()
    assert body["ok"] is False
    assert "give it a moment" in body["message"]
    assert app.state.station_state.heading is None
    assert app.state.station_state.heading_pending_announcement == ""
    assert app.state.station_state.playlist_revision == start_revision
    assert read_persisted_heading(tmp_path) is None


@pytest.mark.asyncio
async def test_heading_repeat_same_seed_is_idempotent(tmp_path):
    app = _make_app(tmp_path)
    heading_tracks = [_track("Estate", "Bruno", "h1")]

    with patch("mammamiradio.web.streamer.load_explicit_source", return_value=(heading_tracks, _source())) as load:
        async with _client(app) as client:
            first = await client.post("/api/heading", json={"seed": "classic://italian/80s"})
            revision = app.state.station_state.playlist_revision
            second = await client.post("/api/heading", json={"seed": "classic://italian/80s"})

    assert first.json()["ok"] is True
    assert second.json()["ok"] is True
    assert second.json()["idempotent"] is True
    assert load.call_count == 1
    assert app.state.station_state.playlist_revision == revision
    assert len([track for track in app.state.station_state.playlist if track.heading_id]) == 1


@pytest.mark.asyncio
async def test_heading_clear_keeps_blended_tracks_and_deletes_persistence(tmp_path):
    app = _make_app(tmp_path)
    heading_tracks = [_track("Splendido", "Donatella", "h1")]

    with patch("mammamiradio.web.streamer.load_explicit_source", return_value=(heading_tracks, _source())):
        async with _client(app) as client:
            await client.post("/api/heading", json={"seed": "classic://italian/80s"})
            resp = await client.post("/api/heading/clear")

    assert resp.json()["ok"] is True
    assert app.state.station_state.heading is None
    assert app.state.station_state.heading_pending_announcement == ""
    assert [track.title for track in app.state.station_state.playlist if track.spotify_id == "h1"] == ["Splendido"]
    assert read_persisted_heading(tmp_path) is None


@pytest.mark.asyncio
async def test_heading_same_era_after_clear_retags_existing_tracks(tmp_path):
    app = _make_app(tmp_path)
    heading_tracks = [_track("Splendido", "Donatella", "h1")]

    with patch("mammamiradio.web.streamer.load_explicit_source", return_value=(heading_tracks, _source())):
        async with _client(app) as client:
            first = await client.post("/api/heading", json={"seed": "classic://italian/80s"})
            old_heading_id = app.state.station_state.heading.id
            await client.post("/api/heading/clear")
            second = await client.post("/api/heading", json={"seed": "classic://italian/80s"})

    state = app.state.station_state
    assert first.json()["ok"] is True
    assert second.json()["ok"] is True
    assert second.json()["added"] == 0
    assert second.json()["retagged_existing"] == 1
    assert state.heading is not None
    assert state.heading.id != old_heading_id
    assert [track.heading_id for track in state.playlist if track.title == "Splendido"] == [state.heading.id]
    assert state.heading_pending_announcement == ""
    assert read_persisted_heading(tmp_path) == state.heading


@pytest.mark.asyncio
async def test_heading_excludes_banned_tracks(tmp_path):
    app = _make_app(tmp_path)
    app.state.station_state.blocklist = {("banned artist", "banned song"): {"display": "Banned Artist - Banned Song"}}
    fetched = [
        _track("Banned Song", "Banned Artist", "bad"),
        _track("Good Song", "Good Artist", "good"),
    ]

    with patch("mammamiradio.web.streamer.load_explicit_source", return_value=(fetched, _source())):
        async with _client(app) as client:
            resp = await client.post("/api/heading", json={"seed": "classic://italian/80s"})

    assert resp.json()["ok"] is True
    assert resp.json()["added"] == 1
    assert "Banned Song" not in [track.title for track in app.state.station_state.playlist]
    assert "Good Song" in [track.title for track in app.state.station_state.playlist]


@pytest.mark.asyncio
async def test_heading_skips_existing_track_with_different_youtube_id(tmp_path):
    existing = _track("Estate", "Bruno", "base", youtube_id="yt-old")
    app = _make_app(tmp_path, tracks=[existing])
    fetched = [
        _track("Estate", "Bruno", "dupe", youtube_id="yt-new"),
        _track("Notte", "Gianna", "new", youtube_id="yt-other"),
    ]

    with patch("mammamiradio.web.streamer.load_explicit_source", return_value=(fetched, _source())):
        async with _client(app) as client:
            resp = await client.post("/api/heading", json={"seed": "classic://italian/80s"})

    body = resp.json()
    assert body["ok"] is True
    assert body["added"] == 1
    assert body["skipped_existing"] == 1
    assert [(track.artist, track.title) for track in app.state.station_state.playlist].count(("Bruno", "Estate")) == 1
    assert [track.title for track in app.state.station_state.playlist if track.heading_id] == ["Notte"]


@pytest.mark.asyncio
async def test_playlist_load_clears_active_heading(tmp_path):
    app = _make_app(tmp_path)
    heading = Heading("h-old", "classic://italian/80s", "Anni '80", 1.0, "operator")
    app.state.station_state.heading = heading
    app.state.station_state.heading_pending_announcement = "Anni '80"
    write_persisted_heading(tmp_path, heading)
    new_tracks = [_track("New Base", "New Artist", "new")]

    with (
        patch(
            "mammamiradio.web.streamer.load_explicit_source",
            return_value=(new_tracks, _source("classic://italian/70s")),
        ),
        patch("mammamiradio.web.streamer.write_persisted_source"),
    ):
        async with _client(app) as client:
            resp = await client.post("/api/playlist/load", json={"url": "classic://italian/70s"})

    assert resp.json()["ok"] is True
    assert app.state.station_state.heading is None
    assert app.state.station_state.heading_pending_announcement == ""
    assert read_persisted_heading(tmp_path) is None


@pytest.mark.asyncio
async def test_heading_malformed_body_returns_422_not_500(tmp_path):
    """An empty or non-JSON POST body degrades to a warm 422, never a raw 500.

    The admin UI always sends ``{seed: ...}``, but a malformed request must still
    get a human, way-out message (leadership principle #5) rather than a 500.
    """
    app = _make_app(tmp_path)
    async with _client(app) as client:
        empty = await client.post("/api/heading")  # no body at all
        garbage = await client.post(
            "/api/heading",
            content=b"not json",
            headers={"content-type": "application/json"},
        )

    for resp in (empty, garbage):
        assert resp.status_code == 422
        body = resp.json()
        assert body["ok"] is False
        assert body["error"]  # a human message with a way out
    # a malformed request must not arm a heading
    assert app.state.station_state.heading is None
