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


# ── Additional edge-case coverage ────────────────────────────────────────────


def test_validate_segment_audio_raises_if_file_missing(tmp_path):
    """Missing audio file raises AudioQualityError immediately."""
    with pytest.raises(AudioQualityError, match="missing"):
        validate_segment_audio(tmp_path / "nonexistent.mp3", SegmentType.BANTER)


def test_validate_segment_audio_raises_if_file_too_small(tmp_path):
    """A file smaller than 1024 bytes raises AudioQualityError."""
    tiny = tmp_path / "tiny.mp3"
    tiny.write_bytes(b"\xff" * 100)
    with pytest.raises(AudioQualityError, match="too small"):
        validate_segment_audio(tiny, SegmentType.BANTER)


def test_validate_segment_audio_raises_on_long_silence_span(tmp_path):
    """A banter segment with a long silent span raises AudioQualityError.

    Total silence is kept below the ratio threshold so the span check is reached.
    """
    audio = _mk_audio(tmp_path / "ok.mp3")

    def _run(cmd, capture_output, text, check):
        joined = " ".join(cmd)
        if "ffprobe" in joined:
            return _cp(stdout="60.0\n")  # 60s so a 5s silence is only 8% ratio
        if "silencedetect" in joined:
            return _cp(stderr="silence_duration: 5.0\n")  # below ratio, above span
        if "volumedetect" in joined:
            return _cp(stderr="mean_volume: -20.0 dB\nmax_volume: -3.0 dB\n")
        raise AssertionError(f"Unexpected command: {joined}")

    with (
        patch("mammamiradio.audio_quality.subprocess.run", side_effect=_run),
        pytest.raises(AudioQualityError, match="silent gap"),
    ):
        validate_segment_audio(audio, SegmentType.BANTER)


def test_probe_duration_raises_on_invalid_float_output(tmp_path):
    """_probe_duration_sec raises AudioQualityError when ffprobe output is not a float."""
    with (
        patch(
            "mammamiradio.audio_quality.subprocess.run",
            return_value=_cp(returncode=0, stdout="not-a-float\n"),
        ),
        pytest.raises(AudioQualityError, match="Could not parse"),
    ):
        _probe_duration_sec(tmp_path / "x.mp3")


def test_probe_silence_returns_zero_when_no_silence_detected(tmp_path):
    """_probe_silence returns (0.0, 0.0) when ffmpeg produces no silence_duration lines."""
    with patch(
        "mammamiradio.audio_quality.subprocess.run",
        return_value=_cp(returncode=0, stderr=""),
    ):
        total, maximum = _probe_silence(tmp_path / "x.mp3")
    assert total == 0.0
    assert maximum == 0.0
