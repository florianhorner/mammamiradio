"""Tests for the ad system: brand picking, multi-ad breaks, music beds."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from fakeitaliradio.models import (
    AdBrand, AdHistoryEntry, AdPart, AdScript, AdVoice, StationState, Track,
)
from fakeitaliradio.producer import _pick_brand
from fakeitaliradio.scriptwriter import AD_BREAK_INTROS, AD_BREAK_OUTROS


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

def test_music_bed_generation():
    from fakeitaliradio.normalizer import generate_music_bed
    tmp = Path(tempfile.mkdtemp())
    try:
        for mood in ("dramatic", "lounge", "upbeat", "mysterious", "epic"):
            out = generate_music_bed(tmp / f"bed_{mood}.mp3", mood, 3.0)
            assert out.exists()
            assert out.stat().st_size > 1000
    finally:
        shutil.rmtree(tmp)


def test_bumper_jingle_generation():
    from fakeitaliradio.normalizer import generate_bumper_jingle
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


def test_mix_with_bed():
    from fakeitaliradio.normalizer import generate_music_bed, generate_tone, mix_with_bed
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

def test_campaign_arc_in_ad_history():
    """Verify that same-brand history is tracked for campaign arcs."""
    state = StationState()
    state.after_ad(brand="Caffè", summary="First ad: mysterious coffee")
    state.after_ad(brand="Gelato", summary="Gelato ad")
    state.after_ad(brand="Caffè", summary="Second ad: coffee conspiracy")

    caffe_history = [e for e in state.ad_history if e.brand == "Caffè"]
    assert len(caffe_history) == 2
    assert "mysterious" in caffe_history[0].summary
    assert "conspiracy" in caffe_history[1].summary
