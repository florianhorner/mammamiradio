"""Authenticated configuration surface for optional transient music sources."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mammamiradio.core.config import JAMENDO_ACK_REVISION
from mammamiradio.core.models import GenerationWasteReason
from mammamiradio.playlist.jamendo_transient import JAMENDO_FAILURE_CODES
from mammamiradio.scheduling.queue_mutations import drop_matching_segments
from mammamiradio.web.auth import require_admin_access
from mammamiradio.web.json_body import read_json_object
from mammamiradio.web.persistence import _save_media_source_settings

router = APIRouter()
logger = logging.getLogger(__name__)

_JAMENDO_STATES = frozenset(
    {
        "disabled",
        "needs_config",
        "idle",
        "discovering",
        "fetching",
        "normalizing",
        "ready",
        "queued",
        "playing",
        "consumed",
        "degraded",
        "blocked",
    }
)
# Derived from the provider's own code set rather than hand-maintained. This
# list previously omitted metadata_changed / identity_mismatch / empty_results,
# and the coercion below turned each of them into "api_failed" — which is why
# the operator card showed a generic reason for the failures that actually fire.
_JAMENDO_FAILURE_CODES = frozenset({""}) | JAMENDO_FAILURE_CODES
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def _media_error(
    status_code: int,
    error_code: str,
    message: str,
    *,
    field: str | None = None,
    retryable: bool,
    next_action: str,
) -> JSONResponse:
    body: dict[str, Any] = {
        "ok": False,
        "error_code": error_code,
        "message": message,
        "retryable": retryable,
        "next_action": next_action,
        "stream_status": "unaffected",
    }
    if field:
        body["field"] = field
    return JSONResponse(body, status_code=status_code)


def _provider_status(provider: object | None) -> dict[str, Any]:
    if provider is None:
        return {}
    status_fn = getattr(provider, "status", None)
    if not callable(status_fn):
        return {}
    try:
        value = status_fn()
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def safe_jamendo_status(config: object, provider: object | None = None) -> dict[str, Any]:
    """Return the one redacted status object shared by APIs and admin polling."""
    playlist = config.playlist  # type: ignore[attr-defined]
    enabled = bool(getattr(playlist, "jamendo_enabled", False))
    client_configured = bool(str(getattr(playlist, "jamendo_client_id", "") or "").strip())
    acknowledged = bool(getattr(playlist, "jamendo_noncommercial_acknowledged", False)) and (
        getattr(playlist, "jamendo_ack_revision", "") == JAMENDO_ACK_REVISION
    )
    raw = _provider_status(provider)
    state = str(raw.get("state") or "")
    if state == "playing":
        # Revoking enablement, acknowledgement, or the saved client ID releases
        # every not-yet-playing lease, but the one segment already on air may
        # finish. Preserve that terminal truth until its release callback runs.
        pass
    elif not enabled:
        state = "disabled"
    elif not client_configured or not acknowledged:
        state = "needs_config"
    elif state not in _JAMENDO_STATES:
        state = "idle"
    failure = str(raw.get("last_failure_code") or "")
    if failure not in _JAMENDO_FAILURE_CODES:
        failure = "api_failed" if failure else ""
    age = raw.get("last_success_age_sec")
    if isinstance(age, bool) or not isinstance(age, int | float) or age < 0:
        age = None
    rejected = raw.get("rejected_count", 0)
    if isinstance(rejected, bool) or not isinstance(rejected, int) or rejected < 0:
        rejected = 0
    dominant = str(raw.get("dominant_failure_code_this_attempt") or "")
    if dominant not in _JAMENDO_FAILURE_CODES:
        dominant = "api_failed" if dominant else ""
    raw_breakdown = raw.get("attempt_rejections")
    breakdown: dict[str, int] = {}
    if isinstance(raw_breakdown, Mapping):
        for code, count in raw_breakdown.items():
            if (
                code
                and code in _JAMENDO_FAILURE_CODES
                and not isinstance(count, bool)
                and isinstance(count, int)
                and count > 0
            ):
                breakdown[code] = count
    # Derived from the surviving rows rather than taken raw, so the count and the
    # breakdown can never disagree. A raw total that still included a code the
    # filter dropped would leave the card doing arithmetic on two different
    # populations.
    attempt_rejected = sum(breakdown.values())
    return {
        "enabled": enabled,
        "state": state,
        "client_id_configured": client_configured,
        "noncommercial_acknowledged": acknowledged,
        "terms_scope": "noncommercial_api_use",
        # Always "pending" on purpose — Jamendo never clears a station model.
        # Not the operator's acknowledgement (that is the field above).
        "provider_confirmation": "pending",
        "ready": enabled and client_configured and acknowledged and bool(raw.get("ready", state == "ready")),
        "in_flight": enabled
        and client_configured
        and acknowledged
        and bool(raw.get("in_flight", state in {"discovering", "fetching", "normalizing"})),
        "last_success_age_sec": age,
        "last_failure_code": failure or None,
        "rejected_count": rejected,
        "rejected_this_attempt": attempt_rejected,
        "dominant_failure_code_this_attempt": dominant or None,
        "attempt_rejections": breakdown,
    }


async def _read_object(request: Request) -> dict[str, Any] | None:
    value, error = await read_json_object(request)
    return None if error is not None else value


def _drop_queued_jamendo(request: Request) -> int:
    state = request.app.state.station_state
    queue = request.app.state.queue
    return drop_matching_segments(
        queue,
        state,
        should_drop=lambda segment: (
            isinstance(segment.metadata, dict)
            and (segment.metadata.get("source_kind") == "jamendo" or segment.metadata.get("audio_source") == "jamendo")
        ),
        reason=GenerationWasteReason.SOURCE_SWITCH,
    )


def _jamendo_operation_lock(request: Request) -> asyncio.Lock:
    """Serialize config saves and retries so an older request cannot win late."""
    lock = getattr(request.app.state, "jamendo_operation_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.jamendo_operation_lock = lock
    return lock


def _provider_apply_values(config: object) -> dict[str, Any]:
    playlist = config.playlist  # type: ignore[attr-defined]
    return {
        "enabled": bool(playlist.jamendo_enabled),
        "client_id": str(playlist.jamendo_client_id or ""),
        "noncommercial_acknowledged": bool(playlist.jamendo_noncommercial_acknowledged),
        "tags": str(getattr(playlist, "jamendo_tags", "pop")),
        "country": str(getattr(playlist, "jamendo_country", "")),
        "order": str(getattr(playlist, "jamendo_order", "popularity_week")),
        "limit": int(getattr(playlist, "jamendo_limit", 20)),
    }


async def _apply_provider_config(config: object, provider: object | None) -> bool:
    apply_config = getattr(provider, "apply_config", None)
    if not callable(apply_config):
        return not bool(config.playlist.jamendo_enabled)  # type: ignore[attr-defined]
    try:
        await apply_config(**_provider_apply_values(config))
    except Exception:
        return False
    return True


def _mark_provider_apply_failure(provider: object | None) -> None:
    mark_failure = getattr(provider, "mark_apply_failure", None)
    if not callable(mark_failure):
        return
    try:
        mark_failure()
    except Exception:
        # The response-level safe status still fails closed.  Never let a
        # diagnostic hook turn an already-persisted operator choice into a 500.
        pass


def _log_provider_control_failure(code: str) -> None:
    """Log the stable failure code without exception text."""
    logger.warning("Jamendo provider control failed failure_code=%s", code)


def _apply_failure_status(config: object, provider: object | None) -> dict[str, Any]:
    status = safe_jamendo_status(config, provider)
    status.update(
        {
            "state": "blocked",
            "ready": False,
            "in_flight": False,
            "last_failure_code": "api_failed",
            "stream_status": "unaffected",
        }
    )
    return status


async def _persist_and_apply_jamendo(
    request: Request,
    *,
    enabled: bool,
    acknowledged: bool,
    clear_id: bool,
    supplied: bool,
    raw_client_id: object,
) -> dict[str, Any] | JSONResponse:
    async with _jamendo_operation_lock(request):
        config = request.app.state.config
        current_id = str(config.playlist.jamendo_client_id or "").strip()
        effective_id = "" if clear_id else (raw_client_id.strip() if isinstance(raw_client_id, str) else current_id)
        if enabled and not effective_id:
            return _media_error(
                409,
                "jamendo_client_id_required",
                "Add a Jamendo client ID before enabling live rotation.",
                field="client_id",
                retryable=False,
                next_action="Add a client ID, then enable Jamendo.",
            )
        if enabled and not acknowledged:
            return _media_error(
                409,
                "jamendo_ack_required",
                "Confirm non-commercial API use before enabling Jamendo.",
                field="noncommercial_acknowledged",
                retryable=False,
                next_action="Review and confirm the current non-commercial acknowledgement.",
            )

        updates = {
            "MAMMAMIRADIO_JAMENDO_ENABLED": "true" if enabled else "false",
            "MAMMAMIRADIO_JAMENDO_NONCOMMERCIAL_ACKNOWLEDGED": "true" if acknowledged else "false",
            "MAMMAMIRADIO_JAMENDO_ACK_REVISION": JAMENDO_ACK_REVISION if acknowledged else "",
        }
        if supplied or clear_id:
            updates["JAMENDO_CLIENT_ID"] = effective_id

        try:
            await asyncio.to_thread(_save_media_source_settings, updates, addon=bool(config.is_addon))
        except Exception:
            return _media_error(
                500,
                "jamendo_config_save_failed",
                "Jamendo settings were not saved. Starter and local music continue. "
                "Check add-on storage and try again.",
                retryable=True,
                next_action="Check storage permissions, then save again.",
            )

        prior_id = current_id
        prior_enabled = bool(config.playlist.jamendo_enabled)
        prior_ack = bool(config.playlist.jamendo_noncommercial_acknowledged)
        for key, value in updates.items():
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        config.playlist.jamendo_client_id = effective_id
        config.playlist.jamendo_enabled = enabled
        config.playlist.jamendo_noncommercial_acknowledged = acknowledged
        config.playlist.jamendo_ack_revision = JAMENDO_ACK_REVISION if acknowledged else ""

        live_apply_failed = False
        if prior_enabled and (not enabled or prior_id != effective_id or prior_ack != acknowledged):
            try:
                _drop_queued_jamendo(request)
            except Exception:
                live_apply_failed = True
                _log_provider_control_failure("queue_cleanup_failed")
        provider = getattr(request.app.state, "jamendo_provider", None)
        if not await _apply_provider_config(config, provider):
            live_apply_failed = True
            _log_provider_control_failure("config_apply_failed")

        request.app.state.jamendo_apply_failed = live_apply_failed
        if live_apply_failed:
            _mark_provider_apply_failure(provider)
            return {"ok": True, "status": _apply_failure_status(config, provider)}
        return {"ok": True, "status": safe_jamendo_status(config, provider)}


@router.put("/api/media-sources/jamendo")
async def configure_jamendo(request: Request, _: None = Depends(require_admin_access)):
    body = await _read_object(request)
    if body is None or set(body) - {
        "enabled",
        "noncommercial_acknowledged",
        "client_id",
        "clear_client_id",
    }:
        return _media_error(
            422,
            "jamendo_invalid_request",
            "Review the Jamendo fields and try again.",
            retryable=False,
            next_action="Review the fields and save again.",
        )
    enabled = body.get("enabled")
    acknowledged = body.get("noncommercial_acknowledged")
    clear_id = body.get("clear_client_id", False)
    if not isinstance(enabled, bool) or not isinstance(acknowledged, bool) or not isinstance(clear_id, bool):
        return _media_error(
            422,
            "jamendo_invalid_request",
            "Review the Jamendo fields and try again.",
            retryable=False,
            next_action="Use true or false for each switch.",
        )
    supplied = "client_id" in body
    raw_client_id = body.get("client_id")
    if supplied and clear_id:
        return _media_error(
            422,
            "jamendo_client_id_conflict",
            "Replace or clear the client ID, not both.",
            field="client_id",
            retryable=False,
            next_action="Choose Replace or Clear, then save again.",
        )
    if supplied and (not isinstance(raw_client_id, str) or not _CLIENT_ID_RE.fullmatch(raw_client_id.strip())):
        return _media_error(
            422,
            "jamendo_client_id_invalid",
            "Enter a valid Jamendo client ID.",
            field="client_id",
            retryable=False,
            next_action="Enter the client ID from the Jamendo developer portal.",
        )

    return await _persist_and_apply_jamendo(
        request,
        enabled=enabled,
        acknowledged=acknowledged,
        clear_id=clear_id,
        supplied=supplied,
        raw_client_id=raw_client_id,
    )


@router.post("/api/media-sources/jamendo/retry", status_code=202)
async def retry_jamendo(request: Request, _: None = Depends(require_admin_access)):
    async with _jamendo_operation_lock(request):
        config = request.app.state.config
        provider = getattr(request.app.state, "jamendo_provider", None)
        if not config.playlist.jamendo_enabled:
            return _media_error(
                409,
                "jamendo_retry_disabled",
                "Jamendo is off. Enable it before checking again.",
                retryable=False,
                next_action="Enable Jamendo, then try again.",
            )

        if bool(getattr(request.app.state, "jamendo_apply_failed", False)):
            if not await _apply_provider_config(config, provider):
                _log_provider_control_failure("config_apply_failed")
                _mark_provider_apply_failure(provider)
                return {
                    "started": False,
                    "already_in_progress": False,
                    "status": _apply_failure_status(config, provider),
                }
            request.app.state.jamendo_apply_failed = False
            return {
                "started": True,
                "already_in_progress": False,
                "status": safe_jamendo_status(config, provider),
            }

        retry = getattr(provider, "retry", None)
        if not callable(retry):
            _log_provider_control_failure("retry_failed")
            _mark_provider_apply_failure(provider)
            request.app.state.jamendo_apply_failed = True
            return {
                "started": False,
                "already_in_progress": False,
                "status": _apply_failure_status(config, provider),
            }
        try:
            started = bool(retry())
        except Exception:
            _log_provider_control_failure("retry_failed")
            _mark_provider_apply_failure(provider)
            request.app.state.jamendo_apply_failed = True
            return {
                "started": False,
                "already_in_progress": False,
                "status": _apply_failure_status(config, provider),
            }
        return {
            "started": started,
            "already_in_progress": not started,
            "status": safe_jamendo_status(config, provider),
        }
