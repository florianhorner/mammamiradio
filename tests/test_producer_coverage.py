"""Extended tests for mammamiradio/producer.py — coverage sprint.

Covers: _select_ad_creative, _cast_voices, _pick_brand, _latest_music_file,
        _set_last_music_file, _try_crossfade, and producer helper utilities.
"""

from __future__ import annotations

import random
from unittest.mock import MagicMock, patch

import pytest

from mammamiradio.models import (
    AdBrand,
    AdFormat,
    AdHistoryEntry,
    AdVoice,
    CampaignSpine,
    SonicWorld,
    StationState,
)
from mammamiradio.producer import (
    _cast_voices,
    _latest_music_file,
    _pick_brand,
    _select_ad_creative,
    _set_last_music_file,
)

# ---------------------------------------------------------------------------
# _pick_brand
# ---------------------------------------------------------------------------


def test_pick_brand_avoids_recent():
    """Avoids the last 3 aired brands."""
    brands = [
        AdBrand(name="BrandA", tagline="A", category="tech"),
        AdBrand(name="BrandB", tagline="B", category="food"),
        AdBrand(name="BrandC", tagline="C", category="fashion"),
        AdBrand(name="BrandD", tagline="D", category="beauty"),
    ]
    history = [
        AdHistoryEntry(brand="BrandA", summary="test", format="classic_pitch"),
        AdHistoryEntry(brand="BrandB", summary="test", format="classic_pitch"),
        AdHistoryEntry(brand="BrandC", summary="test", format="classic_pitch"),
    ]
    # Only BrandD is eligible
    random.seed(42)
    result = _pick_brand(brands, history)
    assert result.name == "BrandD"


def test_pick_brand_allows_repeats_when_exhausted():
    """Falls back to all brands when pool is exhausted."""
    brands = [AdBrand(name="OnlyBrand", tagline="Only", category="tech")]
    history = [AdHistoryEntry(brand="OnlyBrand", summary="test", format="classic_pitch")]
    result = _pick_brand(brands, history)
    assert result.name == "OnlyBrand"


def test_pick_brand_weights_recurring():
    """Recurring brands are weighted higher."""
    brands = [
        AdBrand(name="Recurring", tagline="R", category="tech", recurring=True),
        AdBrand(name="OneShot", tagline="O", category="tech", recurring=False),
    ]
    random.seed(1)
    picks = [_pick_brand(brands, []) for _ in range(50)]
    recurring_count = sum(1 for p in picks if p.name == "Recurring")
    assert recurring_count > 25  # Should be weighted 3:1


# ---------------------------------------------------------------------------
# _select_ad_creative
# ---------------------------------------------------------------------------


def test_select_ad_creative_basic():
    """Returns format, sonic world, and roles."""
    brand = AdBrand(name="TestBrand", tagline="Test", category="tech")
    state = StationState()
    config = MagicMock()
    config.ads.voices = []

    fmt, sonic, roles = _select_ad_creative(brand, state, config)
    assert isinstance(fmt, str)
    assert isinstance(sonic, SonicWorld)
    assert isinstance(roles, list)


def test_select_ad_creative_campaign_format():
    """Uses campaign format pool when specified."""
    campaign = CampaignSpine(format_pool=["classic_pitch", "live_remote"])
    brand = AdBrand(name="TestBrand", tagline="Test", category="tech", campaign=campaign)
    state = StationState()
    config = MagicMock()
    config.ads.voices = []

    fmt, _, _ = _select_ad_creative(brand, state, config)
    assert fmt in ["classic_pitch", "live_remote"]


def test_select_ad_creative_voice_guard():
    """Excludes multi-voice formats when < 2 voices available."""
    brand = AdBrand(name="TestBrand", tagline="Test", category="tech")
    state = StationState()
    config = MagicMock()
    config.ads.voices = [AdVoice(name="Solo", voice="it-voice", style="warm")]

    fmt, _, _ = _select_ad_creative(brand, state, config)
    assert AdFormat(fmt).voice_count < 2


def test_select_ad_creative_avoids_last_format():
    """Avoids the last-used format for the same brand."""
    brand = AdBrand(name="TestBrand", tagline="Test", category="tech")
    state = StationState()
    state.ad_history = [AdHistoryEntry(brand="TestBrand", summary="test", format="classic_pitch")]
    config = MagicMock()
    config.ads.voices = [
        AdVoice(name="V1", voice="v1", style="warm"),
        AdVoice(name="V2", voice="v2", style="cool"),
    ]

    # Run many times — should not always pick classic_pitch
    formats = set()
    for _ in range(20):
        fmt, _, _ = _select_ad_creative(brand, state, config)
        formats.add(fmt)
    assert len(formats) > 1


