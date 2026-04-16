"""Structural invariants pinned during the 2026-04-16 documentation audit.

Two findings from `docs/2026-04-16-documentation-structure-audit.md` are locked here
because they are easy to regress during refactors and the docs alone will not catch it:

  * Finding #6 — root route (`/`) ownership.
    `/` MUST serve the public listener UI for anonymous visitors and MUST serve the
    admin control-room UI when the request arrives through HA addon ingress. This
    dual-mode behavior is intentional: operators on HA land on their cockpit without
    typing `/admin`, while public listeners see the player.
    Implementation: `mammamiradio/streamer.py:787-796`.

  * Finding #13 — `repository.yaml` duplication.
    Only the repo-root `repository.yaml` is wired into HA addon discovery; all install
    docs tell users to add the repo root URL, and `scripts/test-addon-local.sh`
    /`scripts/validate-addon.sh` only check the root file. The nested
    `ha-addon/repository.yaml` previously shipped with identical contents but had no
    consumer — it was removed to eliminate the silent sync boundary. This test
    guarantees the duplicate does not come back.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.config import load_config
from mammamiradio.models import StationState, Track
from mammamiradio.streamer import LiveStreamHub, router

REPO_ROOT = Path(__file__).resolve().parent.parent
TOML_PATH = str(REPO_ROOT / "radio.toml")


def _build_app(*, is_addon: bool) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    config = load_config(TOML_PATH)
    config.is_addon = is_addon
    state = StationState(
        playlist=[Track(title="Test", artist="Test", duration_ms=180_000, spotify_id="t1")],
    )
    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    hub = LiveStreamHub()
    hub.bind_state(state)
    app.state.stream_hub = hub
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    return app


# ---------------------------------------------------------------------------
# Finding #6 — root route dual-mode contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_root_serves_listener_for_public_visitors():
    """`/` must serve the listener UI when the request is not HA addon ingress."""
    app = _build_app(is_addon=False)
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert "Regia — Control Room" not in resp.text, (
        "`/` swapped to admin UI for a public visitor. Contract: `/` is listener-first; "
        "only trusted HA ingress may flip it to admin. See findings #6 in the 2026-04-16 audit."
    )


@pytest.mark.asyncio
async def test_root_serves_admin_for_ha_ingress():
    """`/` must serve the admin UI when an addon request arrives via trusted HA ingress."""
    app = _build_app(is_addon=True)
    transport = httpx.ASGITransport(app=app, client=("172.30.32.5", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})
    assert resp.status_code == 200
    assert "Regia — Control Room" in resp.text, (
        "`/` failed to swap to admin UI for a trusted HA ingress request. The dual-mode "
        "contract lets addon operators land on their cockpit without typing `/admin`."
    )


# ---------------------------------------------------------------------------
# Finding #13 — single repository.yaml contract
# ---------------------------------------------------------------------------


def test_repository_yaml_exists_only_at_root():
    """The HA addon repository manifest must live at exactly one path: the repo root."""
    root = REPO_ROOT / "repository.yaml"
    nested = REPO_ROOT / "ha-addon" / "repository.yaml"

    assert root.is_file(), (
        "`repository.yaml` is missing from the repo root. HA cannot discover the addon "
        "without it — install docs tell users to add the repo root URL."
    )
    assert not nested.exists(), (
        "`ha-addon/repository.yaml` reappeared. Only the root file is wired into HA "
        "addon discovery; a second copy creates a silent sync boundary. See finding #13 "
        "in the 2026-04-16 documentation audit."
    )
