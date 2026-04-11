"""FastAPI application entrypoint for the mammamiradio station."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time

from fastapi import FastAPI

from mammamiradio.config import load_config
from mammamiradio.models import StationState
from mammamiradio.persona import PersonaStore
from mammamiradio.playlist import DEMO_TRACKS, fetch_startup_playlist, read_persisted_source
from mammamiradio.producer import run_producer
from mammamiradio.streamer import LiveStreamHub, router, run_playback_loop
from mammamiradio.sync import init_db

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("mammamiradio")

app = FastAPI(title="mammamiradio")
app.include_router(router)

_producer_task: asyncio.Task | None = None
_playback_task: asyncio.Task | None = None


@app.on_event("startup")
async def startup():
    """Load config, build initial state, and start producer/playback workers."""
    global _producer_task, _playback_task

    config = load_config()
    logger.info("Station: %s (%s)", config.station.name, config.station.language)

    config.tmp_dir.mkdir(parents=True, exist_ok=True)
    config.cache_dir.mkdir(parents=True, exist_ok=True)

    # Initialize persona database and store for compounding listener memory
    db_path = config.cache_dir / "mammamiradio.db"
    init_db(db_path)
    persona_store = PersonaStore(db_path)

    # Dependency checks with install hints
    import shutil

    if not shutil.which("ffmpeg"):
        logger.warning(
            "FFmpeg not found — audio generation will fail. "
            "Install: brew install ffmpeg (macOS) or apt install ffmpeg (Linux)"
        )

    # Restore stop state so a reload/restart honours an operator-issued stop
    _stopped_flag = config.cache_dir / "session_stopped.flag"
    _session_stopped = _stopped_flag.exists()
    if _session_stopped:
        logger.info("Restoring stopped session state from previous run")

    persisted_source = read_persisted_source(config.cache_dir)
    logger.info("Fetching startup playlist")
    try:
        tracks, playlist_source, startup_source_error = fetch_startup_playlist(config, persisted_source)
    except Exception as e:
        logger.error("Playlist fetch crashed: %s — using demo playlist", e)
        tracks = list(DEMO_TRACKS)
        from mammamiradio.models import PlaylistSource

        playlist_source = PlaylistSource(
            kind="demo",
            source_id="demo",
            label="Built-in modern Italian demo mix",
            track_count=len(tracks),
            selected_at=time.time(),
        )
        startup_source_error = str(e)
    logger.info("Loaded %d tracks", len(tracks))

    state = StationState(
        playlist=tracks,
        playlist_source=playlist_source,
        startup_source_error=startup_source_error,
        persona_store=persona_store,
        session_stopped=_session_stopped,
    )
    queue: asyncio.Queue = asyncio.Queue(maxsize=config.pacing.lookahead_segments + 2)

    # Set app.state for streamer access
    app.state.queue = queue
    app.state.skip_event = asyncio.Event()
    app.state.source_switch_lock = asyncio.Lock()
    app.state.csrf_token = secrets.token_urlsafe(32)
    app.state.stream_hub = LiveStreamHub()
    app.state.stream_hub.bind_state(state)
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()

    # Pre-queue a welcome clip so listeners hear audio instantly (no 2-5s gap)
    from mammamiradio.models import Segment, SegmentType
    from mammamiradio.producer import _pick_canned_clip

    welcome = _pick_canned_clip("welcome")
    if not welcome:
        welcome = _pick_canned_clip("banter", state=state)
    if welcome:
        await queue.put(Segment(type=SegmentType.BANTER, path=welcome, metadata={"type": "welcome", "canned": True}))
        state.after_banter()
        logger.info("Pre-queued welcome clip for instant playback")

    _playback_task = asyncio.create_task(run_playback_loop(app))
    _producer_task = asyncio.create_task(run_producer(queue, state, config, skip_event=app.state.skip_event))
    app.state.playback_task = _playback_task
    app.state.producer_task = _producer_task
    logger.info(
        "Producer started. Stream at http://%s:%d/stream",
        config.bind_host,
        config.port,
    )


@app.on_event("shutdown")
async def shutdown():
    """Stop background workers and close shared streaming resources."""
    tasks_to_cancel = []
    if _producer_task:
        _producer_task.cancel()
        tasks_to_cancel.append(_producer_task)
    if _playback_task:
        _playback_task.cancel()
        tasks_to_cancel.append(_playback_task)
    if tasks_to_cancel:
        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
    if hasattr(app.state, "producer_task"):
        app.state.producer_task = None
    if hasattr(app.state, "playback_task"):
        app.state.playback_task = None
    if hasattr(app.state, "stream_hub"):
        app.state.stream_hub.close()


if __name__ == "__main__":
    import uvicorn

    config = load_config()
    uvicorn.run(
        "mammamiradio.main:app",
        host=config.bind_host,
        port=config.port,
    )
