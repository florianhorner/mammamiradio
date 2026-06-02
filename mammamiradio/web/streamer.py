"""Live streaming transport, HTTP routes, and admin controls."""

from __future__ import annotations

import asyncio
import copy
import importlib
import ipaddress
import logging
import math
import os
import random as _random
import re as _re
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from mammamiradio.audio.normalizer import humanize_norm_filename, load_track_metadata
from mammamiradio.audio.stream_format import stream_audio_metadata
from mammamiradio.core.capabilities import capabilities_to_dict, get_capabilities
from mammamiradio.core.models import (
    ChaosSubtype,
    PartyMode,
    PersonalityAxes,
    PlaylistSource,
    Segment,
    SegmentType,
    StationState,
    Track,
)
from mammamiradio.core.provider_checks import check_provider_keys
from mammamiradio.core.setup_status import addon_options_snippet, build_setup_status, classify_station_mode
from mammamiradio.home.ha_context import push_state_to_ha
from mammamiradio.home.ha_enrichment import EVENT_RETENTION_SECONDS
from mammamiradio.playlist.playlist import (
    ExplicitSourceError,
    load_explicit_source,
    write_persisted_source,
)
from mammamiradio.scheduling.scheduler import preview_upcoming
from mammamiradio.web.assets import (
    _ASSET_VERSION,
    _ASSETS_DIR,
    _STATIC_DIR,
    _TEMPLATES_DIR,
    _bust_static_cache,
)
from mammamiradio.web.mp3_frames import _skip_id3_and_xing_header
from mammamiradio.web.pages import _get_injected_html, _sanitize_ingress_prefix
from mammamiradio.web.persistence import (
    _CREDENTIAL_ENV_TO_FIELD,
    _CREDENTIAL_FIELDS,
    _apply_live_credentials,
    _sanitize_credential_value,
    _save_addon_option,
    _save_addon_options,
    _save_dotenv,
)
from mammamiradio.web.provider_verdict import (
    _record_provider_verdict,
    _run_provider_verdict,
)
from mammamiradio.web.ui_copy import copy_strings

logger = logging.getLogger(__name__)

router = APIRouter()
security = HTTPBasic(auto_error=False)

# TODO: split — this 2,395-line god module is a postal address, not a destination.
# See docs/2026-04-28-cathedral-restructure.md (PR 5) for the routes/auth/playback split plan.
# Path roots, the static-asset content hash (_ASSET_VERSION), and
# _bust_static_cache now live in web/assets.py and are imported above.
#
# Jinja2 templates for brand-engine listener page (PR-C). Admin/live still use
# string-replace via _inject_ingress_prefix (web/pages.py); only listener migrates to Jinja for now.
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# Admin/live pages still loaded as raw strings + post-render prefix injection.
# Listener no longer needs _LISTENER_HTML — it's rendered from template per-request.
_LISTENER_HTML = _bust_static_cache((_TEMPLATES_DIR / "listener.html").read_text())  # kept for tests + fallback

_ADMIN_HTML = _bust_static_cache((_TEMPLATES_DIR / "admin.html").read_text())
_LIVE_HTML = _bust_static_cache((_TEMPLATES_DIR / "live.html").read_text())


