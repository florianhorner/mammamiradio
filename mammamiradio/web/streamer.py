"""Live streaming transport, HTTP routes, and admin controls."""

from __future__ import annotations

import asyncio
import importlib
import ipaddress
import logging
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
from mammamiradio.core.capabilities import capabilities_to_dict, get_capabilities
from mammamiradio.core.models import PersonalityAxes, PlaylistSource, Segment, SegmentType, StationState, Track
from mammamiradio.core.setup_status import addon_options_snippet, build_setup_status, classify_station_mode
from mammamiradio.home.ha_enrichment import EVENT_RETENTION_SECONDS
from mammamiradio.playlist.playlist import (
    ExplicitSourceError,
    load_explicit_source,
    write_persisted_source,
)
from mammamiradio.scheduling.scheduler import preview_upcoming

logger = logging.getLogger(__name__)

router = APIRouter()
security = HTTPBasic(auto_error=False)

# TODO: split — this 2,395-line god module is a postal address, not a destination.
# See docs/2026-04-28-cathedral-restructure.md (PR 5) for the routes/auth/playback split plan.
_THIS_DIR = Path(__file__).resolve().parent  # mammamiradio/web/
_PKG_ROOT = _THIS_DIR.parent  # mammamiradio/
_TEMPLATES_DIR = _THIS_DIR / "templates"
_STATIC_DIR = _THIS_DIR / "static"
_ASSETS_DIR = _PKG_ROOT / "assets"
_ASSET_VERSION = importlib.metadata.version("mammamiradio")

# Jinja2 templates for brand-engine listener page (PR-C). Admin/regia/live still use
# string-replace via _inject_ingress_prefix; only listener migrates to Jinja for now.
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _bust_static_cache(html: str) -> str:
    """Append ?v=VERSION to /static/*.css and /static/*.js URLs to bust browser cache on upgrade."""
    return _re.sub(r'(/static/[^"?]+\.(css|js))"', rf'\1?v={_ASSET_VERSION}"', html)


# Admin/regia/live pages still loaded as raw strings + post-render prefix injection.
# Listener no longer needs _LISTENER_HTML — it's rendered from template per-request.
_LISTENER_HTML = _bust_static_cache((_TEMPLATES_DIR / "listener.html").read_text())  # kept for tests + fallback

_ADMIN_HTML = _bust_static_cache((_TEMPLATES_DIR / "admin.html").read_text())
_REGIA_HTML = _bust_static_cache((_TEMPLATES_DIR / "regia.html").read_text())
_LIVE_HTML = _bust_static_cache((_TEMPLATES_DIR / "live.html").read_text())

_INGRESS_PREFIX_RE = _re.compile(r"^/[a-zA-Z0-9/_-]+$")

# Cache ingress-injected HTML to avoid repeated string replacements on every request.
# Key: (html_id, prefix) → injected HTML. Typically 1-2 entries per page.
_injected_html_cache: dict[tuple[str, str], str] = {}


def _get_injected_html(html_id: str, html: str, prefix: str) -> str:
    """Return ingress-injected HTML, cached by (page, prefix)."""
    key = (html_id, prefix)
    if key not in _injected_html_cache:
        _injected_html_cache[key] = _inject_ingress_prefix(html, prefix)
    return _injected_html_cache[key]


