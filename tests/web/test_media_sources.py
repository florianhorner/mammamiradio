"""Contract tests for the transient Jamendo configuration boundary."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.core.config import JAMENDO_ACK_REVISION
from mammamiradio.core.models import Segment, SegmentType, StationState
from mammamiradio.web import media_sources, persistence
from mammamiradio.web.persistence import _save_media_source_settings


class _Provider:
    def __init__(self) -> None:
        self.applied: list[dict] = []
        self.retry_started = True
        self.state = "idle"
        self.last_failure_code = ""
        self.apply_error: Exception | None = None
        self.retry_error: Exception | None = None

    async def apply_config(self, **values) -> None:
        self.applied.append(values)
        if self.apply_error is not None:
            raise self.apply_error
        self.state = "idle"
        self.last_failure_code = ""

    def mark_apply_failure(self) -> None:
        self.state = "blocked"
        self.last_failure_code = "api_failed"

    def retry(self) -> bool:
        if self.retry_error is not None:
            raise self.retry_error
        return self.retry_started

    def status(self) -> dict:
        return {
            "state": self.state,
            "ready": False,
            "in_flight": False,
            "last_failure_code": self.last_failure_code,
            "rejected_count": 0,
        }


def _app(*, enabled: bool = False, client_id: str = "", acknowledged: bool = False) -> FastAPI:
    app = FastAPI()
    app.include_router(media_sources.router)
    playlist = SimpleNamespace(
        jamendo_enabled=enabled,
        jamendo_client_id=client_id,
        jamendo_noncommercial_acknowledged=acknowledged,
        jamendo_ack_revision=JAMENDO_ACK_REVISION if acknowledged else "",
    )
    app.state.config = SimpleNamespace(
        playlist=playlist,
        is_addon=False,
        admin_token="",
        admin_password="",
        admin_username="admin",
    )
    app.state.station_state = StationState()
    app.state.queue = asyncio.Queue()
    app.state.jamendo_provider = _Provider()
    return app


def _provider_control_records(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == media_sources.__name__ and record.getMessage().startswith("Jamendo provider control failed")
    ]


def test_disabled_status_cannot_leak_stale_provider_readiness() -> None:
    app = _app(enabled=False, client_id="client_123", acknowledged=True)
    app.state.jamendo_provider.status = lambda: {
        "state": "ready",
        "ready": True,
        "in_flight": True,
    }

    status = media_sources.safe_jamendo_status(app.state.config, app.state.jamendo_provider)

    assert status["state"] == "disabled"
    assert status["ready"] is False
    assert status["in_flight"] is False


@pytest.mark.parametrize(("client_id", "acknowledged"), [("client_123", True), ("", False)])
def test_disabled_status_preserves_currently_playing_terminal_state(client_id, acknowledged) -> None:
    app = _app(enabled=False, client_id=client_id, acknowledged=acknowledged)
    app.state.jamendo_provider.status = lambda: {
        "state": "playing",
        "ready": False,
        "in_flight": False,
    }

    status = media_sources.safe_jamendo_status(app.state.config, app.state.jamendo_provider)

    assert status["enabled"] is False
    assert status["state"] == "playing"
    assert status["ready"] is False
    assert status["in_flight"] is False


def test_safe_status_has_exact_redacted_contract_even_with_hostile_provider_facts() -> None:
    app = _app(enabled=True, client_id="client_123", acknowledged=True)
    app.state.jamendo_provider.status = lambda: {
        "state": "ready",
        "ready": True,
        "in_flight": False,
        "last_success_age_sec": 12.5,
        "last_failure_code": "unexpected-private-detail",
        "rejected_count": 3,
        "rejected_this_attempt": 2,
        # Hostile per-attempt facts: an unknown dominant code must coerce, and a
        # breakdown carrying a private key must not survive the projection.
        "dominant_failure_code_this_attempt": "unexpected-private-detail",
        "attempt_rejections": {
            "identity_mismatch": 2,
            "operation_id": "private-operation",
            "empty_results": -1,
        },
        "client_id": "client_123",
        "stream_url": "https://storage.jamendo.com/file.mp3?token=private",
        "filesystem_path": "/tmp/jamendo/private/normalized.mp3",
        "boot_id": "private-boot",
        "operation_id": "private-operation",
        "lease_id": "private-lease",
    }

    status = media_sources.safe_jamendo_status(app.state.config, app.state.jamendo_provider)

    assert status == {
        "enabled": True,
        "state": "ready",
        "client_id_configured": True,
        "noncommercial_acknowledged": True,
        "terms_scope": "noncommercial_api_use",
        "provider_confirmation": "pending",
        "ready": True,
        "in_flight": False,
        "last_success_age_sec": 12.5,
        "last_failure_code": "api_failed",
        "rejected_count": 3,
        "rejected_this_attempt": 2,
        "dominant_failure_code_this_attempt": "api_failed",
        # Only the known code with a positive count survives: the private key is
        # dropped and the negative count is refused.
        "attempt_rejections": {"identity_mismatch": 2},
    }
    rendered = str(status)
    for private_value in (
        "client_123",
        "token=private",
        "/tmp/jamendo/private",
        "private-boot",
        "private-operation",
        "private-lease",
    ):
        assert private_value not in rendered


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        (None, {}),
        (object(), {}),
        (SimpleNamespace(status=lambda: "not-a-dict"), {}),
    ],
)
def test_provider_status_rejects_missing_or_malformed_status(provider: object | None, expected: dict) -> None:
    assert media_sources._provider_status(provider) == expected


def test_provider_status_contains_provider_exception() -> None:
    def fail() -> dict:
        raise RuntimeError("private provider detail")

    assert media_sources._provider_status(SimpleNamespace(status=fail)) == {}


@pytest.mark.parametrize(
    ("raw_status", "expected_state", "expected_age", "expected_rejected"),
    [
        ({"state": "unknown", "last_success_age_sec": True, "rejected_count": True}, "idle", None, 0),
        ({"state": "ready", "last_success_age_sec": -1, "rejected_count": -1}, "ready", None, 0),
        ({"state": "ready", "last_success_age_sec": "recent", "rejected_count": 1.5}, "ready", None, 0),
    ],
)
def test_safe_status_normalizes_malformed_provider_counters(
    raw_status: dict,
    expected_state: str,
    expected_age: float | None,
    expected_rejected: int,
) -> None:
    app = _app(enabled=True, client_id="client_123", acknowledged=True)
    app.state.jamendo_provider.status = lambda: raw_status

    status = media_sources.safe_jamendo_status(app.state.config, app.state.jamendo_provider)

    assert status["state"] == expected_state
    assert status["last_success_age_sec"] == expected_age
    assert status["rejected_count"] == expected_rejected


def test_safe_status_reports_needs_config_until_ack_revision_and_id_are_current() -> None:
    app = _app(enabled=True, client_id="", acknowledged=False)
    app.state.jamendo_provider.state = "ready"

    status = media_sources.safe_jamendo_status(app.state.config, app.state.jamendo_provider)

    assert status["state"] == "needs_config"
    assert status["ready"] is False
    assert status["in_flight"] is False


@pytest.mark.asyncio
async def test_enable_persists_before_live_apply_and_redacts_client_id(monkeypatch) -> None:
    app = _app()
    events: list[str] = []

    def persist(updates, *, addon) -> None:
        assert updates["JAMENDO_CLIENT_ID"] == "client_123"
        assert addon is False
        events.append("persist")

    async def apply(**values) -> None:
        assert events == ["persist"]
        events.append("apply")
        assert values["client_id"] == "client_123"

    monkeypatch.setattr(media_sources, "_save_media_source_settings", persist)
    app.state.jamendo_provider.apply_config = apply
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/media-sources/jamendo",
            json={
                "enabled": True,
                "noncommercial_acknowledged": True,
                "client_id": "client_123",
            },
        )

    assert response.status_code == 200
    assert events == ["persist", "apply"]
    payload = response.json()
    assert payload["ok"] is True
    assert payload["status"]["client_id_configured"] is True
    assert "client_123" not in response.text


@pytest.mark.asyncio
async def test_apply_provider_config_fails_closed_when_enabled_provider_has_no_apply_hook() -> None:
    enabled = _app(enabled=True, client_id="client_123", acknowledged=True)
    disabled = _app(enabled=False)

    assert await media_sources._apply_provider_config(enabled.state.config, None) is False
    assert await media_sources._apply_provider_config(disabled.state.config, None) is True


@pytest.mark.asyncio
async def test_config_apply_failure_logs_when_provider_hook_is_absent(monkeypatch, caplog) -> None:
    monkeypatch.setattr(media_sources, "_save_media_source_settings", lambda *_args, **_kwargs: None)
    app = _app(enabled=True, client_id="client_123", acknowledged=True)
    app.state.jamendo_provider = None
    caplog.set_level(logging.INFO, logger=media_sources.__name__)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/media-sources/jamendo",
            json={"enabled": True, "noncommercial_acknowledged": True},
        )

    assert response.status_code == 200
    assert response.json()["status"]["state"] == "blocked"
    control_records = _provider_control_records(caplog)
    assert [record.getMessage() for record in control_records] == [
        "Jamendo provider control failed failure_code=config_apply_failed"
    ]
    assert all(record.levelno == logging.WARNING for record in control_records)
    assert "client_123" not in caplog.text


def test_mark_provider_apply_failure_is_optional_and_exception_safe() -> None:
    media_sources._mark_provider_apply_failure(None)
    media_sources._mark_provider_apply_failure(object())

    def fail() -> None:
        raise RuntimeError("private diagnostic detail")

    media_sources._mark_provider_apply_failure(SimpleNamespace(mark_apply_failure=fail))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            {"enabled": True, "noncommercial_acknowledged": True},
            "jamendo_client_id_required",
        ),
        (
            {"enabled": True, "noncommercial_acknowledged": False, "client_id": "client_123"},
            "jamendo_ack_required",
        ),
        (
            {
                "enabled": False,
                "noncommercial_acknowledged": False,
                "client_id": "abc",
                "clear_client_id": True,
            },
            "jamendo_client_id_conflict",
        ),
    ],
)
async def test_locked_validation_errors(body: dict, code: str, monkeypatch) -> None:
    monkeypatch.setattr(media_sources, "_save_media_source_settings", lambda *_args, **_kwargs: None)
    app = _app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put("/api/media-sources/jamendo", json=body)

    assert response.status_code in {409, 422}
    assert response.json()["error_code"] == code
    assert response.json()["stream_status"] == "unaffected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "status_code", "expected"),
    [
        (
            {"enabled": "yes", "noncommercial_acknowledged": True},
            422,
            {
                "ok": False,
                "error_code": "jamendo_invalid_request",
                "message": "Review the Jamendo fields and try again.",
                "retryable": False,
                "next_action": "Use true or false for each switch.",
                "stream_status": "unaffected",
            },
        ),
        (
            {"enabled": False, "noncommercial_acknowledged": False, "client_id": "short"},
            422,
            {
                "ok": False,
                "error_code": "jamendo_client_id_invalid",
                "message": "Enter a valid Jamendo client ID.",
                "field": "client_id",
                "retryable": False,
                "next_action": "Enter the client ID from the Jamendo developer portal.",
                "stream_status": "unaffected",
            },
        ),
        (
            {"enabled": True, "noncommercial_acknowledged": True},
            409,
            {
                "ok": False,
                "error_code": "jamendo_client_id_required",
                "message": "Add a Jamendo client ID before enabling live rotation.",
                "field": "client_id",
                "retryable": False,
                "next_action": "Add a client ID, then enable Jamendo.",
                "stream_status": "unaffected",
            },
        ),
        (
            {"enabled": True, "noncommercial_acknowledged": False, "client_id": "client_123"},
            409,
            {
                "ok": False,
                "error_code": "jamendo_ack_required",
                "message": "Confirm non-commercial API use before enabling Jamendo.",
                "field": "noncommercial_acknowledged",
                "retryable": False,
                "next_action": "Review and confirm the current non-commercial acknowledgement.",
                "stream_status": "unaffected",
            },
        ),
    ],
)
async def test_locked_error_envelopes_are_flat_and_actionable(body, status_code, expected) -> None:
    app = _app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put("/api/media-sources/jamendo", json=body)

    assert response.status_code == status_code
    assert response.json() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "headers"),
    [
        (b"[]", {"content-type": "application/json"}),
        (b'{"enabled":false,"noncommercial_acknowledged":false,"unknown":true}', {"content-type": "application/json"}),
        (b"{not-json", {"content-type": "application/json"}),
    ],
)
async def test_configure_rejects_non_object_unknown_and_malformed_json(content: bytes, headers: dict[str, str]) -> None:
    app = _app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put("/api/media-sources/jamendo", content=content, headers=headers)

    assert response.status_code == 422
    assert response.json()["error_code"] == "jamendo_invalid_request"
    assert response.json()["stream_status"] == "unaffected"


@pytest.mark.asyncio
async def test_media_source_routes_require_admin_access_off_loopback() -> None:
    app = _app()
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        configured = await client.put(
            "/api/media-sources/jamendo",
            json={"enabled": False, "noncommercial_acknowledged": False},
        )
        retried = await client.post("/api/media-sources/jamendo/retry")

    assert configured.status_code == 403
    assert retried.status_code == 403


@pytest.mark.asyncio
async def test_omitted_id_retains_and_explicit_clear_removes(monkeypatch) -> None:
    saved: list[dict[str, str]] = []
    monkeypatch.setattr(
        media_sources,
        "_save_media_source_settings",
        lambda updates, *, addon: saved.append(dict(updates)),
    )
    app = _app(client_id="saved_id", acknowledged=True)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        retained = await client.put(
            "/api/media-sources/jamendo",
            json={"enabled": False, "noncommercial_acknowledged": True},
        )
        cleared = await client.put(
            "/api/media-sources/jamendo",
            json={"enabled": False, "noncommercial_acknowledged": False, "clear_client_id": True},
        )

    assert retained.status_code == 200
    assert "JAMENDO_CLIENT_ID" not in saved[0]
    assert saved[1]["JAMENDO_CLIENT_ID"] == ""
    assert cleared.json()["status"]["client_id_configured"] is False


@pytest.mark.asyncio
async def test_disabling_drops_only_queued_jamendo_segments(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(media_sources, "_save_media_source_settings", lambda *_args, **_kwargs: None)
    app = _app(enabled=True, client_id="client_123", acknowledged=True)
    jamendo = Segment(
        SegmentType.MUSIC,
        tmp_path / "jamendo.mp3",
        metadata={"source_kind": "jamendo", "queue_id": "jamendo-1"},
        ephemeral=False,
    )
    local = Segment(
        SegmentType.MUSIC,
        tmp_path / "local.mp3",
        metadata={"source_kind": "local", "queue_id": "local-1"},
        ephemeral=False,
    )
    app.state.queue.put_nowait(jamendo)
    app.state.queue.put_nowait(local)
    app.state.station_state.queued_segments = [
        {"id": "jamendo-1", "type": "music"},
        {"id": "local-1", "type": "music"},
    ]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/media-sources/jamendo",
            json={"enabled": False, "noncommercial_acknowledged": False},
        )

    assert response.status_code == 200
    assert app.state.queue.qsize() == 1
    assert app.state.queue.get_nowait() is local
    assert app.state.station_state.queued_segments == [{"id": "local-1", "type": "music"}]


@pytest.mark.asyncio
async def test_queue_drop_failure_blocks_live_apply_without_reverting_persisted_intent(monkeypatch, caplog) -> None:
    monkeypatch.setattr(media_sources, "_save_media_source_settings", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        media_sources,
        "_drop_queued_jamendo",
        lambda _request: (_ for _ in ()).throw(RuntimeError("queue internals for client_123")),
    )
    app = _app(enabled=True, client_id="client_123", acknowledged=True)
    caplog.set_level(logging.INFO, logger=media_sources.__name__)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/media-sources/jamendo",
            json={"enabled": False, "noncommercial_acknowledged": False},
        )

    assert response.status_code == 200
    assert response.json()["status"]["state"] == "blocked"
    assert response.json()["status"]["stream_status"] == "unaffected"
    assert app.state.config.playlist.jamendo_enabled is False
    assert app.state.jamendo_apply_failed is True
    control_records = _provider_control_records(caplog)
    assert [record.getMessage() for record in control_records] == [
        "Jamendo provider control failed failure_code=queue_cleanup_failed"
    ]
    assert all(record.levelno == logging.WARNING for record in control_records)
    assert "client_123" not in caplog.text
    assert "queue internals" not in caplog.text


@pytest.mark.asyncio
async def test_save_failure_does_not_apply_live_config(monkeypatch) -> None:
    def fail(*_args, **_kwargs) -> None:
        raise RuntimeError("disk full near client_123")

    monkeypatch.setattr(media_sources, "_save_media_source_settings", fail)
    monkeypatch.setenv("JAMENDO_CLIENT_ID", "original_id")
    monkeypatch.setenv("MAMMAMIRADIO_JAMENDO_ENABLED", "false")
    app = _app(client_id="original_id")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/media-sources/jamendo",
            json={"enabled": True, "noncommercial_acknowledged": True, "client_id": "client_123"},
        )

    assert response.status_code == 500
    assert response.json()["error_code"] == "jamendo_config_save_failed"
    assert app.state.config.playlist.jamendo_enabled is False
    assert app.state.config.playlist.jamendo_client_id == "original_id"
    assert app.state.config.playlist.jamendo_noncommercial_acknowledged is False
    assert app.state.jamendo_provider.applied == []
    assert os.environ["JAMENDO_CLIENT_ID"] == "original_id"
    assert os.environ["MAMMAMIRADIO_JAMENDO_ENABLED"] == "false"
    assert "client_123" not in response.text
    assert "disk full" not in response.text


@pytest.mark.parametrize("addon", [False, True])
def test_media_source_read_failure_preserves_every_existing_secret(tmp_path: Path, addon: bool) -> None:
    """A transient read failure must fail closed, never rebuild a shared secret file."""
    dotenv = tmp_path / ".env"
    addon_secrets = tmp_path / "secrets.env"
    target = addon_secrets if addon else dotenv
    original = "ANTHROPIC_API_KEY=keep-me\nOPENAI_API_KEY=also-keep-me\n"
    target.write_text(original, encoding="utf-8")

    real_read_text = Path.read_text

    def fail_target_read(path: Path, *args, **kwargs) -> str:
        if path == target:
            raise PermissionError("temporary read failure")
        return real_read_text(path, *args, **kwargs)

    with (
        patch.object(Path, "read_text", fail_target_read),
        pytest.raises(PermissionError, match="temporary read failure"),
    ):
        _save_media_source_settings(
            {"JAMENDO_CLIENT_ID": "new-client"},
            addon=addon,
            dotenv_path=dotenv,
            addon_secrets_path=addon_secrets,
        )

    assert target.read_text(encoding="utf-8") == original
    assert not target.with_suffix(target.suffix + ".tmp").exists()


def test_media_source_settings_reject_unknown_persistence_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported media source setting"):
        _save_media_source_settings(
            {"ANTHROPIC_API_KEY": "must-not-be-touched"},
            addon=False,
            dotenv_path=tmp_path / ".env",
        )

    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("addon", [False, True])
def test_media_source_settings_create_one_owner_only_store(tmp_path: Path, addon: bool) -> None:
    dotenv = tmp_path / ".env"
    addon_secrets = tmp_path / "secrets.env"

    _save_media_source_settings(
        {
            "JAMENDO_CLIENT_ID": "client_\n123",
            "MAMMAMIRADIO_JAMENDO_ENABLED": "true",
            "MAMMAMIRADIO_JAMENDO_NONCOMMERCIAL_ACKNOWLEDGED": "true",
            "MAMMAMIRADIO_JAMENDO_ACK_REVISION": JAMENDO_ACK_REVISION,
        },
        addon=addon,
        dotenv_path=dotenv,
        addon_secrets_path=addon_secrets,
    )

    target = addon_secrets if addon else dotenv
    other = dotenv if addon else addon_secrets
    content = target.read_text(encoding="utf-8")
    assert "client_123" in content
    assert "JAMENDO_CLIENT_ID" in content
    assert "MAMMAMIRADIO_JAMENDO_ENABLED" in content
    assert "MAMMAMIRADIO_JAMENDO_NONCOMMERCIAL_ACKNOWLEDGED" in content
    assert "MAMMAMIRADIO_JAMENDO_ACK_REVISION" in content
    assert (target.stat().st_mode & 0o777) == 0o600
    assert not other.exists()


@pytest.mark.parametrize("addon", [False, True])
def test_media_source_settings_retain_then_remove_only_the_exact_client_id(tmp_path: Path, addon: bool) -> None:
    dotenv = tmp_path / ".env"
    addon_secrets = tmp_path / "secrets.env"
    target = addon_secrets if addon else dotenv
    target.write_text(
        "# keep\n"
        "ANTHROPIC_API_KEY=keep-me\n"
        "UNRELATED=value\n"
        "export JAMENDO_CLIENT_ID=old-client\n"
        "MAMMAMIRADIO_JAMENDO_ENABLED=true\n"
        "MAMMAMIRADIO_JAMENDO_NONCOMMERCIAL_ACKNOWLEDGED=true\n",
        encoding="utf-8",
    )

    _save_media_source_settings(
        {"MAMMAMIRADIO_JAMENDO_ENABLED": "false"},
        addon=addon,
        dotenv_path=dotenv,
        addon_secrets_path=addon_secrets,
    )
    retained = target.read_text(encoding="utf-8")
    assert "export JAMENDO_CLIENT_ID=old-client" in retained
    assert "ANTHROPIC_API_KEY=keep-me" in retained
    assert "UNRELATED=value" in retained

    _save_media_source_settings(
        {
            "JAMENDO_CLIENT_ID": "",
            "MAMMAMIRADIO_JAMENDO_ENABLED": "false",
            "MAMMAMIRADIO_JAMENDO_NONCOMMERCIAL_ACKNOWLEDGED": "false",
            "MAMMAMIRADIO_JAMENDO_ACK_REVISION": "",
        },
        addon=addon,
        dotenv_path=dotenv,
        addon_secrets_path=addon_secrets,
    )
    cleared = target.read_text(encoding="utf-8")
    assert "JAMENDO_CLIENT_ID" not in cleared
    assert "MAMMAMIRADIO_JAMENDO_ACK_REVISION" not in cleared
    assert cleared.count("MAMMAMIRADIO_JAMENDO_ENABLED=") == 1
    assert cleared.count("MAMMAMIRADIO_JAMENDO_NONCOMMERCIAL_ACKNOWLEDGED=") == 1
    assert "ANTHROPIC_API_KEY=keep-me" in cleared
    assert "UNRELATED=value" in cleared


def test_media_source_atomic_replace_failure_preserves_authoritative_store(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    original = "ANTHROPIC_API_KEY=keep-me\nJAMENDO_CLIENT_ID=old-client\n"
    dotenv.write_text(original, encoding="utf-8")

    with (
        patch.object(Path, "replace", side_effect=OSError("replace failed")),
        pytest.raises(OSError, match="replace failed"),
    ):
        _save_media_source_settings(
            {"JAMENDO_CLIENT_ID": "new-client"},
            addon=False,
            dotenv_path=dotenv,
        )

    assert dotenv.read_text(encoding="utf-8") == original
    staging = dotenv.with_suffix(dotenv.suffix + ".tmp")
    assert (staging.stat().st_mode & 0o777) == 0o600
    staging.unlink()


def test_owner_only_writer_closes_raw_fd_when_stream_open_fails(tmp_path: Path) -> None:
    target = tmp_path / "secret.env"
    raw_fd = os.open(target, os.O_WRONLY | os.O_CREAT, 0o600)
    real_close = os.close

    with (
        patch.object(persistence.os, "open", return_value=raw_fd),
        patch.object(persistence.os, "fdopen", side_effect=OSError("fdopen failed")),
        patch.object(persistence.os, "close", wraps=real_close) as close,
        pytest.raises(OSError, match="fdopen failed"),
    ):
        persistence._write_owner_only_text(target, "secret")

    close.assert_called_once_with(raw_fd)


def test_addon_option_read_error_fails_closed(monkeypatch) -> None:
    """A failed current-state read must abort the save before any option write.

    Add-on options live in Supervisor's store now, not in a locally rewritten
    ``/data/options.json`` — so the fail-closed shape is: the state read raises,
    the sanitized persistence error surfaces, and no replacement POST fires.
    """
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-secret-token")
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)
    posts: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(200, json={"result": "ok"})
        raise PermissionError("options temporarily unreadable")

    real_client = httpx.Client

    def client_factory(**kwargs):
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(persistence.httpx, "Client", client_factory)

    with pytest.raises(persistence._AddonPersistenceError) as exc_info:
        persistence._save_addon_option_batch({"jamendo_enabled": False})

    assert posts == []
    assert "temporarily unreadable" not in str(exc_info.value)


def test_credential_save_read_error_preserves_secret_store(tmp_path: Path, monkeypatch) -> None:
    """An unreadable secrets.env must fail closed, never be rebuilt from scratch.

    Credential saves touch only ``/config/secrets.env`` now; legacy option-store
    values migrate through the Supervisor toggle path, which carries its own
    fail-closed coverage. This pins the direct save: the read error surfaces as
    the sanitized persistence error and the store stays byte-identical.
    """
    secrets = tmp_path / "secrets.env"
    secrets_original = "OPENAI_API_KEY=keep-me\n"
    secrets.write_text(secrets_original, encoding="utf-8")
    monkeypatch.setattr(persistence, "_ADDON_SECRETS_PATH", secrets)
    real_read_text = Path.read_text

    def fail_target_read(path: Path, *args, **kwargs) -> str:
        if path == secrets:
            raise PermissionError("secrets temporarily unreadable")
        return real_read_text(path, *args, **kwargs)

    with (
        patch.object(Path, "read_text", fail_target_read),
        pytest.raises(persistence._AddonPersistenceError) as exc_info,
    ):
        persistence._save_addon_options({"ANTHROPIC_API_KEY": "new-value"})

    assert "temporarily unreadable" not in str(exc_info.value)
    assert secrets.read_text(encoding="utf-8") == secrets_original
    assert not secrets.with_suffix(secrets.suffix + ".tmp").exists()


@pytest.mark.asyncio
async def test_persisted_intent_survives_live_apply_failure_with_safe_status(monkeypatch, caplog) -> None:
    saved: list[dict[str, str]] = []
    monkeypatch.setattr(
        media_sources,
        "_save_media_source_settings",
        lambda updates, *, addon: saved.append(dict(updates)),
    )
    app = _app()
    app.state.jamendo_provider.apply_error = RuntimeError("live apply exposed client_123")

    def fail_marker() -> None:
        raise RuntimeError("marker exposed client_123")

    app.state.jamendo_provider.mark_apply_failure = fail_marker
    caplog.set_level(logging.INFO, logger=media_sources.__name__)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/media-sources/jamendo",
            json={"enabled": True, "noncommercial_acknowledged": True, "client_id": "client_123"},
        )

    assert response.status_code == 200
    assert set(response.json()) == {"ok", "status"}
    assert response.json()["ok"] is True
    assert response.json()["status"]["state"] == "blocked"
    assert response.json()["status"]["stream_status"] == "unaffected"
    assert response.json()["status"]["ready"] is False
    assert response.json()["status"]["in_flight"] is False
    assert response.json()["status"]["last_failure_code"] == "api_failed"
    assert app.state.config.playlist.jamendo_enabled is True
    assert app.state.config.playlist.jamendo_client_id == "client_123"
    assert app.state.config.playlist.jamendo_noncommercial_acknowledged is True
    assert saved[0]["JAMENDO_CLIENT_ID"] == "client_123"
    assert "client_123" not in response.text
    assert "live apply" not in response.text
    control_records = _provider_control_records(caplog)
    assert [record.getMessage() for record in control_records] == [
        "Jamendo provider control failed failure_code=config_apply_failed"
    ]
    assert all(record.levelno == logging.WARNING for record in control_records)
    assert "client_123" not in caplog.text
    assert "live apply" not in caplog.text
    assert "marker exposed" not in caplog.text


@pytest.mark.asyncio
async def test_retry_is_disabled_or_coalesced() -> None:
    disabled = _app()
    enabled = _app(enabled=True, client_id="client_123", acknowledged=True)
    enabled.state.jamendo_provider.retry_started = False
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=disabled, client=("127.0.0.1", 1234)),
        base_url="http://test",
    ) as client:
        disabled_response = await client.post("/api/media-sources/jamendo/retry")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=enabled, client=("127.0.0.1", 1234)),
        base_url="http://test",
    ) as client:
        enabled_response = await client.post("/api/media-sources/jamendo/retry")

    assert disabled_response.status_code == 409
    assert disabled_response.json() == {
        "ok": False,
        "error_code": "jamendo_retry_disabled",
        "message": "Jamendo is off. Enable it before checking again.",
        "retryable": False,
        "next_action": "Enable Jamendo, then try again.",
        "stream_status": "unaffected",
    }
    assert enabled_response.status_code == 202
    assert enabled_response.json()["started"] is False
    assert enabled_response.json()["already_in_progress"] is True


@pytest.mark.asyncio
async def test_retry_converges_after_live_apply_failure_without_leaking_secret(monkeypatch) -> None:
    monkeypatch.setattr(media_sources, "_save_media_source_settings", lambda *_args, **_kwargs: None)
    app = _app()
    app.state.jamendo_provider.apply_error = RuntimeError("secret client_123")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        saved = await client.put(
            "/api/media-sources/jamendo",
            json={"enabled": True, "noncommercial_acknowledged": True, "client_id": "client_123"},
        )
        app.state.jamendo_provider.apply_error = None
        retried = await client.post("/api/media-sources/jamendo/retry")

    assert saved.status_code == 200
    assert saved.json()["status"]["state"] == "blocked"
    assert retried.status_code == 202
    assert retried.json()["started"] is True
    assert retried.json()["already_in_progress"] is False
    assert retried.json()["status"]["state"] == "idle"
    assert "client_123" not in retried.text


@pytest.mark.asyncio
async def test_retry_reports_blocked_when_reapplying_persisted_config_fails(caplog) -> None:
    app = _app(enabled=True, client_id="client_123", acknowledged=True)
    app.state.jamendo_apply_failed = True
    app.state.jamendo_provider.apply_error = RuntimeError("private client_123")
    caplog.set_level(logging.INFO, logger=media_sources.__name__)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/media-sources/jamendo/retry")

    assert response.status_code == 202
    assert response.json()["started"] is False
    assert response.json()["status"]["state"] == "blocked"
    assert app.state.jamendo_apply_failed is True
    assert "client_123" not in response.text
    control_records = _provider_control_records(caplog)
    assert [record.getMessage() for record in control_records] == [
        "Jamendo provider control failed failure_code=config_apply_failed"
    ]
    assert all(record.levelno == logging.WARNING for record in control_records)
    assert "client_123" not in caplog.text


@pytest.mark.asyncio
async def test_retry_reports_blocked_when_provider_has_no_retry_hook(caplog) -> None:
    app = _app(enabled=True, client_id="client_123", acknowledged=True)
    app.state.jamendo_provider = SimpleNamespace(status=lambda: {"state": "idle"})
    caplog.set_level(logging.INFO, logger=media_sources.__name__)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/media-sources/jamendo/retry")

    assert response.status_code == 202
    assert response.json()["started"] is False
    assert response.json()["status"]["state"] == "blocked"
    assert app.state.jamendo_apply_failed is True
    control_records = _provider_control_records(caplog)
    assert [record.getMessage() for record in control_records] == [
        "Jamendo provider control failed failure_code=retry_failed"
    ]
    assert all(record.levelno == logging.WARNING for record in control_records)


@pytest.mark.asyncio
async def test_retry_failure_stays_safe_and_does_not_leak_exception(caplog) -> None:
    app = _app(enabled=True, client_id="client_123", acknowledged=True)
    app.state.jamendo_provider.retry_error = RuntimeError("retry failed for client_123")
    caplog.set_level(logging.INFO, logger=media_sources.__name__)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/media-sources/jamendo/retry")

    assert response.status_code == 202
    assert response.json()["started"] is False
    assert response.json()["already_in_progress"] is False
    assert response.json()["status"]["state"] == "blocked"
    assert response.json()["status"]["stream_status"] == "unaffected"
    assert "client_123" not in response.text
    assert "retry failed" not in response.text
    control_records = _provider_control_records(caplog)
    assert [record.getMessage() for record in control_records] == [
        "Jamendo provider control failed failure_code=retry_failed"
    ]
    assert all(record.levelno == logging.WARNING for record in control_records)
    assert "client_123" not in caplog.text
    assert "retry failed for" not in caplog.text
