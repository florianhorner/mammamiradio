"""Live streaming transport, HTTP routes, and admin controls."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re as _re
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from mammamiradio.capabilities import capabilities_to_dict, get_capabilities
from mammamiradio.models import PersonalityAxes, PlaylistSource, Segment, SegmentType, StationState
from mammamiradio.playlist import (
    ExplicitSourceError,
    list_user_playlists,
    load_explicit_source,
    supports_user_sources,
    write_persisted_source,
)
from mammamiradio.scheduler import preview_upcoming
from mammamiradio.setup_status import addon_options_snippet, build_setup_status, classify_station_mode

logger = logging.getLogger(__name__)

router = APIRouter()
security = HTTPBasic(auto_error=False)

_PKG_DIR = Path(__file__).parent
_STATIC_DIR = _PKG_DIR / "static"

_DASHBOARD_HTML = _PKG_DIR.joinpath("dashboard.html").read_text()

_LISTENER_HTML = _PKG_DIR.joinpath("listener.html").read_text()

_ADMIN_HTML = _PKG_DIR.joinpath("admin.html").read_text()

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


_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_CSRF_TOKEN_PLACEHOLDER = "__MAMMAMIRADIO_CSRF_TOKEN__"


def _purge_segment_queue(q) -> int:
    """Drain all pre-produced segments from the queue and unlink temp files."""
    purged = 0
    while not q.empty():
        try:
            seg = q.get_nowait()
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


def _golden_path_status(config, state) -> dict:
    """Build a single, explicit music onboarding status for UI surfaces."""
    spotify_api = bool(config.spotify_client_id and config.spotify_client_secret)
    spotify_connected = bool(state.spotify_connected)
    allow_ytdlp = os.getenv("MAMMAMIRADIO_ALLOW_YTDLP", "false").lower() in ("true", "1", "yes")
    has_demo_assets = _has_any_mp3(_PKG_DIR / "demo_assets" / "music")
    has_local_music = _has_any_mp3(Path("music"))

    fallback_sources: list[str] = []
    if has_demo_assets:
        fallback_sources.append("bundled demo tracks")
    if has_local_music:
        fallback_sources.append("local music/*.mp3 files")
    if allow_ytdlp:
        fallback_sources.append("yt-dlp downloads")

    silent_music_fallback = (not spotify_connected) and not fallback_sources
    shared = {
        "fallback_sources": fallback_sources,
        "silent_music_fallback": silent_music_fallback,
    }

    if spotify_connected:
        return {
            "stage": "connected",
            "blocking": False,
            "headline": "Connected to Spotify. Real music is live.",
            "detail": "Your in-page player is now using your Spotify-powered station audio.",
            "steps": [],
            **shared,
        }

    # Music is available via yt-dlp, local files, or demo assets — not blocking,
    # but only when Spotify credentials are absent. Once credentials exist, the
    # user still needs the explicit Connect/browser-login guidance.
    if fallback_sources and not spotify_connected and not spotify_api:
        source_label = ", ".join(fallback_sources)
        return {
            "stage": "music_available",
            "blocking": False,
            "headline": f"Music via {source_label}.",
            "detail": (
                f"Playing music from: {source_label}. "
                "Add Spotify credentials in Advanced Settings for streaming from your own library."
            ),
            "steps": [],
            **shared,
        }

    if not spotify_api:
        detail = "No music source available."
        if silent_music_fallback:
            detail += " Music segments fall back to silence placeholders."
        detail += " Set MAMMAMIRADIO_ALLOW_YTDLP=true or add Spotify credentials."
        return {
            "stage": "needs_music_source",
            "blocking": True,
            "headline": "No music source configured.",
            "detail": detail,
            "steps": [
                "Set MAMMAMIRADIO_ALLOW_YTDLP=true for YouTube music, or",
                "Open Advanced Settings and paste Spotify App ID and Secret.",
            ],
            **shared,
        }

    auth_url = getattr(state, "spotify_auth_url", "") or ""
    if auth_url:
        detail = (
            "Your Mac hostname causes a Spotify Connect discovery issue. "
            "Click the link below to log in via browser instead (one-time setup)."
        )
        if fallback_sources:
            detail += f" Until then, using: {', '.join(fallback_sources)}."
        return {
            "stage": "needs_spotify_browser_login",
            "blocking": True,
            "headline": "Action required: log in to Spotify via browser.",
            "detail": detail,
            "steps": [
                "Click the login link below.",
                "Log in with your Spotify account.",
                "After login, the station connects automatically.",
            ],
            "auth_url": auth_url,
            **shared,
        }

    detail = "Spotify credentials are present, but Spotify Connect is not attached yet."
    if fallback_sources:
        detail += f" Temporary fallback available: {', '.join(fallback_sources)}."
    elif silent_music_fallback:
        detail += " Current fallback is silent placeholder tracks."
    return {
        "stage": "needs_spotify_connect",
        "blocking": True,
        "headline": "Action required: connect Spotify to MammaMiRadio.",
        "detail": detail,
        "steps": [
            "Open Spotify on your phone or desktop.",
            "Tap/click the device picker (speaker icon).",
            "Select MammaMiRadio as the playback device.",
        ],
        **shared,
    }


def _sync_runtime_state(request: Request) -> None:
    """Refresh UI-facing state from long-lived runtime backends."""
    state = request.app.state.station_state
    state.runtime_sync_events += 1
    spotify_player = getattr(request.app.state, "spotify_player", None)
    if spotify_player:
        auth_url = getattr(spotify_player, "spotify_auth_url", "") or ""
        if auth_url and state.spotify_auth_url != auth_url:
            state.spotify_auth_url = auth_url

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
        "audio_source": audio_source or "unknown",
        "failover_active": bool(audio_source and audio_source.startswith("fallback")),
        "shadow_queue_corrections": state.shadow_queue_corrections,
    }


def _apply_loaded_source(
    request,
    tracks: list,
    resolved_source,
) -> dict:
    """Atomically swap the station source and trigger immediate cutover."""
    config = request.app.state.config
    state = request.app.state.station_state

    state.switch_playlist(tracks, resolved_source)

    # Synchronise URL config: only keep it for URL sources
    if resolved_source.kind == "url":
        config.playlist.spotify_url = resolved_source.url
    else:
        config.playlist.spotify_url = ""

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
    if not config.spotify_client_id or not config.spotify_client_secret:
        return "Add Spotify credentials first. Then the source picker and playlist link tools will unlock."
    return f"Spotify auth is not ready yet: {exc}"


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
    html = html.replace('href="/listen"', f'href="{prefix}/listen"')
    html = html.replace('href="/spotify/auth"', f'href="{prefix}/spotify/auth"')
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
        for _, queue in listeners:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass


_HASSIO_NETWORK = ipaddress.ip_network("172.30.32.0/23")


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


def require_admin_access(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> None:
    """Authorize admin-only routes using token, basic auth, or loopback trust."""
    config = request.app.state.config

    # Trust HA Ingress proxy — Supervisor handles authentication.
    # Trust X-Ingress-Path from loopback or the Hassio internal network
    # (172.30.32.0/23) to prevent external spoofing on the mapped port.
    ingress_prefix = request.headers.get("X-Ingress-Path", "")
    if config.is_addon and ingress_prefix and _is_hassio_or_loopback(request):
        return
    is_loopback = _is_loopback_client(request)
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

    if config.admin_password:
        client_ip = request.client.host if request.client else "unknown"
        logger.warning("Failed admin auth attempt from %s", client_ip)
        raise HTTPException(
            status_code=401,
            detail="Admin authentication required",
            headers={"WWW-Authenticate": 'Basic realm="mammamiradio admin"'},
        )

    # Token matched above would have returned; reaching here means the token was absent or wrong
    if config.admin_token:
        if is_loopback:
            return
        client_ip = request.client.host if request.client else "unknown"
        logger.warning("Missing admin token from %s", client_ip)
        raise HTTPException(
            status_code=401,
            detail="X-Radio-Admin-Token required",
        )

    if is_loopback:
        return

    raise HTTPException(
        status_code=403,
        detail="Admin endpoints are only available from localhost unless admin auth is configured",
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

    while True:
        try:
            segment: Segment = await asyncio.wait_for(segment_queue.get(), timeout=30.0)
        except TimeoutError:
            logger.warning("Queue empty for 30s, waiting...")
            continue

        state.on_stream_segment(segment)
        if state.queued_segments:
            state.queued_segments.pop(0)
        logger.info(
            ">>> NOW STREAMING %s: %s",
            segment.type.value,
            segment.metadata.get("title", segment.metadata),
        )

        try:
            send_start = time.monotonic()
            bytes_sent = 0
            skip_event.clear()
            with open(segment.path, "rb") as f:
                while chunk := f.read(chunk_size):
                    if skip_event.is_set():
                        logger.info("Skipping current segment")
                        skip_event.clear()
                        break

                    await hub.broadcast(chunk)
                    bytes_sent += len(chunk)

                    elapsed = time.monotonic() - send_start
                    expected = bytes_sent / bytes_per_sec
                    ahead = expected - elapsed
                    if ahead > 0.005:
                        await asyncio.sleep(ahead)
        finally:
            if segment.ephemeral:
                segment.path.unlink(missing_ok=True)
            segment_queue.task_done()


async def _audio_generator(request: Request):
    """Stream the live station feed from the playback loop."""
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


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_admin_access)])
async def dashboard(request: Request):
    """Serve the authenticated control-plane dashboard."""
    prefix = request.headers.get("X-Ingress-Path", "")
    html = _get_injected_html("dashboard", _DASHBOARD_HTML, prefix)
    html = _inject_csrf_token(html, _get_csrf_token(request.app))
    return html


@router.get("/admin", response_class=HTMLResponse, dependencies=[Depends(require_admin_access)])
async def admin_panel(request: Request):
    """Serve the admin control room panel."""
    prefix = request.headers.get("X-Ingress-Path", "")
    html = _get_injected_html("admin", _ADMIN_HTML, prefix)
    html = _inject_csrf_token(html, _get_csrf_token(request.app))
    return html


@router.get("/listen", response_class=HTMLResponse)
async def listener(request: Request):
    """Serve the public listener UI."""
    prefix = request.headers.get("X-Ingress-Path", "")
    return _get_injected_html("listener", _LISTENER_HTML, prefix)


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
        "icy-name": config.station.name,
        "icy-genre": config.station.theme[:64],
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
    """Return recent go-librespot + producer logs."""
    config = request.app.state.config
    return {
        "go_librespot": _tail_log(str(config.tmp_dir / "go-librespot.log"), lines),
    }


@router.get("/api/setup/status")
async def setup_status(request: Request, _: None = Depends(require_admin_access)):
    """Return the current first-run setup snapshot for onboarding."""
    config = request.app.state.config
    state = request.app.state.station_state
    return build_setup_status(config, state)


@router.post("/api/setup/recheck")
async def setup_recheck(request: Request, _: None = Depends(require_admin_access)):
    """Force a fresh setup snapshot with live Spotify probe.

    The probe makes a synchronous Spotify API call, so we run it off the
    event loop to avoid stalling the audio stream for connected listeners.
    """
    config = request.app.state.config
    state = request.app.state.station_state
    return await asyncio.to_thread(build_setup_status, config, state, probe=True)


@router.post("/api/setup/save-keys", dependencies=[Depends(require_admin_access)])
async def save_keys(request: Request):
    """Save API credentials to .env (or addon options.json) and update the live config."""
    body = await request.json()
    config = request.app.state.config

    allowed = {
        "SPOTIFY_CLIENT_ID",
        "SPOTIFY_CLIENT_SECRET",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "PLAYLIST_SPOTIFY_URL",
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
    for k, v in updates.items():
        os.environ[k] = v
    if "SPOTIFY_CLIENT_ID" in updates:
        config.spotify_client_id = updates["SPOTIFY_CLIENT_ID"]
    if "SPOTIFY_CLIENT_SECRET" in updates:
        config.spotify_client_secret = updates["SPOTIFY_CLIENT_SECRET"]
    if "ANTHROPIC_API_KEY" in updates:
        config.anthropic_api_key = updates["ANTHROPIC_API_KEY"]
    if "OPENAI_API_KEY" in updates:
        config.openai_api_key = updates["OPENAI_API_KEY"]
    if "PLAYLIST_SPOTIFY_URL" in updates:
        config.playlist.spotify_url = updates["PLAYLIST_SPOTIFY_URL"]

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
        "SPOTIFY_CLIENT_ID": "spotify_client_id",
        "SPOTIFY_CLIENT_SECRET": "spotify_client_secret",
        "ANTHROPIC_API_KEY": "anthropic_api_key",
        "OPENAI_API_KEY": "openai_api_key",
        "PLAYLIST_SPOTIFY_URL": "playlist_spotify_url",
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

    now = state.now_streaming or {}
    result["now_playing"] = now
    result["connect_status"] = "connected" if state.spotify_connected else "waiting"
    try:
        from mammamiradio.go_librespot_config import load_go_librespot_device_name

        result["connect_device_name"] = load_go_librespot_device_name(config.audio.go_librespot_config_dir)
    except Exception:
        result["connect_device_name"] = "MammaMiRadio"

    # Spotify username for welcome message.
    # Only expose when spotify_api is true — go-librespot /status username is an
    # internal device auth token, not a display name (prior learning: librespot-username-device-token).
    result["spotify_username"] = ""
    if state.spotify_connected and caps.spotify_api:
        try:
            import httpx as _httpx

            async with _httpx.AsyncClient() as _client:
                r = await _client.get(f"http://127.0.0.1:{config.audio.go_librespot_port}/status", timeout=1.0)
            if r.status_code == 200:
                result["spotify_username"] = r.json().get("username", "")
        except Exception:
            pass

    # Shareware trial state
    from mammamiradio.producer import SHAREWARE_CANNED_LIMIT

    result["trial"] = {
        "canned_clips_streamed": state.canned_clips_streamed,
        "limit": SHAREWARE_CANNED_LIMIT,
        "exhausted": state.canned_clips_streamed >= SHAREWARE_CANNED_LIMIT,
    }
    result["golden_path"] = _golden_path_status(config, state)
    result["startup_source_error"] = state.startup_source_error or ""
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

    request.app.state.skip_event.set()
    state.now_streaming = {"type": "skipping", "label": "Skipping...", "started": time.time()}
    return {"ok": True}


@router.post("/api/purge")
async def purge_queue(request: Request, _: None = Depends(require_admin_access)):
    """Drain all pre-produced segments from the queue."""
    purged = _purge_segment_queue(request.app.state.queue)
    request.app.state.station_state.queued_segments.clear()
    return {"ok": True, "purged": purged}


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
    # Signal producer to pause
    state.session_stopped = True
    state.now_streaming = {"type": "stopped", "label": "Session stopped", "started": time.time()}
    logger.info("Session stopped by admin (purged %d segments)", purged)
    return {"ok": True, "purged": purged}


@router.post("/api/resume")
async def resume_session(request: Request, _: None = Depends(require_admin_access)):
    """Resume a stopped session."""
    state = request.app.state.station_state
    state.session_stopped = False
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
        "spotify_client_id": ("SPOTIFY_CLIENT_ID", "spotify_client_id"),
        "spotify_client_secret": ("SPOTIFY_CLIENT_SECRET", "spotify_client_secret"),
        "anthropic_api_key": ("ANTHROPIC_API_KEY", "anthropic_api_key"),
        "openai_api_key": ("OPENAI_API_KEY", "openai_api_key"),
        "playlist_spotify_url": ("PLAYLIST_SPOTIFY_URL", None),
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
        else:
            # playlist_spotify_url lives on a nested object
            config.playlist.spotify_url = value

    if not updates:
        return {"ok": False, "error": "No recognised credential fields in request"}

    # Atomically update .env file (async-wrapped to avoid blocking event loop)
    def _write_env_atomic() -> None:
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
            if key in updates:
                new_lines.append(f'{key}="{updates[key]}"')
                written.add(key)
            else:
                new_lines.append(line)

        for key, value in updates.items():
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
    idx = body.get("index", -1)
    state = request.app.state.station_state
    if 0 <= idx < len(state.playlist):
        removed = state.playlist.pop(idx)
        return {"ok": True, "removed": removed.display}
    return {"ok": False, "error": "Invalid index"}


@router.post("/api/playlist/move")
async def move_track(request: Request, _: None = Depends(require_admin_access)):
    """Move a track in the playlist. body: {from: N, to: N}"""
    body = await request.json()
    src = body.get("from", -1)
    dst = body.get("to", -1)
    state = request.app.state.station_state
    pl = state.playlist
    if 0 <= src < len(pl) and 0 <= dst < len(pl):
        track = pl.pop(src)
        pl.insert(dst, track)
        return {"ok": True, "moved": track.display}
    return {"ok": False, "error": "Invalid indices"}


@router.get("/api/search")
async def search_tracks(request: Request, q: str = "", _: None = Depends(require_admin_access)):
    """Search Spotify for tracks."""
    if not q.strip():
        return {"results": []}

    from mammamiradio.spotify_auth import get_spotify_client

    config = request.app.state.config
    try:
        sp = get_spotify_client(config)
        results = sp.search(q=q, type="track", limit=8)
        tracks = []
        for t in results["tracks"]["items"]:
            artist = t["artists"][0]["name"] if t["artists"] else "Unknown"
            tracks.append(
                {
                    "title": t["name"],
                    "artist": artist,
                    "duration_ms": t["duration_ms"],
                    "spotify_id": t["id"],
                }
            )
        return {"results": tracks}
    except Exception as e:
        logger.error("Search failed: %s", e)
        return {"results": [], "error": "Search unavailable"}


@router.post("/api/playlist/add")
async def add_track(request: Request, _: None = Depends(require_admin_access)):
    """Add a track to the playlist."""
    from mammamiradio.models import Track

    body = await request.json()
    track = Track(
        title=body.get("title", ""),
        artist=body.get("artist", ""),
        duration_ms=body.get("duration_ms", 0),
        spotify_id=body.get("spotify_id", ""),
    )
    if not track.spotify_id:
        return {"ok": False, "error": "Missing spotify_id"}

    state = request.app.state.station_state
    # Insert at position (default: end, or "next" for play next)
    position = body.get("position", "end")
    if position == "next":
        state.playlist.insert(0, track)
    else:
        state.playlist.append(track)
    return {"ok": True, "added": track.display, "position": position}


@router.get("/api/spotify/source-options")
async def spotify_source_options(request: Request, _: None = Depends(require_admin_access)):
    """Return available source selection options for the current run mode."""
    config = request.app.state.config
    state = request.app.state.station_state
    capabilities = {
        "supports_user_sources": supports_user_sources(config),
        "supports_url_source": True,
        "reason": "",
    }
    if not capabilities["supports_user_sources"]:
        capabilities["reason"] = "Spotify source picker is only available in local/macOS mode right now."
        return {
            "ok": True,
            "capabilities": capabilities,
            "account": {"connected": False, "display_name": ""},
            "current_source": _serialize_source(state.playlist_source),
            "playlists": [],
            "liked_songs": {"available": False, "label": "Liked Songs"},
        }

    try:
        playlists = await asyncio.to_thread(list_user_playlists, config)
    except Exception as exc:
        return {
            "ok": False,
            "capabilities": {
                **capabilities,
                "supports_user_sources": False,
                "reason": _source_options_reason(config, exc),
            },
            "account": {"connected": False, "display_name": ""},
            "current_source": _serialize_source(state.playlist_source),
            "playlists": [],
            "liked_songs": {"available": False, "label": "Liked Songs"},
        }

    return {
        "ok": True,
        "capabilities": capabilities,
        "account": {"connected": True, "display_name": "Spotify account"},
        "current_source": _serialize_source(state.playlist_source),
        "playlists": playlists,
        "liked_songs": {"available": True, "label": "Liked Songs"},
    }


@router.post("/api/spotify/source/select")
async def spotify_source_select(request: Request, _: None = Depends(require_admin_access)):
    """Load a selected source and atomically swap the station playlist on success."""
    body = await request.json()
    kind = str(body.get("kind", "")).strip()
    source = PlaylistSource(
        kind=kind,
        source_id=str(body.get("source_id", "")).strip(),
        url=str(body.get("url", "")).strip(),
        label=str(body.get("label", "")).strip(),
    )
    if kind not in {"playlist", "liked_songs", "url"}:
        return {"ok": False, "error": "kind must be one of: playlist, liked_songs, url"}

    config = request.app.state.config
    state = request.app.state.station_state
    source_switch_lock = request.app.state.source_switch_lock

    async with source_switch_lock:
        # Server-side capability enforcement
        if kind in {"playlist", "liked_songs"} and not supports_user_sources(config):
            return {
                "ok": False,
                "error": "This run mode only supports playlist URL loading right now.",
                "current_source": _serialize_source(state.playlist_source),
            }

        try:
            tracks, resolved_source = await asyncio.to_thread(load_explicit_source, config, source)
        except ExplicitSourceError as exc:
            return {"ok": False, "error": str(exc), "current_source": _serialize_source(state.playlist_source)}
        except Exception as exc:
            logger.error("Source selection failed: %s", exc)
            return {
                "ok": False,
                "error": "Failed to load selected source",
                "current_source": _serialize_source(state.playlist_source),
            }

        result = _apply_loaded_source(request, tracks, resolved_source)
        try:
            await asyncio.to_thread(write_persisted_source, config.cache_dir, resolved_source)
        except Exception:
            logger.warning("Failed to persist source selection, live switch still applied")
        return result


@router.post("/api/playlist/load")
async def load_playlist(request: Request, _: None = Depends(require_admin_access)):
    """Load a new playlist from a Spotify URL and replace the current one."""
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
        try:
            await asyncio.to_thread(write_persisted_source, config.cache_dir, resolved_source)
        except Exception:
            logger.warning("Failed to persist playlist load, live switch still applied")
        return {"ok": True, "tracks": len(tracks), "url": url}


@router.post("/api/playlist/move_to_next")
async def move_to_next(request: Request, _: None = Depends(require_admin_access)):
    """Move a track to play next (position 0 in upcoming)."""
    body = await request.json()
    idx = body.get("index", -1)
    state = request.app.state.station_state
    pl = state.playlist

    if 0 <= idx < len(pl):
        track = pl.pop(idx)
        pl.insert(0, track)
        # Invalidate any in-flight generation, then flush buffered lookahead so
        # the reordered track really is next.
        state.playlist_revision += 1
        purged = _purge_segment_queue(request.app.state.queue)
        state.queued_segments.clear()
        state.force_next = SegmentType.MUSIC
        return {"ok": True, "moved": track.display, "to_position": 0, "purged": purged}
    return {"ok": False, "error": "Invalid index"}


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
    """Build the read-only status payload shared by public and admin APIs."""
    _sync_runtime_state(request)
    state = request.app.state.station_state
    config = request.app.state.config
    runtime_health = _runtime_health_snapshot(request)
    if state.queued_segments:
        upcoming = [{**item, "source": "rendered_queue"} for item in state.queued_segments[:5]]
    else:
        upcoming = [
            {**item, "source": "predicted_from_playlist"}
            for item in preview_upcoming(state, config.pacing, state.playlist, count=5)
        ]
    return {
        "station": config.station.name,
        "running_jokes": list(state.running_jokes),
        "now_streaming": state.now_streaming,
        "current_source": _serialize_source(state.playlist_source),
        "golden_path": _golden_path_status(config, state),
        "runtime_health": runtime_health,
        "stream_log": [
            {"type": e.type, "label": e.label, "timestamp": e.timestamp, "metadata": e.metadata}
            for e in state.stream_log
        ],
        "upcoming": upcoming,
        "upcoming_mode": "queued" if upcoming else "building",
    }


@router.get("/healthz")
async def healthz(request: Request):
    """Unauthenticated liveness probe — is the process alive?"""
    start_time = getattr(request.app.state, "start_time", None)
    uptime = round(time.time() - start_time, 1) if start_time else 0
    _sync_runtime_state(request)
    runtime = _runtime_health_snapshot(request)
    return {"status": "ok", "uptime_s": uptime, "runtime": runtime}


@router.get("/readyz")
async def readyz(request: Request):
    """Unauthenticated readiness probe — is the station ready to stream?"""
    _sync_runtime_state(request)
    runtime = _runtime_health_snapshot(request)
    start_time = getattr(request.app.state, "start_time", None)
    queue_depth = runtime["queue_depth"]
    tasks_alive = runtime["producer_task_alive"] and runtime["playback_task_alive"]
    status = "ready" if queue_depth > 0 and tasks_alive else "starting"
    return {
        "status": status,
        "queue_depth": queue_depth,
        "runtime": runtime,
        "uptime_s": round(time.time() - start_time, 1) if start_time else 0,
    }


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
            "spotify_connected": state.spotify_connected,
            "playlist_url": request.app.state.config.playlist.spotify_url or "",
            "playlist_source": _serialize_source(state.playlist_source),
            "produced_log": [{"type": e.type, "label": e.label, "timestamp": e.timestamp} for e in state.segment_log],
            "last_banter_script": state.last_banter_script,
            "last_ad_script": state.last_ad_script,
            "ha_context": state.ha_context if state.ha_context else None,
            "go_librespot_log": _tail_log(str(config.tmp_dir / "go-librespot.log"), 15),
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
            },
            "listeners": {
                "active": state.listeners_active,
                "peak": state.listeners_peak,
                "total": state.listeners_total,
            },
            "runtime_health": runtime_health,
            "force_pending": state.force_next.value if state.force_next else None,
            "session_stopped": state.session_stopped,
            "playlist": [
                {"title": t.title, "artist": t.artist, "display": t.display, "spotify_id": t.spotify_id}
                for t in state.playlist[:100]
            ],
        }
    )
    return payload


def _detect_callback_url(request: Request) -> str:
    """Build the Spotify OAuth callback URL from the current request's origin."""
    override_base = os.getenv("MAMMAMIRADIO_SPOTIFY_REDIRECT_BASE_URL", "").strip().rstrip("/")
    if override_base:
        return f"{override_base}/spotify/callback"

    ingress_prefix = _sanitize_ingress_prefix(request.headers.get("X-Ingress-Path", ""))
    scheme = request.headers.get("X-Forwarded-Proto", str(request.url.scheme))
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or "localhost:8000"
    # Spotify accepts loopback redirect URIs, but rejects plain localhost in
    # some app configurations. Canonicalize all local dev callback URLs to
    # 127.0.0.1 so the registered URI can stay stable.
    if host.startswith("localhost:"):
        host = host.replace("localhost", "127.0.0.1", 1)
    elif host == "localhost":
        host = "127.0.0.1"
    elif host.startswith("[::1]:"):
        host = host.replace("[::1]", "127.0.0.1", 1)
    elif host == "[::1]" or host == "::1":
        host = "127.0.0.1"
    return f"{scheme}://{host}{ingress_prefix}/spotify/callback"


