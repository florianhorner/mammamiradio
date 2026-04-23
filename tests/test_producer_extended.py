"""Extended tests for the producer pipeline — ad breaks, HA context, Spotify path, error recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mammamiradio.ad_creative import (
    AdBrand,
    AdFormat,
    AdPart,
    AdScript,
    AdVoice,
    CampaignSpine,
    _cast_voices,
    _pick_brand,
    _select_ad_creative,
)
from mammamiradio.audio_quality import AudioQualityError, AudioToolError
from mammamiradio.config import load_config
from mammamiradio.models import (
    AdHistoryEntry,
    HostPersonality,
    Segment,
    SegmentType,
    StationState,
    Track,
)
from mammamiradio.producer import run_producer

TOML_PATH = str(Path(__file__).parent.parent / "radio.toml")
MODULE = "mammamiradio.producer"
SCRIPTWRITER_MODULE = "mammamiradio.scriptwriter"


@pytest.fixture(autouse=True)
def _mock_quality_gate():
    with patch(f"{MODULE}.validate_segment_audio", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _mock_download_validation():
    with patch(f"{MODULE}.validate_download", return_value=(True, "ok")):
        yield


def _make_state() -> StationState:
    return StationState(
        playlist=[
            Track(title="Canzone Uno", artist="Artista", duration_ms=200_000, spotify_id="demo1"),
            Track(title="Canzone Due", artist="Artista", duration_ms=180_000, spotify_id="demo2"),
        ],
        listeners_active=1,  # simulate a live listener so the producer gate passes
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
):
    task = asyncio.create_task(run_producer(queue, state, config))
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
        patch(f"{SCRIPTWRITER_MODULE}.write_ad", new_callable=AsyncMock, return_value=fake_script),
        patch(f"{MODULE}.synthesize_ad", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock) as mock_synthesize,
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
    assert mock_synthesize.call_count >= 2
    assert all("engine" in call.kwargs for call in mock_synthesize.call_args_list)


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
        patch(f"{MODULE}.shutil.copy2"),
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
        patch(f"{SCRIPTWRITER_MODULE}.write_ad", new_callable=AsyncMock, return_value=fake_script),
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
    mock_context.events_summary = "- La macchina del caffe: spento/a -> acceso/a (1 min fa)"

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=mock_context) as mock_fetch,
    ):
        await _run_until_queued(queue, state, config)

    mock_fetch.assert_called_once()
    assert state.ha_context == "Il tempo e' bello"
    assert state.ha_events_summary == "- La macchina del caffe: spento/a -> acceso/a (1 min fa)"


@pytest.mark.asyncio
async def test_banter_quality_reject_uses_canned_fallback(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    config.anthropic_api_key = "test-key"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")
    generated = tmp_path / "banter_generated.mp3"
    generated.write_bytes(b"\x00" * 2048)
    canned = tmp_path / "banter_canned.mp3"
    canned.write_bytes(b"\x00" * 2048)

    quality_calls = 0

    def _quality_side_effect(path, seg_type):
        nonlocal quality_calls
        if seg_type == SegmentType.BANTER:
            quality_calls += 1
            if quality_calls == 1:
                raise AudioQualityError("too much silence")
        return None

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            return_value=([(host, "Linea test")], None),
        ),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=generated),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=generated),
        patch(f"{MODULE}.concat_files", return_value=generated),
        patch(f"{MODULE}._pick_canned_clip", return_value=canned),
        patch(f"{MODULE}.validate_segment_audio", side_effect=_quality_side_effect),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.BANTER
    assert seg.metadata.get("canned") is True
    assert seg.path == canned


@pytest.mark.asyncio
async def test_ad_quality_reject_resets_pacing_and_continues(tmp_path):
    state = _make_state()
    state.songs_since_ad = 9
    config = _make_config(tmp_path)
    config.ads.brands = [AdBrand(name="TestBrand", tagline="Buy it")]
    config.ads.voices = [AdVoice(name="VoiceGuy", voice="it-IT-DiegoNeural", style="energetic")]
    config.pacing.ad_spots_per_break = 1
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    fake_script = AdScript(
        brand="TestBrand",
        summary="Test ad",
        parts=[AdPart(type="voice", text="Buy TestBrand today!")],
    )

    call_count = 0

    def _seg_type(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return SegmentType.AD
        return SegmentType.MUSIC

    def _quality_side_effect(path, seg_type):
        if seg_type == SegmentType.AD:
            raise AudioQualityError("silent ad break")
        return None

    with (
        patch(f"{MODULE}.next_segment_type", side_effect=_seg_type),
        patch(f"{SCRIPTWRITER_MODULE}.write_ad", new_callable=AsyncMock, return_value=fake_script),
        patch(f"{MODULE}.synthesize_ad", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{MODULE}.generate_bumper_jingle", side_effect=_fake_path),
        patch(f"{MODULE}.concat_files", side_effect=_fake_path),
        patch(f"{MODULE}.validate_segment_audio", side_effect=_quality_side_effect),
        patch(f"{MODULE}.download_track", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.normalize", side_effect=_fake_path),
        patch(f"{MODULE}.shutil.copy2"),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC
    assert state.songs_since_ad == 1


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
        patch(f"{MODULE}.shutil.copy2"),
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
        patch(f"{MODULE}.shutil.copy2"),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert state.failed_segments == 0


# ---------------------------------------------------------------------------
# AudioToolError pass-through — tool absent should not drop content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audio_tool_error_in_banter_does_not_drop_segment(tmp_path):
    """If ffmpeg is absent during banter quality check, segment should still be queued."""
    state = _make_state()
    config = _make_config(tmp_path)
    config.anthropic_api_key = "test-key"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")
    generated = tmp_path / "banter_generated.mp3"
    generated.write_bytes(b"\x00" * 2048)

    def _quality_side_effect(path, seg_type):
        if seg_type == SegmentType.BANTER:
            raise AudioToolError("ffmpeg not found")
        return None

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=([(host, "Ciao!")], None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=generated),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=generated),
        patch(f"{MODULE}.concat_files", return_value=generated),
        patch(f"{MODULE}.validate_segment_audio", side_effect=_quality_side_effect),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.BANTER


@pytest.mark.asyncio
async def test_audio_tool_error_in_ad_does_not_drop_segment(tmp_path):
    """If ffmpeg is absent during ad quality check, ad break should still be queued."""
    state = _make_state()
    state.songs_since_ad = 9
    config = _make_config(tmp_path)
    config.ads.brands = [AdBrand(name="TestBrand", tagline="Buy it")]
    config.ads.voices = [AdVoice(name="VoiceGuy", voice="it-IT-DiegoNeural", style="energetic")]
    config.pacing.ad_spots_per_break = 1
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    fake_script = AdScript(
        brand="TestBrand",
        summary="Test ad",
        parts=[AdPart(type="voice", text="Buy TestBrand today!")],
    )

    def _quality_side_effect(path, seg_type):
        if seg_type == SegmentType.AD:
            raise AudioToolError("ffmpeg not found")
        return None

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.AD),
        patch(f"{SCRIPTWRITER_MODULE}.write_ad", new_callable=AsyncMock, return_value=fake_script),
        patch(f"{MODULE}.synthesize_ad", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{MODULE}.generate_bumper_jingle", side_effect=_fake_path),
        patch(f"{MODULE}.concat_files", side_effect=_fake_path),
        patch(f"{MODULE}.validate_segment_audio", side_effect=_quality_side_effect),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.AD


# ---------------------------------------------------------------------------
# MUSIC quality gate — corrupt/silent downloads rejected before queueing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_music_quality_reject_retries_next_track(tmp_path):
    """A music track that fails the quality gate should be skipped; producer retries the next track."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    call_count = 0

    def _quality_side_effect(path, seg_type):
        nonlocal call_count
        if seg_type == SegmentType.MUSIC:
            call_count += 1
            if call_count == 1:
                raise AudioQualityError("too short (10.00s < 30.00s)")
        return None

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{MODULE}.download_track", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.normalize", side_effect=_fake_path),
        patch(f"{MODULE}.shutil.copy2"),
        patch(f"{MODULE}.validate_segment_audio", side_effect=_quality_side_effect),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC
    # The first call rejected, second call passed — so quality was checked at least twice
    assert call_count >= 2


