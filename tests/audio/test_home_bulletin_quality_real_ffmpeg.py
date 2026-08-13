"""Real-FFmpeg duration boundaries for fully mixed Casa audio."""

from __future__ import annotations

import subprocess

import pytest

from mammamiradio.audio.audio_quality import AudioQualityError, validate_segment_audio
from mammamiradio.core.models import SegmentType

pytestmark = pytest.mark.requires_ffmpeg


def _make_tone(path, duration_sec: float) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration_sec}:sample_rate=8000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


@pytest.mark.parametrize(
    ("duration", "error"),
    [(29.99, "too short"), (30.0, None), (45.0, None), (45.01, "too long")],
)
def test_home_bulletin_final_audio_duration_boundaries(tmp_path, duration, error):
    audio = tmp_path / f"casa-{duration}.wav"
    _make_tone(audio, duration)

    if error is None:
        validate_segment_audio(audio, SegmentType.HOME_BULLETIN)
    else:
        with pytest.raises(AudioQualityError, match=error):
            validate_segment_audio(audio, SegmentType.HOME_BULLETIN)
