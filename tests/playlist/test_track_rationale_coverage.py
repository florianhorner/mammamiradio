"""Extended tests for mammamiradio/playlist/track_rationale.py — coverage sprint."""

from __future__ import annotations

import random

from mammamiradio.core.models import ListenerProfile, PlaylistSource, Track
from mammamiradio.playlist.track_rationale import (
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
    for _ in range(500):
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
# _GUARDRAIL_BANNED_PATTERNS (import and verify)
# ---------------------------------------------------------------------------


def test_guardrail_patterns_compile():
    """All guardrail banned patterns should compile as valid regex."""
    import re

    from mammamiradio.playlist.track_rationale import _GUARDRAIL_BANNED_PATTERNS

    for pattern in _GUARDRAIL_BANNED_PATTERNS:
        re.compile(pattern, re.IGNORECASE)  # Should not raise


def test_rationale_with_ballad_lover_pattern():
    """Includes ballad-specific reason when listener is a ballad_lover."""
    track = Track(title="Test", artist="Artist", duration_ms=200000)
    listener = ListenerProfile()
    # 3+ slow songs, none skipped → ballad_lover
    for _ in range(3):
        listener.record_outcome(skipped=False, listen_sec=200, energy_hint="low", track_display="Slow Song")
    for _ in range(5):
        listener.record_outcome(skipped=False, listen_sec=200, energy_hint="med", track_display="Med Song")

    assert "ballad_lover" in listener.patterns

    rationales = set()
    for _ in range(200):
        rationales.add(generate_track_rationale(track, listener=listener))
    found = any("romantic" in r or "feelings" in r for r in rationales)
    assert found, f"No ballad_lover rationale found in {len(rationales)} attempts"


def test_rationale_with_energy_seeker_pattern():
    """Includes energy-specific reason when listener is an energy_seeker."""
    track = Track(title="Test", artist="Artist", duration_ms=200000)
    listener = ListenerProfile()
    # 3+ high-energy completions → energy_seeker
    for _ in range(3):
        listener.record_outcome(skipped=False, listen_sec=200, energy_hint="high", track_display="Fast Song")
    for _ in range(3):
        listener.record_outcome(skipped=False, listen_sec=200, energy_hint="med", track_display="Med Song")

    assert "energy_seeker" in listener.patterns

    rationales = set()
    for _ in range(200):
        rationales.add(generate_track_rationale(track, listener=listener))
    found = any("BPM" in r or "moving" in r for r in rationales)
    assert found, f"No energy_seeker rationale found in {len(rationales)} attempts"


def test_rationale_with_bails_on_intros_pattern():
    """Includes intro-specific reason when listener bails on intros."""
    track = Track(title="Test", artist="Artist", duration_ms=200000)
    listener = ListenerProfile()
    # 2+ intro bails (skipped in < 30s) → bails_on_intros
    for _ in range(2):
        listener.record_outcome(skipped=True, listen_sec=10, energy_hint="med", track_display="Song A")
    for _ in range(3):
        listener.record_outcome(skipped=False, listen_sec=200, energy_hint="med", track_display="Song B")

    assert "bails_on_intros" in listener.patterns

    rationales = set()
    for _ in range(200):
        rationales.add(generate_track_rationale(track, listener=listener))
    found = any("point fast" in r or "impatience" in r for r in rationales)
    assert found, f"No bails_on_intros rationale found in {len(rationales)} attempts"
