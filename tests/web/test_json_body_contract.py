"""Malformed JSON-body contract tests for web write routes."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from mammamiradio.core.config import load_config
from mammamiradio.core.models import StationState, Track
from mammamiradio.web.json_body import read_json_object
from mammamiradio.web.listener_requests import router as listener_requests_router
from mammamiradio.web.streamer import LiveStreamHub, router

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def _make_test_app(
    tmp_path: Path,
    *,
    admin_password: str = "",
    admin_token: str = "",
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.include_router(listener_requests_router)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("ADMIN_PASSWORD", "")
        monkeypatch.setenv("ADMIN_TOKEN", "")
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HASSIO_TOKEN", raising=False)
        config = load_config(TOML_PATH)
    config.admin_password = admin_password
    config.admin_token = admin_token
    config.is_addon = False
    config.cache_dir = tmp_path

    state = StationState(
        playlist=[
            Track(title="Song A", artist="Artist A", duration_ms=180_000, spotify_id="a"),
            Track(title="Song B", artist="Artist B", duration_ms=180_000, spotify_id="b"),
            Track(title="Song C", artist="Artist C", duration_ms=180_000, spotify_id="c"),
        ],
    )
    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    app.state.source_switch_lock = asyncio.Lock()
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    hub = LiveStreamHub()
    hub.bind_state(state)
    app.state.stream_hub = hub
    return app


def _state_snapshot(app: FastAPI) -> dict[str, Any]:
    state = app.state.station_state
    config = app.state.config
    return {
        "playlist": tuple(track.display for track in state.playlist),
        "playlist_revision": state.playlist_revision,
        "source_revision": state.source_revision,
        "queue_size": app.state.queue.qsize(),
        "queued_segments": tuple(tuple(sorted(row.items())) for row in state.queued_segments),
        "pending_requests": tuple(tuple(sorted(row.items())) for row in state.pending_requests),
        "listener_rate_limits": dict(state._listener_request_rl),
        "blocklist": dict(state.blocklist),
        "pinned_track": state.pinned_track.display if state.pinned_track else None,
        "force_next": state.force_next,
        "operator_force_pending": state.operator_force_pending,
        "chaos_mode_active": state.chaos_mode_active,
        "chaos_pending": state.chaos_pending,
        "chaos_cutover_epoch": state.chaos_cutover_epoch,
        "super_italian_mode": config.super_italian_mode,
        "broadcast_chain": config.audio.broadcast_chain,
        "quality_profile": config.models.active_profile,
        "party_mode": config.party_mode,
        "host_personalities": tuple(
            (host.name, tuple(sorted(host.personality.to_dict().items()))) for host in config.hosts
        ),
        "env_toggles": tuple(
            (key, os.environ.get(key))
            for key in (
                "MAMMAMIRADIO_CHAOS_MODE",
                "MAMMAMIRADIO_SUPER_ITALIAN",
                "MAMMAMIRADIO_BROADCAST_CHAIN",
                "MAMMAMIRADIO_QUALITY",
                "MAMMAMIRADIO_FESTIVAL_MODE",
            )
        ),
    }


async def _request_with_body(body: bytes) -> Request:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )


@pytest.mark.asyncio
async def test_read_json_object_accepts_json_object():
    request = await _request_with_body(b'{"ok": true, "count": 2}')

    body, error = await read_json_object(request)

    assert error is None
    assert body == {"ok": True, "count": 2}


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{bad",
        b"\x80",
        b'["not", "an", "object"]',
    ],
    ids=["empty", "malformed", "invalid-utf8", "non-object"],
)
@pytest.mark.asyncio
async def test_read_json_object_rejects_parse_layer_failures(body: bytes):
    request = await _request_with_body(body)

    parsed, error = await read_json_object(request)

    assert parsed == {}
    assert error is not None
    assert error.status_code == 422
    payload = json.loads(bytes(error.body))
    assert payload["ok"] is False
    assert isinstance(payload["error"], str)
    assert payload["error"]


JSON_BODY_WRITE_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/api/setup/save-keys"),
    ("POST", "/api/queue/remove"),
    ("POST", "/api/trigger"),
    ("POST", "/api/interrupt"),
    ("PATCH", "/api/pacing"),
    ("POST", "/api/chaos"),
    ("POST", "/api/super-italian"),
    ("POST", "/api/broadcast-chain"),
    ("POST", "/api/quality"),
    ("POST", "/api/party"),
    ("POST", "/api/credentials"),
    ("POST", "/api/playlist/remove"),
    ("POST", "/api/track/ban"),
    ("POST", "/api/track/unban"),
    ("POST", "/api/playlist/move"),
    ("POST", "/api/playlist/add-external"),
    ("POST", "/api/playlist/add"),
    ("POST", "/api/direction"),
    ("POST", "/api/heading"),
    ("POST", "/api/playlist/enrich"),
    ("POST", "/api/playlist/load"),
    ("POST", "/api/playlist/move_to_next"),
    ("POST", "/api/track-rules"),
    ("PATCH", "/api/hosts/Marco/personality"),
    ("POST", "/api/listener-requests/dismiss"),
    ("POST", "/api/listener-request"),
)

BAD_JSON_BODIES: tuple[tuple[str, bytes], ...] = (
    ("empty", b""),
    ("malformed", b"{bad"),
    ("non-object", b'["not", "an", "object"]'),
)


@pytest.mark.parametrize(("method", "path"), JSON_BODY_WRITE_ROUTES)
@pytest.mark.asyncio
async def test_json_body_write_routes_reject_bad_bodies_without_mutation(
    tmp_path: Path,
    method: str,
    path: str,
):
    app = _make_test_app(tmp_path)
    before = _state_snapshot(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url="http://testserver",
    ) as client:
        for case_name, body in BAD_JSON_BODIES:
            response = await client.request(
                method,
                path,
                content=body,
                headers={"content-type": "application/json"},
            )
            payload = response.json()
            assert response.status_code == 422, case_name
            assert payload["ok"] is False
            assert isinstance(payload["error"], str)
            assert payload["error"]
            assert _state_snapshot(app) == before


@pytest.mark.asyncio
async def test_admin_auth_runs_before_json_body_parse_for_non_loopback(tmp_path: Path):
    app = _make_test_app(tmp_path, admin_password="secret")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.10", 12345)),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/quality",
            content=b"{bad",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Admin authentication required"


def test_web_routes_do_not_await_request_json_directly():
    failures: list[str] = []
    web_dir = Path("mammamiradio/web")
    for path in sorted(web_dir.rglob("*.py")):
        if path.name == "json_body.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Await):
                continue
            call = node.value
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if isinstance(func, ast.Attribute) and func.attr == "json":
                failures.append(f"{path}:{node.lineno}")

    assert failures == []
