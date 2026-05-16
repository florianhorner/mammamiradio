"""Listener-request endpoints + background tasks.

Extracted from `streamer.py` (PR Track B v2.11.0). Owns the public POST surface
where listeners submit a dedica or song wish, the admin queue view, the
public sanitized feed, and the dismiss endpoint. Plus the background task that
turns a typed song wish into a downloaded track ready for the producer.

State machine (extended in v2.11.0 for Track B):

    submit ──► [validate, rate-limit, moderate]
                    │
                    ▼
              ┌─ rejected (moderation refuses)
              │
              └─► queued ──► scheduled ──► on_air ──► aired
                              │              │
                              └─► expired ◄──┘  (audio production failed
                                                 or evicted past TTL)

`status` lives on each pending_requests record (added v2.11.0). Existing
admin endpoints continue to use `ts` as id for backward compat; `request_id`
(uuid4) is the canonical admin id going forward, while `public_token` is the
listener-safe tracking token exposed through the public feed.

Identity model: per-session nickname only. No cookies, no login. The
listener self-reports `name` with each submission. `submitter_ip_hash`
(HMAC-SHA256(IP + ADMIN_TOKEN)) is server-side rate-limit key only;
never exposed to listener responses.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import hashlib
import hmac
import ipaddress
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mammamiradio.core.models import SegmentType
from mammamiradio.web.streamer import require_admin_access

logger = logging.getLogger("mammamiradio.listener_requests")

_HASSIO_NETWORK = ipaddress.ip_network("172.30.32.0/23")
_TRUSTED_PROXY_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    _HASSIO_NETWORK,
]

# Bounded executor for listener song searches. Caps concurrency at 2 so
# listener yt-dlp tasks cannot exhaust the default ThreadPoolExecutor and
# starve the producer's audio prefetch work on Pi-class hardware.
_listener_dl_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="listener-dl")
atexit.register(_listener_dl_executor.shutdown, wait=False, cancel_futures=True)

router = APIRouter()


def _hash_submitter_ip(ip: str, config: Any) -> str:
    """HMAC-SHA256(IP, ADMIN_TOKEN) — Eng-Review decision #7.

    Returns hex digest. The hash is a server-side rate-limit key; never returned
    in listener-facing responses. When ADMIN_TOKEN is empty (dev/local), falls
    back to a deterministic placeholder so per-IP grouping still works in tests
    and unauthenticated local runs. The fallback is for function determinism,
    not secrecy.
    """
    secret = (getattr(config, "admin_token", "") or "").encode("utf-8")
    if not secret:
        secret = b"mmr-dev-local-no-admin-token"
    return hmac.new(secret, ip.encode("utf-8"), hashlib.sha256).hexdigest()


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _is_trusted_proxy_ip(value: str) -> bool:
    addr = _parse_ip(value)
    return bool(addr and any(addr in network for network in _TRUSTED_PROXY_NETWORKS))


def _client_ip_for_rate_limit(request: Request) -> str:
    """Return the best rate-limit identity inside the trusted-proxy boundary."""
    direct_ip = request.client.host if request.client else "unknown"
    if not _is_trusted_proxy_ip(direct_ip):
        return direct_ip

    forwarded_for = request.headers.get("X-Forwarded-For", "")
    for part in forwarded_for.split(","):
        forwarded_ip = part.strip()
        if _parse_ip(forwarded_ip):
            return forwarded_ip

    real_ip = request.headers.get("X-Real-IP", "").strip()
    if _parse_ip(real_ip):
        return real_ip

    return direct_ip


async def _read_json_object(request: Request) -> tuple[dict, JSONResponse | None]:
    """Parse the request body as a JSON object.

    Returns `(body, None)` on success or `({}, JSONResponse)` on failure so
    callers can use simple narrowing: `if error is not None: return error`.
    """
    try:
        body = await request.json()
    except ValueError:
        return {}, JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return {}, JSONResponse({"ok": False, "error": "invalid payload"}, status_code=400)
    return body, None


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
                # Track B v2.11.0 (admin-only fields):
                "request_id": r.get("request_id"),
                "status": r.get("status", "queued"),
                "evict_after": r.get("evict_after"),
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
                # Listener-side cards use public_token; request_id remains an
                # admin mutation handle and must not leak through the public feed.
                "public_token": r.get("public_token"),
                "status": r.get("status", "queued"),
            }
            for r in state.pending_requests
        ]
    }


@router.post("/api/listener-requests/dismiss")
async def dismiss_listener_request(request: Request, _: None = Depends(require_admin_access)):
    """Remove a specific listener request from the queue (admin only).

    Accepts either the legacy `ts`-based id or the v2.11.0 canonical `request_id`
    (uuid4). The listener-facing `public_token` is deliberately not accepted as
    an admin mutation handle.
    """
    state = request.app.state.station_state
    body, error = await _read_json_object(request)
    if error is not None:
        return error
    req_id = str(body.get("id") or "")
    if not req_id:
        return JSONResponse({"ok": False, "error": "id required"}, status_code=400)
    removed_requests = []
    kept_requests = []
    for r in state.pending_requests:
        if str(r.get("ts", "")) == req_id or str(r.get("request_id", "")) == req_id:
            removed_requests.append(r)
        else:
            kept_requests.append(r)
    state.pending_requests = kept_requests
    for r in removed_requests:
        track = r.get("song_track_obj")
        if track is None:
            continue
        state.playlist = [t for t in state.playlist if t is not track]
        if state.pinned_track is track:
            state.pinned_track = None
            if state.force_next == SegmentType.MUSIC:
                state.force_next = None
    return {"ok": True, "removed": len(removed_requests)}


@router.post("/api/listener-request")
async def listener_request(request: Request):
    """Accept a listener shoutout or song wish. Public endpoint, IP rate-limited."""
    body, error = await _read_json_object(request)
    if error is not None:
        return error
    raw_name = body.get("name")
    raw_message = body.get("message")
    if raw_name is not None and not isinstance(raw_name, str):
        return JSONResponse({"ok": False, "error": "name must be a string"}, status_code=400)
    if raw_message is not None and not isinstance(raw_message, str):
        return JSONResponse({"ok": False, "error": "message must be a string"}, status_code=400)
    from mammamiradio.hosts.scriptwriter import _sanitize_prompt_data

    name = _sanitize_prompt_data((raw_name or "Un ascoltatore").strip(), max_len=60)
    message = _sanitize_prompt_data((raw_message or "").strip(), max_len=200)
    if not message:
        return JSONResponse({"ok": False, "error": "message required"}, status_code=400)

    # IP rate limit: 1 request per 30s per listener identity.
    ip = _client_ip_for_rate_limit(request)
    state = request.app.state.station_state
    config = request.app.state.config
    submitter_ip_hash = _hash_submitter_ip(ip, config)
    now = time.time()
    last = state._listener_request_rl.get(submitter_ip_hash, 0)
    if now - last < 30:
        return JSONResponse({"ok": False, "retry_after": int(30 - (now - last))}, status_code=429)

    # Prune stale entries on every non-rate-limited request so a sustained
    # queue_full rejection wave can't grow the dict without bound.  Note:
    # rate-limit rejections return before this line, so they don't trigger the
    # prune; their entries expire naturally after 300 s.
    state._listener_request_rl = {k: v for k, v in state._listener_request_rl.items() if now - v < 300}

    # Cap queue. Limiter window is reserved for accepted requests only, so a
    # caller bounced by queue_full can retry immediately when capacity frees up.
    if len(state.pending_requests) >= 10:
        return JSONResponse({"ok": False, "error": "queue_full"}, status_code=429)

    state._listener_request_rl[submitter_ip_hash] = now

    # Detect song request by keyword
    msg_lower = message.lower()
    song_keywords = ["metti", "suona", "play", "voglio sentire", "puoi mettere", "can you play", "mettete"]
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
        "banter_cycles_missed": 0,  # initialized here; incremented by ListenerRequestCommit in scriptwriter.py
        "ts": now,
        # Track B v2.11.0 (Phase 2 — additive, state machine inert).
        # request_id is the canonical id (replaces ts in v2.12). status moves
        # through queued → scheduled → on_air → aired (or rejected/expired)
        # in Phase 3. evict_after is set on terminal transition. submitter_ip_hash
        # is HMAC-SHA256(IP, ADMIN_TOKEN) — never exposed in public responses.
        "request_id": str(uuid.uuid4()),
        "public_token": str(uuid.uuid4()),
        "status": "queued",
        "evict_after": None,
        "submitter_ip_hash": submitter_ip_hash,
    }
    state.pending_requests.append(req)

    # Fire async download for song requests
    if is_song_request:
        _dl_task = asyncio.create_task(_download_listener_song(req, request.app.state, state.playlist_revision))
        request.app.state.background_tasks = getattr(request.app.state, "background_tasks", set())
        request.app.state.background_tasks.add(_dl_task)
        _dl_task.add_done_callback(request.app.state.background_tasks.discard)

    logger.info("Listener request queued: request_id=%s type=%s", req["request_id"], req["type"])
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
        results = await loop.run_in_executor(_listener_dl_executor, search_ytdlp_metadata, query, 1)
        if not results:
            req["song_error"] = True
            logger.info("Listener song request returned no results: request_id=%s", req.get("request_id"))
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
    except asyncio.CancelledError:
        req["song_error"] = True
        if req in state.pending_requests:
            state.pending_requests.remove(req)
        logger.info("Listener song download cancelled: request_id=%s", req.get("request_id"))
        raise
    except Exception:
        req["song_error"] = True
        logger.warning("Listener song download failed: request_id=%s", req.get("request_id"), exc_info=True)
