"""Extended tests for mammamiradio/track_rationale.py — coverage sprint."""

from __future__ import annotations

import random

from mammamiradio.models import ListenerProfile, PlaylistSource, Track
from mammamiradio.track_rationale import (
    GUARDRAIL_RULES,
    TASTE_CRATES,
    TasteCrate,
    classify_track_crate,
    generate_track_rationale,
)

# ---------------------------------------------------------------------------
# classify_track_crate
# ---------------------------------------------------------------------------


def test_classify_demo_source():
    """Demo source tracks go to 'discoveries'."""
    track = Track(title="Test", artist="Artist", duration_ms=1000)
    source = PlaylistSource(kind="demo")
    assert classify_track_crate(track, source) == "discoveries"


def test_classify_high_popularity():
    """High popularity tracks go to 'classics'."""
    track = Track(title="Test", artist="Artist", duration_ms=1000, popularity=80)
    assert classify_track_crate(track, None) == "classics"


def test_classify_explicit():
    """Explicit tracks go to 'guilty_pleasures'."""
    track = Track(title="Test", artist="Artist", duration_ms=1000, explicit=True, popularity=50)
    assert classify_track_crate(track, None) == "guilty_pleasures"


def test_classify_low_popularity():
    """Low popularity tracks go to 'deep_cuts'."""
    track = Track(title="Test", artist="Artist", duration_ms=1000, popularity=20)
    assert classify_track_crate(track, None) == "deep_cuts"


def test_classify_default():
    """Default classification is discoveries or wildcards."""
    track = Track(title="Test", artist="Artist", duration_ms=1000, popularity=50)
    random.seed(42)
    result = classify_track_crate(track, None)
    assert result in ("discoveries", "wildcards")


# ---------------------------------------------------------------------------
# generate_track_rationale
# ---------------------------------------------------------------------------


def test_rationale_basic():
    """Generates a non-empty rationale string."""
    track = Track(title="Volare", artist="Modugno", duration_ms=180000, popularity=75)
    rationale = generate_track_rationale(track)
    assert isinstance(rationale, str)
    assert len(rationale) > 10


def test_rationale_with_listener_patterns():
    """Includes listener-specific reasons when patterns exist."""
    track = Track(title="Test", artist="Artist", duration_ms=1000)
    listener = ListenerProfile()

    # Populate recent_outcomes to trigger pattern detection
    # Need 10+ outcomes with 4+ skips to trigger "restless_skipper"
    for i in range(10):
        listener.record_outcome(skipped=(i < 5), listen_sec=10 if i < 5 else 200, track_display=f"Song {i}")

    assert "restless_skipper" in listener.patterns

    # Run multiple times to verify pattern-aware reasons are in the pool
    rationales = set()
    for _ in range(100):
        rationales.add(generate_track_rationale(track, listener=listener))

    # At least one of the pattern-specific rationales should appear
    found = any("skip it" in r or "Prove us wrong" in r for r in rationales)
    assert found, f"No pattern-specific rationale found in {len(rationales)} attempts"


def test_rationale_with_album():
    """Template substitution includes album name."""
    track = Track(title="Test", artist="Artist", duration_ms=240000, album="Greatest Hits")
    rationales = set()
    for _ in range(100):
        rationales.add(generate_track_rationale(track))

    found = any("Greatest Hits" in r for r in rationales)
    assert found, "Album name should appear in at least one rationale"


def test_rationale_without_album():
    """Falls back to placeholder when album is empty."""
    track = Track(title="Test", artist="Artist", duration_ms=240000)
    rationales = set()
    for _ in range(200):
        rationales.add(generate_track_rationale(track))

    found = any("can't pronounce" in r for r in rationales)
    assert found, "Should use placeholder album name"


# ---------------------------------------------------------------------------
# TasteCrate data structure
# ---------------------------------------------------------------------------


def test_taste_crates_complete():
    """All taste crates have required fields."""
    assert len(TASTE_CRATES) == 5
    for crate in TASTE_CRATES:
        assert isinstance(crate, TasteCrate)
        assert crate.key
        assert crate.label_it
        assert crate.label_en
        assert crate.description
        assert crate.icon


# ---------------------------------------------------------------------------
# Guardrail rules
# ---------------------------------------------------------------------------


def test_guardrail_rules_defined():
    """Guardrail rules exist and cover key areas."""
    assert len(GUARDRAIL_RULES) >= 5
    joined = " ".join(GUARDRAIL_RULES).lower()
    assert "date" in joined
    assert "location" in joined
    assert "sensitive" in joined
    assert "statistic" in joined or "stat" in joined
    assert "deniability" in joined


# ---------------------------------------------------------------------------
# _GUARDRAIL_BANNED_PATTERNS (import and verify)
# ---------------------------------------------------------------------------


def test_guardrail_patterns_compile():
    """All guardrail banned patterns should compile as valid regex."""
    import re

    from mammamiradio.track_rationale import _GUARDRAIL_BANNED_PATTERNS

    for pattern in _GUARDRAIL_BANNED_PATTERNS:
        re.compile(pattern, re.IGNORECASE)  # Should not raise
