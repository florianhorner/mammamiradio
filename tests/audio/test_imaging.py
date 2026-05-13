"""Unit tests for station imaging selection and fallbacks."""

from __future__ import annotations

from unittest.mock import patch

from mammamiradio.audio.imaging import ImagingLibrary
from mammamiradio.core.models import SegmentType


def test_pick_sweeper_sting_delegates_to_station_id_bed(tmp_path):
    out = tmp_path / "sweeper.mp3"
    motif = [523, 659, 784, 1047]
    lib = ImagingLibrary(motif, tmp_path, assets_dir=tmp_path / "assets")

    with patch("mammamiradio.audio.imaging.generate_station_id_bed", return_value=out) as mock_bed:
        result = lib.pick_sweeper_sting(out)

    assert result == out
    mock_bed.assert_called_once_with(out, 2.0, motif)


def test_pick_stinger_uses_matching_asset_before_synthetic(tmp_path):
    assets = tmp_path / "assets"
    stingers = assets / "stingers"
    stingers.mkdir(parents=True)
    asset = stingers / "music_banter.mp3"
    asset.write_bytes(b"asset-sting")
    out = tmp_path / "out.mp3"
    lib = ImagingLibrary([523], tmp_path, assets_dir=assets)

    with patch("mammamiradio.audio.imaging.generate_transition_sting") as mock_generate:
        result = lib.pick_stinger(SegmentType.MUSIC, SegmentType.BANTER, out)

    assert result == out
    assert out.read_bytes() == b"asset-sting"
    mock_generate.assert_not_called()


def test_pick_stinger_music_to_speech_falls_back_to_synthetic(tmp_path):
    out = tmp_path / "transition.mp3"
    motif = [523, 659, 784, 1047]
    lib = ImagingLibrary(motif, tmp_path, assets_dir=tmp_path / "missing")

    with patch("mammamiradio.audio.imaging.generate_transition_sting", return_value=out) as mock_generate:
        result = lib.pick_stinger(SegmentType.MUSIC, SegmentType.NEWS_FLASH, out)

    assert result == out
    mock_generate.assert_called_once_with("music", "news_flash", out, motif)


def test_pick_stinger_speech_to_music_falls_back_to_synthetic(tmp_path):
    out = tmp_path / "transition.mp3"
    motif = [523, 659, 784, 1047]
    lib = ImagingLibrary(motif, tmp_path, assets_dir=tmp_path / "missing")

    with patch("mammamiradio.audio.imaging.generate_transition_sting", return_value=out) as mock_generate:
        result = lib.pick_stinger(SegmentType.BANTER, SegmentType.MUSIC, out)

    assert result == out
    mock_generate.assert_called_once_with("banter", "music", out, motif)


def test_pick_talk_bed_uses_prerecorded_bed_before_source_track(tmp_path):
    assets = tmp_path / "assets"
    beds = assets / "beds"
    beds.mkdir(parents=True)
    asset_bed = beds / "soft.mp3"
    asset_bed.write_bytes(b"bed")
    source = tmp_path / "last_music.mp3"
    source.write_bytes(b"music")
    out = tmp_path / "bed.mp3"
    lib = ImagingLibrary([523], tmp_path, bed_volume_db=-18.0, assets_dir=assets)

    with patch("mammamiradio.audio.imaging._run_ffmpeg") as mock_run:
        result = lib.pick_talk_bed(4.25, out, source)

    assert result == out
    cmd = mock_run.call_args.args[0]
    assert str(asset_bed) in cmd
    assert str(source) not in cmd
    assert any("aloop=loop=-1" in arg for arg in cmd)
    assert any("loudnorm=I=-18" in arg for arg in cmd)


def test_pick_talk_bed_ducks_existing_source_track(tmp_path):
    source = tmp_path / "last_music.mp3"
    source.write_bytes(b"music")
    out = tmp_path / "bed.mp3"
    lib = ImagingLibrary([523], tmp_path, bed_volume_db=-20.0, assets_dir=tmp_path / "assets")

    with patch("mammamiradio.audio.imaging._run_ffmpeg") as mock_run:
        result = lib.pick_talk_bed(3.0, out, source)

    assert result == out
    cmd = mock_run.call_args.args[0]
    assert str(source) in cmd
    assert any("aloop=loop=-1" in arg for arg in cmd)
    assert any("loudnorm=I=-20" in arg for arg in cmd)
    assert "-t" in cmd
    assert cmd[cmd.index("-t") + 1] == "3"


def test_pick_talk_bed_fallback_synthetic_when_source_none(tmp_path):
    """Scenario 2: empty container/cold start has no last_music_file."""
    out = tmp_path / "synthetic_bed.mp3"
    lib = ImagingLibrary([523], tmp_path, bed_volume_db=-18.0, assets_dir=tmp_path / "assets")

    with patch("mammamiradio.audio.imaging._run_ffmpeg") as mock_run:
        result = lib.pick_talk_bed(2.5, out, source_track=None)

    assert result == out
    cmd = mock_run.call_args.args[0]
    joined = " ".join(cmd)
    assert "aevalsrc=" in joined
    assert "sin(2*PI*130*t)" in joined
    assert "sin(2*PI*260*t)" in joined
    assert "loudnorm=I=-18" in joined
    assert str(out) in cmd