@pytest.mark.asyncio
async def test_music_quality_circuit_breaker_after_3_rejections(tmp_path):
    """After 3 consecutive quality gate rejections the circuit breaker lets the next track through."""
    state = _make_state()
    # Need enough tracks so the producer can keep retrying
    state.playlist = [
        Track(title=f"Track {i}", artist="A", duration_ms=200_000, spotify_id=f"demo{i}") for i in range(6)
    ]
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    call_count = 0

    def _always_reject(path, seg_type):
        nonlocal call_count
        if seg_type == SegmentType.MUSIC:
            call_count += 1
            raise AudioQualityError("music has too much silence (100% > 95%)")
        return None

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{MODULE}.download_track", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.normalize", side_effect=_fake_path),
        patch(f"{MODULE}.shutil.copy2"),
        patch(f"{MODULE}.validate_segment_audio", side_effect=_always_reject),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    # No banter clips available in test env → circuit breaker falls back to queuing the
    # silent music track as a last resort (the "no fallback" path).
    assert seg.type == SegmentType.MUSIC
    # Circuit breaker fires on the 3rd rejection, so we should see exactly 3 quality checks
    assert call_count == 3


@pytest.mark.asyncio
async def test_circuit_breaker_silence_inserts_banter_when_available(tmp_path):
    """When all-silence rejections hit the circuit breaker AND a canned banter clip
    is available, the breaker must queue BANTER instead of the silent track."""
    state = _make_state()
    state.playlist = [
        Track(title=f"Track {i}", artist="A", duration_ms=200_000, spotify_id=f"demo{i}") for i in range(6)
    ]
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    fake_banter = tmp_path / "fallback_banter.mp3"
    fake_banter.write_bytes(b"fake")

    def _always_silence(path, seg_type):
        if seg_type == SegmentType.MUSIC:
            raise AudioQualityError("music has too much silence (100% > 95%)")

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{MODULE}.download_track", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.normalize", side_effect=_fake_path),
        patch(f"{MODULE}.shutil.copy2"),
        patch(f"{MODULE}.validate_segment_audio", side_effect=_always_silence),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{MODULE}._pick_canned_clip", return_value=fake_banter),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    # Banter was available → circuit breaker must queue BANTER not the silent track
    assert seg.type == SegmentType.BANTER
    assert seg.metadata.get("silence_fallback") is True


