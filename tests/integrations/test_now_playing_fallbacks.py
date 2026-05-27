"""Fallback-state tests for /api/integrations/v1/now-playing.

Covers the three scenarios from the CLAUDE.md audio-delivery rule applied
to the integration contract:
1. Normal — handled in test_now_playing_contract.py
2. Empty fallback — no now_streaming, no queue, no predictions usable
3. Post-restart — session_stopped persisted across an app boot

Plus the two transient sentinel ``now_streaming`` shapes
(``{"type": "stopped"}`` written by /api/stop and ``{"type": "skipping"}``
written by /api/skip) that must NOT render as music.
"""

from __future__ import annotations

import httpx
import pytest

from tests.integrations.conftest import make_integrations_app


@pytest.mark.asyncio
async def test_empty_queue_no_now_streaming_returns_empty_queue_state():
    """No now_streaming + no queued segments → session_state ``empty_queue``.

    Predicted up_next items may still appear (the scheduler always runs);
    what the consumer needs is the explicit ``empty_queue`` signal so it
    can show a "loading" / "queuing up" UI instead of a stale track.
    """
    app = make_integrations_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    assert body["session_state"] == "empty_queue"
    assert body["now_playing"] is None
    # Stream URL must remain present so MA still knows where to point.
    assert body["stream"]["relative_url"] == "/stream"
    # Predictions are speculation — they may exist, but every item that does
    # appear must still have the stable up_next shape.
    for item in body["up_next"]:
        assert "predicted" in item
        assert "segment_class" in item


@pytest.mark.asyncio
async def test_session_stopped_via_admin_returns_stopped_state():
    app = make_integrations_app()
    state = app.state.station_state
    state.session_stopped = True
    state.now_streaming = {
        "type": "stopped",
        "label": "Session stopped",
        "started": 0.0,
        "metadata": {},
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    assert body["session_state"] == "stopped"
    assert body["now_playing"] is None
    # stream.relative_url still present — operator honesty
    assert body["stream"]["relative_url"] == "/stream"


@pytest.mark.asyncio
async def test_post_restart_session_stopped_persisted_returns_stopped_state():
    """Simulate the post-restart scenario: session_stopped restored on boot."""
    app = make_integrations_app()
    # This mirrors what main.startup does when /data/session_stopped.flag exists
    app.state.station_state.session_stopped = True
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    assert body["session_state"] == "stopped"
    assert body["now_playing"] is None


@pytest.mark.asyncio
async def test_admin_stop_transient_now_streaming_is_unavailable():
    """A live ``{type: stopped}`` sentinel must NOT render as music."""
    app = make_integrations_app()
    state = app.state.station_state
    state.session_stopped = False  # Not yet fully stopped — transient
    state.now_streaming = {
        "type": "stopped",
        "label": "Session stopped",
        "started": 1.0,
        "metadata": {},
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    # Without session_stopped flag this still maps to "stopped" session state
    # because the now_streaming sentinel is type=stopped.
    assert body["session_state"] == "stopped"
    assert body["now_playing"] is None


@pytest.mark.asyncio
async def test_skipping_transient_now_streaming_is_unavailable():
    app = make_integrations_app()
    state = app.state.station_state
    state.session_stopped = False
    state.now_streaming = {
        "type": "skipping",
        "label": "Skipping...",
        "started": 1.0,
        "metadata": {},
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    now = body["now_playing"]
    assert now is not None
    assert now["segment_class"] == "unavailable"
    assert now["artist"] is None
    assert now["artwork"] is None
    # Session is still "live" — skipping is mid-transition
    assert body["session_state"] == "live"
