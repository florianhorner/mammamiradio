"""Tests for the live On-Air Sound dial (/api/broadcast-chain).

The dial (dis)arms the FM egress chain with NO restart and NO queue purge — the
current segment must finish airing untouched (leadership principle #1), so an
operator can A/B the FM colouring against studio-clean on the live stream. The
runtime gate is the normalizer module global, so the test asserts the real
hot-apply, not just a config field flip.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.audio import normalizer
from mammamiradio.core.config import load_config
from mammamiradio.core.models import Segment, SegmentType, StationState, Track
from mammamiradio.web.streamer import LiveStreamHub, router

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


@pytest.fixture(autouse=True)
def _isolate_broadcast_chain_env():
    """The handler writes MAMMAMIRADIO_BROADCAST_CHAIN into os.environ; restore it
    around each test so this suite can't leak the flag into other files' load_config()."""
    prev = os.environ.get("MAMMAMIRADIO_BROADCAST_CHAIN")
    os.environ.pop("MAMMAMIRADIO_BROADCAST_CHAIN", None)
    yield
    if prev is None:
        os.environ.pop("MAMMAMIRADIO_BROADCAST_CHAIN", None)
    else:
        os.environ["MAMMAMIRADIO_BROADCAST_CHAIN"] = prev


def _make_test_app(*, admin_password: str = "", is_addon: bool = False) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    config = load_config(TOML_PATH)
    config.admin_password = admin_password
    config.admin_token = ""
    config.is_addon = is_addon
    state = StationState(playlist=[Track(title="S", artist="A", duration_ms=180_000, spotify_id="t1")])
    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    hub = LiveStreamHub()
    hub.bind_state(state)
    app.state.stream_hub = hub
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://testserver"
    )


# ── GET ───────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_get_broadcast_chain_returns_flag():
    app = _make_test_app()  # radio.toml ships broadcast_chain = false (studio-clean default)
    async with _client(app) as client:
        resp = await client.get("/api/broadcast-chain")
    assert resp.status_code == 200
    assert resp.json() == {"broadcast_chain": False}


# ── POST hot-apply: the dial actually (dis)arms the egress chain ────────────
@pytest.mark.asyncio
async def test_post_broadcast_chain_true_arms_the_egress_chain():
    """Flipping ON must arm the normalizer global so the NEXT segment is coloured —
    proving the toggle reaches the real runtime gate, not just a config field."""
    normalizer.configure_broadcast_chain(False)  # start disarmed
    app = _make_test_app()
    with patch("mammamiradio.web.streamer._save_dotenv"):
        async with _client(app) as client:
            resp = await client.post("/api/broadcast-chain", json={"broadcast_chain": True})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "broadcast_chain": True}
    assert app.state.config.audio.broadcast_chain is True
    assert normalizer._broadcast_output_args is not None  # egress chain is armed


@pytest.mark.asyncio
async def test_post_broadcast_chain_false_disarms_the_egress_chain():
    normalizer.configure_broadcast_chain(True)  # start armed
    app = _make_test_app()
    with patch("mammamiradio.web.streamer._save_dotenv"):
        async with _client(app) as client:
            resp = await client.post("/api/broadcast-chain", json={"broadcast_chain": False})
    assert resp.status_code == 200
    assert app.state.config.audio.broadcast_chain is False
    assert normalizer._broadcast_output_args is None  # egress chain is disarmed (studio-clean)


# ── Persistence ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_post_broadcast_chain_persists_standalone():
    app = _make_test_app(is_addon=False)
    with patch("mammamiradio.web.streamer._save_dotenv") as save_dotenv:
        async with _client(app) as client:
            resp = await client.post("/api/broadcast-chain", json={"broadcast_chain": False})
    assert resp.status_code == 200
    save_dotenv.assert_called_once_with({"MAMMAMIRADIO_BROADCAST_CHAIN": "false"})
    assert os.environ.get("MAMMAMIRADIO_BROADCAST_CHAIN") == "false"


@pytest.mark.asyncio
async def test_post_broadcast_chain_addon_writes_options_json(tmp_path):
    app = _make_test_app(is_addon=True)
    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps({"existing": "value"}))
    with patch("mammamiradio.web.persistence.Path", return_value=options_file):
        async with _client(app) as client:
            resp = await client.post("/api/broadcast-chain", json={"broadcast_chain": False})
    assert resp.status_code == 200
    options = json.loads(options_file.read_text())
    assert options["broadcast_chain"] is False  # same key run.sh reads back
    assert options["existing"] == "value"  # single-key patch, didn't clobber


@pytest.mark.asyncio
async def test_post_broadcast_chain_persist_failure_does_not_change_live():
    """A failed write leaves the live runtime untouched so the flag never drifts from
    what survives a restart."""
    normalizer.configure_broadcast_chain(True)  # armed
    app = _make_test_app(is_addon=False)
    app.state.config.audio.broadcast_chain = True  # operator had opted in (default is off)
    assert app.state.config.audio.broadcast_chain is True
    with patch("mammamiradio.web.streamer._save_dotenv", side_effect=OSError("disk full")):
        async with _client(app) as client:
            resp = await client.post("/api/broadcast-chain", json={"broadcast_chain": False})
    assert resp.status_code == 500
    assert resp.json()["ok"] is False
    assert app.state.config.audio.broadcast_chain is True  # unchanged
    assert normalizer._broadcast_output_args is not None  # still armed
    assert os.environ.get("MAMMAMIRADIO_BROADCAST_CHAIN") is None


# ── Principle #1: A/B must not break the current track ──────────────────────
@pytest.mark.asyncio
async def test_post_broadcast_chain_does_not_purge_queue():
    app = _make_test_app()
    app.state.queue.put_nowait(Segment(type=SegmentType.MUSIC, path=Path("/tmp/x.mp3"), ephemeral=False))
    with patch("mammamiradio.web.streamer._save_dotenv"):
        async with _client(app) as client:
            resp = await client.post("/api/broadcast-chain", json={"broadcast_chain": False})
    assert resp.status_code == 200
    assert app.state.queue.qsize() == 1  # queue untouched
    assert not app.state.skip_event.is_set()  # current segment not skipped


# ── Validation + auth ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_post_broadcast_chain_rejects_non_bool():
    app = _make_test_app()
    with patch("mammamiradio.web.streamer._save_dotenv"):
        async with _client(app) as client:
            resp = await client.post("/api/broadcast-chain", json={"broadcast_chain": "yes"})
    body = resp.json()
    assert body["ok"] is False
    assert "boolean" in body["error"]


@pytest.mark.asyncio
async def test_post_broadcast_chain_rejects_malformed_json():
    app = _make_test_app()
    async with _client(app) as client:
        resp = await client.post("/api/broadcast-chain", content="{bad", headers={"content-type": "application/json"})
    assert resp.json()["ok"] is False  # graceful, not an unhandled 500


@pytest.mark.asyncio
async def test_broadcast_chain_requires_admin_for_public_ip():
    app = _make_test_app(admin_password="secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.9", 9)), base_url="http://testserver"
    ) as client:
        resp = await client.post("/api/broadcast-chain", json={"broadcast_chain": False})
    assert resp.status_code in (401, 403)  # admin-gated from a non-local address
