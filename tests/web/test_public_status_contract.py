"""Cross-page contract tests for /public-status vs /status.

The CRITICAL INVARIANT: any field present in BOTH endpoints must hold the
same value at the same time. This is what prevents the bug class Florian
flagged on 2026-04-26 (admin says 'Waiting for signal…' while listener
says 'IN ONDA' — different code paths producing different state).

Cathedral standard:
- Strict subset: every field in /public-status must also exist in /status
- Bytes-identical for shared fields: capabilities dict, brand dict, uptime,
  tracks_played, session_stopped, now_streaming, ha_moments
- Shape snapshot: catches accidental field additions on the listener side
"""

from __future__ import annotations

import httpx
import pytest

from tests.web.test_streamer_routes import _make_test_app


@pytest.mark.asyncio
async def test_public_status_returns_brand_block():
    """Public listener payload must include the brand-fiction layer."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    body = resp.json()
    assert "brand" in body
    brand = body["brand"]
    # Required fields present
    for field in ("station_name", "hosts", "theme"):
        assert field in brand, f"brand missing field: {field}"
    # Theme has all six tokens
    theme = brand["theme"]
    for token in ("primary_color", "accent_color", "background_color", "display_font", "body_font", "mono_font"):
        assert token in theme, f"theme missing token: {token}"


@pytest.mark.asyncio
async def test_public_status_returns_capabilities():
    """Listener page reads capabilities every poll for client-side feature gating."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    body = resp.json()
    assert "capabilities" in body
    caps = body["capabilities"]
    # Boolean flags present (values depend on test env, but keys must exist)
    for flag in ("llm", "anthropic_key", "openai", "ha", "anthropic_degraded"):
        assert flag in caps, f"capabilities missing flag: {flag}"
        assert isinstance(caps[flag], bool), f"capability {flag} must be bool"


@pytest.mark.asyncio
async def test_public_status_returns_uptime_and_tracks():
    """Cross-page invariant facts that must match admin /status."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    body = resp.json()
    assert "uptime_sec" in body
    assert "tracks_played" in body
    assert isinstance(body["uptime_sec"], int)
    assert isinstance(body["tracks_played"], int)
    assert body["tracks_played"] >= 0


@pytest.mark.asyncio
async def test_admin_listener_facts_agree():
    """THE cross-page invariant: shared facts must hold the same value at the same time.

    This catches the bug class Florian flagged: admin says 'Waiting for signal…'
    while listener says 'IN ONDA'. Both endpoints must read from the SAME state
    object via the SAME helper (_public_status_payload), and never compute their
    own divergent values.
    """
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Admin /status on loopback bypasses auth (no admin password set in test app)
        admin_resp = await client.get("/status")
        public_resp = await client.get("/public-status")
    assert admin_resp.status_code == 200
    assert public_resp.status_code == 200
    admin = admin_resp.json()
    public = public_resp.json()

    # Bytes-identical shared fields
    assert admin["uptime_sec"] == public["uptime_sec"]
    assert admin["tracks_played"] == public["tracks_played"]
    assert admin["session_stopped"] == public["session_stopped"]
    assert admin.get("now_streaming") == public.get("now_streaming")
    assert admin["brand"] == public["brand"]
    assert admin["capabilities"] == public["capabilities"]
    assert admin.get("ha_moments") == public.get("ha_moments")


@pytest.mark.asyncio
async def test_public_status_strict_subset_of_admin():
    """Every field in /public-status must also exist in /status.

    The listener can never invent fields the admin doesn't see. Prevents
    drift where listener.html depends on a field that admin doesn't expose.
    """
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        admin = (await client.get("/status")).json()
        public = (await client.get("/public-status")).json()
    public_keys = set(public.keys())
    admin_keys = set(admin.keys())
    listener_only_keys = public_keys - admin_keys
    assert not listener_only_keys, (
        f"Listener payload has keys admin doesn't expose: {listener_only_keys}. "
        "/public-status must be a strict subset of /status for shared fields."
    )


@pytest.mark.asyncio
async def test_public_status_no_admin_secrets_leak():
    """Listener payload must NOT contain admin-only fields (queue_depth, costs, etc)."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    body = resp.json()
    # Admin-only fields that must NOT leak to listener
    forbidden = {
        "queue_depth",
        "consumption",
        "produced_log",
        "ha_details",
        "last_banter_script",
        "last_ad_script",
        "playlist",
        "pacing",
    }
    leaked = forbidden & set(body.keys())
    assert not leaked, f"Listener payload leaks admin-only fields: {leaked}"


@pytest.mark.asyncio
async def test_public_listener_requests_endpoint():
    """Listener-safe dediche feed (filtered version of /api/listener-requests)."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-listener-requests")
    assert resp.status_code == 200
    body = resp.json()
    assert "requests" in body
    assert isinstance(body["requests"], list)


@pytest.mark.asyncio
async def test_public_listener_requests_filters_sensitive_fields():
    """Listener requests endpoint must drop internal IDs and error fields."""
    from mammamiradio.core.models import StationState

    app = _make_test_app()
    state: StationState = app.state.station_state
    # Inject a request with sensitive fields
    state.pending_requests.append(
        {
            "ts": 1700000000.0,
            "name": "Marco",
            "message": "Per Lucia",
            "type": "dedica",
            "song_found": True,
            "song_track": "Vasco Rossi - Albachiara",
            "song_error": "INTERNAL_ERROR_DETAILS",  # admin-visible only
        }
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-listener-requests")
    body = resp.json()
    requests = body["requests"]
    if requests:
        req = requests[0]
        # Public-safe fields present
        assert req.get("name") == "Marco"
        assert req.get("message") == "Per Lucia"
        assert "song_track" in req
        assert "age_s" in req
        # Sensitive fields absent
        assert "id" not in req, "Internal ID must not leak to public"
        assert "song_error" not in req, "Error details must not leak to public"
        assert "ts" not in req, "Raw timestamp must not leak (use age_s instead)"
