"""Tests for the Chaos Mode admin API."""

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

from mammamiradio.core.config import load_config
from mammamiradio.core.models import ChaosSubtype, Segment, SegmentType, StationState, Track
from mammamiradio.web.persistence import _save_addon_option
from mammamiradio.web.streamer import LiveStreamHub, _provider_health_snapshot, router

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def _make_test_app(*, admin_password: str = "", is_addon: bool = False) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    config = load_config(TOML_PATH)
    config.admin_password = admin_password
    config.admin_token = ""
    config.is_addon = is_addon
    state = StationState(
        playlist=[Track(title="Test Song", artist="Test Artist", duration_ms=180_000, spotify_id="t1")],
    )
    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    hub = LiveStreamHub()
    hub.bind_state(state)
    app.state.stream_hub = hub
    return app


@pytest.mark.asyncio
async def test_get_chaos_returns_current_flag():
    app = _make_test_app()
    app.state.station_state.chaos_mode_active = True

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url="http://testserver",
    ) as client:
        resp = await client.get("/api/chaos")

    assert resp.status_code == 200
    assert resp.json() == {"enabled": True}


@pytest.mark.asyncio
async def test_post_chaos_enable_sets_pending_bumps_epoch_and_purges_queue(tmp_path, monkeypatch):
    app = _make_test_app()
    monkeypatch.delenv("MAMMAMIRADIO_CHAOS_MODE", raising=False)
    old_files = []
    for idx in range(3):
        old_file = tmp_path / f"old-{idx}.mp3"
        old_file.write_bytes(b"old")
        old_files.append(old_file)
        app.state.queue.put_nowait(Segment(type=SegmentType.MUSIC, path=old_file, ephemeral=True))
    app.state.station_state.queued_segments = [{"type": "music", "label": f"Old {idx}"} for idx in range(3)]

    with patch("mammamiradio.web.streamer._save_dotenv") as save_dotenv:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
            base_url="http://testserver",
        ) as client:
            resp = await client.post("/api/chaos", json={"enabled": True})

    body = resp.json()
    state = app.state.station_state
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["enabled"] is True
    assert body["purged"] == 3
    assert state.chaos_mode_active is True
    assert state.chaos_pending in {ChaosSubtype.FOURTH_WALL, ChaosSubtype.ABANDONED_STORM}
    assert state.chaos_cutover_epoch == 1
    assert app.state.queue.qsize() == len(state.queued_segments) == 1
    assert app.state.queue._queue[0].metadata["continuity_reservation"] is True
    assert all(not old_file.exists() for old_file in old_files)
    assert os.environ["MAMMAMIRADIO_CHAOS_MODE"] == "true"
    save_dotenv.assert_called_once_with({"MAMMAMIRADIO_CHAOS_MODE": "true"})


@pytest.mark.asyncio
async def test_post_chaos_disable_clears_pending_bumps_epoch_without_purging_queue(tmp_path, monkeypatch):
    app = _make_test_app()
    monkeypatch.setenv("MAMMAMIRADIO_CHAOS_MODE", "true")
    keep_file = tmp_path / "keep.mp3"
    keep_file.write_bytes(b"keep")
    app.state.queue.put_nowait(Segment(type=SegmentType.BANTER, path=keep_file, ephemeral=True))
    state = app.state.station_state
    state.chaos_mode_active = True
    state.chaos_pending = ChaosSubtype.FOURTH_WALL
    state.chaos_cutover_epoch = 4

    with patch("mammamiradio.web.streamer._save_dotenv"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
            base_url="http://testserver",
        ) as client:
            resp = await client.post("/api/chaos", json={"enabled": False})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert state.chaos_mode_active is False
    assert state.chaos_pending is None
    assert state.chaos_cutover_epoch == 5
    assert app.state.queue.qsize() == 1
    assert keep_file.exists()
    assert os.environ["MAMMAMIRADIO_CHAOS_MODE"] == "false"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [["not", "dict"], {"other": True}, {"enabled": "true"}, {"enabled": 1}])
async def test_post_chaos_validation_rejects_bad_payloads(payload):
    app = _make_test_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url="http://testserver",
    ) as client:
        resp = await client.post("/api/chaos", json=payload)

    assert resp.status_code == (422 if not isinstance(payload, dict) else 200)
    assert resp.json()["ok"] is False
    assert app.state.station_state.chaos_mode_active is False


@pytest.mark.asyncio
async def test_post_chaos_validation_rejects_malformed_json():
    app = _make_test_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url="http://testserver",
    ) as client:
        resp = await client.post("/api/chaos", content="{", headers={"content-type": "application/json"})

    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    assert isinstance(body["error"], str)
    assert body["error"]
    assert app.state.station_state.chaos_mode_active is False


