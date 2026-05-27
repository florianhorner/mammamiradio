"""HA Supervisor ingress + stream URL resolution tests for the v1 contract."""

from __future__ import annotations

import httpx
import pytest

from tests.integrations.conftest import make_integrations_app


@pytest.mark.asyncio
async def test_absolute_url_set_when_request_is_direct():
    app = make_integrations_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    stream = body["stream"]
    assert stream["relative_url"] == "/stream"
    assert stream.get("absolute_url", "").endswith("/stream")


@pytest.mark.asyncio
async def test_absolute_url_omitted_when_request_is_through_supervisor_ingress():
    app = make_integrations_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get(
            "/api/integrations/v1/now-playing",
            headers={"X-Ingress-Path": "/api/hassio_ingress/abc-token"},
        )
    body = resp.json()
    stream = body["stream"]
    # relative_url canonical — same-instance consumers resolve against addon URL
    assert stream["relative_url"] == "/stream"
    # absolute_url MUST be omitted under ingress (per-session ingress token is
    # not safe to bake into an external contract)
    assert "absolute_url" not in stream
