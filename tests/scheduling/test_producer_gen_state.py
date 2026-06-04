"""Live production tracking ("In produzione") — StationState helpers + status API.

Covers the set_gen/end_gen lifecycle that drives the admin "In produzione" feed,
and the production payload exposed by the admin /status endpoint. The helpers are
best-effort display state and must never wedge the producer (a crash that skips
end_gen is overwritten by the next set_gen).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.core.config import load_config
from mammamiradio.core.models import StationState, Track
from mammamiradio.web.listener_requests import router as listener_requests_router
from mammamiradio.web.streamer import LiveStreamHub, router

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


# ---------------------------------------------------------------------------
# StationState.set_gen / end_gen
# ---------------------------------------------------------------------------


def test_set_gen_populates_current_phase():
    state = StationState()
    state.set_gen("writing", "ad", "Writing the Velocino spot")
    assert state.gen_phase == "writing"
    assert state.gen_kind == "ad"
    assert state.gen_label == "Writing the Velocino spot"
    assert state.gen_started > 0.0


def test_end_gen_clears_and_records_success():
    state = StationState()
    state.set_gen("writing", "banter", "Writing banter")
    state.end_gen(ok=True)
    assert state.gen_phase == ""
    assert state.gen_kind == ""
    assert state.gen_label == ""
    assert state.gen_started == 0.0
    assert len(state.gen_recent) == 1
    assert state.gen_recent[0] == {
        "phase": "writing",
        "kind": "banter",
        "label": "Writing banter",
        "ok": True,
    }


def test_end_gen_records_blocked_on_failure():
    state = StationState()
    state.set_gen("finding", "music", "Finding Test Song")
    state.end_gen(ok=False)
    assert state.gen_phase == ""
    assert state.gen_recent[0]["ok"] is False


def test_end_gen_is_noop_when_idle():
    state = StationState()
    state.end_gen(ok=True)
    assert len(state.gen_recent) == 0
    assert state.gen_phase == ""


def test_recent_trail_is_bounded_and_ordered():
    state = StationState()
    for i in range(5):
        state.set_gen("writing", "ad", f"spot {i}")
        state.end_gen(ok=True)
    # maxlen=3, most-recent first
    assert len(state.gen_recent) == 3
    assert [r["label"] for r in state.gen_recent] == ["spot 4", "spot 3", "spot 2"]


def test_crash_without_end_gen_does_not_wedge():
    """A producer crash that skips end_gen must not block the next segment:
    the next set_gen overwrites the stale phase, and no phantom recent entry leaks."""
    state = StationState()
    state.set_gen("writing", "banter", "Writing banter")  # simulate crash before end_gen
    state.set_gen("finding", "music", "Finding Next Song")  # next loop iteration
    assert state.gen_phase == "finding"
    assert state.gen_label == "Finding Next Song"
    assert len(state.gen_recent) == 0  # the crashed phase did not get falsely recorded


# ---------------------------------------------------------------------------
# Admin /status — production payload
# ---------------------------------------------------------------------------


def _make_app(state: StationState) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.include_router(listener_requests_router)
    config = load_config(TOML_PATH)
    config.admin_password = ""
    config.admin_token = ""
    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    hub = LiveStreamHub()
    hub.bind_state(state)
    app.state.stream_hub = hub
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    return app


@pytest.mark.asyncio
async def test_status_exposes_production_current():
    state = StationState(
        playlist=[Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="t1")],
    )
    state.set_gen("writing", "ad", "Writing the Velocino spot")
    app = _make_app(state)
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    production = resp.json()["production"]
    assert production["current"]["kind"] == "ad"
    assert production["current"]["phase"] == "writing"
    assert production["current"]["label"] == "Writing the Velocino spot"
    assert isinstance(production["current"]["elapsed_sec"], int)


@pytest.mark.asyncio
async def test_status_production_null_when_idle():
    state = StationState(
        playlist=[Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="t1")],
    )
    app = _make_app(state)
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    production = resp.json()["production"]
    assert production["current"] is None
    assert production["recent"] == []


@pytest.mark.asyncio
async def test_production_recent_trail_in_status():
    state = StationState(
        playlist=[Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="t1")],
    )
    state.set_gen("finding", "music", "Finding Song")
    state.end_gen(ok=True)
    app = _make_app(state)
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    production = resp.json()["production"]
    assert production["current"] is None
    assert production["recent"] == [{"kind": "music", "label": "Finding Song", "ok": True}]


@pytest.mark.asyncio
async def test_production_not_in_public_listener_status():
    """The production feed is admin-only; the listener-facing /public-status must not leak it."""
    state = StationState(
        playlist=[Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="t1")],
    )
    state.set_gen("writing", "ad", "Writing the Velocino spot")
    app = _make_app(state)
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    assert "production" not in resp.json()
