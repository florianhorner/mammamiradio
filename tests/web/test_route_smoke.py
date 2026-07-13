"""HTTP route smoke for the listener-facing public surface.

These tests pin the durable contract for the routes a browser hits when
loading the listener page: the root and /listen HTML, the public status
poll, the static assets the PWA depends on, the service worker, and the
public listener-request POST. Auth helpers, CSRF, and admin control-plane
contracts are covered elsewhere (test_streamer_coverage.py,
test_ui_control_contracts.py); this file covers what /qa was the only
persistent check for.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mammamiradio.core.config import load_config
from mammamiradio.core.models import StationState, Track
from mammamiradio.web.listener_requests import router as listener_requests_router
from mammamiradio.web.streamer import LiveStreamHub, router

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")
ROOT = Path(__file__).resolve().parents[2]
CANONICAL_LOGO = ROOT / "mammamiradio" / "assets" / "logo.svg"
FAVICON = ROOT / "mammamiradio" / "web" / "static" / "favicon.svg"
TEMPLATES = ROOT / "mammamiradio" / "web" / "templates"


def _make_app() -> FastAPI:
    """Minimal FastAPI app wired with the same state shape as the production lifespan.

    Mirrors the pattern used by test_ui_control_contracts.py:71-102 but trimmed
    to only the state fields the listener-facing routes actually read.
    """
    app = FastAPI()
    app.include_router(router)
    app.include_router(listener_requests_router)
    config = load_config(TOML_PATH)
    config.admin_token = "test-admin-token"
    config.admin_password = ""

    state = StationState(
        playlist=[Track(title="Song A", artist="Artist", duration_ms=180_000, spotify_id="s1")],
    )

    q: asyncio.Queue = asyncio.Queue()
    hub = LiveStreamHub()
    hub.bind_state(state)

    app.state.queue = q
    app.state.skip_event = asyncio.Event()
    app.state.source_switch_lock = asyncio.Lock()
    app.state.stream_hub = hub
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    return app


# ---------------------------------------------------------------------------
# Listener HTML routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_root_returns_listener_html():
    """GET / -> 200, text/html. The listener page is the public landing surface."""
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<html" in resp.text.lower()


@pytest.mark.asyncio
async def test_listen_alias_returns_listener_html():
    """GET /listen -> 200, text/html. Backwards-compatible alias for the listener UI."""
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/listen")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


# ---------------------------------------------------------------------------
# Public status poll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_status_returns_json_with_contract_keys():
    """GET /public-status -> 200, JSON, with the keys the listener UI polls every ~3s."""
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/public-status")
    assert resp.status_code == 200
    payload = resp.json()
    for key in ("station", "now_streaming", "session_stopped", "runtime_health"):
        assert key in payload, f"public-status payload missing {key!r}"


# ---------------------------------------------------------------------------
# PWA static assets and service worker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_listener_css_served():
    """GET /static/listener.css -> 200, css content-type. The listener page can't render without it."""
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/static/listener.css")
    assert resp.status_code == 200
    assert "css" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_static_manifest_served_as_json():
    """GET /static/manifest.json -> 200, valid JSON. PWA install fails without this."""
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/static/manifest.json")
    assert resp.status_code == 200
    json.loads(resp.text)


@pytest.mark.asyncio
async def test_service_worker_served_with_root_scope_header():
    """GET /sw.js -> 200, JS content-type, Service-Worker-Allowed: /.

    Without the Service-Worker-Allowed header the SW (served at /sw.js) cannot
    register at root scope and the PWA silently degrades.
    """
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/sw.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert resp.headers.get("service-worker-allowed") == "/"


@pytest.mark.asyncio
async def test_favicon_default_path_served():
    """GET /favicon.ico serves the canonical app logo for browser fallbacks."""
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/favicon.ico")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert resp.content == CANONICAL_LOGO.read_bytes()


def test_favicon_asset_matches_canonical_app_logo():
    """Keep the browser-tab asset synchronized with the documented logo source."""
    assert FAVICON.read_bytes() == CANONICAL_LOGO.read_bytes()


