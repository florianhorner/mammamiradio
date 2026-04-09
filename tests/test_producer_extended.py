"""Extended tests for the producer pipeline — ad breaks, HA context, Spotify path, error recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mammamiradio.audio_quality import AudioQualityError, AudioToolError
from mammamiradio.config import load_config
from mammamiradio.models import (
    AdBrand,
    AdFormat,
    AdHistoryEntry,
    AdPart,
    AdScript,
    AdVoice,
    CampaignSpine,
    HostPersonality,
    Segment,
    SegmentType,
    StationState,
    Track,
)
from mammamiradio.producer import _cast_voices, _pick_brand, _select_ad_creative, run_producer

TOML_PATH = str(Path(__file__).parent.parent / "radio.toml")
MODULE = "mammamiradio.producer"


@pytest.fixture(autouse=True)
def _mock_quality_gate():
    with patch(f"{MODULE}.validate_segment_audio", return_value=None):
        yield


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
        patch(f"{MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=mock_context) as mock_fetch,
    ):
        await _run_until_queued(queue, state, config)

    mock_fetch.assert_called_once()
    assert state.ha_context == "Il tempo e' bello"


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
        patch(f"{MODULE}.write_banter", new_callable=AsyncMock, return_value=[(host, "Linea test")]),
        patch(f"{MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
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
    mock_player.get_current_track = AsyncMock(return_value=None)  # no autoplay track
    mock_player.capture_current_audio = AsyncMock()

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{MODULE}.download_track_spotify", new_callable=AsyncMock, return_value=_fake_path()) as mock_dl,
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config, spotify_player=mock_player)

    mock_dl.assert_called_once()


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
        patch(f"{MODULE}.write_ad", new_callable=AsyncMock, return_value=fake_script),
        patch(f"{MODULE}.synthesize_ad", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{MODULE}.generate_bumper_jingle", side_effect=_fake_path),
        patch(f"{MODULE}.concat_files", side_effect=_fake_path),
        patch(f"{MODULE}.validate_segment_audio", side_effect=_quality_side_effect),
        patch(f"{MODULE}.download_track", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.normalize", side_effect=_fake_path),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC
    assert state.songs_since_ad == 1


@pytest.mark.asyncio
async def test_music_falls_back_when_spotify_not_authenticated(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    mock_player = MagicMock()
    mock_player._authenticated = False
    mock_player.check_auth = AsyncMock()
    mock_player.get_current_track = AsyncMock(return_value=None)

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
        patch(f"{MODULE}.write_banter", new_callable=AsyncMock, return_value=[(host, "Ciao!")]),
        patch(f"{MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
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
        patch(f"{MODULE}.write_ad", new_callable=AsyncMock, return_value=fake_script),
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
        patch(f"{MODULE}.validate_segment_audio", side_effect=_quality_side_effect),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC
    # The first call rejected, second call passed — so quality was checked at least twice
    assert call_count >= 2


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
        fmt, _sonic, _roles = _select_ad_creative(brand, state, config)
        assert fmt in ("late_night_whisper", "institutional_psa")


def test_select_ad_creative_default_format():
    """Brand without campaign gets a format from the full list."""
    brand = AdBrand(name="Test", tagline="T")
    state = StationState()
    config = _make_config(Path("/tmp"))

    fmt, _sonic, _roles = _select_ad_creative(brand, state, config)
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
        fmt, _sonic, _roles = _select_ad_creative(brand, state, config)
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

    fmt, _sonic, roles = _select_ad_creative(brand, state, config)
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

    voice_map = _cast_voices(brand, config, ["hammer"])
    assert "hammer" in voice_map
    assert voice_map["hammer"].name == "Roberto"


def test_cast_voices_fallback_random():
    """When no voice matches a role, a random voice is used."""
    brand = AdBrand(name="Test", tagline="T")
    config = _make_config(Path("/tmp"))
    config.ads.voices = [
        AdVoice(name="Roberto", voice="it-IT-GianniNeural", style="booming", role="hammer"),
    ]

    voice_map = _cast_voices(brand, config, ["unknown_role"])
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
        fmt, _sonic, _roles = _select_ad_creative(brand, state, config)
        assert fmt == "live_remote"


def test_select_ad_creative_category_sonic_defaults():
    """Brand without campaign but with known category gets one of the configured sonic variants."""
    brand = AdBrand(name="Test", tagline="T", category="food")
    state = StationState()
    config = _make_config(Path("/tmp"))

    _fmt, sonic, _roles = _select_ad_creative(brand, state, config)
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
        _fmt, sonic, _roles = _select_ad_creative(brand, state, config)
        assert not (
            sonic.environment == "cafe"
            and sonic.music_bed == "tarantella_pop"
            and sonic.transition_motif == "register_hit"
        )


def test_cast_voices_host_fallback():
    """When no ad voices are configured, _cast_voices falls back to a host voice."""
    brand = AdBrand(name="Test", tagline="T")
    config = _make_config(Path("/tmp"))
    config.ads.voices = []  # no ad voices

    voice_map = _cast_voices(brand, config, ["hammer"])
    assert "hammer" in voice_map
    # Should be one of the configured hosts
    host_names = {h.name for h in config.hosts}
    assert voice_map["hammer"].name in host_names
