"""operator_action provenance rows for station-wide operator toggles (observability).

A station-wide character flip (Super Italian, Chaos, Festival, AI Quality, On-Air
Sound) used to leave no honest trace: the addon runs FastAPI with ``--no-access-log``,
so the POST never reached the logs and the only feedback was a small toast — which is
why the 2026-06-19 "who switched the hosts to English?" session could not see what the
operator had done. These tests lock in that every station-wide toggle now records an
``operator_action`` ledger row (old -> new), and that the recording is strictly
best-effort: a disabled or failing ledger never affects whether the toggle applied.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.core.config import load_config
from mammamiradio.core.models import StationState, Track
from mammamiradio.web.streamer import LiveStreamHub, router

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")

# The toggle handlers write their MAMMAMIRADIO_* env var into os.environ. Because
# this module drives all five toggles, restore every one after each test so the
# leak can't reach an env-sensitive test under pytest-randomly's shuffled order.
_TOGGLE_ENV_KEYS = (
    "MAMMAMIRADIO_SUPER_ITALIAN",
    "MAMMAMIRADIO_CHAOS_MODE",
    "MAMMAMIRADIO_FESTIVAL_MODE",
    "MAMMAMIRADIO_QUALITY",
    "MAMMAMIRADIO_BROADCAST_CHAIN",
)


@pytest.fixture(autouse=True)
def _restore_toggle_env():
    saved = {k: os.environ.get(k) for k in _TOGGLE_ENV_KEYS}
    try:
        yield
    finally:
        for key, prev in saved.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev


class _FakeLedger:
    """Captures recorded rows in memory; mirrors ProvenanceLedger.record()'s surface."""

    def __init__(self, *, enabled: bool = True, raises: bool = False) -> None:
        self.enabled = enabled
        self.rows: list[dict] = []
        self._raises = raises

    def record(self, row: dict) -> None:
        if self._raises:
            raise RuntimeError("ledger boom")
        self.rows.append(row)