def _as_int_index(value, default: int = -1) -> int:
    """Best-effort parse for playlist index payload fields."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_CSRF_TOKEN_PLACEHOLDER = "__MAMMAMIRADIO_CSRF_TOKEN__"
SESSION_STOPPED_FLAG = "session_stopped.flag"
SILENCE_FAILURE_SECONDS = 30.0
QUEUE_FALLBACK_WAIT_SECONDS = 5.0
STARTUP_GRACE_SECONDS = 30.0
CLIP_RATE_LIMIT_SECONDS = 10.0
CLIP_RATE_PRUNE_SECONDS = 300.0
CLIP_DURATION_SECONDS = 30
CLIP_MAX_SAVED = 50
DEFAULT_CLIP_BITRATE_KBPS = 192


def _purge_segment_queue(q) -> int:
    """Drain all pre-produced segments from the queue and unlink temp files."""
    purged = 0
    while not q.empty():
        try:
            seg = q.get_nowait()
            if seg.ephemeral:
                seg.path.unlink(missing_ok=True)
            q.task_done()
            purged += 1
        except Exception:
            break
    return purged


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


def _clear_session_stopped(state: StationState, config) -> None:
    """Resume playback state and clear the persisted stop marker."""
    state.session_stopped = False
    state.last_state_change_at = time.time()
    state.resume_event.set()
    _persist_session_stopped(config, False)


def _has_any_mp3(path: Path) -> bool:
    """Return True when a directory contains at least one MP3 file."""
    if not path.exists() or not path.is_dir():
        return False
    return any(path.glob("*.mp3"))


_golden_path_cache: dict | None = None
_golden_path_cache_ts: float = 0.0
_GOLDEN_PATH_TTL = 10.0  # seconds — music sources change rarely

_cache_size_mb_val: float = 0.0
_cache_size_mb_ts: float = 0.0
_CACHE_SIZE_TTL = 30.0  # seconds — stat()-ing every MP3 is expensive on Pi


def _cached_cache_size_mb(cache_dir: Path) -> float:
    """Return total MP3 cache size in MB, recomputed at most every 30s."""
    global _cache_size_mb_val, _cache_size_mb_ts
    now = time.time()
    if (now - _cache_size_mb_ts) < _CACHE_SIZE_TTL:
        return _cache_size_mb_val
    _cache_size_mb_val = round(
        sum(f.stat().st_size for f in cache_dir.glob("*.mp3") if f.is_file()) / (1024 * 1024),
        1,
    )
    _cache_size_mb_ts = now
    return _cache_size_mb_val


def _golden_path_status(config, state) -> dict:
    """Build a single, explicit music onboarding status for UI surfaces."""
    global _golden_path_cache, _golden_path_cache_ts
    now = time.time()
    if _golden_path_cache is not None and (now - _golden_path_cache_ts) < _GOLDEN_PATH_TTL:
        return _golden_path_cache

    allow_ytdlp = os.getenv("MAMMAMIRADIO_ALLOW_YTDLP", "false").lower() in ("true", "1", "yes")
    has_demo_assets = _has_any_mp3(_ASSETS_DIR / "demo" / "music")
    has_local_music = _has_any_mp3(Path("music"))

    sources: list[str] = []
    if has_demo_assets:
        sources.append("bundled demo tracks")
    if has_local_music:
        sources.append("local music/*.mp3 files")
    if allow_ytdlp:
        sources.append("yt-dlp downloads")

    shared = {
        "fallback_sources": sources,
        "silent_music_fallback": not sources,
    }

    if sources:
        source_label = ", ".join(sources)
        has_llm = bool(config.anthropic_api_key or config.openai_api_key)
        result = {
            "stage": "music_available",
            "blocking": False,
            "headline": f"Music via {source_label}.",
            "detail": (
                f"Playing music from: {source_label}."
                + ("" if has_llm else " Add an Anthropic API key for AI-generated banter.")
            ),
            "steps": [],
            **shared,
        }
        _golden_path_cache = result
        _golden_path_cache_ts = now
        return result

    result = {
        "stage": "needs_music_source",
        "blocking": True,
        "headline": "No music source configured.",
        "detail": "Set MAMMAMIRADIO_ALLOW_YTDLP=true or add MP3 files to music/.",
        "steps": [
            "Set MAMMAMIRADIO_ALLOW_YTDLP=true for chart music, or",
            "Place MP3 files in the music/ directory.",
        ],
        **shared,
    }
    _golden_path_cache = result
    _golden_path_cache_ts = now
    return result


def _sync_runtime_state(request: Request) -> None:
    """Refresh UI-facing state from int-lived runtime backends."""
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
    shadow_depth = len(state.queued_segments)
    now_streaming = state.now_streaming or {}
    now_metadata = now_streaming.get("metadata", {}) if isinstance(now_streaming, dict) else {}
    audio_source = now_metadata.get("audio_source", "")
    fallback_active = (
        bool(now_metadata.get("fallback"))
        or bool(audio_source and str(audio_source).startswith("fallback"))
        or bool(now_metadata.get("queue_drain_recovery"))
        or bool(now_metadata.get("resume_bridge"))
        or bool(now_metadata.get("silence_fallback"))
        or audio_source in ("norm_cache", "emergency_tone")
    )
    if not audio_source and fallback_active:
        audio_source = "canned"
    if not audio_source or audio_source == "prewarm" or (audio_source == "download" and not fallback_active):
        playlist_source = state.playlist_source
        if playlist_source is not None:
            audio_source = playlist_source.kind
    producer_task = getattr(request.app.state, "producer_task", None)
    playback_task = getattr(request.app.state, "playback_task", None)
    producer_alive = True if producer_task is None else not producer_task.done()
    playback_alive = True if playback_task is None else not playback_task.done()
    queue_empty_elapsed = _queue_empty_elapsed(state)
    return {
        "queue_depth": queue_depth,
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
        "stock": "Stock copy",
        "edge": "Edge TTS",
        "silence": "Silence fallback",
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


def _provider_status(
    provider_class: str,
    *,
    primary_provider: str,
    current_provider: str,
    fallback_active: bool,
    reason: str,
    state: StationState,
) -> dict:
    saved = state.runtime_provider_state.get(provider_class, {})
    return {
        "provider_class": provider_class,
        "primary_provider": primary_provider,
        "primary_label": _runtime_provider_label(primary_provider),
        "current_provider": current_provider,
        "current_label": _runtime_provider_label(current_provider),
        "fallback_active": fallback_active,
        "last_switch_timestamp": saved.get("last_switch_timestamp") if saved else None,
        "switch_reason": saved.get("reason") or reason,
    }


def _script_provider_status(config, state: StationState, provider_health: dict) -> dict:
    anthropic_degraded = bool(provider_health.get("anthropic", {}).get("degraded"))
    if config.anthropic_api_key:
        primary = "anthropic"
        saved = state.runtime_provider_state.get("script_provider", {})
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
    return _provider_status(
        "script_provider",
        primary_provider=primary,
        current_provider=current,
        fallback_active=fallback_active,
        reason=reason,
        state=state,
    )


def _tts_provider_status(config, state: StationState) -> dict:
    openai_hosts = any((host.engine or "edge") == "openai" for host in config.hosts)
    if openai_hosts:
        primary = "openai"
        if config.openai_api_key:
            current = "openai"
            fallback_active = False
            reason = "OpenAI TTS is configured for at least one host"
        else:
            current = "edge"
            fallback_active = True
            reason = "OpenAI TTS host configured but OPENAI_API_KEY is unavailable; Edge voice fallback is active"
    else:
        primary = current = "edge"
        fallback_active = False
        reason = "Edge TTS is the configured voice provider"
    return _provider_status(
        "tts_provider",
        primary_provider=primary,
        current_provider=current,
        fallback_active=fallback_active,
        reason=reason,
        state=state,
    )


def _runtime_status_snapshot(
    request: Request,
    runtime_health: dict | None = None,
    provider_health: dict | None = None,
) -> dict:
    config = request.app.state.config
    state = request.app.state.station_state
    runtime_health = runtime_health or _runtime_health_snapshot(request)
    provider_health = provider_health or _provider_health_snapshot(config, state)

    audio_current = str(runtime_health.get("audio_source") or "unknown")
    audio_primary = state.playlist_source.kind if state.playlist_source is not None else audio_current
    audio_fallback = bool(runtime_health.get("failover_active"))
    audio_reason = "Fallback audio is currently on air" if audio_fallback else "Primary audio source is on air"
    audio_status = _provider_status(
        "audio_source",
        primary_provider=audio_primary or "unknown",
        current_provider=audio_current,
        fallback_active=audio_fallback,
        reason=audio_reason,
        state=state,
    )
    script_status = _script_provider_status(config, state, provider_health)
    tts_status = _tts_provider_status(config, state)
    providers = {
        "audio_source": audio_status,
        "script_provider": script_status,
        "tts_provider": tts_status,
    }
    fallback_active = any(item["fallback_active"] for item in providers.values())
    task_blocked = not runtime_health.get("producer_task_alive", True) or not runtime_health.get(
        "playback_task_alive",
        True,
    )
    if task_blocked:
        health_state = "blocked"
        health_color = "red"
        health_explanation = "A runtime task is stopped; playback needs operator attention."
    elif fallback_active:
        health_state = "degraded"
        health_color = "yellow"
        active = [item["current_label"] for item in providers.values() if item["fallback_active"]]
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
                "runtime_provider_classes": [name for name, item in providers.items() if item["fallback_active"]],
            },
        )

    events_desc = list(reversed(state.runtime_events))
    recent_events = [e.to_dict() for e in events_desc[:10]]
    last_switch = recent_events[0] if recent_events else None
    failover_events = [e.to_dict() for e in events_desc if e.fallback_active][:10]
    return {
        "health_state": health_state,
        "health_color": health_color,
        "health_explanation": health_explanation,
        "fallback_active": fallback_active,
        "providers": providers,
        "last_switch_timestamp": last_switch.get("timestamp") if last_switch else None,
        "switch_reason": last_switch.get("reason") if last_switch else "",
        "recent_events": recent_events,
        "failover_events": failover_events,
        "no_failover_message": "No failover in current session." if not failover_events else "",
    }


def _runtime_monotonic() -> float:
    """Monotonic clock for readiness and silence accounting."""
    return time.monotonic()


def _queue_empty_elapsed(state: StationState) -> float:
    return _runtime_monotonic() - state.queue_empty_since if state.queue_empty_since is not None else 0.0


def _silence_with_listeners(state: StationState, queue_empty_elapsed: float) -> bool:
    return queue_empty_elapsed > SILENCE_FAILURE_SECONDS and state.listeners_active > 0


def _identity_key(value: str) -> str:
    """Normalize listener-facing titles enough to compare cache fallbacks."""
    return _re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _identity_matches(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    return min(len(left), len(right)) >= 12 and (left in right or right in left)


def _segment_identity_keys(segment: dict) -> set[str]:
    """Return comparable labels for a streamed segment."""
    keys: set[str] = set()
    label = str(segment.get("label") or "").strip()
    if label:
        keys.add(_identity_key(label))
    metadata = segment.get("metadata") or {}
    if isinstance(metadata, dict):
        title = str(metadata.get("title") or "").strip()
        title_only = str(metadata.get("title_only") or "").strip()
        artist = str(metadata.get("artist") or "").strip()
        for value in (title, title_only):
            if value:
                keys.add(_identity_key(value))
            if value and artist:
                keys.add(_identity_key(f"{artist} {value}"))
    return {key for key in keys if key}


def _norm_cache_identity_keys(path: Path) -> set[str]:
    """Return comparable title/artist labels for a normalized cache file."""
    keys = {_identity_key(humanize_norm_filename(path.name))}
    sidecar = load_track_metadata(path)
    if sidecar:
        title = str(sidecar.get("title") or "").strip()
        artist = str(sidecar.get("artist") or "").strip()
        if title:
            keys.add(_identity_key(title))
        if title and artist:
            keys.add(_identity_key(f"{artist} {title}"))
    return {key for key in keys if key}


def _select_norm_cache_rescue(cache_dir: Path, state: StationState) -> Path | None:
    """Pick a cache rescue clip without replaying the current/recent song first."""
    norm_files = sorted(cache_dir.glob("norm_*.mp3"))
    if not norm_files:
        return None

    recent_keys: set[str] = set()
    if state.now_streaming:
        recent_keys.update(_segment_identity_keys(state.now_streaming))
    for entry in list(state.stream_log)[-5:]:
        if entry.type == SegmentType.MUSIC.value:
            recent_keys.update(_segment_identity_keys({"label": entry.label, "metadata": entry.metadata}))
    if not recent_keys:
        return _random.choice(norm_files)

    candidates: list[Path] = []
    for path in norm_files:
        path_keys = _norm_cache_identity_keys(path)
        if not any(_identity_matches(path_key, recent_key) for path_key in path_keys for recent_key in recent_keys):
            candidates.append(path)

    return _random.choice(candidates or norm_files)


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
) -> dict:
    """Atomically swap the station source and trigger immediate cutover."""
    state = request.app.state.station_state

    state.switch_playlist(tracks, resolved_source)

    # Immediate cutover: purge queued segments and skip current playback
    purged = _purge_segment_queue(request.app.state.queue)
    state.queued_segments.clear()
    skipped = False
    if state.now_streaming:
        request.app.state.skip_event.set()
        skipped = True

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
    }


def _serialize_source(source: PlaylistSource | None) -> dict | None:
    if not source:
        return None
    return {
        "kind": source.kind,
        "source_id": source.source_id,
        "url": source.url,
        "label": source.label,
        "track_count": source.track_count,
        "selected_at": source.selected_at,
    }


def _serialize_brand(brand) -> dict:
    """Serialize listener/admin brand config through one shared shape."""
    return {
        "station_name": brand.station_name,
        "frequency": brand.frequency,
        "city": brand.city,
        "founded": brand.founded,
        "tagline": brand.tagline,
        "about": brand.about,
        "opengraph_subtitle": brand.opengraph_subtitle,
        "hosts": [
            {"engine_host": h.engine_host, "display_name": h.display_name, "description": h.description}
            for h in brand.hosts
        ],
        "theme": {
            "primary_color": brand.theme.primary_color,
            "accent_color": brand.theme.accent_color,
            "background_color": brand.theme.background_color,
            "display_font": brand.theme.display_font,
            "body_font": brand.theme.body_font,
            "mono_font": brand.theme.mono_font,
        },
    }


def _preview_tracks(tracks: list, limit: int = 3) -> dict:
    return {
        "track_count": len(tracks),
        "tracks": [{"title": track.title, "artist": track.artist} for track in tracks[:limit]],
    }


def _serialize_track(track: Track) -> dict:
    return {
        "title": track.title,
        "artist": track.artist,
        "display": track.display,
        "spotify_id": track.spotify_id,
        "album_art": track.album_art,
        "source": track.source,
        "year": track.year,
        "youtube_id": track.youtube_id,
        "duration_ms": track.duration_ms,
    }


def _source_options_reason(config, exc: Exception) -> str:
    return f"Source loading failed: {exc}"


def _get_csrf_token(app) -> str:
    token = getattr(app.state, "csrf_token", "")
    if not token:
        token = secrets.token_urlsafe(32)
        app.state.csrf_token = token
    return token


def _inject_csrf_token(html: str, token: str) -> str:
    return html.replace(_CSRF_TOKEN_PLACEHOLDER, token)


def _same_origin(request: Request, candidate: str) -> bool:
    parsed = urlparse(candidate)
    if not parsed.scheme or not parsed.netloc:
        return False
    request_url = request.url

    # Normalize ports: None means the default for the scheme (80/443)
    def _effective_port(port, scheme: str) -> int:
        if port is not None:
            return port
        return 443 if scheme == "https" else 80

    return (
        parsed.scheme == request_url.scheme
        and parsed.hostname == request_url.hostname
        and _effective_port(parsed.port, parsed.scheme) == _effective_port(request_url.port, request_url.scheme)
    )


def _enforce_csrf_for_basic_auth(request: Request, credentials: HTTPBasicCredentials | None, config) -> None:
    if request.method.upper() not in _MUTATING_METHODS:
        return
    if _is_loopback_client(request):
        return

    ingress_prefix = request.headers.get("X-Ingress-Path", "")
    if config.is_addon and ingress_prefix and _is_hassio_or_loopback(request):
        return
    admin_token_header = request.headers.get("X-Radio-Admin-Token", "")
    if config.admin_token and admin_token_header and secrets.compare_digest(admin_token_header, config.admin_token):
        return
    if not config.admin_password or not credentials:
        return

    csrf_token = request.headers.get("X-Radio-CSRF-Token", "")
    if csrf_token and secrets.compare_digest(csrf_token, _get_csrf_token(request.app)):
        return

    origin = request.headers.get("Origin", "")
    if origin and _same_origin(request, origin):
        return

    referer = request.headers.get("Referer", "")
    if referer and _same_origin(request, referer):
        return

    raise HTTPException(
        status_code=403,
        detail="Cross-site admin write blocked. Reload the dashboard and retry.",
    )


class LiveStreamHub:
    """Fan out live audio chunks to all connected listener streams."""

    def __init__(self, listener_queue_size: int = 128):
        self._listener_queue_size = listener_queue_size
        self._listeners: dict[int, asyncio.Queue[bytes | None]] = {}
        self._next_listener_id = 0
        self._state: StationState | None = None

    def bind_state(self, state: StationState) -> None:
        """Attach station state for listener tracking. Call once at startup."""
        self._state = state

    def subscribe(self) -> tuple[int, asyncio.Queue[bytes | None]]:
        """Register a listener and return its dedicated chunk queue."""
        listener_id = self._next_listener_id
        self._next_listener_id += 1
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=self._listener_queue_size)
        self._listeners[listener_id] = queue
        active = len(self._listeners)
        logger.info("Listener connected (%d active)", active)
        if self._state is not None:
            self._state.listeners_active = active
            self._state.listeners_total += 1
            self._state.listeners_peak = max(self._state.listeners_peak, active)
            self._state.new_listeners_pending += 1
        return listener_id, queue

    def unsubscribe(self, listener_id: int) -> None:
        """Remove a listener and drop any future broadcast work for it."""
        if self._listeners.pop(listener_id, None) is not None:
            active = len(self._listeners)
            logger.info("Listener disconnected (%d active)", active)
            if self._state is not None:
                self._state.listeners_active = active

    def has_listener(self, listener_id: int) -> bool:
        """Return whether a listener is still subscribed."""
        return listener_id in self._listeners

    async def broadcast(self, chunk: bytes) -> None:
        """Push one encoded audio chunk to every listener, dropping laggards."""
        slow_listeners = []
        for listener_id, queue in list(self._listeners.items()):
            try:
                queue.put_nowait(chunk)
            except asyncio.QueueFull:
                slow_listeners.append(listener_id)

        for listener_id in slow_listeners:
            logger.warning("Dropping slow listener %d", listener_id)
            self.unsubscribe(listener_id)

    def close(self) -> None:
        """Signal all listeners to terminate and clear the hub."""
        listeners = list(self._listeners.items())
        self._listeners.clear()
        if self._state is not None:
            self._state.listeners_active = 0
        for _, queue in listeners:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass


_HASSIO_NETWORK = ipaddress.ip_network("172.30.32.0/23")

# Private/trusted networks: loopback, RFC1918, link-local, HA Supervisor,
# and Tailscale/CGNAT (100.64.0.0/10). A self-hosted radio station trusts
# its own LAN — the operator installed it themselves.
_TRUSTED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT / Tailscale
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
    _HASSIO_NETWORK,
]


def _is_loopback_client(request: Request) -> bool:
    """Return whether the current request originated from localhost."""
    if not request.client:
        return False
    host = request.client.host
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_private_network(request: Request) -> bool:
    """Return True for loopback, RFC1918, Tailscale CGNAT, or HA Supervisor."""
    if _is_loopback_client(request):
        return True
    if not request.client:
        return False
    try:
        addr = ipaddress.ip_address(request.client.host)
    except ValueError:
        return False
    return any(addr in net for net in _TRUSTED_NETWORKS)


def _is_hassio_or_loopback(request: Request) -> bool:
    """Return True for loopback or the Hassio internal network."""
    if _is_loopback_client(request):
        return True
    if not request.client:
        return False
    try:
        return ipaddress.ip_address(request.client.host) in _HASSIO_NETWORK
    except ValueError:
        return False


def _enforce_csrf_for_private_network(request: Request) -> None:
    """Block cross-site mutating requests from private networks.

    LAN trust skips credential checks but a browser on the LAN could still
    be tricked into a cross-site POST. Require same-origin or CSRF token
    on mutating methods.
    """
    if request.method.upper() not in _MUTATING_METHODS:
        return

    csrf_token = request.headers.get("X-Radio-CSRF-Token", "")
    if csrf_token and secrets.compare_digest(csrf_token, _get_csrf_token(request.app)):
        return

    origin = request.headers.get("Origin", "")
    if origin and _same_origin(request, origin):
        return

    referer = request.headers.get("Referer", "")
    if referer and _same_origin(request, referer):
        return

    raise HTTPException(
        status_code=403,
        detail="Cross-site admin write blocked. Reload the dashboard and retry.",
    )


def require_admin_access(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> None:
    """Authorize admin-only routes using configured credentials or local trust."""
    config = request.app.state.config

    # Loopback is fully trusted — same machine, no CSRF risk.
    if _is_loopback_client(request):
        return

    # HA Supervisor network is Docker-internal (not user-accessible), so
    # CSRF from a browser on that network is not a real threat. Fully trust
    # it in addon mode so HA automations (rest_command, etc.) work without tokens.
    if config.is_addon and _is_hassio_or_loopback(request):
        return

    # Explicit auth for all non-local traffic when credentials are configured.
    if config.admin_token:
        token = request.headers.get("X-Radio-Admin-Token")
        if token and secrets.compare_digest(token, config.admin_token):
            return

    if config.admin_password:
        username = credentials.username if credentials else ""
        password = credentials.password if credentials else ""
        if secrets.compare_digest(username, config.admin_username) and secrets.compare_digest(
            password, config.admin_password
        ):
            _enforce_csrf_for_basic_auth(request, credentials, config)
            return
        client_ip = request.client.host if request.client else "unknown"
        logger.warning("Failed admin auth attempt from %s", client_ip)
        raise HTTPException(
            status_code=401,
            detail="Admin authentication required",
            headers={"WWW-Authenticate": 'Basic realm="mammamiradio admin"'},
        )

    if config.admin_token:
        client_ip = request.client.host if request.client else "unknown"
        logger.warning("Missing admin token from %s", client_ip)
        raise HTTPException(
            status_code=401,
            detail="X-Radio-Admin-Token required",
        )

    # Backward-compatible fallback when no admin credentials are configured.
    # In standalone mode load_config() now rejects a non-loopback bind without
    # creds, so in production this only fires for loopback binds (already
    # short-circuited above). Reachable here mainly via test apps built without
    # creds — kept so credential-less LAN deployments keep working with CSRF.
    if _is_private_network(request):
        _enforce_csrf_for_private_network(request)
        return

    raise HTTPException(
        status_code=403,
        detail="Admin endpoints are only available from private networks unless admin auth is configured",
    )


async def run_playback_loop(app) -> None:
    """Play queued segments on a single station timeline and fan out audio chunks."""
    chunk_size = 4096
    segment_queue = app.state.queue
    skip_event = app.state.skip_event
    state = app.state.station_state
    config = app.state.config
    hub = app.state.stream_hub
    bytes_per_sec = (config.audio.bitrate * 1000) / 8  # bitrate is in kbps; convert to bytes/sec
    _persist_tasks: set[asyncio.Task] = set()  # prevent GC of fire-and-forget tasks
    _ha_push_tasks: set[asyncio.Task] = set()  # prevent GC of HA push tasks

    while True:
        # Pause when nobody is listening — don't burn API tokens or disk on an empty room.
        # The queue stays full; the moment a listener connects, playback resumes instantly.
        if not hub._listeners:
            state.queue_empty_since = None
            await asyncio.sleep(1.0)
            continue

        # Priority slot: interrupt bridge audio plays before anything in the queue.
        _bridge_segment: Segment | None = None
        if state.interrupt_slot is not None:
            bridge_path = state.interrupt_slot
            state.interrupt_slot = None
            if bridge_path.exists():
                _bridge_segment = Segment(
                    type=SegmentType.BANTER,
                    path=bridge_path,
                    metadata={"type": "banter", "interrupt": True},
                    ephemeral=state.interrupt_slot_ephemeral,
                )
                state.interrupt_slot_ephemeral = False
                state.queue_empty_since = None
            else:
                logger.warning("Interrupt slot path missing: %s — skipping bridge", bridge_path)
                state.interrupt_slot_ephemeral = False

        pulled_from_queue = False
        segment: Segment
        if _bridge_segment is not None:
            segment = _bridge_segment
        else:
            if segment_queue.empty() and state.queue_empty_since is None:
                # Mark the exact moment playback ran out of audio. The 30s wait_for()
                # below is part of the listener-visible silence window.
                state.queue_empty_since = _runtime_monotonic()
            try:
                segment = await asyncio.wait_for(segment_queue.get(), timeout=QUEUE_FALLBACK_WAIT_SECONDS)
                pulled_from_queue = True
                state.queue_empty_since = None
            except TimeoutError:
                if not hub._listeners:
                    state.queue_empty_since = None
                    continue

                if state.queue_empty_since is None:
                    state.queue_empty_since = _runtime_monotonic()
                elapsed = _runtime_monotonic() - state.queue_empty_since

                # Serve a canned clip instead of dead air while the producer catches up
                from mammamiradio.scheduling.producer import _pick_canned_clip

                fallback = _pick_canned_clip("banter", state=state) or _pick_canned_clip("welcome")
                if fallback:
                    logger.info("Queue empty — serving fallback clip: %s", fallback.name)
                    state.queue_empty_since = None
                    segment = Segment(
                        type=SegmentType.BANTER,
                        path=fallback,
                        metadata={"type": "banter", "canned": True, "fallback": True},
                        ephemeral=False,
                    )
                else:
                    rescued_from_norm = False
                    if elapsed >= QUEUE_FALLBACK_WAIT_SECONDS:
                        rescue = _select_norm_cache_rescue(config.cache_dir, state)
                        if rescue:
                            logger.warning(
                                "Queue empty %ds - rescuing with norm cache: %s",
                                int(elapsed),
                                rescue.name,
                            )
                            state.queue_empty_since = None
                            rescued_from_norm = True
                            sidecar = load_track_metadata(rescue)
                            if sidecar:
                                rescue_title = f"{sidecar['artist']} – {sidecar['title']}"
                                rescue_artist: str | None = sidecar["artist"]
                            else:
                                rescue_title = humanize_norm_filename(rescue.name)
                                rescue_artist = None
                            segment = Segment(
                                type=SegmentType.MUSIC,
                                path=rescue,
                                metadata={
                                    "type": "music",
                                    "title": rescue_title,
                                    **({"artist": rescue_artist} if rescue_artist else {}),
                                    "audio_source": "fallback_norm_cache",
                                    "fallback": True,
                                },
                                ephemeral=False,
                            )

                    if rescued_from_norm:
                        pass
                    else:
                        # Try bundled demo assets as a last-resort audio source before
                        # forcing banter. Raw (un-normalized) audio beats dead air.
                        demo_music_dir = _ASSETS_DIR / "demo" / "music"
                        demo_files = list(demo_music_dir.glob("*.mp3")) if demo_music_dir.exists() else []
                        if demo_files:
                            rescue = _random.choice(demo_files)
                            # Parse "Artist - Title.mp3" so the listener UI shows proper
                            # metadata instead of the raw stem. Preserves the illusion.
                            stem = rescue.stem
                            if " - " in stem:
                                rescue_artist, rescue_title = stem.split(" - ", 1)
                                rescue_artist = rescue_artist.strip() or "Unknown"
                                rescue_title = rescue_title.strip() or stem
                            else:
                                rescue_artist = "Unknown"
                                rescue_title = stem
                            logger.warning(
                                "Queue empty %ds - rescuing with demo asset: %s",
                                int(elapsed),
                                rescue.name,
                            )
                            state.queue_empty_since = None
                            segment = Segment(
                                type=SegmentType.MUSIC,
                                path=rescue,
                                metadata={
                                    "type": "music",
                                    "title": rescue_title,
                                    "artist": rescue_artist,
                                    "audio_source": "fallback_demo_asset",
                                    "fallback": True,
                                },
                                ephemeral=False,
                            )

                    if rescued_from_norm or (segment_queue.empty() and state.queue_empty_since is None):
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
                        continue

        prev_last_provider_event = state.runtime_events[-1] if state.runtime_events else None
        state.on_stream_segment(segment)
        if state.runtime_events:
            new_last_provider_event = state.runtime_events[-1]
            if new_last_provider_event is not prev_last_provider_event:
                logger.info("provider_switch_event", extra=new_last_provider_event.to_dict())
        if pulled_from_queue and state.queued_segments:
            state.queued_segments.pop(0)
        logger.info(
            ">>> NOW STREAMING %s: %s",
            segment.type.value,
            segment.metadata.get("title", segment.metadata),
        )

        if config.homeassistant.enabled and config.ha_token and config.homeassistant.url:
            _ha_task = asyncio.create_task(
                push_state_to_ha(
                    ha_url=config.homeassistant.url,
                    ha_token=config.ha_token,
                    now_streaming=copy.deepcopy(state.now_streaming),
                    current_track=state.current_track,
                    listeners_active=state.listeners_active,
                    session_stopped=state.session_stopped,
                )
            )
            _ha_push_tasks.add(_ha_task)
            _ha_task.add_done_callback(_ha_push_tasks.discard)

        try:
            send_start = time.monotonic()
            bytes_sent = 0
            was_skipped = False
            skip_event.clear()
            with open(segment.path, "rb") as f:
                _skip_id3_and_xing_header(f)
                while chunk := f.read(chunk_size):
                    if skip_event.is_set():
                        logger.info("Skipping current segment")
                        was_skipped = True
                        skip_event.clear()
                        break

                    await hub.broadcast(chunk)
                    bytes_sent += len(chunk)

                    # Feed the clip ring buffer for "share WTF moment"
                    clip_buf = getattr(app.state, "clip_ring_buffer", None)
                    if clip_buf is not None:
                        clip_buf.append(chunk)

                    elapsed = time.monotonic() - send_start
                    expected = bytes_sent / bytes_per_sec
                    ahead = expected - elapsed
                    if ahead > 0.005:
                        await asyncio.sleep(ahead)
            if segment.type == SegmentType.MUSIC and not was_skipped:
                listen_sec = bytes_sent / bytes_per_sec if bytes_per_sec else None
                # Fire-and-forget: persistence must not block the handoff to the next
                # segment — on Pi, the SQLite writes can take int enough to cause
                # audible gaps between songs.
                coro = _persist_completed_music(state, config, segment.metadata, listen_sec=listen_sec)
                task = asyncio.create_task(coro)
                _persist_tasks.add(task)
                task.add_done_callback(_persist_tasks.discard)
        finally:
            if segment.ephemeral:
                segment.path.unlink(missing_ok=True)
            if pulled_from_queue:
                segment_queue.task_done()


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
        state.pending_actions.append(
            {
                "type": "ha_directive",
                "source": "skip_bit",
                "label": track_name,
                "created_at": time.time(),
            }
        )


async def _audio_generator(request: Request):
    """Stream the live station feed from the playback loop."""
    state = request.app.state.station_state
    config = request.app.state.config
    if state.session_stopped:
        _clear_session_stopped(state, config)

    hub = request.app.state.stream_hub
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


def _render_admin_response(request: Request, prefix: str) -> HTMLResponse:
    # CSP: 'unsafe-inline' is required because admin.html has inline event handlers
    # (onclick, oninput, onchange) on ~40 elements that cannot carry a nonce attribute.
    # esc() on all HA fields in admin.html is the load-bearing XSS defense.
    html = _get_injected_html("admin", _ADMIN_HTML, prefix)
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
    return {
        "brand": config.brand,
        "ingress_prefix": _sanitize_ingress_prefix(prefix),
        "csrf_token": _get_csrf_token(request.app),
        "asset_version": _ASSET_VERSION,
        "copy": copy_strings(bool(config.super_italian_mode)),
    }


@router.get("/", response_class=HTMLResponse)
async def listener_home(request: Request):
    """Serve the public listener UI, except trusted HA ingress opens the control room."""
    prefix = request.headers.get("X-Ingress-Path", "")
    config = request.app.state.config
    if config.is_addon and prefix and _is_hassio_or_loopback(request):
        return _render_admin_response(request, prefix)
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
    return _render_admin_response(request, prefix)


@router.get("/live", response_class=HTMLResponse, dependencies=[Depends(require_admin_access)])
async def live_panel(request: Request):
    """Serve the mobile live control room — phone-optimised operator surface."""
    prefix = request.headers.get("X-Ingress-Path", "")
    html = _get_injected_html("live", _LIVE_HTML, prefix)
    html = _inject_csrf_token(html, _get_csrf_token(request.app))
    csp = "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com"
    return HTMLResponse(content=html, headers={"Content-Security-Policy": csp})


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
    cache_key = f"{config.brand.station_name}:{track.cache_key if track else 'idle'}"

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


@router.get("/sw.js")
async def service_worker():
    """Serve the PWA service worker from root scope."""
    return FileResponse(
        _STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@router.get("/static/{filename:path}")
async def static_files(filename: str):
    """Serve PWA static assets (manifest, icons)."""
    filepath = (_STATIC_DIR / filename).resolve()
    if not filepath.is_relative_to(_STATIC_DIR) or not filepath.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(filepath)


@router.get("/stream")
async def stream(request: Request):
    """Expose the live MP3 stream consumed by browsers and audio players."""
    config = request.app.state.config
    audio_format = stream_audio_metadata(config)
    headers = {
        "icy-name": config.station.name.replace("\r", "").replace("\n", ""),
        "icy-genre": config.station.theme[:64].replace("\r", "").replace("\n", ""),
        "icy-br": str(audio_format["bitrate_kbps"]),
        "Cache-Control": "no-cache, no-store",
        "Connection": "keep-alive",
    }
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
async def setup_status(request: Request, _: None = Depends(require_admin_access)):
    """Return the current first-run setup snapshot for onboarding."""
    config = request.app.state.config
    state = request.app.state.station_state
    return build_setup_status(config, state)


@router.post("/api/setup/recheck")
async def setup_recheck(request: Request, _: None = Depends(require_admin_access)):
    """Force a fresh setup snapshot."""
    config = request.app.state.config
    state = request.app.state.station_state
    return build_setup_status(config, state)


@router.post("/api/setup/provider-check")
async def setup_provider_check(request: Request, _: None = Depends(require_admin_access)):
    """Run active, secret-safe Anthropic/OpenAI connectivity checks.

    Multiple rapid clicks should share one in-flight probe set instead of
    launching overlapping 12-second outbound checks against every provider.
    """
    config = request.app.state.config

    def _record_if_task_keys_match(probe_result: dict) -> None:
        # The verdict must reflect the keys the SHARED in-flight task actually probed,
        # not this waiter's snapshot. A later request joining an old task after a
        # concurrent save swapped a key must NOT accept that task's stale 401. Compare
        # current config to the keys captured when the task was created.
        snapshot = getattr(request.app.state, "_provider_check_task_keys", None)
        if snapshot == (config.anthropic_api_key, config.openai_api_key):
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
            request.app.state._provider_check_task_keys = (config.anthropic_api_key, config.openai_api_key)
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


@router.post("/api/setup/save-keys", dependencies=[Depends(require_admin_access)])
async def save_keys(request: Request):
    """Save API credentials to .env (or addon options.json) and update the live config."""
    body = await request.json()
    updates = _credential_updates_from_env_payload(body, require_nonempty=True)

    if not updates:
        return {"ok": False, "error": "No keys provided"}

    await _persist_and_apply_credentials(request, updates, use_addon_options=True)

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
        await loop.run_in_executor(None, _save_addon_options, updates)
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
    _sync_runtime_state(request)
    config = request.app.state.config
    state = request.app.state.station_state
    caps = get_capabilities(config, state)
    result = capabilities_to_dict(caps)
    capabilities = result.setdefault("capabilities", {})
    capabilities["script_llm"] = bool(config.anthropic_api_key or config.openai_api_key)
    capabilities["anthropic_key"] = bool(config.anthropic_api_key)
    capabilities["openai"] = bool(config.openai_api_key)
    provider_health = _provider_health_snapshot(config, state)
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
    result["golden_path"] = _golden_path_status(config, state)
    result["startup_source_error"] = state.startup_source_error or ""
    result["provider_health"] = provider_health
    return result


@router.post("/api/shuffle")
async def shuffle_playlist(request: Request, _: None = Depends(require_admin_access)):
    """Shuffle upcoming tracks."""
    import random

    state = request.app.state.station_state
    random.shuffle(state.playlist)
    return {"ok": True, "message": "Playlist shuffled"}


@router.post("/api/skip")
async def skip_track(request: Request, _: None = Depends(require_admin_access)):
    """Skip the currently streaming segment."""
    state = request.app.state.station_state
    if not state.now_streaming:
        return {"ok": False, "error": "Nothing is currently streaming"}

    # Record skip in listener profile if this was a music segment
    now_seg = state.now_streaming
    if now_seg.get("type") == "music":
        started = now_seg.get("started", time.time())
        listen_sec = time.time() - started
        state.listener.record_outcome(
            skipped=True,
            listen_sec=listen_sec,
            track_display=now_seg.get("label", ""),
        )
        await _persist_skipped_music(
            state,
            request.app.state.config,
            now_seg.get("metadata") or {},
            listen_sec=listen_sec,
        )

    bridged = False
    if request.app.state.queue.empty() and not state.queued_segments:
        state.force_next = SegmentType.MUSIC
        bridged = True
        state.pending_actions.append(
            {
                "type": "skip_bridge",
                "source": "admin_skip",
                "label": "force next music",
                "created_at": time.time(),
            }
        )
        logger.info("Skip requested with empty queue — forcing next music before cut")

    request.app.state.skip_event.set()
    state.now_streaming = {"type": "skipping", "label": "Skipping...", "started": time.time(), "metadata": {}}
    return {"ok": True, "bridged": bridged}


@router.post("/api/purge")
async def purge_queue(request: Request, _: None = Depends(require_admin_access)):
    """Drain all pre-produced segments from the queue."""
    purged = _purge_segment_queue(request.app.state.queue)
    request.app.state.station_state.queued_segments.clear()
    return {"ok": True, "purged": purged}


@router.post("/api/panic")
async def panic_cut(request: Request, _: None = Depends(require_admin_access)):
    """Emergency cut: purge queue, skip current segment, force next segment to music.

    Does NOT set session_stopped — the stream stays live and listeners do not
    disconnect. Use /api/stop when a full session halt is intended.
    """
    state = request.app.state.station_state
    purged = _purge_segment_queue(request.app.state.queue)
    state.queued_segments.clear()
    if state.now_streaming:
        request.app.state.skip_event.set()
    # force_next is set AFTER skip_event to avoid the producer consuming it
    # before the current segment has been cut.
    state.force_next = SegmentType.MUSIC
    logger.warning("Panic cut triggered by admin — purged %d segments, forcing next=music", purged)
    return {"ok": True, "purged": purged}


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
    body = await request.json()
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
    # back in sync, so the producer/streamer cannot interleave.
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

    # Remove the matching Segment from the real queue. Match by queue_id when
    # available (position-independent); fall back to index alignment otherwise.
    real_removed = False
    if target_id:
        for i, seg in enumerate(items):
            if getattr(seg, "metadata", {}).get("queue_id") == target_id:
                items.pop(i)
                real_removed = True
                break
    if not real_removed and index < len(items):
        items.pop(index)

    for item in items:
        q.put_nowait(item)

    state.queued_segments.pop(index)

    logger.info("Queue item removed by admin: %s (id=%s)", removed_label, target_id or "n/a")
    return {"ok": True, "removed": removed_label}


@router.post("/api/stop")
async def stop_session(request: Request, _: None = Depends(require_admin_access)):
    """Gracefully stop the station: skip current, purge queue, cancel producer."""
    state = request.app.state.station_state
    # Purge queued segments
    purged = _purge_segment_queue(request.app.state.queue)
    state.queued_segments.clear()
    # Skip current segment
    if state.now_streaming:
        request.app.state.skip_event.set()
    # Signal producer to pause and persist across reloads
    state.session_stopped = True
    state.last_state_change_at = time.time()
    config = request.app.state.config
    _persist_session_stopped(config, True)
    state.now_streaming = {"type": "stopped", "label": "Session stopped", "started": time.time(), "metadata": {}}
    logger.info("Session stopped by admin (purged %d segments)", purged)
    return {"ok": True, "purged": purged}


@router.post("/api/resume")
async def resume_session(request: Request, _: None = Depends(require_admin_access)):
    """Resume a stopped session."""
    state = request.app.state.station_state
    config = request.app.state.config
    _clear_session_stopped(state, config)
    logger.info("Session resumed by admin")
    return {"ok": True}


@router.post("/api/trigger")
async def trigger_segment(request: Request, _: None = Depends(require_admin_access)):
    """Force the next produced segment to be banter, ad, or news flash."""
    body = await request.json()
    seg_type = body.get("type", "").lower()
    valid = {"banter": SegmentType.BANTER, "ad": SegmentType.AD, "news_flash": SegmentType.NEWS_FLASH}
    if seg_type not in valid:
        return {"ok": False, "error": f"type must be one of: {list(valid.keys())}"}

    state = request.app.state.station_state
    state.force_next = valid[seg_type]
    return {"ok": True, "triggered": seg_type}


@router.post("/api/interrupt")
async def api_interrupt(request: Request, _: None = Depends(require_admin_access)):
    """Immediately interrupt the stream and have the hosts deliver a pissed/urgent message.

    Body: {"directive": str, "urgency": str}
    - directive: what the hosts should say (required)
    - urgency: "pissed" | "urgent" | "gentle" (default: "pissed")

    Returns 429 if called within the cooldown window (default 60s).
    """
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(status_code=422, content={"ok": False, "error": "invalid JSON body"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=422, content={"ok": False, "error": "expected JSON object"})

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

    Safe to reload: prompt_world / transitions / fallbacks (prompt-fiction + stock copy)
    + scriptwriter (stateless functions + lazy-init clients). Data submodules reload FIRST
    (leaves-first) so the scriptwriter facade re-imports fresh values — reloading the facade
    alone would rebind its ``from .prompt_world`` / ``.transitions`` / ``.fallbacks`` import
    names to the stale submodules.
    NOT reloaded: producer, streamer, persona (hold live task/instance state).
    Requires --workers 1 (importlib reloads only the worker handling the request).
    """
    import mammamiradio.hosts.fallbacks as _fallbacks_mod
    import mammamiradio.hosts.prompt_world as _prompt_world_mod
    import mammamiradio.hosts.scriptwriter as _scriptwriter_mod
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
        importlib.reload(_prompt_world_mod)
        importlib.reload(_transitions_mod)
        importlib.reload(_fallbacks_mod)
        importlib.reload(_scriptwriter_mod)
        duration_ms = int((time.monotonic() - t0) * 1000)
        request.app.state._last_hot_reload_ts = now
        logger.info(
            "hot-reload: reloaded prompt_world + transitions + fallbacks + scriptwriter in %dms",
            duration_ms,
        )
        return {
            "ok": True,
            "reloaded_modules": [
                "mammamiradio.hosts.prompt_world",
                "mammamiradio.hosts.transitions",
                "mammamiradio.hosts.fallbacks",
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


@router.patch("/api/pacing")
async def update_pacing(request: Request, _: None = Depends(require_admin_access)):
    """Update pacing settings in real-time."""
    config = request.app.state.config
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Pacing payload must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Pacing payload must be a JSON object")

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

    songs_between_banter = _parse_pacing_int("songs_between_banter")
    songs_between_ads = _parse_pacing_int("songs_between_ads")
    ad_spots_per_break = _parse_pacing_int("ad_spots_per_break")

    if "songs_between_banter" in body:
        config.pacing.songs_between_banter = max(2, min(60, songs_between_banter or 0))
    if "songs_between_ads" in body:
        config.pacing.songs_between_ads = max(1, min(60, songs_between_ads or 0))
    if "ad_spots_per_break" in body:
        config.pacing.ad_spots_per_break = max(1, min(5, ad_spots_per_break or 0))
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
    """Persist super_italian_mode into /data/options.json for HA addons."""
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
    try:
        body = await request.json()
    except ValueError:
        return {"ok": False, "error": "invalid JSON body"}
    if not isinstance(body, dict) or "enabled" not in body:
        return {"ok": False, "error": "expected JSON object with enabled"}
    raw_value = body["enabled"]
    if not isinstance(raw_value, bool):
        return {"ok": False, "error": "enabled must be a JSON boolean (true/false)"}

    state = request.app.state.station_state
    config = request.app.state.config
    queue = request.app.state.queue
    value = raw_value
    env_value = "true" if value else "false"
    loop = asyncio.get_running_loop()
    purged = 0

    async with _chaos_lock:
        try:
            if config.is_addon:
                await loop.run_in_executor(None, _save_addon_option, "chaos_mode_active", value)
            else:
                await loop.run_in_executor(None, _save_dotenv, {"MAMMAMIRADIO_CHAOS_MODE": env_value})
        except Exception:
            logger.error("Failed to persist Chaos Mode toggle", exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": "failed to persist chaos mode"},
            )

        os.environ["MAMMAMIRADIO_CHAOS_MODE"] = env_value
        if value:
            first_strike = _random.choice([ChaosSubtype.FOURTH_WALL, ChaosSubtype.ABANDONED_STORM])
            state.chaos_mode_active = True
            state.chaos_pending = first_strike
            state.chaos_cutover_epoch += 1
            state.chaos_audio_failures = 0
            state.chaos_last_degraded_reason = ""
            purged = _purge_segment_queue(queue)
            state.queued_segments.clear()
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
    return {"ok": True, "enabled": value, "purged": purged}


@router.post("/api/super-italian")
async def set_super_italian(request: Request, _: None = Depends(require_admin_access)):
    """Toggle Super Italian Mode live and persist it.

    Connected listeners pick up the new copy on the next page reload (it's baked
    into listener.html via Jinja, not refetched on /public-status polls). The
    scriptwriter system-prompt cache invalidates on the mode key so the next
    banter generation uses the new directive without a restart.

    Persistence: writes `MAMMAMIRADIO_SUPER_ITALIAN` to `.env` on standalone
    deploys, and `super_italian_mode` to `/data/options.json` on HA addons —
    so the value survives container restarts in both modes.
    """
    config = request.app.state.config
    body = await request.json()
    if not isinstance(body, dict) or "super_italian_mode" not in body:
        return {"ok": False, "error": "expected JSON object with super_italian_mode"}
    raw_value = body["super_italian_mode"]
    if not isinstance(raw_value, bool):
        return {"ok": False, "error": "super_italian_mode must be a JSON boolean (true/false)"}
    value = raw_value
    env_value = "true" if value else "false"
    loop = asyncio.get_running_loop()
    async with _super_italian_lock:
        config.super_italian_mode = value
        os.environ["MAMMAMIRADIO_SUPER_ITALIAN"] = env_value
        if config.is_addon:
            await loop.run_in_executor(None, _save_addon_option, "super_italian_mode", value)
        else:
            await loop.run_in_executor(None, _save_dotenv, {"MAMMAMIRADIO_SUPER_ITALIAN": env_value})
    return {"ok": True, "super_italian_mode": value}


_party_lock = asyncio.Lock()


@router.get("/api/party")
async def get_party(request: Request, _: None = Depends(require_admin_access)):
    """Return the current party mode state."""
    config = request.app.state.config
    return {"active": config.party_mode is not None, "mode": config.party_mode}


def _save_festival_addon_options(enabled: bool) -> None:
    """Persist festival_mode into /data/options.json for HA addons."""
    _save_addon_option("festival_mode", enabled)


@router.post("/api/party")
async def set_party(request: Request, _: None = Depends(require_admin_access)):
    """Toggle Festival Mode live and persist it.

    POST {"action": "enable", "mode": "festival"} to start festival mode.
    POST {"action": "disable"} to return to normal.

    Idempotent — double-enable or double-disable returns ok without side-effects.
    """
    config = request.app.state.config
    state = request.app.state.station_state
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=422)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "expected JSON object"}, status_code=422)
    action = body.get("action")
    mode = body.get("mode")

    if action not in ("enable", "disable"):
        return JSONResponse({"ok": False, "error": "action must be 'enable' or 'disable'"}, status_code=422)
    if action == "enable" and mode != "festival":
        return JSONResponse({"ok": False, "error": "mode must be 'festival'"}, status_code=422)

    target_mode: PartyMode | None = "festival" if action == "enable" else None
    loop = asyncio.get_running_loop()
    segment_queue = request.app.state.queue

    async with _party_lock:
        if config.party_mode == target_mode:
            return {"ok": True, "active": config.party_mode is not None, "mode": config.party_mode}
        config.party_mode = target_mode
        val = "true" if target_mode == "festival" else "false"
        os.environ["MAMMAMIRADIO_FESTIVAL_MODE"] = val
        if action == "enable":
            state.playlist_revision += 1
            _purge_segment_queue(segment_queue)
            state.force_next = SegmentType.BANTER
        if config.is_addon:
            await loop.run_in_executor(None, _save_festival_addon_options, target_mode == "festival")
        else:
            await loop.run_in_executor(None, _save_dotenv, {"MAMMAMIRADIO_FESTIVAL_MODE": val})

    logger.info("Festival Mode %s by admin", "enabled" if target_mode else "disabled")
    return {"ok": True, "active": config.party_mode is not None, "mode": config.party_mode}


