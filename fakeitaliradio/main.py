from __future__ import annotations

import asyncio
import logging
import time

from fastapi import FastAPI

from fakeitaliradio.config import load_config
from fakeitaliradio.models import StationState
from fakeitaliradio.playlist import fetch_playlist
from fakeitaliradio.producer import run_producer
from fakeitaliradio.spotify_player import SpotifyPlayer
from fakeitaliradio.streamer import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fakeitaliradio")

app = FastAPI(title="fakeitaliradio")
app.include_router(router)

_producer_task: asyncio.Task | None = None
_spotify_player: SpotifyPlayer | None = None


@app.on_event("startup")
async def startup():
    global _producer_task, _spotify_player

    config = load_config()
    logger.info("Station: %s (%s)", config.station.name, config.station.language)

    config.tmp_dir.mkdir(exist_ok=True)
    config.cache_dir.mkdir(exist_ok=True)

    logger.info("Fetching playlist: %s", config.playlist.spotify_url)
    tracks = fetch_playlist(config)
    logger.info("Loaded %d tracks", len(tracks))

    # Start go-librespot for Spotify audio
    spotify_player = None
    try:
        _spotify_player = SpotifyPlayer(config)
        _spotify_player.start()
        spotify_player = _spotify_player
        logger.info("go-librespot started — select 'fakeitaliradio' in your Spotify app to connect")
    except Exception as e:
        logger.warning("Could not start go-librespot: %s — using fallback audio", e)

    state = StationState(playlist=tracks)
    queue: asyncio.Queue = asyncio.Queue(maxsize=config.pacing.lookahead_segments + 2)

    # Set app.state for streamer access
    app.state.queue = queue
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()

    _producer_task = asyncio.create_task(
        run_producer(queue, state, config, spotify_player=spotify_player)
    )
    logger.info(
        "Producer started. Stream at http://%s:%d/stream",
        config.bind_host,
        config.bind_port,
    )


@app.on_event("shutdown")
async def shutdown():
    if _spotify_player:
        _spotify_player.stop()
    if _producer_task:
        _producer_task.cancel()


if __name__ == "__main__":
    import uvicorn
    app_config = load_config()
    uvicorn.run(
        "fakeitaliradio.main:app",
        host=app_config.bind_host,
        port=app_config.bind_port,
    )