def _make_app(ledger: _FakeLedger | None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    config = load_config(TOML_PATH)
    config.admin_password = ""  # local/dev: no auth required
    config.admin_token = ""
    config.is_addon = False
    config.cache_dir = Path("/tmp/mammamiradio-test-cache")
    config.cache_dir.mkdir(parents=True, exist_ok=True)
    # Normalize the toggle baseline so a test is independent of ambient MAMMAMIRADIO_*
    # env leaked by another module (e.g. a stale FESTIVAL_MODE would make festival
    # "enable" a no-op early-return and record no row). Tests that need a non-default
    # start (the old/new cases) set their field explicitly after _make_app.
    config.super_italian_mode = False
    config.party_mode = None
    config.audio.broadcast_chain = False
    config.models.active_profile = "balanced"

    state = StationState(
        playlist=[Track(title="Song A", artist="Artist A", duration_ms=180_000, spotify_id="t1")],
    )
    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    app.state.source_switch_lock = asyncio.Lock()
    app.state.stream_hub = LiveStreamHub()
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    if ledger is not None:
        # _record_operator_action reads request.app.state.ledger (not station_state).
        app.state.ledger = ledger
    return app


def _operator_rows(ledger: _FakeLedger) -> list[dict]:
    return [r for r in ledger.rows if r.get("record") == "operator_action"]


# (action, path, payload) for each station-wide toggle.
TOGGLES = [
    ("super_italian_mode", "/api/super-italian", {"super_italian_mode": True}),
    ("chaos_mode", "/api/chaos", {"enabled": True}),
    ("festival_mode", "/api/party", {"action": "enable", "mode": "festival"}),
    ("quality_profile", "/api/quality", {"quality_profile": "premium"}),
    ("broadcast_chain", "/api/broadcast-chain", {"broadcast_chain": True}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("action,path,payload", TOGGLES)
async def test_toggle_records_operator_action(action, path, payload):
    """Every station-wide toggle records exactly one operator_action row."""
    ledger = _FakeLedger(enabled=True)
    app = _make_app(ledger)
    transport = httpx.ASGITransport(app=app)
    with (
        patch("mammamiradio.web.streamer._save_dotenv"),
        patch("mammamiradio.web.streamer._save_addon_option"),
        patch("mammamiradio.web.streamer.configure_broadcast_chain"),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(path, json=payload)
    assert resp.status_code == 200
    rows = _operator_rows(ledger)
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == action
    assert row["source"] == "admin"
    assert "old_value" in row and "new_value" in row
    assert "ts" in row and "schema_version" in row


@pytest.mark.asyncio
async def test_operator_action_records_old_and_new_values():
    """The row carries the true before/after, so a debrief can see what changed."""
    ledger = _FakeLedger(enabled=True)
    app = _make_app(ledger)
    app.state.config.super_italian_mode = True  # known starting state
    transport = httpx.ASGITransport(app=app)
    with patch("mammamiradio.web.streamer._save_dotenv"):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/super-italian", json={"super_italian_mode": False})
    assert resp.status_code == 200
    row = _operator_rows(ledger)[0]
    assert row["old_value"] is True
    assert row["new_value"] is False


@pytest.mark.asyncio
async def test_disabled_ledger_records_nothing_but_toggle_succeeds():
    """When the ledger is off (the standalone default), the toggle still works and
    no row is written — never a crash, never a silent partial state."""
    ledger = _FakeLedger(enabled=False)
    app = _make_app(ledger)
    transport = httpx.ASGITransport(app=app)
    with patch("mammamiradio.web.streamer._save_dotenv"):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/super-italian", json={"super_italian_mode": True})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert _operator_rows(ledger) == []


@pytest.mark.asyncio
async def test_failing_ledger_never_breaks_toggle():
    """A ledger that raises inside record() must never bubble into the toggle."""
    ledger = _FakeLedger(enabled=True, raises=True)
    app = _make_app(ledger)
    transport = httpx.ASGITransport(app=app)
    with patch("mammamiradio.web.streamer._save_dotenv"):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/super-italian", json={"super_italian_mode": True})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert _operator_rows(ledger) == []


@pytest.mark.asyncio
async def test_festival_noop_records_no_row():
    """Festival has an idempotent early-return BEFORE _record_operator_action, so
    pressing 'Festival on' while already on must record nothing — a debrief should
    never see a phantom 'festival on -> on' row."""
    ledger = _FakeLedger(enabled=True)
    app = _make_app(ledger)
    app.state.config.party_mode = "festival"  # already on
    transport = httpx.ASGITransport(app=app)
    with patch("mammamiradio.web.streamer._save_dotenv"):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/party", json={"action": "enable", "mode": "festival"})
    assert resp.status_code == 200
    assert _operator_rows(ledger) == []


# (path, payload, mutate-state, expected old_value, expected new_value) — each toggle
# captures old_value from a DIFFERENT state field, so a wrong-field copy/paste would
# record a misleading value. Start each from a non-default state and assert both ends.
_OLDNEW = [
    ("/api/chaos", {"enabled": False}, lambda s, c: setattr(s, "chaos_mode_active", True), True, False),
    ("/api/party", {"action": "disable"}, lambda s, c: setattr(c, "party_mode", "festival"), True, False),
    (
        "/api/quality",
        {"quality_profile": "premium"},
        lambda s, c: setattr(c.models, "active_profile", "economy"),
        "economy",
        "premium",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path,payload,mutate,old,new", _OLDNEW)
async def test_operator_action_old_new_per_field(path, payload, mutate, old, new):
    ledger = _FakeLedger(enabled=True)
    app = _make_app(ledger)
    mutate(app.state.station_state, app.state.config)
    transport = httpx.ASGITransport(app=app)
    with (
        patch("mammamiradio.web.streamer._save_dotenv"),
        patch("mammamiradio.web.streamer._save_addon_option"),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(path, json=payload)
    assert resp.status_code == 200
    row = _operator_rows(ledger)[0]
    assert row["old_value"] == old
    assert row["new_value"] == new


# (path, payload, drift-probe) — every toggle persists BEFORE mutating, so a failed
# persist must return 500, leave runtime state untouched (no drift from what survives a
# restart), and record NO operator_action row (don't log a change that didn't happen).
_PERSIST_FAIL = [
    ("/api/chaos", {"enabled": True}, lambda app: app.state.station_state.chaos_mode_active is False),
    ("/api/super-italian", {"super_italian_mode": True}, lambda app: app.state.config.super_italian_mode is False),
    ("/api/party", {"action": "enable", "mode": "festival"}, lambda app: app.state.config.party_mode is None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path,payload,unchanged", _PERSIST_FAIL)
async def test_persist_failure_records_no_row_and_no_drift(path, payload, unchanged):
    ledger = _FakeLedger(enabled=True)
    app = _make_app(ledger)
    transport = httpx.ASGITransport(app=app)
    with patch("mammamiradio.web.streamer._save_dotenv", side_effect=OSError("disk full")):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(path, json=payload)
    assert resp.status_code == 500
    assert _operator_rows(ledger) == []
    assert unchanged(app)  # runtime did not drift from the failed persist


# Pacing is a PATCH endpoint (not a POST toggle), so it can't ride the POST-based
# lists above — but it follows the same persist-first + operator_action contract.


@pytest.mark.asyncio
async def test_pacing_records_operator_action_old_and_new():
    """A pacing slider save records one operator_action row per changed field."""
    ledger = _FakeLedger(enabled=True)
    app = _make_app(ledger)
    app.state.config.pacing.songs_between_banter = 4
    transport = httpx.ASGITransport(app=app)
    with patch("mammamiradio.web.streamer._save_dotenv"):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.patch("/api/pacing", json={"songs_between_banter": 6})
    assert resp.status_code == 200
    rows = _operator_rows(ledger)
    assert len(rows) == 1
    assert rows[0]["action"] == "pacing_songs_between_banter"
    assert rows[0]["old_value"] == 4
    assert rows[0]["new_value"] == 6
    assert rows[0]["source"] == "admin"


@pytest.mark.asyncio
async def test_pacing_noop_records_no_row():
    """Re-saving the same pacing value records nothing (no phantom old==new row)."""
    ledger = _FakeLedger(enabled=True)
    app = _make_app(ledger)
    app.state.config.pacing.songs_between_ads = 5
    transport = httpx.ASGITransport(app=app)
    with patch("mammamiradio.web.streamer._save_dotenv"):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.patch("/api/pacing", json={"songs_between_ads": 5})
    assert resp.status_code == 200
    assert _operator_rows(ledger) == []


@pytest.mark.asyncio
async def test_pacing_persist_failure_records_no_row_and_no_drift():
    """A failed pacing persist returns 500, records no row, and leaves live config
    untouched — the same contract the station-wide toggles hold."""
    ledger = _FakeLedger(enabled=True)
    app = _make_app(ledger)
    app.state.config.pacing.songs_between_banter = 4
    transport = httpx.ASGITransport(app=app)
    with patch("mammamiradio.web.streamer._save_dotenv", side_effect=OSError("disk full")):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.patch("/api/pacing", json={"songs_between_banter": 6})
    assert resp.status_code == 500
    assert _operator_rows(ledger) == []
    assert app.state.config.pacing.songs_between_banter == 4
