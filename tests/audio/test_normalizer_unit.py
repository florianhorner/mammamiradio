"""Unit tests for mammamiradio.normalizer with mocked subprocess (no real ffmpeg)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mammamiradio.audio.normalizer import (
    _run_ffmpeg,
    concat_files,
    generate_music_bed,
    generate_silence,
    generate_sweep,
    generate_tone,
    humanize_norm_filename,
    load_track_metadata,
    mix_oneshot_sfx,
    mix_quiet_bleed,
    mix_with_bed,
    norm_cache_duration_sec,
    normalize,
    refresh_track_metadata,
    save_track_metadata,
)


@pytest.fixture
def mock_subprocess():
    """Patch subprocess.run to return success by default.

    Also disables the post-concat duration probe (`probe_duration_sec`) so
    tests that inspect `mock_run.call_args` see the ffmpeg call as the last
    subprocess invocation, not a trailing ffprobe from the Item 1 guard.
    Tests that want to exercise the guard explicitly monkeypatch the probe.
    """
    completed = MagicMock(spec=subprocess.CompletedProcess)
    completed.returncode = 0
    completed.stderr = b""
    completed.stdout = b""

    with (
        patch("mammamiradio.audio.normalizer.subprocess.run", return_value=completed) as mock_run,
        patch("mammamiradio.audio.normalizer.probe_duration_sec", return_value=None),
    ):
        yield mock_run, completed


# ---------------------------------------------------------------------------
# _run_ffmpeg
# ---------------------------------------------------------------------------


def test_run_ffmpeg_passes_command(mock_subprocess):
    mock_run, _ = mock_subprocess
    cmd = ["ffmpeg", "-y", "-i", "in.mp3", "out.mp3"]
    _run_ffmpeg(cmd, "test")
    mock_run.assert_called_once_with(cmd, capture_output=True, timeout=180.0)


def test_run_ffmpeg_logs_stage_timing_at_debug(mock_subprocess, caplog):
    """Render-latency deep-dive: every ffmpeg stage logs its wall time at DEBUG,
    labelled by description, so the seconds can be attributed per stage."""
    import logging

    with caplog.at_level(logging.DEBUG, logger="mammamiradio.audio.normalizer"):
        _run_ffmpeg(["ffmpeg", "-y", "out.mp3"], "normalize song.mp3")

    timing = [r for r in caplog.records if "ffmpeg stage normalize song.mp3" in r.getMessage()]
    assert timing, "expected a DEBUG per-stage timing line for the ffmpeg call"
    assert timing[-1].levelno == logging.DEBUG


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
    assert "-write_xing" in cmd
    assert cmd[cmd.index("-write_xing") + 1] == "0"


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


def test_run_ffmpeg_uses_global_ffmpeg_semaphore(mock_subprocess):
    mock_run, _ = mock_subprocess
    held = {"value": False}

    class RecordingSem:
        def __enter__(self):
            held["value"] = True

        def __exit__(self, exc_type, exc, tb):
            held["value"] = False

    def _assert_slot_held(*_args, **_kwargs):
        assert held["value"] is True
        return mock_subprocess[1]

    mock_run.side_effect = _assert_slot_held
    with patch("mammamiradio.audio.admission._NORM_SEM", RecordingSem()):
        _run_ffmpeg(["ffmpeg", "-y"], "leaf gate")

    assert held["value"] is False
    mock_run.assert_called_once()


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
    # Fast path = intermediate TTS lines for dialogue assembly. A per-line
    # fade-in would produce choppy speech; it belongs only on final output.
    assert "afade" not in audio_filter


def test_normalize_applies_fade_in_on_final_output(mock_subprocess):
    """Every final-output segment carries a soft fade-in so music→voice
    hand-offs aren't hard cuts. Florian flagged the drop as audible during
    a 2026-04-21 listening session; this test guards against regression.
    """
    mock_run, _ = mock_subprocess
    inp = Path("/tmp/in.mp3")
    out = Path("/tmp/out.mp3")

    normalize(inp, out, loudnorm=True)

    cmd = mock_run.call_args[0][0]
    filter_idx = cmd.index("-filter:a")
    audio_filter = cmd[filter_idx + 1]
    assert "afade=t=in:d=0.25" in audio_filter
    # Fade must come after silence trim, otherwise it fades into silence.
    assert audio_filter.index("silenceremove") < audio_filter.index("afade=t=in")


def test_normalize_music_eq_also_gets_fade_in(mock_subprocess):
    """music_eq=True (yt-dlp tracks) goes through the same final-output
    pipeline and also needs a soft entry."""
    mock_run, _ = mock_subprocess
    inp = Path("/tmp/song.mp3")
    out = Path("/tmp/song_norm.mp3")

    normalize(inp, out, loudnorm=True, music_eq=True)

    cmd = mock_run.call_args[0][0]
    filter_idx = cmd.index("-filter:a")
    audio_filter = cmd[filter_idx + 1]
    assert "afade=t=in:d=0.25" in audio_filter
    assert "highpass" in audio_filter  # music EQ still applied first


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
    assert "-write_xing" in cmd
    assert cmd[cmd.index("-write_xing") + 1] == "0"


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
    assert "-write_xing" in cmd
    assert cmd[cmd.index("-write_xing") + 1] == "0"


def test_generate_tone_includes_write_xing_0(mock_subprocess):
    mock_run, _ = mock_subprocess
    out = Path("/tmp/tone.mp3")

    generate_tone(out)

    cmd = mock_run.call_args[0][0]
    assert "-write_xing" in cmd
    assert cmd[cmd.index("-write_xing") + 1] == "0"


def test_mix_with_bed_includes_write_xing_0(mock_subprocess):
    mock_run, _ = mock_subprocess
    voice = Path("/tmp/voice.mp3")
    bed = Path("/tmp/bed.mp3")
    out = Path("/tmp/mixed.mp3")

    mix_with_bed(voice, bed, out)

    cmd = mock_run.call_args[0][0]
    assert "-write_xing" in cmd
    assert cmd[cmd.index("-write_xing") + 1] == "0"


def test_generate_sweep_same_frequency_uses_tone(mock_subprocess):
    with patch("mammamiradio.audio.normalizer.generate_tone", return_value=Path("/tmp/tone.mp3")) as mock_tone:
        result = generate_sweep(Path("/tmp/tone.mp3"), start_hz=440, end_hz=440, duration_sec=0.3)

    assert result == Path("/tmp/tone.mp3")
    mock_tone.assert_called_once_with(Path("/tmp/tone.mp3"), freq_hz=440, duration_sec=0.3)


@pytest.mark.requires_ffmpeg
def test_generate_sweep_with_ffmpeg(tmp_path):
    out = generate_sweep(tmp_path / "sweep.mp3", start_hz=200, end_hz=2000, duration_sec=0.2)

    assert out.exists()
    assert out.stat().st_size > 1000


def test_generate_music_bed_suspicious_jazz_escapes_lavfi_expression_commas(tmp_path):
    commands = []

    def _capture(cmd, _label):
        commands.append(cmd)

    with patch("mammamiradio.audio.normalizer._run_ffmpeg", side_effect=_capture):
        generate_music_bed(tmp_path / "jazz.mp3", "suspicious_jazz", 1.0)
        generate_music_bed(tmp_path / "upbeat.mp3", "upbeat", 1.0)

    jazz_cmd, upbeat_cmd = commands
    jazz_input = jazz_cmd[jazz_cmd.index("-i") + 1]
    upbeat_input = upbeat_cmd[upbeat_cmd.index("-i") + 1]

    assert "\\," in jazz_input
    assert "max(0\\,1-2*mod(t\\,2))" in jazz_input
    assert "abs(mod(t\\,2)-0.5)" in jazz_input
    assert "220.0" in jazz_input
    assert "277.0" in jazz_input
    assert "\\," not in upbeat_input


@pytest.mark.requires_ffmpeg
def test_generate_music_bed_suspicious_jazz_renders_with_ffmpeg(tmp_path):
    out = generate_music_bed(tmp_path / "suspicious_jazz.mp3", "suspicious_jazz", 0.5)

    assert out.exists()
    assert out.stat().st_size > 0


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
    from mammamiradio.audio.normalizer import measure_lufs

    fake_result = MagicMock(spec=subprocess.CompletedProcess)
    fake_result.returncode = 0
    fake_result.stderr = "  Integrated loudness:\n    I:         -16.2 LUFS\n    Threshold: -26.2 LUFS\n"
    with patch("mammamiradio.audio.normalizer.subprocess.run", return_value=fake_result):
        result = measure_lufs(Path("/tmp/test.mp3"))
    assert result == pytest.approx(-16.2)


def test_measure_lufs_takes_summary_not_per_frame_floor():
    """ebur128 logs a per-frame 'I: -70.0 LUFS' (the gate floor, before data has
    accumulated) for EVERY frame, then the true integrated value in its
    end-of-stream Summary. measure_lufs must return the Summary value, not the
    first per-frame -70.0 — the regression that made it return -70 for everything
    and silently disabled both the fast-path skip and any LUFS-based correction.
    """
    from mammamiradio.audio.normalizer import measure_lufs

    fake_result = MagicMock(spec=subprocess.CompletedProcess)
    fake_result.returncode = 0
    fake_result.stderr = (
        "[Parsed_ebur128_0 @ 0x1] t: 0.1 TARGET:-23 LUFS  M: -70.0 S:-70.0  I: -70.0 LUFS  LRA: 0.0 LU\n"
        "[Parsed_ebur128_0 @ 0x1] t: 0.2 TARGET:-23 LUFS  M: -22.0 S:-70.0  I: -70.0 LUFS  LRA: 0.0 LU\n"
        "[Parsed_ebur128_0 @ 0x1] Summary:\n\n"
        "  Integrated loudness:\n    I:         -16.2 LUFS\n    Threshold: -26.2 LUFS\n"
    )
    with patch("mammamiradio.audio.normalizer.subprocess.run", return_value=fake_result):
        result = measure_lufs(Path("/tmp/test.mp3"))
    assert result == pytest.approx(-16.2)  # the Summary value, not the -70.0 per-frame floor


def test_measure_lufs_returns_none_on_failure():
    """measure_lufs returns None when ffmpeg/ebur128 fails."""
    from mammamiradio.audio.normalizer import measure_lufs

    fake_result = MagicMock(spec=subprocess.CompletedProcess)
    fake_result.returncode = 1
    fake_result.stderr = ""
    with patch("mammamiradio.audio.normalizer.subprocess.run", return_value=fake_result):
        assert measure_lufs(Path("/tmp/test.mp3")) is None


def test_measure_lufs_returns_none_on_timeout():
    """measure_lufs returns None on subprocess timeout."""
    from mammamiradio.audio.normalizer import measure_lufs

    with patch("mammamiradio.audio.normalizer.subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 30)):
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

    with patch("mammamiradio.audio.normalizer.measure_lufs", return_value=-15.8):
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

    with patch("mammamiradio.audio.normalizer.measure_lufs", return_value=-25.0):
        normalize(input_file, output_file, loudnorm=True)

    # ffmpeg should have been called for normalization
    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# Equalizer chain: 2 filters only — 3rd EQ removed to prevent ffmpeg 8.x SIGABRT
# ---------------------------------------------------------------------------


def _extract_af_value(mock_run) -> str:
    """Helper: extract the -filter:a value from the ffmpeg call args."""
    assert mock_run.called, "subprocess.run was not called"
    cmd = mock_run.call_args[0][0]
    for i, arg in enumerate(cmd):
        if arg == "-filter:a" and i + 1 < len(cmd):
            return cmd[i + 1]
    return ""


def test_normalize_filter_chain_has_exactly_two_equalizers_with_music_eq(mock_subprocess, tmp_path):
    """With music_eq=True the filter chain must contain exactly two equalizer filters.

    The chain is:
      1. de-mud at 200 Hz
      2. presence at 3 kHz
    A 3rd equalizer (f=12000, HF harshness shelf) must NOT be added back — three
    equalizers combined with loudnorm trigger a psymodel.c:576 assertion crash
    (calc_energy SIGABRT) in ffmpeg 8.x on Pi aarch64.
    """
    input_file = tmp_path / "input.mp3"
    input_file.write_bytes(b"\xff" * 1000)
    output_file = tmp_path / "output.mp3"

    mock_run, _ = mock_subprocess

    with patch("mammamiradio.audio.normalizer.measure_lufs", return_value=-25.0):
        normalize(input_file, output_file, loudnorm=True, music_eq=True)

    af_value = _extract_af_value(mock_run)
    assert af_value, "No -filter:a filter chain found in ffmpeg command"
    equalizer_count = af_value.count("equalizer=")
    assert equalizer_count == 2, (
        f"Expected exactly 2 equalizer filters with music_eq=True, got {equalizer_count}. "
        f"3 equalizers + loudnorm = psymodel.c:576 SIGABRT on ffmpeg 8.x (Pi aarch64). "
        f"Filter chain: {af_value}"
    )


def test_normalize_filter_chain_excludes_hf_shelf_at_12khz(mock_subprocess, tmp_path):
    """The 3rd equalizer (HF harshness shelf at 12kHz) must NOT be in the music_eq chain.

    Three equalizers combined with loudnorm trigger a calc_energy assertion crash
    (psymodel.c:576 SIGABRT) in ffmpeg 8.x on Pi aarch64. The HF shelf was removed
    as the safest fix. Do not re-add it until ffmpeg resolves the underlying bug.
    """
    input_file = tmp_path / "input.mp3"
    input_file.write_bytes(b"\xff" * 1000)
    output_file = tmp_path / "output.mp3"

    mock_run, _ = mock_subprocess

    with patch("mammamiradio.audio.normalizer.measure_lufs", return_value=-25.0):
        normalize(input_file, output_file, loudnorm=True, music_eq=True)

    af_value = _extract_af_value(mock_run)
    assert af_value, "No -filter:a filter chain found in ffmpeg command"
    assert "equalizer=f=12000" not in af_value, (
        f"Forbidden: HF shelf (equalizer=f=12000) is in the filter chain: {af_value}\n"
        "Three equalizers + loudnorm = psymodel.c:576 SIGABRT on ffmpeg 8.x (Pi aarch64).\n"
        "Do not re-add the 3rd EQ until ffmpeg resolves the psymodel bug."
    )


def test_normalize_music_eq_false_still_has_no_equalizer_filters(mock_subprocess, tmp_path):
    """With music_eq=False (the default), no equalizer= filters appear in the chain.

    Equalizers are only added by the broadcast EQ branch (music_eq=True).
    """
    input_file = tmp_path / "input.mp3"
    input_file.write_bytes(b"\xff" * 1000)
    output_file = tmp_path / "output.mp3"

    mock_run, _ = mock_subprocess

    with patch("mammamiradio.audio.normalizer.measure_lufs", return_value=-25.0):
        normalize(input_file, output_file, loudnorm=True, music_eq=False)

    af_value = _extract_af_value(mock_run)
    assert af_value, "No -filter:a filter chain found in ffmpeg command"
    equalizer_count = af_value.count("equalizer=")
    assert equalizer_count == 0, (
        f"Expected 0 equalizer filters with music_eq=False, got {equalizer_count}. Filter chain: {af_value}"
    )


# ── Track metadata sidecars (Item 20) ──────────────────────────────────────────


def test_save_and_load_track_metadata_roundtrip(tmp_path):
    norm = tmp_path / "norm_artie_5ive_sogno_americano_192k.mp3"
    norm.write_bytes(b"pretend mp3")
    save_track_metadata(norm, title="SOGNO AMERICANO", artist="Artie 5ive")
    meta = load_track_metadata(norm)
    assert meta == {"title": "SOGNO AMERICANO", "artist": "Artie 5ive"}


def test_save_and_load_track_metadata_roundtrip_with_duration(tmp_path):
    norm = tmp_path / "norm_artie_5ive_sogno_americano_192k.mp3"
    norm.write_bytes(b"pretend mp3")
    save_track_metadata(norm, title="SOGNO AMERICANO", artist="Artie 5ive", duration_ms=204_192)
    meta = load_track_metadata(norm)
    assert meta == {"title": "SOGNO AMERICANO", "artist": "Artie 5ive", "duration_ms": 204_192}
    assert norm_cache_duration_sec(norm, bitrate_kbps=192) == pytest.approx(204.192)


def test_load_track_metadata_missing_sidecar_returns_none(tmp_path):
    norm = tmp_path / "norm_missing_192k.mp3"
    norm.write_bytes(b"pretend mp3")
    assert load_track_metadata(norm) is None


def test_load_track_metadata_malformed_json_returns_none(tmp_path):
    norm = tmp_path / "norm_bad_192k.mp3"
    norm.write_bytes(b"pretend mp3")
    sidecar = tmp_path / "norm_bad_192k.mp3.json"
    sidecar.write_text("{not valid json")
    assert load_track_metadata(norm) is None


def test_load_track_metadata_incomplete_data_returns_none(tmp_path):
    norm = tmp_path / "norm_incomplete_192k.mp3"
    norm.write_bytes(b"pretend mp3")
    sidecar = tmp_path / "norm_incomplete_192k.mp3.json"
    sidecar.write_text('{"title": "only title, no artist"}')
    assert load_track_metadata(norm) is None


def test_save_track_metadata_drops_stale_reconciled_marker(tmp_path):
    # save_track_metadata runs only for a freshly (re)normalized file, so a
    # reconciled_lufs marker in a leftover/orphaned sidecar (eviction unlinks the
    # .mp3 but leaves the .json) is tied to the OLD content and MUST be dropped —
    # the fresh file re-earns it on the next cache-hit reconcile. Other keys survive.
    norm = tmp_path / "norm_merge_192k.mp3"
    norm.write_bytes(b"pretend mp3")
    sidecar = tmp_path / "norm_merge_192k.mp3.json"
    sidecar.write_text(
        json.dumps(
            {
                "duration_ms": 123000,
                "duration_sec": 123.0,
                "duration_s": 123.0,
                "reconciled_lufs": -16.0,
                "stray": "keep",
            }
        )
    )
    save_track_metadata(norm, title="T", artist="A")
    data = json.loads(sidecar.read_text())
    assert "reconciled_lufs" not in data  # stale marker dropped
    assert "duration_ms" not in data  # old-content duration dropped unless restamped
    assert "duration_sec" not in data
    assert "duration_s" not in data
    assert data["title"] == "T" and data["artist"] == "A"
    assert data["stray"] == "keep"  # unrelated keys preserved


def test_refresh_track_metadata_preserves_reconciled_marker(tmp_path):
    norm = tmp_path / "norm_merge_192k.mp3"
    norm.write_bytes(b"pretend mp3")
    sidecar = tmp_path / "norm_merge_192k.mp3.json"
    sidecar.write_text(json.dumps({"title": "Old", "artist": "Old", "duration_sec": 123.0, "reconciled_lufs": -16.0}))

    refresh_track_metadata(norm, title="New", artist="Artist", duration_ms=180_000)

    data = json.loads(sidecar.read_text())
    assert data["title"] == "New"
    assert data["artist"] == "Artist"
    assert data["duration_ms"] == 180_000
    assert "duration_sec" not in data
    assert data["reconciled_lufs"] == -16.0


def test_norm_cache_duration_estimates_older_sidecar_from_size_and_bitrate(tmp_path):
    norm = tmp_path / "norm_old_sidecar_192k.mp3"
    norm.write_bytes(b"x" * 24_000)
    save_track_metadata(norm, title="Old", artist="Cache")
    assert load_track_metadata(norm) == {"title": "Old", "artist": "Cache"}
    assert norm_cache_duration_sec(norm, bitrate_kbps=192) == pytest.approx(1.0)


def test_norm_cache_duration_uses_filename_bitrate_before_current_config(tmp_path):
    norm = tmp_path / "norm_legacy_track_320k.mp3"
    norm.write_bytes(b"x" * 40_000)
    save_track_metadata(norm, title="Old", artist="Cache")

    assert norm_cache_duration_sec(norm, bitrate_kbps=128) == pytest.approx(1.0)


def test_load_track_metadata_non_utf8_returns_none(tmp_path):
    # Sibling of the _load_sidecar fix: a non-UTF8 sidecar must return None, not raise.
    norm = tmp_path / "norm_bad_utf8_192k.mp3"
    norm.write_bytes(b"pretend mp3")
    sidecar = tmp_path / "norm_bad_utf8_192k.mp3.json"
    sidecar.write_bytes(b"\xff\xfe\x00not utf-8")
    assert load_track_metadata(norm) is None


def test_save_track_metadata_swallows_oserror_on_readonly_dir(tmp_path):
    # save_track_metadata must not raise when the sidecar cannot be written.
    # We mock write_text to raise OSError rather than relying on chmod, which
    # has no effect when the process runs as root.
    norm = tmp_path / "norm_x_192k.mp3"
    norm.write_bytes(b"ok")
    with patch("pathlib.Path.write_text", side_effect=OSError("read-only filesystem")) as mock_write:
        save_track_metadata(norm, title="t", artist="a")  # must not raise
    mock_write.assert_called_once()
    # Sidecar was not written; load returns None cleanly.
    assert load_track_metadata(norm) is None


def test_humanize_norm_filename_typical():
    assert humanize_norm_filename("norm_artie_5ive_sogno_americano_192k.mp3") == "Artie 5Ive Sogno Americano"


def test_humanize_norm_filename_no_bitrate_suffix():
    assert humanize_norm_filename("norm_simple_track.mp3") == "Simple Track"


def test_humanize_norm_filename_fallback_empty():
    assert humanize_norm_filename("norm_.mp3") == "Recovered track"


def test_humanize_norm_filename_passthrough_when_no_norm_prefix():
    # Legacy or externally-named files still get humanized.
    assert humanize_norm_filename("rescue_thing.mp3") == "Rescue Thing"


# ── Item 1: concat duration-invariant guard ───────────────────────────────────


class TestConcatFilesDurationInvariant:
    """concat_files must warn when ffmpeg silently produced a short output —
    the canonical fingerprint of Item 1 (banter mid-sentence cutoff). Never
    crash on probe failure; just log the shortfall with enough detail to
    identify the culprit input from the warning alone.
    """

    def test_duration_guard_logs_warning_when_output_too_short(self, tmp_path, caplog, monkeypatch):
        import mammamiradio.audio.normalizer as norm

        # Stub ffmpeg run so no real encode happens.
        monkeypatch.setattr(norm, "_run_ffmpeg", lambda *a, **kw: None)

        # Simulate a concat where the 3 input MP3s are each 10s (total 30s
        # + 2*0.3s silence gaps = 30.6s) but the "output" came out 15s —
        # exactly the class of failure Item 1 is guarding against.
        durations = {
            "input_a.mp3": 10.0,
            "input_b.mp3": 10.0,
            "input_c.mp3": 10.0,
            "concat_out.mp3": 15.0,  # ← truncated
        }

        def fake_probe(path):
            return durations.get(Path(path).name)

        monkeypatch.setattr(norm, "probe_duration_sec", fake_probe)

        inputs = [tmp_path / "input_a.mp3", tmp_path / "input_b.mp3", tmp_path / "input_c.mp3"]
        for p in inputs:
            p.write_bytes(b"stub")
        output = tmp_path / "concat_out.mp3"
        output.write_bytes(b"stub")

        caplog.set_level("WARNING", logger="mammamiradio.audio.normalizer")
        norm.concat_files(inputs, output, silence_ms=300, loudnorm=False)

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("duration shortfall" in r.message for r in warnings), (
            "concat_files should log a 'duration shortfall' warning when the "
            "output is shorter than the sum of inputs by more than 5%."
        )

    def test_duration_guard_strict_raises_when_output_too_short(self, tmp_path, caplog, monkeypatch):
        import mammamiradio.audio.normalizer as norm

        monkeypatch.setattr(norm, "_run_ffmpeg", lambda *a, **kw: None)
        durations = {
            "input_a.mp3": 6.0,
            "input_b.mp3": 6.0,
            "concat_out.mp3": 5.0,
        }
        monkeypatch.setattr(norm, "probe_duration_sec", lambda p: durations.get(Path(p).name))

        inputs = [tmp_path / "input_a.mp3", tmp_path / "input_b.mp3"]
        for p in inputs:
            p.write_bytes(b"stub")
        output = tmp_path / "concat_out.mp3"
        output.write_bytes(b"stub")

        caplog.set_level("WARNING", logger="mammamiradio.audio.normalizer")
        with pytest.raises(norm.ConcatDurationError, match="duration shortfall"):
            norm.concat_files(inputs, output, silence_ms=0, loudnorm=False, strict_duration=True)

        assert any("duration shortfall" in r.message for r in caplog.records)

    def test_duration_guard_silent_when_output_matches(self, tmp_path, caplog, monkeypatch):
        import mammamiradio.audio.normalizer as norm

        monkeypatch.setattr(norm, "_run_ffmpeg", lambda *a, **kw: None)
        durations = {
            "input_a.mp3": 10.0,
            "input_b.mp3": 10.0,
            "concat_out.mp3": 20.3,  # matches inputs + 1*0.3s gap
        }
        monkeypatch.setattr(norm, "probe_duration_sec", lambda p: durations.get(Path(p).name))

        inputs = [tmp_path / "input_a.mp3", tmp_path / "input_b.mp3"]
        for p in inputs:
            p.write_bytes(b"stub")
        output = tmp_path / "concat_out.mp3"
        output.write_bytes(b"stub")

        caplog.set_level("WARNING", logger="mammamiradio.audio.normalizer")
        norm.concat_files(inputs, output, silence_ms=300, loudnorm=False)

        warnings = [r for r in caplog.records if r.levelname == "WARNING" and "duration shortfall" in r.message]
        assert not warnings, "No warning expected when output duration matches expected sum."

    def test_duration_guard_silent_when_probe_fails(self, tmp_path, caplog, monkeypatch):
        import mammamiradio.audio.normalizer as norm

        monkeypatch.setattr(norm, "_run_ffmpeg", lambda *a, **kw: None)
        # Probe returns None on every call — guard must skip gracefully.
        monkeypatch.setattr(norm, "probe_duration_sec", lambda p: None)

        inputs = [tmp_path / "a.mp3", tmp_path / "b.mp3"]
        for p in inputs:
            p.write_bytes(b"stub")
        output = tmp_path / "out.mp3"
        output.write_bytes(b"stub")

        caplog.set_level("WARNING", logger="mammamiradio.audio.normalizer")
        # Must not raise.
        norm.concat_files(inputs, output, silence_ms=0, loudnorm=False)
        warnings = [r for r in caplog.records if r.levelname == "WARNING" and "duration shortfall" in r.message]
        assert not warnings, "Guard must stay silent when probes can't determine durations."

    def test_duration_guard_silent_when_input_probe_partial_failure(self, tmp_path, caplog, monkeypatch):
        """Output probe succeeds but one input probe returns None — guard must
        return without warning (line 325) rather than attempt arithmetic on
        a partial list."""
        import mammamiradio.audio.normalizer as norm

        monkeypatch.setattr(norm, "_run_ffmpeg", lambda *a, **kw: None)
        durations = {
            "a.mp3": 10.0,
            "b.mp3": None,  # partial probe failure
            "out.mp3": 20.0,
        }
        monkeypatch.setattr(norm, "probe_duration_sec", lambda p: durations.get(Path(p).name))

        inputs = [tmp_path / "a.mp3", tmp_path / "b.mp3"]
        for x in inputs:
            x.write_bytes(b"stub")
        output = tmp_path / "out.mp3"
        output.write_bytes(b"stub")

        caplog.set_level("WARNING", logger="mammamiradio.audio.normalizer")
        norm.concat_files(inputs, output, silence_ms=0, loudnorm=False)

        warnings = [r for r in caplog.records if r.levelname == "WARNING" and "duration shortfall" in r.message]
        assert not warnings, "Guard must bail out cleanly when any input probe returns None."

    def test_duration_guard_swallows_probe_exception(self, tmp_path, caplog, monkeypatch):
        """If probe_duration_sec raises, the guard must catch it (lines
        341-342) — instrumentation never breaks production playback."""
        import mammamiradio.audio.normalizer as norm

        monkeypatch.setattr(norm, "_run_ffmpeg", lambda *a, **kw: None)

        def _boom(_p):
            raise RuntimeError("ffprobe exploded")

        monkeypatch.setattr(norm, "probe_duration_sec", _boom)

        inputs = [tmp_path / "a.mp3"]
        inputs[0].write_bytes(b"stub")
        output = tmp_path / "out.mp3"
        output.write_bytes(b"stub")

        caplog.set_level("DEBUG")
        # Must not raise — that is the invariant being guarded.
        norm.concat_files(inputs, output, silence_ms=0, loudnorm=False)
        # Also assert that no WARNING leaked, i.e. the exception path was taken
        # instead of the shortfall path.
        warnings = [r for r in caplog.records if r.levelname == "WARNING" and "duration shortfall" in r.message]
        assert not warnings, "Exception path must not masquerade as a shortfall warning."


# ── probe_duration_sec parser: exercise the real function body, not the fixture mock ──


class TestFFprobeDurationSecParser:
    """Every concat_files test above monkeypatches `probe_duration_sec` to
    None. That leaves the real function body uncovered by the suite. These
    tests hit the real function directly, mocking only `subprocess.run`, so the
    parser + error branches are measured by the coverage ratchet.
    """

    def _fake_completed(self, returncode=0, stdout="", stderr=""):
        cp = MagicMock(spec=subprocess.CompletedProcess)
        cp.returncode = returncode
        cp.stdout = stdout
        cp.stderr = stderr
        return cp

    def test_valid_duration_parses_as_float(self, tmp_path, monkeypatch):
        from mammamiradio.audio.normalizer import probe_duration_sec

        p = tmp_path / "ok.mp3"
        p.write_bytes(b"x")
        monkeypatch.setattr(
            "mammamiradio.audio.normalizer.subprocess.run",
            lambda *a, **kw: self._fake_completed(returncode=0, stdout="12.345\n"),
        )
        assert probe_duration_sec(p) == 12.345

    def test_nonzero_returncode_returns_none(self, tmp_path, monkeypatch):
        from mammamiradio.audio.normalizer import probe_duration_sec

        p = tmp_path / "bad.mp3"
        p.write_bytes(b"x")
        monkeypatch.setattr(
            "mammamiradio.audio.normalizer.subprocess.run",
            lambda *a, **kw: self._fake_completed(returncode=1, stderr="bogus"),
        )
        assert probe_duration_sec(p) is None

    def test_unparseable_stdout_returns_none(self, tmp_path, monkeypatch):
        from mammamiradio.audio.normalizer import probe_duration_sec

        p = tmp_path / "junk.mp3"
        p.write_bytes(b"x")
        monkeypatch.setattr(
            "mammamiradio.audio.normalizer.subprocess.run",
            lambda *a, **kw: self._fake_completed(returncode=0, stdout="not-a-number"),
        )
        assert probe_duration_sec(p) is None

    def test_oserror_returns_none(self, tmp_path, monkeypatch):
        from mammamiradio.audio.normalizer import probe_duration_sec

        p = tmp_path / "missing.mp3"
        p.write_bytes(b"x")

        def _raises(*a, **kw):
            raise OSError("ffprobe not installed")

        monkeypatch.setattr("mammamiradio.audio.normalizer.subprocess.run", _raises)
        assert probe_duration_sec(p) is None

    def test_timeout_returns_none(self, tmp_path, monkeypatch):
        from mammamiradio.audio.normalizer import probe_duration_sec

        p = tmp_path / "slow.mp3"
        p.write_bytes(b"x")

        def _timesout(*a, **kw):
            raise subprocess.TimeoutExpired(cmd=["ffprobe"], timeout=5)

        monkeypatch.setattr("mammamiradio.audio.normalizer.subprocess.run", _timesout)
        assert probe_duration_sec(p) is None

    def test_empty_stdout_returns_none(self, tmp_path, monkeypatch):
        from mammamiradio.audio.normalizer import probe_duration_sec

        p = tmp_path / "empty.mp3"
        p.write_bytes(b"x")
        monkeypatch.setattr(
            "mammamiradio.audio.normalizer.subprocess.run",
            lambda *a, **kw: self._fake_completed(returncode=0, stdout=""),
        )
        assert probe_duration_sec(p) is None


def test_normalize_real_encode_has_no_xing_header(tmp_path):
    """Encode a real MP3 via normalize() and confirm no Xing/Info header is present.

    Safari fires ended at the Xing/Info declared duration, cutting segments short.
    This test verifies the -write_xing 0 flag actually suppresses the header in
    the encoded output — not just that the flag is present in the ffmpeg command.
    """
    src = generate_tone(tmp_path / "tone.mp3", freq_hz=440, duration_sec=0.5)
    out = tmp_path / "normed.mp3"
    normalize(src, out, loudnorm=False)

    assert out.exists()
    raw = out.read_bytes()[:2048]
    assert b"Xing" not in raw, "Xing VBR header found — -write_xing 0 did not suppress it"
    assert b"Info" not in raw, "Info CBR header found — -write_xing 0 did not suppress it"


# ---------------------------------------------------------------------------
# silenceremove must not truncate speech (2026-05-18 HA Green banter outage)
# ---------------------------------------------------------------------------


def test_normalize_silenceremove_uses_negative_stop_periods(mock_subprocess):
    """silenceremove stop_periods must be negative on BOTH the loudnorm and fast
    filter chains.

    A positive stop_periods makes ffmpeg's silenceremove halt output at the FIRST
    silence period, truncating multi-phrase host lines at their first pause (~1.6s).
    The 2026-05-18 HA Green run rejected every banter for 4+ hours because of this.
    A negative stop_periods trims trailing silence only.
    """
    mock_run, _ = mock_subprocess
    for loudnorm in (True, False):
        mock_run.reset_mock()
        with patch("mammamiradio.audio.normalizer.measure_lufs", return_value=-25.0):
            normalize(Path("/tmp/in.mp3"), Path("/tmp/out.mp3"), loudnorm=loudnorm)
        cmd = mock_run.call_args[0][0]
        audio_filter = cmd[cmd.index("-filter:a") + 1]
        assert "silenceremove" in audio_filter
        assert "stop_periods=-1" in audio_filter, f"loudnorm={loudnorm}: {audio_filter}"
        assert "stop_periods=1" not in audio_filter, (
            f"loudnorm={loudnorm}: a positive stop_periods truncates host speech at its first pause: {audio_filter}"
        )


def test_normalize_fast_path_preserves_speech_with_internal_pauses(tmp_path):
    """normalize(loudnorm=False) must NOT truncate a host line at its internal pauses.

    Behavioural guard for the 2026-05-18 HA Green banter outage: silenceremove with a
    positive stop_periods halts output at the first silence period, collapsing every
    multi-phrase host line to ~1.6s. Builds a 7.6s line (speech with three internal
    0.4s pauses) and asserts it survives the per-line fast-path encode intact.
    """
    from mammamiradio.audio.normalizer import probe_duration_sec

    line = tmp_path / "host_line.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=300:duration=7.6:sample_rate=44100",
            "-af",
            "volume=0:enable='between(t,1.3,1.7)+between(t,3.3,3.7)+between(t,5.7,6.1)'",
            str(line),
        ],
        check=True,
    )
    line_dur = probe_duration_sec(line)
    assert line_dur is not None and line_dur > 7.0, f"fixture line too short: {line_dur}"

    out = tmp_path / "host_line_norm.mp3"
    normalize(line, out, loudnorm=False)
    out_dur = probe_duration_sec(out)

    assert out_dur is not None, "normalize produced an unprobeable file"
    assert out_dur > line_dur * 0.8, (
        f"normalize() truncated host speech to {out_dur:.2f}s of {line_dur:.2f}s. "
        f"silenceremove stop_periods must stay negative (trailing-only trim), "
        f"else multi-phrase banter is rejected as implausibly short."
    )
