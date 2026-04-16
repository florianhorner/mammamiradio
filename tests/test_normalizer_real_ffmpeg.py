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
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", "3",
            "-acodec", "libmp3lame", "-ab", "128k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


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
