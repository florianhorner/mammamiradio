"""Tests for mammamiradio.audio_quality quality gate."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from mammamiradio.audio_quality import (
    AudioQualityError,
    AudioToolError,
    _probe_duration_sec,
    _probe_silence,
    _probe_volume,
    validate_segment_audio,
)
from mammamiradio.models import SegmentType


def _cp(*, returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _mk_audio(path: Path) -> Path:
    path.write_bytes(b"\x01" * 4096)
    return path


def test_validate_segment_audio_passes(tmp_path):
    audio = _mk_audio(tmp_path / "ok.mp3")

    def _run(cmd, capture_output, text, check):
        joined = " ".join(cmd)
        if "ffprobe" in joined:
            return _cp(stdout="12.0\n")
        if "silencedetect" in joined:
            return _cp(stderr="silence_duration: 0.6\nsilence_duration: 0.4\n")
        if "volumedetect" in joined:
            return _cp(stderr="mean_volume: -20.0 dB\nmax_volume: -3.0 dB\n")
        raise AssertionError(f"Unexpected command: {joined}")

    with patch("mammamiradio.audio_quality.subprocess.run", side_effect=_run):
        validate_segment_audio(audio, SegmentType.AD)


def test_validate_segment_audio_rejects_short_duration(tmp_path):
    audio = _mk_audio(tmp_path / "short.mp3")

    def _run(cmd, capture_output, text, check):
        joined = " ".join(cmd)
        if "ffprobe" in joined:
            return _cp(stdout="2.5\n")
        if "silencedetect" in joined:
            return _cp(stderr="")
        if "volumedetect" in joined:
            return _cp(stderr="mean_volume: -20.0 dB\nmax_volume: -3.0 dB\n")
        raise AssertionError(f"Unexpected command: {joined}")

    with (
        patch("mammamiradio.audio_quality.subprocess.run", side_effect=_run),
        pytest.raises(AudioQualityError, match="too short"),
    ):
        validate_segment_audio(audio, SegmentType.BANTER)


def test_validate_segment_audio_rejects_high_silence_ratio(tmp_path):
    audio = _mk_audio(tmp_path / "silence.mp3")

    def _run(cmd, capture_output, text, check):
        joined = " ".join(cmd)
        if "ffprobe" in joined:
            return _cp(stdout="10.0\n")
        if "silencedetect" in joined:
            return _cp(stderr="silence_duration: 5.0\n")
        if "volumedetect" in joined:
            return _cp(stderr="mean_volume: -15.0 dB\nmax_volume: -2.0 dB\n")
        raise AssertionError(f"Unexpected command: {joined}")

    with (
        patch("mammamiradio.audio_quality.subprocess.run", side_effect=_run),
        pytest.raises(AudioQualityError, match="too much silence"),
    ):
        validate_segment_audio(audio, SegmentType.BANTER)


def test_validate_segment_audio_rejects_very_quiet_signal(tmp_path):
    audio = _mk_audio(tmp_path / "quiet.mp3")

    def _run(cmd, capture_output, text, check):
        joined = " ".join(cmd)
        if "ffprobe" in joined:
            return _cp(stdout="12.0\n")
        if "silencedetect" in joined:
            return _cp(stderr="silence_duration: 0.0\n")
        if "volumedetect" in joined:
            return _cp(stderr="mean_volume: -55.0 dB\nmax_volume: -40.0 dB\n")
        raise AssertionError(f"Unexpected command: {joined}")

    with (
        patch("mammamiradio.audio_quality.subprocess.run", side_effect=_run),
        pytest.raises(AudioQualityError, match="too quiet"),
    ):
        validate_segment_audio(audio, SegmentType.AD)


# ── AudioToolError tests ──────────────────────────────────────────────────────


def test_probe_duration_raises_audio_tool_error_on_ffprobe_failure(tmp_path):
    _fail = _cp(returncode=1, stderr="ffprobe: not found")
    with (
        patch("mammamiradio.audio_quality.subprocess.run", return_value=_fail),
        pytest.raises(AudioToolError),
    ):
        _probe_duration_sec(tmp_path / "x.mp3")


def test_probe_silence_raises_audio_tool_error_on_ffmpeg_failure(tmp_path):
    _fail = _cp(returncode=1, stderr="ffmpeg: not found")
    with (
        patch("mammamiradio.audio_quality.subprocess.run", return_value=_fail),
        pytest.raises(AudioToolError),
    ):
        _probe_silence(tmp_path / "x.mp3")


def test_probe_volume_raises_audio_tool_error_on_ffmpeg_failure(tmp_path):
    _fail = _cp(returncode=1, stderr="ffmpeg: not found")
    with (
        patch("mammamiradio.audio_quality.subprocess.run", return_value=_fail),
        pytest.raises(AudioToolError),
    ):
        _probe_volume(tmp_path / "x.mp3")


def test_validate_segment_audio_propagates_tool_error(tmp_path):
    """AudioToolError must propagate out of validate_segment_audio, not be swallowed."""
    audio = _mk_audio(tmp_path / "ok.mp3")
    _fail = _cp(returncode=1, stderr="ffprobe gone")
    with (
        patch("mammamiradio.audio_quality.subprocess.run", return_value=_fail),
        pytest.raises(AudioToolError),
    ):
        validate_segment_audio(audio, SegmentType.BANTER)


# ── MUSIC threshold tests ─────────────────────────────────────────────────────


def test_music_segment_rejected_if_too_short(tmp_path):
    """MUSIC files under 30s should be rejected as likely truncated/placeholder."""
    audio = _mk_audio(tmp_path / "short.mp3")

    def _run(cmd, capture_output, text, check):
        joined = " ".join(cmd)
        if "ffprobe" in joined:
            return _cp(stdout="10.0\n")
        if "silencedetect" in joined:
            return _cp(stderr="")
        if "volumedetect" in joined:
            return _cp(stderr="mean_volume: -20.0 dB\nmax_volume: -3.0 dB\n")
        raise AssertionError(f"Unexpected command: {joined}")

    with (
        patch("mammamiradio.audio_quality.subprocess.run", side_effect=_run),
        pytest.raises(AudioQualityError, match="too short"),
    ):
        validate_segment_audio(audio, SegmentType.MUSIC)


def test_music_segment_passes_at_30s(tmp_path):
    """MUSIC files at exactly 30s should pass the permissive gate."""
    audio = _mk_audio(tmp_path / "ok.mp3")

    def _run(cmd, capture_output, text, check):
        joined = " ".join(cmd)
        if "ffprobe" in joined:
            return _cp(stdout="30.0\n")
        if "silencedetect" in joined:
            return _cp(stderr="silence_duration: 2.0\n")
        if "volumedetect" in joined:
            return _cp(stderr="mean_volume: -25.0 dB\nmax_volume: -5.0 dB\n")
        raise AssertionError(f"Unexpected command: {joined}")

    with patch("mammamiradio.audio_quality.subprocess.run", side_effect=_run):
        validate_segment_audio(audio, SegmentType.MUSIC)  # must not raise


# ── Edge cases: missing / too-small file ────────────────────────────────────


def test_validate_segment_audio_raises_on_missing_file(tmp_path):
    """Raise AudioQualityError when the audio file does not exist."""
    missing = tmp_path / "does_not_exist.mp3"
    with pytest.raises(AudioQualityError, match="audio missing"):
        validate_segment_audio(missing, SegmentType.BANTER)


def test_validate_segment_audio_raises_on_too_small_file(tmp_path):
    """Raise AudioQualityError when the audio file is smaller than 1 KiB."""
    tiny = tmp_path / "tiny.mp3"
    tiny.write_bytes(b"\x00" * 512)  # 512 bytes < 1024 threshold
    with pytest.raises(AudioQualityError, match="too small"):
        validate_segment_audio(tiny, SegmentType.BANTER)


# ── Edge case: long silent gap ───────────────────────────────────────────────


def test_validate_segment_audio_rejects_long_silent_gap(tmp_path):
    """Raise AudioQualityError when a single silence span exceeds the threshold.

    BANTER thresholds: max_silence_ratio=35%, max_silence_span_sec=3.0s.
    Use a 20s clip with 4.5s total silence (ratio=22.5% — passes the ratio
    check) but one span of 4.0s (> 3.0s — triggers the span check).
    """
    audio = _mk_audio(tmp_path / "gap.mp3")

    def _run(cmd, capture_output, text, check):
        joined = " ".join(cmd)
        if "ffprobe" in joined:
            return _cp(stdout="20.0\n")
        if "silencedetect" in joined:
            # total=4.5s (22.5% ratio, below 35% limit), max span=4.0s (>3.0s limit)
            return _cp(stderr="silence_duration: 0.5\nsilence_duration: 4.0\n")
        if "volumedetect" in joined:
            return _cp(stderr="mean_volume: -20.0 dB\nmax_volume: -3.0 dB\n")
        raise AssertionError(f"Unexpected command: {joined}")

    with (
        patch("mammamiradio.audio_quality.subprocess.run", side_effect=_run),
        pytest.raises(AudioQualityError, match="long silent gap"),
    ):
        validate_segment_audio(audio, SegmentType.BANTER)


# ── Edge case: invalid ffprobe duration output ───────────────────────────────


def test_probe_duration_raises_on_invalid_ffprobe_output(tmp_path):
    """Raise AudioQualityError when ffprobe returns non-numeric duration."""
    _invalid = _cp(returncode=0, stdout="not-a-number\n")
    with (
        patch("mammamiradio.audio_quality.subprocess.run", return_value=_invalid),
        pytest.raises(AudioQualityError, match="Could not parse duration"),
    ):
        _probe_duration_sec(tmp_path / "x.mp3")


# ── Edge case: no silence detected ──────────────────────────────────────────


def test_probe_silence_returns_zeros_when_no_silence_detected(tmp_path):
    """Return (0.0, 0.0) when ffmpeg produces no silence_duration lines."""
    _silent_free = _cp(returncode=0, stderr="frame=  100\n")
    with patch("mammamiradio.audio_quality.subprocess.run", return_value=_silent_free):
        total, span = _probe_silence(tmp_path / "x.mp3")
    assert total == 0.0
    assert span == 0.0
