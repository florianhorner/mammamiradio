"""Extended route tests for streamer.py — covering admin API routes, health probes, auth edge cases."""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.config import load_config
from mammamiradio.models import PlaylistSource, Segment, SegmentType, StationState, Track
from mammamiradio.playlist import ExplicitSourceError
from mammamiradio.streamer import LiveStreamHub, _download_listener_song, router

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
    # move_to_next pins the track instead of reordering the playlist
    assert app.state.station_state.pinned_track is not None
    assert app.state.station_state.pinned_track.title == "Song C"
    assert app.state.station_state.force_next == SegmentType.MUSIC
    assert app.state.station_state.playlist_revision == starting_revision + 1
    # Pre-rendered segments are intentionally preserved — no purge on move_to_next
    assert app.state.station_state.queued_segments == [{"type": "banter", "label": "Queued"}]
    assert app.state.queue.qsize() == 1
    assert queued_file.exists()


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
async def test_move_to_next_updates_public_upcoming_preview():
    app = _make_test_app()
    app.state.station_state.segments_produced = 1
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        move_resp = await client.post("/api/playlist/move_to_next", json={"index": 2})
        status_resp = await client.get("/public-status")

    assert move_resp.status_code == 200
    assert move_resp.json()["ok"] is True
    upcoming = status_resp.json()["upcoming"]
    assert upcoming[0]["type"] == "music"
    assert upcoming[0]["label"] == "Artist C – Song C"
    assert upcoming[0]["playlist_index"] == 2


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
async def test_add_track_missing_title():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/playlist/add",
            json={
                "artist": "Artist",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


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
async def test_search_returns_playlist_and_external_results():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch(
        "mammamiradio.downloader.search_ytdlp_metadata",
        return_value=[
            {
                "youtube_id": "yt1",
                "title": "Song X",
                "artist": "Artist X",
                "display": "Artist X – Song X",
                "duration_ms": 123000,
            }
        ],
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/api/search?q=Song")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) >= 1
    assert len(body["external"]) == 1
    assert body["external"][0]["youtube_id"] == "yt1"


@pytest.mark.asyncio
async def test_search_external_failure_returns_playlist_results():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.downloader.search_ytdlp_metadata", side_effect=RuntimeError("yt-dlp unavailable")):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/api/search?q=Song")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) >= 1
    assert body["external"] == []


# ---------------------------------------------------------------------------
# Listener requests and add-external
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listener_request_valid_shoutout():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.streamer._download_listener_song", new_callable=AsyncMock) as dl_mock:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/listener-request", json={"name": "Luca", "message": "Ciao a tutti!"})
        await asyncio.sleep(0)
    assert resp.status_code == 200
    assert resp.json()["type"] == "shoutout"
    assert len(app.state.station_state.pending_requests) == 1
    assert dl_mock.await_count == 0


@pytest.mark.asyncio
async def test_listener_request_valid_song_starts_background_download():
    app = _make_test_app()
    app.state.config.allow_ytdlp = True  # song_request classification requires ytdlp enabled
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.streamer._download_listener_song", new_callable=AsyncMock) as dl_mock:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/api/listener-request", json={"name": "Luca", "message": "puoi mettere Albachiara?"}
            )
        await asyncio.sleep(0)
    assert resp.status_code == 200
    assert resp.json()["type"] == "song_request"
    assert dl_mock.await_count == 1


@pytest.mark.asyncio
async def test_listener_request_rate_limited_alt_client():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post("/api/listener-request", json={"name": "A", "message": "ciao"})
        second = await client.post("/api/listener-request", json={"name": "B", "message": "ciao ancora"})
    assert first.status_code == 200
    assert second.status_code == 429
    assert "retry_after" in second.json()


@pytest.mark.asyncio
async def test_listener_request_queue_full_prefilled_state():
    app = _make_test_app()
    app.state.station_state.pending_requests = [{"message": f"m{i}"} for i in range(10)]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/listener-request", json={"name": "Luca", "message": "ciao"})
    assert resp.status_code == 429
    assert resp.json()["error"] == "queue_full"


