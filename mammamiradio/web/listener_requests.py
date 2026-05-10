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
admin endpoints continue to use `ts` as id for backward compat; the new
`request_id` (uuid4) is the canonical id going forward and replaces `ts`
in v2.12.

Identity model: per-session nickname only. No cookies, no login. The
listener self-reports `name` with each submission. `submitter_ip_hash`
(HMAC-SHA256(IP + ADMIN_TOKEN)) is server-side rate-limit key only;
never exposed to listener responses.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mammamiradio.core.models import SegmentType
from mammamiradio.web.streamer import require_admin_access

logger = logging.getLogger("mammamiradio.listener_requests")

router = APIRouter()


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
