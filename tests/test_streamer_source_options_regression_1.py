"""Regression tests for source-option availability states."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.config import load_config
from mammamiradio.models import StationState, Track
from mammamiradio.streamer import LiveStreamHub, router

TOML_PATH = str(Path(__file__).parent.parent / "radio.toml")


def _make_test_app(*, is_addon: bool = False) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    config = load_config(TOML_PATH)
    config.is_addon = is_addon
    config.spotify_client_id = ""
    config.spotify_client_secret = ""
    config.cache_dir = Path("/tmp/mammamiradio-test-cache")
    config.cache_dir.mkdir(parents=True, exist_ok=True)

    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    app.state.stream_hub = LiveStreamHub()
    app.state.station_state = StationState(
        playlist=[
            Track(title="Song A", artist="Artist A", duration_ms=180_000, spotify_id="t1"),
        ]
    )
    app.state.config = config
    app.state.start_time = time.time()
    return app


@pytest.mark.asyncio
async def test_source_options_disable_picker_when_spotify_auth_is_unavailable():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    with patch("mammamiradio.streamer.list_user_playlists", side_effect=Exception("No client_id")):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/api/spotify/source-options")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["capabilities"]["supports_user_sources"] is False
    assert "Add Spotify credentials first" in body["capabilities"]["reason"]