# ---------------------------------------------------------------------------
# Signature ad system: _select_ad_creative and _cast_voices
# ---------------------------------------------------------------------------


def test_select_ad_creative_uses_format_pool():
    """When brand has a campaign.format_pool, format is picked from it."""
    brand = AdBrand(
        name="Test",
        tagline="T",
        campaign=CampaignSpine(format_pool=["late_night_whisper", "institutional_psa"]),
    )
    state = StationState()
    config = _make_config(Path("/tmp"))
    config.ads.voices = [
        AdVoice(name="A", voice="v1", style="s", role="hammer"),
        AdVoice(name="B", voice="v2", style="s", role="seductress"),
    ]

    for _ in range(20):
        fmt, _sonic, _roles = _select_ad_creative(brand, state, len(config.ads.voices))
        assert fmt in ("late_night_whisper", "institutional_psa")


def test_select_ad_creative_default_format():
    """Brand without campaign gets a format from the full list."""
    brand = AdBrand(name="Test", tagline="T")
    state = StationState()
    config = _make_config(Path("/tmp"))

    fmt, _sonic, _roles = _select_ad_creative(brand, state, len(config.ads.voices))
    all_formats = [f.value for f in AdFormat]
    assert fmt in all_formats


def test_select_ad_creative_voice_count_guard():
    """With < 2 voices, duo_scene and testimonial should be excluded."""
    brand = AdBrand(
        name="Test",
        tagline="T",
        campaign=CampaignSpine(format_pool=["duo_scene", "testimonial", "classic_pitch"]),
    )
    state = StationState()
    config = _make_config(Path("/tmp"))
    config.ads.voices = [AdVoice(name="Solo", voice="v1", style="s")]  # only 1 voice

    for _ in range(20):
        fmt, _sonic, _roles = _select_ad_creative(brand, state, len(config.ads.voices))
        assert fmt not in ("duo_scene", "testimonial")


def test_select_ad_creative_speaker_count():
    """duo_scene should request 2 roles."""
    brand = AdBrand(
        name="Test",
        tagline="T",
        campaign=CampaignSpine(format_pool=["duo_scene"], spokesperson="hammer"),
    )
    state = StationState()
    config = _make_config(Path("/tmp"))
    config.ads.voices = [
        AdVoice(name="A", voice="v1", style="s", role="hammer"),
        AdVoice(name="B", voice="v2", style="s", role="maniac"),
    ]

    fmt, _sonic, roles = _select_ad_creative(brand, state, len(config.ads.voices))
    assert fmt == "duo_scene"
    assert len(roles) == 2


def test_cast_voices_with_spokesperson():
    """Brand with spokesperson gets that role's voice as primary."""
    brand = AdBrand(
        name="Test",
        tagline="T",
        campaign=CampaignSpine(spokesperson="hammer"),
    )
    config = _make_config(Path("/tmp"))
    config.ads.voices = [
        AdVoice(name="Roberto", voice="it-IT-GianniNeural", style="booming", role="hammer"),
        AdVoice(name="Fiamma", voice="it-IT-FiammaNeural", style="enthusiastic", role="maniac"),
    ]

    voice_map = _cast_voices(brand, config.ads.voices, config.hosts, ["hammer"])
    assert "hammer" in voice_map
    assert voice_map["hammer"].name == "Roberto"


def test_cast_voices_fallback_random():
    """When no voice matches a role, a random voice is used."""
    brand = AdBrand(name="Test", tagline="T")
    config = _make_config(Path("/tmp"))
    config.ads.voices = [
        AdVoice(name="Roberto", voice="it-IT-GianniNeural", style="booming", role="hammer"),
    ]

    voice_map = _cast_voices(brand, config.ads.voices, [], ["unknown_role"])
    assert "unknown_role" in voice_map
    assert voice_map["unknown_role"].name == "Roberto"  # only option


