from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from fakeitaliradio.models import Segment
from fakeitaliradio.scheduler import preview_upcoming

logger = logging.getLogger(__name__)

router = APIRouter()

DASHBOARD_HTML = (
    __import__("pathlib").Path(__file__).with_name("dashboard.html").read_text()
)

LISTENER_HTML = (
    __import__("pathlib").Path(__file__).with_name("listener.html").read_text()
)


async def _audio_generator(request: Request):
    """Stream audio at playback rate so dashboard stays in sync with listener."""
    CHUNK = 4096  # smaller chunks for tighter pacing
    segment_queue = request.app.state.queue
    state = request.app.state.station_state
    config = request.app.state.config

    # Throttle to bitrate so server stays in sync with what listener hears
    bytes_per_sec = (config.station.bitrate * 1000) / 8  # 192kbps = 24000 B/s
    chunk_duration = CHUNK / bytes_per_sec  # seconds per chunk

    while True:
        if await request.is_disconnected():
            logger.info("Client disconnected")
            state.now_streaming = {}
            break

        try:
            segment: Segment = await asyncio.wait_for(
                segment_queue.get(), timeout=30.0
            )
        except asyncio.TimeoutError:
            logger.warning("Queue empty for 30s, waiting...")
            continue

        # Mark this segment as NOW STREAMING
        state.on_stream_segment(segment)

        logger.info(
            ">>> NOW STREAMING %s: %s",
            segment.type.value,
            segment.metadata.get("title", segment.metadata),
        )

        try:
            send_start = time.monotonic()
            bytes_sent = 0
            with open(segment.path, "rb") as f:
                while chunk := f.read(CHUNK):
                    yield chunk
                    bytes_sent += len(chunk)

                    # Throttle: sleep to match playback rate
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


@router.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


@router.get("/listen", response_class=HTMLResponse)
async def listener():
    return LISTENER_HTML


@router.get("/stream")
async def stream(request: Request):
    config = request.app.state.config
    headers = {
        "Content-Type": "audio/mpeg",
        "icy-name": config.station.name,
        "icy-genre": config.station.theme[:64],
        "icy-br": str(config.station.bitrate),
        "Cache-Control": "no-cache, no-store",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        _audio_generator(request),
        headers=headers,
        media_type="audio/mpeg",
    )


@router.get("/api/logs")
async def logs(lines: int = 50):
    """Return recent go-librespot + producer logs."""
    return {
        "go_librespot": _tail_log("tmp/go-librespot.log", lines),
    }


@router.post("/api/shuffle")
async def shuffle_playlist(request: Request):
    """Shuffle upcoming tracks."""
    import random
    state = request.app.state.station_state
    random.shuffle(state.playlist)
    return {"ok": True, "message": "Playlist shuffled"}


@router.post("/api/skip")
async def skip_track(request: Request):
    """Skip the currently streaming segment."""
    state = request.app.state.station_state
    state.now_streaming = {"type": "skipping", "label": "Skipping...", "started": time.time()}
    return {"ok": True}


@router.post("/api/purge")
async def purge_queue(request: Request):
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
async def remove_track(request: Request):
    """Remove a track from playlist by index."""
    body = await request.json()
    idx = body.get("index", -1)
    state = request.app.state.station_state
    if 0 <= idx < len(state.playlist):
        removed = state.playlist.pop(idx)
        return {"ok": True, "removed": removed.display}
    return {"ok": False, "error": "Invalid index"}


@router.post("/api/playlist/move")
async def move_track(request: Request):
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


@router.post("/api/playlist/move_to_next")
async def move_to_next(request: Request):
    """Move a track to play next (position 0 in upcoming)."""
    body = await request.json()
    idx = body.get("index", -1)
    state = request.app.state.station_state
    pl = state.playlist

    # Find current position
    current_idx = 0
    if state.current_track:
        for i, t in enumerate(pl):
            if t.spotify_id == state.current_track.spotify_id:
                current_idx = i
                break

    # The "next" position is current_idx + 1
    next_pos = (current_idx + 1) % len(pl) if pl else 0

    if 0 <= idx < len(pl):
        track = pl.pop(idx)
        # Adjust next_pos if we popped before it
        if idx < next_pos:
            next_pos -= 1
        pl.insert(next_pos, track)
        return {"ok": True, "moved": track.display, "to_position": next_pos}
    return {"ok": False, "error": "Invalid index"}


@router.get("/status")
async def status(request: Request):
    state = request.app.state.station_state
    config = request.app.state.config
    segment_queue = request.app.state.queue
    start_time = request.app.state.start_time
    return {
        "station": config.station.name,
        "queue_depth": segment_queue.qsize(),
        "segments_produced": state.segments_produced,
        "tracks_played": len(state.played_tracks),
        "running_jokes": state.running_jokes,
        "uptime_sec": round(time.time() - start_time),
        "spotify_connected": state.spotify_connected,
        # What the listener hears RIGHT NOW
        "now_streaming": state.now_streaming,
        # What the producer has made (queued, waiting to stream)
        "produced_log": [
            {"type": e.type, "label": e.label, "timestamp": e.timestamp}
            for e in state.segment_log
        ],
        # What has actually been streamed to the listener
        "stream_log": [
            {"type": e.type, "label": e.label, "timestamp": e.timestamp,
             "metadata": e.metadata}
            for e in state.stream_log
        ],
        "upcoming": preview_upcoming(state, config.pacing, state.playlist, count=5),
        "last_banter_script": state.last_banter_script,
        "last_ad_script": state.last_ad_script,
        "ha_context": state.ha_context if state.ha_context else None,
        "go_librespot_log": _tail_log("tmp/go-librespot.log", 15),
        "producer_errors": [
            {"type": e.type, "label": e.label, "metadata": e.metadata}
            for e in state.segment_log
            if e.metadata.get("error")
        ][-5:],
    }


def _tail_log(path: str, lines: int = 15) -> list[str]:
    try:
        with open(path, "r") as f:
            return f.readlines()[-lines:]
    except Exception:
        return []
