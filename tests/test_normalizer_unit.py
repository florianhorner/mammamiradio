"""Unit tests for mammamiradio.normalizer with mocked subprocess (no real ffmpeg)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mammamiradio.normalizer import (
    _run_ffmpeg,
    concat_files,
    generate_silence,
    generate_sweep,
    mix_oneshot_sfx,
    mix_quiet_bleed,
    normalize,
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
# _run_ffmpeg
# ---------------------------------------------------------------------------


def test_run_ffmpeg_passes_command(mock_subprocess):
    mock_run, _ = mock_subprocess
    cmd = ["ffmpeg", "-y", "-i", "in.mp3", "out.mp3"]
    _run_ffmpeg(cmd, "test")
    mock_run.assert_called_once_with(cmd, capture_output=True)


def test_run_ffmpeg_raises_on_nonzero_return(mock_subprocess):
    _mock_run, completed = mock_subprocess
    completed.returncode = 1
    completed.stderr = b"some error output"
    completed.check_returncode.side_effect = subprocess.CalledProcessError(1, "ffmpeg")

    with pytest.raises(subprocess.CalledProcessError):
        _run_ffmpeg(["ffmpeg", "-y"], "failing command")


def test_run_ffmpeg_logs_stderr_on_failure(mock_subprocess, caplog):
    _mock_run, completed = mock_subprocess
    completed.returncode = 1
    long_stderr = b"x" * 600
    completed.stderr = long_stderr
    completed.check_returncode.side_effect = subprocess.CalledProcessError(1, "ffmpeg")

    with pytest.raises(subprocess.CalledProcessError):
        _run_ffmpeg(["ffmpeg"], "log test")

    # The logger should have captured the last 500 chars of stderr
    assert any("log test" in r.message for r in caplog.records)


def test_run_ffmpeg_returns_completed_process(mock_subprocess):
    _, completed = mock_subprocess
    result = _run_ffmpeg(["ffmpeg"], "ok")
    assert result is completed


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


def test_normalize_builds_correct_default_command(mock_subprocess):
    mock_run, _ = mock_subprocess
    inp = Path("/tmp/in.mp3")
    out = Path("/tmp/out.mp3")

    normalize(inp, out)

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "ffmpeg"
    assert "-y" in cmd
    assert str(inp) in cmd
    assert str(out) in cmd
    # Default values
    assert "48000" in cmd  # sample_rate
    assert "2" in cmd  # channels
    assert "192k" in cmd  # bitrate
    assert any("loudnorm=I=-16:LRA=11:TP=-1.5" in arg for arg in cmd)


def test_normalize_uses_config_params(mock_subprocess):
    mock_run, _ = mock_subprocess
    inp = Path("/tmp/in.mp3")
    out = Path("/tmp/out.mp3")

    config = MagicMock()
    config.audio.sample_rate = 44100
    config.audio.channels = 1
    config.audio.bitrate = 128

    normalize(inp, out, config)

    cmd = mock_run.call_args[0][0]
    assert "44100" in cmd
    assert "1" in cmd
    assert "128k" in cmd


def test_normalize_forces_single_thread(mock_subprocess):
    mock_run, _ = mock_subprocess
    inp = Path("/tmp/in.mp3")
    out = Path("/tmp/out.mp3")

    normalize(inp, out)

    cmd = mock_run.call_args[0][0]
    threads_idx = cmd.index("-threads")
    assert cmd[threads_idx + 1] == "1"


def test_normalize_without_loudnorm_uses_fast_filter(mock_subprocess):
    mock_run, _ = mock_subprocess
    inp = Path("/tmp/in.mp3")
    out = Path("/tmp/out.mp3")

    normalize(inp, out, loudnorm=False)

    cmd = mock_run.call_args[0][0]
    filter_idx = cmd.index("-filter:a")
    audio_filter = cmd[filter_idx + 1]
    assert "silenceremove" in audio_filter
    assert "loudnorm" not in audio_filter


def test_normalize_addon_uses_dynaudnorm(mock_subprocess):
    mock_run, _ = mock_subprocess
    inp = Path("/tmp/in.mp3")
    out = Path("/tmp/out.mp3")
    config = MagicMock()
    config.audio.sample_rate = 48000
    config.audio.channels = 2
    config.audio.bitrate = 192
    config.is_addon = True

    normalize(inp, out, config, loudnorm=True)

    cmd = mock_run.call_args[0][0]
    filter_idx = cmd.index("-filter:a")
    audio_filter = cmd[filter_idx + 1]
    assert "dynaudnorm=f=150:g=13" in audio_filter
    assert "alimiter=limit=0.95" in audio_filter
    assert "loudnorm=I=-16:LRA=11:TP=-1.5" not in audio_filter


# ---------------------------------------------------------------------------
# concat_files
# ---------------------------------------------------------------------------


def test_concat_single_file_returns_same_path():
    """concat_files with one file just returns that file — no ffmpeg call."""
    p = Path("/tmp/only.mp3")
    result = concat_files([p], Path("/tmp/out.mp3"))
    assert result == p


def test_concat_multiple_files_builds_filter_graph(mock_subprocess):
    mock_run, _ = mock_subprocess
    paths = [Path(f"/tmp/p{i}.mp3") for i in range(3)]
    out = Path("/tmp/concat.mp3")

    concat_files(paths, out)

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "ffmpeg"
    # 3 audio files + 2 silence segments (default silence_ms=300) = 5 inputs
    i_count = sum(1 for c in cmd if c == "-i")
    assert i_count == 5
    # Filter graph with concat (5 streams: 3 audio + 2 silence)
    filter_idx = cmd.index("-filter_complex")
    filter_str = cmd[filter_idx + 1]
    assert "concat=n=5:v=0:a=1" in filter_str
    assert "[0:a]" in filter_str
    assert "[1:a]" in filter_str
    assert "[2:a]" in filter_str


# ---------------------------------------------------------------------------
# generate_silence
# ---------------------------------------------------------------------------


def test_generate_silence_correct_duration(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/silence.mp3")

    generate_silence(out, 5.0)

    cmd = mock_run.call_args[0][0]
    assert "anullsrc" in " ".join(cmd)
    t_idx = cmd.index("-t")
    assert cmd[t_idx + 1] == "5.0"


def test_generate_silence_default_duration(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/silence.mp3")

    generate_silence(out)

    cmd = mock_run.call_args[0][0]
    t_idx = cmd.index("-t")
    assert cmd[t_idx + 1] == "3.0"


# ---------------------------------------------------------------------------
# generate_sweep
# ---------------------------------------------------------------------------


def test_generate_sweep_builds_command(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/sweep.mp3")

    generate_sweep(out)

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "ffmpeg"
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "0.2*sin(2*PI*200*0.8/log(10)*((10)^(t/0.8)-1))" in joined
    assert ":c=stereo" in joined


def test_generate_sweep_same_frequency_uses_tone(mock_subprocess):
    with patch("mammamiradio.normalizer.generate_tone", return_value=Path("/tmp/tone.mp3")) as mock_tone:
        result = generate_sweep(Path("/tmp/tone.mp3"), start_hz=440, end_hz=440, duration_sec=0.3)

    assert result == Path("/tmp/tone.mp3")
    mock_tone.assert_called_once_with(Path("/tmp/tone.mp3"), freq_hz=440, duration_sec=0.3)


@pytest.mark.requires_ffmpeg
def test_generate_sweep_with_ffmpeg(tmp_path):
    out = generate_sweep(tmp_path / "sweep.mp3", start_hz=200, end_hz=2000, duration_sec=0.2)

    assert out.exists()
    assert out.stat().st_size > 1000


# ---------------------------------------------------------------------------
# mix_quiet_bleed
# ---------------------------------------------------------------------------


def test_mix_quiet_bleed_builds_correct_command(mock_subprocess):
    mock_run, _ = mock_subprocess
    base = Path("/tmp/base.mp3")
    bleed = Path("/tmp/bleed.mp3")
    out = Path("/tmp/bleed_out.mp3")

    result = mix_quiet_bleed(base, bleed, out)

    assert result == out
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "ffmpeg"
    assert "-y" in cmd
    assert str(base) in cmd
    assert str(bleed) in cmd
    assert str(out) in cmd
    joined = " ".join(cmd)
    assert "volume=-22.0dB" in joined
    assert "afade" in joined
    assert "amix" in joined
    assert "loudnorm" in joined


def test_mix_quiet_bleed_custom_params(mock_subprocess):
    mock_run, _ = mock_subprocess
    base = Path("/tmp/base.mp3")
    bleed = Path("/tmp/bleed.mp3")
    out = Path("/tmp/bleed_out.mp3")

    result = mix_quiet_bleed(base, bleed, out, bleed_volume_db=-30.0, bleed_duration_sec=6.0)

    assert result == out
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "volume=-30.0dB" in joined
    assert "atrim=0:6.0" in joined


# ---------------------------------------------------------------------------
# mix_oneshot_sfx
# ---------------------------------------------------------------------------


def test_mix_oneshot_sfx_builds_correct_command(mock_subprocess):
    mock_run, _ = mock_subprocess
    base = Path("/tmp/base.mp3")
    sfx = Path("/tmp/sfx.mp3")
    out = Path("/tmp/sfx_out.mp3")

    result = mix_oneshot_sfx(base, sfx, out, offset_sec=2.5, sfx_volume_db=-15.0)

    assert result == out
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "ffmpeg"
    assert "-y" in cmd
    assert str(base) in cmd
    assert str(sfx) in cmd
    assert str(out) in cmd
    joined = " ".join(cmd)
    assert "volume=-15.0dB" in joined
    assert "adelay=2500|2500" in joined
    assert "amix" in joined
    assert "loudnorm" in joined


def test_mix_oneshot_sfx_default_params(mock_subprocess):
    mock_run, _ = mock_subprocess
    base = Path("/tmp/base.mp3")
    sfx = Path("/tmp/sfx.mp3")
    out = Path("/tmp/sfx_out.mp3")

    result = mix_oneshot_sfx(base, sfx, out)

    assert result == out
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert "volume=-18.0dB" in joined
    assert "adelay=0|0" in joined


# ── measure_lufs tests ──


def test_measure_lufs_parses_integrated_loudness():
    """measure_lufs extracts integrated LUFS from ebur128 stderr."""
    from mammamiradio.normalizer import measure_lufs

    fake_result = MagicMock(spec=subprocess.CompletedProcess)
    fake_result.returncode = 0
    fake_result.stderr = "  Integrated loudness:\n    I:         -16.2 LUFS\n    Threshold: -26.2 LUFS\n"
    with patch("mammamiradio.normalizer.subprocess.run", return_value=fake_result):
        result = measure_lufs(Path("/tmp/test.mp3"))
    assert result == pytest.approx(-16.2)


def test_measure_lufs_returns_none_on_failure():
    """measure_lufs returns None when ffmpeg/ebur128 fails."""
    from mammamiradio.normalizer import measure_lufs

    fake_result = MagicMock(spec=subprocess.CompletedProcess)
    fake_result.returncode = 1
    fake_result.stderr = ""
    with patch("mammamiradio.normalizer.subprocess.run", return_value=fake_result):
        assert measure_lufs(Path("/tmp/test.mp3")) is None


def test_measure_lufs_returns_none_on_timeout():
    """measure_lufs returns None on subprocess timeout."""
    from mammamiradio.normalizer import measure_lufs

    with patch("mammamiradio.normalizer.subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 30)):
        assert measure_lufs(Path("/tmp/test.mp3")) is None


def test_normalize_skips_loudnorm_when_lufs_within_tolerance(mock_subprocess, tmp_path):
    """normalize uses fast format conversion (no loudnorm) when LUFS is within ±1.5 of -16.

    Previously this did a bare shutil.copy2, which skipped format conversion and could
    leave the output at the wrong sample rate or bitrate. Now it falls through to the
    fast encode path (loudnorm=False) so format conversion still happens.
    """
    mock_run, _ = mock_subprocess
    input_file = tmp_path / "input.mp3"
    input_file.write_bytes(b"\xff" * 1000)
    output_file = tmp_path / "output.mp3"

    with patch("mammamiradio.normalizer.measure_lufs", return_value=-15.8):
        result = normalize(input_file, output_file, loudnorm=True)

    assert result == output_file
    # FFmpeg must be called once for fast format conversion (silence trim + re-encode)
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    # Find the -filter:a value (the element after "-filter:a")
    filter_val = ""
    for i, c in enumerate(cmd):
        if str(c) == "-filter:a" and i + 1 < len(cmd):
            filter_val = str(cmd[i + 1])
            break
    # Loudnorm and dynaudnorm filters must NOT be in the filter chain — this is the fast path
    assert "loudnorm" not in filter_val
    assert "dynaudnorm" not in filter_val


def test_normalize_proceeds_when_lufs_out_of_tolerance(mock_subprocess, tmp_path):
    """normalize runs full pipeline when LUFS is outside tolerance."""
    mock_run, _ = mock_subprocess
    input_file = tmp_path / "input.mp3"
    input_file.write_bytes(b"\xff" * 1000)
    output_file = tmp_path / "output.mp3"

    with patch("mammamiradio.normalizer.measure_lufs", return_value=-25.0):
        normalize(input_file, output_file, loudnorm=True)

    # ffmpeg should have been called for normalization
    mock_run.assert_called_once()
