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
    assert "loudnorm=I=-16:LRA=11:TP=-1.5" in cmd


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