def _read_oauth_state(tmp_dir: Path) -> tuple[str, str]:
    """Read OAuth state + callback URL from disk (survives server reload)."""
    import json

    state_file = tmp_dir / "spotify-oauth-state.json"
    try:
        data = json.loads(state_file.read_text())
        return data.get("state", ""), data.get("callback_url", "")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return "", ""


def _write_oauth_state(tmp_dir: Path, state: str, callback_url: str) -> None:
    """Persist OAuth state to disk so it survives server reload."""
    import json

    state_file = tmp_dir / "spotify-oauth-state.json"
    state_file.write_text(json.dumps({"state": state, "callback_url": callback_url}))


def _clear_oauth_state(tmp_dir: Path) -> None:
    (tmp_dir / "spotify-oauth-state.json").unlink(missing_ok=True)


@router.get("/spotify/auth", dependencies=[Depends(require_admin_access)])
async def spotify_auth_start(request: Request):
    """Start Spotify OAuth — redirects browser to Spotify authorization page."""
    from mammamiradio.spotify_auth import build_auth_url

    config = request.app.state.config
    if not config.spotify_client_id or not config.spotify_client_secret:
        raise HTTPException(400, "Configure spotify_client_id and spotify_client_secret first")

    callback_url = _detect_callback_url(request)
    state = secrets.token_urlsafe(32)

    _write_oauth_state(config.tmp_dir, state, callback_url)

    auth_url = build_auth_url(config, callback_url, state=state)
    logger.info("Spotify OAuth started, callback=%s", callback_url)
    return RedirectResponse(auth_url, status_code=302)