@pytest.mark.asyncio
async def test_listener_request_invalid_payload_types():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        bad_payload = await client.post("/api/listener-request", json=["not", "an", "object"])
        bad_name = await client.post("/api/listener-request", json={"name": 123, "message": "ciao"})
        bad_message = await client.post("/api/listener-request", json={"name": "Luca", "message": 456})
    assert bad_payload.status_code == 400
    assert bad_payload.json()["error"] == "invalid payload"
    assert bad_name.status_code == 400
    assert bad_message.status_code == 400


@pytest.mark.asyncio
async def test_listener_request_song_keyword_treated_as_shoutout_when_ytdlp_disabled():
    app = _make_test_app()
    app.state.config.allow_ytdlp = False
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.streamer._download_listener_song", new_callable=AsyncMock) as dl_mock:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/api/listener-request", json={"name": "Luca", "message": "puoi mettere Albachiara?"}
            )
        await asyncio.sleep(0)
    assert resp.status_code == 200
    assert resp.json()["type"] == "shoutout"
    assert dl_mock.await_count == 0


@pytest.mark.asyncio
async def test_get_listener_requests_returns_age():
    app = _make_test_app()
    now = time.time()
    app.state.station_state.pending_requests = [
        {
            "name": "Marta",
            "message": "Ciao",
            "type": "shoutout",
            "song_found": False,
            "song_error": False,
            "song_track": None,
            "ts": now - 8,
        }
    ]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/listener-requests")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["requests"]) == 1
    assert body["requests"][0]["age_s"] >= 8


@pytest.mark.asyncio
async def test_add_external_track_success(tmp_path):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.allow_ytdlp = True
    original_len = len(app.state.station_state.playlist)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch(
        "mammamiradio.downloader.download_external_track",
        new_callable=AsyncMock,
        return_value=tmp_path / "dl.mp3",
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/api/playlist/add-external",
                json={"youtube_id": "abc123", "title": "Brano", "artist": "Artista", "duration_ms": 123000},
            )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert len(app.state.station_state.playlist) == original_len + 1
    assert app.state.station_state.pinned_track is not None
    assert app.state.station_state.pinned_track.youtube_id == "abc123"


@pytest.mark.asyncio
async def test_add_external_track_missing_youtube_id():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/playlist/add-external", json={"title": "x", "artist": "y", "duration_ms": 1000})
    assert resp.status_code == 400
    assert resp.json()["error"] == "youtube_id required"


@pytest.mark.asyncio
async def test_add_external_track_invalid_duration():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/playlist/add-external",
            json={"youtube_id": "abc123", "title": "Brano", "artist": "Artista", "duration_ms": "abc"},
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid duration_ms"


@pytest.mark.asyncio
async def test_add_external_track_invalid_payload():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/playlist/add-external", json=["not", "an", "object"])
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid payload"


@pytest.mark.asyncio
async def test_add_external_track_rejected_when_ytdlp_disabled():
    app = _make_test_app()
    app.state.config.allow_ytdlp = False
    original_len = len(app.state.station_state.playlist)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/playlist/add-external",
            json={"youtube_id": "abc123", "title": "Brano", "artist": "Artista", "duration_ms": 123000},
        )
    assert resp.status_code == 409
    assert resp.json()["error"] == "external_downloads_disabled"
    assert len(app.state.station_state.playlist) == original_len
    assert app.state.station_state.pinned_track is None


