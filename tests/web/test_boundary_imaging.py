"""Engine Room Transitions dial: live at the next seam, persist-first when standalone."""

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
from mammamiradio.core.models import Segment, SegmentType, StationState, Track
from mammamiradio.web.streamer import LiveStreamHub, router

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


@pytest.fixture(autouse=True)
def _isolate_boundary_imaging_env():
    prev = os.environ.get("MAMMAMIRADIO_BOUNDARY_IMAGING")
    os.environ.pop("MAMMAMIRADIO_BOUNDARY_IMAGING", None)
    yield
    if prev is None:
        os.environ.pop("MAMMAMIRADIO_BOUNDARY_IMAGING", None)
    else:
        os.environ["MAMMAMIRADIO_BOUNDARY_IMAGING"] = prev


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


def _client(app: FastAPI, host: str = "127.0.0.1") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(host, 1)),
        base_url="http://testserver",
    )


@pytest.mark.asyncio
async def test_get_boundary_imaging_defaults_on():
    app = _make_test_app()
    async with _client(app) as client:
        resp = await client.get("/api/boundary-imaging")
    assert resp.status_code == 200
    assert resp.json() == {"boundary_imaging": True, "resets_on_restart": False, "carts_aired": 0}


@pytest.mark.asyncio
async def test_get_boundary_imaging_addon_resets_on_restart():
    app = _make_test_app(is_addon=True)
    async with _client(app) as client:
        body = (await client.get("/api/boundary-imaging")).json()
    assert body["resets_on_restart"] is True
    assert body["boundary_imaging"] is True


@pytest.mark.asyncio
async def test_post_persists_before_the_runtime_changes():
    app = _make_test_app(is_addon=False)
    with patch("mammamiradio.web.streamer._save_dotenv") as save_dotenv:
        async with _client(app) as client:
            resp = await client.post("/api/boundary-imaging", json={"boundary_imaging": False})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "boundary_imaging": False, "resets_on_restart": False}
    save_dotenv.assert_called_once_with({"MAMMAMIRADIO_BOUNDARY_IMAGING": "false"})
    assert os.environ["MAMMAMIRADIO_BOUNDARY_IMAGING"] == "false"
    assert app.state.config.audio.boundary_imaging is False


@pytest.mark.asyncio
async def test_persist_failure_leaves_the_runtime_unchanged():
    app = _make_test_app(is_addon=False)
    assert app.state.config.audio.boundary_imaging is True
    with patch("mammamiradio.web.streamer._save_dotenv", side_effect=OSError("disk full")):
        async with _client(app) as client:
            resp = await client.post("/api/boundary-imaging", json={"boundary_imaging": False})
    assert resp.status_code == 500
    assert resp.json()["ok"] is False
    assert app.state.config.audio.boundary_imaging is True
    assert "MAMMAMIRADIO_BOUNDARY_IMAGING" not in os.environ


@pytest.mark.asyncio
async def test_addon_updates_runtime_without_writing_dotenv():
    app = _make_test_app(is_addon=True)
    with (
        patch("mammamiradio.web.streamer._save_dotenv") as save_dotenv,
        patch("mammamiradio.web.streamer._save_addon_option") as save_addon,
    ):
        async with _client(app) as client:
            resp = await client.post("/api/boundary-imaging", json={"boundary_imaging": False})
    assert resp.status_code == 200
    assert resp.json()["resets_on_restart"] is True
    assert app.state.config.audio.boundary_imaging is False
    save_dotenv.assert_not_called()
    save_addon.assert_not_called()


@pytest.mark.asyncio
async def test_post_rejects_non_boolean_and_malformed_json():
    app = _make_test_app()
    async with _client(app) as client:
        rejected = await client.post("/api/boundary-imaging", json={"boundary_imaging": "yes"})
        malformed = await client.post(
            "/api/boundary-imaging",
            content="{bad",
            headers={"content-type": "application/json"},
        )
    assert rejected.status_code == 200
    assert rejected.json()["ok"] is False
    assert "boolean" in rejected.json()["error"]
    assert malformed.json()["ok"] is False
    assert app.state.config.audio.boundary_imaging is True


@pytest.mark.asyncio
async def test_public_ip_requires_admin():
    app = _make_test_app(admin_password="secret")
    async with _client(app, host="203.0.113.9") as client:
        resp = await client.post("/api/boundary-imaging", json={"boundary_imaging": False})
    assert resp.status_code in (401, 403)
    assert app.state.config.audio.boundary_imaging is True


@pytest.mark.asyncio
async def test_toggle_does_not_purge_the_queue():
    app = _make_test_app()
    app.state.queue.put_nowait(Segment(type=SegmentType.MUSIC, path=Path("/tmp/x.mp3"), ephemeral=False))
    with patch("mammamiradio.web.streamer._save_dotenv"):
        async with _client(app) as client:
            resp = await client.post("/api/boundary-imaging", json={"boundary_imaging": False})
    assert resp.status_code == 200
    assert app.state.queue.qsize() == 1
    assert not app.state.skip_event.is_set()


@pytest.mark.asyncio
async def test_status_reports_the_same_boundary_counters_to_admin_and_listener():
    from tests.web.test_streamer_routes import _make_test_app as _fat_app

    app = _fat_app()
    app.state.config.audio.boundary_imaging = False
    app.state.station_state.boundary_carts_aired = 4
    app.state.station_state.boundary_imaging_skips["asset_missing"] = 2
    async with _client(app) as client:
        admin = (await client.get("/status")).json()
        public = (await client.get("/public-status")).json()
    expected = {"enabled": False, "carts_aired": 4, "skips": {"asset_missing": 2}}
    assert admin["runtime_health"]["boundary_imaging"] == expected
    assert public["runtime_health"]["boundary_imaging"] == expected
