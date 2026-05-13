"""Focused producer attribution tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import HostPersonality, Segment, SegmentType, StationState, Track
from mammamiradio.scheduling.producer import RenderedMusicTrack, run_producer

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")
PRODUCER_MODULE = "mammamiradio.scheduling.producer"
SCRIPTWRITER_MODULE = "mammamiradio.hosts.scriptwriter"


def _make_config(tmp_path: Path):
    config = load_config(TOML_PATH)
    config.pacing.lookahead_segments = 1
    config.homeassistant.enabled = False
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    return config


async def _run_until_status_queued(
    queue: asyncio.Queue[Segment],
    state: StationState,
    config,
    timeout: float = 5.0,
) -> None:
    task = asyncio.create_task(run_producer(queue, state, config))
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while not state.queued_segments:
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError("Producer did not queue a status segment in time")
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.fixture(autouse=True)
def _mock_audio_validation():
    with (
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=180.0),
    ):
        yield


@pytest.mark.asyncio
async def test_queued_segment_includes_playlist_index_for_music(tmp_path):
    """Music segments must carry playlist_index >= 0 and source_kind."""
    tracks = [
        Track(title="Canzone Uno", artist="Artista", duration_ms=200_000, spotify_id="demo1", source="classic"),
        Track(title="Canzone Due", artist="Artista", duration_ms=180_000, spotify_id="demo2", source="jamendo"),
    ]
    state = StationState(playlist=tracks, listeners_active=1)
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    music_path = tmp_path / "music.mp3"
    music_path.write_bytes(b"fake audio")

    async def fake_render(track: Track, *_args, **_kwargs) -> RenderedMusicTrack:
        return RenderedMusicTrack(track=track, path=music_path, cache_path=music_path, cache_hit=True)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}._render_music_track", new_callable=AsyncMock, side_effect=fake_render),
        patch(f"{PRODUCER_MODULE}._prefetch_next", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.generate_track_rationale", return_value="Because it fits."),
        patch(f"{PRODUCER_MODULE}.classify_track_crate", return_value="test"),
    ):
        await _run_until_status_queued(queue, state, config)

    queued = state.queued_segments[-1]
    assert queued["playlist_index"] >= 0
    assert "source_kind" in queued
    assert queued["source_kind"] == state.playlist[queued["playlist_index"]].source


@pytest.mark.asyncio
async def test_queued_segment_playlist_index_minus_one_for_nonmusic(tmp_path):
    """Non-music segments must have playlist_index == -1."""
    state = StationState(
        playlist=[Track(title="Canzone Uno", artist="Artista", duration_ms=200_000, spotify_id="demo1")],
        listeners_active=1,
    )
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            return_value=([(host, "Che bella giornata!")], None),
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=tmp_path / "banter.mp3"),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=None),
    ):
        await _run_until_status_queued(queue, state, config)

    assert state.queued_segments[-1]["playlist_index"] == -1
    assert state.queued_segments[-1]["source_kind"] == ""