@pytest.mark.asyncio
async def test_download_listener_song_success(tmp_path):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    state = app.state.station_state
    original_len = len(state.playlist)
    req = {"song_query": "albachiara", "message": "metti albachiara", "song_found": False, "song_error": False}
    state.pending_requests.append(req)
    with (
        patch(
            "mammamiradio.downloader.search_ytdlp_metadata",
            return_value=[
                {"title": "Albachiara", "artist": "Vasco Rossi", "duration_ms": 120000, "youtube_id": "yt123"}
            ],
        ),
        patch(
            "mammamiradio.downloader.download_external_track",
            new_callable=AsyncMock,
            return_value=tmp_path / "song.mp3",
        ),
    ):
        await _download_listener_song(req, app.state, state.playlist_revision)
    assert req["song_found"] is True
    assert req["song_error"] is False
    assert req["song_track"] == "Vasco Rossi – Albachiara"
    assert req["song_track_obj"].display == "Vasco Rossi – Albachiara"
    assert state.pinned_track is not None
    assert len(state.playlist) == original_len + 1


@pytest.mark.asyncio
async def test_download_listener_song_no_results_marks_error(tmp_path):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    state = app.state.station_state
    original_len = len(state.playlist)
    req = {"song_query": "missing", "message": "missing", "song_found": False, "song_error": False}
    state.pending_requests.append(req)
    with patch("mammamiradio.downloader.search_ytdlp_metadata", return_value=[]):
        await _download_listener_song(req, app.state, state.playlist_revision)
    assert req["song_found"] is False
    assert req["song_error"] is True
    assert len(state.playlist) == original_len
    assert state.pinned_track is None


@pytest.mark.asyncio
async def test_download_listener_song_drops_track_on_revision_change(tmp_path):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    state = app.state.station_state
    original_len = len(state.playlist)
    req = {"song_query": "track", "message": "track", "song_found": False, "song_error": False}
    state.pending_requests.append(req)

    async def _download_with_revision_bump(*_args, **_kwargs):
        state.playlist_revision += 1
        return tmp_path / "song.mp3"

    with (
        patch(
            "mammamiradio.downloader.search_ytdlp_metadata",
            return_value=[{"title": "Track", "artist": "Artist", "duration_ms": 120000, "youtube_id": "yt987"}],
        ),
        patch(
            "mammamiradio.downloader.download_external_track",
            new_callable=AsyncMock,
            side_effect=_download_with_revision_bump,
        ),
    ):
        await _download_listener_song(req, app.state, state.playlist_revision)
    assert req["song_found"] is False
    assert req["song_error"] is False
    assert len(state.playlist) == original_len
    assert state.pinned_track is None


@pytest.mark.asyncio
async def test_download_listener_song_download_exception_marks_error(tmp_path):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    state = app.state.station_state
    original_len = len(state.playlist)
    req = {"song_query": "track", "message": "track", "song_found": False, "song_error": False}
    state.pending_requests.append(req)
    with (
        patch(
            "mammamiradio.downloader.search_ytdlp_metadata",
            return_value=[{"title": "Track", "artist": "Artist", "duration_ms": 120000, "youtube_id": "yt987"}],
        ),
        patch(
            "mammamiradio.downloader.download_external_track",
            new_callable=AsyncMock,
            side_effect=RuntimeError("download failed"),
        ),
    ):
        await _download_listener_song(req, app.state, state.playlist_revision)
    assert req["song_found"] is False
    assert req["song_error"] is True
    assert len(state.playlist) == original_len
    assert state.pinned_track is None


