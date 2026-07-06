"""Tests for normalizer imaging helpers: generate_transition_sting, mix_voice_with_bed,
and previously uncovered SFX/concat/ad branches."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from mammamiradio.audio.normalizer import (
    crossfade_voice_over_music,
    generate_sfx,
    generate_transition_sting,
    mix_ad_with_bed,
    mix_voice_with_bed,
)


@pytest.fixture()
def mock_run():
    completed = MagicMock(spec=subprocess.CompletedProcess)
    completed.returncode = 0
    completed.stderr = b""
    completed.stdout = b"3.0"

    with (
        patch("mammamiradio.audio.normalizer.subprocess.run", return_value=completed) as m,
        patch("mammamiradio.audio.normalizer.probe_duration_sec", return_value=None),
    ):
        yield m


# ---------------------------------------------------------------------------
# generate_transition_sting
# ---------------------------------------------------------------------------


def test_transition_sting_music_to_banter(tmp_path, mock_run):
    out = tmp_path / "sting.mp3"
    with (
        patch("mammamiradio.audio.normalizer.generate_sweep") as mock_sweep,
        patch("mammamiradio.audio.normalizer.generate_station_id_bed") as mock_bed,
        patch("mammamiradio.audio.normalizer.concat_files") as mock_concat,
    ):
        mock_sweep.side_effect = lambda p, **_: p.write_bytes(b"sweep") or p
        mock_bed.side_effect = lambda p, *_, **__: p.write_bytes(b"motif") or p
        mock_concat.return_value = out
        result = generate_transition_sting("music", "banter", out)

    assert result == out
    mock_sweep.assert_called_once()
    mock_bed.assert_called_once()
    _, kwargs = mock_sweep.call_args
    duration = kwargs.get(
        "duration_sec",
        mock_sweep.call_args.args[1] if len(mock_sweep.call_args.args) > 1 else 0.8,
    )
    assert duration == pytest.approx(0.8, abs=0.01)


def test_transition_sting_music_to_speech_variant_changes_shape(tmp_path, mock_run):
    out = tmp_path / "sting.mp3"
    with (
        patch("mammamiradio.audio.normalizer.generate_sweep") as mock_sweep,
        patch("mammamiradio.audio.normalizer.generate_station_id_bed") as mock_bed,
        patch("mammamiradio.audio.normalizer.concat_files") as mock_concat,
    ):
        mock_sweep.side_effect = lambda p, **_: p.write_bytes(b"sweep") or p
        mock_bed.side_effect = lambda p, *_, **__: p.write_bytes(b"motif") or p
        mock_concat.return_value = out
        result = generate_transition_sting("music", "banter", out, variant=1)

    assert result == out
    assert mock_sweep.call_args.kwargs["duration_sec"] == pytest.approx(0.55, abs=0.01)
    assert mock_bed.call_args.args[1] == pytest.approx(1.0, abs=0.01)
    assert mock_bed.call_args.args[2] == [1047, 784, 659, 523]


def test_transition_sting_speech_to_music(tmp_path, mock_run):
    out = tmp_path / "sting.mp3"
    with (
        patch("mammamiradio.audio.normalizer.generate_station_id_bed") as mock_bed,
        patch("mammamiradio.audio.normalizer.generate_bumper_jingle") as mock_bumper,
        patch("mammamiradio.audio.normalizer.concat_files") as mock_concat,
    ):
        mock_bed.side_effect = lambda p, *_, **__: p.write_bytes(b"motif") or p
        mock_bumper.side_effect = lambda p, *_, **__: p.write_bytes(b"bump") or p
        mock_concat.return_value = out
        result = generate_transition_sting("banter", "music", out)

    assert result == out
    mock_bed.assert_called_once()
    mock_bumper.assert_called_once()


def test_transition_sting_speech_to_music_variant_changes_shape(tmp_path, mock_run):
    out = tmp_path / "sting.mp3"
    with (
        patch("mammamiradio.audio.normalizer.generate_sweep") as mock_sweep,
        patch("mammamiradio.audio.normalizer.generate_station_id_bed") as mock_bed,
        patch("mammamiradio.audio.normalizer.generate_bumper_jingle") as mock_bumper,
        patch("mammamiradio.audio.normalizer.concat_files") as mock_concat,
    ):
        mock_sweep.side_effect = lambda p, **_: p.write_bytes(b"sweep") or p
        mock_bed.side_effect = lambda p, *_, **__: p.write_bytes(b"motif") or p
        mock_bumper.side_effect = lambda p, *_, **__: p.write_bytes(b"bump") or p
        mock_concat.return_value = out
        result = generate_transition_sting("banter", "music", out, variant=2)

    assert result == out
    mock_bed.assert_not_called()
    assert mock_sweep.call_args.kwargs["duration_sec"] == pytest.approx(0.45, abs=0.01)
    assert mock_bumper.call_args.args[1] == pytest.approx(0.55, abs=0.01)


def test_transition_sting_news_flash_to_music(tmp_path, mock_run):
    out = tmp_path / "sting.mp3"
    with (
        patch("mammamiradio.audio.normalizer.generate_station_id_bed") as mock_bed,
        patch("mammamiradio.audio.normalizer.generate_bumper_jingle") as mock_bumper,
        patch("mammamiradio.audio.normalizer.concat_files") as mock_concat,
    ):
        mock_bed.side_effect = lambda p, *_, **__: p.write_bytes(b"motif") or p
        mock_bumper.side_effect = lambda p, *_, **__: p.write_bytes(b"bump") or p
        mock_concat.return_value = out
        result = generate_transition_sting("news_flash", "music", out)

    assert result == out


def test_transition_sting_music_to_station_id_uses_branded_motif(tmp_path, mock_run, caplog):
    out = tmp_path / "sting.mp3"
    caplog.set_level("WARNING", logger="mammamiradio.audio.normalizer")

    with (
        patch("mammamiradio.audio.normalizer.generate_sweep") as mock_sweep,
        patch("mammamiradio.audio.normalizer.generate_station_id_bed") as mock_bed,
        patch("mammamiradio.audio.normalizer.concat_files") as mock_concat,
    ):
        mock_sweep.side_effect = lambda p, **_: p.write_bytes(b"sweep") or p
        mock_bed.side_effect = lambda p, *_, **__: p.write_bytes(b"motif") or p
        mock_concat.return_value = out
        result = generate_transition_sting("music", "station_id", out)

    assert result == out
    mock_sweep.assert_called_once()
    mock_bed.assert_called_once()
    assert not any("unsupported pair" in record.message for record in caplog.records)


def test_transition_sting_station_id_to_music_uses_bumper(tmp_path, mock_run, caplog):
    out = tmp_path / "sting.mp3"
    caplog.set_level("WARNING", logger="mammamiradio.audio.normalizer")

    with (
        patch("mammamiradio.audio.normalizer.generate_station_id_bed") as mock_bed,
        patch("mammamiradio.audio.normalizer.generate_bumper_jingle") as mock_bumper,
        patch("mammamiradio.audio.normalizer.concat_files") as mock_concat,
    ):
        mock_bed.side_effect = lambda p, *_, **__: p.write_bytes(b"motif") or p
        mock_bumper.side_effect = lambda p, *_, **__: p.write_bytes(b"bump") or p
        mock_concat.return_value = out
        result = generate_transition_sting("station_id", "music", out)

    assert result == out
    mock_bed.assert_called_once()
    mock_bumper.assert_called_once()
    assert not any("unsupported pair" in record.message for record in caplog.records)


def test_transition_sting_unsupported_pair_falls_back_to_sweep(tmp_path, mock_run):
    out = tmp_path / "sting.mp3"
    with patch("mammamiradio.audio.normalizer.generate_sweep") as mock_sweep:
        mock_sweep.return_value = out
        result = generate_transition_sting("sweeper", "station_id", out)

    assert result == out
    mock_sweep.assert_called_once()


def test_transition_sting_uses_custom_motif_notes(tmp_path, mock_run):
    out = tmp_path / "sting.mp3"
    custom_notes = [440, 550, 660, 880]
    with (
        patch("mammamiradio.audio.normalizer.generate_sweep") as mock_sweep,
        patch("mammamiradio.audio.normalizer.generate_station_id_bed") as mock_bed,
        patch("mammamiradio.audio.normalizer.concat_files") as mock_concat,
    ):
        mock_sweep.side_effect = lambda p, **_: p.write_bytes(b"s") or p
        mock_bed.side_effect = lambda p, *args, **__: p.write_bytes(b"m") or p
        mock_concat.return_value = out
        generate_transition_sting("music", "ad", out, motif_notes=custom_notes)

    notes_passed = mock_bed.call_args.args[2] if len(mock_bed.call_args.args) > 2 else mock_bed.call_args.args[1]
    assert notes_passed == custom_notes


# ---------------------------------------------------------------------------
# crossfade_voice_over_music
# ---------------------------------------------------------------------------


def test_crossfade_voice_over_music_starts_voice_without_music_only_replay(tmp_path, mock_run):
    music = tmp_path / "music.mp3"
    voice = tmp_path / "voice.mp3"
    out = tmp_path / "mixed.mp3"

    result = crossfade_voice_over_music(music, voice, out)

    assert result == out
    cmd = mock_run.call_args[0][0]
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "adelay=1500|1500" not in filter_complex
    assert "adelay=150|150" in filter_complex


# ---------------------------------------------------------------------------
# mix_voice_with_bed
# ---------------------------------------------------------------------------


def test_mix_voice_with_bed_returns_output_path(tmp_path, mock_run):
    voice = tmp_path / "voice.mp3"
    bed = tmp_path / "bed.mp3"
    out = tmp_path / "mixed.mp3"
    voice.write_bytes(b"voice")
    bed.write_bytes(b"bed")

    result = mix_voice_with_bed(voice, bed, out)

    assert result == out
    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    assert str(voice) in joined
    assert str(bed) in joined
    assert str(out) in joined


def test_mix_voice_with_bed_applies_volume_scale(tmp_path, mock_run):
    voice = tmp_path / "voice.mp3"
    bed = tmp_path / "bed.mp3"
    out = tmp_path / "mixed.mp3"

    mix_voice_with_bed(voice, bed, out, bed_db=-24.0)

    cmd = mock_run.call_args[0][0]
    joined = " ".join(cmd)
    # 10^(-24/20) ≈ 0.063
    assert "volume=0.063" in joined


def test_mix_voice_with_bed_uses_duration_first(tmp_path, mock_run):
    voice = tmp_path / "voice.mp3"
    bed = tmp_path / "bed.mp3"
    out = tmp_path / "mixed.mp3"

    mix_voice_with_bed(voice, bed, out)

    cmd = mock_run.call_args[0][0]
    assert "duration=first" in " ".join(cmd)


# ---------------------------------------------------------------------------
# generate_sfx — uncovered branches
# ---------------------------------------------------------------------------


def test_generate_sfx_tape_stop_simple_fallback(tmp_path):
    """tape_stop simple-fallback branch (line 621) fires when rich path raises."""
    out = tmp_path / "sfx.mp3"
    ok = MagicMock(spec=subprocess.CompletedProcess, returncode=0, stderr=b"", stdout=b"")
    calls = iter([RuntimeError("ffmpeg broken"), ok])

    def _run(*args, **kwargs):
        v = next(calls)
        if isinstance(v, Exception):
            raise v
        return v

    with (
        patch("mammamiradio.audio.normalizer.subprocess.run", side_effect=_run),
        patch("mammamiradio.audio.normalizer.probe_duration_sec", return_value=None),
    ):
        result = generate_sfx(out, "tape_stop")
    assert result == out


def test_generate_sfx_hotline_beep_simple_fallback(tmp_path):
    """hotline_beep simple-fallback branch (lines 624-625) fires when rich path raises."""
    out = tmp_path / "sfx.mp3"
    ok = MagicMock(spec=subprocess.CompletedProcess, returncode=0, stderr=b"", stdout=b"")
    calls = iter([RuntimeError("ffmpeg broken"), ok])

    def _run(*args, **kwargs):
        v = next(calls)
        if isinstance(v, Exception):
            raise v
        return v

    with (
        patch("mammamiradio.audio.normalizer.subprocess.run", side_effect=_run),
        patch("mammamiradio.audio.normalizer.probe_duration_sec", return_value=None),
    ):
        result = generate_sfx(out, "hotline_beep")
    assert result == out


def test_generate_sfx_unknown_type_simple_fallback_default_tone(tmp_path):
    """Unknown sfx_type simple-fallback default (line 626) fires when rich path raises."""
    out = tmp_path / "sfx.mp3"
    ok = MagicMock(spec=subprocess.CompletedProcess, returncode=0, stderr=b"", stdout=b"")
    calls = iter([RuntimeError("ffmpeg broken"), ok])

    def _run(*args, **kwargs):
        v = next(calls)
        if isinstance(v, Exception):
            raise v
        return v

    with (
        patch("mammamiradio.audio.normalizer.subprocess.run", side_effect=_run),
        patch("mammamiradio.audio.normalizer.probe_duration_sec", return_value=None),
    ):
        result = generate_sfx(out, "completely_unknown_sfx_type_xyz")
    assert result == out


# ---------------------------------------------------------------------------
# concat_files — defense-in-depth exception branch (line 383-384)
# ---------------------------------------------------------------------------


def test_concat_files_duration_probe_exception_is_swallowed(tmp_path, mock_run):
    from mammamiradio.audio.normalizer import concat_files

    p1 = tmp_path / "a.mp3"
    p2 = tmp_path / "b.mp3"
    out = tmp_path / "out.mp3"

    with patch(
        "mammamiradio.audio.normalizer.probe_duration_sec",
        side_effect=RuntimeError("probe failed"),
    ):
        result = concat_files([p1, p2], out)

    assert result == out


# ---------------------------------------------------------------------------
# mix_ad_with_bed (lines 1384-1428)
# ---------------------------------------------------------------------------


def test_mix_ad_with_bed_returns_output(tmp_path, mock_run):
    voiceover = tmp_path / "ad.mp3"
    out = tmp_path / "ad_with_bed.mp3"
    voiceover.write_bytes(b"ad audio")
    mock_run.return_value.stdout = "28.5"
    mock_run.return_value.returncode = 0

    result = mix_ad_with_bed(voiceover, out)

    assert result == out
    calls = [" ".join(c[0][0]) for c in mock_run.call_args_list]
    ffmpeg_calls = [c for c in calls if "ffmpeg" in c]
    assert ffmpeg_calls, "Expected at least one ffmpeg call"
    assert str(voiceover) in ffmpeg_calls[-1]
    assert str(out) in ffmpeg_calls[-1]


def test_mix_ad_with_bed_ffprobe_failure_uses_default_duration(tmp_path, mock_run):
    voiceover = tmp_path / "ad.mp3"
    out = tmp_path / "ad_with_bed.mp3"
    mock_run.return_value.returncode = 1
    mock_run.return_value.stdout = ""

    result = mix_ad_with_bed(voiceover, out)

    assert result == out
    # Even with ffprobe fail, ffmpeg should still be called with fallback 30s
    ffmpeg_calls = [c[0][0] for c in mock_run.call_args_list if c[0][0][0] == "ffmpeg"]
    assert ffmpeg_calls
    assert "30.000" in " ".join(ffmpeg_calls[-1])


def test_mix_ad_with_bed_ffprobe_invalid_float_uses_default(tmp_path, mock_run):
    voiceover = tmp_path / "ad.mp3"
    out = tmp_path / "ad_with_bed.mp3"
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "not_a_number"

    result = mix_ad_with_bed(voiceover, out)

    assert result == out
    ffmpeg_calls = [c[0][0] for c in mock_run.call_args_list if c[0][0][0] == "ffmpeg"]
    assert ffmpeg_calls
    assert "30.000" in " ".join(ffmpeg_calls[-1])


def test_run_ffmpeg_raises_on_timeout():
    from mammamiradio.audio.normalizer import _run_ffmpeg

    with (
        pytest.raises(subprocess.TimeoutExpired),
        patch(
            "mammamiradio.audio.normalizer.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["ffmpeg"], timeout=180),
        ),
    ):
        _run_ffmpeg(["ffmpeg"], "hung command")