@pytest.mark.asyncio
async def test_chaos_endpoints_require_admin_for_public_ip():
    app = _make_test_app(admin_password="secret")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.50", 9999)),
        base_url="http://testserver",
    ) as client:
        get_resp = await client.get("/api/chaos")
        post_resp = await client.post("/api/chaos", json={"enabled": True})

    assert get_resp.status_code == 401
    assert post_resp.status_code == 401


@pytest.mark.asyncio
async def test_chaos_addon_mode_writes_options_json(tmp_path, monkeypatch):
    app = _make_test_app(is_addon=True)
    monkeypatch.delenv("MAMMAMIRADIO_CHAOS_MODE", raising=False)
    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps({"existing": "value"}))

    with patch("mammamiradio.web.persistence.Path", return_value=options_file):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
            base_url="http://testserver",
        ) as client:
            resp = await client.post("/api/chaos", json={"enabled": True})

    assert resp.status_code == 200
    options = json.loads(options_file.read_text())
    assert options["chaos_mode_active"] is True
    assert options["existing"] == "value"


@pytest.mark.asyncio
async def test_chaos_persistence_failure_rolls_back_live_state(tmp_path, monkeypatch):
    app = _make_test_app()
    monkeypatch.delenv("MAMMAMIRADIO_CHAOS_MODE", raising=False)
    keep_file = tmp_path / "mammamiradio-chaos-persist-failure.mp3"
    keep_file.write_bytes(b"keep")
    app.state.queue.put_nowait(Segment(type=SegmentType.MUSIC, path=keep_file, ephemeral=True))
    app.state.station_state.queued_segments = [{"type": "music", "label": "Keep"}]
    state = app.state.station_state
    observed_during_persist = {}

    def _fail_persist(_updates):
        observed_during_persist.update(
            {
                "chaos_mode_active": state.chaos_mode_active,
                "chaos_pending": state.chaos_pending,
                "chaos_cutover_epoch": state.chaos_cutover_epoch,
                "queue_size": app.state.queue.qsize(),
                "queued_segments": list(state.queued_segments),
                "file_exists": keep_file.exists(),
            }
        )
        raise OSError("disk full")

    try:
        with patch("mammamiradio.web.streamer._save_dotenv", side_effect=_fail_persist):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
                base_url="http://testserver",
            ) as client:
                resp = await client.post("/api/chaos", json={"enabled": True})

        assert resp.status_code == 500
        body = resp.json()
        assert body == {"ok": False, "error": "failed to persist chaos mode"}
        assert observed_during_persist == {
            "chaos_mode_active": False,
            "chaos_pending": None,
            "chaos_cutover_epoch": 0,
            "queue_size": 1,
            "queued_segments": [{"type": "music", "label": "Keep"}],
            "file_exists": True,
        }
        assert state.chaos_mode_active is False
        assert state.chaos_pending is None
        assert state.chaos_cutover_epoch == 0
        assert app.state.queue.qsize() == 1
        assert state.queued_segments == [{"type": "music", "label": "Keep"}]
        assert keep_file.exists()
    finally:
        keep_file.unlink(missing_ok=True)


def test_save_addon_option_handles_corrupt_file(tmp_path):
    options_file = tmp_path / "options.json"
    options_file.write_text("not json")

    with patch("mammamiradio.web.persistence.Path", return_value=options_file):
        _save_addon_option("chaos_mode_active", True)

    assert json.loads(options_file.read_text()) == {"chaos_mode_active": True}


def test_boot_read_back_does_not_arm_first_strike(tmp_path):
    from mammamiradio.main import _read_persisted_chaos_mode

    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps({"chaos_mode_active": True}))
    config = load_config(TOML_PATH)
    config.is_addon = True

    with patch("mammamiradio.main.Path", return_value=options_file):
        enabled = _read_persisted_chaos_mode(config)

    state = StationState(chaos_mode_active=enabled)
    assert state.chaos_mode_active is True
    assert state.chaos_pending is None


def test_provider_health_distinguishes_chaos_script_and_audio_degradation():
    config = load_config(TOML_PATH)
    state = StationState(
        chaos_mode_active=True,
        chaos_script_fallbacks=2,
        chaos_audio_failures=1,
        chaos_last_degraded_reason="audio_failure",
    )

    health = _provider_health_snapshot(config, state)

    assert health["chaos"]["enabled"] is True
    assert health["chaos"]["script_fallbacks"] == 2
    assert health["chaos"]["audio_failures"] == 1
    assert health["chaos"]["last_degraded_reason"] == "audio_failure"
