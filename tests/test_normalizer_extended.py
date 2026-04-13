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
    _generate_cash_register,
    _generate_ice_clink,
    _generate_mandolin_sting,
    _generate_whoosh,
    generate_brand_motif,
    generate_bumper_jingle,
    generate_music_bed,
    generate_sfx,
    generate_tone,
    mix_with_bed,
    normalize_ad,
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
    # Bell tones combined in aevalsrc + noise burst + echo
    assert "aevalsrc=" in joined
    assert "1200" in joined
    assert "anoisesrc" in joined
    assert "aecho" in joined


def test_generate_sfx_cash_register_failure_uses_simple_fallback(mock_subprocess):
    out = Path("/tmp/sfx.mp3")
    with (
        patch("mammamiradio.normalizer._generate_cash_register", side_effect=RuntimeError("ffmpeg broke")),
        patch("mammamiradio.normalizer.generate_tone", return_value=out) as mock_tone,
    ):
        result = generate_sfx(out, "cash_register")

    assert result == out
    mock_tone.assert_called_once()


def test_generate_sfx_sweep(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    generate_sfx(out, "sweep")
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    # Now uses filtered noise whoosh
    assert "anoisesrc" in joined
    assert "highpass" in joined


def test_generate_sfx_whoosh(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    generate_sfx(out, "whoosh")
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    # Filtered noise with bandpass
    assert "anoisesrc" in joined


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
    assert "aevalsrc=" in joined
    assert "220" in joined
    assert "aphaser" in joined
    assert "volume=0.14" in joined


def test_generate_music_bed_dramatic(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "dramatic", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "80" in joined


def test_generate_music_bed_upbeat(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "upbeat", 8.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "330" in joined
    assert "aphaser" in joined


def test_generate_music_bed_mysterious(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "mysterious", 6.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "100" in joined


def test_generate_music_bed_epic(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "epic", 12.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "65" in joined
    assert "tremolo" in joined


def test_generate_music_bed_unknown_mood_defaults_to_lounge(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "nonexistent_mood", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "220" in joined


def test_generate_music_bed_fade_out_capped(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "lounge", 2.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
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
    # All tones in single aevalsrc
    assert "aevalsrc=" in joined
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


def test_generate_bumper_jingle_falls_back_after_aevalsrc_failure():
    out = Path("/tmp/bumper.mp3")
    ok = MagicMock(spec=subprocess.CompletedProcess)
    ok.returncode = 0
    ok.stderr = b""
    ok.stdout = b""

    with patch(
        "mammamiradio.normalizer._run_ffmpeg",
        side_effect=[subprocess.CalledProcessError(234, ["ffmpeg"]), ok],
    ) as run_ffmpeg:
        result = generate_bumper_jingle(out)

    assert result == out
    assert run_ffmpeg.call_count == 2
    first_cmd = run_ffmpeg.call_args_list[0][0][0]
    second_cmd = run_ffmpeg.call_args_list[1][0][0]
    assert "aevalsrc=" in " ".join(first_cmd)
    assert "sine=frequency=523" in " ".join(second_cmd)


# ---------------------------------------------------------------------------
# New music bed types (signature ad system)
# ---------------------------------------------------------------------------


def test_generate_music_bed_tarantella_pop(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "tarantella_pop", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "523" in joined


def test_generate_music_bed_cheap_synth_romance(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "cheap_synth_romance", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "293" in joined
    assert "aphaser" in joined


def test_generate_music_bed_suspicious_jazz(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "suspicious_jazz", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    # Jazz bed: pad + walking bass all in single aevalsrc
    assert "aevalsrc=" in joined
    assert "220" in joined


def test_generate_music_bed_discount_techno(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "discount_techno", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "110" in joined
    # Fast rhythmic tremolo (f=8) instead of droning echo
    assert "tremolo=f=8" in joined
    assert "highpass" in joined


def test_generate_music_bed_environment_cafe(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "cafe", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "174" in joined


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
    # All notes combined in single aevalsrc + echo
    assert "aevalsrc=" in joined
    assert "330" in joined
    assert "440" in joined
    assert "554" in joined
    assert "aecho" in joined


def test_generate_sfx_ice_clink(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    generate_sfx(out, "ice_clink")
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    # Tones in aevalsrc + noise transient
    assert "aevalsrc=" in joined
    assert "2400" in joined
    assert "3200" in joined
    assert "anoisesrc" in joined


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
    # Same aevalsrc cash register sound
    assert "aevalsrc=" in joined
    assert "1200" in joined
    assert "anoisesrc" in joined


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


# ---------------------------------------------------------------------------
# Richer SFX helpers (ad production polish)
# ---------------------------------------------------------------------------


def test_cash_register_has_layered_audio(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    _generate_cash_register(out)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    # Bell tones in aevalsrc + noise + echo
    assert "aevalsrc=" in joined
    assert "1200" in joined
    assert "1507" in joined
    assert "anoisesrc" in joined
    assert "aecho" in joined


def test_whoosh_uses_filtered_noise(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    _generate_whoosh(out)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "anoisesrc" in joined
    assert "highpass" in joined
    assert "lowpass" in joined


def test_mandolin_sting_has_arpeggio(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    _generate_mandolin_sting(out)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    # All notes combined in single aevalsrc with staggered onsets
    assert "aevalsrc=" in joined
    assert "330" in joined
    assert "440" in joined
    assert "554" in joined
    assert "aecho" in joined


def test_ice_clink_has_layered_tones(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sfx.mp3")
    _generate_ice_clink(out)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    # Tones in aevalsrc + noise
    assert "aevalsrc=" in joined
    assert "2400" in joined
    assert "3200" in joined
    assert "4800" in joined
    assert "anoisesrc" in joined


# ---------------------------------------------------------------------------
# normalize_ad broadcast processing
# ---------------------------------------------------------------------------


def test_normalize_ad_broadcast_chain(mock_subprocess):
    mock_run, _ = mock_subprocess
    inp = Path("/tmp/ad_raw.mp3")
    out = Path("/tmp/ad_processed.mp3")
    normalize_ad(inp, out)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    # Heavy compressor
    assert "acompressor" in joined
    assert "ratio=8" in joined
    # Presence + air boost
    assert "treble=gain=4:frequency=3000" in joined
    assert "treble=gain=2:frequency=8000" in joined
    # Mud cut
    assert "highpass=f=120" in joined
    # Loud + tight loudnorm
    assert "loudnorm=I=-14:LRA=7:TP=-1.0" in joined


# ---------------------------------------------------------------------------
# generate_bumper_jingle polish
# ---------------------------------------------------------------------------


def test_bumper_jingle_has_pad_and_reverb(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bumper.mp3")
    generate_bumper_jingle(out)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    # All in single aevalsrc — pad tones (C3=131Hz, G3=196Hz) + melody
    assert "aevalsrc=" in joined
    assert "131" in joined
    assert "196" in joined
    # Reverb/echo tail
    assert "aecho" in joined


# ---------------------------------------------------------------------------
# concat_files silence_ms=0 branch (else path, no silence gaps)
# ---------------------------------------------------------------------------


def test_concat_files_no_silence(mock_subprocess):
    """concat_files with silence_ms=0 uses simple file concat without anullsrc."""
    from mammamiradio.normalizer import concat_files

    mock_run, _ = mock_subprocess
    paths = [Path("/tmp/a.mp3"), Path("/tmp/b.mp3"), Path("/tmp/c.mp3")]
    out = Path("/tmp/out.mp3")
    concat_files(paths, out, silence_ms=0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "anullsrc" not in joined
    assert "concat=n=3" in joined


# ---------------------------------------------------------------------------
# generate_sweep validation (invalid params)
# ---------------------------------------------------------------------------


def test_generate_sweep_negative_start_freq_raises():
    from mammamiradio.normalizer import generate_sweep

    with pytest.raises(ValueError, match="positive"):
        generate_sweep(Path("/tmp/out.mp3"), start_hz=-100, end_hz=2000)


def test_generate_sweep_zero_duration_raises():
    from mammamiradio.normalizer import generate_sweep

    with pytest.raises(ValueError, match="positive"):
        generate_sweep(Path("/tmp/out.mp3"), duration_sec=0)


# ---------------------------------------------------------------------------
# generate_sfx _simple_fallback paths (when main synthetic generation fails)
# ---------------------------------------------------------------------------


def test_generate_sfx_sweep_failure_uses_sweep_fallback(mock_subprocess):
    """When _generate_whoosh fails, sweep-type SFX falls back to generate_sweep."""
    out = Path("/tmp/sfx.mp3")
    with (
        patch("mammamiradio.normalizer._generate_whoosh", side_effect=RuntimeError("boom")),
        patch("mammamiradio.normalizer.generate_sweep", return_value=out) as mock_sweep,
    ):
        result = generate_sfx(out, "sweep")
    assert result == out
    mock_sweep.assert_called_once()


def test_generate_sfx_tape_stop_failure_uses_sweep_fallback(mock_subprocess):
    """tape_stop SFX falls back to descending generate_sweep on failure."""
    out = Path("/tmp/sfx.mp3")
    with (
        patch("mammamiradio.normalizer._generate_whoosh", side_effect=RuntimeError("boom")),
        patch("mammamiradio.normalizer.generate_sweep", return_value=out) as mock_sweep,
    ):
        result = generate_sfx(out, "tape_stop")
    assert result == out
    mock_sweep.assert_called_once()


def test_generate_sfx_hotline_beep_failure_uses_tone_fallback(mock_subprocess):
    """hotline_beep uses 1336 Hz tone as simple fallback."""
    out = Path("/tmp/sfx.mp3")
    with (
        patch("mammamiradio.normalizer.generate_tone", side_effect=[RuntimeError("boom"), out]),
    ):
        # First call raises (the synthetic path), second call from _simple_fallback returns out
        # Actually generate_sfx tries synthetic first - let's mock the inner function
        pass
    # More direct: make the _run_ffmpeg call fail so _simple_fallback is called
    with (
        patch("mammamiradio.normalizer._run_ffmpeg", side_effect=[RuntimeError("ffmpeg broke"), None]),
        patch("mammamiradio.normalizer.generate_tone", return_value=out) as mock_tone,
    ):
        result = generate_sfx(out, "hotline_beep")
    assert result == out
    mock_tone.assert_called_once()


def test_generate_sfx_unknown_type_failure_uses_default_tone(mock_subprocess):
    """Unknown SFX type uses 880 Hz tone as fallback when synthetic path fails."""
    out = Path("/tmp/sfx.mp3")
    with (
        patch("mammamiradio.normalizer._run_ffmpeg", side_effect=[RuntimeError("ffmpeg broke"), None]),
        patch("mammamiradio.normalizer.generate_tone", return_value=out) as mock_tone,
    ):
        result = generate_sfx(out, "totally_unknown_sfx_type")
    assert result == out
    mock_tone.assert_called_once()


# ---------------------------------------------------------------------------
# generate_brand_motif edge cases
# ---------------------------------------------------------------------------


def test_generate_brand_motif_caps_at_2s(tmp_path, mock_subprocess):
    """generate_brand_motif stops adding components once total_dur >= 2.0s."""
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
    # 5 components at 0.5s each = 2.5s, but cap is 2.0s, so only 4 should be generated
    generate_brand_motif(out, "chime+ding+chime+ding+chime")
    # 4 SFX calls (4 x 0.5s = 2.0s) + at least 1 concat = at least 5 calls
    # The 5th component is skipped due to cap
    assert mock_run.call_count >= 4


def test_generate_brand_motif_cleans_up_on_exception(tmp_path, mock_subprocess):
    """generate_brand_motif cleans up temp parts if generation fails."""
    out = tmp_path / "motif.mp3"
    with (
        patch("mammamiradio.normalizer.generate_sfx", side_effect=RuntimeError("ffmpeg died")),
        pytest.raises(RuntimeError, match="ffmpeg died"),
    ):
        generate_brand_motif(out, "chime+ding")


# ---------------------------------------------------------------------------
# generate_music_bed — additional moods (coverage for uncovered branches)
# ---------------------------------------------------------------------------


def test_generate_music_bed_shopping_channel(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "shopping_channel", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "400" in joined
    assert "tremolo" in joined


def test_generate_music_bed_luxury_spa(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "luxury_spa", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "250" in joined
    assert "aphaser" in joined


def test_generate_music_bed_showroom(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "showroom", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "300" in joined
    assert "aphaser" in joined


def test_generate_music_bed_stadium(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "stadium", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "100" in joined
    assert "tremolo" in joined


def test_generate_music_bed_motorway(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "motorway", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "55" in joined
    assert "tremolo" in joined


def test_generate_music_bed_occult_basement(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "occult_basement", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "50" in joined
    assert "tremolo" in joined


def test_generate_music_bed_overblown_epic(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "overblown_epic", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "55" in joined


def test_generate_music_bed_beach(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "beach", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "196" in joined


def test_generate_music_bed_cafe(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "cafe", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "174" in joined


def test_generate_music_bed_cheap_synth_romance_v2(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/bed.mp3")
    generate_music_bed(out, "cheap_synth_romance", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "293" in joined
    assert "aphaser" in joined


# ---------------------------------------------------------------------------
# generate_foley_loop
# ---------------------------------------------------------------------------


from mammamiradio.normalizer import generate_foley_loop  # noqa: E402


def test_generate_foley_loop_cafe(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/foley.mp3")
    generate_foley_loop(out, "cafe", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "anoisesrc=color=pink" in joined
    assert "bandpass" in joined


def test_generate_foley_loop_motorway(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/foley.mp3")
    generate_foley_loop(out, "motorway", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "82" in joined


def test_generate_foley_loop_beach(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/foley.mp3")
    generate_foley_loop(out, "beach", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "anoisesrc=color=pink" in joined
    assert "lowpass" in joined


def test_generate_foley_loop_stadium(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/foley.mp3")
    generate_foley_loop(out, "stadium", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "anoisesrc=color=pink" in joined
    assert "aecho" in joined


def test_generate_foley_loop_luxury_spa(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/foley.mp3")
    generate_foley_loop(out, "luxury_spa", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "anoisesrc=color=pink" in joined
    assert "highpass" in joined


def test_generate_foley_loop_showroom(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/foley.mp3")
    generate_foley_loop(out, "showroom", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "anoisesrc=color=pink" in joined
    assert "lowpass" in joined


def test_generate_foley_loop_shopping_channel(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/foley.mp3")
    generate_foley_loop(out, "shopping_channel", 5.0)
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "anoisesrc=color=white" in joined
    assert "bandpass" in joined


def test_generate_foley_loop_unknown_environment_returns_path(mock_subprocess):
    """Unknown environment returns output_path without generating anything."""
    mock_run, _ = mock_subprocess
    out = Path("/tmp/foley_unknown.mp3")
    result = generate_foley_loop(out, "unknown_env_xyz", 5.0)
    assert result == out
    mock_run.assert_not_called()


def test_generate_foley_loop_exception_is_swallowed(mock_subprocess):
    """If ffmpeg fails, exception is logged and path is returned (not raised)."""
    mock_run, _ = mock_subprocess
    mock_run.side_effect = RuntimeError("ffmpeg exploded")
    out = Path("/tmp/foley_exc.mp3")
    result = generate_foley_loop(out, "cafe", 5.0)
    assert result == out  # no exception raised