@pytest.mark.asyncio
async def test_download_listener_song_non_head_request_does_not_pin_out_of_order(tmp_path):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    state = app.state.station_state
    first_req = {"song_query": "first", "message": "metti first", "song_found": False, "song_error": False}
    second_req = {"song_query": "second", "message": "metti second", "song_found": False, "song_error": False}
    state.pending_requests.extend([first_req, second_req])

    with (
        patch(
            "mammamiradio.downloader.search_ytdlp_metadata",
            return_value=[{"title": "Second", "artist": "Artist 2", "duration_ms": 120000, "youtube_id": "yt2"}],
        ),
        patch(
            "mammamiradio.downloader.download_external_track",
            new_callable=AsyncMock,
            return_value=tmp_path / "second.mp3",
        ),
    ):
        await _download_listener_song(second_req, app.state, state.playlist_revision)

    assert second_req["song_found"] is True
    assert second_req["song_track"] == "Artist 2 – Second"
    assert second_req["song_track_obj"].display == "Artist 2 – Second"
    assert state.pending_requests[0] is first_req
    assert state.pinned_track is None
    assert state.force_next is None


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
    with patch(
        "mammamiradio.streamer.load_explicit_source",
        side_effect=ExplicitSourceError("Charts unavailable"),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/playlist/load", json={"url": "https://example.com/playlist/empty"})
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
    assert isinstance(resp.json(), dict)


# ---------------------------------------------------------------------------
# Auth edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hassio_ingress_auth_bypass():
    """HA addon ingress should land directly on the admin panel and bypass auth."""
    app = _make_test_app(is_addon=True)
    # Hassio internal network: 172.30.32.x
    transport = httpx.ASGITransport(app=app, client=("172.30.32.5", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})
        assert resp.status_code == 200
        assert "Regia — Control Room" in resp.text
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
        resp = await client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})
        assert resp.status_code == 200
        assert "Regia — Control Room" not in resp.text
        # /dashboard requires admin auth — spoofed ingress should NOT bypass
        resp = await client.get("/dashboard", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_supervisor_network_addon_fully_trusted():
    """HA Supervisor network (172.30.32.x) is fully trusted in addon mode, including POST."""
    app = _make_test_app(is_addon=True)
    transport = httpx.ASGITransport(app=app, client=("172.30.32.5", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/shuffle")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_basic_auth_mutation_requires_same_origin_or_csrf():
    app = _make_test_app(admin_password="secret")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/shuffle", headers=_basic_auth_header())
    assert resp.status_code == 403
    assert "Cross-site admin write blocked" in resp.text


@pytest.mark.asyncio
async def test_basic_auth_mutation_allows_same_origin():
    app = _make_test_app(admin_password="secret")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
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
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
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
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
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
async def test_token_auth_public_ip_requires_token():
    """Token-only auth: public IP without token should fail."""
    app = _make_test_app(admin_token="tok-123")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_token_auth_private_network_trusted():
    """Private network (RFC1918) should be trusted without token."""
    app = _make_test_app(admin_token="tok-123")
    transport = httpx.ASGITransport(app=app, client=("10.0.0.1", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_private_network_mutation_requires_csrf():
    """Private network POST without origin/CSRF should be blocked (cross-site protection)."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/shuffle")
    assert resp.status_code == 403
    assert "Cross-site" in resp.text


@pytest.mark.asyncio
async def test_private_network_mutation_allows_same_origin():
    """Private network POST with same-origin header should succeed."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/shuffle", headers={"Origin": "http://testserver"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_private_network_read_no_csrf_needed():
    """Private network GET should succeed without CSRF."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_token_auth_non_loopback_with_valid_token():
    app = _make_test_app(admin_token="tok-123")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
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


@pytest.mark.asyncio
async def test_listener_page_includes_casa_card_and_public_status_binding():
    """Listener UI must render HA moments from /public-status via Casa card IDs."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/listen")

    assert resp.status_code == 200
    assert 'id="casa-card"' in resp.text
    assert 'id="casa-mood"' in resp.text
    assert "updateCasa(data.ha_moments);" in resp.text
    assert "fetch(_base + '/public-status')" in resp.text


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


# ---------------------------------------------------------------------------
# Listener requests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listener_request_shoutout():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/listener-request", json={"name": "Marco", "message": "Ciao a tutti!"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["type"] == "shoutout"
    assert len(app.state.station_state.pending_requests) == 1


@pytest.mark.asyncio
async def test_listener_request_missing_message_rejected():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/listener-request", json={"name": "Marco"})
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


@pytest.mark.asyncio
async def test_listener_request_rate_limited():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("10.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        r1 = await client.post("/api/listener-request", json={"name": "A", "message": "primo"})
        r2 = await client.post("/api/listener-request", json={"name": "A", "message": "secondo"})
    assert r1.status_code == 200
    assert r2.status_code == 429
    assert "retry_after" in r2.json()


@pytest.mark.asyncio
async def test_listener_request_queue_full():
    app = _make_test_app()
    state = app.state.station_state
    # Pre-fill the queue with 10 entries (the cap)
    for i in range(10):
        state.pending_requests.append({"name": f"U{i}", "message": f"msg{i}", "ts": 0})
    transport = httpx.ASGITransport(app=app, client=("99.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/listener-request", json={"name": "Late", "message": "ciao"})
    assert resp.status_code == 429
    assert resp.json()["error"] == "queue_full"


@pytest.mark.asyncio
async def test_get_listener_requests_returns_queue():
    app = _make_test_app()
    import time as _time

    state = app.state.station_state
    state.pending_requests.append(
        {"name": "Giulia", "message": "metti Volare", "type": "song_request", "ts": _time.time()}
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/listener-requests")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["requests"]) == 1
    assert data["requests"][0]["name"] == "Giulia"


# ---------------------------------------------------------------------------
# Track rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_track_rules_missing_fields():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/track-rules", json={"youtube_id": "abc123"})
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


@pytest.mark.asyncio
async def test_track_rules_saves_rule(tmp_path):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    # Ensure DB exists so add_rule can write to it
    from mammamiradio.sync import init_db

    init_db(tmp_path / "mammamiradio.db")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/track-rules", json={"youtube_id": "dQw4w9WgXcQ", "rule": "plays too often"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# Add track endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_track_appends_to_playlist():
    app = _make_test_app()
    initial_len = len(app.state.station_state.playlist)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/playlist/add", json={"title": "Azzurro", "artist": "Celentano", "duration_ms": 200000}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert len(app.state.station_state.playlist) == initial_len + 1


@pytest.mark.asyncio
async def test_add_track_inserts_at_next_position():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/playlist/add",
            json={"title": "Priority Track", "artist": "DJ", "duration_ms": 180000, "position": "next"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["position"] == "next"
    assert app.state.station_state.playlist[0].title == "Priority Track"


# ---------------------------------------------------------------------------
# Pacing endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pacing_returns_config():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/pacing")
    assert resp.status_code == 200
    body = resp.json()
    assert "songs_between_banter" in body
    assert "songs_between_ads" in body
    assert "ad_spots_per_break" in body


@pytest.mark.asyncio
async def test_patch_pacing_updates_values():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.patch(
            "/api/pacing",
            json={"songs_between_banter": 3, "songs_between_ads": 6, "ad_spots_per_break": 2},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["songs_between_banter"] == 3
    assert body["songs_between_ads"] == 6
    assert body["ad_spots_per_break"] == 2


@pytest.mark.asyncio
async def test_patch_pacing_enforces_floor():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.patch("/api/pacing", json={"songs_between_banter": 0})
    assert resp.status_code == 200
    # Floor of 1 enforced
    assert resp.json()["songs_between_banter"] == 1


# ---------------------------------------------------------------------------
# Credentials endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credentials_no_recognised_fields():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/credentials", json={"unknown_field": "value"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "error" in body


@pytest.mark.asyncio
async def test_credentials_saves_valid_key(tmp_path):
    """Valid anthropic_api_key updates config and triggers file write."""
    app = _make_test_app()
    # Patch run_in_executor so no actual .env write happens
    with patch("asyncio.AbstractEventLoop.run_in_executor", return_value=None):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/credentials", json={"anthropic_api_key": "sk-test-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "ANTHROPIC_API_KEY" in body["saved"]


# ---------------------------------------------------------------------------
# Clip sharing endpoints
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def _clear_clip_rate():
    """Clear clip rate limiter state before each clip test."""
    from mammamiradio.streamer import _clip_rate

    _clip_rate.clear()
    yield
    _clip_rate.clear()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clear_clip_rate")
async def test_clip_create_empty_ring_buffer():
    """POST /api/clip returns error when ring buffer is empty."""
    app = _make_test_app()
    from collections import deque

    app.state.clip_ring_buffer = deque(maxlen=240)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/clip")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "No audio" in body["error"] or "Buffer" in body["error"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clear_clip_rate")
async def test_clip_create_no_ring_buffer():
    """POST /api/clip returns error when clip_ring_buffer is missing."""
    app = _make_test_app()
    # No clip_ring_buffer set at all
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/clip")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clear_clip_rate")
async def test_clip_create_with_data(tmp_path):
    """POST /api/clip extracts and saves a clip when buffer has data."""
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    app.state.config.cache_dir.mkdir()
    app.state.config.audio.bitrate = 192
    from collections import deque

    ring = deque(maxlen=240)
    # Fill with some fake audio chunks
    for _ in range(10):
        ring.append(b"\xff" * 4096)
    app.state.clip_ring_buffer = ring

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/clip")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "clip_id" in body
    assert body["url"].startswith("/clips/")


@pytest.mark.asyncio
async def test_clip_serve_valid(tmp_path):
    """GET /clips/{id}.mp3 serves an existing clip file."""
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    clips_dir = app.state.config.cache_dir / "clips"
    clips_dir.mkdir(parents=True)
    clip_file = clips_dir / "abc123.mp3"
    clip_file.write_bytes(b"\xff" * 1000)

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/clips/abc123.mp3")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_clip_serve_missing(tmp_path):
    """GET /clips/{id}.mp3 returns 404 for nonexistent clip."""
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    (tmp_path / "cache" / "clips").mkdir(parents=True)

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/clips/nonexistent.mp3")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_clip_serve_path_traversal(tmp_path):
    """GET /clips/{id}.mp3 rejects clip IDs containing '..'."""
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Use a clip_id that contains '..' but no slashes (slashes won't match the route)
        resp = await client.get("/clips/..evil..thing.mp3")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "Invalid" in body["error"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clear_clip_rate")
async def test_clip_rate_limiting(tmp_path):
    """POST /api/clip rate limits to 1 clip per 10s per IP."""
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    app.state.config.cache_dir.mkdir()
    app.state.config.audio.bitrate = 192
    from collections import deque

    ring = deque(maxlen=240)
    for _ in range(10):
        ring.append(b"\xff" * 4096)
    app.state.clip_ring_buffer = ring

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # First request should succeed
        resp1 = await client.post("/api/clip")
        assert resp1.status_code == 200
        assert resp1.json()["ok"] is True

        # Second request within 10s should be rate limited
        resp2 = await client.post("/api/clip")
        assert resp2.status_code == 429


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clear_clip_rate")
async def test_clip_rate_prune_keeps_recent_entries():
    """Clip limiter pruning drops stale IPs without clearing recent limits."""
    app = _make_test_app()
    from collections import deque

    from mammamiradio import streamer as streamer_mod

    # Empty ring buffer is enough: pruning happens before clip extraction.
    app.state.clip_ring_buffer = deque(maxlen=240)
    now = 1_700_000_000.0
    streamer_mod._clip_rate["198.51.100.1"] = now - 5
    streamer_mod._clip_rate["198.51.100.2"] = now - 301

    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 12345))
    with patch("mammamiradio.streamer.time.time", return_value=now):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/clip")

    assert resp.status_code == 200
    assert streamer_mod._clip_rate["198.51.100.1"] == pytest.approx(now - 5)
    assert "198.51.100.2" not in streamer_mod._clip_rate
    assert streamer_mod._clip_rate["203.0.113.9"] == pytest.approx(now)


# ---------------------------------------------------------------------------
# HA moments (Casa card) — public-status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_status_ha_moments_absent_when_no_ha_context():
    """ha_moments is None when HA context is not set."""
    app = _make_test_app()
    app.state.station_state.ha_context = ""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    assert resp.json()["ha_moments"] is None


@pytest.mark.asyncio
async def test_public_status_ha_moments_present_with_mood():
    """ha_moments carries mood and weather when HA context is active."""
    app = _make_test_app()
    state = app.state.station_state
    state.ha_context = "some HA context"
    state.ha_home_mood = "Serata cinema"
    state.ha_weather_arc = "Meteo: soleggiato, 22°C."
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    ha = resp.json()["ha_moments"]
    assert ha is not None
    assert ha["mood"] == "Serata cinema"
    assert ha["weather"] == "Meteo: soleggiato, 22°C."


@pytest.mark.asyncio
async def test_public_status_ha_moments_hidden_when_empty():
    """ha_moments is None when HA is connected but no mood/weather/event to show."""
    app = _make_test_app()
    state = app.state.station_state
    state.ha_context = "some HA context"
    state.ha_home_mood = ""
    state.ha_weather_arc = ""
    state.ha_last_event_label = ""
    state.ha_last_event_ts = 0.0
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    assert resp.json()["ha_moments"] is None


@pytest.mark.asyncio
async def test_public_status_ha_moments_event_within_retention():
    """ha_moments includes last_event_label when the event is within 30 min."""
    app = _make_test_app()
    state = app.state.station_state
    state.ha_context = "some HA context"
    state.ha_home_mood = ""
    state.ha_weather_arc = ""
    now = 1_700_000_000.0
    state.ha_last_event_label = "Luci terrazza"
    state.ha_last_event_ts = now - 120  # 2 minutes ago
    with patch("mammamiradio.streamer.time.time", return_value=now):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/public-status")
    assert resp.status_code == 200
    ha = resp.json()["ha_moments"]
    assert ha is not None
    assert ha["last_event_label"] == "Luci terrazza"
    assert ha["last_event_ago_min"] == 2


@pytest.mark.asyncio
async def test_public_status_ha_moments_event_outside_retention():
    """ha_moments omits stale events older than EVENT_RETENTION_SECONDS."""
    app = _make_test_app()
    state = app.state.station_state
    state.ha_context = "some HA context"
    state.ha_home_mood = "Serata cinema"
    state.ha_weather_arc = ""
    now = 1_700_000_000.0
    state.ha_last_event_label = "Stale event"
    state.ha_last_event_ts = now - 2000  # ~33 min ago, beyond 30 min window
    with patch("mammamiradio.streamer.time.time", return_value=now):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/public-status")
    assert resp.status_code == 200
    ha = resp.json()["ha_moments"]
    assert ha is not None
    assert "last_event_label" not in ha


# ---------------------------------------------------------------------------
# HA details — admin /status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_status_ha_details_absent_when_no_ha_context():
    """ha_details is None in /status when HA context is not set."""
    app = _make_test_app(admin_token="secret-tok")
    app.state.station_state.ha_context = ""
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status", headers={"Authorization": "Bearer secret-tok"})
    assert resp.status_code == 200
    assert resp.json()["ha_details"] is None


@pytest.mark.asyncio
async def test_admin_status_ha_details_present_with_full_context():
    """ha_details carries mood, weather_arc, events_summary, and event counts."""
    app = _make_test_app(admin_token="secret-tok")
    state = app.state.station_state
    state.ha_context = "some HA context"
    state.ha_home_mood = "Lavatrice in funzione"
    state.ha_weather_arc = "Meteo: nuvoloso, 15°C."
    state.ha_events_summary = "- Lavatrice: inattivo → 450 W"
    state.ha_recent_event_count = 3
    state.ha_last_event_label = "Lavatrice (consumo)"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status", headers={"Authorization": "Bearer secret-tok"})
    assert resp.status_code == 200
    hd = resp.json()["ha_details"]
    assert hd is not None
    assert hd["mood"] == "Lavatrice in funzione"
    assert hd["weather_arc"] == "Meteo: nuvoloso, 15°C."
    assert hd["events_summary"] == "- Lavatrice: inattivo → 450 W"
    assert hd["recent_event_count"] == 3
    assert hd["last_event_label"] == "Lavatrice (consumo)"
