"""ETag + If-None-Match tests for /public-status listener polling."""

from __future__ import annotations

import math
import time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from tests.web.test_route_smoke import _make_app


async def _get_public_status(app, headers=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        return await client.get("/public-status", headers=headers)


@pytest.mark.asyncio
async def test_public_status_response_has_etag_and_cache_control():
    app = _make_app()
    app.state.config.identity.station_name = "Radio Città"
    app.state.station_state.now_streaming = {
        "type": "music",
        "label": "Città",
        "started": time.time() - 1,
        "duration_sec": math.inf,
        "metadata": {
            "public_asset": Path("music/citta.mp3"),
            "duration_ms": math.inf,
            "analysis": {"peak": math.nan, "rms": Decimal("NaN")},
        },
    }
    resp = await _get_public_status(app)
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
    assert b"Infinity" not in resp.content and b"NaN" not in resp.content
    payload = resp.json()
    assert payload["now_streaming"]["metadata"]["public_asset"] == "music/citta.mp3"
    assert payload["now_streaming"]["duration_sec"] is None
    assert payload["now_streaming"]["metadata"]["duration_ms"] is None
    assert payload["now_streaming"]["metadata"]["analysis"] == {"peak": None, "rms": None}
    assert payload["current_duration_sec"] is None


@pytest.mark.asyncio
async def test_public_status_if_none_match_unchanged_state_returns_304():
    app = _make_app()
    first = await _get_public_status(app)
    etag = first.headers["ETag"]
    second = await _get_public_status(app, {"If-None-Match": etag})
    assert second.status_code == 304
    assert second.headers.get("ETag") == etag
    assert second.content == b""


@pytest.mark.asyncio
@pytest.mark.parametrize("validator_kind", ["strong", "list", "wildcard"])
async def test_public_status_if_none_match_uses_weak_comparison(validator_kind):
    app = _make_app()
    first = await _get_public_status(app)
    etag = first.headers["ETag"]
    strong = etag.removeprefix("W/")
    validators = {"strong": strong, "list": f'W/"stale", {strong}', "wildcard": "*"}
    second = await _get_public_status(app, {"If-None-Match": validators[validator_kind]})

    assert second.status_code == 304
    assert second.headers["ETag"] == etag
    assert second.headers["Cache-Control"] == "public, max-age=1"
    assert second.content == b""


@pytest.mark.asyncio
@pytest.mark.parametrize("validator", ['W/"other"', 'W/"unterminated', '*, W/"other"', ""])
async def test_public_status_malformed_or_stale_validator_returns_200(validator):
    app = _make_app()
    response = await _get_public_status(app, {"If-None-Match": validator})

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
    first = await _get_public_status(app)
    etag = first.headers["ETag"]
    assert first.json()["current_progress_sec"] is not None
    second = await _get_public_status(app, {"If-None-Match": etag})
    assert second.status_code == 304
    assert second.headers.get("ETag") == etag


@pytest.mark.asyncio
async def test_public_status_if_none_match_state_change_returns_200_with_new_etag():
    app = _make_app()
    first = await _get_public_status(app)
    old_etag = first.headers["ETag"]
    app.state.station_state.session_stopped = True
    app.state.station_state.last_state_change_at = time.time()
    second = await _get_public_status(app, {"If-None-Match": old_etag})
    assert second.status_code == 200
    assert second.headers["ETag"] != old_etag
    assert second.json()["session_stopped"] is True
