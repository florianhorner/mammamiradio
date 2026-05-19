"""Tests for POST /api/interrupt — auth, cooldown, queue drain, skip_event."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mammamiradio.core.config import load_config
from mammamiradio.core.models import ChaosSubtype, InterruptSpec, Segment, SegmentType, StationState, Track
from mammamiradio.web.listener_requests import router as listener_requests_router
from mammamiradio.web.streamer import LiveStreamHub, router

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def _make_test_app(*, admin_token: str = "test-token") -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.include_router(listener_requests_router)

    config = load_config(TOML_PATH)
    config.admin_token = admin_token
    config.admin_password = ""

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
# Auth
# ---------------------------------------------------------------------------


def test_interrupt_requires_auth():
    app = _make_test_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post("/api/interrupt", json={"directive": "test"})
    assert resp.status_code == 401


def test_interrupt_accepts_valid_token():
    app = _make_test_app()
    with (
        TestClient(app, raise_server_exceptions=True) as client,
        patch("mammamiradio.scheduling.producer._fire_interrupt", new_callable=AsyncMock) as mock_fire,
    ):
        resp = client.post(
            "/api/interrupt",
            json={"directive": "La pasta scotta!"},
            headers={"X-Radio-Admin-Token": "test-token"},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    mock_fire.assert_awaited_once()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_interrupt_missing_directive_returns_422():
    app = _make_test_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post(
            "/api/interrupt",
            json={"urgency": "pissed"},
            headers={"X-Radio-Admin-Token": "test-token"},
        )
    assert resp.status_code == 422


def test_interrupt_empty_directive_returns_422():
    app = _make_test_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post(
            "/api/interrupt",
            json={"directive": "   "},
            headers={"X-Radio-Admin-Token": "test-token"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Cooldown (429)
# ---------------------------------------------------------------------------


def test_interrupt_cooldown_returns_429():
    app = _make_test_app()
    state: StationState = app.state.station_state
    # Simulate interrupt fired 5 seconds ago
    state.last_interrupt_ts = time.time() - 5

    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post(
            "/api/interrupt",
            json={"directive": "Again!"},
            headers={"X-Radio-Admin-Token": "test-token"},
        )
    assert resp.status_code == 429
    body = resp.json()
    assert body["ok"] is False
    assert "retry_after" in body
    assert body["retry_after"] > 0


def test_interrupt_fires_after_cooldown_expires():
    app = _make_test_app()
    state: StationState = app.state.station_state
    # Simulate interrupt fired 120 seconds ago (well past 60s cooldown)
    state.last_interrupt_ts = time.time() - 120

    with (
        TestClient(app, raise_server_exceptions=True) as client,
        patch("mammamiradio.scheduling.producer._fire_interrupt", new_callable=AsyncMock) as mock_fire,
    ):
        resp = client.post(
            "/api/interrupt",
            json={"directive": "La pasta scotta!"},
            headers={"X-Radio-Admin-Token": "test-token"},
        )
    assert resp.status_code == 200
    mock_fire.assert_awaited_once()


# ---------------------------------------------------------------------------
# Scenario 1 (normal): queue drain + skip_event + directive injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_interrupt_drains_queue_and_fires_skip():
    """Scenario 1: interrupt fires → queue drained, skip_event set, directive injected."""
    from mammamiradio.scheduling.producer import _fire_interrupt

    state = StationState(
        playlist=[Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="t1")],
    )
    queue: asyncio.Queue[Segment] = asyncio.Queue()
    skip_event = asyncio.Event()

    # Pre-fill queue with buffered segments
    dummy_path = Path("/tmp/dummy_test_segment.mp3")
    dummy_path.touch()
    for _ in range(3):
        await queue.put(Segment(type=SegmentType.MUSIC, path=dummy_path, metadata={"type": "music"}, ephemeral=False))

    spec = InterruptSpec(directive="La pasta sta bruciando!", urgency="pissed", cooldown=60)
    await _fire_interrupt(state, spec, queue, skip_event)

    assert queue.empty(), "queue must be drained after interrupt"
    assert skip_event.is_set(), "skip_event must be set"
    assert state.ha_pending_directive == "La pasta sta bruciando!"
    assert state.chaos_pending == ChaosSubtype.URGENT_INTERRUPT
    assert state.last_interrupt_ts > 0


# ---------------------------------------------------------------------------
# Scenario 2 (empty fallback): alert.mp3 absent → interrupt_slot is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_interrupt_no_bridge_when_alert_missing():
    """Scenario 2: alert.mp3 absent → interrupt_slot=None, but interrupt still fires."""
    from mammamiradio.scheduling.producer import _fire_interrupt

    state = StationState(
        playlist=[Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="t1")],
    )
    queue: asyncio.Queue[Segment] = asyncio.Queue()
    skip_event = asyncio.Event()
    spec = InterruptSpec(directive="Svegliati!", urgency="urgent", cooldown=60)

    with patch("mammamiradio.scheduling.producer._SFX_DIR", Path("/nonexistent")):
        await _fire_interrupt(state, spec, queue, skip_event)

    assert state.interrupt_slot is None
    assert skip_event.is_set()
    assert state.ha_pending_directive == "Svegliati!"


# ---------------------------------------------------------------------------
# Scenario 3 (post-restart): session_stopped state cleared on interrupt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_interrupt_works_after_session_stopped():
    """Scenario 3: interrupt fires even when session was previously stopped."""
    from mammamiradio.scheduling.producer import _fire_interrupt

    state = StationState(
        playlist=[Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="t1")],
    )
    state.session_stopped = True  # simulate post-restart state
    queue: asyncio.Queue[Segment] = asyncio.Queue()
    skip_event = asyncio.Event()
    spec = InterruptSpec(directive="Alzati!", urgency="pissed", cooldown=60)

    await _fire_interrupt(state, spec, queue, skip_event)

    # Interrupt still fires — producer will resume from stopped state after skip
    assert state.ha_pending_directive == "Alzati!"
    assert skip_event.is_set()


# ---------------------------------------------------------------------------
# Scenario: cooldown enforced in _fire_interrupt itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_interrupt_respects_cooldown():
    from mammamiradio.scheduling.producer import _fire_interrupt

    state = StationState(
        playlist=[Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="t1")],
    )
    state.last_interrupt_ts = time.time() - 5  # 5s ago, 60s cooldown not expired
    queue: asyncio.Queue[Segment] = asyncio.Queue()
    skip_event = asyncio.Event()
    spec = InterruptSpec(directive="Di nuovo!", urgency="pissed", cooldown=60)

    await _fire_interrupt(state, spec, queue, skip_event)

    assert not skip_event.is_set(), "skip_event must NOT be set when cooldown is active"
    assert state.ha_pending_directive == "", "directive must NOT be injected during cooldown"