@router.post("/api/credentials")
async def save_credentials(request: Request, _: None = Depends(require_admin_access)):
    """Write credentials to .env and apply them live without a restart."""
    body = await request.json()
    updates = _credential_updates_from_field_payload(body)

    if not updates:
        return {"ok": False, "error": "No recognised credential fields in request"}

    await _persist_and_apply_credentials(request, updates, use_addon_options=False)

    logger.info("Credentials saved to .env: %s", ", ".join(updates.keys()))
    return {"ok": True, "saved": list(updates.keys())}


@router.post("/api/playlist/remove")
async def remove_track(request: Request, _: None = Depends(require_admin_access)):
    """Remove a track from playlist by index."""
    body = await request.json()
    idx = _as_int_index(body.get("index", -1))
    state = request.app.state.station_state
    if 0 <= idx < len(state.playlist):
        removed = state.playlist.pop(idx)
        return {"ok": True, "removed": removed.display}
    return {"ok": False, "error": "Invalid index"}


@router.post("/api/playlist/move")
async def move_track(request: Request, _: None = Depends(require_admin_access)):
    """Move a track in the playlist. body: {from: N, to: N}"""
    body = await request.json()
    src = _as_int_index(body.get("from", -1))
    dst = _as_int_index(body.get("to", -1))
    state = request.app.state.station_state
    pl = state.playlist
    if 0 <= src < len(pl) and 0 <= dst < len(pl):
        track = pl.pop(src)
        pl.insert(dst, track)
        return {"ok": True, "moved": track.display}
    return {"ok": False, "error": "Invalid indices"}


