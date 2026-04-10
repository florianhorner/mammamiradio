"""Extended route tests for streamer.py — covering admin API routes, health probes, auth edge cases."""

from __future__ import annotations

import asyncio
import base64
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.config import load_config
from mammamiradio.models import PlaylistSource, Segment, SegmentType, StationState, Track
from mammamiradio.streamer import LiveStreamHub, router

TOML_PATH = str(Path(__file__).parent.parent / "radio.toml")


def _basic_auth_header(username: str = "admin", password: str = "secret") -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _make_test_app(*, admin_password: str = "", admin_token: str = "", is_addon: bool = False) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    config = load_config(TOML_PATH)
    config.admin_password = admin_password
    config.admin_token = admin_token
    config.is_addon = is_addon
    config.cache_dir = Path("/tmp/mammamiradio-test-cache")
    config.cache_dir.mkdir(parents=True, exist_ok=True)

    state = StationState(
        playlist=[
            Track(title="Song A", artist="Artist A", duration_ms=180_000, spotify_id="t1"),
            Track(title="Song B", artist="Artist B", duration_ms=200_000, spotify_id="t2"),
            Track(title="Song C", artist="Artist C", duration_ms=160_000, spotify_id="t3"),
        ],
    )

    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    app.state.source_switch_lock = asyncio.Lock()
    app.state.stream_hub = LiveStreamHub()
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    return app


# ---------------------------------------------------------------------------
# Health probes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthz_returns_ok():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "uptime_s" in body
    assert "runtime" in body
    assert "shadow_queue_in_sync" in body["runtime"]


@pytest.mark.asyncio
async def test_healthz_no_start_time():
    app = _make_test_app()
    del app.state.start_time
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["uptime_s"] == 0


@pytest.mark.asyncio
async def test_readyz_starting():
    app = _make_test_app()
    # Empty queue → starting
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "starting"
    assert body["ready"] is False
    assert body["watchdog_status"] == "ok"
    assert body["queue_depth"] == 0


@pytest.mark.asyncio
async def test_readyz_ready():
    app = _make_test_app()
    # Put something in queue
    app.state.queue.put_nowait(MagicMock())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["ready"] is True
    assert body["watchdog_status"] == "ok"
    assert "runtime" in body


@pytest.mark.asyncio
async def test_readyz_no_queue():
    app = _make_test_app()
    del app.state.queue
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["queue_depth"] == -1


@pytest.mark.asyncio
async def test_public_status_marks_upcoming_source_type():
    app = _make_test_app()
    app.state.queue.put_nowait(Segment(type=SegmentType.MUSIC, path=Path("/tmp/fake-upcoming.mp3"), metadata={}))
    app.state.station_state.queued_segments = [{"type": "music", "label": "Queued Song"}]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    upcoming = resp.json()["upcoming"]
    assert upcoming
    assert upcoming[0]["source"] == "rendered_queue"


@pytest.mark.asyncio
async def test_public_status_trims_shadow_queue_drift(tmp_path):
    app = _make_test_app()
    fake_file = tmp_path / "seg.mp3"
    fake_file.write_bytes(b"data")
    app.state.queue.put_nowait(Segment(type=SegmentType.MUSIC, path=fake_file, metadata={"title": "Real"}))
    app.state.station_state.queued_segments = [
        {"type": "music", "label": "Real"},
        {"type": "banter", "label": "Stale"},
    ]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    assert len(resp.json()["upcoming"]) == 1
    assert app.state.station_state.shadow_queue_corrections == 1


# ---------------------------------------------------------------------------
# Shuffle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shuffle_playlist():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/shuffle")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# Purge queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_empty_queue():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/purge")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["purged"] == 0


@pytest.mark.asyncio
async def test_purge_with_segments(tmp_path):
    app = _make_test_app()
    # Add segments to the queue
    fake_file = tmp_path / "seg.mp3"
    fake_file.write_bytes(b"data")
    seg = Segment(type=SegmentType.MUSIC, path=fake_file, metadata={"title": "test"})
    app.state.queue.put_nowait(seg)

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/purge")
    assert resp.status_code == 200
    assert resp.json()["purged"] == 1
    assert not fake_file.exists()  # File should be deleted


