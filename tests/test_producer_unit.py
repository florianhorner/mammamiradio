"""Unit tests for the producer pipeline in fakeitaliradio/producer.py."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from fakeitaliradio.config import load_config
from fakeitaliradio.models import (
    HostPersonality,
    Segment,
    SegmentType,
    StationState,
    Track,
)
from fakeitaliradio.producer import run_producer

TOML_PATH = str(Path(__file__).parent.parent / "radio.toml")
PRODUCER_MODULE = "fakeitaliradio.producer"


def _make_state() -> StationState:
    return StationState(
        playlist=[
            Track(title="Canzone Uno", artist="Artista", duration_ms=200_000, spotify_id="demo1"),
            Track(title="Canzone Due", artist="Artista", duration_ms=180_000, spotify_id="demo2"),
        ],
    )


def _make_config():
    config = load_config(TOML_PATH)
    config.pacing.lookahead_segments = 1
    config.homeassistant.enabled = False
    config.tmp_dir = Path("/tmp/fakeitaliradio_test")
    return config


def _fake_path(*_args, **_kwargs) -> Path:
    """Return a dummy Path that satisfies type checks."""
    return Path("/tmp/fakeitaliradio_test/fake.mp3")


async def _run_until_queued(queue: asyncio.Queue, state: StationState, config, timeout: float = 5.0):
    """Run the producer, waiting until at least one segment is queued, then cancel."""
    task = asyncio.create_task(run_producer(queue, state, config))
    try:
        # Poll until at least one segment appears
        deadline = asyncio.get_event_loop().time() + timeout
        while queue.qsize() == 0:
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError("Producer did not queue a segment in time")
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Music segment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_music_segment_queued():
    """Producer queues a MUSIC segment when next_segment_type returns MUSIC."""
    state = _make_state()
    config = _make_config()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.normalize", side_effect=_fake_path),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert queue.qsize() >= 1
    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC
    assert "title" in seg.metadata


# ---------------------------------------------------------------------------
# Banter segment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_banter_segment_queued():
    """Producer queues a BANTER segment with synthesized dialogue."""
    state = _make_state()
    config = _make_config()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")
    banter_lines = [(host, "Che bella giornata!")]

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{PRODUCER_MODULE}.write_banter", new_callable=AsyncMock, return_value=banter_lines),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert queue.qsize() >= 1
    seg = queue.get_nowait()
    assert seg.type == SegmentType.BANTER
    assert seg.metadata.get("type") == "banter"


# ---------------------------------------------------------------------------
# Error recovery — silence fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_recovery_queues_silence():
    """When download_track raises, producer inserts a silence segment and increments failed_segments."""
    state = _make_state()
    config = _make_config()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=RuntimeError("network down")),
        patch(f"{PRODUCER_MODULE}.generate_silence", side_effect=_fake_path),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert queue.qsize() >= 1
    seg = queue.get_nowait()
    # Segment type matches what was attempted (MUSIC), but metadata has error
    assert seg.type == SegmentType.MUSIC
    assert "error" in seg.metadata
    assert state.failed_segments >= 1
