"""ETag + Cache-Control + If-None-Match tests for the v1 contract."""

from __future__ import annotations

import httpx
import pytest

from tests.integrations.conftest import make_integrations_app, play_music_segment


@pytest.mark.asyncio
async def test_response_has_etag_and_cache_control():
    app = make_integrations_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/integrations/v1/now-playing")
    assert resp.status_code == 200
    etag = resp.headers.get("ETag")
    assert etag is not None
    assert etag.startswith('W/"')
    assert "max-age" in resp.headers.get("Cache-Control", "")


@pytest.mark.asyncio
async def test_if_none_match_unchanged_state_returns_304():
    app = make_integrations_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.get("/api/integrations/v1/now-playing")
        etag = first.headers["ETag"]
        second = await client.get(
            "/api/integrations/v1/now-playing",
            headers={"If-None-Match": etag},
        )
    assert second.status_code == 304
    assert second.headers.get("ETag") == etag
    # 304 should not have a JSON body
    assert second.content == b""


@pytest.mark.asyncio
async def test_if_none_match_state_change_returns_200_with_new_etag():
    app = make_integrations_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.get("/api/integrations/v1/now-playing")
        old_etag = first.headers["ETag"]
        # Mutate state — a new segment starts
        play_music_segment(app.state.station_state)
        second = await client.get(
            "/api/integrations/v1/now-playing",
            headers={"If-None-Match": old_etag},
        )
    assert second.status_code == 200
    new_etag = second.headers["ETag"]
    assert new_etag != old_etag
    body = second.json()
    assert body["now_playing"] is not None
    assert body["now_playing"]["segment_class"] == "music"


@pytest.mark.asyncio
async def test_etag_includes_session_stopped_in_fingerprint():
    app = make_integrations_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.get("/api/integrations/v1/now-playing")
        old_etag = first.headers["ETag"]
        # Flip session_stopped — should change fingerprint
        app.state.station_state.session_stopped = True
        app.state.station_state.last_state_change_at += 1.0
        second = await client.get("/api/integrations/v1/now-playing")
    new_etag = second.headers["ETag"]
    assert new_etag != old_etag