# ---------------------------------------------------------------------------
# Skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_nothing_streaming():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/skip")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


# ---------------------------------------------------------------------------
# Remove track
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_track_valid_index():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/playlist/remove", json={"index": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "Song B" in body["removed"]
    assert len(app.state.station_state.playlist) == 2


@pytest.mark.asyncio
async def test_remove_track_invalid_index():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/playlist/remove", json={"index": 99})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


@pytest.mark.asyncio
async def test_remove_track_rejects_non_integer_index_without_mutating_playlist():
    app = _make_test_app()
    before = [t.spotify_id for t in app.state.station_state.playlist]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/playlist/remove", json={"index": "abc"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert [t.spotify_id for t in app.state.station_state.playlist] == before


# ---------------------------------------------------------------------------
# Move track
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_move_track_valid():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/playlist/move", json={"from": 2, "to": 0})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "Song C" in body["moved"]
    # Song C should now be first
    assert app.state.station_state.playlist[0].title == "Song C"


@pytest.mark.asyncio
async def test_move_track_invalid_indices():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/playlist/move", json={"from": -1, "to": 100})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


@pytest.mark.asyncio
async def test_move_track_rejects_non_integer_indices_without_mutating_playlist():
    app = _make_test_app()
    before = [t.spotify_id for t in app.state.station_state.playlist]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/playlist/move", json={"from": "x", "to": 0})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert [t.spotify_id for t in app.state.station_state.playlist] == before


# ---------------------------------------------------------------------------
# Move to next
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_move_to_next_valid(tmp_path):
    app = _make_test_app()
    queued_file = tmp_path / "queued-next.mp3"
    queued_file.write_bytes(b"queued")
    app.state.queue.put_nowait(Segment(type=SegmentType.BANTER, path=queued_file, metadata={"title": "Queued"}))
    app.state.station_state.queued_segments = [{"type": "banter", "label": "Queued"}]
    starting_revision = app.state.station_state.playlist_revision
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/playlist/move_to_next", json={"index": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["purged"] == 1
    assert app.state.station_state.playlist[0].title == "Song C"
    assert app.state.station_state.force_next == SegmentType.MUSIC
    assert app.state.station_state.playlist_revision == starting_revision + 1
    assert app.state.station_state.queued_segments == []
    assert app.state.queue.qsize() == 0
    assert not queued_file.exists()


@pytest.mark.asyncio
async def test_move_to_next_invalid():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/playlist/move_to_next", json={"index": 99})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


@pytest.mark.asyncio
async def test_move_to_next_rejects_non_integer_index_without_side_effects(tmp_path):
    app = _make_test_app()
    queued_file = tmp_path / "queued-next-invalid.mp3"
    queued_file.write_bytes(b"queued")
    app.state.queue.put_nowait(Segment(type=SegmentType.BANTER, path=queued_file, metadata={"title": "Queued"}))
    app.state.station_state.queued_segments = [{"type": "banter", "label": "Queued"}]
    starting_revision = app.state.station_state.playlist_revision
    before = [t.spotify_id for t in app.state.station_state.playlist]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/playlist/move_to_next", json={"index": "abc"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert [t.spotify_id for t in app.state.station_state.playlist] == before
    assert app.state.station_state.playlist_revision == starting_revision
    assert app.state.station_state.queued_segments == [{"type": "banter", "label": "Queued"}]
    assert app.state.queue.qsize() == 1
    assert queued_file.exists()