@pytest.mark.parametrize(
    ("template_name", "favicon_href"),
    [
        ("listener.html", "{{ ingress_prefix }}/static/favicon.svg"),
        ("admin.html", "/static/favicon.svg"),
        ("clip.html", "{{ ingress_prefix }}/static/favicon.svg"),
    ],
)
def test_browser_templates_reference_app_logo_favicon(template_name: str, favicon_href: str):
    """Every browser page must use the app logo rather than a PWA placeholder."""
    html = (TEMPLATES / template_name).read_text()
    assert f'<link rel="icon" href="{favicon_href}" type="image/svg+xml">' in html


@pytest.mark.asyncio
async def test_static_path_traversal_rejected():
    """GET /static/../etc/passwd -> 404. The handler's is_relative_to check must hold."""
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/static/../../../etc/passwd")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Public listener-request POST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listener_request_accepts_unauthenticated_post():
    """POST /api/listener-request with a valid body -> 200 from a public client.

    The dedica form on the listener page calls this endpoint without auth. If a
    future refactor adds Depends(require_admin_access) the form silently breaks.
    """
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/listener-request", json={"name": "Marco", "message": "Ciao"})
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("ok") is True
    assert len(app.state.station_state.pending_requests) == 1


@pytest.mark.asyncio
async def test_listener_request_rejects_empty_message():
    """POST /api/listener-request with no message -> 400. Empty messages are dropped at the gate."""
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/listener-request", json={"name": "Marco", "message": "   "})
    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"name": "Melóni", "message": "Ciao"},
        {"name": "Marco", "message": "Una dedica per meloni"},
    ],
)
async def test_listener_request_blocked_name_rejects_without_queueing(payload):
    """Configured blocked names reject before a listener request reaches the queue."""
    app = _make_app()
    app.state.config.moderation.blocked_names = ["Meloni"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/listener-request", json=payload)

    assert resp.status_code == 400
    body = resp.json()
    assert body == {"ok": False, "error": "request not accepted"}
    assert "Meloni" not in resp.text
    assert "block" not in resp.text.lower()
    assert app.state.station_state.pending_requests == []


@pytest.mark.asyncio
async def test_listener_request_blocked_name_does_not_consume_rate_limit():
    """A static moderation rejection must not burn the 30s per-IP submit window."""
    app = _make_app()
    app.state.config.moderation.blocked_names = ["Meloni"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        blocked = await c.post("/api/listener-request", json={"name": "Meloni", "message": "Ciao"})
        allowed = await c.post("/api/listener-request", json={"name": "Marco", "message": "Ciao"})

    assert blocked.status_code == 400
    assert blocked.json()["ok"] is False
    assert allowed.status_code == 200
    assert allowed.json()["ok"] is True
    assert len(app.state.station_state.pending_requests) == 1


@pytest.mark.asyncio
async def test_listener_request_blocked_name_not_split_by_truncation():
    """A blocked name padded past the sanitizer length cap must still reject.

    The gate matches the raw input, so a name placed at the 60/200-char
    truncation boundary cannot be split (and thus bypass the word-boundary
    match) by sanitization's ellipsis clip.
    """
    app = _make_app()
    app.state.config.moderation.blocked_names = ["Meloni"]

    name_pad = "a" * 58 + " Meloni"  # blocked word lands past the 60-char name cap
    message_pad = "ciao " * 40 + "Meloni"  # past the 200-char message cap

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        by_name = await c.post("/api/listener-request", json={"name": name_pad, "message": "Ciao"})
        by_message = await c.post("/api/listener-request", json={"name": "Marco", "message": message_pad})

    assert by_name.status_code == 400
    assert by_name.json() == {"ok": False, "error": "request not accepted"}
    assert by_message.status_code == 400
    assert by_message.json() == {"ok": False, "error": "request not accepted"}
    assert app.state.station_state.pending_requests == []
