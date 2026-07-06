"""Tests for the shared fallback / stream-outcome classifier.

Guards the prior-learning failure mode ``producer-rescue-paths-miss-fallback-flag``:
producer rescue clips set queue_drain_recovery / silence_fallback / resume_bridge
WITHOUT fallback:True, and a naive metadata["fallback"] check would miss them.
"""

from __future__ import annotations

import pytest

from mammamiradio.core import segment_status as ss


@pytest.mark.parametrize(
    "metadata",
    [
        {"fallback": True},
        {"rescue": True},
        {"error_recovery": True},
        {"queue_drain_recovery": True},
        {"resume_bridge": True},
        {"silence_fallback": True},
        # idle warm-up canned clips set idle_bridge but no audio_source, so the
        # flag must classify them as rescue (else a warm-up clip reads as the
        # primary station). Regression guard for #547.
        {"idle_bridge": True},
        {"audio_source": "fallback_demo_asset"},
        {"audio_source": "norm_cache"},
        {"audio_source": "emergency_tone"},
    ],
)
def test_is_fallback_active_true_for_every_rescue_signal(metadata):
    assert ss.is_fallback_active(metadata) is True


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"audio_source": "download"},
        {"audio_source": "charts"},
        {"fallback": False, "audio_source": ""},
        {"title": "Some Song"},
    ],
)
def test_is_fallback_active_false_for_clean_audio(metadata):
    assert ss.is_fallback_active(metadata) is False


def test_classify_stream_outcome_aired():
    assert ss.classify_stream_outcome(was_skipped=False, bytes_sent=1000, listeners=2) == ss.AIRED


def test_classify_stream_outcome_skipped():
    assert ss.classify_stream_outcome(was_skipped=True, bytes_sent=500, listeners=2) == ss.SKIPPED


def test_classify_stream_outcome_no_listeners():
    assert ss.classify_stream_outcome(was_skipped=False, bytes_sent=1000, listeners=0) == ss.NO_LISTENERS


def test_classify_stream_outcome_not_streamed_on_zero_bytes():
    assert ss.classify_stream_outcome(was_skipped=False, bytes_sent=0, listeners=3) == ss.NOT_STREAMED


def test_classify_stream_outcome_fallback_rescue_when_fallback_active():
    # Rescue audio that actually aired reads as fallback_rescue, not a clean air.
    assert (
        ss.classify_stream_outcome(was_skipped=False, bytes_sent=1000, listeners=2, fallback_active=True)
        == ss.FALLBACK_RESCUE
    )


def test_reach_problems_take_priority_over_fallback_source():
    # A skipped rescue clip is reported by its reach problem (skipped).
    assert ss.classify_stream_outcome(was_skipped=True, bytes_sent=10, listeners=2, fallback_active=True) == ss.SKIPPED
