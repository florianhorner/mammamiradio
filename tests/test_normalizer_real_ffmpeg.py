"""Real-ffmpeg integration tests for the normalizer filter chain.

These tests call actual ffmpeg — no subprocess mocks. They exist to catch
crashes (SIGABRT, non-zero exits) that mocked tests cannot detect, such as
the psymodel.c:576 SIGABRT triggered by 3 equalizers + loudnorm on ffmpeg 8.x
aarch64.

Run by the pi-smoke CI job on ubuntu-24.04-arm to exercise the filter chain
on real ARM hardware with the system ffmpeg.

Skipped automatically when ffmpeg is not in PATH.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from mammamiradio.normalizer import normalize

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg not in PATH — skipping real-ffmpeg integration tests",
)


def _make_silent_mp3(path) -> None:
    """Generate a 3-second silent MP3 using ffmpeg lavfi source."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "3",
            "-acodec",
            "libmp3lame",
            "-ab",
            "128k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_tone_mp3(path, duration_sec: float = 2.0, freq: int = 440) -> None:
    """Generate a constant-amplitude sine tone — used to verify fade-in shapes."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={duration_sec}:sample_rate=44100",
            "-ac",
            "2",
            "-acodec",
            "libmp3lame",
            "-ab",
            "128k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _measure_rms(path, start_sec: float, window_sec: float) -> float:
    """Return the RMS volume (linear, 0-1) of an mp3 window via ffmpeg volumedetect."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-ss",
            f"{start_sec}",
            "-i",
            str(path),
            "-t",
            f"{window_sec}",
            "-filter:a",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        check=False,
        capture_output=True,
    )
    stderr = result.stderr.decode(errors="ignore")
    # volumedetect reports "mean_volume: -X.X dB"
    import re

    m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", stderr)
    if not m:
        return 0.0
    db = float(m.group(1))
    return 10 ** (db / 20.0)


def test_normalize_music_eq_chain_does_not_crash_real_ffmpeg(tmp_path):
    """The music_eq filter chain must not crash ffmpeg (no SIGABRT, exit 0).

    Three equalizers + loudnorm trigger psymodel.c:576 SIGABRT in ffmpeg 8.x
    on aarch64. This test exercises the real filter chain on real ffmpeg to
    catch that crash class before it reaches production Pi/HA Green installs.
    """
    src = tmp_path / "input.mp3"
    out = tmp_path / "output.mp3"
    _make_silent_mp3(src)

    # Must not raise — a SIGABRT or ffmpeg error raises CalledProcessError
    normalize(src, out, loudnorm=True, music_eq=True)

    assert out.exists(), "normalize() produced no output file"
    assert out.stat().st_size > 0, "normalize() produced an empty output file"


def test_normalize_no_music_eq_does_not_crash_real_ffmpeg(tmp_path):
    """The standard loudnorm-only path must also complete without error."""
    src = tmp_path / "input.mp3"
    out = tmp_path / "output.mp3"
    _make_silent_mp3(src)

    normalize(src, out, loudnorm=True, music_eq=False)

    assert out.exists()
    assert out.stat().st_size > 0


def test_normalize_applies_soft_fade_in_on_real_audio(tmp_path):
    """End-to-end: a constant-amplitude tone normalized via the final-output path
    must show a measurable amplitude ramp in the first 250ms, so music→voice
    hand-offs in the stream aren't hard cuts.

    Guards against the 2026-04-21 regression Florian flagged ("the fade overs
    from song to speakers arent soft there is a noticable drop").
    """
    src = tmp_path / "tone.mp3"
    out = tmp_path / "tone_norm.mp3"
    _make_tone_mp3(src, duration_sec=2.0)

    normalize(src, out, loudnorm=True, music_eq=False)
    assert out.exists() and out.stat().st_size > 0

    # First 100ms should be significantly quieter than mid-track.
    head_rms = _measure_rms(out, start_sec=0.0, window_sec=0.1)
    mid_rms = _measure_rms(out, start_sec=1.0, window_sec=0.2)
    # fade is linear over 250ms; first 100ms averages ~20% of target amplitude
    # (rough — allow wide margin for ffmpeg encoding + loudnorm variability).
    assert mid_rms > 0, "mid-track RMS should be non-zero on a steady tone"
    assert head_rms < mid_rms * 0.6, (
        f"first 100ms ({head_rms:.4f}) should be noticeably quieter than mid-track ({mid_rms:.4f}) due to 250ms fade-in"
    )
