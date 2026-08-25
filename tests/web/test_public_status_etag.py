import math
import time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from mammamiradio.home.authorization import HomeAuthorization
from mammamiradio.home.moment_receipts import MomentStore
from tests.web.test_route_smoke import _make_app


async def _get_public_status(app, headers=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        return await client.get("/public-status", headers=headers)


@pytest.mark.asyncio
async def test_public_status_response_has_etag_and_cache_control():
    app = _make_app()
    app.state.config.identity.station_name = "Radio Città"
    app.state.station_state.now_streaming = {
        "label": "Città",
        "started": time.time() - 1,
        "duration_sec": math.inf,
        "metadata": {"public_asset": Path("music/citta.mp3"), "analysis": [math.nan, Decimal("NaN")]},
    }
    resp = await _get_public_status(app)
    assert resp.status_code == 200 and resp.headers["ETag"].startswith('W/"')
    assert {"public", "max-age=1"} <= {part.strip() for part in resp.headers["Cache-Control"].split(",")}
    assert "Radio Città" in resp.text and b"Radio Citt\\u00e0" not in resp.content
    assert b"Infinity" not in resp.content and b"NaN" not in resp.content
    streaming = resp.json()["now_streaming"]
    assert streaming["metadata"] == {"public_asset": "music/citta.mp3", "analysis": [None, None]}
    assert streaming["duration_sec"] is None and resp.json()["current_duration_sec"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("validator_kind", ["weak", "strong", "list", "wildcard"])
async def test_public_status_if_none_match_uses_weak_comparison(validator_kind):
    app = _make_app()
    app.state.station_state.now_streaming = {"started": time.time() - 12, "metadata": {"duration_ms": 180_000}}
    first = await _get_public_status(app)
    assert first.json()["current_progress_sec"] is not None
    etag = first.headers["ETag"]
    strong = etag.removeprefix("W/")
    validators = {"weak": etag, "strong": strong, "list": f'W/"stale", {strong}', "wildcard": "*"}
    second = await _get_public_status(app, {"If-None-Match": validators[validator_kind]})
    assert second.status_code == 304 and second.content == b""
    assert second.headers["ETag"] == etag and second.headers["Cache-Control"] == "public, max-age=1"


@pytest.mark.asyncio
@pytest.mark.parametrize("validator", ['W/"other"', 'W/"unterminated', '*, W/"other"', ""])
async def test_public_status_malformed_or_stale_validator_returns_200(validator):
    app = _make_app()
    response = await _get_public_status(app, {"If-None-Match": validator})
    assert response.status_code == 200 and response.json()["session_stopped"] is False


@pytest.mark.asyncio
async def test_same_label_home_recurrences_change_etag_without_leaking_hidden_rows():
    app = _make_app()
    state = app.state.station_state
    config = app.state.config
    config.homeassistant.context_enabled = config.homeassistant.enabled = True
    config.ha_token = "test-token"
    state.ha_context = "enabled"
    state.home_authorization = HomeAuthorization.legacy()
    state.moment_store = MomentStore()
    state.ha_last_event_label = "Porta ingresso"
    minute = (time.time() // 60 - 1) * 60
    state.ha_last_event_ts = minute + 30
    for _ in range(3):
        state.moment_store.record(lane="interrupt", family="arrival", public_label="Rientro", status="aired")
    first = await _get_public_status(app)
    state.moment_store.record(lane="interrupt", family="arrival", public_label="Rientro", status="dropped")
    hidden = await _get_public_status(app, {"If-None-Match": first.headers["ETag"]})
    assert hidden.status_code == 304
    state.moment_store.record(lane="interrupt", family="arrival", public_label="Rientro", status="aired")
    rolled = await _get_public_status(app, {"If-None-Match": first.headers["ETag"]})
    assert rolled.status_code == 200 and rolled.json()["ha_moments"]["recent"] == first.json()["ha_moments"]["recent"]
    state.ha_last_event_ts = minute + 31
    second = await _get_public_status(app, {"If-None-Match": rolled.headers["ETag"]})
    assert second.status_code == 200 and second.headers["ETag"] != rolled.headers["ETag"]
    assert second.json()["ha_moments"]["last_event_ago_min"] == 1
    config.ha_token = "other-token"
    assert (await _get_public_status(app, {"If-None-Match": second.headers["ETag"]})).status_code == 200
    config.ha_token = ""
    without_secret = await _get_public_status(app)
    state.ha_last_event_ts += 1
    assert (await _get_public_status(app, {"If-None-Match": without_secret.headers["ETag"]})).status_code == 304


@pytest.mark.asyncio
async def test_public_status_if_none_match_state_change_returns_200_with_new_etag():
    app = _make_app()
    first = await _get_public_status(app)
    old_etag = first.headers["ETag"]
    app.state.station_state.session_stopped = True
    app.state.station_state.last_state_change_at = time.time()
    second = await _get_public_status(app, {"If-None-Match": old_etag})
    assert second.status_code == 200 and second.headers["ETag"] != old_etag
    assert second.json()["session_stopped"] is True