def _as_int_index(value, default: int = -1) -> int:
    """Best-effort parse for playlist index payload fields."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_CSRF_TOKEN_PLACEHOLDER = "__MAMMAMIRADIO_CSRF_TOKEN__"


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
    shadow_depth = len(state.queued_segments)
    now_streaming = state.now_streaming or {}
    now_metadata = now_streaming.get("metadata", {}) if isinstance(now_streaming, dict) else {}
    audio_source = now_metadata.get("audio_source", "")
    if not audio_source or audio_source == "prewarm":
        playlist_source = state.playlist_source
        if playlist_source is not None:
            audio_source = playlist_source.kind
    producer_task = getattr(request.app.state, "producer_task", None)
    playback_task = getattr(request.app.state, "playback_task", None)
    producer_alive = True if producer_task is None else not producer_task.done()
    playback_alive = True if playback_task is None else not playback_task.done()
    return {
        "queue_depth": queue_depth,
        "shadow_queue_depth": shadow_depth,
        "shadow_queue_in_sync": queue_depth == shadow_depth,
        "producer_task_alive": producer_alive,
        "playback_task_alive": playback_alive,
        "playback_epoch": state.playback_epoch,
        "queue_empty_since": state.queue_empty_since,
        "audio_source": audio_source or "unknown",
        "failover_active": bool(audio_source and audio_source.startswith("fallback")),
        "shadow_queue_corrections": state.shadow_queue_corrections,
    }


def _runtime_monotonic() -> float:
    """Monotonic clock for readiness and silence accounting."""
    return time.monotonic()


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
        },
        "openai": {
            "configured": bool(config.openai_api_key),
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


def _preview_tracks(tracks: list, limit: int = 3) -> dict:
    return {
        "track_count": len(tracks),
        "tracks": [{"title": track.title, "artist": track.artist} for track in tracks[:limit]],
    }


def _source_options_reason(config, exc: Exception) -> str:
    return f"Source loading failed: {exc}"


def _sanitize_ingress_prefix(prefix: str) -> str:
    """Validate and sanitize the X-Ingress-Path header to prevent XSS."""
    prefix = prefix.rstrip("/")
    if not prefix or not _INGRESS_PREFIX_RE.match(prefix):
        return ""
    return prefix


def _inject_ingress_prefix(html: str, prefix: str) -> str:
    """Rewrite static HTML attribute URLs to work behind HA Ingress proxy.

    Only rewrites HTML attributes (href=, src=) — JavaScript API calls use the
    client-side ``_base`` variable derived from ``window.location.pathname``,
    so JS string literals must NOT be replaced here to avoid double-prefixing.
    """
    prefix = _sanitize_ingress_prefix(prefix)
    if not prefix:
        return html
    # Only rewrite HTML attributes (double-quoted href=, src=) and standalone JS
    # paths without _base. NEVER rewrite single-quoted JS strings that use _base
    # (e.g. _base + '/api/hosts') — that causes double-prefixing.
    html = html.replace('href="/static/', f'href="{prefix}/static/')
    html = html.replace('src="/static/', f'src="{prefix}/static/')
    html = html.replace('href="/listen"', f'href="{prefix}/listen"')
    html = html.replace('href="/dashboard"', f'href="{prefix}/dashboard"')
    html = html.replace('href="/admin"', f'href="{prefix}/admin"')
    html = html.replace('href="/live"', f'href="{prefix}/live"')
    html = html.replace('src="/stream"', f'src="{prefix}/stream"')
    # Service worker registration is standalone (no _base), needs rewriting
    html = html.replace("'/sw.js'", f"'{prefix}/sw.js'")
    return html


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
    """Authorize admin-only routes using private network trust, token, or basic auth.

    Trust hierarchy (first match wins):
    1. Private network (LAN, Tailscale, HA Supervisor) — trusted for reads,
       CSRF-checked for writes
    2. Admin token (header only)
    3. Basic auth (username/password)
    4. Reject
    """
    config = request.app.state.config

    # Loopback is fully trusted — same machine, no CSRF risk.
    if _is_loopback_client(request):
        return

    # HA Supervisor network is Docker-internal (not user-accessible), so
    # CSRF from a browser on that network is not a real threat. Fully trust
    # it in addon mode so HA automations (rest_command, etc.) work without tokens.
    if config.is_addon and _is_hassio_or_loopback(request):
        return

    # Other private networks (LAN, Tailscale): trusted for identity but
    # CSRF-checked on writes to prevent cross-site POSTs from other browsers.
    if _is_private_network(request):
        _enforce_csrf_for_private_network(request)
        return

    # Explicit auth for public/remote access.
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

    raise HTTPException(
        status_code=403,
        detail="Admin endpoints are only available from private networks unless admin auth is configured",
    )


_MPEG1_L3_BITRATES_KBPS = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_MPEG1_SAMPLE_RATES = (44100, 48000, 32000)


def _is_mpeg1_l3_header(frame_header: bytes, *, allow_free_bitrate: bool) -> bool:
    """Return whether ``frame_header`` is a plausible MPEG-1 Layer III frame."""
    if len(frame_header) < 4 or frame_header[0] != 0xFF or (frame_header[1] & 0xE0) != 0xE0:
        return False

    version = (frame_header[1] >> 3) & 0x03
    layer = (frame_header[1] >> 1) & 0x03
    bitrate_idx = (frame_header[2] >> 4) & 0x0F
    sample_rate_idx = (frame_header[2] >> 2) & 0x03

    if version != 3 or layer != 1 or sample_rate_idx == 3 or bitrate_idx == 0x0F:
        return False
    return not (not allow_free_bitrate and bitrate_idx == 0)


def _skip_id3_and_xing_header(f) -> None:
    """Advance the file pointer past any leading ID3v2 tag and Xing/Info metadata frame.

    Safari's ``<audio>`` element honors the Xing/Info duration header of each
    concatenated segment as end-of-track, causing short segments (banter ~9 s,
    news flash ~6 s) to fire ``ended`` at the declared duration instead of
    playing through the ongoing stream. Long music segments (180 s+) don't
    trip this because the listener tops up buffered bytes before the counter
    expires. Stripping the tag on every segment makes the stream look like a
    continuous ICECast feed, which all browsers handle correctly.

    The helper is defensive: any unexpected header shape rewinds to the start,
    so the worst case is "did nothing" rather than "cut a real audio frame".
    """
    header = f.read(10)
    if len(header) == 10 and header[:3] == b"ID3":
        size = ((header[6] & 0x7F) << 21) | ((header[7] & 0x7F) << 14) | ((header[8] & 0x7F) << 7) | (header[9] & 0x7F)
        f.seek(10 + size)
    else:
        f.seek(0)

    frame_start = f.tell()
    frame_header = f.read(4)
    if not _is_mpeg1_l3_header(frame_header, allow_free_bitrate=True):
        f.seek(frame_start)
        return

    bitrate_idx = (frame_header[2] >> 4) & 0x0F
    sample_rate_idx = (frame_header[2] >> 2) & 0x03
    padding = (frame_header[2] >> 1) & 0x01
    channel_mode = (frame_header[3] >> 6) & 0x03

    magic_offset = 21 if channel_mode == 3 else 36
    f.seek(frame_start + magic_offset)
    magic = f.read(4)
    if magic not in (b"Xing", b"Info"):
        f.seek(frame_start)
        return

    if bitrate_idx == 0:
        # VBR info frame (free-format): frame_length is unknown from the header alone.
        # Scan forward from just after the Xing magic and only accept plausible
        # MPEG-1 Layer III headers so sync-like metadata bytes are ignored.
        f.seek(frame_start + magic_offset + 4)
        data = f.read(8192)
        sync_pos = -1
        for i in range(len(data) - 3):
            if _is_mpeg1_l3_header(data[i : i + 4], allow_free_bitrate=False):
                sync_pos = i
                break
        if sync_pos >= 0:
            f.seek(frame_start + magic_offset + 4 + sync_pos)
        else:
            f.seek(frame_start)
        return

    bitrate_kbps = _MPEG1_L3_BITRATES_KBPS[bitrate_idx]
    sample_rate = _MPEG1_SAMPLE_RATES[sample_rate_idx]
    frame_length = (144 * bitrate_kbps * 1000 // sample_rate) + padding
    f.seek(frame_start + frame_length)


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

    while True:
        # Pause when nobody is listening — don't burn API tokens or disk on an empty room.
        # The queue stays full; the moment a listener connects, playback resumes instantly.
        if not hub._listeners:
            state.queue_empty_since = None
            await asyncio.sleep(1.0)
            continue

        pulled_from_queue = False
        if segment_queue.empty() and state.queue_empty_since is None:
            # Mark the exact moment playback ran out of audio. The 30s wait_for()
            # below is part of the listener-visible silence window.
            state.queue_empty_since = _runtime_monotonic()
        try:
            segment: Segment = await asyncio.wait_for(segment_queue.get(), timeout=30.0)
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
                if elapsed >= 30.0:
                    norm_files = sorted(config.cache_dir.glob("norm_*.mp3"))
                    if norm_files:
                        rescue = norm_files[0]
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

        state.on_stream_segment(segment)
        if pulled_from_queue and state.queued_segments:
            state.queued_segments.pop(0)
        logger.info(
            ">>> NOW STREAMING %s: %s",
            segment.type.value,
            segment.metadata.get("title", segment.metadata),
        )

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
                # segment — on Pi, the SQLite writes can take long enough to cause
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


async def _audio_generator(request: Request):
    """Stream the live station feed from the playback loop."""
    state = request.app.state.station_state
    config = request.app.state.config
    if state.session_stopped:
        state.session_stopped = False
        state.resume_event.set()
        (config.cache_dir / "session_stopped.flag").unlink(missing_ok=True)

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
        {
            "brand": config.brand,
            "ingress_prefix": _sanitize_ingress_prefix(prefix),
            "csrf_token": _get_csrf_token(request.app),
            "asset_version": _ASSET_VERSION,
        },
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


@router.get("/regia", response_class=HTMLResponse, dependencies=[Depends(require_admin_access)])
async def regia_prototype(request: Request):
    """Serve the Regia Screen 1 (ON AIR) prototype — Concept A Time-Horizon Stack MVP."""
    prefix = request.headers.get("X-Ingress-Path", "")
    html = _get_injected_html("regia", _REGIA_HTML, prefix)
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
        {
            "brand": config.brand,
            "ingress_prefix": _sanitize_ingress_prefix(prefix),
            "csrf_token": _get_csrf_token(request.app),
            "asset_version": _ASSET_VERSION,
        },
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
    headers = {
        "Content-Type": "audio/mpeg",
        "icy-name": config.station.name.replace("\r", "").replace("\n", ""),
        "icy-genre": config.station.theme[:64].replace("\r", "").replace("\n", ""),
        "icy-br": str(config.audio.bitrate),
        "Cache-Control": "no-cache, no-store",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        _audio_generator(request),
        headers=headers,
        media_type="audio/mpeg",
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


@router.post("/api/setup/save-keys", dependencies=[Depends(require_admin_access)])
async def save_keys(request: Request):
    """Save API credentials to .env (or addon options.json) and update the live config."""
    body = await request.json()
    config = request.app.state.config

    allowed = {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    }
    updates = {k: v.strip() for k, v in body.items() if k in allowed and isinstance(v, str) and v.strip()}

    if not updates:
        return {"ok": False, "error": "No keys provided"}

    # Persist to disk (async-wrapped to avoid blocking the event loop)
    loop = asyncio.get_running_loop()
    if config.is_addon:
        await loop.run_in_executor(None, _save_addon_options, updates)
    else:
        await loop.run_in_executor(None, _save_dotenv, updates)

    # Update env + live config so re-check sees the values immediately
    state = request.app.state.station_state
    for k, v in updates.items():
        os.environ[k] = v
    if "ANTHROPIC_API_KEY" in updates:
        config.anthropic_api_key = updates["ANTHROPIC_API_KEY"]
        from mammamiradio.hosts.scriptwriter import reset_provider_backoff

        reset_provider_backoff()
        state.anthropic_disabled_until = 0.0
        state.anthropic_last_error = ""
    if "OPENAI_API_KEY" in updates:
        config.openai_api_key = updates["OPENAI_API_KEY"]

    return {"ok": True, "saved": list(updates.keys())}


def _save_dotenv(updates: dict[str, str]) -> None:
    """Write key=value pairs to .env, updating existing keys or appending new ones."""
    env_path = Path(".env")
    lines = env_path.read_text().splitlines() if env_path.exists() else []

    # Sanitize values: strip newlines to prevent env injection
    safe_updates = {k: v.replace("\n", "").replace("\r", "") for k, v in updates.items()}

    written = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in safe_updates:
                new_lines.append(f'{key}="{safe_updates[key]}"')
                written.add(key)
                continue
        new_lines.append(line)

    for key, value in safe_updates.items():
        if key not in written:
            new_lines.append(f'{key}="{value}"')

    env_path.write_text("\n".join(new_lines) + "\n")


def _save_addon_options(updates: dict[str, str]) -> None:
    """Update /data/options.json with new credential values."""
    import json as _json

    options_path = Path("/data/options.json")
    options = {}
    if options_path.exists():
        try:
            options = _json.loads(options_path.read_text())
        except (ValueError, OSError):
            pass

    key_map = {
        "ANTHROPIC_API_KEY": "anthropic_api_key",
        "OPENAI_API_KEY": "openai_api_key",
    }
    for env_key, value in updates.items():
        opt_key = key_map.get(env_key)
        if opt_key:
            options[opt_key] = value

    options_path.write_text(_json.dumps(options, indent=2))


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

    request.app.state.skip_event.set()
    state.now_streaming = {"type": "skipping", "label": "Skipping...", "started": time.time(), "metadata": {}}
    return {"ok": True}


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
    """Remove a single pre-produced segment from the queue by shadow-list index.

    Drains the asyncio.Queue, removes the item at the given index, then re-pushes
    the remaining segments. The queue is empty for ~1ms during the operation; the
    streamer's 30-second empty-queue countdown resets as soon as items land back.
    """
    body = await request.json()
    index = body.get("index")
    if not isinstance(index, int):
        raise HTTPException(status_code=422, detail="index must be an integer")

    state = request.app.state.station_state
    q = request.app.state.queue

    if not state.queued_segments:
        return {"ok": True, "removed": None}

    if index < 0 or index >= len(state.queued_segments):
        raise HTTPException(
            status_code=422,
            detail=f"index {index} out of range (queue has {len(state.queued_segments)} items)",
        )

    removed_label = state.queued_segments[index].get("label", "unknown")

    # Drain the asyncio.Queue into a list, remove item N, re-push rest.
    items: list = []
    while not q.empty():
        try:
            items.append(q.get_nowait())
        except asyncio.QueueEmpty:
            break

    if index < len(items):
        items.pop(index)

    for item in items:
        await q.put(item)

    state.queued_segments.pop(index)

    logger.info("Queue item %d removed by admin: %s", index, removed_label)
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
    config = request.app.state.config
    config.cache_dir.mkdir(parents=True, exist_ok=True)
    (config.cache_dir / "session_stopped.flag").touch()
    state.now_streaming = {"type": "stopped", "label": "Session stopped", "started": time.time(), "metadata": {}}
    logger.info("Session stopped by admin (purged %d segments)", purged)
    return {"ok": True, "purged": purged}


@router.post("/api/resume")
async def resume_session(request: Request, _: None = Depends(require_admin_access)):
    """Resume a stopped session."""
    state = request.app.state.station_state
    state.session_stopped = False
    state.resume_event.set()
    config = request.app.state.config
    (config.cache_dir / "session_stopped.flag").unlink(missing_ok=True)
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


@router.post("/api/hot-reload")
async def hot_reload_modules(request: Request, _: None = Depends(require_admin_access)):
    """Reload scriptwriter module in-place. Stream continues uninterrupted.

    Safe to reload: scriptwriter (stateless functions + lazy-init clients).
    NOT reloaded: producer, streamer, persona (hold live task/instance state).
    Requires --workers 1 (importlib reloads only the worker handling the request).
    """
    import mammamiradio.hosts.scriptwriter as _scriptwriter_mod

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
        importlib.reload(_scriptwriter_mod)
        duration_ms = int((time.monotonic() - t0) * 1000)
        request.app.state._last_hot_reload_ts = now
        logger.info("hot-reload: reloaded mammamiradio.hosts.scriptwriter in %dms", duration_ms)
        return {
            "ok": True,
            "reloaded_modules": ["mammamiradio.hosts.scriptwriter"],
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
    body = await request.json()
    if "songs_between_banter" in body:
        config.pacing.songs_between_banter = max(1, int(body["songs_between_banter"]))
    if "songs_between_ads" in body:
        config.pacing.songs_between_ads = max(1, int(body["songs_between_ads"]))
    if "ad_spots_per_break" in body:
        config.pacing.ad_spots_per_break = max(1, min(5, int(body["ad_spots_per_break"])))
    return {
        "ok": True,
        "songs_between_banter": config.pacing.songs_between_banter,
        "songs_between_ads": config.pacing.songs_between_ads,
        "ad_spots_per_break": config.pacing.ad_spots_per_break,
    }


@router.post("/api/credentials")
async def save_credentials(request: Request, _: None = Depends(require_admin_access)):
    """Write credentials to .env and apply them live without a restart."""
    body = await request.json()
    config = request.app.state.config

    # Allowed keys and their mapping to env var name and live config attribute
    allowed: dict[str, tuple[str, str | None]] = {
        "anthropic_api_key": ("ANTHROPIC_API_KEY", "anthropic_api_key"),
        "openai_api_key": ("OPENAI_API_KEY", "openai_api_key"),
    }

    updates: dict[str, str] = {}
    for field, (env_key, config_attr) in allowed.items():
        if field not in body:
            continue
        value = str(body[field]).strip()
        updates[env_key] = value
        os.environ[env_key] = value
        if config_attr:
            setattr(config, config_attr, value)

    if not updates:
        return {"ok": False, "error": "No recognised credential fields in request"}

    # Atomically update .env file (async-wrapped to avoid blocking event loop)
    def _write_env_atomic() -> None:
        # Sanitize values: strip newlines to prevent env injection (same as _save_dotenv)
        safe = {k: v.replace("\n", "").replace("\r", "") for k, v in updates.items()}
        env_path = Path(".env")
        try:
            existing = env_path.read_text() if env_path.exists() else ""
        except OSError:
            existing = ""

        lines = existing.splitlines()
        written: set[str] = set()
        new_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                new_lines.append(line)
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in safe:
                new_lines.append(f'{key}="{safe[key]}"')
                written.add(key)
            else:
                new_lines.append(line)

        for key, value in safe.items():
            if key not in written:
                new_lines.append(f'{key}="{value}"')

        tmp = env_path.with_suffix(".env.tmp")
        tmp.write_text("\n".join(new_lines) + "\n")
        tmp.replace(env_path)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _write_env_atomic)

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


@router.get("/api/listener-requests")
async def get_listener_requests(request: Request, _: None = Depends(require_admin_access)):
    """Return current pending listener request queue (admin only)."""
    state = request.app.state.station_state
    now = time.time()
    return {
        "requests": [
            {
                "id": str(r.get("ts", "")),
                "name": r.get("name"),
                "message": r.get("message"),
                "type": r.get("type"),
                "song_found": r.get("song_found"),
                "song_error": r.get("song_error"),
                "song_track": r.get("song_track"),
                "age_s": int(now - r.get("ts", now)),
            }
            for r in state.pending_requests
        ]
    }


@router.get("/public-listener-requests")
async def get_public_listener_requests(request: Request):
    """Listener-safe view of recent dediche / requests.

    Filtered for public consumption: drops internal IDs, error fields, and
    raw timestamps. Listener page renders this as the Dediche feed. No auth
    (public endpoint). Sensitive fields (song_error, ts as ID) stay admin-only.
    """
    state = request.app.state.station_state
    now = time.time()
    return {
        "requests": [
            {
                "name": r.get("name", ""),
                "message": r.get("message", ""),
                "type": r.get("type", "dedica"),
                "song_track": r.get("song_track") if r.get("song_found") else None,
                "age_s": int(now - r.get("ts", now)),
            }
            for r in state.pending_requests
        ]
    }


@router.post("/api/listener-requests/dismiss")
async def dismiss_listener_request(request: Request, _: None = Depends(require_admin_access)):
    """Remove a specific listener request from the queue by id (admin only)."""
    state = request.app.state.station_state
    body = await request.json()
    req_id = str(body.get("id", ""))
    if not req_id:
        return JSONResponse({"ok": False, "error": "id required"}, status_code=400)
    before = len(state.pending_requests)
    state.pending_requests = [r for r in state.pending_requests if str(r.get("ts", "")) != req_id]
    removed = before - len(state.pending_requests)
    return {"ok": True, "removed": removed}


@router.post("/api/listener-request")
async def listener_request(request: Request):
    """Accept a listener shoutout or song wish. Public endpoint, IP rate-limited."""
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid payload"}, status_code=400)
    raw_name = body.get("name")
    raw_message = body.get("message")
    if raw_name is not None and not isinstance(raw_name, str):
        return JSONResponse({"ok": False, "error": "name must be a string"}, status_code=400)
    if raw_message is not None and not isinstance(raw_message, str):
        return JSONResponse({"ok": False, "error": "message must be a string"}, status_code=400)
    name = (raw_name or "Un ascoltatore").strip()[:60]
    message = (raw_message or "").strip()[:200]
    if not message:
        return JSONResponse({"ok": False, "error": "message required"}, status_code=400)

    # IP rate limit: 1 request per 30s per IP
    ip = request.client.host if request.client else "unknown"
    state = request.app.state.station_state
    now = time.time()
    last = state._listener_request_rl.get(ip, 0)
    if now - last < 30:
        return JSONResponse({"ok": False, "retry_after": int(30 - (now - last))}, status_code=429)
    state._listener_request_rl[ip] = now
    # Prune stale entries to avoid unbounded growth
    state._listener_request_rl = {k: v for k, v in state._listener_request_rl.items() if now - v < 300}

    # Cap queue
    if len(state.pending_requests) >= 10:
        return JSONResponse({"ok": False, "error": "queue_full"}, status_code=429)

    # Detect song request by keyword
    msg_lower = message.lower()
    song_keywords = ["metti", "suona", "play", "voglio sentire", "puoi mettere", "can you play", "mettete"]
    config = request.app.state.config
    allow_ytdlp = getattr(config, "allow_ytdlp", False)
    is_song_request = allow_ytdlp and any(kw in msg_lower for kw in song_keywords)
    req: dict = {
        "name": name,
        "message": message,
        "type": "song_request" if is_song_request else "shoutout",
        "song_query": message if is_song_request else None,
        "song_found": False,
        "song_error": False,
        "song_track": None,
        "banter_cycles_missed": 0,
        "ts": now,
    }
    state.pending_requests.append(req)

    # Fire async download for song requests
    if is_song_request:
        _dl_task = asyncio.create_task(_download_listener_song(req, request.app.state, state.playlist_revision))
        request.app.state.background_tasks = getattr(request.app.state, "background_tasks", set())
        request.app.state.background_tasks.add(_dl_task)
        _dl_task.add_done_callback(request.app.state.background_tasks.discard)

    logger.info("Listener request queued from %s: %s (%s)", name, message[:40], req["type"])
    return {"ok": True, "queued": True, "type": req["type"]}


async def _download_listener_song(req: dict, app_state, originating_revision: int) -> None:
    """Background task: search yt-dlp for a listener song request and pin it.

    Stream-safe: does NOT purge the pre-buffered queue.  The pinned track
    enters the queue naturally after the current lookahead drains, avoiding
    any audible silence gap.  If the playlist source changed while downloading
    (revision mismatch) or the request was already consumed, the track is
    dropped entirely to prevent leaking old requests into the new source.
    """
    from mammamiradio.core.models import Track
    from mammamiradio.playlist.downloader import download_external_track, search_ytdlp_metadata

    state = app_state.station_state
    config = app_state.config
    query = req.get("song_query") or req.get("message") or ""
    try:
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, search_ytdlp_metadata, query, 1)
        if not results:
            req["song_error"] = True
            logger.info("Listener song request: no yt-dlp results for %r", query)
            return
        meta = results[0]
        track = Track(
            title=meta["title"],
            artist=meta["artist"],
            duration_ms=meta["duration_ms"],
            youtube_id=meta["youtube_id"],
        )
        # Download so it's ready when the producer picks it up
        await download_external_track(track, config.cache_dir, music_dir=Path("music"))

        # Guard: if the playlist source switched while we were downloading,
        # drop the result entirely.  Adding it to the new playlist would embed
        # an old listener wish in a freshly loaded source.
        if state.playlist_revision != originating_revision or req not in state.pending_requests:
            logger.info("Listener song downloaded but playlist changed or request consumed: %s", track.display)
            return

        state.playlist.append(track)
        req["song_found"] = True
        req["song_track"] = track.display
        req["song_track_obj"] = track
        if state.pending_requests and state.pending_requests[0] is req:
            state.pinned_track = track
            state.force_next = SegmentType.MUSIC
        logger.info("Listener song request ready: %s", track.display)
    except Exception:
        req["song_error"] = True
        logger.warning("Listener song download failed for %r", query, exc_info=True)


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
            return {"ok": False, "error": str(exc)}
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
    tests/test_public_status_contract.py.
    """
    _sync_runtime_state(request)
    state = request.app.state.station_state
    config = request.app.state.config
    runtime_health = _runtime_health_snapshot(request)
    start_time = getattr(request.app.state, "start_time", None) or 0
    uptime_sec = round(time.time() - start_time) if start_time else 0
    if state.queued_segments:
        upcoming = [{**item, "source": "rendered_queue"} for item in state.queued_segments[:5]]
    else:
        upcoming = [
            {**item, "source": "predicted_from_playlist"}
            for item in preview_upcoming(state, config.pacing, state.playlist, count=5)
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
        "ha_moments": ha_moments,
        # Brand-fiction layer (PR-A schema). Listener renders against this.
        "brand": {
            "station_name": config.brand.station_name,
            "frequency": config.brand.frequency,
            "city": config.brand.city,
            "founded": config.brand.founded,
            "tagline": config.brand.tagline,
            "about": config.brand.about,
            "opengraph_subtitle": config.brand.opengraph_subtitle,
            "hosts": [
                {"engine_host": h.engine_host, "display_name": h.display_name, "description": h.description}
                for h in config.brand.hosts
            ],
            "theme": {
                "primary_color": config.brand.theme.primary_color,
                "accent_color": config.brand.theme.accent_color,
                "background_color": config.brand.theme.background_color,
                "display_font": config.brand.theme.display_font,
                "body_font": config.brand.theme.body_font,
                "mono_font": config.brand.theme.mono_font,
            },
        },
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
        if now - _clip_rate.get(client_ip, 0) < 10:
            from fastapi.responses import JSONResponse

            return JSONResponse({"ok": False, "error": "Rate limited — try again in a few seconds"}, status_code=429)
        _clip_rate[client_ip] = now
        # Prune stale entries to avoid unbounded growth.
        stale_keys = [k for k, v in _clip_rate.items() if now - v >= 300]
        for key in stale_keys:
            _clip_rate.pop(key, None)

    ring_buffer = getattr(request.app.state, "clip_ring_buffer", None)
    if ring_buffer is None or len(ring_buffer) == 0:
        return {"ok": False, "error": "No audio buffered yet"}

    config = request.app.state.config
    bitrate = config.audio.bitrate if hasattr(config, "audio") else 192
    clip_data = extract_clip(ring_buffer, duration_seconds=30, bitrate_kbps=bitrate)
    if not clip_data:
        return {"ok": False, "error": "Buffer empty"}

    clips_dir = config.cache_dir / "clips"

    # Cap total clips on disk to prevent unbounded writes
    existing = sorted(clips_dir.glob("*.mp3"), key=lambda f: f.stat().st_mtime) if clips_dir.is_dir() else []
    if len(existing) >= 50:
        for old in existing[: len(existing) - 49]:
            old.unlink(missing_ok=True)

    clip_id = save_clip(clip_data, clips_dir)
    cleanup_old_clips(clips_dir, max_age_hours=CLIP_TTL_SECONDS // 3600)
    return {"ok": True, "clip_id": clip_id, "url": f"/clips/{clip_id}.mp3"}


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


@router.get("/healthz")
async def healthz(request: Request):
    """Unauthenticated liveness probe — alive AND not silently failing with listeners."""
    start_time = getattr(request.app.state, "start_time", None)
    uptime = round(time.time() - start_time, 1) if start_time else 0
    _sync_runtime_state(request)
    runtime = _runtime_health_snapshot(request)
    state = request.app.state.station_state
    queue_empty_elapsed = _runtime_monotonic() - state.queue_empty_since if state.queue_empty_since is not None else 0.0
    silence_with_listeners = queue_empty_elapsed > 30.0 and state.listeners_active > 0
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
    startup_complete = start_time is not None and (time.time() - start_time) > 30
    state = request.app.state.station_state
    queue_empty_elapsed = _runtime_monotonic() - state.queue_empty_since if state.queue_empty_since is not None else 0.0
    silence_with_listeners = queue_empty_elapsed > 30.0 and state.listeners_active > 0
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
            "provider_health": _provider_health_snapshot(config, state),
            "force_pending": state.force_next.value if state.force_next else None,
            "session_stopped": state.session_stopped,
            "playlist": [
                {"title": t.title, "artist": t.artist, "display": t.display, "spotify_id": t.spotify_id}
                for t in state.playlist[:100]
            ],
            "brand": {
                "station_name": config.brand.station_name,
                "frequency": config.brand.frequency,
                "city": config.brand.city,
                "founded": config.brand.founded,
                "tagline": config.brand.tagline,
                "about": config.brand.about,
                "opengraph_subtitle": config.brand.opengraph_subtitle,
                "hosts": [
                    {"engine_host": h.engine_host, "display_name": h.display_name, "description": h.description}
                    for h in config.brand.hosts
                ],
                "theme": {
                    "primary_color": config.brand.theme.primary_color,
                    "accent_color": config.brand.theme.accent_color,
                    "background_color": config.brand.theme.background_color,
                    "display_font": config.brand.theme.display_font,
                    "body_font": config.brand.theme.body_font,
                    "mono_font": config.brand.theme.mono_font,
                },
            },
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
