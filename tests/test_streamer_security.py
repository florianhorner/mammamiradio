from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fakeitaliradio.models import StationState, Track
from fakeitaliradio.streamer import router


def _track(idx: int) -> Track:
    return Track(
        title=f"Song {idx}",
        artist="Artist",
        duration_ms=180000,
        spotify_id=f"track-{idx}",
    )


def _client_with_state(api_key: str, playlist_size: int = 2) -> tuple[TestClient, StationState]:
    app = FastAPI()
    app.include_router(router)

    state = StationState(playlist=[_track(i) for i in range(playlist_size)])
    app.state.station_state = state
    app.state.queue = asyncio.Queue()
    app.state.start_time = 0.0
    app.state.config = SimpleNamespace(dashboard_api_key=api_key)

    return TestClient(app), state


def test_write_endpoints_require_api_key_when_configured():
    client, _ = _client_with_state(api_key="secret")

    response = client.post("/api/shuffle")
    assert response.status_code == 401

    response = client.post("/api/shuffle", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401

    response = client.post("/api/shuffle", headers={"X-API-Key": "secret"})
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_write_endpoints_accept_query_key_fallback():
    client, _ = _client_with_state(api_key="secret")
    response = client.post("/api/skip?key=secret")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_remove_track_blocks_last_track():
    client, state = _client_with_state(api_key="secret", playlist_size=1)
    response = client.post(
        "/api/playlist/remove",
        headers={"X-API-Key": "secret"},
        json={"index": 0},
    )
    assert response.status_code == 400
    assert "last track" in response.json()["detail"].lower()
    assert len(state.playlist) == 1


def test_remove_track_succeeds_when_more_than_one_track():
    client, state = _client_with_state(api_key="secret", playlist_size=2)
    response = client.post(
        "/api/playlist/remove",
        headers={"X-API-Key": "secret"},
        json={"index": 0},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert len(state.playlist) == 1