def test_select_ad_creative_campaign_sonic_signature():
    """Uses campaign sonic signature."""
    campaign = CampaignSpine(sonic_signature="piano+strings")
    brand = AdBrand(name="TestBrand", tagline="Test", category="food", campaign=campaign)
    state = StationState()
    config = MagicMock()
    config.ads.voices = []

    _, sonic, _ = _select_ad_creative(brand, state, config)
    assert sonic.sonic_signature == "piano+strings"
    assert sonic.transition_motif == "piano"


def test_select_ad_creative_campaign_spokesperson():
    """Uses campaign spokesperson as primary role."""
    campaign = CampaignSpine(spokesperson="seductress")
    brand = AdBrand(name="TestBrand", tagline="Test", category="beauty", campaign=campaign)
    state = StationState()
    config = MagicMock()
    config.ads.voices = [
        AdVoice(name="V1", voice="v1", style="warm", role="seductress"),
    ]

    _, _, roles = _select_ad_creative(brand, state, config)
    assert "seductress" in roles


# ---------------------------------------------------------------------------
# _cast_voices
# ---------------------------------------------------------------------------


def test_cast_voices_with_matching_roles():
    """Maps roles to matching AdVoice instances."""
    voices = [
        AdVoice(name="Hammer", voice="v1", style="aggressive", role="hammer"),
        AdVoice(name="Maniac", voice="v2", style="crazy", role="maniac"),
    ]
    config = MagicMock()
    config.ads.voices = voices
    brand = AdBrand(name="Test", tagline="T", category="tech")

    result = _cast_voices(brand, config, ["hammer", "maniac"])
    assert result["hammer"].name == "Hammer"
    assert result["maniac"].name == "Maniac"


def test_cast_voices_fallback_random():
    """Falls back to random voice when role not found."""
    voices = [AdVoice(name="Generic", voice="v1", style="warm")]
    config = MagicMock()
    config.ads.voices = voices
    brand = AdBrand(name="Test", tagline="T", category="tech")

    result = _cast_voices(brand, config, ["unknown_role"])
    assert "unknown_role" in result
    assert result["unknown_role"].name == "Generic"


def test_cast_voices_no_voices_configured():
    """Uses host as fallback when no voices configured."""
    host = MagicMock()
    host.name = "Marco"
    host.voice = "it-voice"
    host.style = "warm"
    config = MagicMock()
    config.ads.voices = []
    config.hosts = [host]
    brand = AdBrand(name="Test", tagline="T", category="tech")

    result = _cast_voices(brand, config, ["hammer"])
    assert "hammer" in result


# ---------------------------------------------------------------------------
# _latest_music_file / _set_last_music_file
# ---------------------------------------------------------------------------


def test_set_and_get_latest_music_file(tmp_path):
    from mammamiradio import producer

    orig = producer._last_music_file

    try:
        f = tmp_path / "music_abc.mp3"
        f.write_bytes(b"audio")
        _set_last_music_file(f)
        assert _latest_music_file(tmp_path) == f
    finally:
        producer._last_music_file = orig


def test_latest_music_file_fallback(tmp_path):
    from mammamiradio import producer

    orig = producer._last_music_file

    try:
        producer._last_music_file = None
        f = tmp_path / "music_xyz.mp3"
        f.write_bytes(b"audio")
        result = _latest_music_file(tmp_path)
        assert result == f
    finally:
        producer._last_music_file = orig


def test_latest_music_file_none(tmp_path):
    from mammamiradio import producer

    orig = producer._last_music_file

    try:
        producer._last_music_file = None
        assert _latest_music_file(tmp_path) is None
    finally:
        producer._last_music_file = orig


# ---------------------------------------------------------------------------
# _try_crossfade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_try_crossfade_no_music(tmp_path):
    from mammamiradio.producer import _try_crossfade

    voice_path = tmp_path / "voice.mp3"
    voice_path.write_bytes(b"voice")
    config = MagicMock()
    config.tmp_dir = tmp_path

    with patch("mammamiradio.producer._latest_music_file", return_value=None):
        result = await _try_crossfade(voice_path, config, tmp_path / "output.mp3")
        assert result == voice_path


@pytest.mark.asyncio
async def test_try_crossfade_failure(tmp_path):
    from mammamiradio.producer import _try_crossfade

    voice_path = tmp_path / "voice.mp3"
    voice_path.write_bytes(b"voice")
    music_path = tmp_path / "music.mp3"
    music_path.write_bytes(b"music")
    config = MagicMock()
    config.tmp_dir = tmp_path

    with (
        patch("mammamiradio.producer._latest_music_file", return_value=music_path),
        patch(
            "mammamiradio.producer.crossfade_voice_over_music",
            side_effect=Exception("ffmpeg failed"),
        ),
    ):
        result = await _try_crossfade(voice_path, config, tmp_path / "output.mp3")
        assert result == voice_path