@router.get("/api/search")
async def search_tracks(request: Request, q: str = "", _: None = Depends(require_admin_access)):
    """Search the current playlist and yt-dlp for tracks matching the query."""
    from mammamiradio.playlist.downloader import search_ytdlp_metadata

    if not q.strip():
        return {"results": [], "external": []}
    query = q.strip().lower()
    state = request.app.state.station_state

    # Playlist matches (instant)
    results = []
    for i, track in enumerate(state.playlist):
        text = f"{track.title} {track.artist}".lower()
        if query in text:
            results.append(
                {
                    "index": i,
                    "title": track.title,
                    "artist": track.artist,
                    "display": track.display,
                    "duration_ms": track.duration_ms,
                    "id": track.spotify_id or track.cache_key,
                }
            )
            if len(results) >= 20:
                break

    # External yt-dlp search (blocking, run off the event loop)
    loop = asyncio.get_running_loop()
    try:
        external = await loop.run_in_executor(None, search_ytdlp_metadata, q.strip(), 5)
    except Exception:
        logger.warning("yt-dlp external search failed for query %r", q, exc_info=True)
        external = []

    return {"results": results, "external": external}


@router.post("/api/playlist/add-external")
async def add_external_track(request: Request, _: None = Depends(require_admin_access)):
    """Download a yt-dlp search result and pin it to play next."""
    from mammamiradio.core.models import Track
    from mammamiradio.playlist.downloader import download_external_track

    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid payload"}, status_code=400)
    youtube_id = str(body.get("youtube_id") or "").strip()
    title = str(body.get("title") or "").strip()
    artist = str(body.get("artist") or "").strip()
    try:
        duration_ms = int(body.get("duration_ms") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid duration_ms"}, status_code=400)
    if not youtube_id:
        return JSONResponse({"ok": False, "error": "youtube_id required"}, status_code=400)
    if not _re.fullmatch(r"[A-Za-z0-9_-]{11}", youtube_id):
        return JSONResponse({"ok": False, "error": "invalid youtube_id format"}, status_code=400)

    state = request.app.state.station_state
    config = request.app.state.config
    if not config.allow_ytdlp:
        return JSONResponse({"ok": False, "error": "external_downloads_disabled"}, status_code=409)

    track = Track(
        title=title,
        artist=artist,
        duration_ms=duration_ms,
        youtube_id=youtube_id,
    )

    # Pre-download so the cache is warm before we purge the queue.
    # Without this, the producer would hit a cache miss after the purge,
    # causing 30-60s of silence while yt-dlp downloads.
    try:
        await download_external_track(track, config.cache_dir, music_dir=Path("music"))
    except Exception:
        logger.warning("External track download failed for %s (yt:%s)", track.display, youtube_id, exc_info=True)
        return JSONResponse({"ok": False, "error": "download_failed"}, status_code=502)

    # Add to playlist pool so it's available for future cycles too
    state.playlist.append(track)

    # Pin it so the producer plays it next
    state.pinned_track = track
    state.playlist_revision += 1
    purged = _purge_segment_queue(request.app.state.queue)
    state.queued_segments.clear()
    state.force_next = SegmentType.MUSIC

    logger.info("Queued external track: %s (yt:%s)", track.display, youtube_id)
    return {"ok": True, "queued": track.display, "purged": purged}


# Listener-request endpoints + _download_listener_song background task moved to
# mammamiradio/web/listener_requests.py (Track B v2.11.0 extraction). The new
# router is mounted in main.py alongside this one.


@router.post("/api/playlist/add")
async def add_track(request: Request, _: None = Depends(require_admin_access)):
    """Add a track to the playlist."""
    from mammamiradio.core.models import Track

    body = await request.json()
    track = Track(
        title=body.get("title", ""),
        artist=body.get("artist", ""),
        duration_ms=body.get("duration_ms", 0),
        spotify_id=body.get("spotify_id", ""),
    )
    if not track.title:
        return {"ok": False, "error": "Missing title"}

    state = request.app.state.station_state
    position = body.get("position", "end")
    if position == "next":
        state.playlist.insert(0, track)
    else:
        state.playlist.append(track)
    return {"ok": True, "added": track.display, "position": position}


@router.post("/api/playlist/enrich")
async def enrich_playlist(request: Request, _: None = Depends(require_admin_access)):
    """Add tracks from a source without replacing programme or purging playback."""
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "JSON object required"}, status_code=422)
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
        try:
            tracks, resolved_source = await asyncio.to_thread(load_explicit_source, config, source)
        except ExplicitSourceError as exc:
            _msg = exc.args[0] if exc.args else "Playlist source unavailable"
            return {"ok": False, "error": _msg}
        except Exception as exc:
            logger.error("Playlist enrich failed: %s", exc)
            return {"ok": False, "error": "Failed to load playlist source"}

        seen = {track.cache_key for track in state.playlist}
        new_tracks: list[Track] = []
        for track in tracks:
            if track.cache_key in seen:
                continue
            seen.add(track.cache_key)
            new_tracks.append(track)
        if position == "next":
            state.playlist[0:0] = new_tracks
        else:
            state.playlist.extend(new_tracks)
        if new_tracks:
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
        }


