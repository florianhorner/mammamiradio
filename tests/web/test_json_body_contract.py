"""Malformed JSON-body contract tests for web write routes."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

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
    app.state.csrf_token = "json-contract-csrf-token"
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
        "boundary_imaging": config.audio.boundary_imaging,
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
                "MAMMAMIRADIO_BOUNDARY_IMAGING",
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
    ("POST", "/api/boundary-imaging"),
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
            headers = {"content-type": "application/json"}
            if path == "/api/setup/save-keys":
                headers.update(
                    {
                        "host": "127.0.0.1",
                        "x-radio-csrf-token": "json-contract-csrf-token",
                    }
                )
            response = await client.request(
                method,
                path,
                content=body,
                headers=headers,
            )
            payload = response.json()
            assert response.status_code == 422, case_name
            assert payload["ok"] is False
            assert isinstance(payload["error"], str)
            assert payload["error"]
            assert _state_snapshot(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/quality", "/api/direction"])
async def test_admin_auth_runs_before_json_body_parse_for_non_loopback(tmp_path: Path, path: str):
    app = _make_test_app(tmp_path, admin_password="secret")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.10", 12345)),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            path,
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


# ---------------------------------------------------------------------------
# Public listener-request body bounds. The route is unauthenticated and its
# parsing runs on the event loop that feeds the audio stream, so the body must
# be bounded BEFORE it is buffered or decoded, and a throttled caller must be
# turned away before a single body byte is read.
# ---------------------------------------------------------------------------

_LISTENER_CLIENT = ("203.0.113.9", 40000)


class _ChunkedDedication:
    """ASGI receive serving a valid oversized dedication in 1 KiB chunks.

    ``served`` counts the chunks handed to the app, so a test can prove the
    reader stopped early instead of buffering the whole body.
    """

    def __init__(self, total_chunks: int = 1000) -> None:
        filler = b"a" * 1024
        head = b'{"name": "Luca", "message": "'
        tail = b'"}'
        self._chunks = [head + filler[: 1024 - len(head)]]
        self._chunks += [filler] * (total_chunks - 2)
        self._chunks.append(filler[: 1024 - len(tail)] + tail)
        self.served = 0

    async def __call__(self) -> dict[str, Any]:
        if self.served >= len(self._chunks):
            return {"type": "http.disconnect"}
        chunk = self._chunks[self.served]
        self.served += 1
        return {"type": "http.request", "body": chunk, "more_body": self.served < len(self._chunks)}


async def _post_raw(
    app: FastAPI,
    receive: _ChunkedDedication,
    *,
    content_length: int | None,
) -> tuple[int, dict[str, Any]]:
    headers = [(b"content-type", b"application/json"), (b"host", b"testserver")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/listener-request",
        "raw_path": b"/api/listener-request",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": _LISTENER_CLIENT,
        "server": ("testserver", 80),
    }
    sent: list[dict[str, Any]] = []

    async def send(message: Any) -> None:
        sent.append(message)

    with patch("mammamiradio.web.listener_requests._download_listener_song", new_callable=AsyncMock):
        await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(body)


def _assert_nothing_admitted(app: FastAPI) -> None:
    state = app.state.station_state
    assert state.pending_requests == []
    assert state._listener_request_rl == {}


@pytest.mark.asyncio
async def test_listener_request_refuses_oversized_content_length_without_reading_body(tmp_path: Path):
    app = _make_test_app(tmp_path)
    receive = _ChunkedDedication()

    status, payload = await _post_raw(app, receive, content_length=1024 * 1000)

    assert status == 413
    assert payload["ok"] is False
    assert "too long" in payload["error"]
    assert receive.served == 0, "an oversized declared body must be refused before any byte is read"
    _assert_nothing_admitted(app)


@pytest.mark.asyncio
async def test_listener_request_stops_reading_oversized_chunked_body_early(tmp_path: Path):
    app = _make_test_app(tmp_path)
    receive = _ChunkedDedication()

    status, payload = await _post_raw(app, receive, content_length=None)

    assert status == 413
    assert payload["ok"] is False
    # 8 KiB cap over 1 KiB chunks: the reader must give up on the 9th chunk,
    # never buffer the remaining ~990 KiB.
    assert receive.served <= 9, f"reader consumed {receive.served} chunks of an oversized body"
    _assert_nothing_admitted(app)


@pytest.mark.asyncio
async def test_listener_request_understated_content_length_is_still_bounded(tmp_path: Path):
    app = _make_test_app(tmp_path)
    receive = _ChunkedDedication()

    status, _payload = await _post_raw(app, receive, content_length=100)

    assert status == 413
    assert receive.served <= 9
    _assert_nothing_admitted(app)


@pytest.mark.asyncio
async def test_listener_request_rate_limited_caller_is_refused_before_body_read(tmp_path: Path):
    from mammamiradio.web.listener_requests import _hash_submitter_ip

    app = _make_test_app(tmp_path)
    state = app.state.station_state
    key = _hash_submitter_ip(_LISTENER_CLIENT[0], app.state.config)
    stamped = time.time()
    state._listener_request_rl = {key: stamped}
    receive = _ChunkedDedication()

    status, payload = await _post_raw(app, receive, content_length=None)

    assert status == 429
    assert payload["ok"] is False
    assert 0 < payload["retry_after"] <= 30
    assert receive.served == 0, "a throttled caller must cost no body read"
    assert state._listener_request_rl == {key: stamped}, "a refusal must not move the window"
    assert state.pending_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "length"), [("name", 241), ("message", 2001)])
async def test_listener_request_refuses_overlong_raw_text_without_queueing(tmp_path: Path, field: str, length: int):
    app = _make_test_app(tmp_path)
    body = {"name": "Luca", "message": "Ciao!"}
    body[field] = "b" * length

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=_LISTENER_CLIENT),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/api/listener-request", json=body)

    assert response.status_code == 413
    payload = response.json()
    assert payload["ok"] is False
    assert "too long" in payload["error"]
    _assert_nothing_admitted(app)


@pytest.mark.asyncio
async def test_listener_request_long_but_legitimate_text_is_still_accepted(tmp_path: Path):
    """Text at the raw bounds keeps the old outcome: accepted, then truncated."""
    app = _make_test_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=_LISTENER_CLIENT),
        base_url="http://testserver",
    ) as client:
        with patch("mammamiradio.web.listener_requests._download_listener_song", new_callable=AsyncMock):
            response = await client.post(
                "/api/listener-request",
                json={"name": "N" * 240, "message": "ciao " * 400},
            )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    pending = app.state.station_state.pending_requests
    assert len(pending) == 1
    # The sanitiser still truncates exactly as before (cap plus an ellipsis).
    assert pending[0]["name"].startswith("N" * 60)
    assert len(pending[0]["name"]) <= 63
    assert len(pending[0]["message"]) <= 203


@pytest.mark.asyncio
async def test_read_json_object_max_bytes_parses_small_body_and_default_is_unbounded():
    small = b'{"name": "Luca", "message": "Ciao!"}'
    body, error = await read_json_object(await _request_with_body(small), max_bytes=8 * 1024)
    assert error is None
    assert body == {"name": "Luca", "message": "Ciao!"}

    big = b'{"message": "' + b"a" * (64 * 1024) + b'"}'
    body, error = await read_json_object(await _request_with_body(big))
    assert error is None
    assert len(body["message"]) == 64 * 1024

    body, error = await read_json_object(await _request_with_body(big), max_bytes=8 * 1024)
    assert body == {}
    assert error is not None
    assert error.status_code == 413


@pytest.mark.parametrize("raw", [b"", b"{bad", b"\x80", b'["x"]'])
@pytest.mark.asyncio
async def test_read_json_object_bounded_path_keeps_parse_layer_422(raw: bytes):
    parsed, error = await read_json_object(await _request_with_body(raw), max_bytes=8 * 1024)
    assert parsed == {}
    assert error is not None
    assert error.status_code == 422
