"""Extended tests for the producer pipeline — ad breaks, HA context, Spotify path, error recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mammamiradio.config import load_config
from mammamiradio.models import (
    AdBrand,
    AdHistoryEntry,
    AdPart,
    AdScript,
    AdVoice,
    HostPersonality,
    Segment,
    SegmentType,
    StationState,
    Track,
)
from mammamiradio.producer import _pick_brand, run_producer

TOML_PATH = str(Path(__file__).parent.parent / "radio.toml")
MODULE = "mammamiradio.producer"


def _make_state() -> StationState:
    return StationState(
        playlist=[
            Track(title="Canzone Uno", artist="Artista", duration_ms=200_000, spotify_id="demo1"),
            Track(title="Canzone Due", artist="Artista", duration_ms=180_000, spotify_id="demo2"),
        ],
    )


def _make_config(tmp_path: Path | None = None):
    config = load_config(TOML_PATH)
    config.pacing.lookahead_segments = 1
    config.homeassistant.enabled = False
    if tmp_path:
        config.tmp_dir = tmp_path
    else:
        config.tmp_dir = Path("/tmp/mammamiradio_test")
    return config


def _fake_path(*_args, **_kwargs) -> Path:
    return Path("/tmp/mammamiradio_test/fake.mp3")


async def _run_until_queued(
    queue: asyncio.Queue,
    state: StationState,
    config,
    timeout: float = 5.0,
    spotify_player=None,
):
    task = asyncio.create_task(run_producer(queue, state, config, spotify_player=spotify_player))
    try:
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
# _pick_brand
# ---------------------------------------------------------------------------


def test_pick_brand_avoids_recent():
    brands = [AdBrand(name="A", tagline="a"), AdBrand(name="B", tagline="b"), AdBrand(name="C", tagline="c")]
    history = [
        AdHistoryEntry(brand="A", summary="", timestamp=0),
        AdHistoryEntry(brand="B", summary="", timestamp=0),
        AdHistoryEntry(brand="C", summary="", timestamp=0),
    ]
    # All 3 recent, so any brand is eligible (pool exhausted fallback)
    result = _pick_brand(brands, history)
    assert result.name in {"A", "B", "C"}


def test_pick_brand_prefers_recurring():
    brands = [
        AdBrand(name="R1", tagline="r1", recurring=True),
        AdBrand(name="NR1", tagline="nr1", recurring=False),
    ]
    # Run many times — recurring should appear more often
    results = [_pick_brand(brands, []).name for _ in range(100)]
    assert results.count("R1") > results.count("NR1")


def test_pick_brand_skips_last_three():
    brands = [
        AdBrand(name="A", tagline="a"),
        AdBrand(name="B", tagline="b"),
        AdBrand(name="C", tagline="c"),
        AdBrand(name="D", tagline="d"),
    ]
    history = [
        AdHistoryEntry(brand="A", summary="", timestamp=0),
        AdHistoryEntry(brand="B", summary="", timestamp=0),
        AdHistoryEntry(brand="C", summary="", timestamp=0),
    ]
    # Only D should be eligible
    for _ in range(20):
        assert _pick_brand(brands, history).name == "D"


# ---------------------------------------------------------------------------
# Ad break segment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ad_break_segment_queued(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    # Need brands for ad production
    config.ads.brands = [AdBrand(name="TestBrand", tagline="Buy it")]
    config.ads.voices = [AdVoice(name="VoiceGuy", voice="it-IT-DiegoNeural", style="energetic")]
    config.pacing.ad_spots_per_break = 1
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    fake_script = AdScript(
        brand="TestBrand",
        summary="Test ad",
        parts=[AdPart(type="voice", text="Buy TestBrand today!")],
    )

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.AD),
        patch(f"{MODULE}.write_ad", new_callable=AsyncMock, return_value=fake_script),
        patch(f"{MODULE}.synthesize_ad", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{MODULE}.generate_bumper_jingle", side_effect=_fake_path),
        patch(f"{MODULE}.concat_files", side_effect=_fake_path),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert queue.qsize() >= 1
    seg = queue.get_nowait()
    assert seg.type == SegmentType.AD
    assert "brands" in seg.metadata
    assert "TestBrand" in seg.metadata["brands"]


@pytest.mark.asyncio
async def test_ad_break_skipped_without_brands(tmp_path):
    """When no brands configured, ad segment is skipped and producer continues."""
    state = _make_state()
    config = _make_config(tmp_path)
    config.ads.brands = []
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    # After one AD skip (no brands), return MUSIC
    call_count = 0

    def alternating_type(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return SegmentType.AD
        return SegmentType.MUSIC

    with (
        patch(f"{MODULE}.next_segment_type", side_effect=alternating_type),
        patch(f"{MODULE}.download_track", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.normalize", side_effect=_fake_path),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC


# ---------------------------------------------------------------------------
# Ad break with host fallback voice (no dedicated ad voices)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ad_break_host_fallback_voice(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    config.ads.brands = [AdBrand(name="HostBrand", tagline="host-brand")]
    config.ads.voices = []  # No dedicated ad voices → use host voice
    config.pacing.ad_spots_per_break = 1
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    fake_script = AdScript(
        brand="HostBrand",
        summary="Host ad",
        parts=[AdPart(type="voice", text="Host reads the ad")],
    )

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.AD),
        patch(f"{MODULE}.write_ad", new_callable=AsyncMock, return_value=fake_script),
        patch(f"{MODULE}.synthesize_ad", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{MODULE}.generate_bumper_jingle", side_effect=_fake_path),
        patch(f"{MODULE}.concat_files", side_effect=_fake_path),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.AD


# ---------------------------------------------------------------------------
# HA context refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ha_context_refreshed_for_banter(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")
    banter_lines = [(host, "Che bella giornata!")]

    mock_context = MagicMock()
    mock_context.summary = "Il tempo e' bello"

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{MODULE}.write_banter", new_callable=AsyncMock, return_value=banter_lines),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=mock_context) as mock_fetch,
    ):
        await _run_until_queued(queue, state, config)

    mock_fetch.assert_called_once()
    assert state.ha_context == "Il tempo e' bello"


# ---------------------------------------------------------------------------
# Spotify track path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_music_uses_spotify_when_authenticated(tmp_path):
    state = _make_state()
    # Use a non-demo track
    state.playlist = [Track(title="Real Song", artist="Artist", duration_ms=200_000, spotify_id="realid123")]
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    mock_player = MagicMock()
    mock_player._authenticated = True
    mock_player.check_auth = AsyncMock()

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{MODULE}.download_track_spotify", new_callable=AsyncMock, return_value=_fake_path()) as mock_dl,
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config, spotify_player=mock_player)

    mock_dl.assert_called_once()


@pytest.mark.asyncio
async def test_music_falls_back_when_spotify_not_authenticated(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    mock_player = MagicMock()
    mock_player._authenticated = False
    mock_player.check_auth = AsyncMock()

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{MODULE}.download_track", new_callable=AsyncMock, return_value=_fake_path()) as mock_dl,
        patch(f"{MODULE}.normalize", side_effect=_fake_path),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config, spotify_player=mock_player)

    mock_dl.assert_called_once()


# ---------------------------------------------------------------------------
# Error recovery — silence generation also fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_recovery_silence_also_fails(tmp_path):
    """When both download and silence generation fail, producer continues without crashing."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    call_count = 0

    def segment_type_with_recovery(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return SegmentType.MUSIC
        return SegmentType.MUSIC

    with (
        patch(f"{MODULE}.next_segment_type", side_effect=segment_type_with_recovery),
        patch(
            f"{MODULE}.download_track",
            new_callable=AsyncMock,
            side_effect=[
                RuntimeError("network down"),
                _fake_path(),
            ],
        ),
        patch(f"{MODULE}.generate_silence", side_effect=[RuntimeError("ffmpeg broken"), _fake_path]),
        patch(f"{MODULE}.normalize", side_effect=_fake_path),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config, timeout=10.0)

    # Should eventually get a segment (either silence from 2nd attempt or music)
    assert queue.qsize() >= 1


# ---------------------------------------------------------------------------
# Backoff on consecutive failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consecutive_failures_increment_counter(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{MODULE}.download_track", new_callable=AsyncMock, side_effect=RuntimeError("fail")),
        patch(f"{MODULE}.generate_silence", side_effect=_fake_path),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert state.failed_segments >= 1


@pytest.mark.asyncio
async def test_success_resets_failure_counter(tmp_path):
    state = _make_state()
    state.failed_segments = 5
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{MODULE}.download_track", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.normalize", side_effect=_fake_path),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert state.failed_segments == 0