@router.post("/api/playlist/load")
async def load_playlist(request: Request, _: None = Depends(require_admin_access)):
    """Load a new playlist from a URL and replace the current one."""
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        return {"ok": False, "error": "No URL provided"}
    config = request.app.state.config
    source_switch_lock = request.app.state.source_switch_lock
    source = PlaylistSource(kind="url", url=url)
    async with source_switch_lock:
        try:
            tracks, resolved_source = await asyncio.to_thread(load_explicit_source, config, source)
        except ExplicitSourceError as exc:
            _msg = exc.args[0] if exc.args else "Playlist source unavailable"
            return {"ok": False, "error": _msg}
        except Exception as exc:
            logger.error("Playlist load failed: %s", exc)
            return {"ok": False, "error": "Failed to load playlist"}

        _apply_loaded_source(request, tracks, resolved_source)
        result: dict[str, object] = {"ok": True, "tracks": len(tracks), "url": url, "persisted": True}
        try:
            await asyncio.to_thread(write_persisted_source, config.cache_dir, resolved_source)
        except Exception:
            logger.warning("Failed to persist playlist load, live switch still applied", exc_info=True)
            result["persisted"] = False
        return result


@router.post("/api/playlist/move_to_next")
async def move_to_next(request: Request, _: None = Depends(require_admin_access)):
    """Move a track to play next (position 0 in upcoming)."""
    body = await request.json()
    idx = _as_int_index(body.get("index", -1))
    state = request.app.state.station_state
    pl = state.playlist

    if 0 <= idx < len(pl):
        track = pl[idx]
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
        return {"ok": True, "moved": track.display, "to_position": 0}
    return {"ok": False, "error": "Invalid index"}


