"""Live streaming transport, HTTP routes, and admin controls."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from mammamiradio.models import PersonalityAxes, Segment
from mammamiradio.scheduler import preview_upcoming

logger = logging.getLogger(__name__)

router = APIRouter()
security = HTTPBasic(auto_error=False)

_DASHBOARD_HTML = __import__("pathlib").Path(__file__).with_name("dashboard.html").read_text()

_LISTENER_HTML = __import__("pathlib").Path(__file__).with_name("listener.html").read_text()


import re as _re

_INGRESS_PREFIX_RE = _re.compile(r"^/[a-zA-Z0-9/_-]+$")


def _sanitize_ingress_prefix(prefix: str) -> str:
    """Validate and sanitize the X-Ingress-Path header to prevent XSS."""
    prefix = prefix.rstrip("/")
    if not prefix or not _INGRESS_PREFIX_RE.match(prefix):
        return ""
    return prefix


def _inject_ingress_prefix(html: str, prefix: str) -> str:
    """Rewrite absolute URL references in HTML to work behind HA Ingress proxy.

    The /api/ replacement must run first — if it runs after /stream or /status,
    those replacements create strings containing '/api/...' (from the ingress
    prefix itself) which then get double-replaced.
    """
    prefix = _sanitize_ingress_prefix(prefix)
    if not prefix:
        return html
    # Prefix-match rules first (to avoid double-replacing specific patterns)
    html = html.replace("'/api/", f"'{prefix}/api/")
    # Exact-match rules (these won't cascade because they match full quoted strings)
    html = html.replace("'/stream'", f"'{prefix}/stream'")
    html = html.replace("'/status'", f"'{prefix}/status'")
    html = html.replace("'/public-status'", f"'{prefix}/public-status'")
    html = html.replace('"/listen"', f'"{prefix}/listen"')
    html = html.replace('href="/listen"', f'href="{prefix}/listen"')
    html = html.replace('src="/stream"', f'src="{prefix}/stream"')
    return html


class LiveStreamHub:
    """Fan out live audio chunks to all connected listener streams."""

    def __init__(self, listener_queue_size: int = 128):
        self._listener_queue_size = listener_queue_size
        self._listeners: dict[int, asyncio.Queue[bytes | None]] = {}
        self._next_listener_id = 0

    def subscribe(self) -> tuple[int, asyncio.Queue[bytes | None]]:
        """Register a listener and return its dedicated chunk queue."""
        listener_id = self._next_listener_id
        self._next_listener_id += 1
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=self._listener_queue_size)
        self._listeners[listener_id] = queue
        logger.info("Listener connected (%d active)", len(self._listeners))
        return listener_id, queue

    def unsubscribe(self, listener_id: int) -> None:
        """Remove a listener and drop any future broadcast work for it."""
        if self._listeners.pop(listener_id, None) is not None:
            logger.info("Listener disconnected (%d active)", len(self._listeners))

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
            return

    if config.admin_password:
        raise HTTPException(
            status_code=401,
            detail="Admin authentication required",
            headers={"WWW-Authenticate": 'Basic realm="mammamiradio admin"'},
        )

    if config.admin_token:
        if is_loopback:
            return
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
    bytes_per_sec = (config.audio.bitrate * 1000) / 8

    while True:
        try:
            segment: Segment = await asyncio.wait_for(segment_queue.get(), timeout=30.0)
        except TimeoutError:
            logger.warning("Queue empty for 30s, waiting...")
            continue

        state.on_stream_segment(segment)
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
                    if ahead > 0.01:
                        await asyncio.sleep(ahead)
                    else:
                        await asyncio.sleep(0)
        finally:
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
    return _inject_ingress_prefix(_DASHBOARD_HTML, prefix)


@router.get("/listen", response_class=HTMLResponse)
async def listener(request: Request):
    """Serve the public listener UI."""
    prefix = request.headers.get("X-Ingress-Path", "")
    return _inject_ingress_prefix(_LISTENER_HTML, prefix)


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

    request.app.state.skip_event.set()
    state.now_streaming = {"type": "skipping", "label": "Skipping...", "started": time.time()}
    return {"ok": True}


@router.post("/api/purge")
async def purge_queue(request: Request, _: None = Depends(require_admin_access)):
    """Drain all pre-produced segments from the queue."""
    q = request.app.state.queue
    purged = 0
    while not q.empty():
        try:
            seg = q.get_nowait()
            seg.path.unlink(missing_ok=True)
            q.task_done()
            purged += 1
        except Exception:
            break
    return {"ok": True, "purged": purged}


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


@router.post("/api/playlist/load")
async def load_playlist(request: Request, _: None = Depends(require_admin_access)):
    """Load a new playlist from a Spotify URL and replace the current one."""
    from mammamiradio.playlist import fetch_playlist

    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        return {"ok": False, "error": "No URL provided"}

    config = request.app.state.config
    state = request.app.state.station_state

    # Temporarily override the playlist URL in config
    original_url = config.playlist.spotify_url
    config.playlist.spotify_url = url
    try:
        tracks = fetch_playlist(config)
    except Exception as e:
        config.playlist.spotify_url = original_url
        logger.error("Playlist load failed: %s", e)
        return {"ok": False, "error": "Failed to load playlist"}

    if not tracks:
        config.playlist.spotify_url = original_url
        return {"ok": False, "error": "No tracks found"}

    state.playlist = tracks
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
        return {"ok": True, "moved": track.display, "to_position": 0}
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
    state = request.app.state.station_state
    config = request.app.state.config
    return {
        "station": config.station.name,
        "running_jokes": state.running_jokes,
        "now_streaming": state.now_streaming,
        "stream_log": [
            {"type": e.type, "label": e.label, "timestamp": e.timestamp, "metadata": e.metadata}
            for e in state.stream_log
        ],
        "upcoming": preview_upcoming(state, config.pacing, state.playlist, count=5),
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
    payload = _public_status_payload(request)
    payload.update(
        {
            "queue_depth": segment_queue.qsize(),
            "segments_produced": state.segments_produced,
            "tracks_played": len(state.played_tracks),
            "uptime_sec": round(time.time() - start_time),
            "spotify_connected": state.spotify_connected,
            "playlist_url": request.app.state.config.playlist.spotify_url or "",
            "produced_log": [{"type": e.type, "label": e.label, "timestamp": e.timestamp} for e in state.segment_log],
            "last_banter_script": state.last_banter_script,
            "last_ad_script": state.last_ad_script,
            "ha_context": state.ha_context if state.ha_context else None,
            "go_librespot_log": _tail_log(str(config.tmp_dir / "go-librespot.log"), 15),
            "producer_errors": [
                {"type": e.type, "label": e.label, "metadata": e.metadata}
                for e in state.segment_log
                if e.metadata.get("error")
            ][-5:],
        }
    )
    return payload


def _tail_log(path: str, lines: int = 15) -> list[str]:
    """Return the last lines from a log file without raising on missing files."""
    try:
        with open(path) as f:
            return f.readlines()[-lines:]
    except Exception:
        return []
