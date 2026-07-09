"""Unit tests for station imaging selection and fallbacks."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

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


def test_pick_stinger_uses_generic_pack_asset_when_pair_is_not_overridden(tmp_path):
    assets = tmp_path / "assets"
    stingers = assets / "stingers"
    stingers.mkdir(parents=True)
    generic = stingers / "music_to_speech.mp3"
    generic.write_bytes(b"night-drive")
    out = tmp_path / "out.mp3"
    lib = ImagingLibrary([523], tmp_path, assets_dir=assets)

    with patch("mammamiradio.audio.imaging.generate_transition_sting") as mock_generate:
        result = lib.pick_stinger(SegmentType.MUSIC, SegmentType.NEWS_FLASH, out)

    assert result == out
    assert out.read_bytes() == b"night-drive"
    mock_generate.assert_not_called()


def test_pack_assets_cover_station_sweeper_time_and_ad_bumper_before_fallback(tmp_path):
    assets = tmp_path / "assets"
    (assets / "bumpers").mkdir(parents=True)
    for relative_path, contents in {
        "station_id.mp3": b"station",
        "sweeper.mp3": b"sweeper",
        "time_check.mp3": b"clock",
        "bumpers/ad_break.mp3": b"bumper",
    }.items():
        path = assets / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    lib = ImagingLibrary([523], tmp_path, assets_dir=assets)

    with (
        patch("mammamiradio.audio.imaging.generate_station_id_bed") as station_fallback,
        patch("mammamiradio.audio.imaging.generate_tone") as time_fallback,
        patch("mammamiradio.audio.imaging.generate_bumper_jingle") as bumper_fallback,
    ):
        station = tmp_path / "station.mp3"
        sweeper = tmp_path / "sweeper.mp3"
        time_check = tmp_path / "time.mp3"
        bumper = tmp_path / "bumper.mp3"
        assert lib.pick_station_id_bed(station) == station
        assert lib.pick_sweeper_sting(sweeper) == sweeper
        assert lib.pick_time_check_sting(time_check) == time_check
        assert lib.pick_ad_bumper(bumper) == bumper

    assert station.read_bytes() == b"station"
    assert sweeper.read_bytes() == b"sweeper"
    assert time_check.read_bytes() == b"clock"
    assert bumper.read_bytes() == b"bumper"
    station_fallback.assert_not_called()
    time_fallback.assert_not_called()
    bumper_fallback.assert_not_called()


def test_ad_sfx_dir_prefers_existing_custom_directory_then_pack(tmp_path):
    assets = tmp_path / "assets"
    bundled = assets / "sfx"
    bundled.mkdir(parents=True)
    custom = tmp_path / "custom"
    custom.mkdir()
    lib = ImagingLibrary([523], tmp_path, assets_dir=assets)

    assert lib.ad_sfx_dir(custom) == custom
    assert lib.ad_sfx_dir(tmp_path / "missing") == bundled
    assert lib.ad_beds_dir() is None

    beds = assets / "beds"
    beds.mkdir()
    (beds / "casa_notte.mp3").write_bytes(b"bed")
    assert lib.ad_beds_dir() == beds


def test_pick_stinger_music_to_speech_falls_back_to_synthetic(tmp_path):
    out = tmp_path / "transition.mp3"
    motif = [523, 659, 784, 1047]
    lib = ImagingLibrary(motif, tmp_path, assets_dir=tmp_path / "missing")

    with (
        patch("mammamiradio.audio.imaging.next_synth_variant", return_value=2),
        patch("mammamiradio.audio.imaging.generate_transition_sting", return_value=out) as mock_generate,
    ):
        result = lib.pick_stinger(SegmentType.MUSIC, SegmentType.NEWS_FLASH, out)

    assert result == out
    mock_generate.assert_called_once_with("music", "news_flash", out, motif, variant=2)


def test_pick_stinger_speech_to_music_falls_back_to_synthetic(tmp_path):
    out = tmp_path / "transition.mp3"
    motif = [523, 659, 784, 1047]
    lib = ImagingLibrary(motif, tmp_path, assets_dir=tmp_path / "missing")

    with (
        patch("mammamiradio.audio.imaging.next_synth_variant", return_value=1),
        patch("mammamiradio.audio.imaging.generate_transition_sting", return_value=out) as mock_generate,
    ):
        result = lib.pick_stinger(SegmentType.BANTER, SegmentType.MUSIC, out)

    assert result == out
    mock_generate.assert_called_once_with("banter", "music", out, motif, variant=1)


def test_pick_stinger_cache_rotates_bounded_synthetic_variant_pool(tmp_path):
    cache_dir = tmp_path / "cache"
    motif = [523, 659, 784, 1047]
    lib = ImagingLibrary(motif, tmp_path, assets_dir=tmp_path / "missing", cache_dir=cache_dir)
    out_a = tmp_path / "transition_a.mp3"
    out_b = tmp_path / "transition_b.mp3"
    out_c = tmp_path / "transition_c.mp3"
    out_d = tmp_path / "transition_d.mp3"

    def _generate(_from_name, _to_name, output_path, _notes, *, variant=0):
        output_path.write_bytes(f"sting-{variant}".encode())
        return output_path

    with (
        patch("mammamiradio.audio.imaging.next_synth_variant", side_effect=[0, 1, 2, 0]),
        patch("mammamiradio.audio.imaging.generate_transition_sting", side_effect=_generate) as mock_generate,
    ):
        assert lib.pick_stinger(SegmentType.MUSIC, SegmentType.NEWS_FLASH, out_a) == out_a
        assert lib.pick_stinger(SegmentType.MUSIC, SegmentType.NEWS_FLASH, out_b) == out_b
        assert lib.pick_stinger(SegmentType.MUSIC, SegmentType.NEWS_FLASH, out_c) == out_c
        assert lib.pick_stinger(SegmentType.MUSIC, SegmentType.NEWS_FLASH, out_d) == out_d

    assert mock_generate.call_count == 3
    assert out_a.read_bytes() == b"sting-0"
    assert out_b.read_bytes() == b"sting-1"
    assert out_c.read_bytes() == b"sting-2"
    assert out_d.read_bytes() == b"sting-0"
    assert len(list(cache_dir.glob("synth_transition_sting_*.mp3"))) == 3
    assert [call.kwargs["variant"] for call in mock_generate.call_args_list] == [0, 1, 2]


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
    assert "-stream_loop" in cmd
    assert cmd[cmd.index("-stream_loop") + 1] == "-1"
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
    assert "-stream_loop" in cmd
    assert cmd[cmd.index("-stream_loop") + 1] == "-1"
    assert any("loudnorm=I=-20" in arg for arg in cmd)
    assert "-t" in cmd
    assert cmd[cmd.index("-t") + 1] == "3"


def test_pick_talk_bed_source_track_bypasses_synth_cache(tmp_path):
    source = tmp_path / "last_music.mp3"
    source.write_bytes(b"music")
    out = tmp_path / "bed.mp3"
    lib = ImagingLibrary(
        [523], tmp_path, bed_volume_db=-20.0, assets_dir=tmp_path / "assets", cache_dir=tmp_path / "cache"
    )

    with (
        patch("mammamiradio.audio.imaging._run_ffmpeg") as mock_run,
        patch("mammamiradio.audio.imaging.materialize_synth_mp3") as mock_cache,
    ):
        result = lib.pick_talk_bed(3.0, out, source)

    assert result == out
    mock_run.assert_called_once()
    mock_cache.assert_not_called()


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


def test_pick_talk_bed_cache_warms_bounded_synthetic_variant_pool(tmp_path):
    cache_dir = tmp_path / "cache"
    lib = ImagingLibrary([523], tmp_path, bed_volume_db=-18.0, assets_dir=tmp_path / "assets", cache_dir=cache_dir)
    outputs = [tmp_path / f"synthetic_{idx}.mp3" for idx in range(4)]

    def _write_output(cmd, _label):
        Path(cmd[-1]).write_bytes(b"drone")

    with patch("mammamiradio.audio.imaging._run_ffmpeg", side_effect=_write_output) as mock_run:
        for output in outputs:
            assert lib.pick_talk_bed(2.5, output, source_track=None) == output

    assert mock_run.call_count == 3
    assert all(output.read_bytes() == b"drone" for output in outputs)
    assert len(list(cache_dir.glob("synth_talk_bed_*.mp3"))) == 3


def test_pick_talk_bed_propagates_loop_bed_ffmpeg_failure(tmp_path):
    source = tmp_path / "last_music.mp3"
    source.write_bytes(b"music")
    out = tmp_path / "bed.mp3"
    lib = ImagingLibrary([523], tmp_path, assets_dir=tmp_path / "assets")

    with (
        pytest.raises(subprocess.TimeoutExpired),
        patch(
            "mammamiradio.audio.imaging._run_ffmpeg",
            side_effect=subprocess.TimeoutExpired(["ffmpeg"], timeout=180),
        ),
    ):
        lib.pick_talk_bed(3.0, out, source)


def test_pick_talk_bed_propagates_synthetic_drone_ffmpeg_failure(tmp_path):
    out = tmp_path / "bed.mp3"
    lib = ImagingLibrary([523], tmp_path, assets_dir=tmp_path / "assets")

    with (
        pytest.raises(subprocess.TimeoutExpired),
        patch(
            "mammamiradio.audio.imaging._run_ffmpeg",
            side_effect=subprocess.TimeoutExpired(["ffmpeg"], timeout=180),
        ),
    ):
        lib.pick_talk_bed(3.0, out, source_track=None)
