"""FastAPI application entrypoint for the mammamiradio station."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from mammamiradio.config import load_config
from mammamiradio.downloader import evict_cache_lru, purge_suspect_cache_files
from mammamiradio.models import StationState
from mammamiradio.persona import PersonaStore
from mammamiradio.playlist import DEMO_TRACKS, fetch_startup_playlist, read_persisted_source
from mammamiradio.producer import prewarm_first_segment, run_producer
from mammamiradio.streamer import LiveStreamHub, router, run_playback_loop
from mammamiradio.sync import init_db

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("mammamiradio")

_producer_task: asyncio.Task | None = None
_playback_task: asyncio.Task | None = None
_prewarm_task: asyncio.Task | None = None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await startup()
    yield
    await shutdown()


app = FastAPI(title="mammamiradio", lifespan=_lifespan)
app.include_router(router)


async def startup():
    """Load config, build initial state, and start producer/playback workers."""
    global _producer_task, _playback_task, _prewarm_task

    config = load_config()
    logger.info("Station: %s (%s)", config.station.name, config.station.language)

    config.tmp_dir.mkdir(parents=True, exist_ok=True)
    config.cache_dir.mkdir(parents=True, exist_ok=True)

    # Purge suspect cache files (likely failed downloads) before serving
    purged = purge_suspect_cache_files(config.cache_dir)
    if purged:
        logger.info("Cache integrity check: purged %d suspect file(s)", purged)
    norm_count = len(list(config.cache_dir.glob("norm_*.mp3")))
    logger.info("Normalization cache: %d tracks pre-normalized", norm_count)

    # Evict old cached tracks if the cache exceeds the configured size limit
    evict_cache_lru(config.cache_dir, config.max_cache_size_mb)

    # Initialize persona database and store for compounding listener memory
    db_path = config.cache_dir / "mammamiradio.db"
    init_db(db_path)
    persona_store = PersonaStore(db_path)

    # Dependency checks with install hints
    _ffmpeg_found = bool(shutil.which("ffmpeg"))
    _ytdlp_found = bool(shutil.which("yt-dlp"))
    if not _ffmpeg_found:
        logger.warning(
            "FFmpeg not found — audio generation will fail. "
            "Install: brew install ffmpeg (macOS) or apt install ffmpeg (Linux)"
        )
    if config.allow_ytdlp and not _ytdlp_found:
        logger.warning(
            "yt-dlp not found but MAMMAMIRADIO_ALLOW_YTDLP is enabled — charts will fall back to demo. "
            "Install: brew install yt-dlp (macOS) or pip install yt-dlp"
        )

    # Restore stop state so a reload/restart honours an operator-issued stop.
    # The operator's /api/resume is the correct way to clear this — a crash or
    # watchdog restart should not silently undo a deliberate stop.
    _stopped_flag = config.cache_dir / "session_stopped.flag"
    _session_stopped = _stopped_flag.exists()
    if _session_stopped:
        logger.info(
            "Restoring stopped session state from previous run — use /api/resume or the admin panel to start playback"
        )

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

    # Ring buffer for clip sharing ("share WTF moment") — holds ~60s of MP3 chunks
    from collections import deque

    try:
        _clip_maxlen = max(240, int(config.audio.bitrate) * 1000 // 8 * 60 // 4096)
    except (TypeError, ValueError, AttributeError):
        _clip_maxlen = 240
    app.state.clip_ring_buffer: deque[bytes] = deque(maxlen=_clip_maxlen)

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

    # Pre-produce music segments in the background so app startup is instant.
    # If a listener arrives before prewarm finishes, the producer's idle-resume
    # logic queues a canned clip as an immediate fallback.
    # Keep prewarm capped at 2 across environments to avoid ffmpeg pileups on
    # constrained addon hardware while still buffering enough for smooth start.
    async def _prewarm_multiple():
        total = 2
        for _ in range(total):
            await prewarm_first_segment(queue, state, config)

    _prewarm_task = asyncio.create_task(_prewarm_multiple())

    _playback_task = asyncio.create_task(run_playback_loop(app))
    _producer_task = asyncio.create_task(run_producer(queue, state, config, skip_event=app.state.skip_event))
    app.state.prewarm_task = _prewarm_task
    app.state.playback_task = _playback_task
    app.state.producer_task = _producer_task
    # Startup diagnostics — first 5 seconds of logs must be actionable for debugging
    _config_file = Path("radio.toml").resolve()
    _audio_src = {"charts": "yt-dlp", "demo": "demo", "local": "local"}.get(
        (playlist_source.kind if playlist_source else ""), "unknown"
    )
    logger.info("Startup diagnostics:")
    logger.info("  config_file=%s  cache_dir=%s", _config_file, config.cache_dir)
    logger.info("  audio_source=%s  tracks=%d", _audio_src, len(tracks))
    logger.info(
        "  keys: anthropic=%s  openai=%s  ha_token=%s",
        "set" if os.getenv("ANTHROPIC_API_KEY") else "missing",
        "set" if os.getenv("OPENAI_API_KEY") else "missing",
        "set" if os.getenv("HA_TOKEN") else "missing",
    )
    logger.info(
        "  deps: ffmpeg=%s  ytdlp=%s (allowed=%s)",
        "found" if _ffmpeg_found else "MISSING",
        "found" if _ytdlp_found else "missing",
        "yes" if config.allow_ytdlp else "no",
    )
    logger.info(
        "Producer started. Stream at http://%s:%d/stream",
        config.bind_host,
        config.port,
    )


async def shutdown():
    """Stop background workers and close shared streaming resources."""
    tasks_to_cancel = []
    if _prewarm_task:
        _prewarm_task.cancel()
        tasks_to_cancel.append(_prewarm_task)
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
    if hasattr(app.state, "prewarm_task"):
        app.state.prewarm_task = None
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
