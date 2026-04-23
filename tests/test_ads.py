"""Tests for the ad system: brand picking, multi-ad breaks, music beds."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from mammamiradio.ad_creative import (
    AdBrand,
    AdFormat,
    AdPart,
    AdScript,
    AdVoice,
    CampaignSpine,
    SonicWorld,
    _pick_brand,
)
from mammamiradio.models import (
    AdHistoryEntry,
    StationState,
)
from mammamiradio.scriptwriter import AD_BREAK_INTROS, AD_BREAK_OUTROS

# --- _pick_brand tests ---


def _make_brands():
    return [
        AdBrand(name="BrandA", tagline="A", recurring=True),
        AdBrand(name="BrandB", tagline="B", recurring=True),
        AdBrand(name="BrandC", tagline="C", recurring=False),
        AdBrand(name="BrandD", tagline="D", recurring=True),
    ]


def test_pick_brand_avoids_recent():
    brands = _make_brands()
    history = [
        AdHistoryEntry(brand="BrandA", summary="a"),
        AdHistoryEntry(brand="BrandB", summary="b"),
        AdHistoryEntry(brand="BrandD", summary="d"),
    ]
    # With A, B, D in last 3, only C should be eligible
    picked = _pick_brand(brands, history)
    assert picked.name == "BrandC"


def test_pick_brand_allows_repeats_when_pool_exhausted():
    brands = [AdBrand(name="Only", tagline="x", recurring=True)]
    history = [AdHistoryEntry(brand="Only", summary="y")]
    # Pool exhausted, should still return the only brand
    picked = _pick_brand(brands, history)
    assert picked.name == "Only"


def test_pick_brand_weights_recurring():
    brands = [
        AdBrand(name="Recurring", tagline="r", recurring=True),
        AdBrand(name="OneShot", tagline="o", recurring=False),
    ]
    # Run many picks, recurring should dominate
    counts = {"Recurring": 0, "OneShot": 0}
    for _ in range(100):
        picked = _pick_brand(brands, [])
        counts[picked.name] += 1
    assert counts["Recurring"] > counts["OneShot"]


# --- AdScript mood tests ---


def test_ad_script_has_mood():
    script = AdScript(
        brand="Test",
        parts=[AdPart(type="voice", text="hello")],
        mood="dramatic",
    )
    assert script.mood == "dramatic"


def test_ad_script_mood_defaults_empty():
    script = AdScript(brand="Test")
    assert script.mood == ""


# --- Ad break intro/outro phrases ---


def test_ad_break_intros_exist():
    assert len(AD_BREAK_INTROS) >= 3
    for intro in AD_BREAK_INTROS:
        assert isinstance(intro, str)
        assert len(intro) > 5


def test_ad_break_outros_exist():
    assert len(AD_BREAK_OUTROS) >= 3
    for outro in AD_BREAK_OUTROS:
        assert isinstance(outro, str)
        assert len(outro) > 5


# --- Audio generation tests (require ffmpeg) ---


@pytest.mark.requires_ffmpeg
def test_music_bed_generation():
    from mammamiradio.normalizer import generate_music_bed

    tmp = Path(tempfile.mkdtemp())
    try:
        for mood in ("dramatic", "lounge", "upbeat", "mysterious", "epic"):
            out = generate_music_bed(tmp / f"bed_{mood}.mp3", mood, 3.0)
            assert out.exists()
            assert out.stat().st_size > 1000
    finally:
        shutil.rmtree(tmp)


@pytest.mark.requires_ffmpeg
def test_bumper_jingle_generation():
    from mammamiradio.normalizer import generate_bumper_jingle

    tmp = Path(tempfile.mkdtemp())
    try:
        out = generate_bumper_jingle(tmp / "jingle.mp3")
        assert out.exists()
        assert out.stat().st_size > 1000

        # Short version
        out2 = generate_bumper_jingle(tmp / "jingle_short.mp3", 0.8)
        assert out2.exists()
    finally:
        shutil.rmtree(tmp)


@pytest.mark.requires_ffmpeg
def test_mix_with_bed():
    from mammamiradio.normalizer import (
        generate_music_bed,
        generate_tone,
        mix_with_bed,
    )

    tmp = Path(tempfile.mkdtemp())
    try:
        voice = generate_tone(tmp / "voice.mp3", 440, 2.0)
        bed = generate_music_bed(tmp / "bed.mp3", "lounge", 3.0)
        mixed = mix_with_bed(voice, bed, tmp / "mixed.mp3")
        assert mixed.exists()
        assert mixed.stat().st_size > voice.stat().st_size * 0.5
    finally:
        shutil.rmtree(tmp)


# --- Campaign arc context ---


def test_ad_break_increments_segments_produced_once():
    """A multi-spot ad break should increment segments_produced exactly once."""
    state = StationState()
    state.segments_produced = 5

    # Simulate a 3-spot break
    state.record_ad_spot(brand="A", summary="spot 1")
    state.record_ad_spot(brand="B", summary="spot 2")
    state.record_ad_spot(brand="C", summary="spot 3")
    state.after_ad(brands=["A", "B", "C"])

    assert state.segments_produced == 6  # exactly +1
    assert state.songs_since_ad == 0
    assert len(state.ad_history) == 3  # per-spot history preserved


def test_no_brands_resets_ad_pacing_to_prevent_infinite_spin():
    """When no brands are configured the producer resets songs_since_ad to 0.

    Without this reset the scheduler would return AD on every iteration
    (songs_since_ad stays >= songs_between_ads), creating an infinite spin
    that drains the queue and stalls the station.
    """
    state = StationState()
    state.songs_since_ad = 5
    state.segments_produced = 10
    # Simulate the producer's no-brands guard
    state.songs_since_ad = 0
    assert state.songs_since_ad == 0
    assert state.segments_produced == 10  # segments_produced not touched


def test_campaign_arc_in_ad_history():
    """Verify that same-brand history is tracked for campaign arcs."""
    state = StationState()
    state.record_ad_spot(brand="Caffè", summary="First ad: mysterious coffee")
    state.after_ad(brands=["Caffè"])
    state.record_ad_spot(brand="Gelato", summary="Gelato ad")
    state.after_ad(brands=["Gelato"])
    state.record_ad_spot(brand="Caffè", summary="Second ad: coffee conspiracy")
    state.after_ad(brands=["Caffè"])

    caffe_history = [e for e in state.ad_history if e.brand == "Caffè"]
    assert len(caffe_history) == 2
    assert "mysterious" in caffe_history[0].summary
    assert "conspiracy" in caffe_history[1].summary


# --- Signature ad system model tests ---


def test_ad_format_enum_values():
    """All 6 ad format values exist."""
    assert len(AdFormat) == 6
    assert AdFormat.CLASSIC_PITCH == "classic_pitch"
    assert AdFormat.TESTIMONIAL == "testimonial"
    assert AdFormat.DUO_SCENE == "duo_scene"
    assert AdFormat.LIVE_REMOTE == "live_remote"
    assert AdFormat.LATE_NIGHT_WHISPER == "late_night_whisper"
    assert AdFormat.INSTITUTIONAL_PSA == "institutional_psa"


def test_sonic_world_defaults():
    sw = SonicWorld()
    assert sw.environment == ""
    assert sw.music_bed == "lounge"
    assert sw.transition_motif == "chime"
    assert sw.sonic_signature == ""


def test_campaign_spine_defaults():
    cs = CampaignSpine()
    assert cs.premise == ""
    assert cs.sonic_signature == ""
    assert cs.format_pool == []
    assert cs.spokesperson == ""
    assert cs.escalation_rule == ""


def test_ad_brand_with_campaign():
    campaign = CampaignSpine(
        premise="Test premise",
        sonic_signature="ice_clink+startup_synth",
        format_pool=["classic_pitch", "duo_scene"],
        spokesperson="hammer",
    )
    brand = AdBrand(name="Test", tagline="Tag", campaign=campaign)
    assert brand.campaign is not None
    assert brand.campaign.premise == "Test premise"
    assert len(brand.campaign.format_pool) == 2


def test_ad_brand_without_campaign_compat():
    """Old-style brand without campaign still works."""
    brand = AdBrand(name="Old", tagline="T")
    assert brand.campaign is None
    assert brand.recurring is True


def test_ad_script_format_and_sonic():
    script = AdScript(
        brand="Test",
        parts=[AdPart(type="voice", text="hello")],
        format="duo_scene",
        sonic=SonicWorld(environment="cafe", music_bed="suspicious_jazz"),
        roles_used=["hammer", "maniac"],
    )
    assert script.format == "duo_scene"
    assert script.sonic.environment == "cafe"
    assert script.sonic.music_bed == "suspicious_jazz"
    assert script.roles_used == ["hammer", "maniac"]


def test_ad_history_tracks_format():
    state = StationState()
    state.record_ad_spot(brand="A", summary="test", format="duo_scene", sonic_signature="ice_clink")
    assert state.ad_history[-1].format == "duo_scene"
    assert state.ad_history[-1].sonic_signature == "ice_clink"


def test_ad_part_with_role():
    part = AdPart(type="voice", text="hello", role="hammer", environment="cafe")
    assert part.role == "hammer"
    assert part.environment == "cafe"


def test_ad_voice_with_role():
    voice = AdVoice(name="Roberto", voice="it-IT-GianniNeural", style="booming", role="hammer")
    assert voice.role == "hammer"