@pytest.mark.asyncio
async def test_load_playlist_clears_shadow_upcoming_after_purge(tmp_path):
    app = _make_test_app()
    queued_file = tmp_path / "queued.mp3"
    queued_file.write_bytes(b"queued")
    app.state.queue.put_nowait(Segment(type=SegmentType.MUSIC, path=queued_file, metadata={"title": "Queued Song"}))
    app.state.station_state.queued_segments = [{"type": "music", "label": "Queued Song"}]
    loaded_tracks = [Track(title="Fresh Song", artist="Fresh Artist", duration_ms=180_000, spotify_id="fresh1")]
    resolved_source = PlaylistSource(kind="url", url="https://open.spotify.com/playlist/test", label="Fresh playlist")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with (
        patch("mammamiradio.streamer.load_explicit_source", return_value=(loaded_tracks, resolved_source)),
        patch("mammamiradio.streamer.write_persisted_source"),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/playlist/load", json={"url": resolved_source.url})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert app.state.station_state.queued_segments == []
    assert app.state.queue.qsize() == 0
    assert not queued_file.exists()


# ---------------------------------------------------------------------------
# Add track
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_track_to_end():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/playlist/add",
            json={
                "title": "New Song",
                "artist": "New Artist",
                "duration_ms": 240_000,
                "spotify_id": "new123",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["position"] == "end"
    assert app.state.station_state.playlist[-1].title == "New Song"


@pytest.mark.asyncio
async def test_add_track_play_next():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/playlist/add",
            json={
                "title": "Priority Song",
                "artist": "Artist",
                "duration_ms": 200_000,
                "spotify_id": "prio123",
                "position": "next",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["position"] == "next"
    assert app.state.station_state.playlist[0].title == "Priority Song"


@pytest.mark.asyncio
async def test_add_track_missing_spotify_id():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/playlist/add",
            json={
                "title": "No ID",
                "artist": "Artist",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


@pytest.mark.asyncio
async def test_source_options_returns_headless_fallback():
    app = _make_test_app(is_addon=True)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/spotify/source-options")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["capabilities"]["supports_user_sources"] is False


@pytest.mark.asyncio
async def test_source_options_returns_playlists_when_available():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch(
        "mammamiradio.streamer.list_user_playlists",
        return_value=[{"id": "abc", "label": "Roadtrip", "track_count": 12}],
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/api/spotify/source-options")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["playlists"][0]["id"] == "abc"


@pytest.mark.asyncio
async def test_source_select_success_swaps_playlist_and_persists():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    new_tracks = [Track(title="Roadtrip", artist="Artist", duration_ms=180_000, spotify_id="new1")]
    with (
        patch(
            "mammamiradio.streamer.load_explicit_source",
            return_value=(
                new_tracks,
                MagicMock(kind="playlist", source_id="abc", url="", label="Roadtrip", track_count=1, selected_at=1.0),
            ),
        ),
        patch("mammamiradio.streamer.write_persisted_source") as write_mock,
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/spotify/source/select", json={"kind": "playlist", "source_id": "abc"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert app.state.station_state.playlist[0].title == "Roadtrip"
    write_mock.assert_called_once()


@pytest.mark.asyncio
async def test_source_select_failure_keeps_previous_playlist():
    app = _make_test_app()
    original_title = app.state.station_state.playlist[0].title
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.streamer.load_explicit_source", side_effect=Exception("boom")):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/spotify/source/select", json={"kind": "playlist", "source_id": "abc"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert app.state.station_state.playlist[0].title == original_title


@pytest.mark.asyncio
async def test_playlist_load_compatibility_wrapper_uses_url_selection():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    new_tracks = [Track(title="From URL", artist="Artist", duration_ms=180_000, spotify_id="new1")]
    with (
        patch(
            "mammamiradio.streamer.load_explicit_source",
            return_value=(
                new_tracks,
                MagicMock(
                    kind="url",
                    source_id="abc",
                    url="https://open.spotify.com/playlist/abc",
                    label="From URL",
                    track_count=1,
                    selected_at=1.0,
                ),
            ),
        ) as load_mock,
        patch("mammamiradio.streamer.write_persisted_source"),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/playlist/load", json={"url": "https://open.spotify.com/playlist/abc"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    source_arg = load_mock.call_args.args[1]
    assert source_arg.kind == "url"


# ---------------------------------------------------------------------------
# Source selection — immediate cutover, URL cleanup, capability enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_select_purges_queue_and_skips():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    # Seed the queue with a fake segment
    seg = Segment(type=SegmentType.MUSIC, path=Path("/tmp/fake-seg.mp3"), duration_sec=10.0)
    app.state.queue.put_nowait(seg)
    app.state.station_state.now_streaming = {"type": "music", "label": "Old Song", "started": time.time()}

    new_tracks = [Track(title="New", artist="A", duration_ms=180_000, spotify_id="n1")]
    with (
        patch(
            "mammamiradio.streamer.load_explicit_source",
            return_value=(
                new_tracks,
                MagicMock(kind="playlist", source_id="abc", url="", label="New PL", track_count=1, selected_at=1.0),
            ),
        ),
        patch("mammamiradio.streamer.write_persisted_source"),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/spotify/source/select", json={"kind": "playlist", "source_id": "abc"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert app.state.queue.empty()
    assert app.state.skip_event.is_set()


@pytest.mark.asyncio
async def test_source_select_no_skip_when_nothing_streaming():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    app.state.station_state.now_streaming = {}

    new_tracks = [Track(title="New", artist="A", duration_ms=180_000, spotify_id="n1")]
    with (
        patch(
            "mammamiradio.streamer.load_explicit_source",
            return_value=(
                new_tracks,
                MagicMock(kind="playlist", source_id="abc", url="", label="PL", track_count=1, selected_at=1.0),
            ),
        ),
        patch("mammamiradio.streamer.write_persisted_source"),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/spotify/source/select", json={"kind": "playlist", "source_id": "abc"})
    assert resp.json()["ok"] is True
    assert not app.state.skip_event.is_set()


@pytest.mark.asyncio
async def test_source_select_clears_stale_url():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    app.state.config.playlist.spotify_url = "https://open.spotify.com/playlist/old"

    new_tracks = [Track(title="Liked", artist="A", duration_ms=180_000, spotify_id="n1")]
    with (
        patch(
            "mammamiradio.streamer.load_explicit_source",
            return_value=(
                new_tracks,
                MagicMock(
                    kind="liked_songs",
                    source_id="liked_songs",
                    url="",
                    label="Liked Songs",
                    track_count=1,
                    selected_at=1.0,
                ),
            ),
        ),
        patch("mammamiradio.streamer.write_persisted_source"),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/spotify/source/select", json={"kind": "liked_songs"})
    assert resp.json()["ok"] is True
    assert app.state.config.playlist.spotify_url == ""


@pytest.mark.asyncio
async def test_source_select_rejects_playlist_in_addon_mode():
    app = _make_test_app(is_addon=True)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    original_playlist = list(app.state.station_state.playlist)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/spotify/source/select", json={"kind": "playlist", "source_id": "abc"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "URL loading" in body["error"]
    assert app.state.station_state.playlist == original_playlist


@pytest.mark.asyncio
async def test_source_select_rejects_liked_songs_in_addon_mode():
    app = _make_test_app(is_addon=True)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/spotify/source/select", json={"kind": "liked_songs"})
    assert resp.json()["ok"] is False
    assert "URL loading" in resp.json()["error"]


@pytest.mark.asyncio
async def test_source_select_allows_url_in_addon_mode():
    app = _make_test_app(is_addon=True)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    new_tracks = [Track(title="URL Track", artist="A", duration_ms=180_000, spotify_id="u1")]
    with (
        patch(
            "mammamiradio.streamer.load_explicit_source",
            return_value=(
                new_tracks,
                MagicMock(
                    kind="url",
                    source_id="",
                    url="https://open.spotify.com/playlist/xyz",
                    label="URL PL",
                    track_count=1,
                    selected_at=1.0,
                ),
            ),
        ),
        patch("mammamiradio.streamer.write_persisted_source"),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/api/spotify/source/select",
                json={"kind": "url", "url": "https://open.spotify.com/playlist/xyz"},
            )
    assert resp.json()["ok"] is True


@pytest.mark.asyncio
async def test_playlist_load_purges_queue_and_skips():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    seg = Segment(type=SegmentType.MUSIC, path=Path("/tmp/fake-seg2.mp3"), duration_sec=10.0)
    app.state.queue.put_nowait(seg)
    app.state.station_state.now_streaming = {"type": "music", "label": "Playing", "started": time.time()}

    new_tracks = [Track(title="URL Track", artist="A", duration_ms=180_000, spotify_id="u1")]
    with (
        patch(
            "mammamiradio.streamer.load_explicit_source",
            return_value=(
                new_tracks,
                MagicMock(
                    kind="url",
                    source_id="",
                    url="https://open.spotify.com/playlist/abc",
                    label="URL PL",
                    track_count=1,
                    selected_at=1.0,
                ),
            ),
        ),
        patch("mammamiradio.streamer.write_persisted_source"),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/playlist/load", json={"url": "https://open.spotify.com/playlist/abc"})
    assert resp.json()["ok"] is True
    assert app.state.queue.empty()
    assert app.state.skip_event.is_set()


@pytest.mark.asyncio
async def test_source_select_persistence_failure_nonfatal():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    new_tracks = [Track(title="New", artist="A", duration_ms=180_000, spotify_id="n1")]
    with (
        patch(
            "mammamiradio.streamer.load_explicit_source",
            return_value=(
                new_tracks,
                MagicMock(kind="playlist", source_id="abc", url="", label="PL", track_count=1, selected_at=1.0),
            ),
        ),
        patch("mammamiradio.streamer.write_persisted_source", side_effect=OSError("disk full")),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/spotify/source/select", json={"kind": "playlist", "source_id": "abc"})
    assert resp.json()["ok"] is True
    assert app.state.station_state.playlist[0].title == "New"


@pytest.mark.asyncio
async def test_source_select_requests_are_serialized():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    load_started = asyncio.Event()
    release_first = threading.Event()
    call_order: list[str] = []
    loop = asyncio.get_running_loop()

    first_tracks = [Track(title="First", artist="A", duration_ms=180_000, spotify_id="first")]
    second_tracks = [Track(title="Second", artist="B", duration_ms=180_000, spotify_id="second")]

    def fake_load_explicit_source(_config, source):
        call_order.append(source.source_id)
        if source.source_id == "first":
            loop.call_soon_threadsafe(load_started.set)
            release_first.wait()
            return (
                first_tracks,
                MagicMock(kind="playlist", source_id="first", url="", label="First", track_count=1, selected_at=1.0),
            )
        return (
            second_tracks,
            MagicMock(kind="playlist", source_id="second", url="", label="Second", track_count=1, selected_at=2.0),
        )

    with (
        patch("mammamiradio.streamer.load_explicit_source", side_effect=fake_load_explicit_source),
        patch("mammamiradio.streamer.write_persisted_source") as write_mock,
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first_task = asyncio.create_task(
                client.post("/api/spotify/source/select", json={"kind": "playlist", "source_id": "first"})
            )
            await asyncio.wait_for(load_started.wait(), timeout=1)
            second_task = asyncio.create_task(
                client.post("/api/spotify/source/select", json={"kind": "playlist", "source_id": "second"})
            )
            await asyncio.sleep(0.05)
            assert call_order == ["first"]
            release_first.set()
            first_resp, second_resp = await asyncio.gather(first_task, second_task)

    assert first_resp.status_code == 200
    assert second_resp.status_code == 200
    assert first_resp.json()["ok"] is True
    assert second_resp.json()["ok"] is True
    assert call_order == ["first", "second"]
    assert app.state.station_state.playlist[0].title == "Second"
    assert app.state.station_state.playlist_source.source_id == "second"
    assert write_mock.call_count == 2


# ---------------------------------------------------------------------------
# Search tracks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_empty_query():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/search?q=")
    assert resp.status_code == 200
    assert resp.json()["results"] == []


@pytest.mark.asyncio
async def test_search_spotify_success():
    app = _make_test_app()
    mock_sp = MagicMock()
    mock_sp.search.return_value = {
        "tracks": {
            "items": [
                {
                    "name": "Found Song",
                    "artists": [{"name": "Found Artist"}],
                    "duration_ms": 180_000,
                    "id": "found123",
                }
            ]
        }
    }
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.spotify_auth.get_spotify_client", return_value=mock_sp):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/api/search?q=Found")
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 1
    assert results[0]["title"] == "Found Song"


@pytest.mark.asyncio
async def test_search_spotify_failure():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.spotify_auth.get_spotify_client", side_effect=Exception("no creds")):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/api/search?q=test")
    assert resp.status_code == 200
    assert resp.json()["results"] == []
    assert "error" in resp.json()


# ---------------------------------------------------------------------------
# Load playlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_playlist_success():
    app = _make_test_app()
    new_tracks = [Track(title="New A", artist="NA", duration_ms=200_000, spotify_id="na1")]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with (
        patch(
            "mammamiradio.streamer.load_explicit_source",
            return_value=(
                new_tracks,
                MagicMock(
                    kind="url",
                    source_id="xyz",
                    url="https://open.spotify.com/playlist/xyz",
                    label="New A",
                    track_count=1,
                    selected_at=1.0,
                ),
            ),
        ),
        patch("mammamiradio.streamer.write_persisted_source"),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/playlist/load", json={"url": "https://open.spotify.com/playlist/xyz"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["tracks"] == 1
    assert app.state.station_state.playlist[0].title == "New A"


@pytest.mark.asyncio
async def test_load_playlist_no_url():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/playlist/load", json={"url": ""})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


@pytest.mark.asyncio
async def test_load_playlist_fetch_failure():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.streamer.load_explicit_source", side_effect=Exception("API error")):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/playlist/load", json={"url": "https://spotify.com/playlist/bad"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


@pytest.mark.asyncio
async def test_load_playlist_empty_result():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.playlist.fetch_playlist", return_value=[]):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/playlist/load", json={"url": "https://spotify.com/playlist/empty"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


# ---------------------------------------------------------------------------
# Logs endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logs_endpoint():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/logs")
    assert resp.status_code == 200
    body = resp.json()
    assert "go_librespot" in body


# ---------------------------------------------------------------------------
# Auth edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hassio_ingress_auth_bypass():
    """HA addon with ingress prefix from Hassio network should bypass auth on admin routes."""
    app = _make_test_app(is_addon=True)
    # Hassio internal network: 172.30.32.x
    transport = httpx.ASGITransport(app=app, client=("172.30.32.5", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # / is public (no auth needed)
        resp = await client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})
        assert resp.status_code == 200
        # /dashboard requires auth — Hassio internal network should bypass
        resp = await client.get("/dashboard", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_hassio_internal_request_without_ingress_header_bypasses_auth():
    """HA-managed internal requests may omit X-Ingress-Path but should still work on admin routes."""
    app = _make_test_app(is_addon=True)
    transport = httpx.ASGITransport(app=app, client=("172.30.32.2", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # / is public (no auth needed)
        resp = await client.get("/")
        assert resp.status_code == 200
        # /dashboard should also work for Hassio internal requests
        resp = await client.get("/dashboard")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_hassio_ingress_spoofed_external():
    """External client spoofing X-Ingress-Path should NOT bypass auth on admin routes."""
    app = _make_test_app(admin_password="secret", is_addon=True)
    transport = httpx.ASGITransport(app=app, client=("8.8.8.8", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # / is the public listener page (no auth required)
        resp = await client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})
        assert resp.status_code == 200
        # /dashboard requires admin auth — spoofed ingress should NOT bypass
        resp = await client.get("/dashboard", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_basic_auth_mutation_requires_same_origin_or_csrf():
    app = _make_test_app(admin_password="secret")
    transport = httpx.ASGITransport(app=app, client=("10.0.0.1", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/shuffle", headers=_basic_auth_header())
    assert resp.status_code == 403
    assert "Cross-site admin write blocked" in resp.text


@pytest.mark.asyncio
async def test_basic_auth_mutation_allows_same_origin():
    app = _make_test_app(admin_password="secret")
    transport = httpx.ASGITransport(app=app, client=("10.0.0.1", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/shuffle",
            headers={**_basic_auth_header(), "Origin": "http://testserver"},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@pytest.mark.asyncio
async def test_basic_auth_mutation_allows_csrf_token_without_origin():
    app = _make_test_app(admin_password="secret")
    transport = httpx.ASGITransport(app=app, client=("10.0.0.1", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        dashboard = await client.get("/dashboard", headers=_basic_auth_header())
        assert dashboard.status_code == 200
        resp = await client.post(
            "/api/shuffle",
            headers={**_basic_auth_header(), "X-Radio-CSRF-Token": app.state.csrf_token},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@pytest.mark.asyncio
async def test_token_auth_mutation_skips_csrf_requirement():
    app = _make_test_app(admin_token="tok-123")
    transport = httpx.ASGITransport(app=app, client=("10.0.0.1", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/shuffle", headers={"X-Radio-Admin-Token": "tok-123"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_token_auth_on_loopback_no_password():
    """Token-only auth: loopback should be trusted even with wrong token."""
    app = _make_test_app(admin_token="tok-123")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_token_auth_non_loopback_requires_token():
    """Token-only auth: non-loopback without token should fail."""
    app = _make_test_app(admin_token="tok-123")
    transport = httpx.ASGITransport(app=app, client=("10.0.0.1", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_token_auth_non_loopback_with_valid_token():
    app = _make_test_app(admin_token="tok-123")
    transport = httpx.ASGITransport(app=app, client=("10.0.0.1", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status", headers={"X-Radio-Admin-Token": "tok-123"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Stream endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_returns_audio_headers():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)

    async def fake_audio_generator(_request):
        yield b"frame"

    with patch("mammamiradio.streamer._audio_generator", fake_audio_generator):
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
            client.stream("GET", "/stream") as resp,
        ):
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "audio/mpeg"
            assert "icy-name" in resp.headers
            assert "icy-br" in resp.headers


@pytest.mark.asyncio
async def test_listener_page_registers_service_worker_inside_main_script():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/listen")

    assert resp.status_code == 200
    assert "navigator.serviceWorker.register(_base + '/static/sw.js')" in resp.text
    assert "</script>\n<script>\nif ('serviceWorker' in navigator)" not in resp.text


# ---------------------------------------------------------------------------
# _tail_log helper
# ---------------------------------------------------------------------------


def test_tail_log_missing_file():
    from mammamiradio.streamer import _tail_log

    result = _tail_log("/nonexistent/path/file.log")
    assert result == []


def test_tail_log_with_content(tmp_path):
    from mammamiradio.streamer import _tail_log

    log_file = tmp_path / "test.log"
    log_file.write_text("line1\nline2\nline3\nline4\n")
    result = _tail_log(str(log_file), lines=2)
    assert len(result) == 2
    assert "line3" in result
    assert "line4" in result


# ---------------------------------------------------------------------------
# _sanitize_ingress_prefix
# ---------------------------------------------------------------------------


def test_sanitize_ingress_prefix_valid():
    from mammamiradio.streamer import _sanitize_ingress_prefix

    assert _sanitize_ingress_prefix("/api/hassio_ingress/abc123") == "/api/hassio_ingress/abc123"


def test_sanitize_ingress_prefix_strips_trailing_slash():
    from mammamiradio.streamer import _sanitize_ingress_prefix

    assert _sanitize_ingress_prefix("/prefix/") == "/prefix"


def test_sanitize_ingress_prefix_rejects_xss():
    from mammamiradio.streamer import _sanitize_ingress_prefix

    assert _sanitize_ingress_prefix('"><script>alert(1)</script>') == ""


def test_sanitize_ingress_prefix_empty():
    from mammamiradio.streamer import _sanitize_ingress_prefix

    assert _sanitize_ingress_prefix("") == ""
