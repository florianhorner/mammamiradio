"""ETag + If-None-Match tests for /public-status listener polling."""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from tests.web.test_route_smoke import _make_app


@pytest.mark.asyncio
async def test_public_status_response_has_etag_and_cache_control():
    app = _make_app()
    app.state.config.identity.station_name = "Radio Città"
    app.state.station_state.now_streaming = {
        "type": "music",
        "label": "Città",
        "started": time.time() - 1,
        "metadata": {"public_asset": Path("music/citta.mp3")},
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    etag = resp.headers.get("ETag")
    assert etag is not None
    assert etag.startswith('W/"')
    cache_control = resp.headers.get("Cache-Control", "")
    directives = {part.strip() for part in cache_control.split(",")}
    assert "public" in directives
    assert "max-age=1" in directives
    assert "Radio Città" in resp.text
    assert b"Radio Citt\\u00e0" not in resp.content
    assert resp.json()["now_streaming"]["metadata"]["public_asset"] == "music/citta.mp3"


@pytest.mark.asyncio
async def test_public_status_if_none_match_unchanged_state_returns_304():
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.get("/public-status")
        etag = first.headers["ETag"]
        second = await client.get("/public-status", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.headers.get("ETag") == etag
    assert second.content == b""


@pytest.mark.asyncio
@pytest.mark.parametrize("validator_kind", ["strong", "list", "wildcard"])
async def test_public_status_if_none_match_uses_weak_comparison(validator_kind):
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.get("/public-status")
        etag = first.headers["ETag"]
        strong = etag.removeprefix("W/")
        validators = {
            "strong": strong,
            "list": f'W/"stale", {strong}',
            "wildcard": "*",
        }
        second = await client.get("/public-status", headers={"If-None-Match": validators[validator_kind]})

    assert second.status_code == 304
    assert second.headers["ETag"] == etag
    assert second.headers["Cache-Control"] == "public, max-age=1"
    assert second.content == b""


@pytest.mark.asyncio
@pytest.mark.parametrize("validator", ['W/"other"', 'W/"unterminated', '*, W/"other"', ""])
async def test_public_status_malformed_or_stale_validator_returns_200(validator):
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/public-status", headers={"If-None-Match": validator})

    assert response.status_code == 200
    assert response.json()["session_stopped"] is False


@pytest.mark.asyncio
async def test_public_status_etag_stable_when_only_progress_advances():
    """Progress and uptime advance every poll; the ETag must not."""
    app = _make_app()
    state = app.state.station_state
    now = time.time()
    state.now_streaming = {
        "type": "music",
        "label": "Artist — Title",
        "started": now - 12.0,
        "metadata": {"duration_ms": 180_000},
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.get("/public-status")
        etag = first.headers["ETag"]
        assert first.json()["current_progress_sec"] is not None
        second = await client.get("/public-status", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.headers.get("ETag") == etag


@pytest.mark.asyncio
async def test_public_status_if_none_match_state_change_returns_200_with_new_etag():
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.get("/public-status")
        old_etag = first.headers["ETag"]
        app.state.station_state.session_stopped = True
        app.state.station_state.last_state_change_at = time.time()
        second = await client.get("/public-status", headers={"If-None-Match": old_etag})
    assert second.status_code == 200
    assert second.headers["ETag"] != old_etag
    assert second.json()["session_stopped"] is True