def test_select_ad_creative_avoids_last_format():
    """_select_ad_creative avoids the last-used format for a brand."""
    brand = AdBrand(
        name="Test",
        tagline="T",
        campaign=CampaignSpine(format_pool=["classic_pitch", "live_remote"]),
    )
    state = StationState()
    config = _make_config(Path("/tmp"))

    # Seed history so last format for this brand was classic_pitch
    state.ad_history.append(AdHistoryEntry(brand="Test", summary="test", timestamp=0.0, format="classic_pitch"))

    # With only 2 options and last-used excluded, should always pick live_remote
    for _ in range(20):
        fmt, _sonic, _roles = _select_ad_creative(brand, state, len(config.ads.voices))
        assert fmt == "live_remote"


def test_select_ad_creative_category_sonic_defaults():
    """Brand without campaign but with known category gets one of the configured sonic variants."""
    brand = AdBrand(name="Test", tagline="T", category="food")
    state = StationState()
    config = _make_config(Path("/tmp"))

    _fmt, sonic, _roles = _select_ad_creative(brand, state, len(config.ads.voices))
    assert sonic.environment in {"cafe", "shopping_channel"}
    assert sonic.music_bed in {"tarantella_pop", "cheap_synth_romance", "upbeat"}
    assert sonic.transition_motif in {"register_hit", "ice_clink", "mandolin_sting"}


def test_select_ad_creative_avoids_last_sonic_variant_when_possible():
    brand = AdBrand(name="Test", tagline="T", category="food")
    state = StationState()
    config = _make_config(Path("/tmp"))
    state.record_ad_spot(
        brand="Test",
        environment="cafe",
        music_bed="tarantella_pop",
        transition_motif="register_hit",
    )

    for _ in range(20):
        _fmt, sonic, _roles = _select_ad_creative(brand, state, len(config.ads.voices))
        assert not (
            sonic.environment == "cafe"
            and sonic.music_bed == "tarantella_pop"
            and sonic.transition_motif == "register_hit"
        )


@pytest.mark.asyncio
async def test_news_flash_segment_is_produced(tmp_path):
    """Producer produces a NEWS_FLASH segment when the scheduler asks for one."""
    state = _make_state()
    config = _make_config(tmp_path)
    config.anthropic_api_key = "test-key"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0]
    flash_path = tmp_path / "flash.mp3"
    flash_path.write_bytes(b"\x00" * 2048)

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.NEWS_FLASH),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_news_flash",
            new_callable=AsyncMock,
            return_value=(host, "Aggiornamento traffico!", "traffic"),
        ),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=flash_path) as mock_synthesize,
        patch(f"{MODULE}._try_crossfade", new_callable=AsyncMock, return_value=flash_path),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.NEWS_FLASH
    assert seg.metadata.get("category") == "traffic"
    assert seg.metadata.get("host") == host.name
    assert mock_synthesize.await_args.kwargs.get("rate") == "+10%"
    assert mock_synthesize.await_args.kwargs.get("pitch") is None


@pytest.mark.asyncio
async def test_news_flash_sports_uses_faster_rate(tmp_path):
    """Producer applies a faster TTS rate for sports news flashes."""
    state = _make_state()
    config = _make_config(tmp_path)
    config.anthropic_api_key = "test-key"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0]
    flash_path = tmp_path / "flash.mp3"
    flash_path.write_bytes(b"\x00" * 2048)

    synthesize_calls: list[dict] = []

    async def _capture_synthesize(text, voice, path, rate=None, pitch=None, **kw):
        synthesize_calls.append({"rate": rate, "pitch": pitch})
        return path

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.NEWS_FLASH),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_news_flash",
            new_callable=AsyncMock,
            return_value=(host, "Gooool!", "sports"),
        ),
        patch(f"{MODULE}.synthesize", side_effect=_capture_synthesize),
        patch(f"{MODULE}._try_crossfade", new_callable=AsyncMock, return_value=flash_path),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert synthesize_calls, "synthesize must be called"
    assert synthesize_calls[0]["rate"] == "+25%"
    assert synthesize_calls[0]["pitch"] == "+12Hz"


def test_cast_voices_host_fallback():
    """When no ad voices are configured, _cast_voices falls back to a host voice."""
    brand = AdBrand(name="Test", tagline="T")
    config = _make_config(Path("/tmp"))
    config.ads.voices = []  # no ad voices

    voice_map = _cast_voices(brand, config.ads.voices, config.hosts, ["hammer"])
    assert "hammer" in voice_map
    # Should be one of the configured hosts
    host_names = {h.name for h in config.hosts}
    assert voice_map["hammer"].name in host_names
