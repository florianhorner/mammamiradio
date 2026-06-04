"""Test fixtures for the v1 integration-contract tests.

These fixtures build a slim FastAPI app holding ONLY the integrations
router plus a populated ``app.state.station_state`` / ``app.state.config``.
They avoid pulling in streamer task plumbing so the integration nave can
be tested in isolation from the producer/playback loops.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi import FastAPI

from mammamiradio.core.config import load_config
from mammamiradio.core.models import Segment, SegmentType, StationState, Track
from mammamiradio.integrations import router as integrations_router

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def make_integrations_app() -> FastAPI:
    """Return a minimal FastAPI app with the integrations router mounted."""
    app = FastAPI()
    app.include_router(integrations_router)
    config = load_config(TOML_PATH)
    state = StationState(
        playlist=[
            Track(title="Volare", artist="Domenico Modugno", duration_ms=210_000, spotify_id="vol1"),
            Track(title="Sapore di Sale", artist="Gino Paoli", duration_ms=200_000, spotify_id="sap1"),
        ],
    )
    app.state.queue = asyncio.Queue()
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    return app


def play_music_segment(
    state: StationState,
    *,
    title: str = "Volare — Domenico Modugno",
    title_only: str = "Volare",
    artist: str = "Domenico Modugno",
    album_art: str = "http://example.test/art.jpg",
    spotify_id: str = "v01",
    youtube_id: str = "y01",
    album: str = "",
    year: int = 0,
    duration_sec: float = 210.0,
    extra_metadata: dict | None = None,
) -> None:
    """Convenience: drive on_stream_segment with a populated music segment."""
    metadata: dict = {
        "title": title,
        "title_only": title_only,
        "artist": artist,
        "album_art": album_art,
        "spotify_id": spotify_id,
        "youtube_id": youtube_id,
    }
    if album:
        metadata["album"] = album
    if year:
        metadata["year"] = year
    if extra_metadata:
        metadata.update(extra_metadata)
    state.on_stream_segment(
        Segment(
            type=SegmentType.MUSIC,
            path=Path("/tmp/test.mp3"),
            duration_sec=duration_sec,
            metadata=metadata,
        )
    )


def play_segment(
    state: StationState,
    seg_type: SegmentType,
    *,
    title: str = "",
    host: str = "",
    duration_sec: float = 20.0,
    extra_metadata: dict | None = None,
) -> None:
    """Drive on_stream_segment with any segment type for fixture setup."""
    metadata: dict = {}
    if title:
        metadata["title"] = title
    if host:
        metadata["host"] = host
    if extra_metadata:
        metadata.update(extra_metadata)
    state.on_stream_segment(
        Segment(
            type=seg_type,
            path=Path(f"/tmp/{seg_type.value}.mp3"),
            duration_sec=duration_sec,
            metadata=metadata,
        )
    )


@pytest.fixture
def integrations_app() -> FastAPI:
    return make_integrations_app()
