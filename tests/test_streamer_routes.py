"""Tests for LiveStreamHub, HTTP routes, and admin auth in streamer.py."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.config import load_config
from mammamiradio.models import Segment, SegmentType, StationState, Track
from mammamiradio.streamer import LiveStreamHub, router

TOML_PATH = str(Path(__file__).parent.parent / "radio.toml")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_app(*, admin_password: str = "", admin_token: str = "") -> FastAPI:
    """Build a minimal FastAPI app with the streamer router and populated state."""
    app = FastAPI()
    app.include_router(router)

    config = load_config(TOML_PATH)
    # Override auth settings for test isolation
    config.admin_password = admin_password
    config.admin_token = admin_token

    state = StationState(
        playlist=[Track(title="Test Song", artist="Test Artist", duration_ms=180_000, spotify_id="t1")],
    )

    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    hub = LiveStreamHub()
    hub.bind_state(state)
    app.state.stream_hub = hub
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    return app


# ---------------------------------------------------------------------------
# LiveStreamHub -- pure async unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_returns_id_and_queue():
    hub = LiveStreamHub()
    lid, q = hub.subscribe()
    assert isinstance(lid, int)
    assert isinstance(q, asyncio.Queue)
    assert hub.has_listener(lid)


@pytest.mark.asyncio
async def test_subscribe_increments_id():
    hub = LiveStreamHub()
    id1, _ = hub.subscribe()
    id2, _ = hub.subscribe()
    assert id2 == id1 + 1


@pytest.mark.asyncio
async def test_unsubscribe_removes_listener():
    hub = LiveStreamHub()
    lid, _ = hub.subscribe()
    hub.unsubscribe(lid)
    assert not hub.has_listener(lid)


@pytest.mark.asyncio
async def test_has_listener_false_for_unknown():
    hub = LiveStreamHub()
    assert not hub.has_listener(999)


@pytest.mark.asyncio
async def test_broadcast_pushes_to_all():
    hub = LiveStreamHub()
    _, q1 = hub.subscribe()
    _, q2 = hub.subscribe()
    chunk = b"audio-data"
    await hub.broadcast(chunk)
    assert q1.get_nowait() == chunk
    assert q2.get_nowait() == chunk


@pytest.mark.asyncio
async def test_broadcast_drops_slow_listeners():
    hub = LiveStreamHub(listener_queue_size=1)
    lid, q = hub.subscribe()
    # Fill the queue so the listener is slow
    q.put_nowait(b"old")
    await hub.broadcast(b"new")
    # Slow listener should have been dropped
    assert not hub.has_listener(lid)


@pytest.mark.asyncio
async def test_close_sends_none():
    hub = LiveStreamHub()
    _, q1 = hub.subscribe()
    _, q2 = hub.subscribe()
    hub.close()
    assert q1.get_nowait() is None
    assert q2.get_nowait() is None


@pytest.mark.asyncio
async def test_close_clears_listeners():
    hub = LiveStreamHub()
    lid, _ = hub.subscribe()
    hub.close()
    assert not hub.has_listener(lid)


# ---------------------------------------------------------------------------
# Route tests -- using httpx.AsyncClient with ASGITransport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_listen_returns_html():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/listen")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_get_root_serves_listener_page():
    """Root serves the public listener page (no auth required)."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Mamma Mi Radio" in resp.text


@pytest.mark.asyncio
async def test_public_status_returns_json():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    body = resp.json()
    assert "station" in body
    assert "now_streaming" in body
    assert "upcoming" in body
    assert "upcoming_mode" in body
    assert "stream_log" in body


@pytest.mark.asyncio
async def test_public_status_upcoming_mode_shows_predictions_when_queue_empty():
    app = _make_test_app()
    # Queue is empty -- predictions from playlist are shown instead
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["upcoming"]) > 0
    assert all(item["source"] == "predicted_from_playlist" for item in body["upcoming"])
    assert body["upcoming_mode"] == "queued"


@pytest.mark.asyncio
async def test_public_status_upcoming_mode_queued_with_shadow_queue():
    app = _make_test_app()
    app.state.queue.put_nowait(Segment(type=SegmentType.MUSIC, path=Path("/tmp/fake.mp3"), metadata={}))
    app.state.station_state.queued_segments = [{"type": "music", "label": "Queued Song"}]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["upcoming"] == [{"type": "music", "label": "Queued Song", "source": "rendered_queue"}]
    assert body["upcoming_mode"] == "queued"


@pytest.mark.asyncio
async def test_setup_status_returns_onboarding_payload():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/setup/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "detected_mode" in body
    assert "essentials" in body
    assert "preflight_checks" in body
    assert "launch" in body
    assert "signature" in body


@pytest.mark.asyncio
async def test_status_includes_station_mode():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "station_mode" in body
    assert "id" in body["station_mode"]


@pytest.mark.asyncio
async def test_setup_recheck_returns_onboarding_payload():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/setup/recheck")
    assert resp.status_code == 200
    body = resp.json()
    assert "detected_mode" in body
    assert "station_mode" in body
    assert "signature" in body


@pytest.mark.asyncio
async def test_addon_snippet_returns_snippet():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/setup/addon-snippet")
    assert resp.status_code == 200
    body = resp.json()
    assert "snippet" in body


