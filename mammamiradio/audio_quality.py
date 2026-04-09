"""Audio quality gate for spoken segments before they reach the live queue."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mammamiradio.models import SegmentType


class AudioQualityError(RuntimeError):
    """Raised when rendered segment audio does not pass station quality checks."""


class AudioToolError(RuntimeError):
    """Raised when an audio tool (ffprobe/ffmpeg) is unavailable or crashes.

    Distinct from AudioQualityError so callers can pass-through on ops failures
    without silently dropping content that was never actually checked.
    """


@dataclass(frozen=True)
class QualityThresholds:
    """Per-segment thresholds used by the quality gate."""

    min_duration_sec: float
    max_silence_ratio: float
    max_silence_span_sec: float
    min_mean_volume_db: float
    min_peak_volume_db: float


_DEFAULT_THRESHOLDS = QualityThresholds(
    min_duration_sec=3.0,
    max_silence_ratio=0.50,
    max_silence_span_sec=6.0,
    min_mean_volume_db=-42.0,
    min_peak_volume_db=-24.0,
)

_THRESHOLDS_BY_TYPE: dict[SegmentType, QualityThresholds] = {
    SegmentType.BANTER: QualityThresholds(
        min_duration_sec=4.0,
        max_silence_ratio=0.35,
        max_silence_span_sec=3.0,
        min_mean_volume_db=-38.0,
        min_peak_volume_db=-20.0,
    ),
    SegmentType.AD: QualityThresholds(
        min_duration_sec=8.0,
        max_silence_ratio=0.25,
        max_silence_span_sec=2.5,
        min_mean_volume_db=-36.0,
        min_peak_volume_db=-18.0,
    ),
    # Permissive: only reject truncated placeholders (<30s) or completely silent files.
    # yt-dlp corrupt downloads and silent placeholders are typically <10s.
    SegmentType.MUSIC: QualityThresholds(
        min_duration_sec=30.0,
        max_silence_ratio=0.95,
        max_silence_span_sec=300.0,
        min_mean_volume_db=-60.0,
        min_peak_volume_db=-50.0,
    ),
}


def validate_segment_audio(path: Path, seg_type: SegmentType) -> None:
    """Validate audio for banter/ad segments and raise on quality failure."""
    th = _THRESHOLDS_BY_TYPE.get(seg_type, _DEFAULT_THRESHOLDS)

    if not path.exists():
        raise AudioQualityError(f"{seg_type.value} audio missing: {path}")
    if path.stat().st_size < 1024:
        raise AudioQualityError(f"{seg_type.value} audio is too small ({path.stat().st_size} bytes)")

    duration = _probe_duration_sec(path)
    if duration < th.min_duration_sec:
        raise AudioQualityError(f"{seg_type.value} audio too short ({duration:.2f}s < {th.min_duration_sec:.2f}s)")

    silence_total, max_silence = _probe_silence(path)
    silence_ratio = silence_total / duration if duration > 0 else 1.0
    if silence_ratio > th.max_silence_ratio:
        raise AudioQualityError(
            f"{seg_type.value} has too much silence ({silence_ratio:.0%} > {th.max_silence_ratio:.0%})"
        )
    if max_silence > th.max_silence_span_sec:
        raise AudioQualityError(
            f"{seg_type.value} has a long silent gap ({max_silence:.2f}s > {th.max_silence_span_sec:.2f}s)"
        )

    mean_db, peak_db = _probe_volume(path)
    if (
        mean_db is not None
        and peak_db is not None
        and mean_db < th.min_mean_volume_db
        and peak_db < th.min_peak_volume_db
    ):
        raise AudioQualityError(f"{seg_type.value} is too quiet (mean={mean_db:.1f}dB, peak={peak_db:.1f}dB)")


def _probe_duration_sec(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AudioToolError(f"ffprobe failed for duration: {result.stderr.strip() or result.stdout.strip()}")
    raw = result.stdout.strip()
    try:
        return float(raw)
    except ValueError as exc:
        raise AudioQualityError(f"Could not parse duration from ffprobe output: {raw!r}") from exc


def _probe_silence(path: Path) -> tuple[float, float]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "silencedetect=n=-38dB:d=0.8",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AudioToolError(f"ffmpeg silencedetect failed: {result.stderr.strip()[-240:]}")
    matches = re.findall(r"silence_duration:\s*([0-9.]+)", result.stderr)
    if not matches:
        return 0.0, 0.0
    durations = [float(v) for v in matches]
    return sum(durations), max(durations)


def _probe_volume(path: Path) -> tuple[float | None, float | None]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AudioToolError(f"ffmpeg volumedetect failed: {result.stderr.strip()[-240:]}")
    mean_match = re.search(r"mean_volume:\s*(-?[0-9.]+)\s*dB", result.stderr)
    peak_match = re.search(r"max_volume:\s*(-?[0-9.]+)\s*dB", result.stderr)
    mean_db = float(mean_match.group(1)) if mean_match else None
    peak_db = float(peak_match.group(1)) if peak_match else None
    return mean_db, peak_db
