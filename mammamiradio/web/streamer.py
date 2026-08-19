"""Live streaming transport, HTTP routes, and admin controls."""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import copy
import functools
import hashlib
import importlib
import ipaddress
import logging
import math
import os
import random as _random
import re as _re
import secrets
import shutil
import stat as _stat
import time
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from mammamiradio.audio.norm_cache import (
    is_recent_music as _is_recent_music,
)
from mammamiradio.audio.norm_cache import (
    recent_music_identity_keys as _recent_music_identity_keys,
)
from mammamiradio.audio.norm_cache import (
    record_rescue_airplay as _record_rescue_airplay,
)
from mammamiradio.audio.norm_cache import (
    rescue_last_heard_at as _rescue_last_heard_at,
)
from mammamiradio.audio.norm_cache import (
    rescue_on_cooldown as _rescue_on_cooldown,
)
from mammamiradio.audio.norm_cache import (
    rescue_rotation_status as _rescue_rotation_status,
)
from mammamiradio.audio.norm_cache import (
    select_norm_cache_rescue as _select_norm_cache_rescue,
)
from mammamiradio.audio.norm_cache import (
    sidecar_track_key as _sidecar_track_key,
)
from mammamiradio.audio.normalizer import (
    configure_broadcast_chain,
    humanize_norm_filename,
    load_track_metadata,
    norm_cache_duration_sec,
    probe_duration_sec,
)
from mammamiradio.audio.stream_format import stream_audio_metadata
from mammamiradio.core.capabilities import capabilities_to_dict, get_capabilities
from mammamiradio.core.config import (
    DEFAULT_STATION_NAME,
    MODEL_REGISTRY_FILENAME,
    PACING_BOUNDS,
    ModelsSection,
    load_model_registry,
)
from mammamiradio.core.first_listen import (
    FirstListenAttemptMismatchError,
    FirstListenInstallOriginStatus,
    FirstListenReceiptStore,
    FirstListenReceiptUnavailableError,
)
from mammamiradio.core.first_listen_show import (
    approved_first_listen_show_path,
    first_listen_show_required,
    iter_first_listen_show_chunks,
)
from mammamiradio.core.listener_session import ListenerSessionCueState
from mammamiradio.core.models import (
    SEGMENT_PLAYLIST_SOURCE_KIND_KEY,
    STREAM_LATE_THRESHOLD_SECONDS,
    STREAM_TARGET_LEAD_SECONDS,
    ChaosSubtype,
    GenerationWasteReason,
    Heading,
    MediaAttribution,
    PartyMode,
    PersonalityAxes,
    PlaylistSource,
    Segment,
    SegmentType,
    StationState,
    Track,
    safe_media_attribution_dict,
    segment_track_key,
)
from mammamiradio.core.packaged_assets import DEMO_ASSETS_DIR as _DEMO_ASSETS_DIR
from mammamiradio.core.packaged_assets import is_packaged_asset
from mammamiradio.core.provider_checks import check_provider_keys
from mammamiradio.core.setup_status import (
    addon_options_snippet,
    build_setup_status,
    classify_station_mode,
)
from mammamiradio.core.spoken_assets import is_approved_packaged_audio_asset, is_approved_spoken_asset
from mammamiradio.home.authorization import HomeAuthorization
from mammamiradio.home.catalog import (
    drain_invalidated_label_generation,
    generation_in_progress,
    invalidate_label_generation,
    schedule_label_generation,
)
from mammamiradio.home.context_director import HomeContextDirector
from mammamiradio.home.context_value import (
    LOW_VALUE_AMBIENT_ENTITY_IDS,
    classify_home_context_entity_ids,
)
from mammamiradio.home.entity_policy import (
    load_entity_policy,
    personal_moment_opt_in_entity_ids,
    policy_revision,
    set_entity_muted,
    set_personal_moment_enabled,
    valid_entity_id,
)
from mammamiradio.home.ha_context import (
    PRESENCE_SENSOR_DEVICE_CLASSES,
    fetch_home_context_preview,
    get_cached_home_context,
    invalidate_all_home_context,
    invalidate_home_context_entity_baselines,
    push_state_to_ha,
)
from mammamiradio.home.ha_enrichment import EVENT_RETENTION_SECONDS
from mammamiradio.home.ha_playback import (
    HAPlaybackError,
    HAPlaybackReason,
    HAPlaybackService,
)
from mammamiradio.home.scene_namer import reset_scene_namer_cache
from mammamiradio.hosts.memory_extractor import (
    drain_revoked_home_memory_extractions,
    revoke_home_memory_extractions,
)
from mammamiradio.hosts.station_name_guard import strip_foreign_station_name
from mammamiradio.playlist.blocklist import block_meta, save_blocklist
from mammamiradio.playlist.direction import (
    DirectionTarget,
    expand_direction,
    find_existing_direction_tracks,
    normalize_direction_text,
    resolve_direction_search_results,
)
from mammamiradio.playlist.downloader import external_media_enabled
from mammamiradio.playlist.music_admission import (
    YOUTUBE_ADMISSION_SEARCH_DEPTH,
    classify_youtube_candidate,
    is_youtube_music_candidate,
)
from mammamiradio.playlist.playlist import (
    PERSISTED_HEADING_FILENAME,
    PERSISTED_SOURCE_FILENAME,
    ExplicitSourceError,
    ExternalMediaUnavailableError,
    LegacyJamendoSourceRetiredError,
    filter_blocklisted,
    load_explicit_source,
    normalized_track_key,
    write_persisted_heading,
    write_persisted_source,
)
from mammamiradio.playlist.preferences import clear_preference, preference_score, save_preferences, set_preference
from mammamiradio.scheduling.handoff import (
    cancel_active_music_handoff,
    finish_handoff_segment,
    handoff_original_source,
    handoff_original_sources,
    has_cancellable_music_handoff,
    is_protected_handoff_successor,
    mark_handoff_segment_selected,
    reconcile_handoff_queue_items,
)
from mammamiradio.scheduling.queue_mutations import drop_matching_segments
from mammamiradio.scheduling.scheduler import buffered_audio_seconds
from mammamiradio.web.assets import (
    _ASSET_VERSION,
    _STATIC_DIR,
    _TEMPLATES_DIR,
    _bust_static_cache,
)
from mammamiradio.web.auth import (  # noqa: F401  facade re-export — routes/tests read these as streamer.*; only some are used in-module
    _CSRF_TOKEN_PLACEHOLDER,
    _HASSIO_NETWORK,
    _MUTATING_METHODS,
    _TRUSTED_NETWORKS,
    _enforce_csrf_for_basic_auth,
    _enforce_csrf_for_private_network,
    _get_csrf_token,
    _inject_csrf_token,
    _is_hassio_or_loopback,
    _is_loopback_client,
    _is_private_network,
    _same_origin,
    require_admin_access,
    security,
)
from mammamiradio.web.json_body import read_json_object
from mammamiradio.web.media_sources import _media_error, safe_jamendo_status
from mammamiradio.web.mp3_frames import _skip_id3_and_xing_header
from mammamiradio.web.pages import _get_injected_html, _sanitize_ingress_prefix
from mammamiradio.web.persistence import (
    _CREDENTIAL_ENV_TO_FIELD,
    _CREDENTIAL_FIELDS,
    _SECRET_WRITE_DURABLE,
    _AddonPersistenceError,
    _apply_live_credentials,
    _sanitize_credential_value,
    _save_addon_option,
    _save_addon_option_batch,
    _save_addon_options,
    _save_dotenv,
)
from mammamiradio.web.provider_verdict import (
    _record_provider_verdict,
    _run_provider_verdict,
)
from mammamiradio.web.status_payload import (  # noqa: F401  facade re-export — routes/tests read these as streamer.*; only some are used in-module
    _admin_track_id,
    _cached_cache_size_mb,
    _duration_sec_from_payload,
    _golden_path_status,
    _ha_details_payload,
    _has_any_mp3,
    _heading_playlist_track_count,
    _page_bounds,
    _paginated_tracks,
    _public_now_streaming_payload,
    _public_segment_metadata,
    _public_starter_catalog,
    _serialize_brand,
    _serialize_heading,
    _serialize_identity,
    _serialize_source,
    _serialize_stream_log_entry,
    _serialize_track,
    _status_now_playback,
)
from mammamiradio.web.ui_copy import copy_strings

logger = logging.getLogger(__name__)
_LONGFORM_NOTICE_REASON = "longform_audio"
_ADMIN_PLAYLIST_LOCK_TIMEOUT_SECONDS = 0.1

# Bounded pool for the admin /api/search yt-dlp lookup. asyncio.wait_for cancels
# the awaiting future on timeout but cannot kill the underlying thread (it runs
# until its socket timeout), so an abandoned search must not accumulate in the
# default executor and starve the producer's audio prefetch on Pi-class hardware.
# Sized above realistic admin search concurrency (typically 1) so a timed-out
# thread holding its slot for the socket-timeout window can't head-of-line-block
# the operator's next search, while staying well under the default pool.
_search_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="admin-search")
atexit.register(_search_executor.shutdown, wait=False, cancel_futures=True)

# Dedicated pool for the direction target metadata fan-out (up to
# MAX_DIRECTION_TARGETS searches at once). Isolated from _search_executor so a
# direction that fans out many searches can't head-of-line-block the operator's
# interactive /api/search on the shared pool.
_direction_search_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="direction-search")
atexit.register(_direction_search_executor.shutdown, wait=False, cancel_futures=True)

# Supervisor option writes are serialized by persistence._ADDON_OPTIONS_LOCK and
# may hold that lock across slow network I/O. Keep both the active request and
# any callers queued behind it out of asyncio's default executor, which also
# carries latency-sensitive audio normalization and TTS work.
_addon_persistence_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="addon-persistence",
)
atexit.register(_addon_persistence_executor.shutdown, wait=False, cancel_futures=True)


async def _run_addon_persistence(operation: Callable[..., Any], /, *args: Any) -> Any:
    """Run one blocking HA add-on persistence operation on its isolated worker."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _addon_persistence_executor,
        functools.partial(operation, *args),
    )


router = APIRouter()

_FIRST_LISTEN_HELP_URL = (
    "https://github.com/florianhorner/mammamiradio/blob/main/docs/integrations/ha-integration.md#first-listen-repair"
)
_HOME_PREVIEW_PROOF_TTL_SECONDS = 5 * 60.0
_HOME_PREVIEW_TOTAL_TIMEOUT_SECONDS = 16.0


@dataclass(frozen=True)
class _HomeContextPreviewProof:
    """Short-lived in-memory evidence for one explicit preview action."""

    expires_at: float
    config_fingerprint: str
    authorization_mode: str
    policy_revision: int
    context_generation: int


_SETUP_ERRORS: dict[str, tuple[str, str, bool, str, int]] = {
    "ha_access_missing": (
        "Home Assistant access is missing",
        "Check the add-on's Home Assistant access, then try again.",
        True,
        "Check access",
        409,
    ),
    "ha_auth_failed": (
        "Home Assistant did not accept access",
        "Reload the add-on so it receives a fresh Supervisor token, then retry.",
        True,
        "Retry connection",
        502,
    ),
    "ha_unreachable": (
        "Home Assistant is not reachable yet",
        "Give Home Assistant a moment, then try finding speakers again.",
        True,
        "Try again",
        503,
    ),
    "media_source_missing": (
        "Mamma Mi Radio is not in HA's media browser",
        "Install or reload the Mamma Mi Radio HACS integration, then retry.",
        True,
        "Check HACS integration",
        409,
    ),
    "ambiguous_station": (
        "Home Assistant found more than one station",
        "Keep one Mamma Mi Radio integration entry, then find speakers again.",
        False,
        "Review HA entries",
        409,
    ),
    "no_players": (
        "No compatible-looking speaker is available",
        "Turn on a Home Assistant media player, wait a moment, then search again.",
        True,
        "Find speakers again",
        404,
    ),
    "player_unavailable": (
        "That speaker is not available now",
        "Choose another speaker or bring this one online, then retry.",
        True,
        "Choose a speaker",
        409,
    ),
    "service_rejected": (
        "Home Assistant could not start playback",
        "Check the speaker's mute and volume in Home Assistant, then try once more.",
        True,
        "Try playback again",
        502,
    ),
    "request_in_flight": (
        "A playback request is already on its way",
        "Wait a few seconds for Home Assistant to answer before trying again.",
        True,
        "Wait a moment",
        409,
    ),
    "receipt_unavailable": (
        "Playback was accepted, but setup progress was not saved",
        "Check that the station data directory is writable, then save this listening check without replaying it.",
        True,
        "Save listening check",
        503,
    ),
    "receipt_recovery_missing": (
        "That unsaved listening check is no longer available",
        "Nothing was replayed. Refresh Setup, then explicitly start the selected speaker once more.",
        True,
        "Start once more",
        409,
    ),
    "attempt_mismatch": (
        "This confirmation belongs to an older playback attempt",
        "Start playback on the selected speaker again, then answer the listening check.",
        True,
        "Start playback again",
        409,
    ),
    "preview_unavailable": (
        "The privacy preview is not ready",
        "No Home context was enabled. Try the preview again, or keep it off.",
        True,
        "Preview again",
        503,
    ),
    "preview_required": (
        "Review a fresh preview before enabling",
        "Open the filtered preview first. It stays local and is not promoted into host context.",
        True,
        "Show filtered preview",
        409,
    ),
    "first_listen_required": (
        "Finish the speaker check first",
        "Confirm that you heard Mamma Mi Radio on a Home Assistant speaker before reviewing Home context.",
        True,
        "Return to listening check",
        409,
    ),
    "privacy_persist_failed": (
        "The privacy choice was not saved for restart",
        "Home context remains off in this running station. Check data-directory permissions, then save again.",
        True,
        "Save again",
        503,
    ),
    "privacy_receipt_unavailable": (
        "The privacy choice is active, but setup progress was not saved",
        "Your choice still controls the running station. Check the data directory, then save it once more.",
        True,
        "Save review again",
        503,
    ),
    "invalid_request": (
        "That setup request is not valid",
        "Refresh Setup and try the action again.",
        False,
        "Refresh setup",
        422,
    ),
}

# Standalone overrides. Same failure, same code, a way out that exists on this
# install. A station run outside the Home Assistant add-on has no add-on options
# page and no Supervisor token to refresh, so the default wording sends the
# operator to a screen they do not have. Only codes whose default instruction is
# add-on-shaped belong here; everything else falls through to _SETUP_ERRORS.
_SETUP_ERRORS_STANDALONE: dict[str, tuple[str, str, bool, str, int]] = {
    "ha_access_missing": (
        "This station is not connected to Home Assistant",
        "Set HA_URL and HA_TOKEN in your .env and restart the station. "
        "The radio plays without a home connection, so you can also skip this step.",
        True,
        "Retry connection",
        409,
    ),
    "ha_auth_failed": (
        "Home Assistant did not accept access",
        "Check that HA_TOKEN is a current long-lived access token, then restart the station and retry.",
        True,
        "Retry connection",
        502,
    ),
    "media_source_missing": (
        "Mamma Mi Radio is not in HA's media browser",
        "Install the Mamma Mi Radio HACS integration in Home Assistant and point it at this station's URL, then retry.",
        True,
        "Check HACS integration",
        409,
    ),
}

# TODO: split — this god module is a postal address, not a destination.
# See docs/archive/2026-04-28-cathedral-restructure.md (PR 5) for the routes/playback split plan.
# Path roots, the static-asset content hash (_ASSET_VERSION), and
# _bust_static_cache now live in web/assets.py; admin auth (require_admin_access,
# CSRF, trusted networks) now lives in web/auth.py — both imported above.
#
# Jinja2 templates for brand-engine listener page (PR-C). Admin still uses
# string-replace via _inject_ingress_prefix (web/pages.py); only listener migrates to Jinja for now.
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# Admin page still loaded as a raw string + post-render prefix injection.
# Listener no longer needs _LISTENER_HTML — it's rendered from template per-request.
_LISTENER_HTML = _bust_static_cache((_TEMPLATES_DIR / "listener.html").read_text())  # kept for tests + fallback

_ADMIN_HTML = _bust_static_cache((_TEMPLATES_DIR / "admin.html").read_text())


def _as_int_index(value, default: int = -1) -> int:
    """Best-effort parse for playlist index payload fields."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _setup_error(
    code: str,
    *,
    addon_mode: bool | None = None,
    accepted: bool | None = None,
    receipt_persisted: bool | None = None,
    station_resumed: bool | None = None,
    extra: Mapping[str, Any] | None = None,
) -> JSONResponse:
    """Return one allowlisted, secret-free first-listen failure envelope.

    ``addon_mode=False`` swaps in the standalone wording where the add-on-shaped
    instruction would send the operator somewhere that does not exist. The error
    code is unchanged either way, so a consumer branching on ``code`` sees no
    difference. ``None`` means the caller does not know, and keeps the default.
    """
    entry = _SETUP_ERRORS.get(code, _SETUP_ERRORS["invalid_request"])
    if addon_mode is False:
        entry = _SETUP_ERRORS_STANDALONE.get(code, entry)
    title, message, retryable, action_label, status_code = entry
    payload: dict[str, Any] = {
        "ok": False,
        "error": {
            "code": code if code in _SETUP_ERRORS else "invalid_request",
            "title": title,
            "message": message,
            "retryable": retryable,
            "action_label": action_label,
            "help_url": _FIRST_LISTEN_HELP_URL,
        },
    }
    if accepted is not None:
        payload["accepted"] = accepted
    if receipt_persisted is not None:
        payload["receipt_persisted"] = receipt_persisted
    if station_resumed is not None:
        payload["station_resumed"] = station_resumed
    if extra:
        payload.update(extra)
    return JSONResponse(payload, status_code=status_code)


async def _strict_setup_json(
    request: Request,
    fields: Mapping[str, type],
) -> tuple[dict[str, Any], JSONResponse | None]:
    """Parse an exact JSON object contract for active setup actions."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return {}, _setup_error("invalid_request")
    body, error = await read_json_object(request)
    if error is not None or set(body) != set(fields):
        return {}, _setup_error("invalid_request")
    for name, expected_type in fields.items():
        value = body.get(name)
        valid = type(value) is bool if expected_type is bool else isinstance(value, expected_type)
        if not valid:
            return {}, _setup_error("invalid_request")
    return body, None


def _home_access_fingerprint(config) -> str:
    """Hash the active HA endpoint/credential without retaining either in proof state."""
    material = f"{config.homeassistant.url}\0{config.ha_token}".encode("utf-8", errors="ignore")
    return hashlib.sha256(material).hexdigest()


def _require_active_setup_access(request: Request, credentials=Depends(security)) -> None:
    """Authorize active setup writes and require an unguessable browser token.

    Same-Origin is deliberately insufficient here: the request host is supplied
    by the client, so DNS rebinding can make an attacker's Origin appear to
    match a loopback URL.  The dashboard receives the per-process CSRF token in
    its HTML; non-browser automation can use the explicit admin token instead.
    """
    require_admin_access(request, credentials)
    config = request.app.state.config
    supplied_token = request.headers.get("X-Radio-Admin-Token", "")
    if config.admin_token and supplied_token and secrets.compare_digest(supplied_token, config.admin_token):
        return
    # A per-process CSRF secret is not a DNS-rebinding defense when an attacker
    # can first fetch a page from the rebound host and read the embedded token.
    # Active setup therefore accepts browser-token auth only on a literal local
    # or private address (or genuine Supervisor ingress). Custom/public hostnames
    # can still use the explicit admin-token escape hatch above.
    peer_host = request.client.host if request.client else ""
    try:
        peer_address = ipaddress.ip_address(peer_host)
    except ValueError:
        peer_address = None
    ingress = request.headers.get("X-Ingress-Path", "")
    trusted_ingress = bool(config.is_addon and ingress and peer_address is not None and peer_address in _HASSIO_NETWORK)
    try:
        request_host = request.url.hostname or ""
        host_address = ipaddress.ip_address(request_host)
    except ValueError:
        host_address = None
    trusted_literal_host = request_host == "localhost" or bool(
        host_address is not None
        and (host_address.is_loopback or any(host_address in network for network in _TRUSTED_NETWORKS))
    )
    if not (trusted_ingress or trusted_literal_host):
        raise HTTPException(
            status_code=403,
            detail="Active setup host is not trusted. Use a local address, Home Assistant ingress, or an admin token.",
        )
    csrf_token = request.headers.get("X-Radio-CSRF-Token", "")
    if csrf_token and secrets.compare_digest(csrf_token, _get_csrf_token(request.app)):
        return
    raise HTTPException(
        status_code=403,
        detail="Active setup request blocked. Reload the dashboard and retry.",
    )


def _admin_target_error() -> JSONResponse:
    """Return the strict contract error shared by Admin row mutations."""
    return JSONResponse(
        content={
            "ok": False,
            "reason": "invalid_target",
            "error": "That rotation action is missing current track details. Refresh the rotation and try again.",
        },
        status_code=422,
    )


def _stale_playlist_error() -> JSONResponse:
    """Return a recoverable conflict when an Admin row snapshot is stale."""
    return JSONResponse(
        content={
            "ok": False,
            "reason": "stale_playlist",
            "error": "The rotation changed before that action finished. Refresh it and try again.",
        },
        status_code=409,
    )


def _rotation_updating_error() -> JSONResponse:
    """Return a recoverable conflict when another rotation update owns the lock."""
    return JSONResponse(
        content={
            "ok": False,
            "reason": "rotation_updating",
            "error": "The rotation is updating right now. Wait a moment and try again.",
        },
        status_code=409,
    )


def _strict_admin_target(
    body: Mapping[str, Any],
    *,
    index_fields: tuple[str, ...],
    id_fields: tuple[str, ...],
) -> tuple[int, dict[str, int], dict[str, str]] | JSONResponse:
    """Parse one revision plus strict non-negative indices and opaque row tokens."""
    raw_revision = body.get("revision")
    if isinstance(raw_revision, bool) or not isinstance(raw_revision, int) or raw_revision < 0:
        return _admin_target_error()

    indices: dict[str, int] = {}
    for field in index_fields:
        raw_index = body.get(field)
        if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
            return _admin_target_error()
        indices[field] = raw_index

    track_ids: dict[str, str] = {}
    for field in id_fields:
        raw_id = body.get(field)
        if not isinstance(raw_id, str) or not raw_id.strip():
            return _admin_target_error()
        track_ids[field] = raw_id.strip()

    return raw_revision, indices, track_ids


async def _acquire_admin_playlist_lock(lock: asyncio.Lock) -> bool:
    """Acquire the rotation lock briefly, returning False instead of waiting indefinitely."""
    try:
        await asyncio.wait_for(lock.acquire(), timeout=_ADMIN_PLAYLIST_LOCK_TIMEOUT_SECONDS)
    except TimeoutError:
        return False
    return True


def _safe_external_album_art(value: Any) -> str:
    """Return a browser-renderable artwork URL without making it server-active."""
    url = str(value or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def _addon_external_media_error(config: Any) -> JSONResponse | None:
    """Return the shared add-on denial for extractor-only operations."""
    if not getattr(config, "is_addon", False):
        return None
    return _media_error(
        403,
        "external_media_unavailable_in_addon",
        "External resolution is unavailable in this Home Assistant add-on. Local search and shout-outs still work.",
        retryable=False,
        next_action="Use a local track or send the request as a shout-out.",
    )


def _external_extractors_status(config: Any) -> dict[str, str]:
    """Report artifact truth without exposing an extractor inventory."""
    if getattr(config, "is_addon", False):
        return {"state": "unavailable_in_addon"}
    return {
        "state": "operator_enabled"
        if external_media_enabled(bool(getattr(config, "allow_ytdlp", False)))
        else "disabled"
    }


SESSION_STOPPED_FLAG = "session_stopped.flag"
SILENCE_FAILURE_SECONDS = 30.0
# Producer rescue-bridge health (#547). A drain/resume/idle bridge firing now and
# then is normal (one startup or resume bridge is expected). Firing repeatedly
# means the lookahead queue is starving and the station is "running on rescue" —
# audio plays, but it is cached rotation, not the real station. BRIDGE_HEALTH_*
# defines that line: this many bridges inside the rolling window flips runtime
# status to unhealthy so the operator sees it instead of a falsely-green station.
BRIDGE_HEALTH_WINDOW_SECONDS = 1800.0  # 30-minute rolling window
BRIDGE_HEALTH_THRESHOLD = 2  # bridges within the window before "running on rescue"
BRIDGE_HEALTH_QUEUE_EMPTY_WINDOW_SECONDS = 600.0
BRIDGE_HEALTH_QUEUE_EMPTY_THRESHOLD_SECONDS = 60.0
# Bridge types that are recorded but must NOT feed the "running on rescue" alarm.
# "continuity" is a live control reserving safety audio that then aired, which
# reflects operator activity rather than the producer falling behind. Every type
# added to record_bridge_fire feeds the rolling window by default, so a new type
# belongs here whenever it fires on ordinary operator activity.
_NON_ALARMING_BRIDGE_TYPES = frozenset({"continuity"})
# Generated segment waste (#397). Counts rendered audio discarded before broadcast.
# The rolling window and thresholds flip the admin "Generated waste" row to degraded
# so operators see frequent purges instead of a falsely-green diagnostics card.
GENERATION_WASTE_WINDOW_SECONDS = 900.0  # 15-minute rolling window
GENERATION_WASTE_DEGRADED_SECONDS = 120.0  # recent discarded audio duration
GENERATION_WASTE_DEGRADED_COUNT = 5  # recent discarded segment count
# Legacy no-content ceiling, kept as the documented upper bound a connected
# listener may wait before the station has put *something* on air (invariant:
# check-release-invariants.sh asserts <= 5s). The actual first-byte reaction is
# FIRST_BYTE_GRACE_SECONDS below; this stays as the ceiling the grace must not
# exceed.
QUEUE_FALLBACK_WAIT_SECONDS = 5.0
# How long a connected listener waits for the producer to deliver a real segment
# before the playback loop reaches for rescue audio — AND the elapsed gate the
# rescue ladder (canned -> norm cache -> demo asset) opens at. This is the
# *first-byte* reaction time: on a cold start or addon restart (queue not yet
# filled, listener already connected) the loop used to block the full
# QUEUE_FALLBACK_WAIT_SECONDS on segment_queue.get() *and* gate the norm-cache
# rescue behind that same 5s, so first byte landed at ~5.9s even with a warm
# cache — past the 1-2s INSTANT AUDIO promise. With the producer's lookahead
# buffer, a timed-out get() only happens under genuine starvation (cold start /
# sustained producer failure), never a normal inter-segment gap, so opening the
# whole ladder at this short grace does not make the loop rescue-happy: it just
# puts cached/bridge audio on air fast instead of holding the listener in
# silence while hoping the producer catches up. Must stay <=
# QUEUE_FALLBACK_WAIT_SECONDS (asserted in test_streamer_routes).
FIRST_BYTE_GRACE_SECONDS = 1.0
FIRST_LISTEN_SHOW_APPROVAL_TIMEOUT_SECONDS = 0.5
assert FIRST_LISTEN_SHOW_APPROVAL_TIMEOUT_SECONDS <= FIRST_BYTE_GRACE_SECONDS
STARTUP_GRACE_SECONDS = 30.0
# Keep the admin "On air" verdict stable across a normal segment handoff. This
# is deliberately far shorter than the 30s silence alarm.
STREAM_HANDOFF_GRACE_SECONDS = 3.0
CLIP_RATE_LIMIT_SECONDS = 10.0
CLIP_RATE_PRUNE_SECONDS = 300.0
CLIP_DURATION_SECONDS = 30
# Ad/banter are operator-authored (no copyright cap), so a shared clip can cover
# the whole segment. This ceiling bounds both the ring buffer (main.py sizes it
# from this) and the per-clip extraction so memory stays bounded on Pi hardware.
CLIP_MAX_SEGMENT_SECONDS = 180
# After an ad/banter ends we keep its snapshot briefly, so a listener who taps
# Share a moment too late (music already playing again) still gets the whole bit.
CLIP_LOOKBACK_SECONDS = 15
CLIP_MAX_SAVED = 50
DEFAULT_CLIP_BITRATE_KBPS = 192
STREAM_MAX_PACKET_SECONDS = 0.125
# Restores 375 ms — ~75% of the old 0.5s lead, ~9% of today's 4s. Calibrated
# against STREAM_TARGET_LEAD_SECONDS; revisit the two together.
STREAM_MAX_RECOVERY_CHUNKS = 3
STREAM_UNDERRUN_WARNING_INTERVAL_SECONDS = 60.0


def _stream_chunk_size(bytes_per_second: float) -> int:
    """Bound source-packet duration so pacing cannot overshoot by a full read."""
    return max(1, min(4096, int(max(float(bytes_per_second), 1.0) * STREAM_MAX_PACKET_SECONDS)))


@dataclass(frozen=True)
class StreamPacingDecision:
    """One post-send pacing decision on the station's media timeline."""

    sleep_seconds: float
    kind: str | None = None
    lateness_seconds: float = 0.0
    remaining_lead_seconds: float = 0.0
    deficit_seconds: float = 0.0
    warn_underrun: bool = False


class StreamPacer:
    """Keep one bounded send-ahead timeline across contiguous segments.

    The first few bounded MP3 packets establish the fixed delivery cushion.
    Ordinary segment boundaries keep the same origin. Only callers that detect
    a real transport discontinuity reset it explicitly.
    """

    def __init__(
        self,
        bytes_per_second: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        target_lead_seconds: float = STREAM_TARGET_LEAD_SECONDS,
        late_threshold_seconds: float = STREAM_LATE_THRESHOLD_SECONDS,
        max_recovery_chunks: int = STREAM_MAX_RECOVERY_CHUNKS,
    ) -> None:
        self.bytes_per_second = max(float(bytes_per_second), 1.0)
        self.target_lead_seconds = max(0.0, float(target_lead_seconds))
        self.late_threshold_seconds = max(0.0, float(late_threshold_seconds))
        self.max_recovery_chunks = max(1, int(max_recovery_chunks))
        self._monotonic = monotonic
        self._origin: float | None = None
        self._media_seconds = 0.0
        self._recovery_chunks = 0
        self._recovery_media_seconds = 0.0
        self._recovery_lateness_seconds = 0.0
        self._recovery_deficit_seconds = 0.0
        self._last_underrun_warning = float("-inf")
        self.reset_count = 0
        self.last_reset_reason = ""

    @property
    def media_seconds(self) -> float:
        """Test-visible media position on the current pacing origin."""
        return self._media_seconds

    def reset_timeline(self, reason: str) -> None:
        """Reset after a real discontinuity, never a natural segment boundary."""
        normalized = str(reason or "discontinuity")
        if self._origin is None and self.last_reset_reason == normalized:
            return
        self._origin = None
        self._media_seconds = 0.0
        self._recovery_chunks = 0
        self._recovery_media_seconds = 0.0
        self._recovery_lateness_seconds = 0.0
        self._recovery_deficit_seconds = 0.0
        self.reset_count += 1
        self.last_reset_reason = normalized

    def after_send(self, chunk_bytes: int) -> StreamPacingDecision:
        """Advance one emitted chunk and return a non-negative bounded wait."""
        chunk_seconds = max(0, int(chunk_bytes)) / self.bytes_per_second
        now = self._monotonic()
        if self._origin is None:
            self._origin = now
            self.last_reset_reason = ""

        media_before = self._media_seconds
        elapsed = max(0.0, now - self._origin)
        send_deadline = self._origin + max(0.0, media_before - self.target_lead_seconds)
        lateness = max(0.0, now - send_deadline)
        remaining_before = media_before - elapsed
        deficit = max(0.0, -remaining_before)
        self._media_seconds += chunk_seconds

        if self._recovery_chunks:
            self._recovery_chunks += 1
            self._recovery_media_seconds += chunk_seconds
            if self._recovery_chunks >= self.max_recovery_chunks:
                # Drop the overdue wall-clock history once. The retained media
                # position is only the bounded recovery burst, so a long pause
                # cannot turn into an unbounded listener-queue catch-up flood.
                self._origin = now
                self._media_seconds = self._recovery_media_seconds
                next_deadline = self._origin + max(0.0, self._media_seconds - self.target_lead_seconds)
                decision = StreamPacingDecision(
                    sleep_seconds=max(0.0, next_deadline - now),
                    kind="overrun_rebased",
                    lateness_seconds=self._recovery_lateness_seconds,
                    remaining_lead_seconds=max(0.0, self._media_seconds),
                    deficit_seconds=self._recovery_deficit_seconds,
                )
                self._recovery_chunks = 0
                self._recovery_media_seconds = 0.0
                self._recovery_lateness_seconds = 0.0
                self._recovery_deficit_seconds = 0.0
                return decision
            return StreamPacingDecision(sleep_seconds=0.0)

        kind: str | None = None
        warn_underrun = False
        if lateness >= self.late_threshold_seconds:
            if remaining_before <= 0.0:
                kind = "underrun"
                self._recovery_chunks = 1
                self._recovery_media_seconds = chunk_seconds
                self._recovery_lateness_seconds = lateness
                self._recovery_deficit_seconds = deficit
                if now - self._last_underrun_warning >= STREAM_UNDERRUN_WARNING_INTERVAL_SECONDS:
                    self._last_underrun_warning = now
                    warn_underrun = True
            else:
                kind = "late"

        if self._recovery_chunks:
            sleep_seconds = 0.0
        else:
            next_deadline = self._origin + max(0.0, self._media_seconds - self.target_lead_seconds)
            sleep_seconds = max(0.0, next_deadline - now)
        return StreamPacingDecision(
            sleep_seconds=sleep_seconds,
            kind=kind,
            lateness_seconds=lateness,
            remaining_lead_seconds=max(0.0, remaining_before),
            deficit_seconds=deficit,
            warn_underrun=warn_underrun,
        )


def _drain_segment_queue(q) -> list:
    """Drain all segments from the queue without unlinking."""
    items: list = []
    while not q.empty():
        try:
            items.append(q.get_nowait())
            q.task_done()
        except Exception:
            break
    return items


def _is_packaged_asset(path: Path) -> bool:
    return is_packaged_asset(path, _DEMO_ASSETS_DIR)


def _queued_audio_seconds(q, *, state: StationState | None = None) -> float:
    """Sum queued audio seconds, optionally requiring playback-valid media."""
    internal = getattr(q, "_queue", None)
    if internal is None:
        return 0.0

    def _duration(item) -> float | None:
        if state is not None and (not isinstance(item, Segment) or not _segment_is_immediately_playable(state, item)):
            return None
        value = item.get("duration_sec") if isinstance(item, dict) else getattr(item, "duration_sec", None)
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            return float(value)
        return None

    return buffered_audio_seconds(_duration(item) for item in list(internal))


def _purge_segment_queue(q) -> int:
    """Drain all pre-produced segments from the queue and unlink temp files."""
    items = _drain_segment_queue(q)
    for seg in items:
        _unlink_ephemeral_best_effort(seg)
    return len(items)


def _unlink_ephemeral_best_effort(seg) -> None:
    """Delete an ephemeral segment's temp render without ever raising (#397).

    Purge paths must always finish clearing the queue/shadow and returning their
    count even when a temp unlink fails (permission/IO). ``missing_ok=True`` only
    swallows a missing file; a real ``OSError`` would otherwise abort the purge
    mid-loop and leave the UI shadow stale behind a half-drained queue.
    """
    release = getattr(seg, "release", None)
    if callable(release):
        release()
    if getattr(seg, "ephemeral", False) and not _is_packaged_asset(seg.path):
        try:
            seg.path.unlink(missing_ok=True)
        except Exception:
            # Broad on purpose: a non-OSError (e.g. a malformed segment whose
            # path is None -> AttributeError) must not abort the purge loop and
            # leave the UI shadow stale behind a half-drained queue. Honors this
            # helper's "without ever raising" contract.
            logger.warning("Ephemeral purge unlink failed for %s", getattr(seg, "path", None), exc_info=True)


def _validated_starter_share_snapshot(segment: Segment) -> dict[str, Any] | None:
    """Return a complete, manifest-verified starter track snapshot.

    Music sharing must never derive bytes from the rolling buffer: it can span
    two tracks or contain Jamendo/local audio.  A starter snapshot therefore
    points only at the immutable packaged file and is created after that exact
    segment reaches EOF for at least one listener.
    """
    metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
    if segment.type is not SegmentType.MUSIC or metadata.get("source_kind") != "starter":
        return None
    raw_attribution = metadata.get("music_attribution")
    if not isinstance(raw_attribution, dict) or raw_attribution.get("basis") != "bundled_manifest":
        return None
    attribution = MediaAttribution(
        provider=str(raw_attribution.get("provider") or ""),
        license_id=str(raw_attribution.get("license_id") or ""),
        license_url=str(raw_attribution.get("license_url") or ""),
        source_url=str(raw_attribution.get("source_url") or ""),
        credit=str(raw_attribution.get("credit") or ""),
        modified=bool(raw_attribution.get("modified")),
        basis="bundled_manifest",
    )
    track = Track(
        title=str(metadata.get("title_only") or ""),
        artist=str(metadata.get("artist") or ""),
        duration_ms=int(metadata.get("duration_ms") or round(segment.duration_sec * 1000)),
        spotify_id=str(metadata.get("spotify_id") or ""),
        local_path=segment.path,
        source="starter",
        provider_track_id=str(metadata.get("provider_track_id") or ""),
        attribution=attribution,
    )
    try:
        from mammamiradio.media.starter import resolve_starter_track

        entry = resolve_starter_track(track)
    except (OSError, TypeError, ValueError):
        logger.warning("Starter share snapshot rejected by manifest validation", exc_info=True)
        return None
    safe_attribution = safe_media_attribution_dict(attribution)
    if safe_attribution is None:
        return None
    return {
        "path": entry.path,
        "ended_monotonic": time.monotonic(),
        "type": "starter",
        "title": entry.title,
        "artist": entry.artist,
        "provider_track_id": entry.isrc,
        "attribution": safe_attribution,
    }


def _drop_segment_moment_receipts(state: StationState, segment, reason: str) -> None:
    """Demote any Moment Receipt a discarded queued segment was carrying.

    Every path that pulls an already-queued BANTER segment out of the real
    queue without letting it air (purge, session-stop mid-selection, an
    operator's /api/queue/remove) must also settle the receipt honestly —
    otherwise the admin trail keeps showing "waiting for its break" for a
    segment that no longer exists. Best-effort like every other receipt call:
    never lets bookkeeping affect the purge/discard it's piggybacking on.
    """
    store = getattr(state, "moment_store", None)
    if store is None:
        return
    meta = getattr(segment, "metadata", None)
    if not isinstance(meta, dict):
        return
    try:
        for key in ("ritual_moment_id", "gag_moment_id"):
            moment_id = str(meta.get(key) or "")
            if moment_id:
                store.mark_dropped(moment_id, reason)
    except Exception:  # pragma: no cover - receipts must never break a purge
        logger.debug("Moment receipt discard drop failed", exc_info=True)


def _discard_handoff_orphans(state: StationState, segments: list[Segment], reason: str) -> int:
    """Account for tail successors invalidated by a synchronous queue rewrite."""

    seen: set[int] = set()
    discarded = 0
    for segment in segments:
        if id(segment) in seen:
            continue
        seen.add(id(segment))
        state.record_discard(segment, reason=reason, already_counted_in_produced=True)
        _drop_segment_moment_receipts(state, segment, reason)
        _unlink_ephemeral_best_effort(segment)
        discarded += 1
    return discarded


def _cancel_active_handoff_from_queue(
    q,
    state: StationState,
    *,
    reason: str,
    excluded_paths: set[Path] | None = None,
    excluded_track_keys: set[tuple[str, str]] | None = None,
) -> int:
    """Remove the queued successor when a currently airing music head is cut.

    The currently streaming head has already left ``q`` and can never be made
    whole again.  This no-await drain/rebuild removes only its paired successor
    before the normal control action makes a skip visible to playback.
    """

    if not has_cancellable_music_handoff(state):
        return 0
    # Snapshot the hidden full source before cancellation releases the private
    # reservation. A continuity rebuild immediately after Skip/Panic/source
    # switch must never select music whose head has already partially aired.
    for original_path, music in handoff_original_sources(state):
        if excluded_paths is not None:
            excluded_paths.add(original_path)
        if excluded_track_keys is not None:
            track_key = _segment_track_key(music)
            if track_key != ("", ""):
                excluded_track_keys.add(track_key)
    items = _drain_segment_queue(q)
    orphaned = cancel_active_music_handoff(state, items)
    discarded = _discard_handoff_orphans(state, orphaned, reason)
    _rebuild_queue_shadow(q, state, items)
    return discarded


def _settle_discarded_selection_handoff(segment_queue, state: StationState, segment, *, reason: str) -> None:
    """Release handoff ownership for a selected pair member that will not air.

    The playback loop's pre-send guards discard a selected segment with a
    ``continue`` before the outer finalizer takes ownership. A discarded music
    head must retract its queued tail successor; a discarded successor must
    release its reservation so the hidden original stops being cache-protected.
    """
    if not getattr(segment, "handoff_id", None):
        return
    prior = state.active_playback_segment
    state.active_playback_segment = segment
    try:
        _cancel_active_handoff_from_queue(segment_queue, state, reason=reason)
        finish_handoff_segment(state, segment, completed_cleanly=False)
    finally:
        state.active_playback_segment = prior if prior is not segment else None


def _purge_queue_and_shadow(q, state: StationState, *, reason: str) -> int:
    """Drain the real queue AND clear the UI shadow in one synchronous block.

    Single home for "purge everything". Every operator purge (stop, panic,
    source-switch, chaos-enable, festival-enable, /api/purge) routes through
    here so the shadow (``state.queued_segments``, the "Up Next" projection) can
    never again be left stale behind a drained real queue. The festival-enable
    path previously drained the queue but forgot the shadow clear, leaving the
    panel showing segments that no longer existed (the queue-shadow drift seen
    when Festival Mode was toggled mid-stream).

    Synchronous: the drain and the shadow clear happen with no ``await`` between
    them, so a caller can keep its epoch bump / ``skip_event`` in the same
    no-await stretch and no reader can observe the two views disagreeing.
    """
    items = _drain_segment_queue(q)
    # The final queue is deliberately empty.  Releasing against that topology
    # removes any tail successor (including one paired with the current music
    # head) and prevents a stop/purge/source switch from reviving it later.
    reconcile_handoff_queue_items(state, [])
    for seg in items:
        state.record_discard(seg, reason=reason, already_counted_in_produced=True)
        _drop_segment_moment_receipts(state, seg, str(reason))
        _unlink_ephemeral_best_effort(seg)
    state.queued_segments.clear()
    return len(items)


_CONTINUITY_RESERVATION_FLAG = "continuity_reservation"
_CONTINUITY_ADMISSION_EPOCH = "continuity_admission_epoch"
_PLAYBACK_GAP_FILL_FLAG = "playback_gap_fill"
_CONTINUITY_CACHE_SCAN_LIMIT = 24
# "Any audio at all", for callers that need a non-empty reservation rather than a
# full runway. Deliberately sub-frame: it only has to beat a zero target.
ANY_PLAYABLE_RUNWAY_SECONDS = 0.001


@dataclass(slots=True)
class ContinuityRunwayOutcome:
    """Describe which runway a destructive control left behind."""

    fresh_reservation: bool = False
    preserved_existing: bool = False


def _segment_track_key(segment: Segment) -> tuple[str, str]:
    """Return the same coarse artist/title identity used by cache continuity."""

    metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
    return (
        str(metadata.get("artist") or "").strip().lower(),
        str(metadata.get("title_only") or metadata.get("title") or "").strip().lower(),
    )


def _indexed_audio_path_is_file(path: Path) -> bool:
    """Best-effort cache-index liveness check for the bounded control hot path."""
    if not path:
        return False
    try:
        file_stat = path.stat()
        return _stat.S_ISREG(file_stat.st_mode) and file_stat.st_size > 0
    except OSError:
        return False


def _segment_blocklist_key(segment: Segment) -> tuple[str, str]:
    """Return the durable blocklist identity carried by a ready segment.

    Delegates to the shared definition in ``core.models`` so the streamer, the
    producer's admission gate, and the restart-handoff validator cannot drift on
    what counts as the same song.
    """
    return segment_track_key(segment)


def _companionship_segment_epoch(segment: Segment) -> tuple[bool, int | None]:
    """Return whether a segment carries the cue marker and its valid epoch."""
    metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
    if metadata.get("listener_session_cue") != "companionship":
        return False, None
    epoch = metadata.get("listener_session_epoch")
    if isinstance(epoch, int) and not isinstance(epoch, bool) and epoch > 0:
        return True, epoch
    return True, None


def _companionship_cue_is_current(state: StationState, epoch: int | None) -> bool:
    """Return whether a companionship cue remains eligible to reach air."""
    return bool(
        epoch is not None
        and epoch == state.listener_session.epoch
        and state.listener_session.companionship_cue_state is ListenerSessionCueState.QUEUED
    )


def _clear_stale_companionship_selection(state: StationState, selected_epoch: int | None) -> None:
    """Remove provisional now-playing state for a rejected companionship cue."""
    if selected_epoch is None:
        return
    now_streaming = state.now_streaming if isinstance(state.now_streaming, dict) else {}
    if now_streaming.get("epoch") != selected_epoch or state.playback_epoch != selected_epoch:
        return
    state.now_streaming = {}
    state.current_stream_audible = False
    if state.audible_playback_epoch == selected_epoch:
        state._last_audible_stream = {}
    state.last_state_change_at = time.time()


def _segment_is_immediately_playable(
    state: StationState,
    segment: Segment,
    *,
    excluded_paths: set[Path] | None = None,
    excluded_track_keys: set[tuple[str, str]] | None = None,
) -> bool:
    """Return whether a queued/slot segment is safe to use as live runway now."""
    excluded_paths = excluded_paths or set()
    excluded_track_keys = excluded_track_keys or set()
    if segment.path in excluded_paths:
        return False
    is_companionship_cue, companionship_epoch = _companionship_segment_epoch(segment)
    if is_companionship_cue and not _companionship_cue_is_current(state, companionship_epoch):
        return False
    if segment.type is SegmentType.MUSIC:
        key = _segment_blocklist_key(segment)
        if key in state.blocklist or key in excluded_track_keys:
            return False
    return _indexed_audio_path_is_file(segment.path)


def _continuity_reservation_segments(
    state: StationState,
    config,
    target_seconds: float,
    *,
    max_segments: int | None = None,
    excluded_paths: set[Path] | None = None,
    excluded_track_keys: set[tuple[str, str]] | None = None,
) -> list[Segment]:
    """Build no-wait packaged/cache fallback segments for a control action.

    This deliberately avoids ffprobe, network, synthesis, and FFmpeg.  A control
    can only reserve audio that is already safe to play now.
    """
    selected: list[Segment] = []
    covered = 0.0
    excluded_paths = excluded_paths or set()
    persistent_blocked_keys = set(state.blocklist)
    blocked_keys = persistent_blocked_keys | (excluded_track_keys or set())
    recovery = _DEMO_ASSETS_DIR / "recovery" / "continuity_1.mp3"
    emergency_tone = _DEMO_ASSETS_DIR / "recovery" / "emergency_tone.mp3"
    reservation_id = uuid4().hex

    def _can_add() -> bool:
        return max_segments is None or len(selected) < max_segments

    def _target_met() -> bool:
        return bool(selected) and covered >= target_seconds

    def _add(segment: Segment) -> None:
        nonlocal covered
        selected.append(segment)
        covered += segment.duration_sec

    def _add_cached_segment(cached: Path, duration: float, sidecar: dict) -> None:
        _add(
            Segment(
                type=SegmentType.MUSIC,
                path=cached,
                duration_sec=duration,
                metadata={
                    "title": str(sidecar.get("title") or "Cached music"),
                    "title_only": str(sidecar.get("title") or "Cached music"),
                    "artist": str(sidecar.get("artist") or ""),
                    "duration_ms": round(duration * 1000),
                    "audio_source": "norm_cache",
                    "rescue": True,
                    _CONTINUITY_RESERVATION_FLAG: True,
                    "continuity_reservation_id": reservation_id,
                    "queue_reason": "Protected continuity audio.",
                },
                ephemeral=False,
            )
        )

    # Stop as soon as the target/capacity contract is satisfied. A control hot
    # path must not stat and read sidecars for the whole warm cache merely to
    # choose the first one or two immediately playable tracks.
    scanned = 0
    prune_paths: list[Path] = []
    deferred_cooling: list[tuple[Path, float, dict]] = []
    indexed_items = list(state.immediate_audio_index.items())
    # Computed once, in memory, before the scan: no filesystem access, and the
    # per-candidate check below reuses the sidecar the loop already loaded.
    recent_keys = _recent_music_identity_keys(state)
    cooling_by_path: dict[Path, bool] = {}
    if state.rescue_airplay:
        rotation_now = time.monotonic()
        # Cooling entries remain in the index by design. Put eligible entries
        # first so the bounded scan does not spend all 24 slots on a cooling
        # prefix while a fresh cache track waits just beyond it.
        cooling_by_path = {cached: _rescue_on_cooldown(state, cached, now=rotation_now) for cached, _ in indexed_items}
        indexed_items.sort(key=lambda item: cooling_by_path[item[0]])
    for cached, duration in indexed_items:
        if not _can_add() or _target_met():
            break
        if scanned >= _CONTINUITY_CACHE_SCAN_LIMIT:
            logger.info(
                "Continuity cache scan stopped at the %d-entry live-control limit",
                _CONTINUITY_CACHE_SCAN_LIMIT,
            )
            break
        scanned += 1
        if not cached.name.startswith("norm_") or cached == state.last_music_file or cached in excluded_paths:
            continue
        if duration <= 0 or not _indexed_audio_path_is_file(cached):
            # The index is an optimization, not durable state. Removing entries
            # that cannot possibly play now keeps subsequent controls bounded and
            # lets them advance to still-live cache candidates.
            prune_paths.append(cached)
            continue
        metadata = load_track_metadata(cached) or {}
        cache_key = _sidecar_track_key(metadata)
        if cache_key in blocked_keys:
            logger.info("Skipping blocklisted cached continuity track (%s - %s)", cache_key[0], cache_key[1])
            if cache_key in persistent_blocked_keys:
                # A durable ban makes this path unusable for the session. It can
                # be re-indexed by a later render or the next startup after unban.
                prune_paths.append(cached)
            continue
        if _is_recent_music(cached, recent_keys, sidecar=metadata):
            # The song on air right now (or one of the last few) must never be
            # re-reserved. Unlike the cooldown below this is a HARD skip: it is
            # never rescued into deferred_cooling, because a listener hearing
            # the same song twice in four minutes is the illusion breaking. The
            # rungs below (packaged clip, emergency tone, and the playback
            # loop's own ladder) keep audio flowing.
            logger.info("Skipping on-air/recent cached continuity track: %s", cached.name)
            continue
        if cooling_by_path.get(cached, False):
            # This song aired as a rescue within the hour. Prefer a fresher track
            # so repeated controls don't reserve the same song; keep it as a
            # least-recent fallback only if nothing else is available.
            deferred_cooling.append((cached, duration, metadata))
            continue
        _add_cached_segment(cached, duration, metadata)

    # Every fresh cache candidate is still cooling down: reserve the
    # least-recently-heard one anyway so a control keeps real music, not dead air.
    if deferred_cooling and _can_add() and not _target_met():
        for cached, duration, metadata in sorted(
            deferred_cooling, key=lambda item: _rescue_last_heard_at(state, item[0]) or 0.0
        ):
            if not _can_add() or _target_met():
                break
            _add_cached_segment(cached, duration, metadata)

    for path in prune_paths:
        state.immediate_audio_index.pop(path, None)

    # The packaged sweeper is the rung BELOW cached music, not a mandatory
    # preamble to it. When the warm cache yielded a real song the control goes
    # straight into it: a 4.4s canned line in front of the song is what made a
    # repeat sound like a deliberate rotation choice instead of a hiccup, and it
    # is the one voice on air that belongs to neither host.
    if (
        not selected
        and _can_add()
        and recovery not in excluded_paths
        and is_approved_spoken_asset(recovery, assets_root=_DEMO_ASSETS_DIR)
    ):
        _add(
            Segment(
                type=SegmentType.BANTER,
                path=recovery,
                duration_sec=4.44,
                metadata={
                    "type": "banter",
                    "title": "Station continuity",
                    "canned": True,
                    "rescue": True,
                    _CONTINUITY_RESERVATION_FLAG: True,
                    "continuity_reservation_id": reservation_id,
                    "queue_reason": "Protected continuity audio.",
                },
                ephemeral=False,
            )
        )

    # This asset is deliberately separate from the normal continuity copy: it is
    # the cold-cache, no-clip final fallback and is available without a render.
    if (
        not selected
        and _can_add()
        and emergency_tone not in excluded_paths
        and is_approved_packaged_audio_asset(emergency_tone, assets_root=_DEMO_ASSETS_DIR)
    ):
        _add(
            Segment(
                type=SegmentType.MUSIC,
                path=emergency_tone,
                duration_sec=2.0,
                metadata={
                    "title": "Station continuity",
                    "artist": "",
                    "duration_ms": 2000,
                    "audio_source": "emergency_tone",
                    "rescue": True,
                    _CONTINUITY_RESERVATION_FLAG: True,
                    "continuity_reservation_id": reservation_id,
                    "queue_reason": "Protected continuity audio.",
                },
                ephemeral=False,
            )
        )

    return selected


def _continuity_slot_seconds(state: StationState, *, self_heal: bool = True) -> float:
    """Return ready seconds held in the capacity-exempt continuity slot.

    ``self_heal`` clears a slot whose backing file has vanished. Mutation paths
    (skip / reserve / playback) leave it on so a dangling slot is dropped; the
    read-only status snapshots pass ``self_heal=False`` so a listener poll can
    never clear the reserved dead-air safety slot on a transient stat blip.
    """
    slot = state.continuity_slot
    if slot is None:
        return 0.0
    if not _indexed_audio_path_is_file(slot.path):
        if self_heal:
            logger.warning("Protected continuity slot disappeared before playback; clearing it")
            state.continuity_slot = None
        return 0.0
    return buffered_audio_seconds([float(getattr(slot, "duration_sec", 0.0) or 0.0)])


def _claim_continuity_slot(state: StationState) -> Segment | None:
    """Claim the capacity-exempt slot only if it is still safe to broadcast.

    A durable ban can land after reservation but before the queue drains. This
    last-mile gate keeps that newly banned cached song off air without letting
    stale slot state block the normal empty-queue recovery ladder.
    """
    slot = state.continuity_slot
    if slot is None or _continuity_slot_seconds(state) <= 0:
        return None
    if slot.type is SegmentType.MUSIC and _segment_blocklist_key(slot) in state.blocklist:
        logger.warning("Protected continuity slot became blocklisted before playback; clearing it")
        state.continuity_slot = None
        return None
    state.continuity_slot = None
    return slot


def _playable_runway_available(q, state: StationState, *, self_heal: bool = True) -> bool:
    """Return whether cutting the current segment has ready audio behind it.

    ``self_heal`` is forwarded to :func:`_continuity_slot_seconds`; read-only
    callers (status snapshots) pass ``False`` so evaluating runway can never
    mutate ``state.continuity_slot``.
    """
    # Mirror ``run_playback_loop``: the capacity-exempt slot is only consumed
    # once the real queue is empty. A non-empty queue means the next audio the
    # loop pulls is ``queued[0]`` — never the slot — so gate strictly on the
    # head there instead of letting a ready slot mask an unplayable head.
    queued = list(getattr(q, "_queue", ()))
    if queued:
        return _segment_is_immediately_playable(state, queued[0])
    slot = state.continuity_slot
    return bool(
        slot is not None
        and _continuity_slot_seconds(state, self_heal=self_heal) > 0
        and _segment_is_immediately_playable(state, slot)
    )


def _playable_runway_source(q, state: StationState) -> str:
    """Describe the immediate runway without probing or mutating it."""
    queued = list(getattr(q, "_queue", ()))
    candidate = queued[0] if queued and _segment_is_immediately_playable(state, queued[0]) else state.continuity_slot
    if candidate is None or not _segment_is_immediately_playable(state, candidate):
        return "none"
    metadata = candidate.metadata if isinstance(candidate.metadata, dict) else {}
    source = str(metadata.get("audio_source") or candidate.path.name or "reserved_audio")
    return "norm_cache" if source == "fallback_norm_cache" else source


def _stamp_continuity_runway_epoch(q, state: StationState) -> None:
    """Bind freshly reserved runway to the timeline that admitted it.

    A playback task may already be blocked in ``queue.get()`` when a live
    control reserves runway — most sharply during a Stop plus fast Resume. Its
    local selection epoch is necessarily old, but the runway just queued is new
    and must survive that ABA cycle. Stamping only protected candidates
    distinguishes that fresh runway from pre-Stop selections without blessing
    ordinary stale work.
    """
    for segment in list(getattr(q, "_queue", ())):
        metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
        if metadata.get(_CONTINUITY_RESERVATION_FLAG):
            metadata[_CONTINUITY_ADMISSION_EPOCH] = state.continuity_epoch
    slot = state.continuity_slot
    if slot is not None:
        metadata = slot.metadata if isinstance(slot.metadata, dict) else {}
        if metadata.get(_CONTINUITY_RESERVATION_FLAG):
            metadata[_CONTINUITY_ADMISSION_EPOCH] = state.continuity_epoch


def _segment_is_canned_recovery(segment: Segment) -> bool:
    """True for packaged ident/recovery speech, not for a real queued song."""
    metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
    return bool(metadata.get("canned")) and bool(
        metadata.get("rescue") or metadata.get("fallback") or metadata.get("playback_gap_fill")
    )


def _stamp_playback_gap_fill(segment: Segment, state: StationState) -> Segment:
    """Bind a playback-built rescue fill to the current continuity timeline."""
    metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
    metadata["rescue"] = True
    metadata[_PLAYBACK_GAP_FILL_FLAG] = True
    metadata[_CONTINUITY_RESERVATION_FLAG] = True
    metadata[_CONTINUITY_ADMISSION_EPOCH] = state.continuity_epoch
    metadata.setdefault("continuity_reservation_id", f"playback-gap-{uuid4().hex}")
    metadata.setdefault("queue_reason", "Playback gap recovery.")
    segment.metadata = metadata
    return segment


def _continuity_slot_status(state: StationState) -> dict | None:
    """Admin-only projection of the capacity-exempt safety reservation."""
    slot = state.continuity_slot
    if slot is None:
        return None
    # Read-only status projection: never self-heal (clear) the reserved slot from
    # a /status poll — only mutation paths may drop a dangling slot. A slot whose
    # file has vanished still must not be advertised as ready audio, so report
    # nothing (rather than a phantom 0-duration entry) while leaving the reservation
    # in place for a mutation path to reconcile.
    duration_sec = _continuity_slot_seconds(state, self_heal=False)
    if duration_sec <= 0:
        return None
    metadata = slot.metadata if isinstance(slot.metadata, dict) else {}
    return {
        "label": str(metadata.get("title") or "Protected continuity"),
        "duration_sec": duration_sec,
        "audio_source": str(metadata.get("audio_source") or "packaged_recovery"),
        "reservation_id": str(metadata.get("continuity_reservation_id") or ""),
    }


def _rebuild_queue_shadow(q, state: StationState, items: list[Segment]) -> None:
    """Synchronously replace real queue, operator projection, and tail adjacency."""
    from mammamiradio.scheduling.producer import _queue_shadow_entry, _remember_enqueued

    prior_rows = {str(row.get("id")): row for row in state.queued_segments if row.get("id")}
    while not q.empty():
        try:
            q.get_nowait()
            q.task_done()
        except asyncio.QueueEmpty:
            break
    rows: list[dict] = []
    for segment in items:
        q.put_nowait(segment)
        queue_id = str(segment.metadata.get("queue_id") or "")
        rows.append(prior_rows.get(queue_id) or _queue_shadow_entry(segment))
    state.queued_segments = rows
    # The next generated segment's music-bed eligibility follows the ACTUAL queue
    # tail, never the item that was removed by a live control. Reuse the enqueue
    # funnel's tail bookkeeping so a cached rescue tail also supplies its own
    # clean bed source rather than an earlier, removed song.
    if items:
        _remember_enqueued(state, items[-1], items[-1].path)
    else:
        state.last_enqueued_type = None


def _reconcile_queue_tail_adjacency(q, state: StationState, *, prior_tail: Segment | None) -> None:
    """Keep speech-bed eligibility aligned after queued audio is removed.

    When the real tail is unchanged, its existing clean-bed bookkeeping remains
    authoritative. If removal exposes a different tail, only rescue/recycled
    music can safely re-anchor its clean source from the queued segment itself;
    ordinary rendered music may carry an egress-processed path, so fail closed.
    """
    queued = list(getattr(q, "_queue", ()))
    tail = queued[-1] if queued else None
    if tail is None:
        state.last_enqueued_type = None
        return
    if prior_tail is not None and tail is prior_tail:
        if tail.type is SegmentType.MUSIC and not _segment_is_immediately_playable(state, tail):
            state.last_enqueued_type = None
        return

    from mammamiradio.scheduling.producer import _adjacency_type_for, _remember_enqueued

    adjacency = _adjacency_type_for(tail)
    metadata = tail.metadata if isinstance(tail.metadata, dict) else {}
    if adjacency is SegmentType.MUSIC and (
        not (metadata.get("rescue") or metadata.get("recycled")) or not _segment_is_immediately_playable(state, tail)
    ):
        state.last_enqueued_type = None
        return
    _remember_enqueued(state, tail, tail.path)


def _discard_unplayable_queue_prefix(q, state: StationState, *, reason: str) -> int:
    """Drop leading segments that playback would reject before its first byte."""
    queued = list(getattr(q, "_queue", ()))
    prefix_length = 0
    while prefix_length < len(queued) and not _segment_is_immediately_playable(state, queued[prefix_length]):
        prefix_length += 1
    if prefix_length == 0:
        return 0

    dropped = queued[:prefix_length]
    survivors = queued[prefix_length:]
    for segment in dropped:
        discard_reason = reason
        is_companionship_cue, companionship_epoch = _companionship_segment_epoch(segment)
        if is_companionship_cue and not _companionship_cue_is_current(state, companionship_epoch):
            discard_reason = GenerationWasteReason.LISTENER_SESSION_STALE
        state.record_discard(segment, reason=discard_reason, already_counted_in_produced=True)
        _drop_segment_moment_receipts(state, segment, discard_reason)
        _unlink_ephemeral_best_effort(segment)
    prior_tail = queued[-1] if queued else None
    _rebuild_queue_shadow(q, state, survivors)
    # Trimming the head can expose a surviving unplayable MUSIC tail (a queued
    # song later banned or whose cache file was evicted). _rebuild_queue_shadow
    # only sets last_enqueued_type naively; re-run the same fail-closed check the
    # other mutation sites use so a discarded tail can't bed a stale song under
    # the next speech segment. Because only the head is trimmed, the tail is
    # unchanged, driving the reconcile's keep-branch (clears when now-unplayable).
    _reconcile_queue_tail_adjacency(q, state, prior_tail=prior_tail)
    state.continuity_epoch += 1
    # Advancing the epoch invalidates any admission stamp written before this
    # call. Callers reserve runway first and trim the dead head second (Resume,
    # Skip), so without a re-stamp the surviving reservation carries the old
    # epoch and the playback loop discards the exact audio the control just
    # reserved to avoid dead air. Re-stamp here rather than at each call site:
    # every path that bumps the epoch owns keeping its own survivors admissible.
    _stamp_continuity_runway_epoch(q, state)
    return len(dropped)


def _reserve_continuity_runway(
    app_state,
    state: StationState,
    config,
    *,
    replace_queue: bool = False,
    discard_reason: str = GenerationWasteReason.OPERATOR_PURGE,
    excluded_paths: set[Path] | None = None,
    excluded_track_keys: set[tuple[str, str]] | None = None,
    outcome: ContinuityRunwayOutcome | None = None,
    minimum_runway_seconds: float = 0.0,
) -> int:
    """Reserve playable runway, then bind it to the timeline it was created on.

    Every reservation MUST be stamped. `_reserve_continuity_runway_unstamped`
    publishes the reservation into the queue — waking any blocked `queue.get()`
    waiter — and only then advances `continuity_epoch`. A waiter that captured
    the pre-reservation epoch therefore sees a changed epoch and, without the
    stamp, discards the very audio this call reserved to prevent dead air. That
    discard also resets `queue_empty_since` and the rescue ladder, so an
    operator tapping a silent Panic/Purge/Air-Next extends the silence instead
    of ending it. Stamping here rather than at each call site is deliberate:
    there is no correct way for a caller to opt out.

    The keyword list mirrors `_reserve_continuity_runway_unstamped` on purpose:
    a bare `**kwargs` pass-through erased keyword checking at every one of the
    ~22 live-control call sites, so a typo'd argument type-checked clean and
    raised at request time inside a control whose job is preventing dead air.
    """
    dropped = _reserve_continuity_runway_unstamped(
        app_state,
        state,
        config,
        replace_queue=replace_queue,
        discard_reason=discard_reason,
        excluded_paths=excluded_paths,
        excluded_track_keys=excluded_track_keys,
        outcome=outcome,
        minimum_runway_seconds=minimum_runway_seconds,
    )
    _stamp_continuity_runway_epoch(app_state.queue, state)
    return dropped


def _reserve_continuity_runway_unstamped(
    app_state,
    state: StationState,
    config,
    *,
    replace_queue: bool = False,
    discard_reason: str = GenerationWasteReason.OPERATOR_PURGE,
    excluded_paths: set[Path] | None = None,
    excluded_track_keys: set[tuple[str, str]] | None = None,
    outcome: ContinuityRunwayOutcome | None = None,
    minimum_runway_seconds: float = 0.0,
) -> int:
    """Reserve immediately playable runway before a live control mutates audio.

    Call `_reserve_continuity_runway` instead — it adds the required epoch stamp.

    The function has no await points.  It may discard only ordinary far-future
    queue items to make room; an existing protected reservation is reused.  A
    full queue with no ordinary tail retains its own audio and stores the short
    packaged clip in a capacity-exempt fallback slot.
    """
    from mammamiradio.scheduling.producer import RUNWAY_FLOOR_SECONDS

    q = app_state.queue
    excluded_paths = set(excluded_paths or ())
    excluded_track_keys = set(excluded_track_keys or ())
    # A live reservation's original source is hidden behind the shortened head
    # path. Exclude it (and canonical cache copies) even for ordinary, non-purge
    # runway additions while that head is active or its successor is due.
    for original_path, music in handoff_original_sources(state):
        excluded_paths.add(original_path)
        track_key = _segment_track_key(music)
        if track_key != ("", ""):
            excluded_track_keys.add(track_key)
    existing = list(getattr(q, "_queue", ()))
    current_queue = list(existing)
    protected = [seg for seg in existing if seg.metadata.get(_CONTINUITY_RESERVATION_FLAG)]
    ordinary = [seg for seg in existing if not seg.metadata.get(_CONTINUITY_RESERVATION_FLAG)]

    # NOTE (perf, deferred): each segment is measured here 2-3x per reservation
    # (ordinary/protected, then current_queue/combined), and the playability check
    # does a path.stat(). If Pi CPU shows up on the skip/remove/reserve path, memoize
    # this by id(segment) — its inputs (excluded_*, state.blocklist) don't mutate
    # mid-call, so a per-call cache dict is safe.
    def _ready_duration(segment: Segment) -> float:
        if not _segment_is_immediately_playable(
            state,
            segment,
            excluded_paths=excluded_paths,
            excluded_track_keys=excluded_track_keys,
        ):
            return 0.0
        return float(getattr(segment, "duration_sec", 0.0) or 0.0)

    slot = state.continuity_slot
    if slot is not None and not _segment_is_immediately_playable(
        state,
        slot,
        excluded_paths=excluded_paths,
        excluded_track_keys=excluded_track_keys,
    ):
        state.continuity_slot = None
        slot = None

    # Measure ordinary runway separately from the active protected set. This
    # prevents double-counting an existing reservation: the target is what the
    # protected queue members + capacity-exempt slot must cover together.
    ordinary_ready = 0.0 if replace_queue else buffered_audio_seconds(_ready_duration(segment) for segment in ordinary)
    # `minimum_runway_seconds` matters only when the configured floor is 0 (runway
    # replenishment disabled). Without it the target would also be 0, an empty
    # protected set would already "meet" it, and the caller would get no audio at
    # all — see ANY_PLAYABLE_RUNWAY_SECONDS at the Resume site.
    runway_floor = max(float(RUNWAY_FLOOR_SECONDS), minimum_runway_seconds)
    target = max(0.0, runway_floor - ordinary_ready)
    protected_ready = (
        0.0
        if replace_queue
        else buffered_audio_seconds(
            [
                *(_ready_duration(segment) for segment in protected),
                _continuity_slot_seconds(state),
            ]
        )
    )
    if not replace_queue and protected_ready >= target:
        return 0

    max_segments = q.maxsize if q.maxsize > 0 else None
    surviving_paths = {segment.path for segment in ordinary} if not replace_queue else set()
    surviving_track_keys = (
        {track_key for segment in ordinary if (track_key := _segment_track_key(segment)) != ("", "")}
        if not replace_queue
        else set()
    )
    reservation = _continuity_reservation_segments(
        state,
        config,
        target,
        max_segments=max_segments,
        # A non-replacing control keeps these ordinary segments in the real
        # queue. Never append the same cached bytes (or the same canonical song
        # through another cached path) as "recovery" behind them. In particular,
        # queue-remove may just have restored a handoff predecessor to its full
        # original path while ``last_music_file`` has advanced to a later song.
        excluded_paths=excluded_paths | surviving_paths,
        excluded_track_keys=excluded_track_keys | surviving_track_keys,
    )
    if not reservation:
        logger.warning("No packaged or cache continuity audio available for live control")
        if replace_queue:
            # Replacement is transactional with respect to ready audio. Keep
            # the first playable queued segment (plus any valid out-of-band
            # slot), but discard every other queued item so the producer has
            # capacity to recover. If neither is ready, mutate nothing: the
            # caller must not cut the current audio.
            playable_index = next(
                (
                    index
                    for index, segment in enumerate(current_queue)
                    if _segment_is_immediately_playable(
                        state,
                        segment,
                        excluded_paths=excluded_paths,
                        excluded_track_keys=excluded_track_keys,
                    )
                ),
                None,
            )
            playable_head = current_queue[playable_index] if playable_index is not None else None
            slot_ready = bool(
                state.continuity_slot is not None
                and _segment_is_immediately_playable(
                    state,
                    state.continuity_slot,
                    excluded_paths=excluded_paths,
                    excluded_track_keys=excluded_track_keys,
                )
            )
            if playable_head is None and not slot_ready:
                return 0
            if outcome is not None:
                outcome.preserved_existing = True
            survivors = [playable_head] if playable_head is not None else []
            failure_dropped = (
                current_queue[:playable_index] + current_queue[playable_index + 1 :]
                if playable_index is not None
                else current_queue
            )
            # This conservative rebuild is still a queue rewrite: a surviving
            # music head whose tail successor was just dropped must be restored
            # to its complete original song, and an orphaned successor must not
            # stay behind as a truncated outro fragment.
            for orphaned in reconcile_handoff_queue_items(state, survivors):
                if not any(existing is orphaned for existing in failure_dropped):
                    failure_dropped.append(orphaned)
            for segment in failure_dropped:
                state.record_discard(segment, reason=discard_reason, already_counted_in_produced=True)
                _drop_segment_moment_receipts(state, segment, discard_reason)
                _unlink_ephemeral_best_effort(segment)
            if failure_dropped:
                _rebuild_queue_shadow(q, state, survivors)
                # Queue capacity has changed, so producer work captured before
                # this conservative rebuild must not refill the freed tail.
                # Keep the epoch stable only for the true no-mutation path.
                state.continuity_epoch += 1
            elif slot_ready:
                # A capacity-exempt slot is an out-of-band runway, not a queued
                # tail. With no queued survivor, the previous queue-tail type is
                # no longer adjacent and must not supply a speech bed.
                state.last_enqueued_type = None
            return len(failure_dropped)
        return 0

    if outcome is not None and replace_queue:
        outcome.fresh_reservation = True

    if not replace_queue and protected_ready > 0:
        if q.maxsize > 0:
            non_evictable_count = sum(
                bool(segment.metadata.get("air_next")) or is_protected_handoff_successor(state, segment)
                for segment in ordinary
            )
            real_protected_capacity = max(0, q.maxsize - non_evictable_count)
            fresh_capacity = real_protected_capacity or 1  # one capacity-exempt slot
        else:
            fresh_capacity = len(reservation)
        fresh_ready = buffered_audio_seconds(_ready_duration(segment) for segment in reservation[:fresh_capacity])
        if protected_ready >= fresh_ready:
            # The active set is already the best runway currently feasible
            # without evicting a ready air-next item. Rebuilding an equivalent
            # partial set would only churn the continuity epoch.
            return 0

    dropped: list[Segment] = []
    planned_slot: Segment | None = None
    if replace_queue:
        dropped = existing
        existing = []
        # The replacement is fully built before the prior slot is superseded.
        state.continuity_slot = None
    else:
        existing = ordinary
    combined = existing + reservation
    while q.maxsize and len(combined) > q.maxsize:
        index = next(
            (
                idx
                for idx in range(len(existing) - 1, -1, -1)
                if not existing[idx].metadata.get(_CONTINUITY_RESERVATION_FLAG)
                and not existing[idx].metadata.get("air_next")
                and not is_protected_handoff_successor(state, existing[idx])
            ),
            None,
        )
        if index is None:
            break
        dropped.append(existing.pop(index))
        combined = existing + reservation

    if q.maxsize and len(combined) > q.maxsize:
        available_slots = max(0, q.maxsize - len(existing))
        if available_slots:
            # Some protected runway fits beside non-evictable air-next audio.
            # Keep that maximal prefix instead of discarding the whole set just
            # because every selected candidate cannot fit.
            reservation = reservation[:available_slots]
            combined = existing + reservation
        else:
            # Queue capacity is occupied entirely by air-next work. The current
            # queue remains audible; keep the single strongest candidate
            # out-of-band for the later empty transition instead of rejecting
            # the operator action. That candidate is a cached song whenever the
            # warm cache had one, and only falls back to the packaged clip.
            planned_slot = reservation[0]
            reservation = []
            combined = existing

    if not replace_queue:
        current_ready = buffered_audio_seconds(
            [
                *(_ready_duration(segment) for segment in current_queue),
                _continuity_slot_seconds(state),
            ]
        )
        planned_ready = buffered_audio_seconds(
            [
                *(_ready_duration(segment) for segment in combined),
                _ready_duration(planned_slot) if planned_slot is not None else 0.0,
            ]
        )
        if planned_ready <= current_ready:
            # Count-bound eviction must never trade a long, ready ordinary tail
            # for shorter safety audio. Preserve the real queue and add the
            # minimal candidate out-of-band instead; this is the maximal runway
            # available without weakening what listeners can already hear.
            # reservation[0] is the best candidate, not necessarily the clip.
            if state.continuity_slot is None:
                fallback_slot = planned_slot or reservation[0]
                if protected:
                    active_reservation_id = str(protected[0].metadata.get("continuity_reservation_id") or "")
                    if active_reservation_id:
                        fallback_slot.metadata["continuity_reservation_id"] = active_reservation_id
                state.continuity_slot = fallback_slot
                state.continuity_epoch += 1
            return 0
        state.continuity_slot = planned_slot

    # Only mutate private handoff ownership once this control has decided to
    # replace the real queue. A count-bound proposal that we abandon above must
    # leave its pair untouched; a committed rewrite restores an unstarted music
    # predecessor or folds an orphaned successor into the ordinary discard set.
    for orphaned in reconcile_handoff_queue_items(state, combined):
        if not any(existing is orphaned for existing in dropped):
            dropped.append(orphaned)
    for segment in dropped:
        state.record_discard(segment, reason=discard_reason, already_counted_in_produced=True)
        _drop_segment_moment_receipts(state, segment, discard_reason)
        _unlink_ephemeral_best_effort(segment)
    _rebuild_queue_shadow(q, state, combined)
    state.continuity_epoch += 1
    return len(dropped)


def _record_continuity_air(state: StationState, segment: Segment) -> None:
    """Report reserved safety audio as a rescue bridge once a LISTENER HAS IT.

    Three things this is deliberately not:

    * Not reservation-time. Every live control reserves safety audio before it
      mutates the queue, and most of it is never heard because the real queue
      refills first. Counting reservations trips ``BRIDGE_HEALTH_THRESHOLD``
      (2 per 30 min) after two ordinary admin actions and tells the operator a
      perfectly healthy station is "running on rescue". A false red is worse than
      the false green this was written to replace.
    * Not segment-start. That fires before ``open()``, so a vanished file or an
      instant skip would count as aired.
    * Not once per reserved track. One control can reserve several segments under
      one ``continuity_reservation_id``; they air back to back, and reporting each
      one would cross the threshold from a single operator action.

    Called from the send loop on the first chunk a listener queue accepted — the
    same "truly heard" predicate the rotation stamp uses. Best-effort: telemetry
    must never affect what airs.
    """
    try:
        metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
        if not metadata.get(_CONTINUITY_RESERVATION_FLAG):
            return
        reservation_id = str(metadata.get("continuity_reservation_id") or "")
        if reservation_id and reservation_id == state.last_continuity_air_reservation_id:
            return
        state.last_continuity_air_reservation_id = reservation_id
        source = str(metadata.get("audio_source") or "")
        if not source:
            source = "canned" if metadata.get("canned") else "unknown"
        state.record_bridge_fire("continuity", source)
        logger.info(
            "Continuity reservation on air: %s (%s)",
            metadata.get("title") or segment.path.name,
            source,
        )
    except Exception:  # pragma: no cover - telemetry must never break the stream
        logger.debug("continuity air telemetry failed", exc_info=True)


def _record_ad_experiment_receipt(
    state: StationState,
    segment: Segment,
    *,
    result: str,
    terminal_reason: str | None,
    all_chunks_audience_delivered: bool,
) -> None:
    """Credit brands after a non-fallback EOF when every ad chunk reached listeners."""
    try:
        if (
            segment.type is not SegmentType.AD
            or result != "aired"
            or terminal_reason != "eof"
            or not all_chunks_audience_delivered
        ):
            return
        metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
        raw_brands = metadata.get("brands")
        brands = (
            [brand for brand in raw_brands if isinstance(brand, str) and brand.strip()]
            if isinstance(raw_brands, list | tuple)
            else []
        )
        if not brands:
            brand = metadata.get("brand")
            brands = [brand] if isinstance(brand, str) else []
        state.record_completed_ad_break(brands)
    except Exception:  # pragma: no cover - isolate receipt errors from audio
        logger.debug("Carosello experiment receipt failed", exc_info=True)


# Floor of rotation tracks a BULK ban must leave behind. Below this the producer
# leans on the rescue path (demo assets / forced banter) — the emergency surface,
# not routine. A bulk ban that would cross the floor is rejected with a warm
# message rather than silently starving the station (leadership #1 + #5). A single
# per-row removal is never rejected — the operator asked for that one song gone.
MIN_ROTATION_AFTER_BAN = 5
# Legacy count ceiling for heading status and persisted compatibility. The actual
# steering bias is now persistent until Back to auto, source replacement, or failure.
HEADING_SELECTION_BUDGET_LIMIT = 10
# Wall-clock budget an all-new direction waits for its first track to actually
# land before it either confirms success or (if every download failed) rolls the
# course back to auto. On timeout the downloads keep going in the background and
# the course stays live — audio continuity wins (leadership #2).
DIRECTION_COMMIT_WAIT_SECONDS = 45
HEADING_SEEDS = {
    "classic://italian/70s": "Anni '70",
    "classic://italian/80s": "Anni '80",
    "classic://italian/90s": "Anni '90",
}


def _purge_home_fact_banter_from_queue(q, state: StationState, entity_ids: set[str]) -> int:
    """Remove unstarted banter tied to newly muted home entities.

    ``entity_ids`` is the tightened set: the muted id plus, in narrow mode, the
    synthetic ambient id(s) a break may be tagged with when its real HA source is
    muted. The current segment is no longer in ``q`` and deliberately finishes.
    Every removed queued segment travels through ``record_discard`` so its
    director reservation is released by the same central lifecycle boundary.
    """
    items = _drain_segment_queue(q)
    survivors: list = []
    dropped_ids: set[str] = set()
    for segment in items:
        metadata = getattr(segment, "metadata", {}) or {}
        if segment.type is SegmentType.BANTER and metadata.get("home_fact_entity_id") in entity_ids:
            queue_id = metadata.get("queue_id")
            if isinstance(queue_id, str):
                dropped_ids.add(queue_id)
            state.record_discard(
                segment,
                reason=GenerationWasteReason.OPERATOR_PURGE,
                already_counted_in_produced=True,
            )
            _drop_segment_moment_receipts(state, segment, GenerationWasteReason.OPERATOR_PURGE)
            _unlink_ephemeral_best_effort(segment)
            continue
        survivors.append(segment)
    # A muted home fact can be carried by a tail-bearing banter successor.  If
    # it is removed before the song starts, reconciliation restores the whole
    # predecessor instead of airing a shortened song with no tail.
    orphaned = reconcile_handoff_queue_items(state, survivors)
    orphaned_count = _discard_handoff_orphans(state, orphaned, GenerationWasteReason.OPERATOR_PURGE)
    if dropped_ids or orphaned_count:
        _rebuild_queue_shadow(q, state, survivors)
    else:
        for segment in survivors:
            q.put_nowait(segment)
    return len(dropped_ids) + orphaned_count


def _apply_ban(state: StationState, config, tracks: list, *, banned_by: str = "operator", queue=None) -> dict:
    """Ban tracks durably: persist, drop from rotation, clear pin, purge queue.

    Synchronous (no ``await``): the in-memory blocklist + playlist mutation and the
    disk persist happen in one stretch so concurrent ban/unban handlers cannot lose
    an update — the single-loop discipline the queue code already relies on. Returns
    ``{"ok", "banned": [display], "removed": int, "purged": int, "persisted": bool}``.
    """
    keys: dict[tuple[str, str], str] = {}
    for track in tracks:
        key = normalized_track_key(track)
        if key not in keys:
            keys[key] = getattr(track, "display", "") or ""
    if not keys:
        return {"ok": True, "banned": [], "removed": 0, "purged": 0}

    for key, display in keys.items():
        existing = state.blocklist.get(key)
        if existing is None:
            state.blocklist[key] = block_meta(display, banned_by=banned_by)
        elif display and not existing.get("display"):
            existing["display"] = display
    # Durability is best-effort: an unwritable/full cache dir means the ban holds
    # for this session but may not survive a restart. Surface that honestly rather
    # than promising "won't come back" (leadership #5) — the caller relays it.
    persisted = save_blocklist(config.cache_dir, state.blocklist)

    banned_keys = set(keys)
    before = len(state.playlist)
    removed_tracks = [t for t in state.playlist if normalized_track_key(t) in banned_keys]
    state.playlist = [t for t in state.playlist if normalized_track_key(t) not in banned_keys]
    removed = before - len(state.playlist)
    state.source_readiness.reconcile_active_tracks(state.playlist, removed_tracks=removed_tracks)
    pin_cleared = False
    if state.pinned_track is not None and normalized_track_key(state.pinned_track) in banned_keys:
        state.pinned_track = None
        pin_cleared = True
    if removed or pin_cleared:
        state.playlist_revision += 1

    def _matches_blocklist(segment: Segment) -> bool:
        # Shared definition, not a local copy. The hand-rolled tuple this
        # replaced used `metadata.get("artist", "")`, so a segment carrying an
        # explicit `artist: None` keyed as ("none", title) and slipped through
        # the purge - the canonical helper coalesces falsy to "".
        return segment.type is SegmentType.MUSIC and _segment_blocklist_key(segment) in banned_keys

    prior_tail = None
    if queue is not None:
        queued_before = list(getattr(queue, "_queue", ()))
        prior_tail = queued_before[-1] if queued_before else None
        purged = drop_matching_segments(
            queue,
            state,
            should_drop=_matches_blocklist,
            reason=GenerationWasteReason.OPERATOR_BAN,
        )
        if purged:
            _reconcile_queue_tail_adjacency(queue, state, prior_tail=prior_tail)
    else:
        purged = 0
    return {
        "ok": True,
        "banned": [state.blocklist[k].get("display") or f"{k[0]} - {k[1]}" for k in keys],
        "removed": removed,
        "purged": purged,
        "persisted": persisted,
    }


def _apply_unban(state: StationState, config, keys: list[tuple[str, str]]) -> dict:
    """Lift bans so the songs can return on the next fetch/refresh."""
    unbanned = 0
    for key in keys:
        if key in state.blocklist:
            del state.blocklist[key]
            unbanned += 1
    persisted = save_blocklist(config.cache_dir, state.blocklist) if unbanned else True
    return {"ok": True, "unbanned": unbanned, "persisted": persisted}


def _normalize_preference_key(raw_key: object) -> tuple[tuple[str, str], str] | None:
    if not isinstance(raw_key, list | tuple) or len(raw_key) != 2:
        return None
    artist_raw = str(raw_key[0] or "").strip()
    title_raw = str(raw_key[1] or "").strip()
    artist = artist_raw.lower()
    title = title_raw.lower()
    if not (artist and title):
        return None
    display = f"{artist_raw} - {title_raw}"
    return (artist, title), display


def _split_artist_title_label(value: object) -> tuple[str, str] | None:
    label = str(value or "").strip()
    parts = _re.split(r"\s[—–-]\s", label, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return None


def _fold_identity_part(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _now_playing_music_track(now_seg: object) -> Track | None:
    if not isinstance(now_seg, dict) or now_seg.get("type") != "music":
        return None
    meta = now_seg.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    artist = str(meta.get("artist") or "").strip()
    title = str(meta.get("title_only") or "").strip()
    raw_title = str(meta.get("title") or "").strip()
    if artist and not title and raw_title:
        parsed_title = _split_artist_title_label(raw_title)
        if parsed_title is None:
            title = raw_title
        else:
            left, right = parsed_title
            folded_artist = _fold_identity_part(artist)
            if _fold_identity_part(left) == folded_artist:
                title = right
            elif _fold_identity_part(right) == folded_artist:
                title = left
            else:
                title = raw_title
    if not (artist and title):
        parsed_label = _split_artist_title_label(now_seg.get("label"))
        if parsed_label is not None:
            artist, title = parsed_label
    if not (artist and title):
        return None
    return Track(title=title, artist=artist, duration_ms=0)


def _now_playing_preference_target(state: StationState) -> tuple[tuple[str, str], str] | None:
    track = _now_playing_music_track(state.now_streaming or {})
    if track is None:
        return None
    return normalized_track_key(track), track.display


def _resolve_preference_target(state: StationState, body: dict) -> tuple[tuple[str, str], str, str] | JSONResponse:
    raw_target = str(body.get("target") or "").strip().lower()
    legacy_now_playing = raw_target in {"current-song", "current_song", "now-playing", "now_playing"}
    target_count = (
        int(body.get("now_playing") is True or legacy_now_playing) + int("index" in body) + int("key" in body)
    )
    if target_count != 1:
        return JSONResponse(
            content={"ok": False, "error": "Choose exactly one preference target."},
            status_code=422,
        )

    if body.get("now_playing") is True or legacy_now_playing:
        target = _now_playing_preference_target(state)
        if target is None:
            return JSONResponse(
                content={"ok": False, "error": "Only a song can be marked — nothing musical is on air right now."}
            )
        key, display = target
        return key, display, "now_playing"

    if "index" in body:
        idx = _as_int_index(body.get("index"))
        if idx < 0 or idx >= len(state.playlist):
            return JSONResponse(content={"ok": False, "error": "Invalid song index."}, status_code=422)
        track = state.playlist[idx]
        return normalized_track_key(track), track.display, "index"

    target = _normalize_preference_key(body.get("key"))
    if target is None:
        return JSONResponse(content={"ok": False, "error": "Invalid song key."}, status_code=422)
    key, display = target
    return key, display, "key"


def _apply_preference(
    state: StationState,
    config,
    key: tuple[str, str],
    display: str,
    vote: str,
    target: str,
) -> dict:
    score_by_vote = {"up": 1, "down": -1, "clear": 0}
    score = score_by_vote[vote]
    updated_at = time.time()
    updated_by = "operator"
    existing = state.song_preferences.get(key)
    existing_score = preference_score(state.song_preferences, key)
    existing_display = str(existing.get("display") or "") if isinstance(existing, dict) else ""
    changed = False
    if score == 0:
        changed = clear_preference(state.song_preferences, key)
    else:
        changed = existing_score != score or existing_display != display
        if isinstance(existing, dict) and not changed:
            meta = existing
        else:
            meta = set_preference(state.song_preferences, key, score, display, updated_by=updated_by)
        raw_updated_at = meta.get("updated_at")
        if isinstance(raw_updated_at, str | int | float):
            updated_at = float(raw_updated_at)
        updated_by = str(meta.get("updated_by", updated_by) or updated_by)
    if changed:
        state.song_preferences_revision += 1
    persisted = save_preferences(config.cache_dir, state.song_preferences)
    return {
        "ok": True,
        "target": target,
        "key": list(key),
        "display": display,
        "vote": vote,
        "score": score,
        "updated_at": updated_at,
        "updated_by": updated_by,
        "persisted": persisted,
        "preference_revision": state.song_preferences_revision,
    }


def _serialize_preference_summary(state: StationState) -> dict:
    rows = []
    up = 0
    down = 0
    for key, meta in state.song_preferences.items():
        score = preference_score(state.song_preferences, key)
        if score > 0:
            up += 1
        elif score < 0:
            down += 1
        rows.append(
            {
                "artist": key[0],
                "title": key[1],
                "key": list(key),
                "score": score,
                "display": meta.get("display") or f"{key[0]} - {key[1]}",
                "updated_at": meta.get("updated_at", 0.0),
                "updated_by": meta.get("updated_by", "operator"),
            }
        )
    rows.sort(key=lambda row: row["updated_at"], reverse=True)
    return {
        "count": len(rows),
        "counts": {"up": up, "down": down},
        "revision": state.song_preferences_revision,
        "preferences": rows,
    }


def _serialize_preference_status(state: StationState) -> dict:
    return {
        "count": len(state.song_preferences),
        "revision": state.song_preferences_revision,
    }


def _current_track_preference_score(state: StationState) -> int:
    target = _now_playing_preference_target(state)
    if target is None:
        return 0
    key, _display = target
    return preference_score(state.song_preferences, key)


def _session_stopped_flag(config) -> Path:
    """Return the persisted operator-stop marker path."""
    return config.cache_dir / SESSION_STOPPED_FLAG


def _persist_session_stopped(config, stopped: bool) -> None:
    """Persist or clear the stopped-session marker."""
    flag = _session_stopped_flag(config)
    if stopped:
        config.cache_dir.mkdir(parents=True, exist_ok=True)
        flag.touch()
    else:
        flag.unlink(missing_ok=True)


def _now_streaming_has_selected_media(state: StationState) -> bool:
    """Return whether transport selected real timeline media."""
    now_streaming = state.now_streaming
    if not isinstance(now_streaming, dict):
        return False
    return str(now_streaming.get("type") or "") in {segment_type.value for segment_type in SegmentType}


def _now_streaming_is_real_media(state: StationState) -> bool:
    """Return whether selected timeline media reached a listener."""
    return state.current_stream_audible and _now_streaming_has_selected_media(state)


def _skip_is_in_flight(state: StationState) -> bool:
    """Return whether a skip has been requested but not yet landed.

    `_request_skip` publishes a `skipping` sentinel and returns; the sentinel
    only clears once the playback loop pulls and opens the next segment, which
    on an empty queue is the first-byte grace window plus the rescue ladder.
    Audio is still audibly playing throughout, so this state is neither "real
    media" nor "nothing is streaming" — it needs its own answer.
    """
    if state.skip_in_flight:
        return True
    now_streaming = state.now_streaming
    if not isinstance(now_streaming, dict):
        return False
    return str(now_streaming.get("type") or "") == "skipping"


def _clear_session_stopped(state: StationState) -> None:
    """Publish running state after the persisted stop marker is gone."""
    state.session_stopped = False
    if isinstance(state.now_streaming, dict) and state.now_streaming.get("type") == "stopped":
        state.now_streaming = {}
    state.current_stream_audible = False
    state.last_air_monotonic = None
    state.last_state_change_at = time.time()


async def _resume_station(app_state: Any) -> None:
    """Resume safely for the First Listen cast without blocking the event loop.

    Home Assistant playback invokes this callback before dispatching the media
    source. Mirror the admin Start gate: a stopped station only resumes after an
    immediately playable runway exists and the durable marker has been removed.
    """
    state: StationState = app_state.station_state
    config = app_state.config
    was_stopped = state.session_stopped
    if was_stopped:
        _reserve_continuity_runway(
            app_state,
            state,
            config,
            discard_reason=GenerationWasteReason.OPERATOR_STOP,
            minimum_runway_seconds=ANY_PLAYABLE_RUNWAY_SECONDS,
        )
        _discard_unplayable_queue_prefix(
            app_state.queue,
            state,
            reason=GenerationWasteReason.OPERATOR_STOP,
        )
        if not _playable_runway_available(app_state.queue, state):
            raise RuntimeError("no immediately playable runway")

    resume_epoch = state.continuity_epoch
    await asyncio.to_thread(_persist_session_stopped, config, False)
    # The off-loop marker write is intentionally interruptible. A Stop accepted
    # while it ran owns the newer epoch and must win both in memory and on disk.
    if state.continuity_epoch != resume_epoch:
        if state.session_stopped:
            _persist_session_stopped(config, True)
        raise RuntimeError("station control changed while resuming")
    if not was_stopped or not state.session_stopped:
        return
    _clear_session_stopped(state)
    state.force_recovery_active = False
    state.resume_event.set()


def _first_listen_store(app_state) -> FirstListenReceiptStore:
    store = getattr(app_state, "first_listen_store", None)
    if store is None:
        store = FirstListenReceiptStore(app_state.config.cache_dir)
        app_state.first_listen_store = store
    return store


async def _first_listen_audio_gate_open(app_state) -> bool:
    """Require audible proof before widening a fresh or unknown install.

    Proven pre-feature installs retain their compatibility path. Missing,
    delayed, or corrupt origin evidence is treated as unknown and therefore
    fails closed until an accepted playback has been audibly confirmed.
    """
    origin = getattr(app_state, "first_listen_install_origin", None)
    if getattr(origin, "status", None) is FirstListenInstallOriginStatus.EXISTING:
        return True
    receipt = getattr(app_state, "first_listen_receipt", None)
    if receipt is None:
        try:
            receipt = await _first_listen_store(app_state).load()
        except Exception:
            receipt = None
        app_state.first_listen_receipt = receipt
    return bool(getattr(receipt, "audio_complete", False))


def _ha_playback_access_snapshot(config) -> tuple[str, str, str]:
    """Return the exact HA access values and fingerprint used by playback."""
    access_url = config.homeassistant.url if config.homeassistant.enabled else ""
    access_token = config.ha_token if config.homeassistant.enabled else ""
    fingerprint = hashlib.sha256(f"{access_url}\0{access_token}".encode()).hexdigest()
    return access_url, access_token, fingerprint


def _ha_playback_service(app_state) -> HAPlaybackService:
    """Create the on-demand HA adapter without adding startup network work."""
    service = getattr(app_state, "ha_playback_service", None)
    config = app_state.config
    access_url, access_token, fingerprint = _ha_playback_access_snapshot(config)
    if service is not None:
        if getattr(app_state, "ha_playback_fingerprint", "") != fingerprint:
            service.configure(access_url, access_token)
            app_state.ha_playback_fingerprint = fingerprint
        return service

    async def _resume() -> None:
        await _resume_station(app_state)

    async def _persist_attempt(entity_id: str) -> str:
        receipt = await _first_listen_store(app_state).record_accepted(entity_id)
        app_state.first_listen_receipt = receipt
        if not receipt.accepted_attempt_id:  # defensive; the store contract supplies one
            raise FirstListenReceiptUnavailableError("accepted attempt id missing")
        return receipt.accepted_attempt_id

    service = HAPlaybackService(
        access_url,
        access_token,
        resume_station=_resume,
        persist_accepted_attempt=_persist_attempt,
    )
    app_state.ha_playback_service = service
    app_state.ha_playback_fingerprint = fingerprint
    return service


def _pending_receipt_recovery(app_state, *, service: object | None = None) -> dict[str, object]:
    """Return a sanitized, read-only projection of recoverable acceptance."""
    if service is None:
        service = getattr(app_state, "ha_playback_service", None)
    _access_url, _access_token, current_fingerprint = _ha_playback_access_snapshot(app_state.config)
    if getattr(app_state, "ha_playback_fingerprint", "") != current_fingerprint:
        return {"available": False, "entity_id": ""}
    projection = getattr(service, "pending_receipt_entity_id", None)
    if not callable(projection):
        return {"available": False, "entity_id": ""}
    try:
        entity_id = str(projection() or "")
    except Exception:
        logger.warning("Could not project pending First Listen receipt recovery", exc_info=True)
        entity_id = ""
    return {"available": bool(entity_id), "entity_id": entity_id}


def _sync_runtime_state(request: Request) -> None:
    """Refresh UI-facing state from long-lived runtime backends."""
    state = request.app.state.station_state
    state.runtime_sync_events += 1

    queue = getattr(request.app.state, "queue", None)
    if queue is None:
        return

    queue_depth = queue.qsize()
    shadow_depth = len(state.queued_segments)
    if shadow_depth > queue_depth:
        state.queued_segments = state.queued_segments[:queue_depth]
        state.shadow_queue_corrections += 1
        logger.warning(
            "Queue shadow drift corrected (shadow=%d, queue=%d)",
            shadow_depth,
            queue_depth,
        )


def _runtime_health_snapshot(request: Request) -> dict:
    state = request.app.state.station_state
    queue = getattr(request.app.state, "queue", None)
    queue_depth = queue.qsize() if queue else -1
    queue_capacity = queue.maxsize if queue else -1
    shadow_depth = len(state.queued_segments)
    now_streaming = state.now_streaming or {}
    now_metadata = now_streaming.get("metadata", {}) if isinstance(now_streaming, dict) else {}
    from mammamiradio.core.segment_status import is_fallback_active

    audio_source = now_metadata.get("audio_source", "")
    if audio_source == "fallback_norm_cache":
        audio_source = "norm_cache"
    fallback_active = is_fallback_active(now_metadata)
    if not audio_source and fallback_active:
        audio_source = "canned"
    if not audio_source or audio_source == "prewarm" or (audio_source == "download" and not fallback_active):
        bound_playlist_source = str(now_metadata.get(SEGMENT_PLAYLIST_SOURCE_KIND_KEY) or "")
        playlist_source = state.playlist_source
        if bound_playlist_source:
            audio_source = bound_playlist_source
        elif playlist_source is not None:
            audio_source = playlist_source.kind
    producer_task = getattr(request.app.state, "producer_task", None)
    playback_task = getattr(request.app.state, "playback_task", None)
    producer_alive = True if producer_task is None else not producer_task.done()
    playback_alive = True if playback_task is None else not playback_task.done()
    queue_empty_elapsed = _queue_empty_elapsed(state)
    return {
        "queue_depth": queue_depth,
        "queue_capacity": queue_capacity,
        "shadow_queue_depth": shadow_depth,
        "shadow_queue_in_sync": queue_depth == shadow_depth,
        "producer_task_alive": producer_alive,
        "playback_task_alive": playback_alive,
        "playback_epoch": state.playback_epoch,
        "queue_empty_since": state.queue_empty_since,
        "queue_empty_elapsed_s": round(queue_empty_elapsed, 1),
        "silence_with_listeners": _silence_with_listeners(state, queue_empty_elapsed),
        "audio_source": audio_source or "unknown",
        "failover_active": fallback_active,
        "shadow_queue_corrections": state.shadow_queue_corrections,
    }


def _runtime_provider_label(provider: str) -> str:
    labels = {
        "anthropic": "Anthropic",
        "openai": "OpenAI",
        "azure": "Azure Speech",
        "elevenlabs": "ElevenLabs",
        "mixed_tts": "Mixed TTS",
        "stock": "Stock copy",
        "edge": "Edge TTS",
        "silence": "Silence fallback",
        "norm_cache": "Norm cache rescue",
        "fallback_norm_cache": "Norm cache rescue",
        "fallback_demo_asset": "Demo asset rescue",
        "canned": "Canned clip",
        "charts": "Charts",
        "local": "Local music",
        "demo": "Demo music",
        "url": "Custom URL",
        "jamendo": "Jamendo",
        "stream": "Stream",
        "unknown": "Unknown",
    }
    return labels.get(provider, provider.replace("_", " ").title() if provider else "Unknown")


_FALLBACK_REASON_LABELS = {
    "anthropic_exception": "Anthropic had a brief API error - retrying automatically",
    "anthropic_max_tokens_truncated": "Anthropic ran long and got cut off - retrying automatically",
    "anthropic_auth_failed": "Anthropic API key rejected - check your key in Engine Room",
    "anthropic_auth_blocked": "Anthropic API key rejected - check your key in Engine Room",
    "anthropic_usage_limit": "Anthropic usage limit reached - check your plan at anthropic.com",
    "anthropic_usage_limit_blocked": "Anthropic usage limit reached - check your plan at anthropic.com",
    "anthropic_nonretryable": "Anthropic service error - check status.anthropic.com",
    "anthropic_transient": (
        "Anthropic is briefly overloaded — using the backup writer, it comes back on its own in a few seconds"
    ),
    "anthropic_transient_blocked": (
        "Anthropic is briefly overloaded — using the backup writer, it comes back on its own in a few seconds"
    ),
    "anthropic_absent": "No Anthropic key configured - running on OpenAI",
}
_ACTION_REQUIRED_FALLBACK_REASONS = {
    "anthropic_auth_failed",
    "anthropic_auth_blocked",
    "anthropic_usage_limit",
    "anthropic_usage_limit_blocked",
    "anthropic_nonretryable",
}


_TTS_RUNTIME_FALLBACK_PREFIX = "Runtime TTS fallback: "


def _tts_single_reason_label(reason: str) -> str:
    """Translate ONE engine's raw TTS fallback reason token into operator copy."""
    normalized = reason.strip().lower()
    if not normalized:
        return ""
    if "missing_credentials" in normalized:
        return "A cloud voice key is missing; Edge voice is carrying the show. Add the key and restart the station."
    if "provider_disabled:http 401" in normalized or "provider_disabled:http 403" in normalized:
        return "A cloud voice key was not accepted; check the saved key and restart the station."
    if "provider_disabled:http 404" in normalized:
        return "A cloud voice route is not available; check the selected voice and restart the station."
    if "cloud tts route rendered successfully" in normalized or "primary_success" in normalized:
        return "Cloud voice route is working."
    if "provider_cooldown" in normalized or "provider_error" in normalized:
        return "A cloud voice route had trouble; Edge voice is carrying the show and will retry automatically."
    if "provider_disabled_session" in normalized:
        return (
            "A cloud voice route is temporarily unavailable; Edge voice is carrying the show and will retry "
            "automatically."
        )
    if "edge_voice_failure" in normalized:
        return "The configured voice was unavailable; the station is trying its house voice."
    if "invalid_edge_voice" in normalized:
        return "The requested voice was unavailable; the station is using its house voice."
    if "unknown_engine" in normalized:
        return "A voice route is not configured; check the voice settings."
    return "A cloud voice route is unavailable; Edge voice is carrying the show."


def _tts_runtime_reason_label(reason: str) -> str:
    """Turn internal TTS route reasons into calm operator-facing copy.

    The raw reason remains in the runtime log (and in ``diagnostic_reason``
    for authenticated API consumers). The admin card should explain what the
    station is doing and what, if anything, the operator can do next.

    A multi-engine aggregate reason (``"Runtime TTS fallback: azure=X; elevenlabs=Y"``)
    is translated per engine and rejoined — translating the joined string as one
    blob would match only the first pattern in the if-chain and silently drop
    every other engine's status from the admin copy.
    """
    if not reason.strip():
        return ""
    if reason.startswith(_TTS_RUNTIME_FALLBACK_PREFIX):
        segments = reason[len(_TTS_RUNTIME_FALLBACK_PREFIX) :].split("; ")
        labeled = []
        for segment in segments:
            engine, _, token = segment.partition("=")
            engine = engine.strip()
            label = _tts_single_reason_label(token) if token else ""
            if engine and label:
                labeled.append(f"{engine}: {label}")
            elif label:
                labeled.append(label)
        if labeled:
            return "; ".join(labeled)
        return _tts_single_reason_label(reason)
    return _tts_single_reason_label(reason)


_PROVIDER_CLASS_LABELS = {
    "audio_source": "Audio source",
    "script_provider": "Script provider",
    "tts_provider": "Voice provider",
}


def _provider_class_label(provider_class: str) -> str:
    """Name a provider lane the way an operator would say it."""
    if provider_class.startswith("tts:"):
        return f"Voice provider ({_runtime_provider_label(provider_class[4:])})"
    return _PROVIDER_CLASS_LABELS.get(provider_class, provider_class.replace("_", " ").strip() or "Provider")


def _display_runtime_event(event) -> dict:
    """Project runtime events for the admin UI without losing diagnostics."""
    payload = event.to_dict()
    provider_class = str(payload.get("provider_class") or "")
    if provider_class == "script_provider":
        raw_reason = str(payload.get("reason") or "")
        payload["diagnostic_reason"] = raw_reason
        payload["reason"] = _script_runtime_reason_label(raw_reason)
    elif provider_class == "tts_provider" or provider_class.startswith("tts:"):
        raw_reason = str(payload.get("reason") or "")
        payload["diagnostic_reason"] = raw_reason
        payload["reason"] = _tts_runtime_reason_label(raw_reason)
    # The failover line is the last provider surface that still read as machine
    # output ("audio_source: charts → norm_cache"). Labels ride alongside the raw
    # keys so diagnostics keep working.
    payload["provider_class_label"] = _provider_class_label(provider_class)
    payload["from_provider_label"] = _runtime_provider_label(str(payload.get("from_provider") or ""))
    payload["to_provider_label"] = _runtime_provider_label(str(payload.get("to_provider") or ""))
    return payload


def _provider_status(
    provider_class: str,
    *,
    primary_provider: str,
    current_provider: str,
    fallback_active: bool,
    reason: str,
    state: StationState,
    recovery_mode: str | None = None,
    retry_in_seconds: int | None = None,
    action_guidance: str = "",
) -> dict:
    saved = state.runtime_provider_state.get(provider_class, {})
    historical_reason = saved.get("last_switch_reason")
    if historical_reason is None and saved.get("last_switch_timestamp") is not None:
        historical_reason = saved.get("reason")
    return {
        "provider_class": provider_class,
        "primary_provider": primary_provider,
        "primary_label": _runtime_provider_label(primary_provider),
        "current_provider": current_provider,
        "current_label": _runtime_provider_label(current_provider),
        "fallback_active": fallback_active,
        "last_switch_timestamp": saved.get("last_switch_timestamp") if saved else None,
        "current_reason": reason,
        "switch_reason": historical_reason or "",
        "recovery_mode": recovery_mode,
        "retry_in_seconds": retry_in_seconds,
        "action_guidance": action_guidance,
    }


def _listener_audible_provider_status(
    status: dict,
    state: StationState,
    *,
    station_on_air: bool,
    reason_formatter: Callable[[str], str] | None = None,
) -> dict:
    """Project only listener-audible provider truth into an admin snapshot.

    Script and TTS generation can run ahead of playback. Their latest observed
    route is useful internally, but showing it while another segment is on air
    (or while paused) recreates the exact selected-vs-audible lie this status
    surface exists to prevent.
    """
    provider_class = str(status.get("provider_class") or "")
    saved = state.runtime_provider_state.get(provider_class, {})
    audible_provider = str(saved.get("last_audible_provider") or "")
    if not audible_provider:
        return status

    latest_primary = str(status.get("primary_provider") or "")
    latest_current = str(status.get("current_provider") or "")
    latest_fallback = bool(status.get("fallback_active"))
    latest_reason = str(status.get("current_reason") or "")
    original_route = (latest_primary, latest_current, latest_fallback, latest_reason)
    latest_observation = {
        "primary_provider": latest_primary,
        "primary_label": str(status.get("primary_label") or ""),
        "current_provider": latest_current,
        "current_label": str(status.get("current_label") or ""),
        "fallback_active": latest_fallback,
        "current_reason": latest_reason,
        "recovery_mode": status.get("recovery_mode"),
        "retry_in_seconds": status.get("retry_in_seconds"),
        "action_guidance": str(status.get("action_guidance") or ""),
    }
    audible_fallback = bool(saved.get("last_audible_fallback_active"))
    audible_primary = str(saved.get("last_audible_primary_provider") or status.get("primary_provider") or "")
    audible_reason = str(saved.get("last_audible_reason") or "")
    if reason_formatter is not None and audible_reason:
        audible_reason = reason_formatter(audible_reason)

    status["primary_provider"] = audible_primary
    status["primary_label"] = _runtime_provider_label(audible_primary)
    status["current_provider"] = audible_provider
    status["current_label"] = _runtime_provider_label(audible_provider)
    status["fallback_active"] = audible_fallback
    if station_on_air:
        status["current_reason"] = audible_reason or "This provider produced the listener-audible segment."
    else:
        context = (
            "Last listener-audible provider; station is paused."
            if state.session_stopped
            else "Last listener-audible provider; no audio is currently on air."
        )
        status["current_reason"] = f"{context} Last observed reason: {audible_reason}" if audible_reason else context

    audible_route = (
        audible_primary,
        audible_provider,
        audible_fallback,
        audible_reason,
    )
    if original_route != audible_route:
        # Recovery metadata belongs to the newer generation observation, not
        # the listener-audible route projected above. Keep it in a separately
        # labeled observation instead of hiding an action-required failure.
        status["latest_observation"] = latest_observation
        status["recovery_mode"] = None
        status["retry_in_seconds"] = None
        status["action_guidance"] = ""
    return status


def _script_runtime_reason_label(reason: str) -> str:
    """Turn an internal script-provider reason into operator-facing copy.

    The sibling of :func:`_tts_runtime_reason_label`. Codes like
    ``anthropic_auth_failed`` are stored verbatim so logs stay diagnosable; this
    is what stands between that code and the control-room screen. Unmapped
    values degrade to spaced words rather than leaking underscores, and copy
    that is already human passes through untouched, so applying it twice is safe.
    """
    return _FALLBACK_REASON_LABELS.get(reason, reason.replace("_", " "))


def _script_provider_status(
    config,
    state: StationState,
    provider_health: dict,
    *,
    use_runtime_observation: bool = True,
) -> dict:
    anthropic_degraded = bool(provider_health.get("anthropic", {}).get("degraded"))
    saved = state.runtime_provider_state.get("script_provider", {}) if use_runtime_observation else {}
    if config.anthropic_api_key:
        primary = "anthropic"
        saved_current = str(saved.get("current_provider") or "")
        saved_fallback = bool(saved.get("fallback_active", False))
        if saved_current and (saved_fallback or saved_current != primary):
            current = saved_current
            fallback_active = saved_fallback or current != primary
            reason = saved.get("reason") or "Script provider fallback is active"
        elif anthropic_degraded and config.openai_api_key:
            current = "openai"
            fallback_active = True
            reason = (
                provider_health["anthropic"].get("last_error")
                or "Anthropic is suspended; OpenAI script fallback is active"
            )
        elif anthropic_degraded:
            current = "stock"
            fallback_active = True
            reason = (
                provider_health["anthropic"].get("last_error")
                or "Anthropic is suspended and no OpenAI key is available"
            )
        else:
            current = "anthropic"
            fallback_active = False
            reason = "Anthropic is the active script provider"
    elif config.openai_api_key:
        primary = current = "openai"
        fallback_active = False
        reason = "OpenAI is the configured script provider"
    else:
        primary = current = "stock"
        fallback_active = False
        reason = "No LLM provider configured; stock copy is active"
    fallback_reason = saved.get("reason") or reason
    recovery_mode: str | None = None
    retry_in_seconds: int | None = None
    action_guidance = ""
    if fallback_active:
        if state.anthropic_disabled_until > time.time():
            recovery_mode = "circuit_breaker"
            _r = provider_health.get("anthropic", {}).get("retry_after_s")
            retry_in_seconds = int(_r) if _r else None
            action_guidance = _FALLBACK_REASON_LABELS.get(fallback_reason, fallback_reason)
        elif fallback_reason in _ACTION_REQUIRED_FALLBACK_REASONS:
            recovery_mode = "action_required"
            action_guidance = _FALLBACK_REASON_LABELS[fallback_reason]
        else:
            recovery_mode = "transient"
            action_guidance = "No action needed - will retry automatically"
    status = _provider_status(
        "script_provider",
        primary_provider=primary,
        current_provider=current,
        fallback_active=fallback_active,
        reason=reason,
        state=state,
        recovery_mode=recovery_mode,
        retry_in_seconds=retry_in_seconds,
        action_guidance=action_guidance,
    )
    # Historical switch reasons are stored as raw provider codes
    # ("anthropic_auth_failed"); the operator screen only ever shows words.
    if status["switch_reason"]:
        status["switch_reason"] = _script_runtime_reason_label(str(status["switch_reason"]))
    if status["current_reason"]:
        status["current_reason"] = _script_runtime_reason_label(str(status["current_reason"]))
    return status


def _tts_provider_status(config, state: StationState, *, use_runtime_observation: bool = True) -> dict:
    engines = {(host.engine or "edge").strip().lower() for host in config.hosts}
    engines.update((voice.engine or "edge").strip().lower() for voice in config.ads.voices)
    if config.sonic_brand.sweeper_voice:
        engines.add((config.sonic_brand.sweeper_engine or "edge").strip().lower())
    cloud_engines = sorted(engine for engine in engines if engine != "edge")
    missing: list[str] = []

    if cloud_engines:
        primary = cloud_engines[0] if len(cloud_engines) == 1 else "mixed_tts"
        configured = {
            # OpenAI TTS also needs a registry-selected speech model. Without it
            # provider checks report model_routing_unavailable, so runtime status
            # must agree and treat the voice as falling back to Edge — otherwise
            # the two operator surfaces disagree.
            "openai": bool(config.openai_api_key and config.models.tts_model("openai")),
            "azure": bool(config.azure_speech_key and config.azure_speech_region),
            "elevenlabs": bool(config.elevenlabs_api_key),
        }
        missing = [engine for engine in cloud_engines if not configured.get(engine, False)]
        if missing and len(missing) == len(cloud_engines):
            current = "edge"
            fallback_active = True
            reason = f"TTS provider key missing for {', '.join(missing)}; Edge voice fallback is active"
        elif missing:
            current = primary
            fallback_active = True
            reason = f"Mixed TTS configured; {', '.join(missing)} voices are falling back to Edge"
        else:
            current = primary
            fallback_active = False
            reason = (
                "Mixed TTS voice providers are configured" if len(cloud_engines) > 1 else f"{primary} TTS is configured"
            )
    else:
        primary = current = "edge"
        fallback_active = False
        reason = "Edge TTS is the configured voice provider"

    # A configured key only proves that a cloud route can be attempted. The
    # synthesis boundary records the route that actually produced audio; use
    # that live state when it says a mixed/cloud route degraded to Edge.
    runtime_tts = state.runtime_provider_state.get("tts_provider", {}) if use_runtime_observation else {}
    if runtime_tts and runtime_tts.get("fallback_active"):
        current = str(runtime_tts.get("current_provider") or "edge")
        fallback_active = True
        reason = str(runtime_tts.get("reason") or "Runtime TTS fallback is active")
    elif runtime_tts and not missing:
        saved_current = str(runtime_tts.get("current_provider") or "")
        if saved_current and saved_current != primary:
            current = saved_current
            fallback_active = False
            reason = str(runtime_tts.get("reason") or reason)

    status = _provider_status(
        "tts_provider",
        primary_provider=primary,
        current_provider=current,
        fallback_active=fallback_active,
        reason=reason,
        state=state,
    )
    runtime_reason = str(runtime_tts.get("reason") or "")
    if status["switch_reason"]:
        status["switch_reason"] = _tts_runtime_reason_label(str(status["switch_reason"]))
    if runtime_reason and reason == runtime_reason:
        status["current_reason"] = _tts_runtime_reason_label(runtime_reason)
    return status


def _runtime_status_snapshot(
    request: Request,
    runtime_health: dict | None = None,
    provider_health: dict | None = None,
) -> dict:
    config = request.app.state.config
    state = request.app.state.station_state
    runtime_health = runtime_health or _runtime_health_snapshot(request)
    provider_health = provider_health or _provider_health_snapshot(config, state)

    tasks_alive = runtime_health.get("producer_task_alive", True) and runtime_health.get("playback_task_alive", True)
    silence_with_listeners = bool(runtime_health.get("silence_with_listeners", False))
    last_air = state.last_air_monotonic
    recent_handoff = bool(
        state.listeners_active > 0
        and last_air is not None
        and 0.0 <= _runtime_monotonic() - last_air <= STREAM_HANDOFF_GRACE_SECONDS
    )
    station_on_air = bool(
        tasks_alive
        and not silence_with_listeners
        and not state.session_stopped
        and (state.current_stream_audible or recent_handoff)
    )
    audible_now = bool(state.current_stream_audible)
    handoff_only = station_on_air and not audible_now

    saved_audio = state.runtime_provider_state.get("audio_source", {})
    selected_metadata = state.now_streaming.get("metadata", {}) if isinstance(state.now_streaming, dict) else {}
    if station_on_air and audible_now:
        audio_current = str(runtime_health.get("audio_source") or "unknown")
        if audio_current == "fallback_norm_cache":
            audio_current = "norm_cache"
        audio_fallback = bool(runtime_health.get("failover_active"))
        audio_reason = (
            str(selected_metadata.get("fallback_reason") or "Fallback audio is currently on air")
            if audio_fallback
            else "Primary audio source is on air"
        )
    else:
        audio_current = str(
            saved_audio.get("last_audible_provider") or saved_audio.get("current_provider") or "unknown"
        )
        if audio_current == "fallback_norm_cache":
            audio_current = "norm_cache"
        saved_audible_fallback = saved_audio.get("last_audible_fallback_active")
        audio_fallback = (
            bool(saved_audible_fallback)
            if saved_audible_fallback is not None
            else bool(saved_audio.get("fallback_active", False))
        )
        audio_reason = (
            "Last listener-audible provider; station is paused."
            if state.session_stopped
            else (
                "Last listener-audible provider; stream handoff is in progress."
                if handoff_only
                else "Last listener-audible provider; no audio is currently on air."
            )
        )
    if station_on_air and audible_now:
        bound_playlist_source = str(selected_metadata.get(SEGMENT_PLAYLIST_SOURCE_KIND_KEY) or "")
        audio_primary = bound_playlist_source or (
            state.playlist_source.kind
            if state.playlist_source is not None
            else str(saved_audio.get("primary_provider") or audio_current)
        )
    else:
        audio_primary = str(
            saved_audio.get("last_audible_primary_provider")
            or saved_audio.get("primary_provider")
            or (state.playlist_source.kind if state.playlist_source is not None and not handoff_only else "")
            or audio_current
        )
    audio_status = _provider_status(
        "audio_source",
        primary_provider=audio_primary or "unknown",
        current_provider=audio_current,
        fallback_active=audio_fallback,
        reason=audio_reason,
        state=state,
    )
    script_saved = state.runtime_provider_state.get("script_provider", {})
    tts_saved = state.runtime_provider_state.get("tts_provider", {})
    script_status = _script_provider_status(
        config,
        state,
        provider_health,
        # Generation-only observations must not become off-air display truth.
        # Once an audible baseline exists, use the live observation to retain
        # recovery guidance when it still describes that exact route; the
        # projection below removes it if a newer unheard route differs.
        use_runtime_observation=bool(script_saved.get("last_audible_provider")),
    )
    tts_status = _tts_provider_status(
        config,
        state,
        use_runtime_observation=bool(tts_saved.get("last_audible_provider")),
    )
    script_status = _listener_audible_provider_status(
        script_status,
        state,
        station_on_air=station_on_air,
        reason_formatter=_script_runtime_reason_label,
    )
    tts_status = _listener_audible_provider_status(
        tts_status,
        state,
        station_on_air=station_on_air,
        reason_formatter=_tts_runtime_reason_label,
    )
    providers = {
        "audio_source": audio_status,
        "script_provider": script_status,
        "tts_provider": tts_status,
    }
    fallback_active = any(item["fallback_active"] for item in providers.values())
    bridge_health = _bridge_health_snapshot(state)
    bridge_unhealthy = bool(bridge_health.get("unhealthy"))
    generation_waste = _generation_waste_snapshot(state, config.models)
    if not tasks_alive:
        health_state = "blocked"
        health_color = "red"
        health_explanation = "A runtime task is stopped; playback needs operator attention."
    elif state.session_stopped:
        # Check a deliberate operator pause BEFORE silence: /api/stop keeps the
        # tasks alive, so an empty queue with a listener still connected would
        # otherwise flip a paused station to the red "Error" state after the
        # silence window. A deliberate pause must read as "Paused", never "Error".
        health_state = "ready"
        health_color = "blue"
        health_explanation = "Station is paused by the operator."
    elif silence_with_listeners:
        # Silence outranks Force Start. Force Start is the recovery for exactly
        # this failure, so letting "recovering" mask it hid an unrecoverable
        # rebuild behind a benign yellow forever — the flag only clears once a
        # listener accepts audio, which is the very thing that is not happening.
        health_state = "blocked"
        health_color = "red"
        health_explanation = (
            "Force Start has not produced audio yet and listeners are waiting; playback needs operator attention."
            if state.force_recovery_active
            else "Listeners are connected but playback is silent; playback needs operator attention."
        )
    elif state.force_recovery_active:
        health_state = "degraded"
        health_color = "yellow"
        health_explanation = "Force Start is rebuilding the first listener-audible host break."
    elif bridge_unhealthy:
        health_state = "degraded"
        health_color = "yellow"
        health_explanation = "Queue rescue is firing often; the station is building more runway."
    elif fallback_active:
        health_state = "degraded"
        health_color = "yellow"
        active = [
            providers[name]["current_label"]
            for name in ("audio_source", "script_provider", "tts_provider")
            if providers[name]["fallback_active"]
        ]
        health_explanation = "Fallback active: " + ", ".join(active)
    else:
        health_state = "ready"
        health_color = "blue"
        health_explanation = "Primary providers are active."

    if state.runtime_health_state != health_state:
        state.runtime_health_state = health_state
        logger.info(
            "provider_health_state",
            extra={
                "event": "provider_health_state",
                "health_state": health_state,
                "fallback_active": fallback_active,
                "runtime_provider_classes": [
                    name
                    for name in ("audio_source", "script_provider", "tts_provider")
                    if providers[name]["fallback_active"]
                ],
            },
        )

    events_desc = list(reversed(state.runtime_events))
    recent_events = [_display_runtime_event(e) for e in events_desc[:10]]
    last_switch = recent_events[0] if recent_events else None
    failover_events = [_display_runtime_event(e) for e in events_desc if e.fallback_active][:10]
    return {
        "health_state": health_state,
        "health_color": health_color,
        "health_explanation": health_explanation,
        "station_on_air": station_on_air,
        "recovering": state.force_recovery_active,
        "fallback_active": fallback_active,
        "providers": providers,
        "last_switch_timestamp": last_switch.get("timestamp") if last_switch else None,
        "switch_reason": last_switch.get("reason") if last_switch else "",
        "recent_events": recent_events,
        "failover_events": failover_events,
        "no_failover_message": "No failover in current session." if not failover_events else "",
        "bridge_health": bridge_health,
        "generation_waste": generation_waste,
        # How the norm-cache rescue rotation is spending its cooldown. Authenticated
        # only, and derived purely from in-memory airplay state — no cache walk, no
        # filesystem paths — so an operator can see the illusion guard working.
        "rescue_rotation": _rescue_rotation_status(state),
        # Capacity-exempt continuity is intentionally not part of the real queue
        # shadow. Expose it separately to authenticated operators only.
        "continuity_slot": _continuity_slot_status(state),
        "producer_headroom": _producer_headroom_snapshot(request, runtime_health),
        "render_timings": {"retention": state.render_timings.maxlen or 20, "recent": list(state.render_timings)},
        "stream_delivery": state.stream_delivery_snapshot(),
    }


def _producer_headroom_snapshot(request: Request, runtime_health: dict) -> dict:
    """Best-effort producer runway status for Pi-sized render latency."""
    # Import at read time so tests and runtime tuning that patch the producer's
    # shared floor keep this admin diagnostic truthful instead of freezing a copy.
    from mammamiradio.scheduling.producer import RUNWAY_FLOOR_SECONDS

    config = request.app.state.config
    state = request.app.state.station_state
    queue = getattr(request.app.state, "queue", None)
    target_segments = max(4, int(config.pacing.lookahead_segments))
    queue_depth = int(runtime_health.get("queue_depth", 0))
    slot = state.continuity_slot
    slot_playable = slot is not None and _segment_is_immediately_playable(state, slot)
    slot_audio_sec = _continuity_slot_seconds(state, self_heal=False) if slot_playable else 0.0
    buffered_audio_sec = buffered_audio_seconds([_queued_audio_seconds(queue, state=state), slot_audio_sec])
    queue_capacity = int(runtime_health.get("queue_capacity", -1))
    headroom_ok = buffered_audio_sec >= RUNWAY_FLOOR_SECONDS and _playable_runway_available(
        queue, state, self_heal=False
    )
    return {
        "queue_depth": queue_depth,
        "queue_capacity": queue_capacity,
        "lookahead_target": target_segments,
        "buffered_audio_sec": buffered_audio_sec,
        "continuity_slot_sec": slot_audio_sec,
        "runway_floor_sec": RUNWAY_FLOOR_SECONDS,
        "headroom_ok": headroom_ok,
        "reason": "ready runway" if headroom_ok else "building runway",
    }


def _bridge_health_snapshot(state: StationState) -> dict:
    """Producer rescue-bridge health for the admin Runtime Status card (#547).

    Windows ``state.bridge_events`` to count recent PRODUCER bridge fires
    (drain/resume/idle); ``unhealthy`` trips once either repeated bridge fires or
    sustained queue-empty time indicate the station is "running on rescue".
    Session counts and queue-empty elapsed ride along so the operator sees one
    honest readout instead of a falsely-green card.

    ``continuity`` fires stay out of the rolling window. A live control reserved
    safety audio and it aired, which is the safety net covering an operator
    action rather than the producer falling behind. Counting them would trip
    ``BRIDGE_HEALTH_THRESHOLD`` (2 per 30 min) after two ordinary admin actions
    and tell the operator a healthy station is "running on rescue". They still
    ride along in ``session_count`` and ``by_type``; only the alarm is scoped.
    """
    now = time.time()
    window = BRIDGE_HEALTH_WINDOW_SECONDS
    recent = [
        e
        for e in state.bridge_events
        if now - float(e.get("timestamp") or 0.0) <= window and e.get("bridge_type") not in _NON_ALARMING_BRIDGE_TYPES
    ]
    last_fire = state.bridge_events[-1] if state.bridge_events else None
    # Compare on the RAW elapsed; round only for the payload. Rounding before the
    # threshold check let raw 59.95s round up to 60.0 and trip "queue_empty" ~0.05s
    # early. The other health readers (/healthz, /readyz, _runtime_health_snapshot)
    # already keep raw for logic and round only for display — this mirrors them.
    queue_empty_elapsed_raw = _queue_empty_elapsed(state)
    queue_empty_elapsed = round(queue_empty_elapsed_raw, 1)
    recent_unhealthy = len(recent) >= BRIDGE_HEALTH_THRESHOLD
    empty_unhealthy = queue_empty_elapsed_raw >= BRIDGE_HEALTH_QUEUE_EMPTY_THRESHOLD_SECONDS
    unhealthy_reasons = []
    if recent_unhealthy:
        unhealthy_reasons.append("bridge_frequency")
    if empty_unhealthy:
        unhealthy_reasons.append("queue_empty")
    return {
        "window_seconds": window,
        "threshold": BRIDGE_HEALTH_THRESHOLD,
        "queue_empty_window_seconds": BRIDGE_HEALTH_QUEUE_EMPTY_WINDOW_SECONDS,
        "queue_empty_threshold_s": BRIDGE_HEALTH_QUEUE_EMPTY_THRESHOLD_SECONDS,
        "session_count": state.bridge_fires_total,
        "by_type": dict(state.bridge_fires_by_type),
        "window_count": len(recent),
        "last_fire": dict(last_fire) if last_fire else None,
        "queue_empty_elapsed_s": queue_empty_elapsed,
        "unhealthy": bool(unhealthy_reasons),
        "unhealthy_reasons": unhealthy_reasons,
    }


def _generation_waste_snapshot(state: StationState, models: ModelsSection | None = None) -> dict:
    """Generated segment waste for the admin Runtime Status card (#397).

    Windows ``state.discard_events`` to surface recent pre-air drops and prorates
    session API/TTS spend across discarded vs produced segment counts. The
    ``cost_basis`` string carries the formula's known imprecision (count-based
    proration over-attributes cost to discarded music).
    """
    now = time.time()
    window = GENERATION_WASTE_WINDOW_SECONDS
    recent = [e for e in state.discard_events if now - float(e.get("timestamp") or 0.0) <= window]
    recent_segments = len(recent)
    # Compare the RAW sum against the degraded threshold; rounding before the
    # comparison shifts the boundary (e.g. 119.96s would round to 120.0 and trip
    # the gate early). Round only the value returned in the payload (#397).
    recent_duration_raw = sum(float(e.get("duration_sec") or 0.0) for e in recent)
    reason_counts: dict[str, int] = {}
    for event in recent:
        reason = str(event.get("reason") or "")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    recent_top_reason = max(reason_counts, key=lambda k: reason_counts[k]) if reason_counts else ""
    session_cost, _ = _estimate_api_cost(state, models)
    produced_plus_discarded = max(1, state.segments_produced + state.discarded_unproduced_segments_total)
    if state.discarded_segments_total:
        # Clamp at session_cost: you cannot waste more than you spent. A burst of
        # already-counted discards (queue purges/bans) can push the count-based
        # ratio above 1.0 and overstate wasted spend on the operator card — an
        # operator-honesty break (#5). The clamp makes the upper bound structural.
        raw_waste = session_cost * state.discarded_segments_total / produced_plus_discarded
        waste_cost = round(min(raw_waste, session_cost), 4)
    else:
        waste_cost = 0.0
    degraded = (
        recent_duration_raw >= GENERATION_WASTE_DEGRADED_SECONDS or recent_segments >= GENERATION_WASTE_DEGRADED_COUNT
    )
    cost_basis = (
        "Rough estimate: session API+TTS cost prorated by discarded segment count "
        f"over segments produced plus discarded ({state.discarded_segments_total} discarded, "
        f"{state.segments_produced} produced, "
        f"{state.discarded_unproduced_segments_total} discarded before the produced counter). "
        "Count-based proration over-attributes cost "
        "to discarded music (which carries little AI/TTS spend)."
    )
    return {
        "total_segments": state.discarded_segments_total,
        "total_duration_sec": round(state.discarded_duration_total_sec, 1),
        "unproduced_segments": state.discarded_unproduced_segments_total,
        "window_seconds": window,
        "recent_segments": recent_segments,
        "recent_duration_sec": round(recent_duration_raw, 1),
        "by_reason": dict(state.discard_by_reason),
        "by_type": dict(state.discard_by_type),
        "recent_top_reason": recent_top_reason,
        "estimated_waste_cost_usd": waste_cost,
        "cost_basis": cost_basis,
        "degraded": degraded,
    }


def _status_is_presence_eligible(status: Mapping[str, object]) -> bool:
    """Return whether a status dict is a room-scoped presence-consent candidate.

    Single source for the personal-moment eligibility predicate used by the
    admin preview rows and the PATCH gate, so they cannot drift apart.
    """
    return bool(
        status.get("domain") == "binary_sensor"
        and status.get("device_class") in PRESENCE_SENSOR_DEVICE_CLASSES
        and status.get("area")
    )


def _safe_home_entity_preview(state: StationState, config) -> dict:
    """Return admin-only, sanitized HA context candidates for onboarding."""
    policy = load_entity_policy(config.cache_dir)
    muted_map = policy.get("muted", {}) if isinstance(policy.get("muted"), dict) else {}
    muted_ids = set(muted_map)
    personal_moment_ids = personal_moment_opt_in_entity_ids(config.cache_dir)
    ctx = get_cached_home_context(config.cache_dir, authorization=state.home_authorization)
    rows: dict[str, dict] = {}
    if ctx is not None:
        for entity in getattr(ctx, "scored", [])[:24]:
            status = entity.to_status_dict()
            entity_id = str(status.get("entity_id") or "")
            if not entity_id:
                continue
            stale_after = max(float(config.homeassistant.poll_interval) * 2, 120.0)
            personal_moment_eligible = _status_is_presence_eligible(status)
            rows[entity_id] = {
                "entity_id": entity_id,
                "label": status.get("label") or entity_id,
                "area": status.get("area") or "",
                "domain": status.get("domain") or entity_id.split(".", 1)[0],
                "state_summary": status.get("summary") or str(status.get("state") or ""),
                "reason": "Used by future host prompts" if entity_id not in muted_ids else "Muted by operator",
                "muted": entity_id in muted_ids,
                "sent_to_prompt": entity_id not in muted_ids,
                "row_state": "muted" if entity_id in muted_ids else "used_by_hosts",
                "personal_moment_eligible": personal_moment_eligible,
                "personal_moment_enabled": entity_id in personal_moment_ids,
                "personal_moment_effective": personal_moment_eligible
                and entity_id in personal_moment_ids
                and entity_id not in muted_ids,
                "last_updated": getattr(ctx, "timestamp", 0.0) or None,
                "stale": bool(getattr(ctx, "age_seconds", 0.0) > stale_after),
            }
    for status in state.ha_scored_entities[:24]:
        entity_id = str(status.get("entity_id") or "")
        if not entity_id or entity_id in rows:
            continue
        personal_moment_eligible = _status_is_presence_eligible(status)
        rows[entity_id] = {
            "entity_id": entity_id,
            "label": status.get("label") or entity_id,
            "area": status.get("area") or "",
            "domain": status.get("domain") or entity_id.split(".", 1)[0],
            "state_summary": status.get("summary") or str(status.get("state") or ""),
            "reason": "Used by future host prompts" if entity_id not in muted_ids else "Muted by operator",
            "muted": entity_id in muted_ids,
            "sent_to_prompt": entity_id not in muted_ids,
            "row_state": "muted" if entity_id in muted_ids else "used_by_hosts",
            "personal_moment_eligible": personal_moment_eligible,
            "personal_moment_enabled": entity_id in personal_moment_ids,
            "personal_moment_effective": personal_moment_eligible
            and entity_id in personal_moment_ids
            and entity_id not in muted_ids,
            "last_updated": state.ha_context_last_updated or None,
            "stale": False,
        }
    for entity_id, entry in muted_map.items():
        if not isinstance(entry, dict) or not isinstance(entity_id, str):
            continue
        rows.setdefault(
            entity_id,
            {
                "entity_id": entity_id,
                "label": entry.get("label") or entity_id,
                "area": entry.get("area") or "",
                "domain": entry.get("domain") or entity_id.split(".", 1)[0],
                "state_summary": "Muted locally",
                "reason": "Muted by operator",
                "muted": True,
                "sent_to_prompt": False,
                "row_state": "muted",
                "personal_moment_eligible": False,
                "personal_moment_enabled": False,
                "personal_moment_effective": False,
                "last_updated": entry.get("muted_at"),
                "stale": False,
            },
        )
    sent_now = [row for row in rows.values() if row["sent_to_prompt"] and not row["muted"]]
    muted = [row for row in rows.values() if row["muted"]]
    candidates = [row for row in rows.values() if not row["sent_to_prompt"] and not row["muted"]]
    entities = [*sent_now[:24], *candidates[:24], *muted[:24]]
    has_reviewable_context = bool(ctx is not None or state.ha_scored_entities or muted)
    preview_status = "ready" if sent_now else "empty" if has_reviewable_context else "checking"
    return {
        "ok": True,
        "status": preview_status,
        "entities": entities[:32],
        "sent_now": sent_now[:12],
        "candidates": candidates[:12],
        "muted": muted[:24],
        "counts": {
            "sent_now": len(sent_now),
            "used_by_hosts": len(sent_now),
            "candidates": len(candidates),
            "not_sent": len(candidates),
            "muted": len(muted),
            "filtered": sum((getattr(ctx, "denylist_hits", {}) or {}).values()) if ctx is not None else 0,
        },
    }


def _safe_detached_home_context_preview(context, config) -> dict:
    """Project a fresh preview without implying that any row reached a host."""
    policy = load_entity_policy(config.cache_dir)
    muted_map = policy.get("muted", {}) if isinstance(policy.get("muted"), dict) else {}
    personal_moment_ids = personal_moment_opt_in_entity_ids(config.cache_dir)
    rows: list[dict[str, Any]] = []
    for entity in list(getattr(context, "scored", []) or [])[:24]:
        status = entity.to_status_dict()
        entity_id = str(status.get("entity_id") or "")
        if not entity_id:
            continue
        personal_moment_eligible = _status_is_presence_eligible(status)
        personal_moment_enabled = personal_moment_eligible and entity_id in personal_moment_ids
        rows.append(
            {
                "entity_id": entity_id,
                "label": status.get("label") or entity_id,
                "area": status.get("area") or "",
                "domain": status.get("domain") or entity_id.split(".", 1)[0],
                "state_summary": status.get("summary") or str(status.get("state") or ""),
                "reason": "Preview only — eligible for future host scripts if you enable it",
                "muted": False,
                "sent_to_prompt": False,
                "row_state": "preview_only",
                "personal_moment_eligible": personal_moment_eligible,
                "personal_moment_enabled": personal_moment_enabled,
                "personal_moment_effective": personal_moment_enabled,
                "last_updated": getattr(context, "timestamp", 0.0) or None,
                "stale": False,
            }
        )
    visible_ids = {str(row["entity_id"]) for row in rows}
    for entity_id, entry in list(muted_map.items())[:24]:
        if entity_id in visible_ids or not isinstance(entity_id, str) or not isinstance(entry, dict):
            continue
        rows.append(
            {
                "entity_id": entity_id,
                "label": entry.get("label") or entity_id,
                "area": entry.get("area") or "",
                "domain": entry.get("domain") or entity_id.split(".", 1)[0],
                "state_summary": "Muted locally",
                "reason": "Muted by operator",
                "muted": True,
                "sent_to_prompt": False,
                "row_state": "muted",
                "personal_moment_eligible": False,
                "personal_moment_enabled": False,
                "personal_moment_effective": False,
                "last_updated": entry.get("muted_at"),
                "stale": False,
            }
        )
    preview_rows = [row for row in rows if not row["muted"]]
    muted_rows = [row for row in rows if row["muted"]]
    context_value = classify_home_context_entity_ids(row["entity_id"] for row in preview_rows)
    useful_context = context_value == "useful"
    ambient_count = sum(row["entity_id"] in LOW_VALUE_AMBIENT_ENTITY_IDS for row in preview_rows)
    useful_count = len(preview_rows) - ambient_count
    return {
        "ok": True,
        "fresh": True,
        "status": context_value if context_value != "useful" else "ready",
        "context_value": context_value,
        "useful_context": useful_context,
        "authorization_mode": str(getattr(context, "authorization_mode", "narrow") or "narrow"),
        "entities": rows[:32],
        "sent_now": [],
        "candidates": preview_rows[:24],
        "muted": muted_rows[:24],
        "counts": {
            "sent_now": 0,
            "used_by_hosts": 0,
            "candidates": len(preview_rows),
            "not_sent": len(preview_rows),
            "muted": len(muted_rows),
            "useful": useful_count,
            "ambient_basic": ambient_count,
            "filtered": sum((getattr(context, "denylist_hits", {}) or {}).values()),
        },
    }


async def _persist_home_context_choice(config, enabled: bool) -> None:
    if config.is_addon:
        await asyncio.to_thread(_save_addon_option_batch, {"ha_context_enabled": enabled})
    else:
        await asyncio.to_thread(
            _save_dotenv,
            {"MAMMAMIRADIO_HA_CONTEXT_ENABLED": "true" if enabled else "false"},
        )


def _home_context_preview_proof_valid(app_state, proof: object) -> bool:
    if not isinstance(proof, _HomeContextPreviewProof):
        return False
    state = app_state.station_state
    authorization = state.home_authorization or HomeAuthorization.narrow()
    return bool(
        proof.expires_at > time.monotonic()
        and proof.config_fingerprint == _home_access_fingerprint(app_state.config)
        and proof.authorization_mode == authorization.mode.value
        and proof.policy_revision == policy_revision(app_state.config.cache_dir)
        and proof.context_generation == getattr(state, "home_context_policy_generation", 0)
    )


async def _fetch_home_context_preview_singleflight(app_state, authorization: HomeAuthorization):
    """Share one bounded detached preview for an unchanged privacy policy."""
    lock = getattr(app_state, "_home_context_preview_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app_state._home_context_preview_lock = lock
    config = app_state.config
    state = app_state.station_state
    key = (
        _home_access_fingerprint(config),
        authorization.mode.value,
        policy_revision(config.cache_dir),
        getattr(state, "home_context_policy_generation", 0),
    )
    async with lock:
        task = getattr(app_state, "_home_context_preview_task", None)
        task_key = getattr(app_state, "_home_context_preview_task_key", None)
        if task is None or task.done() or task_key != key:
            if task is not None and not task.done():
                task.cancel()
            task = asyncio.create_task(
                fetch_home_context_preview(
                    config.homeassistant.url,
                    config.ha_token,
                    cache_dir=config.cache_dir,
                    authorization=authorization,
                ),
                name="home-context-privacy-preview",
            )
            app_state._home_context_preview_task = task
            app_state._home_context_preview_task_key = key
            app_state._home_context_preview_task_deadline = time.monotonic() + _HOME_PREVIEW_TOTAL_TIMEOUT_SECONDS
            _register_background_task(app_state, task)
        deadline = float(
            getattr(
                app_state,
                "_home_context_preview_task_deadline",
                time.monotonic() + _HOME_PREVIEW_TOTAL_TIMEOUT_SECONDS,
            )
        )
    remaining = max(0.001, deadline - time.monotonic())
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
    except TimeoutError:
        # Do not start another worker behind a still-running projection. A later
        # operator action joins this same bounded task until it completes.
        return None
    finally:
        if task.done():
            async with lock:
                if getattr(app_state, "_home_context_preview_task", None) is task:
                    app_state._home_context_preview_task = None
                    app_state._home_context_preview_task_key = None
                    app_state._home_context_preview_task_deadline = 0.0


def _purge_global_home_segments(queue, state: StationState) -> int:
    """Drop every unstarted generated break carrying Home-context provenance."""

    def _uses_home_context(segment: Segment) -> bool:
        if segment.type not in {SegmentType.BANTER, SegmentType.AD, SegmentType.NEWS_FLASH}:
            return False
        metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
        return any(
            key in metadata
            for key in (
                "home_context_generation",
                "home_fact_id",
                "home_fact_entity_id",
                "ritual_moment_id",
                "gag_moment_id",
            )
        )

    return drop_matching_segments(
        queue,
        state,
        should_drop=_uses_home_context,
        reason=GenerationWasteReason.OPERATOR_PURGE,
    )


def _retire_pending_home_interrupt(state: StationState) -> bool:
    """Remove an unstarted Home-owned interrupt without touching operator work.

    Blank or unknown provenance fails closed as Home-owned — the same rule
    that tags the bridge at ``_fire_interrupt`` — so no pending interrupt can
    be tagged by one predicate and skipped by another.
    """
    from mammamiradio.scheduling.producer import _home_owned_directive_source

    if state.interrupt_slot is None and not state.interrupt_slot_source:
        # Nothing is pending; leave unrelated chaos/forced-banter state alone.
        return False
    source = str(state.interrupt_slot_source or "")
    if not _home_owned_directive_source(source):
        return False
    bridge_path = state.interrupt_slot
    if bridge_path is not None and state.interrupt_slot_ephemeral:
        _unlink_ephemeral_best_effort(
            Segment(
                type=SegmentType.BANTER,
                path=bridge_path,
                metadata={"interrupt": True},
                ephemeral=True,
            )
        )
    state.interrupt_slot = None
    state.interrupt_slot_ephemeral = False
    state.interrupt_slot_source = ""
    state.interrupt_slot_home_context_generation = None
    if state.chaos_pending is ChaosSubtype.URGENT_INTERRUPT:
        state.chaos_pending = None
    if state.force_next is SegmentType.BANTER and state.operator_force_pending is None:
        state.force_next = None
    return True


async def _disable_home_context_runtime(app_state) -> int:
    """Apply immediate, global privacy revocation before reporting persistence."""
    config = app_state.config
    state = app_state.station_state
    config.homeassistant.context_enabled = False
    state.home_context_policy_generation = getattr(state, "home_context_policy_generation", 0) + 1
    # Invalidate/cancel post-air Home-derived memory tasks synchronously.  The
    # bounded drain happens below, but the epoch must advance before this
    # privacy cutover yields control for the first time.
    revoked_memory_tasks = revoke_home_memory_extractions()
    # Detached label generation needs the same synchronous cutover. Its epoch
    # advances here; the bounded drain can safely happen after state is blank.
    revoked_label_tasks = invalidate_label_generation()
    coordinator = getattr(state, "ha_context_refresh_mailbox", None)
    suspend = getattr(coordinator, "suspend", None)
    if callable(suspend):
        try:
            suspend()
        except Exception:
            logger.warning("Home-context refresh suspension failed during revocation")
    app_state.home_context_preview_proof = None
    _retire_pending_home_interrupt(state)
    # Scene naming is a detached Home-derived Anthropic call. Its existing
    # reset seam cancels the task and clears task identity synchronously, so a
    # cancellation-resistant completion cannot repopulate the cache.
    reset_scene_namer_cache()
    # Prompt-facing state and Home-owned handoffs must disappear before the
    # first await. A new producer iteration can run while the bounded cleanup
    # drains, and it must see an empty Home surface immediately.
    ledger = _clear_global_home_context_runtime_state(state)
    # Purge synchronously before the first await.  Once the runtime flag and
    # generation change, no older queued Home-derived segment gets a scheduling
    # turn while coordinator cancellation or disk cleanup is in progress.
    purged = _purge_global_home_segments(app_state.queue, state)

    # Drain the coordinator before the slower detached-work drains. Production
    # coordinators were already suspended synchronously above; the await only
    # retires their cancelled request. Legacy/mocked coordinators without a
    # suspend seam still execute revoke here before any other cleanup await.
    revoke = getattr(coordinator, "revoke", None)
    if callable(revoke):
        try:
            outcome = revoke()
            if asyncio.iscoroutine(outcome):
                await outcome
        except Exception:
            logger.warning("Home-context refresh cancellation failed during revocation")

    try:
        await drain_revoked_home_memory_extractions(revoked_memory_tasks)
    except Exception:
        # The synchronous epoch advance above is the durable write fence even
        # if best-effort task draining itself fails.
        logger.warning("Home memory-extraction cancellation failed during revocation")

    try:
        await drain_invalidated_label_generation(revoked_label_tasks)
    except Exception:
        # The catalog epoch advances before its bounded task wait, so even an
        # unexpected cancellation-cleanup failure cannot publish an old result.
        logger.warning("Home label-generation cancellation failed during revocation")

    try:
        await asyncio.to_thread(invalidate_all_home_context, config.cache_dir)
    except Exception:
        logger.warning("Home-context cache cleanup failed during revocation")
    if ledger is not None:
        try:
            await asyncio.to_thread(ledger.save_if_dirty, config.cache_dir)
        except Exception:
            logger.warning("Home-context running-gag cleanup could not be saved")
    return purged


def _enable_home_context_runtime(app_state) -> None:
    config = app_state.config
    state = app_state.station_state
    config.homeassistant.context_enabled = True
    state.ha_context_refresh_stage = "idle"
    coordinator = getattr(state, "ha_context_refresh_mailbox", None)
    enable = getattr(coordinator, "enable", None)
    if callable(enable):
        enable()


def _home_entity_metadata(state: StationState, config, entity_id: str) -> dict[str, str]:
    """Resolve best-effort display metadata without depending on preview shape."""
    ctx = get_cached_home_context(config.cache_dir, authorization=state.home_authorization)
    if ctx is not None:
        for entity in getattr(ctx, "scored", []):
            status = entity.to_status_dict()
            if status.get("entity_id") == entity_id:
                return {
                    "label": str(status.get("label") or entity_id),
                    "domain": str(status.get("domain") or entity_id.split(".", 1)[0]),
                    "area": str(status.get("area") or ""),
                }
    for status in state.ha_scored_entities:
        if status.get("entity_id") == entity_id:
            return {
                "label": str(status.get("label") or entity_id),
                "domain": str(status.get("domain") or entity_id.split(".", 1)[0]),
                "area": str(status.get("area") or ""),
            }
    return {"label": entity_id, "domain": entity_id.split(".", 1)[0], "area": ""}


def _personal_moment_entity_is_eligible(state: StationState, config, entity_id: str) -> bool:
    """Allow consent only for a current, room-scoped presence candidate."""
    ctx = get_cached_home_context(config.cache_dir, authorization=state.home_authorization)
    candidates = list(getattr(ctx, "scored", []) or []) if ctx is not None else []
    for entity in candidates:
        status = entity.to_status_dict()
        if status.get("entity_id") != entity_id:
            continue
        return _status_is_presence_eligible(status)
    for status in state.ha_scored_entities:
        if status.get("entity_id") != entity_id:
            continue
        return _status_is_presence_eligible(status)
    return False


def _copy_home_context_to_state(state: StationState, context) -> None:
    events = list(getattr(context, "events", []) or [])
    newest_event = max(events, key=lambda event: event.timestamp) if events else None
    state.ha_context = str(getattr(context, "summary", "") or "")
    state.ha_events_summary = str(getattr(context, "events_summary", "") or "")
    state.ha_home_mood = str(getattr(context, "mood", "") or "")
    state.ha_weather_arc = str(getattr(context, "weather_arc", "") or "")
    state.ha_recent_event_count = len(events)
    state.ha_last_event_label = str(getattr(newest_event, "label", "") or "") if newest_event else ""
    state.ha_last_event_ts = float(getattr(newest_event, "timestamp", 0.0) or 0.0) if newest_event else 0.0
    state.ha_home_mood_en = str(getattr(context, "mood_en", "") or "")
    state.ha_weather_arc_en = str(getattr(context, "weather_arc_en", "") or "")
    state.ha_events_summary_en = str(getattr(context, "events_summary_en", "") or "")
    state.ha_last_event_label_en = str(getattr(context, "last_event_label_en", "") or "")
    state.ha_scored_entities = [entity.to_status_dict() for entity in getattr(context, "scored", [])]
    state.ha_denylist_hits = dict(getattr(context, "denylist_hits", {}) or {})
    state.ha_catalog_hit_rate = float(getattr(context, "catalog_hit_rate", 0.0) or 0.0)
    state.ha_label_stats = dict(getattr(context, "label_stats", {}) or {})
    state.ha_registry_source = str(getattr(context, "registry_source", "") or "")
    timestamp = getattr(context, "timestamp", 0.0)
    state.ha_context_last_updated = timestamp if isinstance(timestamp, int | float) else 0.0
    state.ha_context_entity_count = len(getattr(context, "scored", []) or [])
    state.ha_context_char_count = len(state.ha_context)


def _blank_home_context_state(state: StationState) -> None:
    state.ha_context = ""
    state.ha_events_summary = ""
    state.ha_home_mood = ""
    state.ha_weather_arc = ""
    state.ha_recent_event_count = 0
    state.ha_last_event_label = ""
    state.ha_last_event_ts = 0.0
    state.ha_home_mood_en = ""
    state.ha_weather_arc_en = ""
    state.ha_events_summary_en = ""
    state.ha_last_event_label_en = ""
    state.ha_scored_entities = []
    state.ha_denylist_hits = {}
    state.ha_catalog_hit_rate = 0.0
    state.ha_label_stats = {}
    state.ha_registry_source = ""
    state.ha_context_entity_count = 0
    state.ha_context_char_count = 0


def _clear_global_home_context_runtime_state(state: StationState):
    """Synchronously remove every prompt/public Home owner at global off."""
    moment_store = getattr(state, "moment_store", None)
    if moment_store is not None:
        try:
            for moment_id in (
                state.ha_pending_directive_moment_id,
                state.ha_running_gag_moment_id,
                state.last_banter_ritual_moment_id,
            ):
                if moment_id:
                    moment_store.mark_dropped(moment_id, "context_disabled")
        except Exception:  # pragma: no cover - receipts cannot weaken revocation
            logger.debug("Moment receipt global-off drop failed", exc_info=True)

    from mammamiradio.scheduling.producer import _home_owned_directive_source

    directive_source = str(state.ha_pending_directive_source or "")
    if _home_owned_directive_source(directive_source):
        state.ha_pending_directive = ""
        state.ha_pending_directive_moment_id = ""
        state.ha_pending_directive_source = ""
    state.ha_running_gag = ""
    state.ha_running_gag_key = ""
    state.ha_running_gag_moment_id = ""
    state.last_banter_ritual_moment_id = ""
    state.last_banter_home_fact = None
    state.last_banter_return_authority = None
    _blank_home_context_state(state)
    state.ha_context_last_updated = 0.0
    state.ha_ritual_context = ""
    state.ha_ritual_public_families = []
    state.ha_ritual_matches = []
    state.ha_ritual_recipe_audit = []
    state.ha_first_home_context_moment_fired = False
    state.home_context_director = HomeContextDirector()
    state.ha_context_refresh_in_flight = False
    state.ha_context_refresh_active_foreground_timed_out = False
    state.ha_context_refresh_configured = False
    state.ha_context_refresh_stale = False
    state.ha_context_refresh_stage = "disabled"

    ledger = state.evening_ledger
    if ledger is not None:
        ledger.buckets.clear()
        ledger._dirty = True
    return ledger


def _set_live_gag_entity_denied(state: StationState, config, entity_id: str, muted: bool) -> bool:
    ledger = state.evening_ledger
    if ledger is None:
        return False
    denylist = set(ledger.entity_denylist)
    if muted:
        denylist.add(entity_id)
    elif entity_id not in set(getattr(config.running_gags, "entity_denylist", []) or []):
        denylist.discard(entity_id)
    ledger.entity_denylist = frozenset(denylist)
    return ledger.purge_entity(entity_id) if muted else False


def _clear_home_context_usage(state: StationState, config, entity_id: str | None = None) -> bool:
    """Clear prompt-facing HA fields after a hard mute policy change.

    Returns True when the evening running-gag ledger was also purged and
    needs `save_if_dirty()` — the caller owns persistence so this stays a
    plain in-memory mutator (no synchronous disk I/O in an async route).
    """
    context = get_cached_home_context(config.cache_dir, authorization=state.home_authorization)
    if context is not None:
        _copy_home_context_to_state(state, context)
    else:
        _blank_home_context_state(state)
    # These transient strings have no durable entity id in StationState, so a live
    # mute clears them even when the rest of the filtered context can be preserved.
    # Their elected Moment Receipt rows are demoted honestly at the same time —
    # otherwise the admin trail would show "waiting for its break" for up to a
    # week about a moment the operator just muted.
    _moment_store = getattr(state, "moment_store", None)
    if _moment_store is not None:
        try:
            for _moment_id in (
                state.ha_pending_directive_moment_id,
                state.ha_running_gag_moment_id,
                # A directive already consumed into an in-flight generation
                # parks its id in the handoff slot — the mute kills that
                # receipt too, or the muted moment would still earn "aired".
                state.last_banter_ritual_moment_id,
            ):
                if _moment_id:
                    _moment_store.mark_dropped(_moment_id, "muted")
        except Exception:  # pragma: no cover - receipts must never break a mute
            logger.debug("Moment receipt mute drop failed", exc_info=True)
    state.ha_pending_directive = ""
    state.ha_pending_directive_moment_id = ""
    state.ha_pending_directive_source = ""
    state.ha_running_gag = ""
    state.ha_running_gag_key = ""
    state.ha_running_gag_moment_id = ""
    state.last_banter_ritual_moment_id = ""
    if entity_id and state.evening_ledger is not None:
        return _set_live_gag_entity_denied(state, config, entity_id, True)
    return False


def _runtime_monotonic() -> float:
    """Monotonic clock for readiness and silence accounting."""
    return time.monotonic()


def _setup_projection(request: Request, *, force_refresh: bool = False) -> dict[str, Any]:
    """Build the shared onboarding snapshot used by setup and capability routes."""
    _sync_runtime_state(request)
    config = request.app.state.config
    state = request.app.state.station_state
    golden_path = _golden_path_status(config, state, force_refresh=force_refresh)
    provider_health = _provider_health_snapshot(config, state)
    origin = getattr(request.app.state, "first_listen_install_origin", None)
    origin_status = getattr(getattr(origin, "status", None), "value", None) or "unknown"
    setup = build_setup_status(
        config,
        state,
        golden_path=golden_path,
        provider_health=provider_health,
        first_listen_receipt=getattr(request.app.state, "first_listen_receipt", None),
        install_origin=str(origin_status),
        context_choice_explicit=bool(getattr(request.app.state, "home_context_choice_explicit", True)),
    )
    setup["guided_setup"]["first_listen"]["bootstrap_ready"] = _first_listen_bootstrap_ready(request.app.state)
    setup["guided_setup"]["first_listen"]["receipt_recovery"] = _pending_receipt_recovery(request.app.state)
    return {
        "config": config,
        "state": state,
        "golden_path": golden_path,
        "provider_health": provider_health,
        "setup": setup,
    }


_FIRST_LISTEN_BOOTSTRAP_WAIT_SECONDS = 0.25


def _first_listen_bootstrap_tasks(app_state) -> tuple[asyncio.Future, ...]:
    """Return the non-blocking install-origin and receipt bootstrap tasks."""
    tasks = (
        getattr(app_state, "first_listen_origin_task", None),
        getattr(app_state, "first_listen_receipt_task", None),
    )
    return tuple(task for task in tasks if isinstance(task, asyncio.Future))


def _first_listen_bootstrap_ready(app_state) -> bool:
    """Report whether First Listen origin and receipt state is authoritative."""
    if bool(getattr(app_state, "first_listen_bootstrap_snapshot_authoritative", False)):
        # Small embedded/test apps may inject both facts synchronously.  This
        # opt-in is deliberately separate from task wiring so an empty task
        # tuple can never become ready through all([]).
        return True
    if not bool(getattr(app_state, "first_listen_bootstrap_wired", False)):
        return False
    origin_task = getattr(app_state, "first_listen_origin_task", None)
    receipt_task = getattr(app_state, "first_listen_receipt_task", None)
    origin_future = origin_task if isinstance(origin_task, asyncio.Future) else None
    receipt_future = receipt_task if isinstance(receipt_task, asyncio.Future) else None
    if origin_future is None or receipt_future is None:
        return False
    return origin_future.done() and receipt_future.done()


async def _wait_for_first_listen_bootstrap(app_state) -> bool:
    """Briefly join already-running setup reads without delaying audio startup."""
    pending = [task for task in _first_listen_bootstrap_tasks(app_state) if not task.done()]
    if pending:
        await asyncio.wait(pending, timeout=_FIRST_LISTEN_BOOTSTRAP_WAIT_SECONDS)
    return _first_listen_bootstrap_ready(app_state)


def _first_listen_entry_state(app_state) -> str:
    """Choose the first admin surface from authoritative in-memory facts only."""
    if not _first_listen_bootstrap_ready(app_state):
        return "pending"
    origin = getattr(app_state, "first_listen_install_origin", None)
    origin_status = getattr(getattr(origin, "status", None), "value", None) or "unknown"
    if origin_status == FirstListenInstallOriginStatus.EXISTING.value:
        return "complete"
    if origin_status not in {
        FirstListenInstallOriginStatus.FRESH.value,
        FirstListenInstallOriginStatus.UNKNOWN.value,
    }:
        return "complete"
    receipt = getattr(app_state, "first_listen_receipt", None)
    complete = bool(getattr(receipt, "audio_complete", False)) and bool(getattr(receipt, "privacy_complete", False))
    return "complete" if complete else "required"


def _queue_empty_elapsed(state: StationState) -> float:
    return _runtime_monotonic() - state.queue_empty_since if state.queue_empty_since is not None else 0.0


def _silence_with_listeners(state: StationState, queue_empty_elapsed: float) -> bool:
    """True only when listeners are connected and nothing is airing.

    queue_empty_since intentionally keeps running while continuity clips
    bridge an empty queue (the rescue ladder escalates on it), so an empty
    queue alone is not silence: a station audibly looping its bridge clip on
    a fresh install must not trip the add-on watchdog mid-first-render. Real
    dead air means the playback loop also stopped starting segments.
    """
    if queue_empty_elapsed <= SILENCE_FAILURE_SECONDS or state.listeners_active <= 0:
        return False
    if state.last_air_monotonic is None:
        return True
    return _runtime_monotonic() - state.last_air_monotonic > SILENCE_FAILURE_SECONDS


def _provider_health_snapshot(config, state: StationState) -> dict:
    """Return current provider degradation state for admin diagnostics."""
    now = time.time()
    anthropic_configured = bool(config.anthropic_api_key)
    anthropic_degraded = anthropic_configured and state.anthropic_disabled_until > now
    retry_after = max(0, int(state.anthropic_disabled_until - now)) if anthropic_degraded else 0
    return {
        "anthropic": {
            "configured": anthropic_configured,
            "degraded": anthropic_degraded,
            "retry_after_s": retry_after,
            "last_error": state.anthropic_last_error if anthropic_degraded else "",
            "auth_failures": state.anthropic_auth_failures,
            "key_status": state.anthropic_key_status,
        },
        "openai": {
            "configured": bool(config.openai_api_key),
            "key_status": state.openai_key_status,
        },
        "azure_speech": {
            "configured": bool(config.azure_speech_key and config.azure_speech_region),
        },
        "elevenlabs": {
            "configured": bool(config.elevenlabs_api_key),
        },
        "chaos": {
            "enabled": state.chaos_mode_active,
            "pending": state.chaos_pending.value if state.chaos_pending else "",
            "script_fallbacks": state.chaos_script_fallbacks,
            "audio_failures": state.chaos_audio_failures,
            "last_degraded_reason": state.chaos_last_degraded_reason,
        },
    }


def _apply_loaded_source(
    request,
    tracks: list,
    resolved_source,
    *,
    captured_continuity_epoch: int | None = None,
) -> dict:
    """Atomically swap the station source and cut over only to fresh runway audio."""
    state = request.app.state.station_state
    q = request.app.state.queue
    captured_epoch = state.continuity_epoch if captured_continuity_epoch is None else captured_continuity_epoch

    # Doorway: a banned song must not return when the operator switches sources.
    tracks = filter_blocklisted(tracks, state.blocklist)
    metadata_only = state.session_stopped or captured_epoch != state.continuity_epoch

    if metadata_only:
        # Stop never waits for source I/O. If this load crossed a stop/control
        # epoch, admit metadata only. The request does not own transport state
        # created after its captured epoch, so preserve the queue, its shadow,
        # and every protected slot exactly as they are.
        preserved_segments = len(getattr(q, "_queue", ()))
        # `switch_playlist()` intentionally clears old-source transport intent
        # during an ordinary cutover. This request is stale, though: controls
        # visible on the current continuity epoch belong to a newer timeline and
        # must survive the metadata commit. Snapshot and restore them without an
        # await so no concurrent control can interleave with the handoff.
        preserved_pending_actions = list(state.pending_actions)
        preserved_pinned_track = state.pinned_track
        preserved_force_next = state.force_next
        preserved_operator_force_pending = state.operator_force_pending
        state.switch_playlist(tracks, resolved_source)
        state.pending_actions.extend(preserved_pending_actions)
        state.pinned_track = preserved_pinned_track
        state.force_next = preserved_force_next
        state.operator_force_pending = preserved_operator_force_pending
        _delete_persisted_heading(request.app.state.config.cache_dir)
        logger.info(
            "Loaded source metadata only kind=%s tracks=%d captured_epoch=%d current_epoch=%d "
            "session_stopped=%s preserved_segments=%d",
            resolved_source.kind,
            len(state.playlist),
            captured_epoch,
            state.continuity_epoch,
            state.session_stopped,
            preserved_segments,
            extra={
                "event": "source_load_metadata_only",
                "source_kind": resolved_source.kind,
                "tracks": len(state.playlist),
                "captured_continuity_epoch": captured_epoch,
                "continuity_epoch": state.continuity_epoch,
                "session_stopped": state.session_stopped,
                "preserved_segments": preserved_segments,
            },
        )
        return {
            "ok": True,
            "source": _serialize_source(resolved_source),
            "preview": _preview_tracks(tracks),
            "tracks": len(state.playlist),
            "skipped": False,
            "metadata_only": True,
            "resume_required": state.session_stopped,
        }

    # The queue replacement and its reservation happen before the source
    # revision changes, so an in-flight render cannot win the tiny gap between
    # a destructive purge and safety audio admission.
    runway = ContinuityRunwayOutcome()
    handoff_excluded_paths: set[Path] = set()
    handoff_excluded_track_keys: set[tuple[str, str]] = set()
    _cancel_active_handoff_from_queue(
        request.app.state.queue,
        state,
        reason=GenerationWasteReason.SOURCE_SWITCH,
        excluded_paths=handoff_excluded_paths,
        excluded_track_keys=handoff_excluded_track_keys,
    )
    purged = _reserve_continuity_runway(
        request.app.state,
        state,
        request.app.state.config,
        replace_queue=True,
        discard_reason=GenerationWasteReason.SOURCE_SWITCH,
        outcome=runway,
        excluded_paths=handoff_excluded_paths,
        excluded_track_keys=handoff_excluded_track_keys,
    )
    state.switch_playlist(tracks, resolved_source)
    jamendo_provider = getattr(request.app.state, "jamendo_provider", None)
    invalidate_jamendo = getattr(jamendo_provider, "invalidate_source", None)
    if callable(invalidate_jamendo):
        task = asyncio.create_task(invalidate_jamendo())
        _register_background_task(request.app.state, task)
    _delete_persisted_heading(request.app.state.config.cache_dir)

    # Immediate cutover is safe only when this action admitted fresh protected
    # audio. An assetless fallback may preserve an old-source queue head or slot;
    # that runway prevents dead air but must not make the source switch cut over
    # into audio from the prior source.
    skipped = False
    if (
        _now_streaming_has_selected_media(state)
        and runway.fresh_reservation
        and _playable_runway_available(request.app.state.queue, state)
    ):
        request.app.state.skip_event.set()
        skipped = True
    elif _now_streaming_has_selected_media(state):
        logger.warning(
            "Source changed without fresh cutover audio; current audio will finish (preserved=%s)",
            runway.preserved_existing,
        )

    logger.info(
        "Loaded source %s: %s (%d tracks), purged %d queued segments%s",
        resolved_source.kind,
        resolved_source.label or "unnamed",
        len(tracks),
        purged,
        ", skipped current segment" if skipped else "",
    )

    return {
        "ok": True,
        "source": _serialize_source(resolved_source),
        "preview": _preview_tracks(tracks),
        "tracks": len(state.playlist),
        "skipped": skipped,
        "metadata_only": False,
        "resume_required": False,
    }


def _delete_persisted_heading(cache_dir: Path) -> bool:
    """Remove the heading overlay from cache; return whether it is gone."""
    try:
        (cache_dir / PERSISTED_HEADING_FILENAME).unlink(missing_ok=True)
        return True
    except OSError:
        logger.warning("Failed to clear persisted heading", exc_info=True)
        return False


def _delete_persisted_source(cache_dir: Path) -> bool:
    """Remove the selected playlist source from cache; return whether it is gone."""
    try:
        (cache_dir / PERSISTED_SOURCE_FILENAME).unlink(missing_ok=True)
        return True
    except OSError:
        logger.warning("Failed to clear persisted playlist source", exc_info=True)
        return False


def _preview_tracks(tracks: list, limit: int = 3) -> dict:
    return {
        "track_count": len(tracks),
        "tracks": [{"title": track.title, "artist": track.artist} for track in tracks[:limit]],
    }


def _source_options_reason(config, exc: Exception) -> str:
    return f"Source loading failed: {exc}"


class LiveStreamHub:
    """Fan out live audio chunks to all connected listener streams."""

    def __init__(self, listener_queue_size: int = 128):
        self._listener_queue_size = listener_queue_size
        self._listeners: dict[int, asyncio.Queue[bytes | None]] = {}
        self._next_listener_id = 0
        # Advances only when an empty room gets its next listener.  The
        # playback loop uses this to spot a disconnect/reconnect that happens
        # while it is still reading the current segment, rather than letting a
        # new listener inherit an old pacing origin.
        self._delivery_generation = 0
        self._state: StationState | None = None
        # Set by subscribe() so the playback loop wakes the instant a listener
        # connects to an empty room, instead of sleeping out a fixed poll. Bound
        # to the loop lazily on first await/set (asyncio.Event, 3.10+).
        self._listener_arrived = asyncio.Event()

    def bind_state(self, state: StationState) -> None:
        """Attach station state for listener tracking. Call once at startup."""
        if self._listeners:
            raise RuntimeError("LiveStreamHub cannot bind state after listeners are connected")
        self._state = state

    def subscribe(self) -> tuple[int, asyncio.Queue[bytes | None]]:
        """Register a listener and return its dedicated chunk queue."""
        room_was_empty = not self._listeners
        listener_id = self._next_listener_id
        self._next_listener_id += 1
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=self._listener_queue_size)
        self._listeners[listener_id] = queue
        if room_was_empty:
            self._delivery_generation += 1
        active = len(self._listeners)
        logger.info("Listener connected (%d active)", active)
        if self._state is not None:
            transition = self._state.listener_session.observe_active_count(active)
            self._state.listeners_active = active
            self._state.listeners_total += 1
            self._state.listeners_peak = max(self._state.listeners_peak, active)
            if transition is not None:
                logger.info(
                    "Listener session %s (epoch=%d, active=%d)",
                    transition.kind.value,
                    transition.epoch,
                    active,
                )
        # Wake a playback loop parked on the empty-room wait. The dict insert
        # above happens-before this set(), so the loop's check-clear-recheck
        # sees the new listener and never misses the wakeup.
        self._listener_arrived.set()
        return listener_id, queue

    @property
    def delivery_generation(self) -> int:
        """Return the current empty-room-to-listener generation.

        It is intentionally a coarse counter, not a listener identity: it
        tells playback that a fresh room needs a fresh send-ahead cushion while
        retaining no per-listener diagnostic state.
        """
        return self._delivery_generation

    def unsubscribe(self, listener_id: int) -> None:
        """Remove a listener and drop any future broadcast work for it."""
        if self._listeners.pop(listener_id, None) is not None:
            active = len(self._listeners)
            logger.info("Listener disconnected (%d active)", active)
            if self._state is not None:
                self._state.listener_session.observe_active_count(active)
                self._state.listeners_active = active

    def has_listener(self, listener_id: int) -> bool:
        """Return whether a listener is still subscribed."""
        return listener_id in self._listeners

    async def broadcast(self, chunk: bytes) -> int:
        """Push one encoded chunk and return how many listener queues accepted it."""
        slow_listeners = []
        accepted = 0
        for listener_id, queue in list(self._listeners.items()):
            try:
                queue.put_nowait(chunk)
                accepted += 1
            except asyncio.QueueFull:
                slow_listeners.append(listener_id)

        for listener_id in slow_listeners:
            logger.warning("Dropping slow listener after stream queue overflow")
            self.unsubscribe(listener_id)
        if slow_listeners and self._state is not None:
            self._state.record_slow_listener_drops(len(slow_listeners))
        return accepted

    def close(self) -> None:
        """Signal all listeners to terminate and clear the hub."""
        listeners = list(self._listeners.items())
        self._listeners.clear()
        if self._state is not None:
            self._state.listener_session.observe_active_count(0)
            self._state.listeners_active = 0
        for _, queue in listeners:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass


# Packaged clips are read-only shipped assets, so a probed duration is stable
# for the process lifetime. Caching it keeps rescue re-serves ffprobe-free.
_packaged_clip_duration_cache: dict[Path, float] = {}


def _consume_queue_shadow(segment_queue: asyncio.Queue[Segment], state: StationState, segment: Segment) -> None:
    """Remove a pulled segment from the admin shadow by identity, reconciling drift.

    The common path verifies and removes the head in O(1).  A mismatch is a
    correctness signal, not permission to pop an unrelated row: rebuild the
    projection from the bounded real queue while preserving known row metadata.
    This function has no await points, so producer and playback cannot interleave
    during the repair.
    """

    metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
    expected_id = str(metadata.get("queue_id") or "")
    shadow = state.queued_segments
    if shadow and expected_id and str(shadow[0].get("id") or "") == expected_id:
        shadow.pop(0)
        return

    if not shadow and not expected_id:
        return

    actual_remaining = list(getattr(segment_queue, "_queue", ()))
    prior_rows = {str(row.get("id")): row for row in shadow if row.get("id")}
    from mammamiradio.scheduling.producer import _queue_shadow_entry

    state.queued_segments = [
        prior_rows.get(str(item.metadata.get("queue_id") or "")) or _queue_shadow_entry(item)
        for item in actual_remaining
    ]
    logger.warning(
        "Queue shadow drift repaired while consuming queue_id=%s (shadow=%d, real=%d)",
        expected_id or "missing",
        len(shadow),
        len(actual_remaining),
    )


def _start_stream_segment(
    app,
    state: StationState,
    config,
    segment: Segment,
    ha_push_tasks: set[asyncio.Task],
) -> int:
    """Publish selected/readable state after the first non-empty file read."""

    playback_epoch = state.on_stream_segment_selected(segment)
    logger.info(
        "Selected readable %s segment for playback (epoch=%d): %s",
        segment.type.value,
        playback_epoch,
        segment.metadata.get("title", segment.metadata),
    )

    if config.homeassistant.enabled and config.ha_token and config.homeassistant.url:
        task = asyncio.create_task(
            push_state_to_ha(
                ha_url=config.homeassistant.url,
                ha_token=config.ha_token,
                now_streaming=copy.deepcopy(state.now_streaming),
                current_track=state.current_track,
                listeners_active=state.listeners_active,
                session_stopped=state.session_stopped,
                queue_depth=len(state.queued_segments),
                station_name=config.display_station_name,
                artwork_url=config.brand.artwork_url,
            )
        )
        ha_push_tasks.add(task)
        task.add_done_callback(ha_push_tasks.discard)
    return playback_epoch


def _commit_audible_stream_segment(
    state: StationState,
    segment: Segment,
    *,
    accepted_listeners: int,
) -> bool:
    """Commit listener-audible state exactly once for one selected segment."""

    # Hold the old objects themselves. Integer ids can be reused as the
    # maxlen deque evicts entries during a multi-provider commit.
    previous_provider_events = tuple(state.runtime_events)
    if not state.on_stream_segment_audible(segment):
        return False

    state.last_air_monotonic = _runtime_monotonic()
    for provider_event in state.runtime_events:
        if all(provider_event is not previous for previous in previous_provider_events):
            logger.info("provider_switch_event", extra=provider_event.to_dict())

    provider_state = state.runtime_provider_state.get("audio_source", {})
    provider = str(
        provider_state.get("current_provider")
        or segment.metadata.get("audio_source")
        or (state.playlist_source.kind if state.playlist_source is not None else "stream")
    )
    if provider == "fallback_norm_cache":
        provider = "norm_cache"
    reason = str(provider_state.get("reason") or "Primary audio source is on air")
    logger.info(
        "Listener-audible segment committed provider=%s reason=%s accepted_listeners=%d epoch=%d",
        provider,
        reason,
        accepted_listeners,
        state.continuity_epoch,
        extra={
            "event": "listener_audible_segment",
            "provider": provider,
            "reason": reason,
            "accepted_listeners": accepted_listeners,
            "continuity_epoch": state.continuity_epoch,
            "playback_epoch": state.playback_epoch,
        },
    )
    return True


async def _packaged_recovery_segment(fallback: Path) -> Segment:
    """Build a packaged continuity Segment for the playback rescue ladder."""
    duration_sec = _packaged_clip_duration_cache.get(fallback, 0.0)
    if duration_sec <= 0:
        # rescue=True: this fill airs instead of dead air, so the probe must
        # take the bounded rescue ffmpeg slot, never queue behind ordinary
        # normalization jobs (#2 INSTANT AUDIO).
        probed = await asyncio.to_thread(probe_duration_sec, fallback, rescue=True)
        duration_sec = probed or 0.0
        if duration_sec > 0:
            _packaged_clip_duration_cache[fallback] = duration_sec
    duration_fields = {"duration_ms": round(duration_sec * 1000)} if duration_sec > 0 else {}
    return Segment(
        type=SegmentType.BANTER,
        path=fallback,
        duration_sec=duration_sec,
        metadata={
            "type": "banter",
            "canned": True,
            "fallback": True,
            "rescue": True,
            "title": "Station continuity",
            **duration_fields,
        },
        ephemeral=False,
    )


def _finalize_selected_playback(
    app,
    segment_queue,
    state: StationState,
    config,
    segment: Segment,
    *,
    pulled_from_queue: bool,
    bytes_sent: int,
    was_skipped: bool,
    send_completed_cleanly: bool,
    start_listeners: int,
    accepted_listener_count: int,
    terminal_reason: str,
    all_chunks_audience_delivered: bool,
    cancel_reason: str = GenerationWasteReason.OPERATOR_PURGE,
) -> None:
    """Settle every selected Segment exactly once, including setup failures.

    Selection happens before public now-playing callbacks and file open. Keeping
    one outer finalizer around that whole region guarantees a callback exception
    cannot strand ``active_playback_segment`` or the queue's unfinished-task
    counter. A clean music head becomes ``SUCCESSOR_DUE`` before the active
    pointer is cleared; an unsuccessful head retracts its queued successor.
    """

    handoff_completed_cleanly = send_completed_cleanly
    if segment.type is SegmentType.MUSIC and segment.handoff_id:
        # Opening an empty/truncated head reaches Python EOF but emits no owned
        # music frame. It cannot authorize the tail successor as if the head
        # had aired successfully.
        handoff_completed_cleanly = send_completed_cleanly and bytes_sent > 0
    try:
        if segment.type is SegmentType.MUSIC and segment.handoff_id and not handoff_completed_cleanly:
            _cancel_active_handoff_from_queue(segment_queue, state, reason=cancel_reason)
        finish_handoff_segment(state, segment, completed_cleanly=handoff_completed_cleanly)
    finally:
        try:
            _schedule_banter_memory_extraction_after_send(
                app.state,
                config,
                state,
                segment,
                bytes_sent=bytes_sent,
                send_completed_cleanly=send_completed_cleanly,
                listeners=start_listeners,
                accepted_listeners=accepted_listener_count,
            )
        finally:
            try:
                _emit_stream_result(
                    state,
                    segment,
                    bytes_sent,
                    was_skipped,
                    start_listeners,
                    terminal_reason=terminal_reason,
                    all_chunks_audience_delivered=all_chunks_audience_delivered,
                    accepted_listener_count=accepted_listener_count,
                )
            finally:
                # Best-effort unlink: a raw unlink here can raise a non-missing
                # OSError and escape the finally, killing the playback loop after
                # we already decided to move on. Reuse the guarded helper.
                _unlink_ephemeral_best_effort(segment)
                try:
                    if pulled_from_queue:
                        segment_queue.task_done()
                finally:
                    if state.active_playback_segment is segment:
                        state.active_playback_segment = None


async def run_playback_loop(app) -> None:
    """Play queued segments on a single station timeline and fan out audio chunks."""
    # Producer admission and playback egress share the same privacy-generation
    # predicate.  Import locally to keep this transport module's import surface
    # light while still making the last pre-air check use the canonical rule.
    from mammamiradio.scheduling.producer import _home_context_generation_is_current

    segment_queue = app.state.queue
    skip_event = app.state.skip_event
    state = app.state.station_state
    config = app.state.config
    hub = app.state.stream_hub
    bytes_per_sec = (config.audio.bitrate * 1000) / 8  # bitrate is in kbps; convert to bytes/sec
    chunk_size = _stream_chunk_size(bytes_per_sec)
    pacer_factory = getattr(app.state, "stream_pacer_factory", StreamPacer)
    pacer = pacer_factory(bytes_per_sec)
    app.state.stream_pacer = pacer
    # Report what this pacer runs at, not what the constant says, so the admin
    # diagnostic can never describe a cushion the loop is not using.
    state.stream_pacing_target_lead_seconds = pacer.target_lead_seconds
    state.stream_pacing_late_threshold_seconds = pacer.late_threshold_seconds
    # A full disconnect/reconnect can happen while the inner file-send loop is
    # active, so the outer empty-room branch alone is not enough to restore a
    # first-packet cushion for that new listener generation.
    pacer_delivery_generation = hub.delivery_generation
    _persist_tasks: set[asyncio.Task] = set()  # prevent GC of fire-and-forget tasks
    _ha_push_tasks: set[asyncio.Task] = set()  # prevent GC of HA push tasks
    gap_clips_served = 0

    while True:
        if state.session_stopped:
            pacer.reset_timeline("playback_stop_resume")
            state.queue_empty_since = None
            gap_clips_served = 0
            try:
                await asyncio.wait_for(state.resume_event.wait(), timeout=1.0)
            except TimeoutError:
                pass
            state.resume_event.clear()
            continue

        # Pause when nobody is listening — don't burn API tokens or disk on an empty room.
        # The queue stays full; the moment a listener connects, playback resumes instantly.
        if not hub._listeners:
            pacer.reset_timeline("no_listeners")
            state.queue_empty_since = None
            gap_clips_served = 0
            # Wait on the listener-arrived event instead of a fixed 1s poll, so a
            # connect to an empty room resumes playback immediately. Clear then
            # re-check (no await between) to avoid a lost wakeup if a listener
            # subscribed between the emptiness check and the clear; the 1s timeout
            # preserves the old periodic re-check as a backstop.
            hub._listener_arrived.clear()
            if not hub._listeners:
                try:
                    await asyncio.wait_for(hub._listener_arrived.wait(), timeout=1.0)
                except TimeoutError:
                    pass
            # If a listener connected while this branch was parked, it starts a
            # fresh room epoch after the reset immediately above.  Recording it
            # here prevents a duplicate reset on the next outer iteration.
            pacer_delivery_generation = hub.delivery_generation
            continue

        # Capture before any queue wait, slot claim, or recovery probe. Stop
        # advances this epoch before purging; a fast Resume can clear the
        # stopped boolean while this selection is still awaiting slow work.
        # The post-selection gate below therefore defeats that ABA cycle for
        # every playback ingress, including out-of-band recovery media.
        selection_continuity_epoch = state.continuity_epoch

        # Priority slot: interrupt bridge audio plays before anything in the queue.
        _bridge_segment: Segment | None = None
        if state.interrupt_slot is not None:
            bridge_path = state.interrupt_slot
            bridge_source = state.interrupt_slot_source
            bridge_home_generation = state.interrupt_slot_home_context_generation
            state.interrupt_slot = None
            state.interrupt_slot_source = ""
            state.interrupt_slot_home_context_generation = None
            if bridge_path.exists():
                bridge_metadata: dict[str, object] = {
                    "type": "banter",
                    "interrupt": True,
                    "interrupt_source": bridge_source,
                }
                if bridge_home_generation is not None:
                    bridge_metadata["home_context_generation"] = bridge_home_generation
                _bridge_segment = Segment(
                    type=SegmentType.BANTER,
                    path=bridge_path,
                    metadata=bridge_metadata,
                    ephemeral=state.interrupt_slot_ephemeral,
                )
                state.interrupt_slot_ephemeral = False
                state.queue_empty_since = None
                gap_clips_served = 0
            else:
                logger.warning("Interrupt slot path missing: %s — skipping bridge", bridge_path)
                state.interrupt_slot_ephemeral = False

        pulled_from_queue = False
        segment: Segment
        if _bridge_segment is not None:
            segment = _bridge_segment
        elif segment_queue.empty() and (continuity_slot := _claim_continuity_slot(state)) is not None:
            # The guard could not reserve capacity without displacing a ready
            # air-next item. It is intentionally consumed only after the normal
            # queue (and any interrupt bridge) has no audio left.
            segment = continuity_slot
            state.queue_empty_since = None
            if not _segment_is_canned_recovery(segment):
                gap_clips_served = 0
        else:
            if segment_queue.empty() and state.queue_empty_since is None:
                # Mark the exact moment playback ran out of audio. The
                # FIRST_BYTE_GRACE_SECONDS wait_for() below is part of the
                # listener-visible silence window.
                state.queue_empty_since = _runtime_monotonic()
            try:
                segment = await asyncio.wait_for(segment_queue.get(), timeout=FIRST_BYTE_GRACE_SECONDS)
                pulled_from_queue = True
                state.queue_empty_since = None
                if not _segment_is_canned_recovery(segment):
                    gap_clips_served = 0
            except TimeoutError:
                if state.session_stopped:
                    pacer.reset_timeline("playback_stop_resume")
                    state.queue_empty_since = None
                    gap_clips_served = 0
                    continue

                if not hub._listeners:
                    pacer.reset_timeline("no_listeners")
                    state.queue_empty_since = None
                    gap_clips_served = 0
                    continue

                if state.queue_empty_since is None:
                    state.queue_empty_since = _runtime_monotonic()
                elapsed = _runtime_monotonic() - state.queue_empty_since
                # Do not reset the send timeline here. The 1s grace timeout is
                # not a transport discontinuity: the next packaged clip is the
                # following segment. Resetting rebuilds the 4s lead cushion by
                # bursting continuity_1.mp3 from byte 0, so a speaker hears the
                # opening ident on a one-second loop.

                from mammamiradio.scheduling.producer import _pick_recovery_clip

                segment_ready = False

                if gap_clips_served == 0 and (fallback := _pick_recovery_clip(state)):
                    logger.info("Queue empty — serving packaged recovery clip: %s", fallback.name)
                    segment = await _packaged_recovery_segment(fallback)
                    gap_clips_served += 1
                    segment_ready = True

                if not segment_ready:
                    rescued_from_norm = False
                    if elapsed >= FIRST_BYTE_GRACE_SECONDS:
                        # Permissive because nothing real sits below this rung.
                        # The bundled-demo-music rung does not ship
                        # (``assets/demo/music/`` is absent from the package), and
                        # the packaged-clip branch below sets ``segment_ready``,
                        # which makes the 60s forced-banter escape unreachable.
                        # The alternative here is the same 4.4s canned line on
                        # repeat, with a playable song sitting in the cache.
                        #
                        # Permissive still prefers a non-recent candidate:
                        # select_norm_cache_rescue returns a recent one only when
                        # the cache holds nothing else. The strict answer belongs
                        # to the producer's drain/resume/idle bridge, which has the
                        # packaged clip and the emergency tone beneath it.
                        rescue = _select_norm_cache_rescue(
                            config.cache_dir,
                            state,
                            allow_recent_repeat=True,
                            require_known_origin=True,
                            allow_external_media=external_media_enabled(config.allow_ytdlp),
                        )
                        if rescue:
                            logger.warning(
                                "Queue empty %ds - rescuing with norm cache: %s",
                                int(elapsed),
                                rescue.name,
                            )
                            state.queue_empty_since = None
                            gap_clips_served = 0
                            rescued_from_norm = True
                            sidecar = load_track_metadata(rescue)
                            if sidecar:
                                # Illusion guard: a poisoned sidecar artist (a foreign
                                # "Radio X" station name) must never surface as the
                                # now-playing artist/label. Strip it and drop to
                                # title-only rather than airing a competitor's name.
                                clean_artist = strip_foreign_station_name(
                                    str(sidecar["artist"]), config.display_station_name
                                )
                                # prefix_only on the song title: drop a "Radio X - Song"
                                # rescue prefix but keep a song really titled "Radio Ga Ga".
                                raw_sidecar_title = str(sidecar["title"])
                                song_title = (
                                    strip_foreign_station_name(
                                        raw_sidecar_title, config.display_station_name, prefix_only=True
                                    )
                                    or raw_sidecar_title
                                )
                                # `title` is the display label and packs the artist in;
                                # `title_only` is the bare song title. Both are stamped
                                # because `segment_track_key` prefers `title_only`, and
                                # without it the ban fence below keys this segment as
                                # ("artist", "artist - title") - a shape that can never
                                # be in state.blocklist, silently disarming the fence.
                                rescue_title_only: str | None = song_title
                                if clean_artist:
                                    rescue_title = f"{clean_artist} – {song_title}"
                                    rescue_artist: str | None = clean_artist
                                else:
                                    rescue_title = song_title
                                    rescue_artist = None
                            else:
                                rescue_title = humanize_norm_filename(rescue.name)
                                rescue_artist = None
                                # No sidecar: the humanized filename is all we have, and
                                # it is not a trustworthy bare title. Leave title_only
                                # unset so the key falls back to `title` as before.
                                rescue_title_only = None
                            duration_sec = norm_cache_duration_sec(rescue, bitrate_kbps=config.audio.bitrate)
                            duration_fields = {"duration_ms": round(duration_sec * 1000)} if duration_sec > 0 else {}
                            segment = Segment(
                                type=SegmentType.MUSIC,
                                path=rescue,
                                duration_sec=duration_sec,
                                metadata={
                                    "type": "music",
                                    "title": rescue_title,
                                    **({"title_only": rescue_title_only} if rescue_title_only else {}),
                                    **({"artist": rescue_artist} if rescue_artist else {}),
                                    **duration_fields,
                                    "audio_source": "norm_cache",
                                    "fallback": True,
                                },
                                ephemeral=False,
                            )

                    if rescued_from_norm:
                        pass
                    else:
                        # Unmanifested packaged music is never a fallback source.
                        # Reuse only approved continuity speech before requesting
                        # fresh banter from the producer.
                        if fallback := _pick_recovery_clip(state):
                            logger.warning(
                                "Queue empty %ds - re-serving packaged recovery clip: %s",
                                int(elapsed),
                                fallback.name,
                            )
                            segment = await _packaged_recovery_segment(fallback)
                            gap_clips_served += 1
                            segment_ready = True

                    if (
                        rescued_from_norm
                        or segment_ready
                        or (segment_queue.empty() and state.queue_empty_since is None)
                    ):
                        pass
                    elif elapsed >= 60.0:
                        # Request forced banter once per silence episode to avoid producer thrash.
                        # queue_empty_since is intentionally NOT reset — the silence gate on
                        # /healthz and /readyz must stay active until real audio resumes.
                        if state.force_next is None:
                            state.force_next = SegmentType.BANTER
                            logger.error(
                                "Queue empty %ds with %d active listeners - requesting forced banter from producer",
                                int(elapsed),
                                len(hub._listeners),
                            )
                        continue
                    else:
                        logger.warning("Queue empty for %ds, no fallback clips available", int(elapsed))
                        pacer.reset_timeline("queue_gap_fallback")
                        continue

                # Building a packaged fill can await a bounded probe while a
                # Stop/Resume or another control advances continuity_epoch.
                # These source-neutral rescue bytes are admitted only after
                # that await, so bind them to the timeline that now owns them.
                # Known trade-off: the stamp makes the staleness gate below
                # unreachable for gap fills, so a control that queued fresh
                # runway mid-probe has its cut deferred until the fill ends.
                # Airing rescue audio is the safer default; teaching the fill
                # to yield needs the full three-scenario test set first.
                segment = _stamp_playback_gap_fill(segment, state)

        segment_metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
        admitted_on_current_timeline = segment_metadata.get(
            _CONTINUITY_ADMISSION_EPOCH
        ) == state.continuity_epoch and bool(segment_metadata.get(_CONTINUITY_RESERVATION_FLAG))

        # Resume reserves runway before it removes the stopped marker. If that
        # persistence step fails, an already-blocked queue waiter can still
        # receive the candidate once the route yields. Put it straight back:
        # failed Resume must leave the runway parked for the operator's retry.
        if state.session_stopped and admitted_on_current_timeline and pulled_from_queue:
            try:
                segment_queue.put_nowait(segment)
                segment_queue.task_done()
                _rebuild_queue_shadow(segment_queue, state, list(getattr(segment_queue, "_queue", ())))
            except asyncio.QueueFull:  # pragma: no cover - the get() freed this exact slot
                _consume_queue_shadow(segment_queue, state, segment)
                segment_queue.task_done()
                state.continuity_slot = segment
            state.queue_empty_since = None
            gap_clips_served = 0
            logger.info(
                "Playback re-parked Resume runway while stopped epoch=%d source=%s",
                state.continuity_epoch,
                str(segment_metadata.get("audio_source") or segment.path.name),
                extra={
                    "event": "resume_runway_reparked",
                    "continuity_epoch": state.continuity_epoch,
                    "runway_source": str(segment_metadata.get("audio_source") or segment.path.name),
                },
            )
            continue

        if (
            selection_continuity_epoch != state.continuity_epoch
            and not admitted_on_current_timeline
            # A live protected tail successor is the continuation of the SAME
            # listener timeline: pair reconciliation bumps the epoch while
            # deliberately keeping it at queue-head. Explicit cuts release its
            # reservation first, so a cancelled successor is not exempt here.
            and not is_protected_handoff_successor(state, segment)
        ):
            if pulled_from_queue:
                _consume_queue_shadow(segment_queue, state, segment)
            state.record_discard(
                segment,
                reason=GenerationWasteReason.STALE_CONTINUITY,
                already_counted_in_produced=pulled_from_queue,
            )
            _drop_segment_moment_receipts(state, segment, GenerationWasteReason.STALE_CONTINUITY)
            _settle_discarded_selection_handoff(
                segment_queue, state, segment, reason=GenerationWasteReason.STALE_CONTINUITY
            )
            _unlink_ephemeral_best_effort(segment)
            if pulled_from_queue:
                segment_queue.task_done()
            # A rejected playback-built rescue did not replace the gap. Keep
            # both escalation clocks running instead of restarting the ladder
            # and extending listener-visible silence.
            if not segment_metadata.get(_PLAYBACK_GAP_FILL_FLAG):
                state.queue_empty_since = None
                gap_clips_served = 0
            logger.warning(
                "Discarding playback selection after continuity epoch changed "
                "captured_epoch=%d current_epoch=%d type=%s",
                selection_continuity_epoch,
                state.continuity_epoch,
                segment.type.value,
                extra={
                    "event": "playback_selection_stale_continuity",
                    "captured_continuity_epoch": selection_continuity_epoch,
                    "continuity_epoch": state.continuity_epoch,
                    "segment_type": segment.type.value,
                },
            )
            continue

        if pulled_from_queue:
            _consume_queue_shadow(segment_queue, state, segment)

        is_companionship_cue, companionship_epoch = _companionship_segment_epoch(segment)
        if is_companionship_cue and not _companionship_cue_is_current(state, companionship_epoch):
            state.record_discard(
                segment,
                reason=GenerationWasteReason.LISTENER_SESSION_STALE,
                already_counted_in_produced=pulled_from_queue,
            )
            _drop_segment_moment_receipts(state, segment, GenerationWasteReason.LISTENER_SESSION_STALE)
            _settle_discarded_selection_handoff(
                segment_queue, state, segment, reason=GenerationWasteReason.LISTENER_SESSION_STALE
            )
            _unlink_ephemeral_best_effort(segment)
            if pulled_from_queue:
                segment_queue.task_done()
            logger.info(
                "Discarding stale companionship cue before playback (segment_epoch=%s, current_epoch=%s)",
                companionship_epoch,
                state.listener_session.epoch,
            )
            continue

        if state.session_stopped:
            # Stop landed mid-selection: drop this segment instead of airing it.
            # Unlink any ephemeral temp (a queue-pulled segment or an interrupt
            # bridge captured just before the stop) and balance the queue
            # bookkeeping — the normal finally calls task_done for pulled segments.
            state.record_discard(
                segment,
                reason=GenerationWasteReason.SESSION_STOPPED,
                already_counted_in_produced=pulled_from_queue,
            )
            _drop_segment_moment_receipts(state, segment, GenerationWasteReason.SESSION_STOPPED)
            _settle_discarded_selection_handoff(
                segment_queue, state, segment, reason=GenerationWasteReason.SESSION_STOPPED
            )
            # Use the hardened helper (never raises) instead of a raw unlink: a
            # throwing unlink here would escape into the playback coroutine and
            # drop the stream (#1), and skip the task_done() balance below.
            _unlink_ephemeral_best_effort(segment)
            if pulled_from_queue:
                segment_queue.task_done()
            state.queue_empty_since = None
            gap_clips_served = 0
            continue

        if not segment.mark_playback_started():
            _unlink_ephemeral_best_effort(segment)
            if pulled_from_queue:
                segment_queue.task_done()
            logger.info("segment_playback_admission_denied")
            continue

        if not _home_context_generation_is_current(state, config, segment):
            # A privacy cutover may land after queue admission but before this
            # segment becomes now-playing.  Reject it at the final unstarted
            # boundary so an older Home-derived render can never start later.
            state.record_discard(
                segment,
                reason=GenerationWasteReason.OPERATOR_PURGE,
                already_counted_in_produced=pulled_from_queue,
            )
            _drop_segment_moment_receipts(state, segment, GenerationWasteReason.OPERATOR_PURGE)
            _settle_discarded_selection_handoff(
                segment_queue, state, segment, reason=GenerationWasteReason.OPERATOR_PURGE
            )
            _unlink_ephemeral_best_effort(segment)
            if pulled_from_queue:
                segment_queue.task_done()
            logger.info("Discarding stale Home-context segment before playback")
            continue

        if segment.type is SegmentType.MUSIC and _segment_blocklist_key(segment) in state.blocklist:
            # The normal queue is purged synchronously when an operator bans a
            # track, but keep the same last-mile fence as the out-of-band slot.
            # A future or degraded mutation path must never turn stale queued
            # bytes into an exception to the station-wide blocklist invariant.
            state.record_discard(
                segment,
                reason=GenerationWasteReason.OPERATOR_BAN,
                already_counted_in_produced=pulled_from_queue,
            )
            _drop_segment_moment_receipts(state, segment, GenerationWasteReason.OPERATOR_BAN)
            _settle_discarded_selection_handoff(
                segment_queue, state, segment, reason=GenerationWasteReason.OPERATOR_BAN
            )
            _unlink_ephemeral_best_effort(segment)
            if pulled_from_queue:
                segment_queue.task_done()
                remaining = list(getattr(segment_queue, "_queue", ()))
                prior_tail = remaining[-1] if remaining else segment
                _reconcile_queue_tail_adjacency(segment_queue, state, prior_tail=prior_tail)
            logger.info("Discarding blocklisted queued music before playback: %s", segment.path)
            continue

        # Keep a private object reference while this Segment is selected or
        # airing. ``now_streaming`` is deliberately just a public projection,
        # so it cannot identify the exact tail-bearing successor that a live
        # Skip/Panic must retract.
        state.active_playback_segment = segment
        stream_selected = False
        selected_playback_epoch: int | None = None

        try:
            bytes_sent = 0
            accepted_listener_count = 0
            was_skipped = False
            send_completed_cleanly = False
            terminal_reason = "aborted"
            companionship_discard_recorded = False
            air_start_stamped = False
            # Selection is the boundary where a queued pair member becomes
            # playback-owned: advance the private handoff phase before any
            # callback or send work below can fail.
            mark_handoff_segment_selected(state, segment)
            # `aired` means at least one chunk reached a listener. A receipt
            # requires every ad chunk to reach at least one listener queue; one
            # zero-delivery chunk makes the break ineligible.
            all_chunks_audience_delivered = True
            # Sample listeners at the START of the send loop so a mid-segment
            # disconnect doesn't mislabel an aired segment as no_listeners
            # (matches classify_stream_outcome's documented contract). Default to
            # 0 first so the finally's _emit_stream_result never references an
            # unbound local if listener sampling itself raises.
            start_listeners = 0
            start_listeners = len(hub._listeners)
            skip_event.clear()
            # A queued segment's file can vanish before it airs — evicted by the
            # cache LRU, deleted externally, or pruned by the restart-handoff spool
            # while still queued. Skip to the next segment instead of letting the
            # OSError escape and kill the playback loop (dead air until restart).
            # Scoped to OSError so non-IO errors keep their behavior; bytes_sent
            # stays 0 so _emit_stream_result records an honest no-air outcome.
            try:
                with open(segment.path, "rb") as f:
                    _skip_id3_and_xing_header(f)
                    while chunk := f.read(chunk_size):
                        if skip_event.is_set():
                            logger.info("Skipping current segment")
                            was_skipped = True
                            terminal_reason = "skip"
                            pacer.reset_timeline("explicit_skip")
                            skip_event.clear()
                            break

                        if is_companionship_cue and (
                            companionship_epoch != state.listener_session.epoch
                            or state.listener_session.companionship_cue_state
                            not in {ListenerSessionCueState.QUEUED, ListenerSessionCueState.CONSUMED}
                        ):
                            terminal_reason = GenerationWasteReason.LISTENER_SESSION_STALE
                            was_skipped = True
                            if not stream_selected:
                                state.record_discard(
                                    segment,
                                    reason=GenerationWasteReason.LISTENER_SESSION_STALE,
                                    already_counted_in_produced=pulled_from_queue,
                                )
                                companionship_discard_recorded = True
                            logger.info(
                                "Stopping stale companionship cue before its next audio chunk "
                                "(segment_epoch=%s, current_epoch=%s)",
                                companionship_epoch,
                                state.listener_session.epoch,
                            )
                            break

                        if not stream_selected:
                            selected_playback_epoch = _start_stream_segment(
                                app,
                                state,
                                config,
                                segment,
                                _ha_push_tasks,
                            )
                            stream_selected = True

                        # The room can briefly become empty and refill while a
                        # long segment is in flight.  Reset before that new
                        # listener receives a packet so it gets the same
                        # bounded bootstrap cushion as a cold start, without
                        # changing natural segment-boundary pacing.
                        if hub.delivery_generation != pacer_delivery_generation:
                            pacer.reset_timeline("no_listeners")
                            pacer_delivery_generation = hub.delivery_generation

                        accepted_listeners = await hub.broadcast(chunk)
                        accepted_count = max(0, int(accepted_listeners or 0))
                        accepted_listener_count = max(accepted_listener_count, accepted_count)
                        if segment.type is SegmentType.AD and accepted_count <= 0:
                            all_chunks_audience_delivered = False
                        if is_companionship_cue and not air_start_stamped:
                            if accepted_count <= 0:
                                terminal_reason = GenerationWasteReason.LISTENER_SESSION_STALE
                                state.record_discard(
                                    segment,
                                    reason=GenerationWasteReason.LISTENER_SESSION_STALE,
                                    already_counted_in_produced=pulled_from_queue,
                                )
                                companionship_discard_recorded = True
                                logger.info("Abandoning companionship cue because no listener accepted its first chunk")
                                break
                            if companionship_epoch is None or not state.listener_session.mark_companionship_consumed(
                                companionship_epoch
                            ):
                                # broadcast() has no await points, so this can only
                                # happen if lifecycle state was already corrupt.
                                terminal_reason = GenerationWasteReason.LISTENER_SESSION_STALE
                                was_skipped = True
                                logger.error("Companionship cue could not transition QUEUED -> CONSUMED")
                                break

                        # `bytes_sent` means "bytes this segment wrote", not
                        # "bytes a listener accepted" — `accepted_listener_count`
                        # below carries the audible truth. Gating it on
                        # `accepted_count` made an ordinary segment that played
                        # to EOF in an empty room report zero bytes, which
                        # `classify_stream_outcome` reads as NOT_STREAMED ("zero
                        # bytes left the box (file error)") rather than the
                        # honest NO_LISTENERS. A cue still only counts when heard,
                        # because an unheard cue is genuinely never delivered.
                        if not is_companionship_cue or accepted_count > 0:
                            bytes_sent += len(chunk)

                        # First chunk a listener queue actually accepted. This is
                        # the single "truly heard" moment for both bookkeeping
                        # jobs below, and it is strictly more accurate than EOF:
                        # a live control firing two minutes into a 3.5-minute
                        # play would otherwise see the on-air song as never
                        # heard and re-reserve it. Audio that never reaches a
                        # listener never gets here, so neither consumes rotation
                        # nor reports a bridge.
                        #
                        # Both callees are cheap, bounded, and self-guarded. Keep
                        # them that way: this is the hottest loop in the product
                        # and anything that blocks here is dead air, so no
                        # filesystem, network, or unbounded work may move behind
                        # this once-per-segment flag.
                        if (
                            not air_start_stamped
                            and accepted_count > 0
                            and _commit_audible_stream_segment(
                                state,
                                segment,
                                accepted_listeners=accepted_count,
                            )
                        ):
                            air_start_stamped = True
                            _record_rescue_airplay(state, segment)
                            _record_continuity_air(state, segment)

                        # Feed only non-music into the generic share ring. Starter
                        # music uses a separately validated package snapshot below;
                        # local, Jamendo, mixed, and unknown music must never enter
                        # clip/share memory even if the endpoint later returns 403.
                        clip_buf = getattr(app.state, "clip_ring_buffer", None)
                        if clip_buf is not None and segment.type is not SegmentType.MUSIC:
                            clip_buf.append(chunk)

                        pacing = pacer.after_send(len(chunk))
                        if pacing.kind is not None:
                            state.record_stream_pacing_event(
                                pacing.kind,
                                lateness_ms=pacing.lateness_seconds * 1000,
                                remaining_lead_ms=pacing.remaining_lead_seconds * 1000,
                                deficit_ms=pacing.deficit_seconds * 1000,
                                segment_type=segment.type.value,
                            )
                        if pacing.warn_underrun:
                            logger.warning(
                                "Stream delivery cushion exhausted by %.1f ms during %s",
                                pacing.deficit_seconds * 1000,
                                segment.type.value,
                            )
                        if pacing.sleep_seconds > 0.005:
                            await asyncio.sleep(pacing.sleep_seconds)
                    else:
                        send_completed_cleanly = True
                        terminal_reason = "eof"
            except asyncio.CancelledError:
                terminal_reason = "cancelled"
                raise
            except OSError as exc:
                logger.warning("Segment file unreadable; moving to next: %s (%s)", segment.path, exc)
                # This handler covers two different failures. A file that never
                # opened wrote nothing, and `bytes_sent == 0` already classifies
                # it `not_streamed` — the honest "zero bytes left the box".
                # A read that fails PART WAY THROUGH is a truncation only when
                # at least one listener accepted an earlier chunk. Without the
                # skip marker that accepted partial delivery would classify
                # `aired`, so a home-triggered Moment Receipt would report
                # "made it to air" for audio that was cut off, and a truncated
                # release beat would count a delivery against max_airings.
                # Bytes rejected by every connected listener are different:
                # preserve `was_skipped=False` so accepted-listener truth
                # classifies that failed delivery `not_streamed`.
                if bytes_sent > 0 and accepted_listener_count > 0:
                    was_skipped = True
                terminal_reason = "file_error"
            # Lookback snapshot: when an ad/banter segment finishes, remember the
            # whole thing so a listener who taps Share just after it ends (music
            # already playing again) still captures it. Single extract at the
            # boundary — no per-chunk work on the throttled send path above.
            if (
                segment.type in (SegmentType.AD, SegmentType.BANTER)
                and not was_skipped
                and air_start_stamped
                and send_completed_cleanly
            ):
                # Wrapped: snapshotting is a nice-to-have. An extract failure
                # (e.g. MemoryError joining a long segment on a Pi) must never
                # escape into the playback coroutine and drop the stream
                # (leadership principle #1). Worst case: no lookback for this bit.
                try:
                    from mammamiradio.scheduling.clip import extract_clip as _extract_clip

                    _clip_buf = getattr(app.state, "clip_ring_buffer", None)
                    if _clip_buf:
                        _bitrate = config.audio.bitrate if hasattr(config, "audio") else DEFAULT_CLIP_BITRATE_KBPS
                        _secs = min(
                            CLIP_MAX_SEGMENT_SECONDS,
                            max(CLIP_DURATION_SECONDS, math.ceil(segment.duration_sec or 0)),
                        )
                        _snap = _extract_clip(_clip_buf, duration_seconds=_secs, bitrate_kbps=_bitrate)
                        if _snap:
                            _meta = segment.metadata if isinstance(segment.metadata, dict) else {}
                            app.state.last_shareworthy_clip = {
                                "bytes": _snap,
                                "ended_monotonic": time.monotonic(),
                                "type": segment.type.value,
                                "title": str(_meta.get("title") or "").strip(),
                                # Carried so a keepsake cut from this snapshot can
                                # tell whether the segment opened with the previous
                                # song's master crossfaded under the voice. Type
                                # alone does not answer that.
                                "has_music_tail": bool(_meta.get("has_music_tail")),
                            }
                except Exception as exc:
                    logger.warning("lookback snapshot failed for %s segment: %s", segment.type.value, exc)
            if (
                segment.type is SegmentType.MUSIC
                and send_completed_cleanly
                and not was_skipped
                and bytes_sent > 0
                and start_listeners > 0
            ):
                starter_snapshot = _validated_starter_share_snapshot(segment)
                if starter_snapshot is not None:
                    app.state.last_shareworthy_starter = starter_snapshot
            if segment.type == SegmentType.MUSIC and not was_skipped and air_start_stamped and send_completed_cleanly:
                listen_sec = bytes_sent / bytes_per_sec if bytes_per_sec else None
                # Fire-and-forget: persistence must not block the handoff to the next
                # segment — on Pi, the SQLite writes can take long enough to cause
                # audible gaps between songs.
                coro = _persist_completed_music(state, config, segment.metadata, listen_sec=listen_sec)
                task = asyncio.create_task(coro)
                _persist_tasks.add(task)
                task.add_done_callback(_persist_tasks.discard)
        finally:
            if is_companionship_cue and terminal_reason == GenerationWasteReason.LISTENER_SESSION_STALE:
                _clear_stale_companionship_selection(state, selected_playback_epoch)
            if (
                is_companionship_cue
                and not air_start_stamped
                and not companionship_discard_recorded
                and companionship_epoch is not None
                and state.listener_session.companionship_cue_state is ListenerSessionCueState.QUEUED
            ):
                state.record_discard(
                    segment,
                    reason=GenerationWasteReason.LISTENER_SESSION_STALE,
                    already_counted_in_produced=pulled_from_queue,
                )
            if selected_playback_epoch is not None and state.playback_epoch == selected_playback_epoch:
                state.current_stream_audible = False
            _finalize_selected_playback(
                app,
                segment_queue,
                state,
                config,
                segment,
                pulled_from_queue=pulled_from_queue,
                bytes_sent=bytes_sent,
                was_skipped=was_skipped,
                send_completed_cleanly=send_completed_cleanly,
                start_listeners=start_listeners,
                accepted_listener_count=accepted_listener_count,
                terminal_reason=terminal_reason,
                all_chunks_audience_delivered=all_chunks_audience_delivered,
            )


def _schedule_banter_memory_extraction_after_send(
    app_state: Any,
    config: Any,
    state: StationState,
    segment: Segment,
    *,
    bytes_sent: int,
    send_completed_cleanly: bool,
    listeners: int,
    accepted_listeners: int | None = None,
) -> None:
    """Start post-air memory extraction only after a listener actually heard it.

    `bytes_sent` counts bytes this loop WROTE, which a room that accepted
    nothing still accumulates. Durable listener memory must follow the audible
    boundary instead, so gate on accepted listeners when the caller knows them.
    """
    if segment.type is not SegmentType.BANTER or not send_completed_cleanly or bytes_sent <= 0:
        return
    if accepted_listeners is None:
        if listeners <= 0:
            return
    elif accepted_listeners <= 0:
        return
    metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
    if not metadata.get("memory_extraction"):
        return
    try:
        from mammamiradio.scheduling.producer import _home_context_generation_is_current

        # A Home-derived segment may have begun airing before the operator
        # revoked access.  It can finish cleanly (no dead air), but its private
        # prompt payload must not leave the box afterward.
        if not _home_context_generation_is_current(state, config, segment):
            return
        from mammamiradio.hosts.memory_extractor import schedule_banter_memory_extraction

        task = schedule_banter_memory_extraction(config=config, state=state, metadata=metadata)
        if task is not None:
            # This is intentionally tied to audio send completion, not queueing:
            # queued/purged/skipped banter never becomes durable listener memory.
            _register_background_task(app_state, task)
    except Exception:
        logger.warning("memory_extract: scheduling failed", exc_info=True)


def _finalize_moment_receipts(
    state,
    segment,
    bytes_sent: int,
    was_skipped: bool,
    listeners: int,
    *,
    accepted_listeners: int | None = None,
) -> None:
    """Record the TRUE outcome on any Moment Receipt this segment carried.

    Independent of the provenance ledger (runs even when Show Memory is off).
    Uses classify_stream_outcome verbatim, so a skipped, unheard, or rescue
    send can never mint a false "aired" receipt. In-memory only — the dirty
    store is flushed at the producer's save site, never from the playback loop.
    """
    store = getattr(state, "moment_store", None)
    if store is None:
        return
    try:
        from mammamiradio.core.segment_status import classify_stream_outcome, is_fallback_active

        meta = segment.metadata if isinstance(segment.metadata, dict) else {}
        moment_ids = [str(meta.get(key) or "") for key in ("ritual_moment_id", "gag_moment_id")]
        moment_ids = [moment_id for moment_id in moment_ids if moment_id]
        if not moment_ids:
            return
        status = classify_stream_outcome(
            was_skipped=was_skipped,
            bytes_sent=bytes_sent,
            listeners=listeners,
            fallback_active=is_fallback_active(meta),
            accepted_listeners=accepted_listeners,
        )
        for moment_id in moment_ids:
            store.finalize(moment_id, status)
    except Exception as exc:  # pragma: no cover - receipts must never break audio
        logger.debug("Moment receipt finalize failed: %s", exc)


def _emit_stream_result(
    state,
    segment,
    bytes_sent: int,
    was_skipped: bool,
    listeners: int,
    *,
    terminal_reason: str | None = None,
    all_chunks_audience_delivered: bool = False,
    accepted_listener_count: int | None = None,
) -> None:
    """Tier-3: record the TRUE aired outcome after the send loop.

    Fires from the (sync) playback loop's finally, so it captures partial and
    failed sends too. Never raises into the stream.
    """
    accepted_count = (
        max(0, int(accepted_listener_count))
        if accepted_listener_count is not None
        else max(0, int(listeners))
        if bytes_sent > 0
        else 0
    )
    _emit_release_campaign_result(
        state,
        segment,
        bytes_sent,
        was_skipped,
        listeners,
        accepted_listeners=accepted_count,
    )
    _finalize_moment_receipts(state, segment, bytes_sent, was_skipped, listeners, accepted_listeners=accepted_count)
    try:
        from mammamiradio.core.segment_status import classify_stream_outcome, is_fallback_active

        meta = segment.metadata if isinstance(segment.metadata, dict) else {}
        result = classify_stream_outcome(
            was_skipped=was_skipped,
            bytes_sent=bytes_sent,
            listeners=listeners,
            fallback_active=is_fallback_active(meta),
            accepted_listeners=accepted_count,
        )
        _record_ad_experiment_receipt(
            state,
            segment,
            result=result,
            terminal_reason=terminal_reason,
            all_chunks_audience_delivered=all_chunks_audience_delivered,
        )
        state.record_stream_outcome(
            segment_type=segment.type.value,
            result=result,
            bytes_sent=bytes_sent,
            starting_listener_count=listeners,
            terminal_reason=terminal_reason or ("skip" if was_skipped else "eof"),
            accepted_listener_count=accepted_count,
        )
    except Exception as exc:  # pragma: no cover - diagnostics must never break audio
        logger.debug("Anonymous stream outcome recording failed: %s", exc)
    # The rotation cooldown is stamped in the send loop, on the first chunk a
    # listener queue accepted — not here. That predicate is strictly tighter than
    # this one was. Stamping at EOF also left a song looking unheard for its
    # entire play, which is how a live control came to re-reserve the song
    # already on the air.
    meta = segment.metadata if isinstance(segment.metadata, dict) else {}
    if meta.get("source_kind") == "jamendo" or meta.get("audio_source") in {
        "jamendo",
        "jamendo_transient",
    }:
        # Jamendo candidate and lease facts are process-local by contract. Keep
        # the anonymous in-memory delivery outcome above, but never serialize
        # title, provider, source, or lease-derived metadata into cache/ledger.
        return
    led = getattr(state, "ledger", None)
    if led is None or not led.enabled:
        return
    try:
        import time as _time

        from mammamiradio.core.ledger import SCHEMA_VERSION
        from mammamiradio.core.segment_status import classify_stream_outcome, is_fallback_active

        fallback_active = is_fallback_active(meta)
        led.record(
            {
                "schema_version": SCHEMA_VERSION,
                "ts": _time.time(),
                "record": "stream_result",
                "segment_id": meta.get("ledger_segment_id"),
                "segment_type": segment.type.value,
                "aired_status": classify_stream_outcome(
                    was_skipped=was_skipped,
                    bytes_sent=bytes_sent,
                    listeners=listeners,
                    fallback_active=fallback_active,
                    accepted_listeners=accepted_count,
                ),
                "bytes_sent": bytes_sent,
                "listeners": listeners,
                "audio_source": str(meta.get("audio_source") or ""),
                "fallback_active": fallback_active,
                "title": meta.get("title") or meta.get("brand"),
            }
        )
    except Exception as exc:  # pragma: no cover - provenance must never break audio
        logger.debug("Provenance Tier-3 emit failed: %s", exc)


def _emit_release_campaign_result(
    state,
    segment,
    bytes_sent: int,
    was_skipped: bool,
    listeners: int,
    *,
    accepted_listeners: int | None = None,
) -> None:
    """Best-effort release campaign accounting, independent from Show Memory."""
    campaign = getattr(state, "release_campaign", None)
    if campaign is None:
        return
    try:
        campaign.record_stream_result(
            segment.metadata or {},
            bytes_sent=bytes_sent,
            was_skipped=was_skipped,
            listeners=listeners,
            accepted_listeners=accepted_listeners,
        )
        # Persist synchronously: the ledger is one tiny object, guarded by _dirty
        # so it writes only on a real change (once per segment, at the segment
        # boundary — not per chunk). A threaded save raced this same loop-thread
        # mutation and cleared _dirty after snapshotting, dropping airings; and it
        # needed an undeclared state attribute. models.record_discard already
        # saves synchronously here, so this matches the existing pattern.
        campaign.save_if_dirty()
    except Exception as exc:  # pragma: no cover - release accounting must never break audio
        logger.debug("Release campaign stream-result hook failed: %s", exc)


def _ad_cast_status_payload(config) -> dict[str, object]:
    """Return only the config compiler's safe direct-cast diagnostics."""

    report = getattr(getattr(config, "ads", None), "cast_report", None)
    raw_excluded = getattr(report, "excluded_brands", ())
    raw_warnings = getattr(report, "warnings", ())
    excluded = (
        sorted(name for name in raw_excluded if isinstance(name, str) and name.strip())
        if isinstance(raw_excluded, set | frozenset | list | tuple)
        else []
    )
    warnings = (
        [warning for warning in raw_warnings if isinstance(warning, str) and warning.strip()][:20]
        if isinstance(raw_warnings, list | tuple)
        else []
    )
    return {"excluded_campaigns": excluded, "warnings": warnings}


def _ad_experiment_status(state: StationState) -> dict[str, object]:
    """Return the process-local receipt payload or an empty fallback."""
    try:
        return state.ad_experiment_snapshot()
    except Exception:  # pragma: no cover - isolate receipt errors from status
        logger.debug("Carosello experiment status failed", exc_info=True)
        return {"scope": "runtime", "completed_breaks": 0, "completed_spots": 0, "brands": []}


def _record_operator_action(request, action: str, old_value, new_value) -> None:
    """Record a station-wide operator toggle in the provenance ledger.

    A station-wide character change (Super Italian, Chaos, Festival, AI Quality,
    On-Air Sound) otherwise leaves no honest trace: FastAPI runs with
    ``--no-access-log`` so the POST never reaches the logs, and the only operator
    feedback is a small toast. A later debrief then cannot see WHAT the operator
    changed or WHEN — the "who switched the hosts to English?" class of mystery.
    This ``operator_action`` row closes that gap so the change is auditable
    alongside the aired moments it shaped.

    Best-effort, mirrors :func:`_emit_stream_result`: enabled-check first, and the
    whole body is wrapped so a ledger failure can NEVER affect whether the toggle
    applied or what the endpoint returns. No-op when the ledger is off (the
    standalone default).

    A row is written on every successful toggle *apply*. The Festival endpoint
    returns early on an idempotent no-op (re-enable while already on), so a no-op
    festival press records nothing; the other four always re-apply their side
    effects (re-persist, re-arm/re-purge), so a re-press records an honest
    ``old_value == new_value`` row — "the operator applied this", not "this changed".
    """
    led = getattr(request.app.state, "ledger", None)
    if led is None or not led.enabled:
        return
    try:
        import time as _time

        from mammamiradio.core.ledger import SCHEMA_VERSION

        led.record(
            {
                "schema_version": SCHEMA_VERSION,
                "ts": _time.time(),
                "record": "operator_action",
                "action": action,
                "old_value": old_value,
                "new_value": new_value,
                "source": "admin",
            }
        )
    except Exception as exc:  # pragma: no cover - provenance must never break a toggle
        logger.debug("Provenance operator_action emit failed: %s", exc)


def _record_heading_ledger(
    request,
    *,
    requested_seed: str,
    added_count: int,
    zero_result: bool,
    persisted: bool,
    source: str,
) -> None:
    """Best-effort audit row for operator course changes."""
    led = getattr(request.app.state, "ledger", None)
    if led is None or not led.enabled:
        return
    try:
        import time as _time

        from mammamiradio.core.ledger import SCHEMA_VERSION

        led.record(
            {
                "schema_version": SCHEMA_VERSION,
                "ts": _time.time(),
                "record": "heading",
                "requested_seed": requested_seed,
                "added_count": added_count,
                "zero_result": zero_result,
                "persisted": persisted,
                "source": source,
            }
        )
    except Exception as exc:  # pragma: no cover - provenance must never affect audio
        logger.debug("Provenance heading emit failed: %s", exc)


def _track_from_music_metadata(metadata: dict) -> Track | None:
    """Build a lightweight Track object from queued music metadata."""
    title = str(metadata.get("title_only") or metadata.get("title") or "").strip()
    artist = str(metadata.get("artist") or "").strip()
    if not title and not artist:
        return None
    return Track(
        title=title,
        artist=artist,
        duration_ms=0,
        spotify_id=str(metadata.get("spotify_id") or "").strip(),
        youtube_id=str(metadata.get("youtube_id") or "").strip(),
    )


async def _persist_completed_music(state: StationState, config, metadata: dict, *, listen_sec: float | None) -> None:
    """Persist only music that actually finished streaming to listeners."""
    if metadata.get("source_kind") == "jamendo" or metadata.get("audio_source") == "jamendo":
        return
    track = _track_from_music_metadata(metadata)
    if track is None:
        return

    from mammamiradio.scheduling.producer import _record_motif

    await _record_motif(state, track, config, listen_duration_s=listen_sec)


async def _persist_skipped_music(state: StationState, config, metadata: dict, *, listen_sec: float) -> None:
    """Persist a real skip so cross-session skip-bit detection has source data."""
    persona_store = getattr(state, "persona_store", None)
    yt_id = str((metadata or {}).get("youtube_id") or "").strip()
    if not persona_store or not yt_id:
        return

    await persona_store.record_play(
        yt_id,
        persona_store._session_id,
        skipped=True,
        listen_duration_s=listen_sec,
    )

    from mammamiradio.playlist.song_cues import detect_skip_bit

    persona_cfg = getattr(config, "persona", None)
    skip_t = persona_cfg.skip_bit_threshold if persona_cfg else 2
    is_new_skip_bit = await detect_skip_bit(config.cache_dir / "mammamiradio.db", yt_id, threshold=skip_t)

    if is_new_skip_bit and not state.ha_pending_directive:
        from mammamiradio.hosts.scriptwriter import _sanitize_prompt_data

        raw_name = metadata.get("title_only") or metadata.get("title") or "questa canzone"
        track_name = _sanitize_prompt_data(str(raw_name), max_len=80)
        state.ha_pending_directive = (
            f"L'ascoltatore ha saltato '{track_name}' troppe volte — "
            "reagisci in modo complice, scherzoso. Fai notare che la skippa sempre."
        )
        state.ha_pending_directive_moment_id = ""  # not a ritual moment — no receipt
        state.ha_pending_directive_source = "skip_bit"
        state.pending_actions.append(
            {
                "type": "ha_directive",
                "source": "skip_bit",
                "label": track_name,
                "created_at": time.time(),
            }
        )


def _next_first_listen_chunk(chunk_iter: Iterator[bytes]) -> bytes | None:
    try:
        return next(chunk_iter)
    except StopIteration:
        return None


async def _audio_generator(request: Request):
    """Stream an eligible first-listen prelude, then the shared live station.

    The packaged show is emitted before subscribing to ``LiveStreamHub``.  It
    therefore cannot alter the shared queue or now-playing state, and its bytes
    give the speaker a runway while the ordinary station wakes for this client.
    """
    hub = request.app.state.stream_hub
    show_path = None
    if first_listen_show_required(request.app.state):
        try:
            show_path = await asyncio.wait_for(
                asyncio.to_thread(approved_first_listen_show_path),
                timeout=FIRST_LISTEN_SHOW_APPROVAL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("First Listen package approval exceeded the first-audio budget; joining live station")
        except Exception:
            # Package approval is local best-effort validation. A corrupt or
            # unreadable install must never delay or terminate the live stream.
            logger.warning("First Listen package approval failed; joining live station", exc_info=True)
    if show_path is not None:
        if await request.is_disconnected():
            return
        logger.info("First Listen client prelude: %s", show_path.name)
        try:
            chunk_iter = iter(iter_first_listen_show_chunks(show_path))
            while True:
                chunk = await asyncio.to_thread(_next_first_listen_chunk, chunk_iter)
                if chunk is None:
                    break
                yield chunk
        except OSError:
            # The reviewed package asset is optional at runtime: corruption or
            # an unreadable installation falls through to the normal instant-
            # audio ladder instead of terminating the speaker request.
            logger.warning("First Listen client prelude became unreadable; joining live station", exc_info=True)
        if await request.is_disconnected():
            return

    listener_id, listener_queue = hub.subscribe()

    try:
        while True:
            if await request.is_disconnected():
                break

            try:
                chunk = await asyncio.wait_for(listener_queue.get(), timeout=5.0)
            except TimeoutError:
                if not hub.has_listener(listener_id):
                    break
                continue

            if chunk is None:
                break

            yield chunk
    finally:
        hub.unsubscribe(listener_id)


async def _render_admin_response(request: Request, prefix: str) -> HTMLResponse:
    # CSP: 'unsafe-inline' is required because admin.html has inline event handlers
    # (onclick, oninput, onchange) on ~40 elements that cannot carry a nonce attribute.
    # esc() on all HA fields in admin.html is the load-bearing XSS defense.
    await _wait_for_first_listen_bootstrap(request.app.state)
    html = _get_injected_html("admin", _ADMIN_HTML, prefix)
    state = getattr(request.app.state, "station_state", None)
    stopped = "true" if bool(getattr(state, "session_stopped", False)) else "false"
    first_listen_entry = _first_listen_entry_state(request.app.state)
    # Keep the stopped-state first paint resilient to harmless body-tag
    # formatting or attributes added by future admin-page work.
    html = _re.sub(
        r"(</head>\s*<body)(?![^>]*\bdata-stopped\b)",
        lambda match: f'{match.group(1)} data-stopped="{stopped}" data-first-listen-entry="{first_listen_entry}"',
        html,
        count=1,
    )
    html = _inject_csrf_token(html, _get_csrf_token(request.app))
    csp = "script-src 'self' 'unsafe-inline'"
    return HTMLResponse(content=html, headers={"Content-Security-Policy": csp})


def _listener_context(request: Request, config, prefix: str) -> dict:
    """Build the Jinja2 context for the listener page.

    `copy` is a frozen dict in the active super_italian_mode; templates use
    `{{ copy.get('key', 'fallback') }}`. The same dict serializes into the
    listener.html `<script type="application/json">` bootstrap so JS reads it
    once at page load instead of refetching on every /public-status poll.
    """
    state = getattr(request.app.state, "station_state", None)
    return {
        "brand": config.brand,
        # Keep listener-facing format copy aligned with /public-status and
        # /stream headers; this helper is the canonical audio-format source.
        "stream_bitrate_kbps": stream_audio_metadata(config)["bitrate_kbps"],
        "ingress_prefix": _sanitize_ingress_prefix(prefix),
        "csrf_token": _get_csrf_token(request.app),
        "asset_version": _ASSET_VERSION,
        "copy": copy_strings(bool(config.super_italian_mode)),
        # Bake the live/stopped state into the first paint so a stopped station
        # never flashes as "live" before the first JS poll hydrates (honesty).
        "session_stopped": bool(getattr(state, "session_stopped", False)),
        # Reflect the active copy register so screen readers don't read English
        # utility copy with Italian phonemes (super_italian off => en).
        "page_lang": "it" if config.super_italian_mode else "en",
    }


@router.get("/", response_class=HTMLResponse)
async def listener_home(request: Request):
    """Serve the public listener UI, except trusted HA ingress opens the control room."""
    prefix = request.headers.get("X-Ingress-Path", "")
    config = request.app.state.config
    if config.is_addon and prefix and _is_hassio_or_loopback(request):
        return await _render_admin_response(request, prefix)
    return _TEMPLATES.TemplateResponse(
        request,
        "listener.html",
        _listener_context(request, config, prefix),
    )


@router.get("/dashboard", response_class=RedirectResponse, dependencies=[Depends(require_admin_access)])
async def dashboard(request: Request):
    """Redirect legacy dashboard traffic to the admin control room."""
    prefix = request.headers.get("X-Ingress-Path", "")
    return RedirectResponse(url=f"{prefix}/admin", status_code=301)


@router.get("/admin", response_class=HTMLResponse, dependencies=[Depends(require_admin_access)])
async def admin_panel(request: Request):
    """Serve the admin control room panel."""
    prefix = request.headers.get("X-Ingress-Path", "")
    return await _render_admin_response(request, prefix)


@router.get("/listen", response_class=HTMLResponse)
async def listener(request: Request):
    """Backwards-compatible alias for the listener UI."""
    prefix = request.headers.get("X-Ingress-Path", "")
    config = request.app.state.config
    return _TEMPLATES.TemplateResponse(
        request,
        "listener.html",
        _listener_context(request, config, prefix),
    )


_og_card_cache: dict[str, bytes] = {}
_OG_CARD_FALLBACK = b""  # populated lazily on first miss


@router.get("/og-card.png")
async def og_card(request: Request):
    """Serve the OG social card PNG. Cached by brand+track key.

    Per design D-Design-2: poster-style 1200x630, brand-dominant typography,
    Italian flag tricolor at top, track info as lower-third band. Falls back
    to the static logo PNG if generation fails — social previews never 404.
    """
    config = request.app.state.config
    state = request.app.state.station_state
    track = state.current_track
    cache_key = f"{config.display_station_name}:{track.cache_key if track else 'idle'}"

    cached = _og_card_cache.get(cache_key)
    if cached is None:
        try:
            from mammamiradio.web.og_card import render_og_card_for_brand

            cached = render_og_card_for_brand(config.brand, track)
            _og_card_cache[cache_key] = cached
            # Cap cache size (one entry per track + idle is bounded by playlist size)
            if len(_og_card_cache) > 200:
                # Evict oldest entry by insertion order
                _og_card_cache.pop(next(iter(_og_card_cache)))
        except Exception as exc:
            logger.warning("OG card render failed: %s; falling back to logo", exc)
            fallback = _STATIC_DIR / "icon-192.svg"
            if fallback.exists():
                return FileResponse(fallback, media_type="image/svg+xml")
            return Response(status_code=503)

    headers = {"Cache-Control": "public, max-age=60"}
    return Response(content=cached, media_type="image/png", headers=headers)


@router.get("/favicon.ico")
async def favicon():
    """Serve the canonical app logo at the browser's default favicon path."""
    return FileResponse(
        _STATIC_DIR / "favicon.svg",
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/sw.js")
async def service_worker():
    """Serve the PWA service worker from root scope."""
    return FileResponse(
        _STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


def _resolve_static_file(filename: str) -> Path | None:
    """Resolve a user-requested static asset path safely.

    Rejects absolute paths and ``..`` components before filesystem lookup, then
    confirms the resolved target stays under the static directory and is a file.
    """
    if filename.startswith("/") or ".." in Path(filename).parts:
        return None

    static_root = _STATIC_DIR.resolve()
    try:
        candidate = (static_root / filename).resolve()
    except OSError:
        return None

    if not candidate.is_relative_to(static_root) or not candidate.is_file():
        return None

    return candidate


@router.get("/static/{filename:path}")
async def static_files(filename: str):
    """Serve PWA static assets (manifest, icons)."""
    filepath = _resolve_static_file(filename)
    if filepath is None:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(filepath)


# Typographic characters people actually type (or that macOS/iOS smart-quote
# substitution inserts for them) mapped to their ASCII twins.
#
# Letters are deliberately NOT enumerated here. NFKD handles the ones that
# decompose, and _ascii_twin() derives the rest from the Unicode name, which
# covers 199 of the 314 Latin letters that latin-1 cannot carry. Only the named
# letters whose Unicode name contains no base letter at all (ENG, SCHWA, KRA,
# the ligatures, SHARP S) need a hand-written twin, and they are listed below.
# Curating the other 199 by hand is how "Łódź" shipped as "ódz" in the first
# place: the list is always one letter short of the next operator's name.
_HEADER_ASCII_FOLDS = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "−": "-",
        "…": "...",
        "•": "*",
        "Ŋ": "N",
        "ŋ": "n",
        "Ə": "E",
        "ə": "e",
        "Œ": "OE",
        "œ": "oe",
        "ẞ": "SS",
        "ĸ": "k",
    }
)

# Bytes an HTTP field value may not carry at all (RFC 9110: field-vchar is
# %x21-7E / %x80-FF, plus interior SP and HTAB). CR and LF are the header
# injection vector. The rest of C0 and DEL are rejected by real servers (h11
# refuses NUL, VT and FF outright), which reproduces this function's own
# outage class from a different direction. HTAB is legal but pointless
# in a station name, so it goes too rather than surviving as a stray tab.
_HEADER_FORBIDDEN_BYTES = dict.fromkeys([*range(0x20), 0x7F])

# "LATIN SMALL LETTER L WITH STROKE" -> l, "LATIN SMALL LETTER DOTLESS I" -> i,
# "LATIN LETTER SMALL CAPITAL G" -> g. The modifier words are skipped so the
# base letter behind them is what lands.
_LATIN_LETTER_NAME = _re.compile(
    r"^LATIN (?:(?P<case>CAPITAL|SMALL) )?LETTER (?:SMALL CAPITAL )?"
    r"(?:DOTLESS |TURNED |REVERSED |INSULAR |SCRIPT )?"
    r"(?P<base>[A-Z]{1,2})\b"
)


def _ascii_twin(char: str) -> str:
    """Derive an ASCII stand-in for a Latin letter latin-1 cannot carry.

    Reached only when NFKD yields nothing, which is true for every Latin letter
    formed by a stroke, bar or hook rather than by a combining accent. Dropping
    those silently turns a name into what looks like a typo: a Turkish name
    written with the dotless i lost every one of them, airing as ``Radyo Krmz``.
    The Unicode name is the fallback source for the base letter. Returns "" when
    the name carries no base letter (ENG, SCHWA, the ligatures); those are
    hand-mapped in _HEADER_ASCII_FOLDS instead.
    """
    match = _LATIN_LETTER_NAME.match(unicodedata.name(char, ""))
    if match is None:
        return ""
    base = match.group("base")
    # Some names omit CAPITAL/SMALL entirely (e.g. "LATIN LETTER YR"), so fall
    # back to the character's own case rather than guessing lowercase.
    upper = match.group("case") == "CAPITAL" or (match.group("case") is None and char.isupper())
    return base if upper else base.lower()


def _fold_char(char: str) -> str:
    """Reduce one character to something latin-1 can carry."""
    if _is_latin1(char):
        return char
    decomposed = unicodedata.normalize("NFKD", char).encode("latin-1", "ignore").decode("latin-1")
    return decomposed or _ascii_twin(char)


def _header_safe(value: object) -> str:
    """Reduce operator-supplied text to a legal, latin-1-encodable field value.

    The guarantee is about the output, not an absolute promise about the call:
    whatever comes back is encodable and is legal HTTP field content, so no
    configured station name or public tagline can break the response. It is stated that
    way deliberately. An earlier "cannot 500" wording was an overclaim, since a
    caller could still hand this an object whose ``__str__`` raises. Nothing in
    a parsed TOML config can.

    Starlette encodes every response header with latin-1, so a single curly
    apostrophe in the station name raises UnicodeEncodeError while the response
    is being built and takes the whole /stream request down with it: no audio,
    for every listener, until the name is changed.

    The steps, each one load-bearing:

    * Coerce to ``str``. ``BrandSection`` is built straight from TOML with no
      runtime coercion, so ``tagline = 42`` in ``radio.toml`` reaches this
      function as an int and used to 500 every listener the same way.
    * Compose to NFC. macOS hands over decomposed text (``a`` + U+0300
      combining grave) for the same ``à`` that Linux writes as one codepoint,
      and only the composed form is latin-1. Without this the combining mark
      alone is dropped and ``Città`` airs as ``Citta``. Before this function
      existed it took /stream down exactly like a curly apostrophe did.
    * Drop C0 and DEL. CR/LF are the header injection vector, and the rest of
      that range is illegal field content that a strict server rejects, which
      would take /stream down exactly like the encode crash did.
    * Fold typographic punctuation to ASCII, then reduce whatever is still
      outside latin-1 (see _fold_char) and drop what has no stand-in at all
      (emoji, CJK).
    * Strip the ends. Folding an emoji away leaves the space beside it, and
      h11 rejects a field value with leading or trailing whitespace outright,
      the same total-failure blast radius as the bug this function fixes.
      Stripping also lets a value that folded away to nothing read as falsy so
      the caller's default can take over.

    Accented Latin letters are latin-1 once composed, so ``Radio Città`` and
    ``Caffè`` keep their accents; only the genuinely unencodable characters
    degrade.
    """
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    cleaned = unicodedata.normalize("NFC", value)
    cleaned = cleaned.translate(_HEADER_FORBIDDEN_BYTES).translate(_HEADER_ASCII_FOLDS)
    try:
        cleaned.encode("latin-1")
    except UnicodeEncodeError:
        # Per character, so one emoji cannot flatten the accents around it.
        cleaned = "".join(_fold_char(char) for char in cleaned)
    return cleaned.strip()


def _is_latin1(char: str) -> bool:
    try:
        char.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


@router.get("/stream")
async def stream(request: Request):
    """Expose the live MP3 stream consumed by browsers and audio players."""
    config = request.app.state.config
    audio_format = stream_audio_metadata(config)
    # ``station.theme`` is a scriptwriter prompt. Use the public brand tagline
    # for the listener-facing ``icy-genre`` header.
    # Fold before capping: the fold expands characters (``…`` becomes three
    # dots), so a cap applied first would not bound what actually ships.
    # Then strip again after the cut: a space landing at index 63 would put
    # trailing whitespace back, and h11 refuses the whole response for it.
    # Both steps are load-bearing — see test_stream_icy_genre_* for the guards.
    icy_genre = _header_safe(config.brand.tagline)[:64].strip()
    headers = {
        # A name made entirely of unencodable characters folds to "", so fall
        # back and let the player show the station rather than a blank label.
        "icy-name": _header_safe(config.display_station_name) or DEFAULT_STATION_NAME,
        "icy-br": str(audio_format["bitrate_kbps"]),
        "Cache-Control": "no-cache, no-store",
        "Connection": "keep-alive",
    }
    # Omit ``icy-genre`` when no listener-facing tagline survives header folding.
    if icy_genre:
        headers["icy-genre"] = icy_genre
    return StreamingResponse(
        _audio_generator(request),
        headers=headers,
        media_type=audio_format["mime_type"],
    )


@router.get("/api/logs")
async def logs(request: Request, lines: int = 50, _: None = Depends(require_admin_access)):
    """Return recent producer logs."""
    return {}


@router.get("/api/setup/status")
async def setup_status(request: Request, _: None = Depends(_require_active_setup_access)):
    """Return the current first-run setup snapshot for onboarding."""
    await _wait_for_first_listen_bootstrap(request.app.state)
    return _setup_projection(request)["setup"]


@router.post("/api/setup/recheck")
async def setup_recheck(request: Request, _: None = Depends(_require_active_setup_access)):
    """Force a fresh setup snapshot."""
    _body, error = await _strict_setup_json(request, {})
    if error is not None:
        return error
    await _wait_for_first_listen_bootstrap(request.app.state)
    return _setup_projection(request, force_refresh=True)["setup"]


@router.post("/api/setup/first-listen/players")
async def setup_first_listen_players(request: Request, _: None = Depends(_require_active_setup_access)):
    """Run explicit HA/HACS preflight and return sanitized speaker choices."""
    _body, error = await _strict_setup_json(request, {})
    if error is not None:
        return error
    service = _ha_playback_service(request.app.state)
    try:
        discovery = await service.discover(force=True)
    except HAPlaybackError as exc:
        return _setup_error(exc.reason.value, addon_mode=request.app.state.config.is_addon)

    receipt = getattr(request.app.state, "first_listen_receipt", None)
    if receipt is None:
        try:
            receipt = await _first_listen_store(request.app.state).load()
        except Exception:
            receipt = None
        request.app.state.first_listen_receipt = receipt
    candidates = [
        {
            "entity_id": candidate.entity_id,
            "friendly_name": candidate.friendly_name,
            "state": candidate.state,
            "device_class": candidate.device_class,
            "area": candidate.area,
            "supports_play_media": candidate.supports_play_media,
            "available": candidate.available,
        }
        for candidate in discovery.candidates
    ]
    selected_entity_id = str(getattr(receipt, "selected_entity_id", "") or "")
    return {
        "ok": True,
        "media_source_ready": discovery.media_source_ready,
        "media_source_uri": "media-source://mammamiradio/live",
        "candidates": candidates,
        "players": candidates,
        "selected_entity_id": selected_entity_id,
        "receipt_recovery": _pending_receipt_recovery(request.app.state, service=service),
    }


@router.post("/api/setup/first-listen/play")
async def setup_first_listen_play(request: Request, _: None = Depends(_require_active_setup_access)):
    """Resume the station and ask HA to play the exact stable media source."""
    body, error = await _strict_setup_json(request, {"entity_id": str})
    if error is not None:
        return error
    entity_id = str(body["entity_id"])
    try:
        result = await _ha_playback_service(request.app.state).play(entity_id)
    except HAPlaybackError as exc:
        return _setup_error(
            exc.reason.value,
            addon_mode=request.app.state.config.is_addon,
            station_resumed=exc.station_resumed,
        )
    if result.receipt_persisted is not True or not result.attempt_id:
        return _setup_error(
            HAPlaybackReason.RECEIPT_UNAVAILABLE.value,
            accepted=True,
            receipt_persisted=False,
            station_resumed=result.station_resumed,
            extra={"entity_id": result.entity_id},
        )
    return {
        "ok": True,
        "accepted": True,
        "receipt_persisted": True,
        "station_resumed": result.station_resumed,
        "entity_id": result.entity_id,
        "attempt_id": result.attempt_id,
        "media_source_uri": "media-source://mammamiradio/live",
        "message": "Home Assistant accepted the request. Audible confirmation is still yours.",
    }


@router.post("/api/setup/first-listen/receipt/retry")
async def setup_first_listen_receipt_retry(request: Request, _: None = Depends(_require_active_setup_access)):
    """Persist the server-owned accepted attempt without sending playback again."""
    body, error = await _strict_setup_json(request, {"entity_id": str})
    if error is not None:
        return error
    try:
        result = await _ha_playback_service(request.app.state).persist_pending_receipt(str(body["entity_id"]))
    except HAPlaybackError as exc:
        if exc.reason is HAPlaybackReason.RECEIPT_UNAVAILABLE:
            return _setup_error(
                exc.reason.value,
                accepted=True,
                receipt_persisted=False,
                station_resumed=exc.station_resumed,
                extra={"entity_id": str(body["entity_id"])},
            )
        return _setup_error(
            exc.reason.value,
            addon_mode=request.app.state.config.is_addon,
            station_resumed=exc.station_resumed,
        )
    if result.receipt_persisted is not True or not result.attempt_id:
        return _setup_error(
            HAPlaybackReason.RECEIPT_UNAVAILABLE.value,
            accepted=True,
            receipt_persisted=False,
            station_resumed=result.station_resumed,
            extra={"entity_id": result.entity_id},
        )
    return {
        "ok": True,
        "accepted": True,
        "receipt_persisted": True,
        "station_resumed": result.station_resumed,
        "entity_id": result.entity_id,
        "attempt_id": result.attempt_id,
        "message": "The accepted listening check is saved. No playback request was sent again.",
    }


@router.post("/api/setup/first-listen/verify")
async def setup_first_listen_verify(request: Request, _: None = Depends(_require_active_setup_access)):
    """Record the operator's Yes/Not-yet answer for the current attempt."""
    body, error = await _strict_setup_json(request, {"attempt_id": str, "heard": bool})
    if error is not None:
        return error
    try:
        receipt = await _first_listen_store(request.app.state).verify(
            str(body["attempt_id"]),
            heard=bool(body["heard"]),
        )
    except FirstListenAttemptMismatchError:
        return _setup_error("attempt_mismatch")
    except (FirstListenReceiptUnavailableError, OSError):
        return _setup_error("receipt_unavailable", accepted=True, receipt_persisted=False)
    request.app.state.first_listen_receipt = receipt
    heard = bool(body["heard"])
    return {
        "ok": True,
        "heard": heard,
        "first_listen_achieved": bool(receipt.audio_complete),
        "attempt_id": receipt.accepted_attempt_id,
        "repair": None
        if heard
        else {
            "title": "Let's get the sound to the right room",
            "steps": [
                "Wait a few seconds, then check the speaker's mute and volume in Home Assistant.",
                "Confirm the selected speaker is the one you expected.",
                "Open HA's media browser and test media-source://mammamiradio/live.",
                "If it is missing, reload the HACS integration and recheck add-on connectivity.",
            ],
        },
    }


@router.post("/api/setup/home-context-preview")
async def setup_home_context_preview(request: Request, _: None = Depends(_require_active_setup_access)):
    """Fetch a fresh, detached privacy preview on explicit operator action."""
    _body, error = await _strict_setup_json(request, {})
    if error is not None:
        return error
    app_state = request.app.state
    if not await _first_listen_audio_gate_open(app_state):
        return _setup_error("first_listen_required")
    config = app_state.config
    state = app_state.station_state
    if not config.homeassistant.enabled or not config.ha_token:
        return _setup_error("ha_access_missing", addon_mode=config.is_addon)

    authorization = state.home_authorization or HomeAuthorization.narrow()
    revision_before = policy_revision(config.cache_dir)
    generation_before = getattr(state, "home_context_policy_generation", 0)
    result = await _fetch_home_context_preview_singleflight(app_state, authorization)
    if result is None or not result.is_fresh:
        if result is None:
            return _setup_error("preview_unavailable")
        return _setup_error(result.error_code or "preview_unavailable")
    if revision_before != policy_revision(config.cache_dir) or generation_before != getattr(
        state, "home_context_policy_generation", 0
    ):
        return _setup_error("preview_unavailable")

    proof = _HomeContextPreviewProof(
        expires_at=time.monotonic() + _HOME_PREVIEW_PROOF_TTL_SECONDS,
        config_fingerprint=_home_access_fingerprint(config),
        authorization_mode=authorization.mode.value,
        policy_revision=revision_before,
        context_generation=generation_before,
    )
    app_state.home_context_preview_proof = proof
    preview = _safe_detached_home_context_preview(result.context, config)
    preview["proof_expires_in_seconds"] = int(_HOME_PREVIEW_PROOF_TTL_SECONDS)
    if preview["context_value"] == "ambient_only":
        preview["message"] = (
            "Only generic daylight is available. It is disclosed below, but it would not make the station "
            "meaningfully more personal. Nothing was promoted into host context or sent to an AI provider."
        )
    elif preview["context_value"] == "empty":
        preview["message"] = (
            "No useful Home context is available. Nothing was promoted into host context or sent to an AI provider."
        )
    else:
        preview["message"] = "Preview only — nothing here was promoted into host context or sent to an AI provider."
    return preview


@router.patch("/api/setup/home-context-choice")
async def setup_home_context_choice(request: Request, _: None = Depends(_require_active_setup_access)):
    """Persist and apply explicit Keep off / Enable Home context choices."""
    body, error = await _strict_setup_json(request, {"enabled": bool})
    if error is not None:
        return error
    app_state = request.app.state
    enabled = bool(body["enabled"])
    lock = getattr(app_state, "home_context_choice_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app_state.home_context_choice_lock = lock

    async with lock:
        if enabled and not await _first_listen_audio_gate_open(app_state):
            return _setup_error("first_listen_required")
        preview_proof = getattr(app_state, "home_context_preview_proof", None)
        if enabled and not _home_context_preview_proof_valid(app_state, preview_proof):
            return _setup_error("preview_required")

        persisted = True
        purged = 0
        if enabled:
            try:
                await _persist_home_context_choice(app_state.config, True)
            except OSError:
                return _setup_error(
                    "privacy_persist_failed",
                    extra={"enabled": False, "persisted": False},
                )
            # Supported entity-policy writes share this route's lock, but an
            # out-of-band policy/config change can still land while the add-on
            # option is being persisted. Re-check the exact preview proof after
            # that await. If it drifted, compensate the already-written choice
            # and latch explicit-off so a delayed install-origin task cannot
            # widen the runtime behind this failed enable attempt.
            if not _home_context_preview_proof_valid(app_state, preview_proof):
                app_state.home_context_choice_explicit = True
                app_state.home_context_preview_proof = None
                try:
                    await _persist_home_context_choice(app_state.config, False)
                except OSError:
                    persisted = False
                purged = await _disable_home_context_runtime(app_state)
                if not persisted:
                    return _setup_error(
                        "privacy_persist_failed",
                        extra={
                            "enabled": False,
                            "persisted": False,
                            "live_off": True,
                            "purged_pending_segments": purged,
                        },
                    )
                return _setup_error(
                    "preview_required",
                    extra={
                        "enabled": False,
                        "persisted": True,
                        "purged_pending_segments": purged,
                    },
                )
            _enable_home_context_runtime(app_state)
        else:
            # This in-process latch is authoritative even when the durable
            # write later fails: a delayed install-origin migration must never
            # reinterpret an explicit Keep off action and turn context back on.
            app_state.home_context_choice_explicit = True
            purged = await _disable_home_context_runtime(app_state)
            try:
                await _persist_home_context_choice(app_state.config, False)
            except OSError:
                persisted = False
        if not persisted:
            return _setup_error(
                "privacy_persist_failed",
                extra={
                    "enabled": False,
                    "persisted": False,
                    "live_off": True,
                    "purged_pending_segments": purged,
                },
            )

        app_state.home_context_choice_explicit = True
        app_state.home_context_preview_proof = None
        try:
            receipt = await _first_listen_store(app_state).record_privacy_reviewed()
        except (FirstListenReceiptUnavailableError, OSError):
            return _setup_error(
                "privacy_receipt_unavailable",
                extra={"enabled": enabled, "persisted": True},
            )
        app_state.first_listen_receipt = receipt
        return {
            "ok": True,
            "enabled": enabled,
            "persisted": True,
            "privacy_reviewed": True,
            "purged_pending_segments": purged,
            "message": (
                "Home context is enabled locally. Without an AI provider, it is not sent to one."
                if enabled
                else "Home context is off and retained prompt context was cleared."
            ),
        }


@router.post("/api/setup/provider-check")
async def setup_provider_check(request: Request, _: None = Depends(_require_active_setup_access)):
    """Run active, secret-safe Anthropic/OpenAI connectivity checks.

    Multiple rapid clicks should share one in-flight probe set instead of
    launching overlapping 12-second outbound checks against every provider.
    """
    _body, error = await _strict_setup_json(request, {})
    if error is not None:
        return error
    config = request.app.state.config

    def _record_if_task_keys_match(probe_result: dict) -> None:
        # The verdict must reflect the keys the SHARED in-flight task actually probed,
        # not this waiter's snapshot. A later request joining an old task after a
        # concurrent save swapped a key must NOT accept that task's stale 401. Compare
        # current config to the keys captured when the task was created.
        snapshot = getattr(request.app.state, "_provider_check_task_keys", None)
        if snapshot == (
            config.anthropic_api_key,
            config.openai_api_key,
            config.azure_speech_key,
            config.azure_speech_region,
            config.elevenlabs_api_key,
        ):
            _record_provider_verdict(request.app.state.station_state, probe_result)

    lock = getattr(request.app.state, "_provider_check_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state._provider_check_lock = lock

    async with lock:
        cached_at = getattr(request.app.state, "_provider_check_cached_at", 0.0)
        cached_result = getattr(request.app.state, "_provider_check_cached_result", None)
        if cached_result is not None and time.time() - cached_at < 2.0:
            return cached_result

        task = getattr(request.app.state, "_provider_check_task", None)
        if task is not None and task.done():
            # Task finished but result wasn't cached yet (done-but-uncached window).
            # Cache it now to close the race instead of spawning a second probe.
            try:
                result = task.result()
            except BaseException:
                request.app.state._provider_check_task = None
            else:
                request.app.state._provider_check_cached_result = result
                request.app.state._provider_check_cached_at = time.time()
                request.app.state._provider_check_task = None
                _record_if_task_keys_match(result)
                return result
            task = None
        if task is None:
            # Capture the keys this task probes so the verdict can't be misattributed
            # to a later config (Codex: snapshot travels with the task, not the waiter).
            request.app.state._provider_check_task_keys = (
                config.anthropic_api_key,
                config.openai_api_key,
                config.azure_speech_key,
                config.azure_speech_region,
                config.elevenlabs_api_key,
            )
            task = asyncio.create_task(check_provider_keys(config))
            request.app.state._provider_check_task = task

    try:
        result = await task
    except BaseException:
        async with lock:
            if getattr(request.app.state, "_provider_check_task", None) is task:
                request.app.state._provider_check_task = None
        raise

    async with lock:
        if getattr(request.app.state, "_provider_check_task", None) is task:
            request.app.state._provider_check_cached_result = result
            request.app.state._provider_check_cached_at = time.time()
            request.app.state._provider_check_task = None
    _record_if_task_keys_match(result)
    return result


@router.post("/api/setup/save-keys")
async def save_keys(request: Request, _: None = Depends(_require_active_setup_access)):
    """Save API credentials to .env or add-on secrets.env and update the live config."""
    body, error = await read_json_object(request)
    if error is not None:
        return error
    updates = _credential_updates_from_env_payload(body, require_nonempty=True)

    if not updates:
        return {"ok": False, "error": "No keys provided"}

    try:
        await _persist_and_apply_credentials(request, updates, use_addon_options=True)
    except _AddonPersistenceError:
        logger.error("Failed to persist add-on credentials", exc_info=True)
        return JSONResponse(status_code=500, content={"ok": False, "error": "failed to save credentials"})

    return {"ok": True, "saved": list(updates.keys())}


def _credential_updates_from_env_payload(body: dict, *, require_nonempty: bool) -> dict[str, str]:
    updates: dict[str, str] = {}
    for env_key in _CREDENTIAL_ENV_TO_FIELD:
        value = body.get(env_key)
        if not isinstance(value, str):
            continue
        clean = _sanitize_credential_value(value.strip())
        if require_nonempty and not clean:
            continue
        updates[env_key] = clean
    return updates


def _credential_updates_from_field_payload(body: dict) -> dict[str, str]:
    updates: dict[str, str] = {}
    for field, (env_key, _config_attr) in _CREDENTIAL_FIELDS.items():
        if field not in body:
            continue
        updates[env_key] = _sanitize_credential_value(str(body[field]).strip())
    return updates


async def _persist_and_apply_credentials(request: Request, updates: dict[str, str], *, use_addon_options: bool) -> None:
    """Persist credential updates and apply them to env/live config."""
    config = request.app.state.config
    loop = asyncio.get_running_loop()
    if use_addon_options and config.is_addon:
        outcome = await _run_addon_persistence(_save_addon_options, updates)
        if outcome != _SECRET_WRITE_DURABLE:
            raise _AddonPersistenceError("Unable to confirm the add-on credential save")
    else:
        await loop.run_in_executor(None, _save_dotenv, updates)

    _apply_live_credentials(request.app.state.station_state, config, updates)

    # Re-validate the freshly-saved key in the background so the admin reflects a bogus
    # key WITHOUT waiting for a banter segment to fail. Applies to EVERY credential-save
    # path (both /api/setup/save-keys and /api/credentials). Fire-and-forget so the
    # response stays fast (Leadership Principle #2); keep a reference so the task isn't
    # garbage-collected mid-flight (RUF006).
    request.app.state.provider_verdict_task = asyncio.create_task(_run_provider_verdict(request.app.state))


@router.get("/api/setup/addon-snippet")
async def setup_addon_snippet(request: Request, _: None = Depends(require_admin_access)):
    """Return a copy-friendly HA add-on configuration snippet."""
    config = request.app.state.config
    return {"snippet": addon_options_snippet(config)}


@router.get("/api/capabilities")
async def capabilities(request: Request, _: None = Depends(require_admin_access)):
    """Return current capability flags and derived tier.

    This is the new API that replaces the multi-step setup wizard payload.
    The dashboard uses these flags to show/hide cards and determine the
    current feature tier (Demo Radio / Your Music / Full AI Radio).
    """
    setup_projection = _setup_projection(request)
    config = setup_projection["config"]
    state = setup_projection["state"]
    caps = get_capabilities(config, state)
    result = capabilities_to_dict(caps)
    capabilities = result.setdefault("capabilities", {})
    capabilities["script_llm"] = bool(config.anthropic_api_key or config.openai_api_key)
    capabilities["anthropic_key"] = bool(config.anthropic_api_key)
    capabilities["openai"] = bool(config.openai_api_key)
    provider_health = setup_projection["provider_health"]
    capabilities["anthropic_degraded"] = provider_health["anthropic"]["degraded"]
    capabilities["anthropic_retry_after_s"] = provider_health["anthropic"]["retry_after_s"]
    # Tri-state key-validation verdict ("unverified" | "valid" | "rejected"), distinct
    # from the time-based `anthropic_degraded`. Lets the admin show a persistent
    # "key not working" state WITHOUT waiting for a banter segment to 401.
    capabilities["anthropic_key_status"] = provider_health["anthropic"]["key_status"]
    capabilities["openai_key_status"] = provider_health["openai"]["key_status"]
    # If a configured key was actively refused and nothing else is confirmed working,
    # steer next_step toward replacing it (placeholder copy — final operator wording is
    # a separate communication pass). Tier itself stays key-presence-derived (conservative).
    statuses = [
        provider_health["anthropic"]["key_status"] if config.anthropic_api_key else None,
        provider_health["openai"]["key_status"] if config.openai_api_key else None,
    ]
    # Only steer once the probes have settled: an "unverified" key is still in flight,
    # so don't nudge "replace your key" while a configured key might yet come back valid.
    if "rejected" in statuses and "valid" not in statuses and "unverified" not in statuses:
        result["next_step"] = {
            "key": "fix_llm_key",
            "message": "An AI key isn't working — replace it in Settings to restore AI hosts",
            "action": "open_settings",
        }

    now = state.now_streaming or {}
    result["now_playing"] = now

    # Shareware trial state
    from mammamiradio.scheduling.producer import SHAREWARE_CANNED_LIMIT

    result["trial"] = {
        "canned_clips_streamed": state.canned_clips_streamed,
        "limit": SHAREWARE_CANNED_LIMIT,
        "exhausted": state.canned_clips_streamed >= SHAREWARE_CANNED_LIMIT,
    }
    result["golden_path"] = setup_projection["golden_path"]
    result["guided_setup"] = setup_projection["setup"]["guided_setup"]
    result["startup_source_error"] = state.startup_source_error or ""
    result["provider_health"] = provider_health
    return result


@router.post("/api/homeassistant/labels/regenerate")
async def regenerate_homeassistant_labels(request: Request, _: None = Depends(require_admin_access)):
    """Force a background refresh of generated HA labels."""
    config = request.app.state.config
    if not config.homeassistant.context_enabled:
        return {"scheduled": False, "reason": "home_context_disabled"}
    if generation_in_progress():
        raise HTTPException(status_code=409, detail="HA label generation already in progress")
    if not config.anthropic_api_key:
        return {"scheduled": False, "reason": "anthropic_key_missing"}
    state = request.app.state.station_state
    authorization = state.home_authorization or HomeAuthorization.narrow()
    if not authorization.allows_label_generation:
        return {"scheduled": False, "reason": "no_candidates"}
    context = get_cached_home_context(config.cache_dir, authorization=authorization)
    if context is None or not context.raw_states:
        return {"scheduled": False, "reason": "home_context_unavailable"}
    scheduled = schedule_label_generation(
        context.raw_states,
        cache_dir=config.cache_dir,
        config=config,
        score_by_entity={entity.entity_id: entity.score for entity in context.scored},
        force=True,
    )
    if not scheduled:
        # schedule_label_generation returns False both when a refresh is already
        # running AND when there is simply nothing new to label. Only the former
        # is a conflict; the latter is a successful no-op.
        if generation_in_progress():
            raise HTTPException(status_code=409, detail="HA label generation already in progress")
        return {"scheduled": False, "reason": "no_candidates"}
    return {"scheduled": True}


@router.get("/api/homeassistant/context-candidates")
async def homeassistant_context_candidates(request: Request, _: None = Depends(require_admin_access)):
    """Return a sanitized admin-only preview of HA context candidates."""
    return _safe_home_entity_preview(request.app.state.station_state, request.app.state.config)


@router.patch("/api/homeassistant/entity-policy")
async def homeassistant_entity_policy(request: Request, _: None = Depends(_require_active_setup_access)):
    """Apply one idempotent Home Context privacy property mutation."""
    body, error = await read_json_object(request)
    if error is not None:
        return error
    entity_id = body.get("entity_id")
    if not isinstance(entity_id, str) or not valid_entity_id(entity_id):
        raise HTTPException(status_code=422, detail="entity_id must be a Home Assistant entity id")
    action_fields = [field for field in ("muted", "personal_moment_enabled") if field in body]
    if len(action_fields) != 1:
        raise HTTPException(status_code=422, detail="provide exactly one of muted or personal_moment_enabled")
    action = action_fields[0]
    value = body.get(action)
    if not isinstance(value, bool):
        raise HTTPException(status_code=422, detail=f"{action} must be true or false")

    config = request.app.state.config
    # No preview-membership gate: some entities (e.g. radio_event-only
    # entities, deliberately kept out of ambient context) never appear in the
    # sanitized preview but an operator still needs to be able to mute them by
    # id — over-inclusive muting can only exclude more, never expose more, so
    # any syntactically valid entity_id (already checked above) is accepted.
    state = request.app.state.station_state
    row = _home_entity_metadata(state, config, entity_id)
    if (
        action == "personal_moment_enabled"
        and value
        and not _personal_moment_entity_is_eligible(state, config, entity_id)
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "That entity can't host a personal moment. Pick a room-presence sensor "
                "that's showing activity right now, then turn it on."
            ),
        )
    loop = asyncio.get_running_loop()
    choice_lock = getattr(request.app.state, "home_context_choice_lock", None)
    if choice_lock is None:
        choice_lock = asyncio.Lock()
        request.app.state.home_context_choice_lock = choice_lock
    # The filtered-preview proof is bound to this policy's revision. Serialize
    # the durable mutation with Home-context enable so its proof cannot be
    # validated, followed by a concurrent unmute committing while the enabled
    # choice is being written. Runtime cleanup below may proceed independently;
    # the revision is the complete preview-invalidating boundary.
    async with choice_lock:
        try:
            mutation = set_entity_muted if action == "muted" else set_personal_moment_enabled
            policy = await loop.run_in_executor(
                None,
                functools.partial(
                    mutation,
                    config.cache_dir,
                    entity_id,
                    value,
                    label=row.get("label") or entity_id,
                    domain=row.get("domain") or entity_id.split(".", 1)[0],
                    area=row.get("area") or "",
                ),
            )
        except OSError as exc:
            logger.warning("Failed to save HA entity policy", exc_info=True)
            raise HTTPException(status_code=500, detail="could not save entity policy") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    purged_pending_banter_count = 0
    # Both a mute and revoking a personal-moment opt-in tighten privacy for this
    # entity: an unstarted queued break that references it must be pulled (the
    # already-airing segment finishes untouched), and the director must advance
    # so an in-flight render for the entity is rejected. Consent revocation
    # previously did neither, so a queued presence break could still air.
    hard_mute = action == "muted" and value
    privacy_tightened = hard_mute or (action == "personal_moment_enabled" and not value)
    revoked_label_tasks: tuple[asyncio.Task, ...] = ()
    if hard_mute:
        # A mute is also an LLM-data cutover. Advance the catalog epoch and
        # clear scene task identity before this handler yields again, otherwise
        # work admitted under the old policy could publish Home-derived text
        # after the mute was durably accepted.
        revoked_label_tasks = invalidate_label_generation()
        reset_scene_namer_cache()
    # In narrow mode a queued/in-flight break is tagged with the synthetic ambient
    # id (sun.ambient / weather.ambient); an operator may instead mute the real
    # underlying HA source. Expand the tightened id to its synthetic projection so
    # the purge and director invalidation honor a real-source mute exactly like the
    # fetch layer (no-op in legacy mode, where ambient_sources is empty).
    tightened_ids = {entity_id}
    # Read the RAW module cache (no cache_dir): passing cache_dir would apply the
    # mute we just wrote and strip this source's synthetic mapping before we read
    # it, defeating the expansion. ambient_sources is stable, non-sensitive routing.
    _ctx = get_cached_home_context(authorization=state.home_authorization)
    if _ctx is not None:
        tightened_ids |= {
            synthetic for synthetic, source in getattr(_ctx, "ambient_sources", {}).items() if source == entity_id
        }
    ledger_dirty = False
    if hard_mute:
        # A hard mute is a temporal boundary as well as a visibility filter:
        # discard the retained source/baseline now so an eventual unmute cannot
        # turn a private transition into a delayed radio event.
        invalidate_home_context_entity_baselines(tightened_ids)
        mailbox = getattr(state, "ha_context_refresh_mailbox", None)
        invalidate_muted_entities = getattr(mailbox, "invalidate_muted_entities", None)
        if callable(invalidate_muted_entities):
            invalidate_muted_entities(tightened_ids)
        ledger_dirty = _clear_home_context_usage(state, config, entity_id)
    elif action == "muted":
        _set_live_gag_entity_denied(state, config, entity_id, False)
    if privacy_tightened:
        purged_pending_banter_count = _purge_home_fact_banter_from_queue(request.app.state.queue, state, tightened_ids)
    # The mutation already returned the authoritative just-written policy; read
    # the revision off it instead of re-reading the file we just wrote.
    current_policy_revision = int(policy.get("policy_revision", 0) or 0)
    director = state.home_context_director
    if director is not None:
        invalidate = getattr(director, "invalidate_entity", None)
        if callable(invalidate) and privacy_tightened:
            # invalidate_entity reports the unstarted reservations the caller must
            # release. The queue purge above already released the physically
            # queued breaks (via record_discard); these remaining ids cover the
            # in-flight race — a fact reserved at admission but not yet enqueued —
            # which the physical-queue scan cannot see. release() is a no-op on
            # any id already cleared, so this cannot double-release.
            for tightened_id in tightened_ids:
                for pending_queue_id in invalidate(tightened_id, policy_revision=current_policy_revision):
                    director.release(pending_queue_id, fact_id=None)
    # All prompt-facing and queue state above is fenced synchronously. Slower
    # disk/task cleanup can now yield without letting old-policy work publish.
    if ledger_dirty and state.evening_ledger is not None:
        await loop.run_in_executor(None, state.evening_ledger.save_if_dirty, config.cache_dir)
    if revoked_label_tasks:
        try:
            await drain_invalidated_label_generation(revoked_label_tasks)
        except Exception:
            logger.warning("Home label-generation cancellation failed during entity mute")
    muted = entity_id in (policy.get("muted", {}) if isinstance(policy.get("muted"), dict) else {})
    personal_moment = entity_id in (
        policy.get("personal_moment_opt_ins", {}) if isinstance(policy.get("personal_moment_opt_ins"), dict) else {}
    )
    eligible = _personal_moment_entity_is_eligible(state, config, entity_id)
    return {
        "ok": True,
        "entity_id": entity_id,
        "muted": muted,
        "personal_moment_enabled": personal_moment,
        "personal_moment_eligible": eligible,
        "personal_moment_effective": bool(personal_moment and eligible and not muted),
        "policy_revision": current_policy_revision,
        "purged_pending_banter_count": purged_pending_banter_count,
        "current_segment_unchanged": True,
        "policy": {
            "schema_version": policy.get("schema_version"),
            "muted_count": len(policy.get("muted", {}) if isinstance(policy.get("muted"), dict) else {}),
            "personal_moment_count": len(
                policy.get("personal_moment_opt_ins", {})
                if isinstance(policy.get("personal_moment_opt_ins"), dict)
                else {}
            ),
        },
    }


@router.post("/api/shuffle")
async def shuffle_playlist(request: Request, _: None = Depends(require_admin_access)):
    """Shuffle upcoming tracks."""
    state = request.app.state.station_state
    _reserve_continuity_runway(request.app.state, state, request.app.state.config)
    _random.shuffle(state.playlist)
    state.playlist_revision += 1
    return {"ok": True, "message": "Playlist shuffled"}


async def _request_skip(
    app_state,
    state: StationState,
    config,
    *,
    source: str,
    discard_reason: str = GenerationWasteReason.OPERATOR_PURGE,
) -> bool:
    """Cut the airing segment now, bridging when no playback-valid runway exists.

    Shared by ``/api/skip`` and ``/api/track/ban-now-playing`` so the skip semantics
    (listener-skip record, empty-queue bridge, ``skip_event``, ``now_streaming`` ->
    skipping) can never drift between the two callers.

    Order matters for callers that mutate state first: ``ban-now-playing`` purges the
    queued copies of the banned song BEFORE calling this, and the bridge decision below
    reads the queue AFTER that purge — so a queue emptied by the ban still forces the
    next music instead of risking dead air (#2 INSTANT AUDIO). Returns whether a bridge
    was forced.
    """
    if state.skip_in_flight:
        raise RuntimeError("skip request already in flight")
    state.skip_in_flight = True
    try:
        return await _request_skip_once(
            app_state,
            state,
            config,
            source=source,
            discard_reason=discard_reason,
        )
    finally:
        state.skip_in_flight = False


async def _request_skip_once(
    app_state,
    state: StationState,
    config,
    *,
    source: str,
    discard_reason: str,
) -> bool:
    """Execute one Skip while :func:`_request_skip` owns the transport."""
    handoff_excluded_paths: set[Path] = set()
    handoff_excluded_track_keys: set[tuple[str, str]] = set()
    _cancel_active_handoff_from_queue(
        app_state.queue,
        state,
        reason=discard_reason,
        excluded_paths=handoff_excluded_paths,
        excluded_track_keys=handoff_excluded_track_keys,
    )
    _reserve_continuity_runway(
        app_state,
        state,
        config,
        discard_reason=discard_reason,
        excluded_paths=handoff_excluded_paths,
        excluded_track_keys=handoff_excluded_track_keys,
    )
    _discard_unplayable_queue_prefix(app_state.queue, state, reason=discard_reason)
    now_seg = state.now_streaming or {}
    skipped_music_metadata: dict | None = None
    skipped_music_listen_sec = 0.0
    if now_seg.get("type") == "music" and state.current_stream_audible:
        started = now_seg.get("started", time.time())
        skipped_music_listen_sec = time.time() - started
        skipped_music_metadata = now_seg.get("metadata") or {}
        state.listener.record_outcome(
            skipped=True,
            listen_sec=skipped_music_listen_sec,
            track_display=now_seg.get("label", ""),
        )
        # This cut already settled the audible track as skipped. Clearing its
        # snapshot prevents the next accepted segment from also settling it as
        # completed. A selected-but-unheard segment must leave the prior audible
        # snapshot alone so that prior song can still complete honestly.
        state._last_audible_stream = {}

    bridged = False
    if not _playable_runway_available(app_state.queue, state):
        state.force_next = SegmentType.MUSIC
        bridged = True
        state.pending_actions.append(
            {
                "type": "skip_bridge",
                "source": source,
                "label": "force next music",
                "created_at": time.time(),
            }
        )
        logger.info("Skip requested without playable runway — forcing next music before cut")

    app_state.skip_event.set()
    state.now_streaming = {"type": "skipping", "label": "Skipping...", "started": time.time(), "metadata": {}}
    # Commit every transport mutation before the first yield. A concurrent Stop
    # that lands while skip history persists must remain the final state rather
    # than being overwritten with a stale skipping sentinel or forced track.
    if skipped_music_metadata is not None:
        try:
            await _persist_skipped_music(
                state,
                config,
                skipped_music_metadata,
                listen_sec=skipped_music_listen_sec,
            )
        except Exception:
            # The cut is already committed above. Skip history improves later
            # programming, but must never turn a successful transport action
            # into an operator-facing failure.
            logger.warning("Could not persist skipped music history after transport commit", exc_info=True)
    return bridged


@router.post("/api/skip")
async def skip_track(request: Request, _: None = Depends(require_admin_access)):
    """Skip the currently streaming segment."""
    state = request.app.state.station_state
    if state.session_stopped:
        return {
            "ok": False,
            "error": "The station is paused. Press Start before skipping to the next track.",
        }
    if _skip_is_in_flight(state):
        return {
            "ok": False,
            "error": "That skip is already on its way — the next track is cueing up. Give it a second.",
        }
    # Audibility is the right gate: the loop parks when nobody is listening and
    # leaves the finished segment's metadata in place at EOF, so a selected-but-
    # inaudible `now_streaming` means there is genuinely nothing to cut. Only the
    # copy needed fixing — "Press Start" named a control the admin hides while
    # the station is running, so it read as broken rather than as idle.
    if not _now_streaming_is_real_media(state):
        return {
            "ok": False,
            "error": "Nothing is on air to skip yet. The station starts the moment someone tunes in.",
        }
    bridged = await _request_skip(request.app.state, state, request.app.state.config, source="admin_skip")
    return {"ok": True, "bridged": bridged}


@router.post("/api/purge")
async def purge_queue(request: Request, _: None = Depends(require_admin_access)):
    """Drain all pre-produced segments from the queue."""
    purged = _reserve_continuity_runway(
        request.app.state,
        request.app.state.station_state,
        request.app.state.config,
        replace_queue=True,
        discard_reason=GenerationWasteReason.OPERATOR_PURGE,
    )
    return {"ok": True, "purged": purged}


@router.post("/api/panic")
async def panic_cut(request: Request, _: None = Depends(require_admin_access)):
    """Emergency cut when safe runway exists; otherwise defer and force next music.

    While the station is running, this does NOT set session_stopped — the stream
    stays live and listeners do not disconnect. A stopped session rejects the
    action; use /api/resume before cutting again.
    """
    state = request.app.state.station_state
    if state.session_stopped:
        return {
            "ok": False,
            "error": "The station is paused. Press Start before using Panic Cut.",
        }
    epoch_before = state.continuity_epoch
    handoff_excluded_paths: set[Path] = set()
    handoff_excluded_track_keys: set[tuple[str, str]] = set()
    _cancel_active_handoff_from_queue(
        request.app.state.queue,
        state,
        reason=GenerationWasteReason.OPERATOR_PANIC,
        excluded_paths=handoff_excluded_paths,
        excluded_track_keys=handoff_excluded_track_keys,
    )
    purged = _reserve_continuity_runway(
        request.app.state,
        state,
        request.app.state.config,
        replace_queue=True,
        discard_reason=GenerationWasteReason.OPERATOR_PANIC,
        excluded_paths=handoff_excluded_paths,
        excluded_track_keys=handoff_excluded_track_keys,
    )
    # An assetless panic may leave the queue untouched. It still supersedes any
    # render already in flight: force_next only affects the next producer loop
    # iteration and cannot invalidate a segment that captured the old epoch.
    if state.continuity_epoch == epoch_before:
        state.continuity_epoch += 1
        # The reservation stamped its survivors against the pre-bump epoch, so
        # re-bless them here or the loop discards the runway this panic just
        # protected — the assetless path is exactly when there is least to spare.
        _stamp_continuity_runway_epoch(request.app.state.queue, state)
    skipped = False
    # A skip already in flight still has audio on air, so panic must be able to
    # cut it. Treating that window as "nothing streaming" made Panic silently
    # withhold skip_event with no log line at all.
    cut_target_on_air = _now_streaming_is_real_media(state) or _skip_is_in_flight(state)
    if cut_target_on_air and _playable_runway_available(request.app.state.queue, state):
        request.app.state.skip_event.set()
        skipped = True
        if state.current_stream_audible:
            # Panic is not a taste signal, but a committed cut must still settle
            # snapshot ownership: the next segment cannot report the cut track
            # as a clean completion. A withheld cut leaves it intact because the
            # current audio is allowed to finish.
            state._last_audible_stream = {}
    elif cut_target_on_air:
        logger.warning("Panic cut withheld because no playable runway is ready; current audio will finish")
    # force_next is set AFTER skip_event to avoid the producer consuming it
    # before the current segment has been cut.
    state.force_next = SegmentType.MUSIC
    logger.warning(
        "Panic action completed by admin — purged %d segments, skipped=%s, forcing next=music",
        purged,
        skipped,
    )
    return {"ok": True, "purged": purged, "skipped": skipped}


@router.post("/api/queue/remove")
async def queue_remove_item(request: Request, _: None = Depends(require_admin_access)):
    """Remove a single pre-produced segment from the queue.

    Identity vs position: the admin UI renders a row, then the click arrives
    after a network round-trip. In that window the streamer can consume the
    head segment, shifting every shadow-list index down by one. So callers
    SHOULD pass a stable ``id`` (the ``queue_id`` stamped by the producer);
    the legacy ``index`` path is kept for older callers and is position-based.

    The drain-and-repush below uses only synchronous queue operations
    (``get_nowait``/``put_nowait`` on an unbounded queue), so no ``await`` runs
    between draining the real queue and mutating the ``queued_segments`` shadow
    list. The producer and streamer therefore cannot interleave and leave the
    two views of the queue divergent.
    """
    body, error = await read_json_object(request)
    if error is not None:
        return error
    seg_id = body.get("id")
    index = body.get("index")

    state = request.app.state.station_state
    q = request.app.state.queue

    if not state.queued_segments:
        return {"ok": True, "removed": None}

    if isinstance(seg_id, str) and seg_id:
        # Identity path: resolve the current shadow-list position from the id.
        index = next(
            (i for i, seg in enumerate(state.queued_segments) if seg.get("id") == seg_id),
            None,
        )
        if index is None:
            # Segment already played out (or was removed) — nothing to do.
            return {"ok": True, "removed": None}
    elif isinstance(index, int):
        # Legacy position path.
        if index < 0 or index >= len(state.queued_segments):
            raise HTTPException(
                status_code=422,
                detail=f"index {index} out of range (queue has {len(state.queued_segments)} items)",
            )
    else:
        raise HTTPException(status_code=422, detail="index must be an integer")

    shadow_entry = state.queued_segments[index]
    removed_label = shadow_entry.get("label", "unknown")
    target_id = shadow_entry.get("id")

    # Synchronous drain + repush — no await points until the shadow list is
    # back in sync and continuity has been reserved against the FINAL queue, so
    # the producer/streamer cannot interleave and the removed item cannot be
    # counted as runway or selected again from the immediate-audio index.
    items: list = []
    while not q.empty():
        try:
            items.append(q.get_nowait())
            # Balance the unfinished-task counter for every drained item, the
            # same way _purge_segment_queue does — survivors are re-counted by
            # put_nowait below. Without this, queue.join() would never settle.
            q.task_done()
        except asyncio.QueueEmpty:
            break
    prior_tail = items[-1] if items else None

    # Remove the matching Segment from the real queue. Match by queue_id when
    # available (position-independent); fall back to index alignment otherwise.
    real_removed = False
    removed_segment = None
    if target_id:
        for i, seg in enumerate(items):
            if getattr(seg, "metadata", {}).get("queue_id") == target_id:
                removed_segment = items.pop(i)
                real_removed = True
                break
    if not real_removed and index < len(items):
        removed_segment = items.pop(index)

    handoff_source = handoff_original_source(state, removed_segment) if removed_segment is not None else None
    if removed_segment is not None:
        state.record_discard(
            removed_segment,
            reason=GenerationWasteReason.OPERATOR_QUEUE_REMOVE,
            already_counted_in_produced=True,
        )
        _drop_segment_moment_receipts(state, removed_segment, GenerationWasteReason.OPERATOR_QUEUE_REMOVE)
        _unlink_ephemeral_best_effort(removed_segment)

    # Removing either half of a private handoff pair must not leave a stale
    # successor in the real queue.  If the user removed only the successor,
    # the still-unstarted predecessor is restored before the shadow rebuild.
    orphaned = reconcile_handoff_queue_items(state, items)
    _discard_handoff_orphans(state, orphaned, GenerationWasteReason.OPERATOR_QUEUE_REMOVE)
    state.queued_segments.pop(index)
    _rebuild_queue_shadow(q, state, items)
    if removed_segment is not None:
        # A single arbitrary-position removal can expose a previously
        # interior, untrusted segment as the new tail — same risk
        # _apply_ban guards against. Re-run the fail-closed check rather
        # than trust _rebuild_queue_shadow's naive tail bookkeeping.
        _reconcile_queue_tail_adjacency(q, state, prior_tail=prior_tail)
    excluded_paths = {removed_segment.path} if removed_segment is not None else set()
    excluded_track_keys: set[tuple[str, str]] = set()
    if handoff_source is not None:
        original_path, original_music = handoff_source
        excluded_paths.add(original_path)
        original_key = _segment_track_key(original_music)
        if original_key != ("", ""):
            excluded_track_keys.add(original_key)
    _reserve_continuity_runway(
        request.app.state,
        state,
        request.app.state.config,
        discard_reason=GenerationWasteReason.OPERATOR_QUEUE_REMOVE,
        excluded_paths=excluded_paths,
        excluded_track_keys=excluded_track_keys,
    )

    logger.info("Queue item removed by admin: %s (id=%s)", removed_label, target_id or "n/a")
    return {"ok": True, "removed": removed_label}


@router.post("/api/stop")
async def stop_session(request: Request, _: None = Depends(require_admin_access)):
    """Persist and publish a fail-safe operator stop."""
    state = request.app.state.station_state
    app_state = request.app.state
    config = app_state.config

    # Persistence is the session boundary. If this fails, no live transport
    # state or queue may change.
    try:
        _persist_session_stopped(config, True)
    except Exception:
        logger.error("Could not persist operator stop; live state is unchanged", exc_info=True)
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error": "Couldn't save the stopped state. Nothing changed; try again.",
            },
        )

    state.session_stopped = True
    state.force_recovery_active = False
    state.current_stream_audible = False
    state.last_air_monotonic = None
    state.continuity_epoch += 1
    visible_epoch = state.continuity_epoch
    if _now_streaming_has_selected_media(state):
        app_state.skip_event.set()

    # An explicit stop cuts the airing music head for good: retract its queued
    # tail successor before the purge so the pair is released as one action.
    _cancel_active_handoff_from_queue(
        app_state.queue,
        state,
        reason=GenerationWasteReason.OPERATOR_STOP,
    )
    purged = _purge_queue_and_shadow(
        app_state.queue,
        state,
        reason=GenerationWasteReason.OPERATOR_STOP,
    )
    cleanup_warnings: list[str] = []

    # Drop every out-of-band continuation before publishing the stopped
    # sentinel. Cleanup is best-effort and cannot roll back the stop.
    if (
        state.interrupt_slot is not None
        and state.interrupt_slot_ephemeral
        and not _is_packaged_asset(state.interrupt_slot)
    ):
        try:
            state.interrupt_slot.unlink(missing_ok=True)
        except Exception as exc:
            cleanup_warnings.append(f"interrupt:{type(exc).__name__}")
            logger.warning("Stopped-state interrupt cleanup failed: %s", exc)
    state.interrupt_slot = None
    state.interrupt_slot_ephemeral = False
    if state.continuity_slot is not None:
        try:
            _unlink_ephemeral_best_effort(state.continuity_slot)
        except Exception as exc:  # pragma: no cover - helper is intentionally non-raising
            cleanup_warnings.append(f"continuity:{type(exc).__name__}")
    state.continuity_slot = None
    state.force_next = None
    state.operator_force_pending = None
    state.last_enqueued_type = None
    state.queue_empty_since = None
    state._last_audible_stream = {}
    state.resume_event.clear()
    app_state.last_shareworthy_clip = None

    state.last_state_change_at = time.time()
    state.now_streaming = {"type": "stopped", "label": "Session stopped", "started": time.time(), "metadata": {}}
    # Drop the remembered starter share snapshot so a clip can't leak across a
    # stop (the generic ad/banter clip snapshot is already cleared above).
    request.app.state.last_shareworthy_starter = None
    logger.info(
        "Session stopped by admin epoch=%d purged_segments=%d cleanup_warnings=%s",
        visible_epoch,
        purged,
        ",".join(cleanup_warnings) if cleanup_warnings else "none",
        extra={
            "event": "session_stopped",
            "continuity_epoch": visible_epoch,
            "purged_segments": purged,
            "cleanup_warnings": cleanup_warnings,
        },
    )
    return {"ok": True, "purged": purged}


@router.post("/api/resume")
async def resume_session(
    request: Request,
    force: bool = False,
    _: None = Depends(require_admin_access),
):
    """Resume with immediate audio, or explicitly force a host-audio rebuild."""
    state = request.app.state.station_state
    app_state = request.app.state
    config = request.app.state.config

    if not state.session_stopped:
        try:
            _persist_session_stopped(config, False)
        except Exception:
            logger.error("Running station marker reconciliation failed", exc_info=True)
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "error": "The station is playing, but we couldn't save that it's running. Try again.",
                },
            )
        logger.info("Resume reconciled restart state; active playback was unchanged")
        return {"ok": True, "recovering": state.force_recovery_active}

    if force:
        # Persistence remains the transaction boundary. An operator-confirmed
        # force start may rebuild without installed recovery assets, but a
        # marker-removal failure must still leave every live field untouched.
        try:
            _persist_session_stopped(config, False)
        except Exception:
            logger.error(
                "Forced resume marker removal failed; station remains stopped epoch=%d",
                state.continuity_epoch,
                exc_info=True,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "error": "Couldn't save the running state. The station is still paused; try again.",
                },
            )

        state.force_next = SegmentType.BANTER
        state.force_recovery_active = True
        _clear_session_stopped(state)
        state.resume_event.set()
        logger.warning(
            "Session force-resumed without recovery runway epoch=%d",
            state.continuity_epoch,
            extra={
                "event": "session_force_resumed",
                "runway_source": "none",
                "continuity_epoch": state.continuity_epoch,
            },
        )
        return {"ok": True, "recovering": True, "runway_source": "none"}

    # Resume needs *some* playable audio, not a full runway: the producer
    # replenishes once it wakes. This keeps the reservation non-empty even when
    # the runway floor is configured to 0, so the fail-closed check below is a
    # real test of playability rather than a no-op. The reservation is epoch-
    # stamped inside the call, so a queue waiter blocked since before the Stop
    # still accepts it.
    _reserve_continuity_runway(
        app_state,
        state,
        config,
        discard_reason=GenerationWasteReason.OPERATOR_STOP,
        minimum_runway_seconds=ANY_PLAYABLE_RUNWAY_SECONDS,
    )
    # The reservation counts ready seconds across every protected segment, but
    # the gate below reads only the queue head — the loop's next pull. A dead
    # head in front of a ready tail would satisfy the reservation and fail the
    # gate on every retry, leaving Start permanently refused with no way out.
    # Clear it here, in the same order `_request_skip` does.
    _discard_unplayable_queue_prefix(
        app_state.queue,
        state,
        reason=GenerationWasteReason.OPERATOR_STOP,
    )
    if not _playable_runway_available(app_state.queue, state):
        logger.warning(
            "Resume rejected: no immediately playable runway epoch=%d",
            state.continuity_epoch,
        )
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error": (
                    "No recovery audio is installed. Restore the packaged recovery assets, "
                    "or confirm Force Start to rebuild the station with host audio."
                ),
                "force_available": True,
            },
        )

    runway_source = _playable_runway_source(app_state.queue, state)
    try:
        _persist_session_stopped(config, False)
    except Exception:
        logger.error(
            "Resume marker removal failed; station remains stopped runway_source=%s epoch=%d",
            runway_source,
            state.continuity_epoch,
            exc_info=True,
        )
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error": "Couldn't save the running state. The station is still paused; try again.",
            },
        )

    _clear_session_stopped(state)
    state.force_recovery_active = False
    # Wake producer and playback only after runway, persistence, and live state
    # have all committed.
    state.resume_event.set()
    logger.info(
        "Session resumed by admin runway_source=%s epoch=%d",
        runway_source,
        state.continuity_epoch,
        extra={
            "event": "session_resumed",
            "runway_source": runway_source,
            "continuity_epoch": state.continuity_epoch,
        },
    )
    return {"ok": True, "recovering": False}


@router.post("/api/trigger")
async def trigger_segment(request: Request, _: None = Depends(require_admin_access)):
    """Force the next produced segment to be banter, ad, or news flash."""
    body, error = await read_json_object(request)
    if error is not None:
        return error
    seg_type = body.get("type", "").lower()
    valid = {"banter": SegmentType.BANTER, "ad": SegmentType.AD, "news_flash": SegmentType.NEWS_FLASH}
    if seg_type not in valid:
        return {"ok": False, "error": f"type must be one of: {list(valid.keys())}"}

    state = request.app.state.station_state
    if state.session_stopped:
        return {
            "ok": False,
            "error": "The station is paused. Press Start, then tap the Air Next control again.",
        }
    # Air-next builds and front-inserts one operator trigger at a time. Reject a
    # second tap while one is still pending — with a way out (leadership #5),
    # never a silent overwrite of the first pick.
    if state.operator_force_pending is not None:
        return {
            "ok": False,
            "error": "Give the tape decks a few seconds to cue your last pick, then tap again.",
        }
    _reserve_continuity_runway(
        request.app.state,
        state,
        request.app.state.config,
        discard_reason=GenerationWasteReason.OPERATOR_PURGE,
    )
    state.force_next = valid[seg_type]
    # Attribute this force to the operator so the admin panel can surface it as a
    # deliberate trigger (internal forces never set this — see StationState).
    state.operator_force_pending = valid[seg_type]
    return {"ok": True, "triggered": seg_type}


@router.post("/api/interrupt")
async def api_interrupt(request: Request, _: None = Depends(require_admin_access)):
    """Immediately interrupt the stream and have the hosts deliver a pissed/urgent message.

    Body: {"directive": str, "urgency": str}
    - directive: what the hosts should say (required)
    - urgency: "pissed" | "urgent" | "gentle" (default: "pissed")

    Returns 429 if called within the cooldown window (default 60s).
    """
    body, error = await read_json_object(request)
    if error is not None:
        return error

    directive = (body.get("directive") or "").strip()
    if not directive:
        return JSONResponse(status_code=422, content={"ok": False, "error": "directive is required"})

    urgency = (body.get("urgency") or "pissed").strip().lower()
    if urgency not in ("pissed", "urgent", "gentle"):
        urgency = "pissed"

    state: StationState = request.app.state.station_state
    now = time.time()
    cooldown = 60
    remaining = cooldown - (now - state.last_interrupt_ts)
    if remaining > 0:
        return JSONResponse(
            status_code=429,
            content={
                "ok": False,
                "error": "interrupt cooldown active",
                "retry_after": max(1, math.ceil(remaining)),
            },
        )

    from mammamiradio.core.models import InterruptSpec
    from mammamiradio.scheduling.producer import _fire_interrupt

    spec = InterruptSpec(directive=directive, urgency=urgency, cooldown=cooldown)

    queue = request.app.state.queue
    skip_event = request.app.state.skip_event
    fired = await _fire_interrupt(
        state,
        spec,
        queue,
        skip_event,
        enforce_global_cooldown=True,
        bridge_tmp_dir=request.app.state.config.tmp_dir,
        directive_source="operator",
    )
    if not fired:
        # _fire_interrupt's global cooldown gate beat us (concurrent caller).
        remaining_after = cooldown - (time.time() - state.last_interrupt_ts)
        return JSONResponse(
            status_code=429,
            content={
                "ok": False,
                "error": "interrupt cooldown active",
                "retry_after": max(1, math.ceil(remaining_after)),
            },
        )
    return {"ok": True, "directive": directive, "urgency": urgency}


@router.post("/api/hot-reload")
async def hot_reload_modules(request: Request, _: None = Depends(require_admin_access)):
    """Reload scriptwriter and its data submodules in-place. Stream continues uninterrupted.

    Safe to reload: language_policy / prompt_world / transitions / fallbacks (language
    contract, prompt-fiction + stock copy)
    + scriptwriter (stateless functions + lazy-init clients). Data submodules reload FIRST
    (leaves-first) so the scriptwriter facade re-imports fresh values — reloading the facade
    alone would rebind its ``from .prompt_world`` / ``.transitions`` / ``.fallbacks`` import
    names to the stale submodules.
    NOT reloaded: producer, streamer, persona, memory_extractor (hold live
    task/instance state), auth (reloading would fork require_admin_access from
    the identity the router captured at import — auth edits would silently not
    apply).
    Requires --workers 1 (importlib reloads only the worker handling the request).
    """
    import mammamiradio.hosts.fallbacks as _fallbacks_mod
    import mammamiradio.hosts.language_policy as _language_policy_mod
    import mammamiradio.hosts.prompt_world as _prompt_world_mod
    import mammamiradio.hosts.scriptwriter as _scriptwriter_mod
    import mammamiradio.hosts.station_name_guard as _station_name_guard_mod
    import mammamiradio.hosts.transitions as _transitions_mod

    # Debounce: reject if called within 5s of last reload (monotonic to avoid NTP skew)
    last_reload: float = getattr(request.app.state, "_last_hot_reload_ts", 0.0)
    now = time.monotonic()
    if now - last_reload < 5.0:
        return JSONResponse(
            status_code=429,
            content={
                "ok": False,
                "error_code": "debounced",
                "retry_after_s": int(5.0 - (now - last_reload)),
                "stream_status": "unaffected",
                "retryable": True,
            },
        )

    t0 = time.monotonic()
    try:
        # Leaves-first ordering is load-bearing. scriptwriter does
        # `from .prompt_world import ...` / `.transitions` / `.fallbacks`, so reloading
        # scriptwriter re-runs those imports and rebinds the names to whatever the data
        # submodules hold NOW. Reload the data submodules FIRST; reload the facade first
        # and it would rebind to the stale submodules, leaving operator edits invisible.
        # Reloading scriptwriter also re-runs its module body, which resets
        # _cached_system_prompt — so edited prompt data takes effect on the next
        # generation rather than serving a stale cache.
        importlib.reload(_language_policy_mod)
        importlib.reload(_prompt_world_mod)
        importlib.reload(_transitions_mod)
        importlib.reload(_fallbacks_mod)
        importlib.reload(_station_name_guard_mod)
        importlib.reload(_scriptwriter_mod)
        duration_ms = int((time.monotonic() - t0) * 1000)
        request.app.state._last_hot_reload_ts = now
        logger.info(
            "hot-reload: reloaded language_policy + prompt_world + transitions + "
            "fallbacks + station_name_guard + scriptwriter in %dms",
            duration_ms,
        )
        return {
            "ok": True,
            "reloaded_modules": [
                "mammamiradio.hosts.language_policy",
                "mammamiradio.hosts.prompt_world",
                "mammamiradio.hosts.transitions",
                "mammamiradio.hosts.fallbacks",
                "mammamiradio.hosts.station_name_guard",
                "mammamiradio.hosts.scriptwriter",
            ],
            "duration_ms": duration_ms,
            "effective_on": "next_banter_generation",
            "stream_status": "unaffected",
        }
    except Exception as exc:
        logger.error("hot-reload: importlib.reload failed: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error_code": "reload_failed",
                "exception": str(exc),
                "stream_status": "unaffected",
                "retryable": True,
            },
        )


@router.get("/api/pacing")
async def get_pacing(request: Request, _: None = Depends(require_admin_access)):
    """Return current pacing settings."""
    config = request.app.state.config
    return {
        "songs_between_banter": config.pacing.songs_between_banter,
        "songs_between_ads": config.pacing.songs_between_ads,
        "ad_spots_per_break": config.pacing.ad_spots_per_break,
    }


_pacing_lock = asyncio.Lock()

# admin pacing field -> (clamp low, clamp high, env var). Bounds come from
# core/config.py so live admin changes and restart-loaded values cannot drift.
_PACING_FIELDS: tuple[tuple[str, int, int, str], ...] = (
    ("songs_between_banter", *PACING_BOUNDS["songs_between_banter"], "MAMMAMIRADIO_PACING_SONGS_BETWEEN_BANTER"),
    ("songs_between_ads", *PACING_BOUNDS["songs_between_ads"], "MAMMAMIRADIO_PACING_SONGS_BETWEEN_ADS"),
    ("ad_spots_per_break", *PACING_BOUNDS["ad_spots_per_break"], "MAMMAMIRADIO_PACING_AD_SPOTS_PER_BREAK"),
)


@router.patch("/api/pacing")
async def update_pacing(request: Request, _: None = Depends(require_admin_access)):
    """Update pacing settings live and persist them.

    Persist FIRST, mutate SECOND (matches the super-italian / chaos / quality /
    broadcast-chain toggles): every present pacing key is written in ONE atomic
    store — Supervisor options on HA addons, .env on standalone — before any
    live mutation. If the write fails we return 500 and leave both live config
    and durable config untouched, so a failed save can never leave the two
    disagreeing after a restart. The admin UI reverts the slider and shows a
    human "couldn't save" message on the 500.
    """
    config = request.app.state.config
    body, error = await read_json_object(request)
    if error is not None:
        return error

    def _parse_pacing_int(field: str) -> int | None:
        if field not in body:
            return None
        raw = body[field]
        if isinstance(raw, bool) or raw is None:
            raise HTTPException(status_code=400, detail=f"{field} must be an integer")
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            text = raw.strip()
            if _re.fullmatch(r"-?\d{1,9}", text):
                return int(text)
        raise HTTPException(status_code=400, detail=f"{field} must be an integer")

    # Parse + clamp every present field up front, so a malformed value raises a
    # 400 before anything is persisted or mutated (no partial write).
    clamped: dict[str, int] = {}
    for attr, lo, hi, _env_key in _PACING_FIELDS:
        if attr in body:
            clamped[attr] = max(lo, min(hi, _parse_pacing_int(attr) or 0))

    if clamped:
        env_updates = {env_key: str(clamped[attr]) for attr, _lo, _hi, env_key in _PACING_FIELDS if attr in clamped}
        loop = asyncio.get_running_loop()
        async with _pacing_lock:
            old_values = {attr: getattr(config.pacing, attr) for attr in clamped}
            # Persist FIRST as one atomic multi-key write. On failure leave live
            # config AND durable config untouched — no partial drift. `clamped` is
            # already {attr: int}, the exact Supervisor option keys.
            try:
                if config.is_addon:
                    await _run_addon_persistence(_save_addon_option_batch, clamped)
                else:
                    await loop.run_in_executor(None, _save_dotenv, env_updates)
            except Exception:
                logger.error("Failed to persist pacing settings", exc_info=True)
                return JSONResponse(
                    status_code=500,
                    content={"ok": False, "error": "failed to persist pacing settings"},
                )
            for attr, value in clamped.items():
                setattr(config.pacing, attr, value)
            # No os.environ write: config.pacing is the live source of truth and
            # the persisted .env / Supervisor option store is the restart source
            # (dotenv and run.sh repopulate the env at boot). Setting it live would
            # only leak MAMMAMIRADIO_PACING_* into a later in-process config reload.
        for attr, new_value in clamped.items():
            if new_value != old_values[attr]:
                _record_operator_action(request, f"pacing_{attr}", old_values[attr], new_value)

    return {
        "ok": True,
        "songs_between_banter": config.pacing.songs_between_banter,
        "songs_between_ads": config.pacing.songs_between_ads,
        "ad_spots_per_break": config.pacing.ad_spots_per_break,
    }


@router.get("/api/super-italian")
async def get_super_italian(request: Request, _: None = Depends(require_admin_access)):
    """Return the current Super Italian Mode flag."""
    config = request.app.state.config
    return {"super_italian_mode": bool(config.super_italian_mode)}


_super_italian_lock = asyncio.Lock()
_chaos_lock = asyncio.Lock()


def _save_super_italian_addon_options(value: bool) -> None:
    """Persist super_italian_mode through Supervisor for HA addons."""
    _save_addon_option("super_italian_mode", value)


@router.get("/api/chaos")
async def get_chaos(request: Request, _: None = Depends(require_admin_access)):
    """Return the current Chaos Mode flag."""
    state = request.app.state.station_state
    return {"enabled": bool(state.chaos_mode_active)}


@router.post("/api/chaos")
async def set_chaos(request: Request, _: None = Depends(require_admin_access)):
    """Toggle Chaos Mode live and persist it.

    Endpoint flow:
    POST enabled=true -> persist -> set active+pending -> bump epoch -> purge lookahead
    POST enabled=false -> persist -> clear pending -> bump epoch, without queue purge
    """
    body, error = await read_json_object(request)
    if error is not None:
        return error
    if "enabled" not in body:
        return {"ok": False, "error": "expected JSON object with enabled"}
    raw_value = body["enabled"]
    if not isinstance(raw_value, bool):
        return {"ok": False, "error": "enabled must be a JSON boolean (true/false)"}

    state = request.app.state.station_state
    config = request.app.state.config
    value = raw_value
    env_value = "true" if value else "false"
    loop = asyncio.get_running_loop()
    purged = 0

    async with _chaos_lock:
        try:
            if config.is_addon:
                await _run_addon_persistence(_save_addon_option, "chaos_mode_active", value)
            else:
                await loop.run_in_executor(None, _save_dotenv, {"MAMMAMIRADIO_CHAOS_MODE": env_value})
        except Exception:
            logger.error("Failed to persist Chaos Mode toggle", exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": "failed to persist chaos mode"},
            )

        os.environ["MAMMAMIRADIO_CHAOS_MODE"] = env_value
        # Capture old_value INSIDE the lock, immediately before the mutation, so a
        # concurrent chaos toggle can't record a stale before-value (matches the
        # other four toggle endpoints).
        old_value = state.chaos_mode_active
        if value:
            first_strike = _random.choice([ChaosSubtype.FOURTH_WALL, ChaosSubtype.ABANDONED_STORM])
            purged = _reserve_continuity_runway(
                request.app.state,
                state,
                config,
                replace_queue=True,
                discard_reason=GenerationWasteReason.STALE_CHAOS,
            )
            state.chaos_mode_active = True
            state.chaos_pending = first_strike
            state.chaos_cutover_epoch += 1
            state.chaos_audio_failures = 0
            state.chaos_last_degraded_reason = ""
        else:
            state.chaos_mode_active = False
            state.chaos_pending = None
            state.chaos_cutover_epoch += 1

    logger.info(
        "Chaos Mode %s by admin%s",
        "enabled" if value else "disabled",
        f" (purged {purged}, first_strike={state.chaos_pending.value if state.chaos_pending else 'none'})"
        if value
        else "",
    )
    _record_operator_action(request, "chaos_mode", old_value, value)
    return {"ok": True, "enabled": value, "purged": purged}


@router.post("/api/super-italian")
async def set_super_italian(request: Request, _: None = Depends(require_admin_access)):
    """Toggle Super Italian Mode live and persist it.

    Connected listeners pick up the new copy on the next page reload (it's baked
    into listener.html via Jinja, not refetched on /public-status polls). The
    scriptwriter system-prompt cache invalidates on the mode key so the next
    banter generation uses the new directive without a restart.

    Persistence: writes `MAMMAMIRADIO_SUPER_ITALIAN` to `.env` on standalone
    deploys, and `super_italian_mode` to Supervisor's durable option store on
    HA addons, so the value survives container restarts in both modes.
    """
    config = request.app.state.config
    body, error = await read_json_object(request)
    if error is not None:
        return error
    if "super_italian_mode" not in body:
        return {"ok": False, "error": "expected JSON object with super_italian_mode"}
    raw_value = body["super_italian_mode"]
    if not isinstance(raw_value, bool):
        return {"ok": False, "error": "super_italian_mode must be a JSON boolean (true/false)"}
    value = raw_value
    env_value = "true" if value else "false"
    loop = asyncio.get_running_loop()
    async with _super_italian_lock:
        old_value = config.super_italian_mode
        # Persist FIRST: if the write fails, leave runtime/env untouched so the live
        # setting never drifts from what survives a restart (matches chaos/quality/
        # broadcast-chain). The operator_action row then only fires on a real,
        # persisted change.
        try:
            if config.is_addon:
                await _run_addon_persistence(_save_addon_option, "super_italian_mode", value)
            else:
                await loop.run_in_executor(None, _save_dotenv, {"MAMMAMIRADIO_SUPER_ITALIAN": env_value})
        except Exception:
            logger.error("Failed to persist Super Italian toggle", exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": "failed to persist super italian mode"},
            )
        config.super_italian_mode = value
        os.environ["MAMMAMIRADIO_SUPER_ITALIAN"] = env_value
    _record_operator_action(request, "super_italian_mode", old_value, value)
    return {"ok": True, "super_italian_mode": value}


_broadcast_chain_lock = asyncio.Lock()


@router.get("/api/broadcast-chain")
async def get_broadcast_chain(request: Request, _: None = Depends(require_admin_access)):
    """Return the current On-Air Sound (FM broadcast chain) flag."""
    config = request.app.state.config
    return {"broadcast_chain": bool(config.audio.broadcast_chain)}


@router.post("/api/broadcast-chain")
async def set_broadcast_chain(request: Request, _: None = Depends(require_admin_access)):
    """Toggle the On-Air Sound (FM broadcast chain) live and persist it.

    Hot-swaps with NO restart and NO queue purge: re-arming the egress chain via
    ``configure_broadcast_chain()`` changes only the segment produced NEXT; the
    current (and already-buffered) segments finish airing as they are — the same
    next-segment semantics as the AI Quality dial, contrast ``/api/playlist/load``
    which purges. This lets an operator A/B the FM colouring against studio-clean on
    the live stream without breaking the current track.

    Persistence: writes ``MAMMAMIRADIO_BROADCAST_CHAIN`` to ``.env`` on standalone
    deploys, and the ``broadcast_chain`` option through Supervisor on HA addons
    (the same key ``run.sh`` reads from the generated startup projection), so the
    choice survives a restart.
    """
    config = request.app.state.config
    body, error = await read_json_object(request)
    if error is not None:
        return error
    if "broadcast_chain" not in body:
        return {"ok": False, "error": "expected JSON object with broadcast_chain"}
    raw_value = body["broadcast_chain"]
    if not isinstance(raw_value, bool):
        return {"ok": False, "error": "broadcast_chain must be a JSON boolean (true/false)"}
    value = raw_value
    env_value = "true" if value else "false"
    loop = asyncio.get_running_loop()
    async with _broadcast_chain_lock:
        # Persist FIRST: if the write fails, leave runtime state untouched so the
        # live setting never drifts from what survives a restart.
        try:
            if config.is_addon:
                await _run_addon_persistence(_save_addon_option, "broadcast_chain", value)
            else:
                await loop.run_in_executor(None, _save_dotenv, {"MAMMAMIRADIO_BROADCAST_CHAIN": env_value})
        except Exception:
            logger.error("Failed to persist On-Air Sound toggle", exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": "failed to persist on-air sound"},
            )
        old_value = config.audio.broadcast_chain
        config.audio.broadcast_chain = value
        os.environ["MAMMAMIRADIO_BROADCAST_CHAIN"] = env_value
        # Hot-apply: (dis)arm the egress chain so the NEXT produced segment reflects
        # the change. No restart, no queue purge.
        configure_broadcast_chain(
            value,
            sample_rate=config.audio.sample_rate,
            channels=config.audio.channels,
            bitrate=config.audio.bitrate,
        )
    logger.info("On-Air Sound (broadcast chain) %s by admin", "enabled" if value else "disabled")
    _record_operator_action(request, "broadcast_chain", old_value, value)
    return {"ok": True, "broadcast_chain": value}


_quality_lock = asyncio.Lock()


@router.get("/api/quality")
async def get_quality(request: Request, _: None = Depends(require_admin_access)):
    """Return the active model quality profile and the available profiles."""
    config = request.app.state.config
    return {
        "active_profile": config.models.active_profile,
        "profiles": sorted(config.models.profiles),
    }


@router.post("/api/quality")
async def set_quality(request: Request, _: None = Depends(require_admin_access)):
    """Switch the active model quality profile (premium|balanced|economy) live.

    Hot-swaps with NO restart and NO queue purge: only the model that voices the
    NEXT generated segment changes; the current segment finishes airing
    untouched (contrast /api/playlist/load, which purges). No prompt-cache reset
    needed — the system prompt is model-independent, so a model-ID change can't
    break the illusion mid-segment.

    Persistence mirrors super_italian: MAMMAMIRADIO_QUALITY to `.env` (standalone)
    or quality_profile through Supervisor's option store (addon).
    """
    config = request.app.state.config
    body, error = await read_json_object(request)
    if error is not None:
        return error
    if "quality_profile" not in body:
        return {"ok": False, "error": "expected JSON object with quality_profile"}
    profile = body["quality_profile"]
    if not isinstance(profile, str) or profile not in config.models.profiles:
        return {"ok": False, "error": f"quality_profile must be one of {sorted(config.models.profiles)}"}
    loop = asyncio.get_running_loop()
    async with _quality_lock:
        try:
            if config.is_addon:
                await _run_addon_persistence(_save_addon_option, "quality_profile", profile)
            else:
                await loop.run_in_executor(None, _save_dotenv, {"MAMMAMIRADIO_QUALITY": profile})
        except Exception as exc:
            logger.warning("Failed to persist quality_profile=%s: %s", profile, exc)
            return JSONResponse({"ok": False, "error": "failed to persist quality_profile"}, status_code=500)
        old_value = config.models.active_profile
        config.models.active_profile = profile
        os.environ["MAMMAMIRADIO_QUALITY"] = profile
    _record_operator_action(request, "quality_profile", old_value, profile)
    return {"ok": True, "active_profile": profile}


# One deliberately-blended TTS rate (~$20 / 1M chars) across Azure / OpenAI /
# ElevenLabs. Cent-accurate TTS cost is impossible — ElevenLabs alone swings 3-5x
# by plan tier — so this is rough on purpose. The honesty lives in the UI label
# ("~$N est"), not in the arithmetic. Only paid cloud chars reach state.tts_characters
# (Edge-tts is free and never counted), so this never bills a silent fallback.
TTS_BLENDED_RATE = 0.00002

COST_BREAKDOWN_CATEGORY_ORDER = (
    "script_banter",
    "script_transition",
    "script_ads",
    "script_home_mood",
    "script_memory",
    "tts",
)
LLM_COST_BREAKDOWN_CATEGORIES = (
    "script_banter",
    "script_transition",
    "script_ads",
    "script_home_mood",
    "script_memory",
)


def _cost_models(models: ModelsSection | None) -> ModelsSection:
    """Use the configured registry even for isolated legacy helper callers."""
    return models or load_model_registry(Path(MODEL_REGISTRY_FILENAME))


def _model_token_cost(model_id: str, toks: dict, models: ModelsSection | None = None) -> tuple[float, bool]:
    input_rate, output_rate, has_unpriced = _cost_models(models).price_for_model(model_id)
    return toks.get("input", 0) * input_rate + toks.get("output", 0) * output_rate, has_unpriced


def _estimate_api_cost(state, models: ModelsSection | None = None) -> tuple[float, bool]:
    """Sum per-model token cost plus a rough TTS estimate. Returns (usd, has_unpriced).

    Prices each model the session actually used (api_tokens_by_model). A model
    without a registry rate uses the registry's conservative fallback and trips
    the UI flag — never a silent $0, never a KeyError.
    Adds a blended TTS character cost on top. getattr keeps a persisted/legacy state
    (no tts_characters attr) safe.
    """
    models = _cost_models(models)
    tts_cost = getattr(state, "tts_characters", 0) * TTS_BLENDED_RATE
    by_model = getattr(state, "api_tokens_by_model", None) or {}
    if not by_model:
        # No per-model breakdown yet (fresh/legacy state). Price on the cheapest
        # configured rate so the counter is never blank yet never inflated, and
        # do NOT trip the unpriced flag — there is no unpriced *model* here, just
        # no per-model data yet. Registry-driven (no hardcoded model id); this
        # reproduces the prior haiku-tier flat estimate.
        in_rate, out_rate = min(
            models.prices.values(),
            key=lambda rate: rate[0] + rate[1],
            default=models.fallback_price,
        )
        llm = state.api_input_tokens * in_rate + state.api_output_tokens * out_rate
        return round(llm + tts_cost, 4), False
    total = 0.0
    has_unpriced = False
    for model_id, toks in by_model.items():
        model_cost, model_unpriced = _model_token_cost(model_id, toks, models)
        total += model_cost
        has_unpriced = has_unpriced or model_unpriced
    return round(total + tts_cost, 4), has_unpriced


def _consumption_cost(state, models: ModelsSection | None = None) -> dict:
    """Cost fields for the /status consumption block (protected UI element)."""
    models = _cost_models(models)
    cost, unpriced = _estimate_api_cost(state, models)
    return {
        "api_cost_estimate_usd": cost,
        "api_cost_unpriced_model": unpriced,
        "cost_breakdown": _cost_breakdown(state, total_usd=cost, unpriced_model=unpriced, models=models),
    }


def _cost_breakdown(state, *, total_usd: float, unpriced_model: bool, models: ModelsSection | None = None) -> dict:
    by_category_model = getattr(state, "api_tokens_by_category_model", None) or {}
    calls_by_category = getattr(state, "api_calls_by_category", None) or {}
    tts_by_category = getattr(state, "tts_characters_by_category", None) or {}

    categories: dict[str, dict] = {}
    raw_total = 0.0
    summed_calls = 0
    summed_input = 0
    summed_output = 0
    summed_tts_chars = 0
    has_unpriced = False

    for category in COST_BREAKDOWN_CATEGORY_ORDER:
        category_raw_cost = 0.0
        category_unpriced = False
        category_calls = 0
        category_input = 0
        category_output = 0
        category_characters = 0
        if category in LLM_COST_BREAKDOWN_CATEGORIES:
            category_calls = int(calls_by_category.get(category, 0) or 0)
            for model_id, toks in (by_category_model.get(category) or {}).items():
                model_cost, model_unpriced = _model_token_cost(model_id, toks, models)
                category_raw_cost += model_cost
                category_unpriced = category_unpriced or model_unpriced
                category_input += int(toks.get("input", 0) or 0)
                category_output += int(toks.get("output", 0) or 0)
        else:
            category_characters = int(tts_by_category.get(category, 0) or 0)
            category_raw_cost = category_characters * TTS_BLENDED_RATE

        raw_total += category_raw_cost
        summed_calls += category_calls
        summed_input += category_input
        summed_output += category_output
        summed_tts_chars += category_characters
        has_unpriced = has_unpriced or category_unpriced
        categories[category] = {
            "cost_usd": round(category_raw_cost, 4),
            "raw_cost_usd": category_raw_cost,
            "unpriced": category_unpriced,
            "calls": category_calls,
            "input_tokens": category_input,
            "output_tokens": category_output,
            "characters": category_characters,
        }

    aggregate_calls = int(getattr(state, "api_calls", 0) or 0)
    aggregate_input = int(getattr(state, "api_input_tokens", 0) or 0)
    aggregate_output = int(getattr(state, "api_output_tokens", 0) or 0)
    aggregate_tts = int(getattr(state, "tts_characters", 0) or 0)
    has_aggregate_usage = any((aggregate_calls, aggregate_input, aggregate_output, aggregate_tts))
    unit_totals_match = (
        summed_calls == aggregate_calls
        and summed_input == aggregate_input
        and summed_output == aggregate_output
        and summed_tts_chars == aggregate_tts
    )
    available = unit_totals_match and (
        not has_aggregate_usage or any(c["raw_cost_usd"] or c["calls"] or c["characters"] for c in categories.values())
    )

    return {
        "available": available,
        "total_usd": total_usd,
        "raw_total_usd": raw_total,
        "unpriced_model": bool(unpriced_model or has_unpriced),
        "categories": categories,
    }


_party_lock = asyncio.Lock()


@router.get("/api/party")
async def get_party(request: Request, _: None = Depends(require_admin_access)):
    """Return the current party mode state."""
    config = request.app.state.config
    return {"active": config.party_mode is not None, "mode": config.party_mode}


def _save_festival_addon_options(enabled: bool) -> None:
    """Persist festival_mode through Supervisor for HA addons."""
    _save_addon_option("festival_mode", enabled)


@router.post("/api/party")
async def set_party(request: Request, _: None = Depends(require_admin_access)):
    """Toggle Festival Mode live and persist it.

    POST {"action": "enable", "mode": "festival"} to start festival mode.
    POST {"action": "disable"} to return to normal.

    Idempotent — double-enable or double-disable returns ok without live
    side-effects. Add-on requests still confirm the selected value with Supervisor.
    """
    config = request.app.state.config
    state = request.app.state.station_state
    body, error = await read_json_object(request)
    if error is not None:
        return error
    action = body.get("action")
    mode = body.get("mode")

    if action not in ("enable", "disable"):
        return JSONResponse({"ok": False, "error": "action must be 'enable' or 'disable'"}, status_code=422)
    if action == "enable" and mode != "festival":
        return JSONResponse({"ok": False, "error": "mode must be 'festival'"}, status_code=422)

    target_mode: PartyMode | None = "festival" if action == "enable" else None
    loop = asyncio.get_running_loop()

    async with _party_lock:
        unchanged = config.party_mode == target_mode
        if unchanged and not config.is_addon:
            return {"ok": True, "active": config.party_mode is not None, "mode": config.party_mode}
        val = "true" if target_mode == "festival" else "false"
        # Persist FIRST. The enable path may replace the live lookahead queue and
        # forces a banter, so a persist failure AFTER that would leave the station
        # re-buffering from empty and risking dead air on a toggle the UI
        # reported as failed. Persisting first means a failed write changes nothing.
        try:
            if config.is_addon:
                await _run_addon_persistence(_save_festival_addon_options, target_mode == "festival")
            else:
                await loop.run_in_executor(None, _save_dotenv, {"MAMMAMIRADIO_FESTIVAL_MODE": val})
        except Exception:
            logger.error("Failed to persist Festival Mode toggle", exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": "failed to persist festival mode"},
            )
        if unchanged:
            return {"ok": True, "active": config.party_mode is not None, "mode": config.party_mode}
        old_on = config.party_mode == "festival"
        config.party_mode = target_mode
        os.environ["MAMMAMIRADIO_FESTIVAL_MODE"] = val
        if action == "enable":
            _reserve_continuity_runway(
                request.app.state,
                state,
                config,
                replace_queue=True,
                discard_reason=GenerationWasteReason.OPERATOR_PURGE,
            )
            state.playlist_revision += 1
            state.force_next = SegmentType.BANTER

    logger.info("Festival Mode %s by admin", "enabled" if target_mode else "disabled")
    _record_operator_action(request, "festival_mode", old_on, target_mode == "festival")
    return {"ok": True, "active": config.party_mode is not None, "mode": config.party_mode}


@router.post("/api/credentials")
async def save_credentials(request: Request, _: None = Depends(require_admin_access)):
    """Write credentials to persistent storage and apply them live without a restart."""
    body, error = await read_json_object(request)
    if error is not None:
        return error
    updates = _credential_updates_from_field_payload(body)

    if not updates:
        return {"ok": False, "error": "No recognised credential fields in request"}

    try:
        await _persist_and_apply_credentials(request, updates, use_addon_options=True)
    except _AddonPersistenceError:
        logger.error("Failed to persist add-on credentials", exc_info=True)
        return JSONResponse(status_code=500, content={"ok": False, "error": "failed to save credentials"})

    target = "add-on secrets.env" if request.app.state.config.is_addon else ".env"
    logger.info("Credentials saved to %s: %s", target, ", ".join(updates.keys()))
    return {"ok": True, "saved": list(updates.keys())}


@router.post("/api/playlist/purge")
async def purge_pool(request: Request, _: None = Depends(require_admin_access)):
    """Empty the rotation pool ("Svuota tutto").

    Clears every track from the pool, the pin, play history and segment counters
    via ``switch_playlist([], None)`` (which also bumps the revision so any
    in-flight producer segment is discarded on commit), then purges the
    pre-produced lookahead queue so the cleared pool takes effect. The current
    segment is left to finish — purging the pool is not a reason to cut the air;
    the next producer pass is forced to banter/continuity until a source is
    re-added (INSTANT AUDIO — never dead air). Sources can be re-added from the
    toolbar.
    """
    state = request.app.state.station_state
    config = request.app.state.config
    source_switch_lock = request.app.state.source_switch_lock
    async with source_switch_lock:
        purged = _reserve_continuity_runway(
            request.app.state,
            state,
            config,
            replace_queue=True,
            discard_reason=GenerationWasteReason.OPERATOR_PURGE,
        )
        state.switch_playlist([], None)
        jamendo_provider = getattr(request.app.state, "jamendo_provider", None)
        invalidate_jamendo = getattr(jamendo_provider, "invalidate_source", None)
        if callable(invalidate_jamendo):
            task = asyncio.create_task(invalidate_jamendo())
            _register_background_task(request.app.state, task)
        state.force_next = SegmentType.BANTER
        persisted = _delete_persisted_source(config.cache_dir)
    logger.info("Rotation pool purged by admin — cleared pool, purged %d queued segments, forced banter", purged)
    return {"ok": True, "purged": purged, "persisted": persisted}


@router.post("/api/playlist/remove")
async def remove_track(request: Request, _: None = Depends(require_admin_access)):
    """Remove a captured track from the rotation pool — a DURABLE ban.

    Removal now persists: the song joins the operator blocklist so it never re-enters
    the pool on restart, source switch, or mid-session chart refresh (the reported
    "deleted songs come back" bug). Also clears the pin and drops any not-yet-started
    queued segment of it. A single removal is never rejected for starvation.
    Body: ``{revision: int, index: int, id: str}``.
    """
    body, error = await read_json_object(request)
    if error is not None:
        return error
    target = _strict_admin_target(body, index_fields=("index",), id_fields=("id",))
    if isinstance(target, JSONResponse):
        return target
    revision, indices, track_ids = target

    source_switch_lock = request.app.state.source_switch_lock
    if not await _acquire_admin_playlist_lock(source_switch_lock):
        return _rotation_updating_error()
    try:
        state = request.app.state.station_state
        idx = indices["index"]
        if (
            revision != state.playlist_revision
            or idx >= len(state.playlist)
            or _admin_track_id(state.playlist[idx]) != track_ids["id"]
        ):
            return _stale_playlist_error()

        config = request.app.state.config
        track = state.playlist[idx]
        result = _apply_ban(state, config, [track], queue=request.app.state.queue)
        _reserve_continuity_runway(
            request.app.state,
            state,
            config,
            discard_reason=GenerationWasteReason.OPERATOR_BAN,
            excluded_track_keys={normalized_track_key(track)},
        )
        display = result["banned"][0] if result.get("banned") else track.display
        return {
            "ok": True,
            "removed": display,
            "banned": True,
            "persisted": result.get("persisted", True),
            "playlist_revision": state.playlist_revision,
        }
    finally:
        source_switch_lock.release()


@router.post("/api/track/ban")
async def ban_tracks(request: Request, _: None = Depends(require_admin_access)):
    """Permanently ban one or more songs (durable across restarts and sources).

    Body: {"indices": [int, ...]} (rotation rows), {"index": int}, or
    {"keys": [[artist, title], ...]}. A bulk ban that would leave fewer than
    MIN_ROTATION_AFTER_BAN songs is refused with a warm, way-out message rather than
    starving the station onto the rescue path.
    """
    from mammamiradio.core.models import Track

    body, error = await read_json_object(request)
    if error is not None:
        return error
    state = request.app.state.station_state
    config = request.app.state.config

    tracks: list = []
    raw_indices = body.get("indices")
    if raw_indices is None and "index" in body:
        raw_indices = [body.get("index")]
    if isinstance(raw_indices, list):
        for raw in raw_indices:
            idx = _as_int_index(raw)
            if 0 <= idx < len(state.playlist):
                tracks.append(state.playlist[idx])
    for raw_key in body.get("keys", []) or []:
        if isinstance(raw_key, list | tuple) and len(raw_key) == 2:
            tracks.append(Track(title=str(raw_key[1]), artist=str(raw_key[0]), duration_ms=0))

    if not tracks:
        return {"ok": False, "error": "Pick at least one song to ban."}

    # D5: a bulk ban must not starve the rotation pool onto the emergency rescue path.
    # Floor is MIN_ROTATION_AFTER_BAN for a healthy pool, but even an already-small
    # pool (< MIN) must keep at least one song — otherwise a single bulk ban could
    # empty the rotation entirely and force permanent rescue playback. (Per-row
    # removal stays exempt: the operator asked for that one song gone.)
    banned_keys = {normalized_track_key(t) for t in tracks}
    in_pool = sum(1 for t in state.playlist if normalized_track_key(t) in banned_keys)
    remaining = len(state.playlist) - in_pool
    floor = MIN_ROTATION_AFTER_BAN if len(state.playlist) >= MIN_ROTATION_AFTER_BAN else 1
    if remaining < floor:
        return {
            "ok": False,
            "error": "That would leave too few songs for the station to keep playing. "
            "Unban a few or add more music first.",
        }

    result = _apply_ban(state, config, tracks, queue=request.app.state.queue)
    _reserve_continuity_runway(
        request.app.state,
        state,
        config,
        discard_reason=GenerationWasteReason.OPERATOR_BAN,
        excluded_track_keys=banned_keys,
    )
    return result


@router.post("/api/track/ban-now-playing")
async def ban_now_playing(request: Request, _: None = Depends(require_admin_access)):
    """Ban the song currently on air and cut to the next segment in one action.

    The on-air console's "Ban" button. Durably blocklists the airing track by its
    ``(artist, title)`` identity, then runs the exact skip path so it leaves the air
    immediately — the ONE ban path that interrupts the current segment (every other
    ban deliberately lets the airing song finish).

    Identity comes from ``now_streaming.metadata`` (the same ``artist`` / ``title_only``
    keys the queue-mutation predicate matches), so this also works for a song that
    is on air from the rescue cache or a one-off download and is not in ``state.playlist``
    at all — a win over the index-based row ban. Starvation-exempt like the per-row ✕ Ban:
    the operator asked for THIS song gone, now. Best-effort persistence is surfaced
    honestly via ``persisted`` (leadership #5).
    """
    state = request.app.state.station_state
    config = request.app.state.config

    if _skip_is_in_flight(state):
        return {
            "ok": False,
            "error": "That skip is already on its way — wait for the next song before banning again.",
        }
    now_seg = state.now_streaming or {}
    if now_seg.get("type") != "music":
        return {"ok": False, "error": "Only a song can be banned — nothing musical is on air right now."}
    if not _now_streaming_is_real_media(state):
        return {
            "ok": False,
            "error": "That song is no longer on air. Wait for the next audible song before banning.",
        }

    # Prefer ``title_only`` so the blocklist key matches both the queue-purge key and
    # the clean ``Track.title`` used at every ingest doorway. Fall back through the
    # shared now-playing identity resolver so Ban and Like/Dislike never persist
    # different keys for the same on-air song.
    track = _now_playing_music_track(now_seg)
    if track is None:
        return {
            "ok": False,
            "error": "I can’t tell which song this is to ban it. Ban it from the rotation list instead.",
        }
    # Ban and purge FIRST, then synchronously reserve against the final queue —
    # the blocked song must never satisfy runway math or re-enter from norm cache.
    result = _apply_ban(state, config, [track], queue=request.app.state.queue)
    _reserve_continuity_runway(
        request.app.state,
        state,
        config,
        discard_reason=GenerationWasteReason.OPERATOR_BAN,
        excluded_track_keys={normalized_track_key(track)},
    )
    bridged = await _request_skip(
        request.app.state,
        state,
        config,
        source="ban_now_playing",
        discard_reason=GenerationWasteReason.OPERATOR_BAN,
    )
    return {
        "ok": True,
        "banned": result.get("banned", []),
        "removed": result.get("removed", 0),
        "purged": result.get("purged", 0),
        "persisted": result.get("persisted", True),
        "skipped": True,
        "bridged": bridged,
        # The server-resolved identity, so the admin's Undo unbans the exact key the
        # server banned — not whatever its last poll happened to show (the airing
        # segment can advance in that window).
        "key": list(normalized_track_key(track)),
    }


@router.post("/api/track/preference")
async def prefer_track(request: Request, _: None = Depends(require_admin_access)):
    """Set or clear an operator-only soft preference for one song.

    Body: ``{"vote": "up"|"down"|"clear", "now_playing": true}``,
    ``{"vote": ..., "index": N}``, or ``{"vote": ..., "key": [artist, title]}``.
    This deliberately does not skip, purge the queue, or touch the blocklist.
    """
    body, error = await read_json_object(request)
    if error is not None:
        return error
    vote = str(body.get("vote") or "").strip().lower()
    if not vote and "score" in body:
        raw_score = body.get("score")
        try:
            score = int(raw_score) if raw_score is not None else 0
        except (TypeError, ValueError):
            score = 0
        vote = "up" if score > 0 else "down" if score < 0 else "clear"
    if vote not in {"up", "down", "clear"}:
        return JSONResponse(
            content={"ok": False, "error": "Preference vote must be up, down, or clear."},
            status_code=422,
        )
    state = request.app.state.station_state
    target = _resolve_preference_target(state, body)
    if isinstance(target, JSONResponse):
        return target
    key, display, target_label = target
    return _apply_preference(state, request.app.state.config, key, display, vote, target_label)


@router.get("/api/track/preferences")
async def track_preferences(request: Request, _: None = Depends(require_admin_access)):
    """List operator song preferences for the admin control room."""
    return {"ok": True, **_serialize_preference_summary(request.app.state.station_state)}


@router.post("/api/track/unban")
async def unban_tracks(request: Request, _: None = Depends(require_admin_access)):
    """Lift a ban so the song can return on the next fetch. Body: {"keys": [[a, t], ...]}."""
    body, error = await read_json_object(request)
    if error is not None:
        return error
    state = request.app.state.station_state
    config = request.app.state.config
    keys: list[tuple[str, str]] = []
    for raw_key in body.get("keys", []) or []:
        if isinstance(raw_key, list | tuple) and len(raw_key) == 2:
            keys.append((str(raw_key[0]).strip().lower(), str(raw_key[1]).strip().lower()))
    if not keys:
        return {"ok": False, "error": "Pick at least one song to unban."}
    return _apply_unban(state, config, keys)


@router.get("/api/track/banlist")
async def banlist(request: Request, _: None = Depends(require_admin_access)):
    """List banned songs for the admin 'banned' view (newest ban first)."""
    state = request.app.state.station_state
    rows = [
        {
            "artist": key[0],
            "title": key[1],
            "display": meta.get("display") or f"{key[0]} - {key[1]}",
            "banned_by": meta.get("banned_by", "operator"),
            "banned_at": meta.get("banned_at", 0.0),
        }
        for key, meta in state.blocklist.items()
    ]
    rows.sort(key=lambda r: r["banned_at"], reverse=True)
    return {"ok": True, "banned": rows, "count": len(rows)}


@router.post("/api/playlist/move")
async def move_track(request: Request, _: None = Depends(require_admin_access)):
    """Move a captured track between two captured playlist rows."""
    body, error = await read_json_object(request)
    if error is not None:
        return error
    target = _strict_admin_target(
        body,
        index_fields=("from", "to"),
        id_fields=("from_id", "to_id"),
    )
    if isinstance(target, JSONResponse):
        return target
    revision, indices, track_ids = target

    source_switch_lock = request.app.state.source_switch_lock
    if not await _acquire_admin_playlist_lock(source_switch_lock):
        return _rotation_updating_error()
    try:
        state = request.app.state.station_state
        pl = state.playlist
        src = indices["from"]
        dst = indices["to"]
        if (
            revision != state.playlist_revision
            or src >= len(pl)
            or dst >= len(pl)
            or _admin_track_id(pl[src]) != track_ids["from_id"]
            or _admin_track_id(pl[dst]) != track_ids["to_id"]
        ):
            return _stale_playlist_error()

        _reserve_continuity_runway(request.app.state, state, request.app.state.config)
        track = pl.pop(src)
        pl.insert(dst, track)
        state.playlist_revision += 1
        return {"ok": True, "moved": track.display, "playlist_revision": state.playlist_revision}
    finally:
        source_switch_lock.release()


@router.get("/api/playlist")
async def playlist_tracks(
    request: Request,
    offset: int = 0,
    limit: int = 80,
    _: None = Depends(require_admin_access),
):
    """Return a bounded playlist page for admin lazy loading."""
    offset, limit = _page_bounds(offset, limit, default_limit=80, max_limit=200)
    state = request.app.state.station_state
    return _paginated_tracks(
        state.playlist,
        offset,
        limit,
        revision=state.playlist_revision,
        preferences=state.song_preferences,
    )


@router.get("/api/search")
async def search_tracks(
    request: Request,
    q: str = "",
    offset: int = 0,
    limit: int = 20,
    external_offset: int = 0,
    external_limit: int = 5,
    include_external: bool = True,
    _: None = Depends(require_admin_access),
):
    """Search the current playlist and yt-dlp for tracks matching the query."""
    from mammamiradio.playlist.downloader import search_ytdlp_metadata

    offset, limit = _page_bounds(offset, limit, default_limit=20, max_limit=50)
    external_offset, external_limit = _page_bounds(external_offset, external_limit, default_limit=5, max_limit=10)
    state = request.app.state.station_state
    playlist_revision = state.playlist_revision
    playlist_snapshot = list(state.playlist)
    if not q.strip():
        return {
            "revision": playlist_revision,
            "results": [],
            "external": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "has_more": False,
            "external_offset": external_offset,
            "external_limit": external_limit,
            "external_has_more": False,
            "external_known_count": 0,
        }
    query = q.strip().lower()

    # Search an immutable row snapshot paired with the captured revision. The
    # external lookup may await for up to 45 seconds, so its completion must not
    # recertify these absolute indices against a newer rotation.
    matches = []
    for i, track in enumerate(playlist_snapshot):
        text = f"{track.title} {track.artist}".lower()
        if query in text:
            matches.append(
                {
                    "index": i,
                    **_serialize_track(track),
                }
            )
    results = matches[offset : offset + limit]

    # External yt-dlp search (blocking, run off the event loop). Bound the total
    # wait so a slow/cold yt-dlp search can't hang past the HA ingress proxy read
    # timeout and surface as a connection error in the admin (same failure class
    # that motivated backgrounding add-external). On timeout we return the
    # in-playlist results with no web hits rather than failing the whole request.
    external_candidates = []
    if include_external and external_media_enabled(request.app.state.config.allow_ytdlp):
        loop = asyncio.get_running_loop()
        # Cap fetch depth to prevent DoS via unbounded external_offset (4-thread pool, 45s timeout).
        fetch_depth = min(external_offset + external_limit + 1, 50)
        try:
            external_candidates = await asyncio.wait_for(
                loop.run_in_executor(
                    _search_executor,
                    search_ytdlp_metadata,
                    q.strip(),
                    fetch_depth,
                ),
                timeout=45,
            )
        except Exception:
            logger.warning("yt-dlp external search failed/timed out for query %r", q, exc_info=True)
            external_candidates = []
    external = external_candidates[external_offset : external_offset + external_limit]
    external_known_count = len(external_candidates) if include_external else external_offset

    return {
        "revision": playlist_revision,
        "results": results,
        "external": external,
        "total": len(matches),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(results) < len(matches),
        "external_offset": external_offset,
        "external_limit": external_limit,
        "external_has_more": external_offset + len(external) < len(external_candidates),
        "external_known_count": external_known_count,
    }


@router.post("/api/playlist/add-external")
async def add_external_track(request: Request, _: None = Depends(require_admin_access)):
    """Queue a yt-dlp search result to play next via a background download.

    The download runs in the background so this request returns immediately. A
    synchronous yt-dlp fetch takes 10-60s, which overruns the HA ingress proxy
    read timeout — the browser fetch then throws and the admin UI shows a false
    "Failed to add to queue" even though the track downloads and queues fine.
    Returning fast keeps the request well under the proxy timeout. Stream-safe:
    the queue is NOT purged here; the pinned track enters play after the current
    lookahead drains, so there is no silence gap (leadership principle #2).
    """
    from mammamiradio.core.models import Track
    from mammamiradio.playlist.downloader import YOUTUBE_VIDEO_ID_RE

    body, error = await read_json_object(request)
    if error is not None:
        return error
    youtube_id = str(body.get("youtube_id") or "").strip()
    title = str(body.get("title") or "").strip()
    artist = str(body.get("artist") or "").strip()
    album_art = _safe_external_album_art(body.get("album_art"))
    try:
        duration_ms = int(body.get("duration_ms") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid duration_ms"}, status_code=400)
    if not youtube_id:
        return JSONResponse({"ok": False, "error": "youtube_id required"}, status_code=400)
    if not YOUTUBE_VIDEO_ID_RE.fullmatch(youtube_id):
        return JSONResponse({"ok": False, "error": "invalid youtube_id format"}, status_code=400)

    state = request.app.state.station_state
    config = request.app.state.config
    if config.is_addon and not external_media_enabled(config.allow_ytdlp):
        denial = _addon_external_media_error(config)
        if denial is not None:
            return denial
    if not external_media_enabled(config.allow_ytdlp):
        return JSONResponse({"ok": False, "error": "external_downloads_disabled"}, status_code=409)

    track = Track(
        title=title,
        artist=artist,
        duration_ms=duration_ms,
        youtube_id=youtube_id,
        album_art=album_art,
    )
    verdict = classify_youtube_candidate(track, state.playlist, config.pacing, metadata=dict(body))
    if not verdict.accepted:
        logger.info(
            "External track held out of rotation before download: %s (yt:%s reason=%s)",
            track.display,
            youtube_id,
            verdict.reason,
        )
        return JSONResponse(
            {
                "ok": False,
                "error": verdict.notice_reason,
                "reason": verdict.reason,
                "message": verdict.message,
            },
            status_code=409,
        )

    # Fire the download in the background and return before the ingress proxy
    # times out the long yt-dlp fetch. The task pins the track to play next once
    # it is ready; see _download_admin_external_track. We capture source_revision
    # (not playlist_revision) so a benign edit during the download — enrich,
    # move-to-next, festival toggle — does not drop the pick.
    dl_task = asyncio.create_task(_download_admin_external_track(track, request.app.state, state.source_revision))
    _register_background_task(request.app.state, dl_task)

    logger.info("Queueing external track (background): %s (yt:%s)", track.display, youtube_id)
    return {"ok": True, "queued": track.display, "status": "downloading"}


def _register_background_task(app_state: Any, task: asyncio.Task) -> None:
    """Track a fire-and-forget task on app.state so it survives GC mid-flight and
    can be cancelled at shutdown (main.shutdown). Shared by the admin
    queue-from-search and listener song-request paths."""
    tasks = getattr(app_state, "background_tasks", None)
    if tasks is None:
        tasks = set()
        app_state.background_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def _commit_external_download(
    track: Any,
    app_state: Any,
    originating_source_revision: int,
    *,
    should_commit: Callable[[], bool],
    should_pin: Callable[[], bool],
) -> str:
    """Download `track` and commit it to the rotation pool unless the playlist
    SOURCE switched while downloading. Only switch_playlist bumps source_revision,
    so benign edits (enrich / move-to-next / festival toggle) do NOT drop the
    pick. Pins the track to play next when `should_pin()` is true. Returns one of:
    "pinned" (committed and claimed the play-next slot), "queued" (committed to
    the rotation pool but the play-next slot was occupied), "banned" (the song is on
    the operator blocklist and was refused), "held" (long-form/non-rotation audio
    refused after download), or "dropped" (source switched / consumed).
    Raises on download failure / cancellation for the caller to surface. Shared by the
    admin and listener download paths."""
    from mammamiradio.playlist.cover_art import maybe_resolve, needs_resolve
    from mammamiradio.playlist.downloader import (
        accept_recovered_download,
        download_external_track,
        reject_cached_download,
    )

    state = app_state.station_state
    config = app_state.config
    captured_continuity_epoch = state.continuity_epoch
    # Upgrade a YouTube video thumbnail to a real album cover (off the event loop —
    # urlopen is blocking) before the slow download. Search-sourced tracks always
    # carry a thumbnail; only resolve when there's one to upgrade, so a track with
    # no art at all doesn't trigger a lookup. Best-effort: falls back to the
    # thumbnail on a miss, never raises.
    current_art = getattr(track, "album_art", "") or ""
    if current_art and needs_resolve(current_art):
        track.album_art = await asyncio.to_thread(
            maybe_resolve, current_art, track.artist, track.title, cache_dir=config.cache_dir
        )
    downloaded_path = await download_external_track(track, config.cache_dir, music_dir=config.music_dir)
    actual_duration_sec: float | None = None
    try:
        downloaded_path = Path(downloaded_path)
        if downloaded_path.exists():
            actual_duration_sec = await asyncio.to_thread(probe_duration_sec, downloaded_path)
    except (OSError, TypeError, ValueError):
        logger.debug("Skipping external duration probe for %s", track.display, exc_info=True)
    # Serialize the commit decision with source switches. /api/playlist/load holds
    # source_switch_lock across the slow load and only bumps source_revision at the
    # very end (switch_playlist). Without this lock a download finishing mid-load
    # would see the not-yet-bumped revision, commit to the about-to-be-replaced
    # playlist, and then get silently wiped by switch_playlist with no notice.
    # Acquiring the lock makes us wait out any in-flight switch, then re-check the
    # (now bumped) revision. The block below is synchronous — it never awaits while
    # holding the lock, so it can't deadlock the switch routes.
    rejected_download_reason = ""
    async with app_state.source_switch_lock:
        if state.source_revision != originating_source_revision or not should_commit():
            return "dropped"
        metadata_only = state.session_stopped or captured_continuity_epoch != state.continuity_epoch
        # Doorway: an admin queue-from-search OR a listener song request must not
        # resurrect a banned song. A distinct "banned" status (not "dropped") lets
        # each caller surface an honest, specific message — the admin sees "it's
        # banned", the listener stops spinning on "searching…" with a real answer.
        if is_youtube_music_candidate(track):
            verdict = classify_youtube_candidate(
                track,
                state.playlist,
                config.pacing,
                actual_duration_sec=actual_duration_sec if actual_duration_sec and actual_duration_sec > 0 else None,
            )
            if not verdict.accepted:
                rejected_download_reason = verdict.reason
        if not rejected_download_reason:
            if normalized_track_key(track) in state.blocklist:
                return "banned"
            # A previously failed source can be retried explicitly by an admin
            # or listener request. Once this download is admitted, it is real
            # playable media again rather than a session-denied cache key.
            accept_recovered_download(config.cache_dir, track.cache_key)
            if not metadata_only:
                _reserve_continuity_runway(app_state, state, config)
            state.playlist.append(track)
            state.source_readiness.observe_tracks([track])
            state.playlist_revision += 1
            # Metadata-only means no audio admission, not loss of an accepted
            # admin play-next claim. Preserve ownership for the resumed producer.
            pin_claimed = should_pin()
            if pin_claimed:
                state.pinned_track = track
                if state.force_next is None:
                    state.force_next = SegmentType.MUSIC
            if metadata_only:
                logger.info(
                    "External download committed as metadata only track=%s captured_epoch=%d "
                    "current_epoch=%d session_stopped=%s",
                    track.display,
                    captured_continuity_epoch,
                    state.continuity_epoch,
                    state.session_stopped,
                    extra={
                        "event": "external_download_metadata_only",
                        "track": track.display,
                        "captured_continuity_epoch": captured_continuity_epoch,
                        "continuity_epoch": state.continuity_epoch,
                        "session_stopped": state.session_stopped,
                    },
                )
                return "pinned" if pin_claimed else "queued"
            # Don't clobber a pin that's still pending — claim the play-next slot only
            # when the caller's guard says it's free. Otherwise the track is in
            # rotation and the caller surfaces that it's queued-behind, not next.
            if not pin_claimed:
                return "queued"
            return "pinned"

    await asyncio.to_thread(reject_cached_download, config.cache_dir, track.cache_key, rejected_download_reason)
    logger.info(
        "External track held out of rotation after download: %s (yt:%s reason=%s)",
        track.display,
        getattr(track, "youtube_id", ""),
        rejected_download_reason,
    )
    return "held"


async def _download_admin_external_track(track: Any, app_state: Any, originating_source_revision: int) -> None:
    """Background download for an admin queue-from-search request.

    Stream-safe: does NOT purge the queue. On success the track joins the rotation
    pool and claims the play-next pin when free. A real source switch during the
    download drops the pick; a failed download or a dropped pick records a notice
    so the admin UI can surface it (the request already returned 200 before the
    download finished)."""
    state = app_state.station_state

    def _notice(ok: bool, reason: str) -> None:
        state.external_add_notices.append({"display": track.display, "ok": ok, "reason": reason, "ts": time.time()})

    try:
        status = await _commit_external_download(
            track,
            app_state,
            originating_source_revision,
            should_commit=lambda: True,
            should_pin=lambda: state.pinned_track is None,
        )
    except asyncio.CancelledError:
        logger.info("Admin external download cancelled: %s (yt:%s)", track.display, track.youtube_id)
        raise
    except Exception:
        logger.warning("External track download failed for %s (yt:%s)", track.display, track.youtube_id, exc_info=True)
        _notice(False, "download_failed")
        return

    if status == "banned":
        logger.info("Admin external track refused — song is on the operator blocklist: %s", track.display)
        _notice(False, "banned")
        return

    if status == "held":
        logger.info("Admin external track held out of rotation: %s", track.display)
        _notice(False, _LONGFORM_NOTICE_REASON)
        return

    if status == "dropped":
        logger.info("Admin external track dropped — playlist source changed: %s", track.display)
        _notice(False, "source_changed")
        return

    if status == "queued":
        # Committed to the rotation pool, but the play-next slot was already taken
        # (a prior add or a listener request) and playlist order is NOT next-play
        # order — so we can't promise it plays right after the current pick. Tell
        # the operator it's in rotation rather than imminent.
        logger.info("Added external track to rotation: %s (yt:%s)", track.display, track.youtube_id)
        _notice(True, "added_to_rotation")
        return

    logger.info("Queued external track: %s (yt:%s)", track.display, track.youtube_id)


async def _resolve_direction_tracks_for_route(
    targets: list[DirectionTarget],
    state: StationState | None = None,
    config: Any | None = None,
) -> list[Track]:
    """Resolve direction targets to YouTube-backed tracks without downloading audio."""
    from mammamiradio.playlist.downloader import search_ytdlp_metadata

    loop = asyncio.get_running_loop()

    async def _search_one(target: DirectionTarget) -> Track | None:
        try:
            results = await loop.run_in_executor(
                _direction_search_executor,
                search_ytdlp_metadata,
                target.query,
                YOUTUBE_ADMISSION_SEARCH_DEPTH,
            )
        except Exception:
            logger.debug("Direction metadata search failed for %s", target.query, exc_info=True)
            return None
        playlist = state.playlist if state is not None and config is not None else None
        pacing = config.pacing if state is not None and config is not None else None
        resolution = resolve_direction_search_results(target, results, playlist=playlist, pacing=pacing)
        if resolution.track is not None:
            return resolution.track
        if state is not None and resolution.rejected_track is not None:
            _record_direction_notice(
                state,
                resolution.rejected_track,
                ok=False,
                reason=resolution.rejected_reason or _LONGFORM_NOTICE_REASON,
            )
        return None

    try:
        resolved = await asyncio.wait_for(
            asyncio.gather(*[_search_one(target) for target in targets], return_exceptions=True),
            timeout=45,
        )
    except Exception:
        logger.warning("Direction metadata search failed/timed out", exc_info=True)
        return []

    tracks: list[Track] = []
    seen: set[tuple[str, str]] = set()
    for item in resolved:
        if not isinstance(item, Track):
            continue
        key = normalized_track_key(item)
        if key in seen:
            continue
        seen.add(key)
        tracks.append(item)
    return tracks


def _record_direction_notice(state: StationState, track: Any, *, ok: bool, reason: str) -> None:
    """Surface a direction-download outcome to the admin UI, mirroring the admin
    queue-from-search notice path (`_download_admin_external_track`). Best-effort —
    a notice failure never affects whether the track actually aired."""
    try:
        state.external_add_notices.append(
            {"display": getattr(track, "display", ""), "ok": ok, "reason": reason, "ts": time.time()}
        )
    except Exception:
        logger.debug("Failed to record direction notice", exc_info=True)


async def _download_direction_track(
    track: Track,
    app_state: Any,
    originating_source_revision: int,
    heading_id: str,
) -> str:
    """Download a direction target and add it to rotation if the heading is still active."""
    state = app_state.station_state
    try:
        status = await _commit_external_download(
            track,
            app_state,
            originating_source_revision,
            should_commit=lambda: state.heading is not None and state.heading.id == heading_id,
            should_pin=lambda: False,
        )
    except asyncio.CancelledError:
        logger.info("Direction download cancelled: %s (yt:%s)", track.display, track.youtube_id)
        raise
    except Exception as exc:
        logger.warning(
            "Direction download failed for %s (yt:%s): %s: %s",
            track.display,
            track.youtube_id,
            type(exc).__name__,
            exc,
        )
        _record_direction_notice(state, track, ok=False, reason="download_failed")
        return "failed"

    if status == "queued":
        logger.info("Added direction track to rotation: %s (yt:%s)", track.display, track.youtube_id)
        # Grow the course's selection budget to cover the newly landed track.
        # Serialize the read-modify-write AND the persist under source_switch_lock
        # with a fresh identity re-check so a "Back to auto" (which deletes
        # heading.json under the same lock) racing this write can't be undone by a
        # stale write that resurrects the just-cleared course on the next restart.
        async with app_state.source_switch_lock:
            heading = state.heading
            if heading is not None and heading.id == heading_id:
                updated_budget = _heading_selection_budget(_heading_playlist_track_count(state, heading_id))
                dirty = False
                if updated_budget > heading.selection_budget:
                    heading.selection_budget = updated_budget
                    dirty = True
                if heading.phase == "hunting":
                    heading.phase = "steering"
                    if heading.first_found_at <= 0:
                        heading.first_found_at = time.time()
                    dirty = True
                if dirty:
                    try:
                        await asyncio.to_thread(write_persisted_heading, app_state.config.cache_dir, heading)
                    except Exception:
                        logger.warning("Failed to persist direction heading after download landed", exc_info=True)
    elif status == "banned":
        logger.info("Direction track refused because it is blocklisted: %s", track.display)
        _record_direction_notice(state, track, ok=False, reason="banned")
    elif status == "held":
        logger.info("Direction track held out of rotation: %s", track.display)
        _record_direction_notice(state, track, ok=False, reason=_LONGFORM_NOTICE_REASON)
    else:
        logger.info("Direction track skipped after %s: %s", status, track.display)
        _record_direction_notice(state, track, ok=False, reason="source_changed")
    return status


async def _await_first_direction_commit(download_tasks: list[asyncio.Task[str]]) -> tuple[int, list[asyncio.Task[str]]]:
    """Wait until one direction download commits, or all attempted downloads fail."""
    pending = set(download_tasks)
    committed = 0
    while pending and committed == 0:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try:
                status = task.result()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Direction download task failed unexpectedly", exc_info=True)
                status = "failed"
            if status == "queued":
                committed += 1
    return committed, list(pending)


async def _clear_empty_heading_after_direction_downloads(
    download_tasks: list[asyncio.Task[str]],
    app_state: Any,
    originating_source_revision: int,
    heading_id: str,
) -> None:
    """Clear an all-new direction if its background batch finishes with no playable tracks."""
    results = await asyncio.gather(*download_tasks, return_exceptions=True)
    if any(result == "queued" for result in results):
        return

    state = app_state.station_state
    async with app_state.source_switch_lock:
        if (
            state.heading is None
            or state.heading.id != heading_id
            or state.source_revision != originating_source_revision
            or _heading_playlist_track_count(state, heading_id) > 0
        ):
            return
        logger.warning("Direction downloads finished with no playable tracks; returning to auto")
        _clear_active_heading(state)
        _delete_persisted_heading(app_state.config.cache_dir)


# Listener-request endpoints + _download_listener_song background task moved to
# mammamiradio/web/listener_requests.py (Track B v2.11.0 extraction). The new
# router is mounted in main.py alongside this one.


@router.post("/api/playlist/add")
async def add_track(request: Request, _: None = Depends(require_admin_access)):
    """Add a track to the playlist."""
    from mammamiradio.core.models import Track

    body, error = await read_json_object(request)
    if error is not None:
        return error
    # Preserve album_art when the caller supplies one (e.g. re-adding a track that
    # already carries a cover). Live cover resolution happens on the download paths
    # (_commit_external_download), not on this fast synchronous append.
    track = Track(
        title=body.get("title", ""),
        artist=body.get("artist", ""),
        duration_ms=body.get("duration_ms", 0),
        spotify_id=body.get("spotify_id", ""),
        album_art=str(body.get("album_art") or "").strip(),
    )
    if not track.title:
        return {"ok": False, "error": "Missing title"}

    config = request.app.state.config
    if config.is_addon and not external_media_enabled(config.allow_ytdlp):
        denial = _addon_external_media_error(config)
        if denial is not None:
            return denial

    state = request.app.state.station_state
    position = body.get("position", "end")
    _reserve_continuity_runway(request.app.state, state, config)
    if position == "next":
        state.playlist.insert(0, track)
    else:
        state.playlist.append(track)
    state.source_readiness.observe_tracks([track])
    state.playlist_revision += 1
    return {"ok": True, "added": track.display, "position": position}


def _heading_selection_budget(track_count: int) -> int:
    return max(0, min(HEADING_SELECTION_BUDGET_LIMIT, int(track_count or 0)))


def _queue_heading_narration(state: StationState, heading: Heading, kind: str) -> None:
    if not heading.id or state.heading_pending_announcement:
        return
    if kind == "hunt_start" and heading.hunt_started_announced:
        return
    if kind == "first_found" and heading.announced:
        return
    state.heading_pending_announcement = heading.label
    state.heading_pending_narration_kind = kind


def _set_active_heading(state: StationState, heading: Heading) -> None:
    state.heading = heading
    state.heading_revision += 1
    state.heading_pending_announcement = ""
    state.heading_pending_narration_kind = ""
    state.heading_announced_id = ""
    _queue_heading_narration(state, heading, "hunt_start" if heading.phase == "hunting" else "first_found")


def _clear_active_heading(state: StationState) -> None:
    state.heading = None
    state.heading_revision += 1
    state.heading_pending_announcement = ""
    state.heading_pending_narration_kind = ""
    state.heading_announced_id = ""


def _stale_heading_response(state: StationState) -> dict:
    return {
        "ok": False,
        "stale": True,
        "message": "The course changed while we were searching - use the current course or try again.",
        "heading": _serialize_heading(state.heading, state),
    }


def _direction_idempotent_response(request: Request, state: StationState) -> dict:
    """Shared 'already steering there' reply for a duplicate direction submit.

    Records the operator-action ledger row (so BOTH the pre-lock and the in-lock
    idempotency checkpoints leave an audit trail — the in-lock one previously did
    not) and returns the identical response shape from one place so they can't
    drift."""
    _record_heading_ledger(
        request,
        requested_seed=state.heading.seed if state.heading is not None else "",
        added_count=0,
        zero_result=False,
        persisted=True,
        source="direction_set",
    )
    return {
        "ok": True,
        "added": 0,
        "skipped_existing": 0,
        "queued_downloads": 0,
        "committed_downloads": 0,
        "idempotent": True,
        "message": "Already steering there.",
        "heading": _serialize_heading(state.heading, state),
    }


async def _set_direction_text(request: Request, text: str):
    """Apply a free-text operator direction as a heading-backed music block."""
    safe_text = normalize_direction_text(text)
    if not safe_text:
        return JSONResponse(
            {"ok": False, "error": "Give the station a direction and try again."},
            status_code=422,
        )

    config = request.app.state.config
    state = request.app.state.station_state
    seed = f"direction://{safe_text.casefold()}"
    requested_heading_revision = state.heading_revision
    # Idempotent on course identity (seed), NOT on how many tracks have landed:
    # a duplicate submit while the first request's downloads are still in flight
    # must be a no-op, never a second competing course that drops the first's
    # downloads. A genuinely failed direction clears the heading to None, so a
    # retry after failure still falls through here and re-runs.
    if state.heading is not None and state.heading.seed == seed:
        return _direction_idempotent_response(request, state)

    expansion = await expand_direction(safe_text, config, state)
    if not expansion.targets:
        _record_heading_ledger(
            request,
            requested_seed=seed,
            added_count=0,
            zero_result=True,
            persisted=False,
            source="direction_set",
        )
        return {
            "ok": False,
            "added": 0,
            "message": "Couldn't shape that set right now - try a simpler direction.",
        }

    resolved_tracks: list[Track] = []
    if external_media_enabled(config.allow_ytdlp):
        resolved_tracks = await _resolve_direction_tracks_for_route(expansion.targets, state, config)
        resolved_tracks = filter_blocklisted(resolved_tracks, state.blocklist)

    source_switch_lock = request.app.state.source_switch_lock
    download_tracks: list[Track] = []
    retagged_existing = 0
    persisted = True
    async with source_switch_lock:
        # Expansion and search happen before this serialized commit boundary.
        # An unrelated control that completed while they were in flight must
        # not make a currently running station look metadata-only.
        captured_continuity_epoch = state.continuity_epoch
        if state.heading is not None and state.heading.seed == seed:
            return _direction_idempotent_response(request, state)
        if state.heading_revision != requested_heading_revision:
            return _stale_heading_response(state)
        metadata_only = state.session_stopped or captured_continuity_epoch != state.continuity_epoch

        existing_tracks = find_existing_direction_tracks(state.playlist, expansion.targets)
        existing_keys = {normalized_track_key(track) for track in state.playlist}
        seen_new: set[tuple[str, str]] = set()
        for track in resolved_tracks:
            key = normalized_track_key(track)
            if key in existing_keys or key in seen_new:
                continue
            seen_new.add(key)
            download_tracks.append(track)

        track_count = len(existing_tracks) + len(download_tracks)
        if track_count == 0:
            _record_heading_ledger(
                request,
                requested_seed=seed,
                added_count=0,
                zero_result=True,
                persisted=False,
                source="direction_set",
            )
            return {
                "ok": False,
                "added": 0,
                "message": "Couldn't pull that vibe right now - give it a moment and try again.",
            }

        heading = Heading(
            id=uuid4().hex,
            seed=seed,
            label=expansion.label,
            set_at=time.time(),
            set_by="operator",
            selection_budget=_heading_selection_budget(len(existing_tracks)),
            targets=expansion.target_dicts,
            phase="steering" if existing_tracks else "hunting",
            first_found_at=time.time() if existing_tracks else 0.0,
        )
        for track in existing_tracks:
            if track.heading_id != heading.id:
                track.heading_id = heading.id
                retagged_existing += 1
        for track in download_tracks:
            track.heading_id = heading.id
        if retagged_existing and not metadata_only:
            _reserve_continuity_runway(request.app.state, state, config)
        if retagged_existing:
            state.playlist_revision += 1
        _set_active_heading(state, heading)
        originating_source_revision = state.source_revision

        try:
            await asyncio.to_thread(write_persisted_heading, config.cache_dir, heading)
        except Exception:
            logger.warning("Failed to persist direction heading; live heading remains active", exc_info=True)
            persisted = False

    # Register every download up-front so a cancelled request (client disconnect)
    # or a shutdown can still find and cancel them — an in-flight download must
    # never outlive teardown unregistered (it would write to state after teardown
    # begins). This holds for BOTH the mixed and all-new branches below.
    dl_tasks = [
        asyncio.create_task(
            _download_direction_track(track, request.app.state, originating_source_revision, heading.id)
        )
        for track in download_tracks
    ]
    for dl_task in dl_tasks:
        _register_background_task(request.app.state, dl_task)

    committed_downloads = 0
    still_downloading = False
    # When the course already has playable existing tracks it is live immediately;
    # downloads fill in behind it and any failures surface via operator notices. An
    # all-new course has nothing to play yet, so wait (bounded) for at least one
    # track to actually land before claiming success — but never block audio: on
    # timeout the downloads keep running in the background and the course stays up.
    if dl_tasks and not existing_tracks:
        try:
            committed_downloads, _still_pending = await asyncio.wait_for(
                _await_first_direction_commit(dl_tasks), timeout=DIRECTION_COMMIT_WAIT_SECONDS
            )
        except TimeoutError:
            still_downloading = True
            cleanup_task = asyncio.create_task(
                _clear_empty_heading_after_direction_downloads(
                    dl_tasks,
                    request.app.state,
                    originating_source_revision,
                    heading.id,
                )
            )
            _register_background_task(request.app.state, cleanup_task)
        else:
            if committed_downloads == 0:
                # Every download failed — roll the course back to auto rather than
                # leave an empty course claiming to steer.
                async with source_switch_lock:
                    if state.heading is not None and state.heading.id == heading.id:
                        _clear_active_heading(state)
                        persisted = _delete_persisted_heading(config.cache_dir)
                _record_heading_ledger(
                    request,
                    requested_seed=seed,
                    added_count=0,
                    zero_result=True,
                    persisted=persisted,
                    source="direction_set",
                )
                return {
                    "ok": False,
                    "added": 0,
                    "queued_downloads": 0,
                    "message": "Couldn't pull that vibe right now - give it a moment and try again.",
                }

    # Confirmed = tracks already in rotation (retagged existing) + downloads that
    # actually landed. Anything still downloading is reported separately so the UI
    # never counts an unconfirmed download as an aired song.
    confirmed = retagged_existing + committed_downloads
    pending_downloads = len(download_tracks) - committed_downloads
    _record_heading_ledger(
        request,
        requested_seed=seed,
        added_count=track_count,
        zero_result=False,
        persisted=persisted,
        source="direction_set",
    )
    return {
        "ok": True,
        "added": confirmed,
        "retagged_existing": retagged_existing,
        "committed_downloads": committed_downloads,
        "queued_downloads": len(download_tracks),
        "pending_downloads": pending_downloads,
        "still_downloading": still_downloading,
        "expansion_source": expansion.source,
        "persisted": persisted,
        "heading": _serialize_heading(heading, state),
        "targets": expansion.target_dicts,
        "tracks": [_serialize_track(track) for track in (existing_tracks + download_tracks)[:20]],
        "metadata_only": metadata_only,
        "resume_required": bool(metadata_only and state.session_stopped),
    }


@router.post("/api/direction")
async def set_direction(request: Request, _: None = Depends(require_admin_access)):
    """Steer the station from free text: expansion -> target search -> heading bias."""
    body, error = await read_json_object(request, error_message="Give the station a direction and try again.")
    if error is not None:
        return error
    return await _set_direction_text(request, str(body.get("text") or ""))


@router.post("/api/heading")
async def set_heading(request: Request, _: None = Depends(require_admin_access)):
    """Blend an operator-selected era heading into the live rotation."""
    body, error = await read_json_object(request, error_message="Choose an era or type a direction and try again.")
    if error is not None:
        return error
    seed = str(body.get("seed", "")).strip()
    if not seed and "text" in body:
        return await _set_direction_text(request, str(body.get("text") or ""))
    label = HEADING_SEEDS.get(seed)
    if label is None:
        return JSONResponse(
            {"ok": False, "error": "Choose an era or type a direction and try again."},
            status_code=422,
        )

    config = request.app.state.config
    state = request.app.state.station_state
    source_switch_lock = request.app.state.source_switch_lock
    async with source_switch_lock:
        if state.heading is not None and state.heading.seed == seed:
            _record_heading_ledger(
                request,
                requested_seed=seed,
                added_count=0,
                zero_result=False,
                persisted=True,
                source="heading_set",
            )
            return {
                "ok": True,
                "added": 0,
                "skipped_existing": 0,
                "idempotent": True,
                "message": "Already steering there.",
                "heading": _serialize_heading(state.heading, state),
            }

        captured_continuity_epoch = state.continuity_epoch
        try:
            tracks, resolved_source = await asyncio.to_thread(
                load_explicit_source,
                config,
                PlaylistSource(kind="url", url=seed),
            )
        except ExternalMediaUnavailableError:
            denial = _addon_external_media_error(config)
            if denial is not None:
                return denial
            _record_heading_ledger(
                request,
                requested_seed=seed,
                added_count=0,
                zero_result=True,
                persisted=False,
                source="heading_set",
            )
            return {
                "ok": False,
                "message": "Couldn't pull that vibe right now - give it a moment and try again.",
            }
        except ExplicitSourceError:
            _record_heading_ledger(
                request,
                requested_seed=seed,
                added_count=0,
                zero_result=True,
                persisted=False,
                source="heading_set",
            )
            return {
                "ok": False,
                "message": "Couldn't pull that vibe right now - give it a moment and try again.",
            }
        except Exception:
            logger.warning("Heading load failed for %s", seed, exc_info=True)
            _record_heading_ledger(
                request,
                requested_seed=seed,
                added_count=0,
                zero_result=True,
                persisted=False,
                source="heading_set",
            )
            return {
                "ok": False,
                "message": "Couldn't pull that vibe right now - give it a moment and try again.",
            }

        tracks = filter_blocklisted(tracks, state.blocklist)
        metadata_only = state.session_stopped or captured_continuity_epoch != state.continuity_epoch
        if not tracks:
            _record_heading_ledger(
                request,
                requested_seed=seed,
                added_count=0,
                zero_result=True,
                persisted=False,
                source="heading_set",
            )
            return {
                "ok": False,
                "added": 0,
                "message": "Couldn't pull that vibe right now - give it a moment and try again.",
            }

        existing_by_key = {normalized_track_key(track): track for track in state.playlist}
        seen = set(existing_by_key)
        new_tracks: list[Track] = []
        for track in tracks:
            key = normalized_track_key(track)
            if key in seen:
                continue
            seen.add(key)
            new_tracks.append(track)

        if not new_tracks:
            heading = Heading(
                id=uuid4().hex,
                seed=seed,
                label=label,
                set_at=time.time(),
                set_by="operator",
                selection_budget=_heading_selection_budget(len(tracks)),
                phase="steering",
                first_found_at=time.time(),
            )
            retagged = 0
            retagged_keys: set[tuple[str, str]] = set()
            for track in tracks:
                key = normalized_track_key(track)
                if key in retagged_keys:
                    continue
                existing_track = existing_by_key.get(key)
                if existing_track is None:
                    continue
                existing_track.heading_id = heading.id
                retagged += 1
                retagged_keys.add(key)

            if not retagged:
                _record_heading_ledger(
                    request,
                    requested_seed=seed,
                    added_count=0,
                    zero_result=True,
                    persisted=False,
                    source="heading_set",
                )
                return {
                    "ok": False,
                    "added": 0,
                    "message": "Couldn't pull that vibe right now - give it a moment and try again.",
                }

            if not metadata_only:
                _reserve_continuity_runway(request.app.state, state, config)
            state.playlist_revision += 1
            _set_active_heading(state, heading)

            persisted = True
            try:
                await asyncio.to_thread(write_persisted_heading, config.cache_dir, heading)
            except Exception:
                logger.warning("Failed to persist heading; live heading remains active", exc_info=True)
                persisted = False

            _record_heading_ledger(
                request,
                requested_seed=seed,
                added_count=0,
                zero_result=False,
                persisted=persisted,
                source="heading_set",
            )
            logger.info(
                "Heading set from %s: retagged %d existing track(s)",
                resolved_source.label or resolved_source.kind,
                retagged,
            )
            return {
                "ok": True,
                "added": 0,
                "skipped_existing": len(tracks),
                "retagged_existing": retagged,
                "persisted": persisted,
                "heading": _serialize_heading(heading, state),
                "tracks": [],
                "metadata_only": metadata_only,
                "resume_required": bool(metadata_only and state.session_stopped),
            }

        heading = Heading(
            id=uuid4().hex,
            seed=seed,
            label=label,
            set_at=time.time(),
            set_by="operator",
            selection_budget=_heading_selection_budget(len(new_tracks)),
            phase="steering",
            first_found_at=time.time(),
        )
        for track in new_tracks:
            track.heading_id = heading.id
        if not metadata_only:
            _reserve_continuity_runway(request.app.state, state, config)
        state.playlist.extend(new_tracks)
        state.source_readiness.observe_tracks(new_tracks)
        state.playlist_revision += 1
        _set_active_heading(state, heading)

        persisted = True
        try:
            await asyncio.to_thread(write_persisted_heading, config.cache_dir, heading)
        except Exception:
            logger.warning("Failed to persist heading; live heading remains active", exc_info=True)
            persisted = False

        _record_heading_ledger(
            request,
            requested_seed=seed,
            added_count=len(new_tracks),
            zero_result=False,
            persisted=persisted,
            source="heading_set",
        )
        logger.info(
            "Heading set from %s: added %d, skipped %d existing",
            resolved_source.label or resolved_source.kind,
            len(new_tracks),
            len(tracks) - len(new_tracks),
        )
        return {
            "ok": True,
            "added": len(new_tracks),
            "skipped_existing": len(tracks) - len(new_tracks),
            "persisted": persisted,
            "heading": _serialize_heading(heading, state),
            "tracks": [_serialize_track(track) for track in new_tracks[:20]],
            "metadata_only": metadata_only,
            "resume_required": bool(metadata_only and state.session_stopped),
        }


@router.post("/api/heading/clear")
async def clear_heading(request: Request, _: None = Depends(require_admin_access)):
    """Return to automatic rotation without purging blended tracks."""
    config = request.app.state.config
    state = request.app.state.station_state
    source_switch_lock = request.app.state.source_switch_lock
    async with source_switch_lock:
        previous_seed = state.heading.seed if state.heading is not None else ""
        _clear_active_heading(state)
        persisted = _delete_persisted_heading(config.cache_dir)
    _record_heading_ledger(
        request,
        requested_seed=previous_seed,
        added_count=0,
        zero_result=False,
        persisted=persisted,
        source="back_to_auto",
    )
    return {"ok": True, "heading": _serialize_heading(None), "persisted": persisted, "message": "Back to auto."}


@router.post("/api/playlist/enrich")
async def enrich_playlist(request: Request, _: None = Depends(require_admin_access)):
    """Add tracks from a source without replacing programme or purging playback."""
    body, error = await read_json_object(request)
    if error is not None:
        return error
    url = str(body.get("url", "")).strip()
    position = str(body.get("position", "end")).strip().lower()
    if not url:
        return {"ok": False, "error": "No URL provided"}
    if position not in {"end", "next"}:
        return JSONResponse({"ok": False, "error": "position must be 'end' or 'next'"}, status_code=422)

    config = request.app.state.config
    state = request.app.state.station_state
    source_switch_lock = request.app.state.source_switch_lock
    source = PlaylistSource(kind="url", url=url)
    async with source_switch_lock:
        captured_continuity_epoch = state.continuity_epoch
        try:
            tracks, resolved_source = await asyncio.to_thread(load_explicit_source, config, source)
        except LegacyJamendoSourceRetiredError:
            return _media_error(
                410,
                "legacy_jamendo_source_retired",
                "Saved Jamendo playlists were retired in favor of transient playback.",
                retryable=False,
                next_action="Open Motore -> Setup -> Music sources to configure Jamendo.",
            )
        except ExternalMediaUnavailableError as exc:
            denial = _addon_external_media_error(config)
            if denial is not None:
                return denial
            _msg = exc.args[0] if exc.args else "Playlist source unavailable"
            return {"ok": False, "error": _msg}
        except ExplicitSourceError as exc:
            _msg = exc.args[0] if exc.args else "Playlist source unavailable"
            return {"ok": False, "error": _msg}
        except Exception as exc:
            logger.error("Playlist enrich failed: %s", exc)
            return {"ok": False, "error": "Failed to load playlist source"}

        # Doorway: /enrich is a bulk source import — it must honor the operator's
        # bans (re-importing the same source should not resurrect a banned song).
        # Only an explicit single /api/playlist/add is treated as an intentional
        # override and bypasses the blocklist.
        tracks = filter_blocklisted(tracks, state.blocklist)
        metadata_only = state.session_stopped or captured_continuity_epoch != state.continuity_epoch

        seen = {track.cache_key for track in state.playlist}
        new_tracks: list[Track] = []
        for track in tracks:
            if track.cache_key in seen:
                continue
            seen.add(track.cache_key)
            new_tracks.append(track)
        if new_tracks and not metadata_only:
            _reserve_continuity_runway(request.app.state, state, config)
        if position == "next":
            state.playlist[0:0] = new_tracks
        else:
            state.playlist.extend(new_tracks)
        if new_tracks:
            state.source_readiness.observe_tracks(new_tracks)
            state.playlist_revision += 1
        logger.info(
            "Playlist enriched from %s: added %d, skipped %d existing",
            resolved_source.label or resolved_source.kind,
            len(new_tracks),
            len(tracks) - len(new_tracks),
        )
        return {
            "ok": True,
            "added": len(new_tracks),
            "skipped_existing": len(tracks) - len(new_tracks),
            "position": position,
            "source": _serialize_source(resolved_source),
            "tracks": [_serialize_track(track) for track in new_tracks[:20]],
            "metadata_only": metadata_only,
            "resume_required": bool(metadata_only and state.session_stopped),
        }


@router.post("/api/playlist/load")
async def load_playlist(request: Request, _: None = Depends(require_admin_access)):
    """Load a new playlist from a URL and replace the current one."""
    body, error = await read_json_object(request)
    if error is not None:
        return error
    url = body.get("url", "").strip()
    if not url:
        return {"ok": False, "error": "No URL provided"}
    config = request.app.state.config
    state = request.app.state.station_state
    source_switch_lock = request.app.state.source_switch_lock
    source = PlaylistSource(kind="url", url=url)
    async with source_switch_lock:
        # Capture immediately before slow source I/O. A Stop can still advance
        # the epoch without taking this lock, while a second queued source load
        # does not inherit an epoch from before the first load committed.
        captured_continuity_epoch = state.continuity_epoch
        try:
            tracks, resolved_source = await asyncio.to_thread(load_explicit_source, config, source)
        except LegacyJamendoSourceRetiredError:
            return _media_error(
                410,
                "legacy_jamendo_source_retired",
                "Saved Jamendo playlists were retired in favor of transient playback.",
                retryable=False,
                next_action="Open Motore -> Setup -> Music sources to configure Jamendo.",
            )
        except ExternalMediaUnavailableError as exc:
            denial = _addon_external_media_error(config)
            if denial is not None:
                return denial
            _msg = exc.args[0] if exc.args else "Playlist source unavailable"
            return {"ok": False, "error": _msg}
        except ExplicitSourceError as exc:
            _msg = exc.args[0] if exc.args else "Playlist source unavailable"
            return {"ok": False, "error": _msg}
        except Exception as exc:
            logger.error("Playlist load failed: %s", exc)
            return {"ok": False, "error": "Failed to load playlist"}

        source_result = _apply_loaded_source(
            request,
            tracks,
            resolved_source,
            captured_continuity_epoch=captured_continuity_epoch,
        )
        result: dict[str, object] = {
            "ok": True,
            "tracks": int(source_result["tracks"]),
            "url": url,
            "persisted": True,
            "skipped": bool(source_result.get("skipped")),
            "metadata_only": bool(source_result.get("metadata_only")),
            "resume_required": bool(source_result.get("resume_required")),
        }
        try:
            await asyncio.to_thread(write_persisted_source, config.cache_dir, resolved_source)
        except Exception:
            logger.warning("Failed to persist playlist load, live switch still applied", exc_info=True)
            result["persisted"] = False
        return result


@router.post("/api/playlist/move_to_next")
async def move_to_next(request: Request, _: None = Depends(require_admin_access)):
    """Pin a captured rotation row to play next (position 0 in upcoming)."""
    body, error = await read_json_object(request)
    if error is not None:
        return error
    target = _strict_admin_target(body, index_fields=("index",), id_fields=("id",))
    if isinstance(target, JSONResponse):
        return target
    revision, indices, track_ids = target

    source_switch_lock = request.app.state.source_switch_lock
    if not await _acquire_admin_playlist_lock(source_switch_lock):
        return _rotation_updating_error()
    try:
        state = request.app.state.station_state
        pl = state.playlist
        idx = indices["index"]
        if revision != state.playlist_revision or idx >= len(pl) or _admin_track_id(pl[idx]) != track_ids["id"]:
            return _stale_playlist_error()

        track = pl[idx]
        _reserve_continuity_runway(request.app.state, state, request.app.state.config)
        # Pin the track so select_next_track returns it immediately on the next
        # music pick, regardless of weighted-random ordering.
        state.pinned_track = track
        # Bump revision so the producer picks up the pin on its next cycle.
        # We intentionally do NOT purge pre-produced segments here — draining
        # the lookahead queue felt like the entire playlist was destroyed.
        # The pinned track will play after the buffered segments drain (≤1-2
        # songs), which is correct behaviour for "move to upcoming".
        state.playlist_revision += 1
        state.force_next = SegmentType.MUSIC
        return {
            "ok": True,
            "moved": track.display,
            "to_position": 0,
            "playlist_revision": state.playlist_revision,
        }
    finally:
        source_switch_lock.release()


@router.post("/api/track-rules")
async def add_track_rule(request: Request, _: None = Depends(require_admin_access)):
    """Flag a reaction rule for the currently playing track."""
    from mammamiradio.playlist.track_rules import add_rule

    payload, error = await read_json_object(request)
    if error is not None:
        return error
    youtube_id = payload.get("youtube_id", "")
    rule_text = payload.get("rule", "")
    if not youtube_id or not rule_text:
        return JSONResponse({"ok": False, "error": "youtube_id and rule required"}, status_code=400)
    config = request.app.state.config
    db_path = config.cache_dir / "mammamiradio.db"
    add_rule(db_path, youtube_id, rule_text)
    return {"ok": True}


@router.get("/api/hosts")
async def get_hosts(request: Request, _: None = Depends(require_admin_access)):
    """Return all host configs including current personality slider values."""
    config = request.app.state.config
    return {
        "hosts": [
            {
                "name": h.name,
                "voice": h.voice,
                "style": h.style,
                "personality": h.personality.to_dict(),
            }
            for h in config.hosts
        ]
    }


@router.patch("/api/hosts/{host_name}/personality")
async def update_host_personality(host_name: str, request: Request, _: None = Depends(require_admin_access)):
    """Update one or more personality axes for a host.  Takes effect on the next generated segment."""
    body, error = await read_json_object(request)
    if error is not None:
        return error

    config = request.app.state.config
    host = next((h for h in config.hosts if h.name.lower() == host_name.lower()), None)
    if not host:
        raise HTTPException(status_code=404, detail=f"Host '{host_name}' not found")

    valid_axes = PersonalityAxes.AXIS_NAMES
    updates = {k: v for k, v in body.items() if k in valid_axes and isinstance(v, int | float)}
    if not updates:
        raise HTTPException(status_code=400, detail=f"Provide at least one axis: {valid_axes}")

    for axis, value in updates.items():
        setattr(host.personality, axis, max(0, min(100, int(value))))

    return {"ok": True, "host": host.name, "personality": host.personality.to_dict()}


@router.post("/api/hosts/{host_name}/personality/reset")
async def reset_host_personality(host_name: str, request: Request, _: None = Depends(require_admin_access)):
    """Reset a host's personality sliders to neutral defaults (all 50)."""
    config = request.app.state.config
    host = next((h for h in config.hosts if h.name.lower() == host_name.lower()), None)
    if not host:
        raise HTTPException(status_code=404, detail=f"Host '{host_name}' not found")

    host.personality = PersonalityAxes()
    return {"ok": True, "host": host.name, "personality": host.personality.to_dict()}


def _public_status_payload(request: Request) -> dict:
    """Build the read-only status payload shared by public and admin APIs.

    The listener page polls this endpoint every ~3s. The admin /status route
    extends this payload with operator-only fields (queue depth, segment log,
    api costs, etc). The CROSS-PAGE INVARIANT is that any field present in
    both payloads must hold the same value at the same time — enforced by
    tests/web/test_public_status_contract.py.

    SECOND CONSUMER — Music Assistant: the mammamiradio MA provider
    (music-assistant/server: providers/mammamiradio/) polls /public-status to
    drive its now-playing card, reading ``now_streaming`` (incl.
    ``metadata.title``/``title_only``/``artist``/``album_art``/``host``),
    ``upcoming``, ``ha_moments``, and ``brand``. Renaming or dropping any of
    those silently degrades the merged MA provider — the MA-contract tests in
    tests/web/test_public_status_contract.py are the drift detector.
    """
    _sync_runtime_state(request)
    state = request.app.state.station_state
    config = request.app.state.config
    audio_format = stream_audio_metadata(config)
    runtime_health = _runtime_health_snapshot(request)
    start_time = getattr(request.app.state, "start_time", None) or 0
    uptime_sec = round(time.time() - start_time) if start_time else 0
    now_ts = time.time()
    upcoming = [{**item, "source": "rendered_queue"} for item in state.queued_segments[:8]]
    upcoming_mode = "queued" if upcoming else "building"
    # HA moments for the Casa card (public-safe, no person entity details)
    ha_moments: dict | None = None
    # Moment Receipts strip: aired moments as generic family labels + coarse
    # age only — the same exposure ritual_families already accepts on this
    # unauthenticated endpoint. An "airing" row shows only while it belongs to
    # the segment now_streaming is playing (send-start is provisional).
    recent_moments: list[dict] = []
    ha_capable = bool(config.ha_token and config.homeassistant.enabled and config.homeassistant.context_enabled)
    moment_store = getattr(state, "moment_store", None)
    authorization = state.home_authorization or HomeAuthorization.narrow()
    if ha_capable and moment_store is not None and authorization.allows_household_moments:
        try:
            _ns_meta = (state.now_streaming or {}).get("metadata") or {}
            _active_ids = {str(_ns_meta.get(_key) or "") for _key in ("ritual_moment_id", "gag_moment_id")} - {""}
            recent_moments = moment_store.to_public_rows(now=now_ts, active_ids=_active_ids, limit=3)
        except Exception:  # pragma: no cover - receipts must never break status
            logger.debug("Moment receipt public rows failed", exc_info=True)
            recent_moments = []
    if config.homeassistant.context_enabled and (state.ha_context or state.ha_ritual_public_families or recent_moments):
        ha_moments = {
            "connected": True,
            "mood": state.ha_home_mood or None,
            "weather": state.ha_weather_arc or None,
        }
        # Event fields: only if within retention window (person filter applied in producer)
        _retention = EVENT_RETENTION_SECONDS
        _now = now_ts
        if state.ha_last_event_ts > 0 and (_now - state.ha_last_event_ts) < _retention:
            ha_moments["last_event_label"] = state.ha_last_event_label
            ha_moments["last_event_ago_min"] = max(1, round((_now - state.ha_last_event_ts) / 60))
        if state.ha_ritual_public_families:
            ha_moments["ritual_families"] = list(state.ha_ritual_public_families[:4])
        if recent_moments:
            ha_moments["recent"] = recent_moments
        # Hide card if nothing interesting to show
        if (
            not ha_moments.get("mood")
            and not ha_moments.get("weather")
            and not ha_moments.get("last_event_label")
            and not ha_moments.get("ritual_families")
            and not ha_moments.get("recent")
        ):
            ha_moments = None

    playback = _status_now_playback(state.now_streaming, now_ts)
    return {
        "station": config.display_station_name,
        "identity": _serialize_identity(config),
        "running_jokes": list(state.running_jokes),
        **playback,
        "current_source": _serialize_source(state.playlist_source),
        "starter_catalog": _public_starter_catalog(),
        "rotation_track_count": len(state.playlist),
        "heading": _serialize_heading(state.heading, state),
        "golden_path": _golden_path_status(config, state),
        "runtime_health": runtime_health,
        "session_stopped": state.session_stopped,
        "stream_log": [_serialize_stream_log_entry(e) for e in state.stream_log],
        "upcoming": upcoming,
        "upcoming_mode": upcoming_mode,
        "stream": {
            "frequency": config.brand.frequency,
            "bitrate_kbps": audio_format["bitrate_kbps"],
            "audio_format": audio_format,
        },
        "playback_actions": {
            "skip_ready": _now_streaming_is_real_media(state),
            "skip_would_bridge": bool(
                _now_streaming_is_real_media(state)
                and not _playable_runway_available(request.app.state.queue, state, self_heal=False)
            ),
        },
        "ha_moments": ha_moments,
        # Process-local receipt counts reused by the admin status payload.
        "ad_experiment": _ad_experiment_status(state),
        # Brand-fiction layer (PR-A schema). Listener renders against this.
        "brand": _serialize_brand(config.brand),
        # Capability flags (listener-safe subset). Listener JS reads these every
        # poll and toggles [data-cap=KEY] elements (per design D2: client-side
        # capability-conditional rendering reacts to runtime cap drift).
        "capabilities": {
            "llm": bool(config.anthropic_api_key or config.openai_api_key),
            "anthropic_key": bool(config.anthropic_api_key),
            "openai": bool(config.openai_api_key),
            "ha": ha_capable,
            "anthropic_degraded": _provider_health_snapshot(config, state)["anthropic"]["degraded"],
        },
        # Cross-page invariant facts (must match admin /status exactly).
        "uptime_sec": uptime_sec,
        "tracks_played": len(state.played_tracks),
    }


# ---------------------------------------------------------------------------
# Clip sharing ("Share WTF moment")
# ---------------------------------------------------------------------------


_clip_rate: dict[str, float] = {}  # IP -> last clip timestamp
_clip_rate_lock = asyncio.Lock()


async def _release_clip_stamp(client_ip: str, stamp: float) -> None:
    """Roll back a rate-limit stamp under the lock, only if it's still ours.

    The stamp is written under ``_clip_rate_lock``; the rollback must be too, and
    must pop only the value this request wrote (``stamp``). A concurrent request
    from the same IP can overwrite the entry with its own successful stamp before
    this one's failure path runs — a bare ``pop`` by IP would delete that stamp and
    briefly disable the rate limit (#519).
    """
    async with _clip_rate_lock:
        if _clip_rate.get(client_ip) == stamp:
            _clip_rate.pop(client_ip, None)


def _read_validated_starter_share(snapshot: Mapping[str, Any]) -> bytes | None:
    """Read a starter file only while every snapshot fact still matches the manifest."""
    try:
        from mammamiradio.media.starter import load_starter_catalog, starter_track, verify_starter_entry

        provider_track_id = str(snapshot.get("provider_track_id") or "")
        catalog = load_starter_catalog(require_complete=True, verify_all=False)
        matches = [entry for entry in catalog.entries if entry.isrc == provider_track_id]
        if len(matches) != 1:
            return None
        entry = matches[0]
        expected_track = starter_track(entry)
        if (
            snapshot.get("path") != entry.path
            or snapshot.get("title") != entry.title
            or snapshot.get("artist") != entry.artist
            or snapshot.get("attribution") != safe_media_attribution_dict(expected_track.attribution)
        ):
            return None
        verify_starter_entry(entry)
        return entry.path.read_bytes()
    except (OSError, TypeError, ValueError):
        logger.warning("Starter share read rejected by manifest validation", exc_info=True)
        return None


@router.post("/api/clip")
async def create_clip(request: Request):
    """Share exactly one validated, complete bundled starter track."""
    from mammamiradio.scheduling.clip import CLIP_TTL_SECONDS, cleanup_old_clips, save_clip

    # Rate limit: 1 clip per 10 seconds per IP. Return retry_after (seconds), not
    # tech-lingo prose — the listener UI turns it into warm, actionable copy.
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    async with _clip_rate_lock:
        last = _clip_rate.get(client_ip, 0)
        if now - last < CLIP_RATE_LIMIT_SECONDS:
            retry_after = max(1, math.ceil(CLIP_RATE_LIMIT_SECONDS - (now - last)))
            return JSONResponse({"ok": False, "retry_after": retry_after}, status_code=429)
        _clip_rate[client_ip] = now
        # Prune stale entries to avoid unbounded growth.
        stale_keys = [k for k, v in _clip_rate.items() if now - v >= CLIP_RATE_PRUNE_SECONDS]
        for key in stale_keys:
            _clip_rate.pop(key, None)

    config = request.app.state.config
    station_state = getattr(request.app.state, "station_state", None)
    now_streaming = getattr(station_state, "now_streaming", None) or {}
    if not isinstance(now_streaming, dict):
        now_streaming = {}

    snap = getattr(request.app.state, "last_shareworthy_starter", None)
    valid_window = (
        isinstance(snap, dict)
        and snap.get("type") == "starter"
        and (time.monotonic() - float(snap.get("ended_monotonic", 0))) < CLIP_LOOKBACK_SECONDS
    )
    clip_data = (
        await asyncio.to_thread(_read_validated_starter_share, snap)
        if valid_window and isinstance(snap, dict)
        else None
    )
    if not clip_data or not isinstance(snap, dict):
        await _release_clip_stamp(client_ip, now)
        return JSONResponse(
            {
                "ok": False,
                "error_code": "music_share_unavailable",
                "message": "Only a complete bundled starter track can be shared.",
                "retryable": False,
                "next_action": "Wait for a bundled starter track to finish, then try again.",
                "stream_status": "unaffected",
            },
            status_code=403,
        )

    clip_title_override = str(snap.get("title") or "").strip()
    clip_artist_override = str(snap.get("artist") or "").strip()
    clip_attribution_override = snap.get("attribution")

    clips_dir = config.cache_dir / "clips"

    # Cap total clips on disk to prevent unbounded writes; prune .json sidecars too
    existing = sorted(clips_dir.glob("*.mp3"), key=lambda f: f.stat().st_mtime) if clips_dir.is_dir() else []
    if len(existing) >= CLIP_MAX_SAVED:
        for old in existing[: len(existing) - (CLIP_MAX_SAVED - 1)]:
            old.unlink(missing_ok=True)
            old.with_suffix(".json").unlink(missing_ok=True)

    clip_id = save_clip(clip_data, clips_dir)

    # Capture track context at clip creation time as a JSON sidecar.
    # Best-effort: missing now_streaming or schema drift falls back to station_name.
    import json as _json

    # metadata is producer-managed and normally a dict, but a None or an
    # unexpected scalar would crash the .get() below and turn a successful
    # clip into a 500. Normalize to dict before reading fields.
    raw_meta = now_streaming.get("metadata", {})
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    station_name = config.display_station_name
    track_title = str(meta.get("title_only") or meta.get("title") or "").strip()
    track_artist = str(meta.get("artist") or "").strip()
    # ``now_streaming`` may already describe the next segment. The complete
    # manifested snapshot remains the sole authority for this share.
    track_title = clip_title_override
    track_artist = clip_artist_override
    sidecar = {
        "station_name": station_name,
        "track_title": track_title,
        "track_artist": track_artist,
        "created_at": int(time.time()),
    }
    if clip_attribution_override is not None:
        sidecar["music_attribution"] = clip_attribution_override
    try:
        (clips_dir / f"{clip_id}.json").write_text(_json.dumps(sidecar))
    except OSError as exc:
        logger.warning("clip sidecar write failed for %s: %s", clip_id, exc)

    cleanup_old_clips(clips_dir, max_age_hours=CLIP_TTL_SECONDS // 3600)
    return {
        "ok": True,
        "clip_id": clip_id,
        "url": f"/clips/{clip_id}.mp3",
        "share_url": f"/clips/{clip_id}",
        "track_title": track_title,
        "track_artist": track_artist,
    }


# A segment shorter than this has not aired enough to be worth keeping, and the
# arithmetic that would pad it out reaches into the previous segment.
KEEPSAKE_MIN_SECONDS = 3.0
# Keepsakes are the one thing in the cache directory that nothing reclaims, so
# the ceiling has to be explicit rather than emergent. At roughly 4 MB for a
# long segment this bounds the archive at a few hundred MB, and the refusal is
# honest rather than a silent stop.
KEEPSAKE_MAX_SAVED = 200
# Never let keeping a moment be the write that fills the disk and takes the
# station off air with it (leadership principle #2).
KEEPSAKE_MIN_FREE_MB = 256


async def _snapshot_ring(ring_buffer, duration_seconds: int, bitrate_kbps: int) -> bytes | None:
    """Copy the ring buffer on the event loop, then join the bytes off it.

    Two constraints pull in opposite directions. ``extract_clip`` iterates the
    deque that the playback loop appends to, so iterating it from a worker
    thread is a ``RuntimeError: deque mutated during iteration`` waiting to
    happen. But joining up to ~4 MB on the loop stalls every coroutine including
    the sender, whose delivery cushion is four seconds.

    So: take a shallow list copy here, where nothing else can run, and hand the
    copy to a thread. The copy is references, not payload, so it is cheap.
    """
    from collections import deque

    from mammamiradio.scheduling.clip import extract_clip

    frozen = deque(list(ring_buffer))
    return await asyncio.to_thread(extract_clip, frozen, duration_seconds=duration_seconds, bitrate_kbps=bitrate_kbps)


@router.post("/api/clip/keep")
async def keep_this(request: Request, _: None = Depends(require_admin_access)):
    """Keep the voice segment on air right now, durably and forever.

    The on-air console's "Keep this" button. Clips expire after 24 hours and the
    provenance ledger prunes after two weeks, so a segment worth keeping is
    deleted twice over by default and nothing in the product could say "not this
    one". This is that sentence.

    Voice-only, and that is a hard refusal rather than a preference: a keepsake
    has no expiry and is meant to be shareable, so it may never contain a
    third-party master. ``is_keepsake_eligible`` fails closed on any segment
    type it does not recognise.
    """
    from mammamiradio.scheduling.clip import (
        KEEPSAKES_DIRNAME,
        is_keepsake_eligible,
        save_keepsake,
    )

    config = request.app.state.config
    bitrate = config.audio.bitrate if hasattr(config, "audio") else DEFAULT_CLIP_BITRATE_KBPS
    station_state = getattr(request.app.state, "station_state", None)
    now_streaming = getattr(station_state, "now_streaming", None) or {}
    if not isinstance(now_streaming, dict):
        now_streaming = {}
    ring_buffer = getattr(request.app.state, "clip_ring_buffer", None)
    now = time.time()

    seg_type = now_streaming.get("type")
    clip_data: bytes | None = None
    kept_type: str | None = None
    kept_title = ""
    source = ""

    raw_meta_now = now_streaming.get("metadata", {})
    if is_keepsake_eligible(seg_type, raw_meta_now) and ring_buffer:
        # Only what has actually aired of THIS segment. /api/clip floors the
        # window at CLIP_DURATION_SECONDS because it means "the last 30
        # seconds"; keeping means "this segment", and the same floor would reach
        # back past the segment boundary into whatever preceded it and then
        # label the result with this segment's title. Pressing Keep two seconds
        # into a host break would have saved twenty-eight seconds of the ad
        # before it.
        started = float(now_streaming.get("started") or now)
        duration_sec = float(now_streaming.get("duration_sec") or CLIP_DURATION_SECONDS)
        elapsed = max(0.0, now - started)
        cap = min(float(CLIP_MAX_SEGMENT_SECONDS), duration_sec)
        secs = min(cap, elapsed)
        if secs >= KEEPSAKE_MIN_SECONDS:
            clip_data = await _snapshot_ring(ring_buffer, math.ceil(secs), bitrate)
        if clip_data:
            kept_type = str(seg_type)
            raw_meta = now_streaming.get("metadata", {})
            meta = raw_meta if isinstance(raw_meta, dict) else {}
            kept_title = str(meta.get("title") or "").strip()
            source = "live"

    if clip_data is None:
        # Reaching for the button a second late is the normal case, not the edge
        # case: the payoff lands, the segment ends, music starts. The lookback
        # snapshot is only ever written for AD/BANTER, and its own recorded type
        # is re-checked here rather than trusted.
        snap = getattr(request.app.state, "last_shareworthy_clip", None)
        if (
            isinstance(snap, dict)
            and snap.get("bytes")
            and is_keepsake_eligible(snap.get("type"), snap)
            and (time.monotonic() - float(snap.get("ended_monotonic", 0))) < CLIP_LOOKBACK_SECONDS
        ):
            clip_data = snap["bytes"]
            kept_type = str(snap.get("type"))
            kept_title = str(snap.get("title") or "").strip()
            source = "lookback"

    if not clip_data:
        from fastapi.responses import JSONResponse

        # Name the actual reason. One catch-all message asserted "a song can't be
        # saved" for a stopped station, an empty buffer and a segment that ended
        # unwitnessed, so four times in five the stated cause was false and the
        # offered next step was wrong advice.
        if seg_type in ("stopped", None, "") or not now_streaming:
            reason, message = (
                "not_on_air",
                "The station isn't on air right now. Press Start, and Keep will be here when the hosts are.",
            )
        elif seg_type == "music":
            reason, message = (
                "music",
                "A song can't be kept, because that recording isn't ours to hand out. "
                "Wait for the hosts to come back and tap Keep while they're talking.",
            )
        elif is_keepsake_eligible(seg_type) and not is_keepsake_eligible(seg_type, raw_meta_now):
            reason, message = (
                "music_tail",
                "This break opens over the end of the last song, so it isn't ours to hand out. "
                "The next one will be clean.",
            )
        else:
            reason, message = (
                "too_early",
                "Give it a couple of seconds and tap Keep again — there isn't enough of this one yet.",
            )
        return JSONResponse({"ok": False, "reason": reason, "message": message}, status_code=409)

    keepsakes_dir = config.cache_dir / KEEPSAKES_DIRNAME
    # The one directory nothing reclaims, so the ceiling is checked here rather
    # than left to the disk. Both refusals name the situation and a way out.
    from fastapi.responses import JSONResponse

    existing = sorted(keepsakes_dir.glob("*.mp3")) if keepsakes_dir.is_dir() else []
    if len(existing) >= KEEPSAKE_MAX_SAVED:
        return JSONResponse(
            {
                "ok": False,
                "reason": "archive_full",
                "message": (
                    f"You've kept {len(existing)} moments and that's the shelf full. "
                    "Delete a few from the keepsakes folder to make room."
                ),
            },
            status_code=409,
        )
    try:
        free_mb = shutil.disk_usage(config.cache_dir).free / (1024 * 1024)
    except OSError:
        free_mb = None  # unreadable mount: do not invent a reason to refuse
    if free_mb is not None and free_mb < KEEPSAKE_MIN_FREE_MB:
        return JSONResponse(
            {
                "ok": False,
                "reason": "no_room",
                "message": (
                    "The station is low on disk, so this one wasn't saved. Free some space and tap Keep again."
                ),
            },
            status_code=507,
        )
    # `track_title` / `track_artist` are the keys `clip_landing` reads, so a
    # keepsake renders on the existing share page with no template change.
    # `segment_type` and `source` are extra provenance the clip sidecar lacks.
    sidecar = {
        "station_name": config.display_station_name,
        "track_title": kept_title,
        "track_artist": "",
        "segment_type": kept_type,
        "source": source,
        "created_at": int(now),
    }
    try:
        # Off the loop: this writes up to ~4 MB to eMMC or SD while the playback
        # sender is working against a four-second cushion.
        keepsake_id = await asyncio.to_thread(save_keepsake, clip_data, keepsakes_dir, sidecar=sidecar)
    except OSError as exc:
        logger.warning("keepsake write failed: %s", exc)

        return JSONResponse(
            {
                "ok": False,
                "reason": "write_failed",
                "message": "Couldn't save that one — check the station has room on disk, then tap Keep again.",
            },
            status_code=500,
        )

    _record_operator_action(request, "keep_this", kept_type, keepsake_id)
    return {
        "ok": True,
        "keepsake_id": keepsake_id,
        "segment_type": kept_type,
        "title": kept_title,
        "url": f"/clips/{keepsake_id}.mp3",
        "share_url": f"/clips/{keepsake_id}",
    }


@router.get("/clips/{clip_id}.mp3")
async def serve_clip(clip_id: str, request: Request):
    """Serve a saved clip file — no auth required (clips are for sharing)."""
    from fastapi.responses import FileResponse

    from mammamiradio.scheduling.clip import CLIP_TTL_SECONDS

    # Sanitize clip_id to prevent path traversal
    if "/" in clip_id or "\\" in clip_id or ".." in clip_id:
        return {"ok": False, "error": "Invalid clip ID"}

    config = request.app.state.config
    clip_path = config.cache_dir / "clips" / f"{clip_id}.mp3"
    if not clip_path.exists():
        # Keepsakes share the /clips/ URL space so a link that was shared once
        # keeps working, and they are checked second so an id can never resolve
        # to a keepsake while a live clip of the same id exists.
        from mammamiradio.scheduling.clip import KEEPSAKES_DIRNAME

        keepsake_path = config.cache_dir / KEEPSAKES_DIRNAME / f"{clip_id}.mp3"
        if keepsake_path.exists():
            # No TTL check: never expiring is the entire point of a keepsake.
            return FileResponse(keepsake_path, media_type="audio/mpeg")

        from fastapi.responses import JSONResponse

        return JSONResponse({"ok": False, "error": "Clip not found"}, status_code=404)

    # Enforce TTL — don't serve expired clips
    if time.time() - clip_path.stat().st_mtime > CLIP_TTL_SECONDS:
        clip_path.unlink(missing_ok=True)
        from fastapi.responses import JSONResponse

        return JSONResponse({"ok": False, "error": "Clip expired"}, status_code=404)

    return FileResponse(clip_path, media_type="audio/mpeg")


@router.get("/clips/{clip_id}")
async def clip_landing(clip_id: str, request: Request):
    """HTML landing page for a shared clip — OG meta + audio player.

    Expired clips return HTTP 200 with a graceful "expired" state, not 404.
    OG scrapers (WhatsApp, iMessage) cache 404s permanently; a 200 with
    "Questo momento è passato" preserves the brand and points to the live stream.
    """
    import json as _json

    from mammamiradio.scheduling.clip import CLIP_TTL_SECONDS

    if "/" in clip_id or "\\" in clip_id or ".." in clip_id:
        return JSONResponse({"ok": False, "error": "Invalid clip ID"}, status_code=400)

    from mammamiradio.scheduling.clip import KEEPSAKES_DIRNAME

    config = request.app.state.config
    clips_dir = config.cache_dir / "clips"
    clip_path = clips_dir / f"{clip_id}.mp3"
    sidecar_path = clips_dir / f"{clip_id}.json"

    # Single stat() instead of exists() then stat() — saves a syscall and
    # avoids the TOCTOU window between the two. Missing clip → expired page.
    expired = False
    try:
        if time.time() - clip_path.stat().st_mtime > CLIP_TTL_SECONDS:
            clip_path.unlink(missing_ok=True)
            sidecar_path.unlink(missing_ok=True)
            expired = True
    except FileNotFoundError:
        expired = True

    # A keepsake never expires, so it is consulted only once the clips dir has
    # already come up empty or stale — an id can never resolve to a keepsake
    # while a live clip of the same id is still servable.
    if expired:
        keepsakes_dir = config.cache_dir / KEEPSAKES_DIRNAME
        keepsake_path = keepsakes_dir / f"{clip_id}.mp3"
        if keepsake_path.exists():
            clip_path = keepsake_path
            sidecar_path = keepsakes_dir / f"{clip_id}.json"
            expired = False

    # Sidecar read is best-effort. read_text() already raises FileNotFoundError
    # when missing, so an explicit exists() check would be a redundant syscall.
    # _json.loads can return a list/string/number for valid-but-wrong-shape
    # files; isinstance guard keeps later .get() calls from crashing the route.
    sidecar: dict = {}
    try:
        loaded = _json.loads(sidecar_path.read_text())
        if isinstance(loaded, dict):
            sidecar = loaded
    except (FileNotFoundError, OSError, ValueError):
        sidecar = {}

    ingress_prefix = _sanitize_ingress_prefix(request.headers.get("X-Ingress-Path", ""))
    public_base_url = f"{str(request.base_url).rstrip('/')}{ingress_prefix}"
    station_name = sidecar.get("station_name") or config.display_station_name
    track_title = sidecar.get("track_title", "")
    track_artist = sidecar.get("track_artist", "")

    return _TEMPLATES.TemplateResponse(
        request,
        "clip.html",
        {
            "clip_id": clip_id,
            "expired": expired,
            "station_name": station_name,
            "track_title": track_title,
            "track_artist": track_artist,
            "clip_mp3_url": f"{public_base_url}/clips/{clip_id}.mp3",
            "og_image_url": f"{public_base_url}/og-card.png",
            "station_url": f"{public_base_url}/listen" if ingress_prefix else f"{public_base_url}/",
            "ingress_prefix": ingress_prefix,
            "asset_version": _ASSET_VERSION,
        },
    )


@router.get("/healthz")
async def healthz(request: Request):
    """Unauthenticated liveness probe — alive AND not silently failing with listeners."""
    start_time = getattr(request.app.state, "start_time", None)
    uptime = round(time.time() - start_time, 1) if start_time else 0
    _sync_runtime_state(request)
    runtime = _runtime_health_snapshot(request)
    state = request.app.state.station_state
    queue_empty_elapsed = _queue_empty_elapsed(state)
    silence_with_listeners = _silence_with_listeners(state, queue_empty_elapsed)
    body = {
        "status": "failing" if silence_with_listeners else "ok",
        "uptime_s": uptime,
        "silence_with_listeners": silence_with_listeners,
        "queue_empty_elapsed_s": round(queue_empty_elapsed, 1),
        "runtime": runtime,
    }
    return JSONResponse(content=body, status_code=503 if silence_with_listeners else 200)


@router.get("/readyz")
async def readyz(request: Request):
    """Unauthenticated readiness probe — is the station ready to stream?"""
    _sync_runtime_state(request)
    runtime = _runtime_health_snapshot(request)
    start_time = getattr(request.app.state, "start_time", None)
    queue_depth = runtime["queue_depth"]
    tasks_alive = runtime["producer_task_alive"] and runtime["playback_task_alive"]
    state = request.app.state.station_state
    queue_empty_elapsed = _queue_empty_elapsed(state)
    silence_with_listeners = _silence_with_listeners(state, queue_empty_elapsed)
    accepted_audio = state.current_stream_audible or state.last_air_monotonic is not None
    ready = (
        tasks_alive
        and accepted_audio
        and not silence_with_listeners
        and not state.session_stopped
        and not state.force_recovery_active
    )
    status = "ready" if ready else "stopped" if state.session_stopped else "starting"
    body = {
        "status": status,
        "ready": ready,
        "session_stopped": state.session_stopped,
        "watchdog_status": "ok",
        "queue_depth": queue_depth,
        "silence_with_listeners": silence_with_listeners,
        "queue_empty_elapsed_s": round(queue_empty_elapsed, 1),
        "runtime": runtime,
        "uptime_s": round(time.time() - start_time, 1) if start_time else 0,
    }
    return JSONResponse(content=body, status_code=200 if ready else 503)


@router.get("/public-status")
async def public_status(request: Request):
    """Return listener-safe station metadata and render-ready upcoming segments."""
    return _public_status_payload(request)


@router.get("/status")
async def status(
    request: Request,
    playlist_offset: int = 0,
    playlist_limit: int = 80,
    _: None = Depends(require_admin_access),
):
    """Return full admin diagnostics for the running station."""
    config = request.app.state.config
    state = request.app.state.station_state
    segment_queue = request.app.state.queue
    start_time = request.app.state.start_time
    station_mode = classify_station_mode(config, state)
    payload = _public_status_payload(request)
    runtime_health = _runtime_health_snapshot(request)
    provider_health = _provider_health_snapshot(config, state)
    runtime_status = _runtime_status_snapshot(request, runtime_health=runtime_health, provider_health=provider_health)
    ad_cast = _ad_cast_status_payload(config)
    playlist_offset, playlist_limit = _page_bounds(playlist_offset, playlist_limit, default_limit=80, max_limit=200)
    authorization = state.home_authorization or HomeAuthorization.narrow()
    try:
        moments_admin = (
            state.moment_store.to_admin_rows(limit=25)
            if state.moment_store is not None and authorization.allows_household_moments
            else None
        )
    except Exception:  # pragma: no cover - receipts must never break admin polling
        logger.debug("Moment receipt admin rows failed", exc_info=True)
        moments_admin = []
    playlist_page = _paginated_tracks(
        state.playlist,
        playlist_offset,
        playlist_limit,
        revision=state.playlist_revision,
        preferences=state.song_preferences,
    )
    payload.update(
        {
            "queue_depth": segment_queue.qsize(),
            # Honest airtime-ahead readout for the admin panel: the summed
            # duration of the rendered queue. Surfaces SECONDS of buffered audio,
            # not item count (3 short banters are not 3 songs of runway). Reads
            # the real asyncio queue and counts only immediately-playable audio
            # (same filtering as the producer_headroom.buffered_audio_sec
            # observability readout; NOT the unfiltered pacing count), so
            # banned/stale/evicted segments the playback loop will discard don't
            # inflate the number.
            "buffered_audio_sec": _queued_audio_seconds(segment_queue, state=state),
            "segments_produced": state.segments_produced,
            "tracks_played": len(state.played_tracks),
            "uptime_sec": round(time.time() - start_time),
            # Live production feed ("In produzione", admin-only): what the producer
            # is building right now + a short trail of just-finished work. Best-effort
            # display state; never gates audio. current is null when the producer is idle.
            "production": {
                "current": (
                    {
                        "phase": state.gen_phase,
                        "kind": state.gen_kind,
                        "label": state.gen_label,
                        "elapsed_sec": (int(time.monotonic() - state.gen_started) if state.gen_started else None),
                    }
                    if state.gen_phase
                    else None
                ),
                "recent": [{"kind": r["kind"], "label": r["label"], "ok": r["ok"]} for r in list(state.gen_recent)],
            },
            "playlist_source": _serialize_source(state.playlist_source),
            "jamendo": safe_jamendo_status(config, getattr(request.app.state, "jamendo_provider", None)),
            "external_extractors": _external_extractors_status(config),
            "produced_log": [{"type": e.type, "label": e.label, "timestamp": e.timestamp} for e in state.segment_log],
            "last_banter_script": state.last_banter_script,
            "last_ad_script": state.last_ad_script,
            "ha_context": state.ha_context if state.ha_context else None,
            "ha_details": _ha_details_payload(state),
            # Moment Receipts full trail (admin-only): entity, lane, confidence,
            # and status trail stay behind admin auth — the public payload gets
            # only generic labels via ha_moments.recent.
            "moments_admin": moments_admin,
            "pending_actions": list(state.pending_actions)[-10:] or None,
            # Background queue-from-search outcomes the admin couldn't see
            # synchronously; the UI toasts new entries by ts. Return the whole
            # bounded deque (maxlen 10) so a burst between two polls can't evict an
            # un-toasted entry past the client watermark. See admin.html.
            "external_add_notices": list(state.external_add_notices) or None,
            "station_mode": station_mode,
            "producer_errors": [
                {"type": e.type, "label": e.label, "metadata": e.metadata}
                for e in state.segment_log
                if e.metadata.get("error")
            ][-5:],
            "pacing": {
                "songs_between_banter": config.pacing.songs_between_banter,
                "songs_between_ads": config.pacing.songs_between_ads,
                "ad_spots_per_break": config.pacing.ad_spots_per_break,
                "songs_since_banter": state.songs_since_banter,
                "songs_since_ad": state.songs_since_ad,
            },
            "consumption": {
                "api_calls": state.api_calls,
                "input_tokens": state.api_input_tokens,
                "output_tokens": state.api_output_tokens,
                "tts_characters": state.tts_characters,
                # Model-aware cost: prices each model the session actually ran
                # (api_cost_estimate_usd stays present — protected UI element).
                **_consumption_cost(state, config.models),
                "cache_size_mb": _cached_cache_size_mb(config.cache_dir),
                "cache_limit_mb": config.max_cache_size_mb,
            },
            "listeners": {
                "active": state.listeners_active,
                "peak": state.listeners_peak,
                "total": state.listeners_total,
            },
            # Admin-only alias: this is cumulative HTTP stream connections, not
            # unique people.  Keep the legacy nested shape unchanged.
            "connections_total": state.listeners_total,
            "listener_session": state.listener_session.snapshot().to_dict(),
            "runtime_health": runtime_health,
            "runtime_status": runtime_status,
            "provider_health": provider_health,
            "chaos_mode": {
                "enabled": state.chaos_mode_active,
                "pending": state.chaos_pending.value if state.chaos_pending else "",
                "cutover_epoch": state.chaos_cutover_epoch,
                "last_degraded_reason": state.chaos_last_degraded_reason,
            },
            "force_pending": state.force_next.value if state.force_next else None,
            # Operator-attributed trigger (set only by /api/trigger) — the panel
            # uses THIS, never force_pending, so internal/rescue forces don't
            # false-light the "Triggered" row.
            "operator_force_pending": (state.operator_force_pending.value if state.operator_force_pending else None),
            "session_stopped": state.session_stopped,
            "song_preferences": _serialize_preference_status(state),
            "current_track_preference": _current_track_preference_score(state),
            "playlist": playlist_page["tracks"],
            "playlist_page": {
                "total": playlist_page["total"],
                "offset": playlist_page["offset"],
                "limit": playlist_page["limit"],
                "has_more": playlist_page["has_more"],
                "revision": playlist_page["revision"],
            },
            "brand": _serialize_brand(config.brand),
            "brand_warnings": list(config.brand_warnings),
            # Invalid direct mappings are skipped rather than recast. Keep the
            # config diagnostics behind admin auth and out of public status.
            "ad_cast": ad_cast,
        }
    )
    return payload


def _tail_log(path: str, lines: int = 15) -> list[str]:
    """Return the last lines from a log file efficiently (seek from end)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            return f.read().decode(errors="replace").splitlines()[-lines:]
    except Exception:
        return []
