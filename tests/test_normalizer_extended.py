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
    generate_brand_motif,
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
    assert "aevalsrc=" in joined
    assert "0.2*sin(2*PI*300*0.6/log(10)*((10)^(t/0.6)-1))" in joined


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


# ---------------------------------------------------------------------------
# New music bed types (signature ad system)
# ---------------------------------------------------------------------------


def test_generate_music_bed_tarantella_pop(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "tarantella_pop", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "sine=frequency=523" in joined


def test_generate_music_bed_cheap_synth_romance(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "cheap_synth_romance", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "sine=frequency=300" in joined


def test_generate_music_bed_suspicious_jazz(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "suspicious_jazz", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "sine=frequency=220" in joined
    assert "sine=frequency=277" in joined


def test_generate_music_bed_discount_techno(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "discount_techno", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "sine=frequency=440" in joined
    assert "sine=frequency=880" in joined


def test_generate_music_bed_environment_cafe(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "cafe", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "sine=frequency=180" in joined


# ---------------------------------------------------------------------------
# New SFX types (signature ad system)
# ---------------------------------------------------------------------------


def test_generate_sfx_tape_stop(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    generate_sfx(out, "tape_stop")
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    # Descending sweep (2000 -> 80)
    assert "aevalsrc=" in joined


def test_generate_sfx_hotline_beep(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    generate_sfx(out, "hotline_beep")
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "1336" in joined


def test_generate_sfx_mandolin_sting(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    generate_sfx(out, "mandolin_sting")
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined


def test_generate_sfx_ice_clink(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    generate_sfx(out, "ice_clink")
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "2400" in joined


def test_generate_sfx_startup_synth(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    generate_sfx(out, "startup_synth")
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined


def test_generate_sfx_register_hit(mock_subprocess):
    """register_hit is an alias for cash_register."""
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    generate_sfx(out, "register_hit")
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "1200" in joined


# ---------------------------------------------------------------------------
# mix_with_bed volume_scale (signature ad system)
# ---------------------------------------------------------------------------


def test_mix_with_bed_custom_volume_scale(mock_subprocess):
    mock_run, _ = mock_subprocess
    voice = Path("/tmp/voice.mp3")
    bed = Path("/tmp/bed.mp3")
    out = Path("/tmp/mixed.mp3")

    mix_with_bed(voice, bed, out, volume_scale=0.06)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "volume=0.06" in joined


# ---------------------------------------------------------------------------
# generate_brand_motif (signature ad system)
# ---------------------------------------------------------------------------


def test_generate_brand_motif_parses_and_concats(tmp_path, mock_subprocess):
    mock_run, completed = mock_subprocess

    def _create_output(cmd, **kwargs):
        for i, arg in enumerate(cmd):
            if arg == "-f" and i + 1 < len(cmd) and cmd[i + 1] == "mp3":
                Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
                Path(cmd[-1]).write_bytes(b"\x00" * 64)
                break
        return completed

    mock_run.side_effect = _create_output

    out = tmp_path / "motif.mp3"
    generate_brand_motif(out, "ice_clink+startup_synth")
    # Should have called ffmpeg multiple times (2 SFX + concat)
    assert mock_run.call_count >= 2


def test_generate_brand_motif_single_component(tmp_path, mock_subprocess):
    mock_run, completed = mock_subprocess

    # Make subprocess.run create the output file so shutil.move can find it
    def _create_output(cmd, **kwargs):
        for i, arg in enumerate(cmd):
            if arg == "-f" and i + 1 < len(cmd) and cmd[i + 1] == "mp3":
                # The output path is the last argument
                Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
                Path(cmd[-1]).write_bytes(b"\x00" * 64)
                break
        return completed

    mock_run.side_effect = _create_output

    out = tmp_path / "motif.mp3"
    generate_brand_motif(out, "chime")
    assert out.exists()


def test_generate_brand_motif_empty_signature_raises():
    with pytest.raises(ValueError, match="Empty sonic_signature"):
        generate_brand_motif(Path("/tmp/motif.mp3"), "")
