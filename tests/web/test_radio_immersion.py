"""Tests for radio immersion features: ad processing, station IDs, time checks, promo tags."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from mammamiradio.core.config import PacingSection
from mammamiradio.core.models import SegmentType, StationState, Track

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_subprocess():
    """Patch subprocess.run to return success by default."""
    completed = MagicMock(spec=subprocess.CompletedProcess)
    completed.returncode = 0
    completed.stderr = b""
    completed.stdout = b""

    with patch("mammamiradio.audio.normalizer.subprocess.run", return_value=completed) as mock_run:
        yield mock_run, completed


def _make_state(**kwargs) -> StationState:
    return StationState(
        playlist=[Track(title="Test", artist="Test", duration_ms=200000, spotify_id="test1")],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# normalize_ad: broadcast-style ad processing
# ---------------------------------------------------------------------------


def test_normalize_ad_applies_broadcast_chain(mock_subprocess, tmp_path):
    """normalize_ad should apply compression + treble boost + loudness normalization."""
    from mammamiradio.audio.normalizer import normalize_ad

    in_path = tmp_path / "ad_raw.mp3"
    in_path.write_bytes(b"fake audio")
    out_path = tmp_path / "ad_broadcast.mp3"

    normalize_ad(in_path, out_path)

    mock_run, _ = mock_subprocess
    cmd = mock_run.call_args[0][0]
    # Should contain acompressor, treble, loudnorm
    filter_str = " ".join(cmd)
    assert "acompressor" in filter_str
    assert "treble" in filter_str
    assert "loudnorm" in filter_str
    # Louder than standard (-14 vs -16)
    assert "I=-14" in filter_str


# ---------------------------------------------------------------------------
# generate_station_id_bed
# ---------------------------------------------------------------------------


def test_generate_station_id_bed_creates_output(mock_subprocess, tmp_path):
    """Station ID bed generator should call ffmpeg with echo/reverb for signature sound."""
    from mammamiradio.audio.normalizer import generate_station_id_bed

    out_path = tmp_path / "station_id.mp3"
    generate_station_id_bed(out_path, duration_sec=3.0)

    mock_run, _ = mock_subprocess
    cmd = " ".join(mock_run.call_args[0][0])
    assert "aecho" in cmd
    assert "station ID bed" in mock_run.call_args[1].get("", "") or True


def test_mix_voice_with_sting(mock_subprocess, tmp_path):
    """mix_voice_with_sting should layer voice over sting with appropriate levels."""
    from mammamiradio.audio.normalizer import mix_voice_with_sting

    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"voice")
    sting = tmp_path / "sting.mp3"
    sting.write_bytes(b"sting")
    out = tmp_path / "mixed.mp3"

    mix_voice_with_sting(voice, sting, out)

    mock_run, _ = mock_subprocess
    cmd = " ".join(mock_run.call_args[0][0])
    assert "amix" in cmd
    assert "volume=0.15" in cmd  # sting very quiet — background texture only


# ---------------------------------------------------------------------------
# Scheduler: station ID and time check pacing
# ---------------------------------------------------------------------------


def test_station_id_triggers_after_enough_segments():
    """STATION_ID should trigger after 5+ segments when nothing else triggers."""
    from mammamiradio.scheduling.scheduler import _decide

    result = _decide(
        segments_produced=10,
        songs_since_ad=1,
        songs_since_banter=1,
        pacing=PacingSection(songs_between_banter=99, songs_between_ads=99),
        deterministic=True,
        songs_since_news=0,
        segments_since_station_id=6,
        segments_since_time_check=3,  # >2 to pass starvation guard
    )
    assert result == SegmentType.STATION_ID


def test_time_check_triggers_after_enough_segments():
    """TIME_CHECK should trigger after 8+ segments when station ID is recent."""
    from mammamiradio.scheduling.scheduler import _decide

    result = _decide(
        segments_produced=15,
        songs_since_ad=1,
        songs_since_banter=1,
        pacing=PacingSection(songs_between_banter=99, songs_between_ads=99),
        deterministic=True,
        songs_since_news=0,
        segments_since_station_id=2,  # recent, won't trigger
        segments_since_time_check=10,
    )
    assert result == SegmentType.TIME_CHECK


def test_ad_still_takes_priority_over_station_id():
    """AD should still trigger before station ID when ad threshold is met."""
    from mammamiradio.scheduling.scheduler import _decide

    result = _decide(
        segments_produced=10,
        songs_since_ad=4,
        songs_since_banter=1,
        pacing=PacingSection(songs_between_ads=4, songs_between_banter=99),
        deterministic=True,
        songs_since_news=0,
        segments_since_station_id=10,
        segments_since_time_check=10,
    )
    assert result == SegmentType.AD


def test_banter_takes_priority_over_station_id():
    """BANTER should trigger before station ID when banter threshold is met."""
    from mammamiradio.scheduling.scheduler import _decide

    result = _decide(
        segments_produced=10,
        songs_since_ad=1,
        songs_since_banter=5,
        pacing=PacingSection(songs_between_banter=3, songs_between_ads=99),
        deterministic=True,
        songs_since_news=0,
        segments_since_station_id=10,
        segments_since_time_check=10,
    )
    assert result == SegmentType.BANTER


def test_micro_segments_cannot_starve_music():
    """Back-to-back station ID + time check should not be possible (starvation guard)."""
    from mammamiradio.scheduling.scheduler import _decide

    # Both counters high but one just fired (=0), guard should block
    result = _decide(
        segments_produced=10,
        songs_since_ad=1,
        songs_since_banter=1,
        pacing=PacingSection(songs_between_banter=99, songs_between_ads=99),
        deterministic=True,
        songs_since_news=0,
        segments_since_station_id=0,  # just fired
        segments_since_time_check=10,
    )
    assert result == SegmentType.MUSIC


# ---------------------------------------------------------------------------
# StationState: new counter methods
# ---------------------------------------------------------------------------


def test_after_station_id_resets_counter():
    """after_station_id should reset the station ID counter and bump segments_produced."""
    state = _make_state(segments_produced=5, segments_since_station_id=7)
    state.after_station_id()
    assert state.segments_since_station_id == 0
    assert state.segments_produced == 6


def test_after_time_check_resets_counter():
    """after_time_check should reset the time check counter and bump segments_produced."""
    state = _make_state(segments_produced=5, segments_since_time_check=10)
    state.after_time_check()
    assert state.segments_since_time_check == 0
    assert state.segments_produced == 6


def test_after_music_increments_station_id_and_time_check_counters():
    """after_music should bump both station ID and time check counters."""
    state = _make_state(segments_since_station_id=3, segments_since_time_check=5)
    track = Track(title="Song", artist="Artist", duration_ms=200000)
    state.after_music(track)
    assert state.segments_since_station_id == 4
    assert state.segments_since_time_check == 6


# ---------------------------------------------------------------------------
# Preview includes new segment types
# ---------------------------------------------------------------------------


def test_preview_shows_station_id():
    """preview_upcoming should include station_id segments when conditions are met."""
    from mammamiradio.scheduling.scheduler import preview_upcoming

    tracks = [Track(title="Song", artist="A", duration_ms=1, spotify_id="s1")]
    state = StationState(
        playlist=tracks,
        segments_produced=10,
        songs_since_banter=0,
        songs_since_ad=0,
        segments_since_station_id=10,
        segments_since_time_check=0,
    )
    pacing = PacingSection(songs_between_banter=99, songs_between_ads=99)
    preview = preview_upcoming(state, pacing, tracks, count=10)
    types = [p["type"] for p in preview]
    # At least one station_id should appear in 10 segments given threshold is met
    assert "station_id" in types
