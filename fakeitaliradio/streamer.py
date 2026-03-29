from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from fakeitaliradio.models import Segment

logger = logging.getLogger(__name__)

router = APIRouter()


async def _audio_generator(request: Request):
    CHUNK = 16384
    segment_queue = request.app.state.queue

    while True:
        if await request.is_disconnected():
            logger.info("Client disconnected")
            break

        try:
            segment: Segment = await asyncio.wait_for(
                segment_queue.get(), timeout=30.0
            )
        except asyncio.TimeoutError:
            logger.warning("Queue empty for 30s, waiting...")
            continue

        logger.info(
            "Streaming %s: %s",
            segment.type.value,
            segment.metadata.get("title", segment.metadata),
        )

        try:
            with open(segment.path, "rb") as f:
                while chunk := f.read(CHUNK):
                    yield chunk
                    await asyncio.sleep(0)
        finally:
            segment.path.unlink(missing_ok=True)
            segment_queue.task_done()


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


@router.get("/status")
async def status(request: Request):
    state = request.app.state.station_state
    config = request.app.state.config
    segment_queue = request.app.state.queue
    start_time = request.app.state.start_time
    return {
        "station": config.station.name,
        "queue_depth": segment_queue.qsize(),
        "current_track": state.current_track.display
        if state.current_track
        else None,
        "segments_produced": state.segments_produced,
        "tracks_played": len(state.played_tracks),
        "running_jokes": state.running_jokes,
        "uptime_sec": round(time.time() - start_time),
    }
