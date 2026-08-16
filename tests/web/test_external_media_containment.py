from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.core.config import load_config
from mammamiradio.core.models import StationState, Track
from mammamiradio.web.streamer import LiveStreamHub, _external_extractors_status, router

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def _app(tmp_path: Path, *, is_addon: bool) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    config = load_config(TOML_PATH)
    config.is_addon = is_addon
    config.allow_ytdlp = False
    config.cache_dir = tmp_path
    app.state.config = config
    app.state.station_state = StationState(
        playlist=[
            Track(
                title="Local Song",
                artist="Operator",
                duration_ms=180_000,
                local_path=tmp_path / "local.mp3",
                source="local",
            )
        ]
    )
    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    app.state.source_switch_lock = asyncio.Lock()
    app.state.stream_hub = LiveStreamHub()
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url="http://testserver",
    )


def test_extractor_status_reports_shared_addon_artifact_truth(tmp_path):
    addon = _app(tmp_path, is_addon=True)
    addon.state.config.allow_ytdlp = True

    assert _external_extractors_status(addon.state.config) == {"state": "unavailable_in_addon"}


def test_extractor_status_uses_effective_standalone_capability(tmp_path):
    standalone = _app(tmp_path, is_addon=False)
    standalone.state.config.allow_ytdlp = True

    with patch("mammamiradio.web.streamer.external_media_enabled", return_value=True):
        assert _external_extractors_status(standalone.state.config) == {"state": "operator_enabled"}
    with patch("mammamiradio.web.streamer.external_media_enabled", return_value=False):
        assert _external_extractors_status(standalone.state.config) == {"state": "disabled"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/api/playlist/add-external",
            {
                "youtube_id": "dQw4w9WgXcQ",
                "title": "External",
                "artist": "Artist",
                "duration_ms": 180_000,
            },
        ),
        (
            "/api/playlist/add",
            {"title": "Metadata only", "artist": "Artist", "duration_ms": 180_000},
        ),
        ("/api/playlist/enrich", {"url": "https://example.com/playlist"}),
        ("/api/playlist/load", {"url": "https://example.com/playlist"}),
        ("/api/heading", {"seed": "classic://italian/80s"}),
    ],
)
async def test_addon_external_only_routes_return_actionable_403(tmp_path, path, payload):
    app = _app(tmp_path, is_addon=True)
    async with _client(app) as client:
        response = await client.post(path, json=payload)

    assert response.status_code == 403
    assert response.json() == {
        "ok": False,
        "error_code": "external_media_unavailable_in_addon",
        "message": (
            "External resolution is unavailable in this Home Assistant add-on. Local search and shout-outs still work."
        ),
        "retryable": False,
        "next_action": "Use a local track or send the request as a shout-out.",
        "stream_status": "unaffected",
    }


@pytest.mark.asyncio
async def test_addon_search_keeps_local_results_without_calling_extractor(tmp_path):
    app = _app(tmp_path, is_addon=True)
    with patch("mammamiradio.playlist.downloader.search_ytdlp_metadata") as external_search:
        async with _client(app) as client:
            response = await client.get("/api/search", params={"q": "Local", "include_external": "true"})

    assert response.status_code == 200
    assert [item["title"] for item in response.json()["results"]] == ["Local Song"]
    assert response.json()["external"] == []
    external_search.assert_not_called()


@pytest.mark.asyncio
async def test_standalone_disabled_external_route_keeps_existing_conflict_contract(tmp_path):
    app = _app(tmp_path, is_addon=False)
    async with _client(app) as client:
        response = await client.post(
            "/api/playlist/add-external",
            json={
                "youtube_id": "dQw4w9WgXcQ",
                "title": "External",
                "artist": "Artist",
                "duration_ms": 180_000,
            },
        )

    assert response.status_code == 409
    assert response.json() == {"ok": False, "error": "external_downloads_disabled"}
