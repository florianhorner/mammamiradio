"""Extended unit tests for normalizer.py.

Covers generate_tone, generate_sfx, generate_music_bed, mix_with_bed,
and generate_bumper_jingle.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mammamiradio.normalizer import (
    generate_bumper_jingle,
    generate_music_bed,
    generate_sfx,
    generate_tone,
    mix_with_bed,
)


@pytest.fixture
def mock_subprocess():
    """Patch subprocess.run to return success by default."""
    completed = MagicMock(spec=subprocess.CompletedProcess)
    completed.returncode = 0
    completed.stderr = b""
    completed.stdout = b""

    with patch("mammamiradio.normalizer.subprocess.run", return_value=completed) as mock_run:
        yield mock_run, completed


# ---------------------------------------------------------------------------
# generate_tone
# ---------------------------------------------------------------------------


def test_generate_tone_default_params(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/tone.mp3")
    result = generate_tone(out)
    assert result == out
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "sine=frequency=880" in joined
    assert "duration=0.5" in joined


def test_generate_tone_custom_params(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/tone.mp3")
    generate_tone(out, freq_hz=440, duration_sec=2.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "sine=frequency=440" in joined
    assert "duration=2.0" in joined


def test_generate_tone_fade_capped_for_short_duration(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/tone.mp3")
    generate_tone(out, duration_sec=0.2)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    # Fade should be 0.2/3 ≈ 0.0667, capped to that
    assert "afade" in joined


# ---------------------------------------------------------------------------
# generate_sfx
# ---------------------------------------------------------------------------


def test_generate_sfx_prerecorded(tmp_path, mock_subprocess):
    """SFX from pre-recorded file takes priority."""
    sfx_dir = tmp_path / "sfx"
    sfx_dir.mkdir()
    source = sfx_dir / "chime.mp3"
    source.write_bytes(b"fake-audio-data")
    out = tmp_path / "out.mp3"

    result = generate_sfx(out, "chime", sfx_dir)
    assert result == out
    assert out.read_bytes() == b"fake-audio-data"
    # Should NOT have called ffmpeg
    mock_subprocess[0].assert_not_called()


def test_generate_sfx_prerecorded_wav(tmp_path, mock_subprocess):
    """Falls back to .wav extension."""
    sfx_dir = tmp_path / "sfx"
    sfx_dir.mkdir()
    (sfx_dir / "ding.wav").write_bytes(b"wav-data")
    out = tmp_path / "out.mp3"

    result = generate_sfx(out, "ding", sfx_dir)
    assert result == out
    assert out.read_bytes() == b"wav-data"


def test_generate_sfx_chime_synthetic(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    generate_sfx(out, "chime")
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "sine=frequency=880" in joined


def test_generate_sfx_ding_synthetic(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    generate_sfx(out, "ding")
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "880" in joined


def test_generate_sfx_cash_register(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    generate_sfx(out, "cash_register")
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "1200" in joined


def test_generate_sfx_sweep(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    generate_sfx(out, "sweep")
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "sine=frequency=300" in joined


def test_generate_sfx_whoosh(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    generate_sfx(out, "whoosh")
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "300" in joined


def test_generate_sfx_unknown_type(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    generate_sfx(out, "explosion")
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    # Falls back to chime (880 Hz)
    assert "880" in joined


def test_generate_sfx_no_sfx_dir(mock_subprocess):
    """When sfx_dir is None, always uses synthetic."""
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    generate_sfx(out, "chime", sfx_dir=None)
    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# generate_music_bed
# ---------------------------------------------------------------------------


def test_generate_music_bed_lounge(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "lounge", 10.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "sine=frequency=220" in joined
    assert "tremolo" in joined
    assert "volume=0.15" in joined


def test_generate_music_bed_dramatic(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "dramatic", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "sine=frequency=80" in joined


def test_generate_music_bed_upbeat(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "upbeat", 8.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "sine=frequency=440" in joined


def test_generate_music_bed_mysterious(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "mysterious", 6.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "sine=frequency=100" in joined


def test_generate_music_bed_epic(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "epic", 12.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "sine=frequency=60" in joined


def test_generate_music_bed_unknown_mood_defaults_to_lounge(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "nonexistent_mood", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "sine=frequency=220" in joined


def test_generate_music_bed_fade_out_capped(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "lounge", 2.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    # fade_out = min(1.5, 2.0/3) = 0.667
    assert "afade=t=out" in joined


# ---------------------------------------------------------------------------
# mix_with_bed
# ---------------------------------------------------------------------------


def test_mix_with_bed_builds_filter(mock_subprocess):
    mock_run, _ = mock_subprocess
    voice = Path("/tmp/voice.mp3")
    bed = Path("/tmp/bed.mp3")
    out = Path("/tmp/mixed.mp3")

    result = mix_with_bed(voice, bed, out)
    assert result == out

    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert str(voice) in joined
    assert str(bed) in joined
    assert "volume=0.12" in joined
    assert "amix=inputs=2" in joined


# ---------------------------------------------------------------------------
# generate_bumper_jingle
# ---------------------------------------------------------------------------


def test_generate_bumper_jingle_default(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bumper.mp3")
    result = generate_bumper_jingle(out)
    assert result == out
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    # Should have C5, E5, G5 frequencies
    assert "523" in joined
    assert "659" in joined
    assert "784" in joined


def test_generate_bumper_jingle_custom_duration(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bumper.mp3")
    generate_bumper_jingle(out, duration_sec=0.8)
    cmd = mock_run.call_args[0][0]
    # -t flag with custom duration
    t_idx = cmd.index("-t")
    assert cmd[t_idx + 1] == "0.8"