@router.post("/api/track-rules")
async def add_track_rule(request: Request, _: None = Depends(require_admin_access)):
    """Flag a reaction rule for the currently playing track."""
    from mammamiradio.playlist.track_rules import add_rule

    payload = await request.json()
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
    config = request.app.state.config
    host = next((h for h in config.hosts if h.name.lower() == host_name.lower()), None)
    if not host:
        raise HTTPException(status_code=404, detail=f"Host '{host_name}' not found")

    body = await request.json()
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
    if state.queued_segments:
        upcoming = [{**item, "source": "rendered_queue"} for item in state.queued_segments[:8]]
    else:
        upcoming = [
            {**item, "source": "predicted_from_playlist"}
            for item in preview_upcoming(state, config.pacing, state.playlist, count=8)
        ]
    # HA moments for the Casa card (public-safe, no person entity details)
    ha_moments: dict | None = None
    if state.ha_context:
        ha_moments = {
            "connected": True,
            "mood": state.ha_home_mood or None,
            "weather": state.ha_weather_arc or None,
        }
        # Event fields: only if within retention window (person filter applied in producer)
        _retention = EVENT_RETENTION_SECONDS
        _now = time.time()
        if state.ha_last_event_ts > 0 and (_now - state.ha_last_event_ts) < _retention:
            ha_moments["last_event_label"] = state.ha_last_event_label
            ha_moments["last_event_ago_min"] = max(1, round((_now - state.ha_last_event_ts) / 60))
        # Hide card if nothing interesting to show
        if not ha_moments.get("mood") and not ha_moments.get("weather") and not ha_moments.get("last_event_label"):
            ha_moments = None

    return {
        "station": config.station.name,
        "running_jokes": list(state.running_jokes),
        "now_streaming": state.now_streaming,
        "current_source": _serialize_source(state.playlist_source),
        "golden_path": _golden_path_status(config, state),
        "runtime_health": runtime_health,
        "session_stopped": state.session_stopped,
        "stream_log": [
            {"type": e.type, "label": e.label, "timestamp": e.timestamp, "metadata": e.metadata}
            for e in state.stream_log
        ],
        "upcoming": upcoming,
        "upcoming_mode": "queued" if upcoming else "building",
        "stream": {
            "frequency": config.brand.frequency,
            "bitrate_kbps": audio_format["bitrate_kbps"],
            "audio_format": audio_format,
        },
        "playback_actions": {
            "skip_ready": bool(state.now_streaming),
            "skip_would_bridge": bool(
                state.now_streaming and runtime_health.get("queue_depth", 0) == 0 and not state.queued_segments
            ),
        },
        "ha_moments": ha_moments,
        # Brand-fiction layer (PR-A schema). Listener renders against this.
        "brand": _serialize_brand(config.brand),
        # Capability flags (listener-safe subset). Listener JS reads these every
        # poll and toggles [data-cap=KEY] elements (per design D2: client-side
        # capability-conditional rendering reacts to runtime cap drift).
        "capabilities": {
            "llm": bool(config.anthropic_api_key or config.openai_api_key),
            "anthropic_key": bool(config.anthropic_api_key),
            "openai": bool(config.openai_api_key),
            "ha": bool(config.ha_token and config.homeassistant.enabled),
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


@router.post("/api/clip")
async def create_clip(request: Request):
    """Extract the last ~30s of audio into a shareable clip."""
    from mammamiradio.scheduling.clip import CLIP_TTL_SECONDS, cleanup_old_clips, extract_clip, save_clip

    # Rate limit: 1 clip per 10 seconds per IP
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    async with _clip_rate_lock:
        if now - _clip_rate.get(client_ip, 0) < CLIP_RATE_LIMIT_SECONDS:
            from fastapi.responses import JSONResponse

            return JSONResponse({"ok": False, "error": "Rate limited — try again in a few seconds"}, status_code=429)
        _clip_rate[client_ip] = now
        # Prune stale entries to avoid unbounded growth.
        stale_keys = [k for k, v in _clip_rate.items() if now - v >= CLIP_RATE_PRUNE_SECONDS]
        for key in stale_keys:
            _clip_rate.pop(key, None)

    ring_buffer = getattr(request.app.state, "clip_ring_buffer", None)
    if ring_buffer is None or len(ring_buffer) == 0:
        return {"ok": False, "error": "No audio buffered yet"}

    config = request.app.state.config
    bitrate = config.audio.bitrate if hasattr(config, "audio") else DEFAULT_CLIP_BITRATE_KBPS
    clip_data = extract_clip(ring_buffer, duration_seconds=CLIP_DURATION_SECONDS, bitrate_kbps=bitrate)
    if not clip_data:
        return {"ok": False, "error": "Buffer empty"}

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

    station_state = getattr(request.app.state, "station_state", None)
    now_streaming = getattr(station_state, "now_streaming", None) or {}
    # metadata is producer-managed and normally a dict, but a None or an
    # unexpected scalar would crash the .get() below and turn a successful
    # clip into a 500. Normalize to dict before reading fields.
    raw_meta = now_streaming.get("metadata", {}) if isinstance(now_streaming, dict) else {}
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    station_name = getattr(config.brand, "station_name", "Mamma Mi Radio")
    track_title = str(meta.get("title_only") or meta.get("title") or "").strip()
    track_artist = str(meta.get("artist") or "").strip()
    sidecar = {
        "station_name": station_name,
        "track_title": track_title,
        "track_artist": track_artist,
        "created_at": int(time.time()),
    }
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
    station_name = sidecar.get("station_name") or getattr(config.brand, "station_name", "Mamma Mi Radio")
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
    startup_complete = start_time is not None and (time.time() - start_time) > STARTUP_GRACE_SECONDS
    state = request.app.state.station_state
    queue_empty_elapsed = _queue_empty_elapsed(state)
    silence_with_listeners = _silence_with_listeners(state, queue_empty_elapsed)
    ready = (
        tasks_alive
        and (queue_depth > 0 or startup_complete)
        and not silence_with_listeners
        and not state.session_stopped
    )
    status = "ready" if ready else "starting"
    body = {
        "status": status,
        "ready": ready,
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
    """Return listener-safe station metadata and upcoming segment previews."""
    return _public_status_payload(request)


@router.get("/status")
async def status(request: Request, _: None = Depends(require_admin_access)):
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
    payload.update(
        {
            "queue_depth": segment_queue.qsize(),
            "segments_produced": state.segments_produced,
            "tracks_played": len(state.played_tracks),
            "uptime_sec": round(time.time() - start_time),
            "playlist_source": _serialize_source(state.playlist_source),
            "produced_log": [{"type": e.type, "label": e.label, "timestamp": e.timestamp} for e in state.segment_log],
            "last_banter_script": state.last_banter_script,
            "last_ad_script": state.last_ad_script,
            "ha_context": state.ha_context if state.ha_context else None,
            "ha_details": {
                "mood": state.ha_home_mood or None,
                "weather_arc": state.ha_weather_arc or None,
                "events_summary": state.ha_events_summary or None,
                "pending_directive": state.ha_pending_directive or None,
                "recent_event_count": state.ha_recent_event_count,
                "last_event_label": state.ha_last_event_label or None,
                "mood_en": state.ha_home_mood_en or None,
                "weather_arc_en": state.ha_weather_arc_en or None,
                "events_summary_en": state.ha_events_summary_en or None,
                "last_event_label_en": state.ha_last_event_label_en or None,
            }
            if state.ha_context
            else None,
            "pending_actions": list(state.pending_actions)[-10:] or None,
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
                # Haiku pricing: $0.80/M input, $4.00/M output (claude-haiku-4-5, 2026)
                "api_cost_estimate_usd": round(
                    state.api_input_tokens * 0.0000008 + state.api_output_tokens * 0.000004,
                    4,
                ),
                "cache_size_mb": _cached_cache_size_mb(config.cache_dir),
                "cache_limit_mb": config.max_cache_size_mb,
            },
            "listeners": {
                "active": state.listeners_active,
                "peak": state.listeners_peak,
                "total": state.listeners_total,
            },
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
            "session_stopped": state.session_stopped,
            "playlist": [_serialize_track(t) for t in state.playlist[:100]],
            "brand": _serialize_brand(config.brand),
            "brand_warnings": list(config.brand_warnings),
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