@pytest.mark.asyncio
async def test_setup_save_keys_updates_live_config_without_disk_write():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    with patch("mammamiradio.streamer._save_dotenv") as save_dotenv:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/api/setup/save-keys",
                json={"ANTHROPIC_API_KEY": "ant-test", "OPENAI_API_KEY": "openai-test"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "ANTHROPIC_API_KEY" in body["saved"]
    assert "OPENAI_API_KEY" in body["saved"]
    assert app.state.config.anthropic_api_key == "ant-test"
    assert app.state.config.openai_api_key == "openai-test"
    save_dotenv.assert_called_once()


@pytest.mark.asyncio
async def test_setup_save_keys_rejects_empty_payload():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/setup/save-keys", json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "No keys provided" in body["error"]


@pytest.mark.asyncio
async def test_admin_status_without_auth_non_loopback_rejected():
    """Non-loopback client without credentials should be rejected."""
    app = _make_test_app(admin_password="secret123")
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_status_with_basic_auth():
    app = _make_test_app(admin_password="secret123")
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status", auth=("admin", "secret123"))
    assert resp.status_code == 200
    body = resp.json()
    assert "queue_depth" in body
    assert "segments_produced" in body
    assert "runtime_health" in body


@pytest.mark.asyncio
async def test_admin_status_with_token():
    app = _make_test_app(admin_token="tok-abc-123")
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status", headers={"X-Radio-Admin-Token": "tok-abc-123"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_admin_status_bad_credentials():
    app = _make_test_app(admin_password="secret123")
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status", auth=("admin", "wrong"))
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_skip_with_admin_auth():
    app = _make_test_app(admin_password="secret123")
    # Put something in now_streaming so skip has something to act on
    app.state.station_state.now_streaming = {"type": "music", "label": "Test", "started": time.time()}
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/skip", auth=("admin", "secret123"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True


@pytest.mark.asyncio
async def test_stop_and_resume_toggle_session_state():
    app = _make_test_app()
    app.state.station_state.now_streaming = {"type": "music", "label": "Test", "started": time.time()}
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        stop = await client.post("/api/stop")
        assert stop.status_code == 200
        assert app.state.station_state.session_stopped is True
        assert app.state.station_state.now_streaming["type"] == "stopped"

        resume = await client.post("/api/resume")
        assert resume.status_code == 200
        assert app.state.station_state.session_stopped is False


@pytest.mark.asyncio
async def test_loopback_bypasses_auth_when_no_password():
    """Loopback client with no admin_password/token configured gets through."""
    app = _make_test_app()  # no password, no token
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_non_loopback_no_auth_configured_rejected():
    """Non-loopback with no auth configured gets 403."""
    app = _make_test_app()  # no password, no token
    transport = httpx.ASGITransport(app=app, client=("10.0.0.5", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# PWA static asset routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sw_js_returns_javascript():
    """GET /sw.js should return the service worker with correct content-type."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/sw.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "CACHE_NAME" in resp.text


@pytest.mark.asyncio
async def test_static_manifest_returns_json():
    """GET /static/manifest.json should serve the PWA manifest."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/static/manifest.json")
    assert resp.status_code == 200
    assert "Radio" in resp.text


@pytest.mark.asyncio
async def test_static_nonexistent_returns_404():
    """GET /static/nonexistent.txt should return 404."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/static/nonexistent.txt")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_static_path_traversal_blocked():
    """Path traversal attempts in /static/ should return 404."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/static/../streamer.py")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /admin route tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_panel_loopback_no_password_returns_html():
    """GET /admin on loopback with no credentials configured should return 200 HTML."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/admin")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_admin_panel_non_loopback_without_auth_rejected():
    """GET /admin from non-loopback without credentials should return 401."""
    app = _make_test_app(admin_password="secret")
    transport = httpx.ASGITransport(app=app, client=("10.0.0.1", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/admin")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_panel_with_basic_auth_returns_html():
    """GET /admin with valid basic auth should return 200 HTML."""
    app = _make_test_app(admin_password="secret")
    transport = httpx.ASGITransport(app=app, client=("10.0.0.1", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/admin", auth=("admin", "secret"))
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


# ---------------------------------------------------------------------------
# /api/capabilities route tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capabilities_loopback_returns_flags():
    """GET /api/capabilities on loopback returns capability flags and tier."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/capabilities")
    assert resp.status_code == 200
    body = resp.json()
    # Flags are nested under "capabilities"; top-level has tier, trial, etc.
    caps = body.get("capabilities", body)
    assert "llm" in caps
    assert "tier" in body
    assert "trial" in body
    assert "canned_clips_streamed" in body["trial"]


@pytest.mark.asyncio
async def test_capabilities_non_loopback_without_auth_rejected():
    """GET /api/capabilities from non-loopback without credentials returns 401."""
    app = _make_test_app(admin_password="secret")
    transport = httpx.ASGITransport(app=app, client=("192.168.1.5", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/capabilities")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_capabilities_openai_only_marks_ai_as_available():
    app = _make_test_app()
    app.state.config.openai_api_key = "openai-key"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/capabilities")
    assert resp.status_code == 200
    assert resp.json()["capabilities"]["llm"] is True
    assert resp.json()["next_step"]["key"] != "add_ai_key"


@pytest.mark.asyncio
async def test_capabilities_trial_exhausted_flag():
    """trial.exhausted is True when canned_clips_streamed >= limit."""
    from mammamiradio.producer import SHAREWARE_CANNED_LIMIT

    app = _make_test_app()
    app.state.station_state.canned_clips_streamed = SHAREWARE_CANNED_LIMIT
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/capabilities")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trial"]["exhausted"] is True
    assert body["trial"]["canned_clips_streamed"] == SHAREWARE_CANNED_LIMIT