@router.get("/spotify/callback")
async def spotify_auth_callback(
    request: Request,
    code: str = "",
    error: str = "",
    state: str = "",
):
    """Handle Spotify OAuth callback after user authorization."""
    from urllib.parse import quote

    from mammamiradio.spotify_auth import exchange_code

    config = request.app.state.config
    prefix = _sanitize_ingress_prefix(request.headers.get("X-Ingress-Path", ""))
    dashboard = f"{prefix}/"

    if error:
        logger.warning("Spotify OAuth error from provider: %s", error)
        return RedirectResponse(f"{dashboard}?spotify=error&detail={quote(error)}", status_code=302)

    expected_state, callback_url = _read_oauth_state(config.tmp_dir)
    if not state or not expected_state or not secrets.compare_digest(state, expected_state):
        logger.warning("Spotify OAuth state mismatch (server reloaded?)")
        return RedirectResponse(f"{dashboard}?spotify=error&detail=invalid_state", status_code=302)

    _clear_oauth_state(config.tmp_dir)

    if not code or not callback_url:
        logger.warning("Spotify OAuth callback missing code or callback_url")
        return RedirectResponse(f"{dashboard}?spotify=error&detail=missing_params", status_code=302)

    success = await asyncio.to_thread(exchange_code, config, code, callback_url)
    if success:
        logger.info("Spotify OAuth completed successfully")
        return RedirectResponse(f"{dashboard}?spotify=connected", status_code=302)

    logger.error("Spotify OAuth token exchange failed")
    return RedirectResponse(f"{dashboard}?spotify=error&detail=token_exchange_failed", status_code=302)


@router.get("/api/spotify/auth-status", dependencies=[Depends(require_admin_access)])
async def spotify_auth_status(request: Request):
    """Return Spotify OAuth status for the dashboard."""
    from mammamiradio.spotify_auth import has_user_token

    config = request.app.state.config
    has_creds = bool(config.spotify_client_id and config.spotify_client_secret)
    has_token = await asyncio.to_thread(has_user_token, config) if has_creds else False
    callback_url = _detect_callback_url(request)
    return {
        "has_credentials": has_creds,
        "has_user_token": has_token,
        "callback_url": callback_url,
    }


@router.post("/api/spotify/disconnect", dependencies=[Depends(require_admin_access)])
async def spotify_disconnect(request: Request):
    """Clear cached Spotify user token."""
    from mammamiradio.spotify_auth import clear_user_token

    config = request.app.state.config
    clear_user_token(config)
    return {"ok": True}


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
