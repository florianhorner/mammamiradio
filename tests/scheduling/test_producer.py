from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import Segment, SegmentType, StationState, Track
from mammamiradio.scheduling.producer import RenderedMusicTrack, run_producer

PRODUCER_MODULE = "mammamiradio.scheduling.producer"
TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")
VALID_SOURCE_KINDS = {"youtube", "jamendo", "local", "demo", "classic"}


def _make_config(tmp_path: Path):
    config = load_config(TOML_PATH)
    config.pacing.lookahead_segments = 1
    config.homeassistant.enabled = False
    config.cache_dir = tmp_path / "cache"
    config.tmp_dir = tmp_path / "tmp"
    config.cache_dir.mkdir()
    config.tmp_dir.mkdir()
    return config


async def _run_until_queued(queue: asyncio.Queue[Segment], state: StationState, config, timeout: float = 5.0) -> None:
    task = asyncio.create_task(run_producer(queue, state, config))
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while not state.queued_segments:
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError("Producer did not append queued segment in time")
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_music_queued_segment_has_playlist_index_and_source_kind(tmp_path: Path):
    tracks = [
        Track(title="Uno", artist="Artista", duration_ms=200_000, spotify_id="one", source="classic"),
        Track(title="Due", artist="Artista Due", duration_ms=180_000, spotify_id="two", source="jamendo"),
    ]
    state = StationState(playlist=tracks, listeners_active=1)
    config = _make_config(tmp_path)
    rendered_path = config.tmp_dir / "music.mp3"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    async def _prefetch_noop(*_args, **_kwargs) -> None:
        return None

    def _rendered(track: Track, *_args, **_kwargs) -> RenderedMusicTrack:
        return RenderedMusicTrack(track=track, path=rendered_path, cache_path=rendered_path, cache_hit=True)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}._render_music_track", new_callable=AsyncMock, side_effect=_rendered),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=180.0),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
        patch(f"{PRODUCER_MODULE}.generate_track_rationale", return_value="test rationale"),
        patch(f"{PRODUCER_MODULE}.classify_track_crate", return_value="test"),
        patch(f"{PRODUCER_MODULE}._prefetch_next", side_effect=_prefetch_noop),
    ):
        await _run_until_queued(queue, state, config)

    queued = state.queued_segments[0]
    assert queued["type"] == "music"
    assert queued["playlist_index"] >= 0
    assert queued["source_kind"] in VALID_SOURCE_KINDS


@pytest.mark.asyncio
async def test_banter_queued_segment_has_no_playlist_index(tmp_path: Path):
    state = StationState(
        playlist=[Track(title="Uno", artist="Artista", duration_ms=200_000, spotify_id="one")],
        listeners_active=1,
    )
    config = _make_config(tmp_path)
    canned_path = config.tmp_dir / "banter.mp3"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{PRODUCER_MODULE}._sw.has_script_llm", return_value=False),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=canned_path),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=30.0),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
    ):
        await _run_until_queued(queue, state, config)

    queued = state.queued_segments[0]
    assert queued["type"] == "banter"
    assert queued["playlist_index"] == -1
