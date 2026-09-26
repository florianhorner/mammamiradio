"""Pure seam picker and packaged-asset proof for the playback cart."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from mammamiradio.audio.imaging import ImagingLibrary, default_imaging_assets_dir
from mammamiradio.core.config import load_config
from mammamiradio.core.models import Segment, SegmentType
from mammamiradio.scheduling import boundary_glue
from mammamiradio.scheduling.boundary_glue import (
    AD_IN,
    AD_OUT,
    MUSIC_TO_SPEECH,
    SKIP_INTERRUPT,
    SKIP_LIVE_AD,
    SKIP_NEXT_ERROR,
    SKIP_NEXT_MUSIC_TAIL,
    SKIP_NEXT_RESCUE,
    SKIP_PREV_IMAGING,
    SKIP_PREV_NONE,
    SKIP_PREV_RESCUE,
    SPEECH_TO_MUSIC,
    AiredBoundary,
    glue_for,
    seam_choice,
    validated_playable_bytes,
)
from mammamiradio.web.mp3_frames import mpeg1_l3_bitrate_kbps

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def _frames(
    count: int,
    marker: bytes = b"CART",
    *,
    bitrate_index: int = 11,
    sample_rate_idx: int = 1,
    reservoir: int = 0,
    crc: bool = False,
    channel_mode: int = 0,
) -> bytes:
    header = bytes((0xFF, 0xFA if crc else 0xFB, (bitrate_index << 4) | (sample_rate_idx << 2), channel_mode << 6))
    bitrate = mpeg1_l3_bitrate_kbps(header)
    assert bitrate is not None
    sample_rate = (44100, 48000, 32000)[sample_rate_idx]
    frame_length = 144 * bitrate * 1000 // sample_rate
    side = bytes(((reservoir >> 1) & 0xFF, (reservoir & 1) << 7))
    payload = (b"\x89\xab" if crc else b"") + side + marker
    padding = frame_length - 4 - len(payload)
    assert padding >= 0
    return (header + payload + b"\0" * padding) * count


def _segment(kind: SegmentType, **metadata) -> Segment:
    return Segment(type=kind, path=Path("programme.mp3"), metadata=metadata, ephemeral=False)


def _aired(kind: SegmentType, *, rescue: bool = False, generation: int = 1, **metadata) -> AiredBoundary:
    return AiredBoundary(kind=kind, rescue=rescue, generation=generation, **metadata)


@pytest.fixture(autouse=True)
def _clear_asset_cache():
    boundary_glue.warn_unusable.cache_clear()
    boundary_glue._VALIDATION_CACHE.clear()
    yield
    boundary_glue._VALIDATION_CACHE.clear()


def test_glue_for_four_packaged_directions(tmp_path: Path):
    for relative in (MUSIC_TO_SPEECH, SPEECH_TO_MUSIC, AD_IN, AD_OUT):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"asset")
    cases = (
        (_aired(SegmentType.MUSIC), _segment(SegmentType.BANTER), MUSIC_TO_SPEECH),
        (_aired(SegmentType.BANTER), _segment(SegmentType.MUSIC), SPEECH_TO_MUSIC),
        (_aired(SegmentType.MUSIC), _segment(SegmentType.AD, packaged=True), AD_IN),
        (_aired(SegmentType.AD, packaged=True), _segment(SegmentType.MUSIC), AD_OUT),
        (_aired(SegmentType.MUSIC), _segment(SegmentType.NEWS_FLASH), MUSIC_TO_SPEECH),
    )
    for previous, nxt, relative in cases:
        found = glue_for(previous, nxt, assets_dir=tmp_path)
        assert found == (tmp_path / relative).resolve()


def test_glue_for_missing_asset_is_a_clean_cut(tmp_path: Path):
    assert glue_for(_aired(SegmentType.MUSIC), _segment(SegmentType.BANTER), assets_dir=tmp_path) is None


def test_packaged_boundary_asset_rejects_missing_and_unsafe_paths(tmp_path: Path):
    assert ImagingLibrary.packaged_boundary_asset(MUSIC_TO_SPEECH, assets_dir=tmp_path) is None
    (tmp_path / "stingers").mkdir()
    assert ImagingLibrary.packaged_boundary_asset(MUSIC_TO_SPEECH, assets_dir=tmp_path) is None
    assert ImagingLibrary.packaged_boundary_asset("../secrets.mp3", assets_dir=tmp_path) is None
    assert ImagingLibrary.packaged_boundary_asset("", assets_dir=tmp_path) is None


def test_seam_skips_previous_none_imaging_and_rescue():
    speech = _segment(SegmentType.BANTER)
    music = _segment(SegmentType.MUSIC)
    assert seam_choice(None, speech).skip_reason == SKIP_PREV_NONE
    assert seam_choice(_aired(SegmentType.SWEEPER), music).skip_reason == SKIP_PREV_IMAGING
    assert seam_choice(_aired(SegmentType.STATION_ID), music).skip_reason == SKIP_PREV_IMAGING
    assert seam_choice(_aired(SegmentType.TIME_CHECK), music).skip_reason == SKIP_PREV_IMAGING
    assert seam_choice(_aired(SegmentType.MUSIC, rescue=True), speech).skip_reason == SKIP_PREV_RESCUE
    assert glue_for(None, speech, assets_dir=Path(".")) is None


def test_seam_skips_next_rescue_error_and_music_tail():
    previous = _aired(SegmentType.MUSIC)
    assert seam_choice(previous, _segment(SegmentType.BANTER, rescue=True)).skip_reason == SKIP_NEXT_RESCUE
    errored = seam_choice(previous, _segment(SegmentType.BANTER, error="render failed"))
    assert errored.skip_reason == SKIP_NEXT_ERROR
    assert seam_choice(previous, _segment(SegmentType.BANTER, has_music_tail=True)).skip_reason == SKIP_NEXT_MUSIC_TAIL
    assert seam_choice(previous, _segment(SegmentType.MUSIC, has_music_tail=True)).skip_reason is None
    music_after_talk = seam_choice(_aired(SegmentType.BANTER), _segment(SegmentType.MUSIC, has_music_tail=True))
    assert music_after_talk.skip_reason == SKIP_NEXT_MUSIC_TAIL
    assert seam_choice(previous, _segment(SegmentType.MUSIC)).relative is None


def test_same_class_seams_are_not_skips():
    assert seam_choice(_aired(SegmentType.MUSIC), _segment(SegmentType.MUSIC)).relative is None
    assert seam_choice(_aired(SegmentType.BANTER), _segment(SegmentType.AD)).skip_reason is None


def _install(tmp_path: Path, payload: bytes) -> Path:
    path = tmp_path / MUSIC_TO_SPEECH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_validation_accepts_a_matching_short_asset(tmp_path: Path):
    path = _install(tmp_path, _frames(4))
    playable = validated_playable_bytes(path, sample_rate=48000, bitrate=192)
    assert playable == path.read_bytes()


def test_validation_rejects_bitrate_sample_rate_duration_and_reservoir(tmp_path: Path, caplog):
    bad = (
        (_frames(4, bitrate_index=9), "bitrate"),
        (_frames(4, sample_rate_idx=0), "sample rate"),
        (_frames(63), "longer"),
        (_frames(4, reservoir=1), "previous file"),
    )
    for index, (payload, needle) in enumerate(bad):
        boundary_glue._VALIDATION_CACHE.clear()
        path = _install(tmp_path / f"case{index}", payload)
        with caplog.at_level(logging.WARNING):
            assert validated_playable_bytes(path, sample_rate=48000, bitrate=192) is None
            assert validated_playable_bytes(path, sample_rate=48000, bitrate=192) is None
        warnings = [
            record for record in caplog.records if record.levelno == logging.WARNING and needle in record.message
        ]
        assert len(warnings) == 1
        caplog.clear()


def test_validation_cache_reloads_replaced_file_and_rejects_deleted_file(tmp_path: Path):
    path = _install(tmp_path, _frames(4))
    first = validated_playable_bytes(path, sample_rate=48000, bitrate=192)
    assert first == _frames(4)
    replacement = _frames(5, b"NEW")
    path.write_bytes(replacement)
    assert validated_playable_bytes(path, sample_rate=48000, bitrate=192) == replacement
    path.unlink()
    assert validated_playable_bytes(path, sample_rate=48000, bitrate=192) is None


def test_boundary_imaging_env_parse(monkeypatch, caplog):
    monkeypatch.delenv("MAMMAMIRADIO_BOUNDARY_IMAGING", raising=False)
    assert load_config(TOML_PATH).audio.boundary_imaging is True

    monkeypatch.setenv("MAMMAMIRADIO_BOUNDARY_IMAGING", "false")
    assert load_config(TOML_PATH).audio.boundary_imaging is False

    monkeypatch.setenv("MAMMAMIRADIO_BOUNDARY_IMAGING", "sometimes")
    with caplog.at_level(logging.WARNING):
        config = load_config(TOML_PATH)
    assert config.audio.boundary_imaging is True
    assert "leaving transitions on" in caplog.text


@pytest.mark.parametrize("packaged", [False, True])
def test_ad_carts_only_surround_packaged_spots(packaged):
    into = seam_choice(_aired(SegmentType.MUSIC), _segment(SegmentType.AD, packaged=packaged))
    out = seam_choice(_aired(SegmentType.AD, packaged=packaged), _segment(SegmentType.MUSIC))
    assert (into.relative, out.relative) == ((AD_IN, AD_OUT) if packaged else (None, None))
    assert (into.skip_reason, out.skip_reason) == ((None, None) if packaged else (SKIP_LIVE_AD, SKIP_LIVE_AD))


def test_interrupts_have_no_cart_on_either_side():
    assert (
        seam_choice(_aired(SegmentType.MUSIC), _segment(SegmentType.BANTER, interrupt=True)).skip_reason
        == SKIP_INTERRUPT
    )
    assert (
        seam_choice(_aired(SegmentType.BANTER, interrupt=True), _segment(SegmentType.MUSIC)).skip_reason
        == SKIP_INTERRUPT
    )


@pytest.mark.parametrize("relative", [MUSIC_TO_SPEECH, SPEECH_TO_MUSIC, AD_IN, AD_OUT])
def test_real_packaged_cart_matches_stream_format(relative):
    path = default_imaging_assets_dir() / relative
    audio = load_config(TOML_PATH).audio
    assert validated_playable_bytes(path, sample_rate=audio.sample_rate, bitrate=audio.bitrate, channels=audio.channels)


@pytest.mark.parametrize("reservoir", [0, 1])
def test_crc_frames_read_reservoir_after_checksum(tmp_path, reservoir):
    payload = _frames(4, crc=True, reservoir=reservoir)
    path = _install(tmp_path, payload)
    assert validated_playable_bytes(path, sample_rate=48000, bitrate=192) == (payload if reservoir == 0 else None)


@pytest.mark.parametrize(
    "payload",
    [
        _frames(4, channel_mode=3),
        _frames(2) + _frames(2, channel_mode=1),
        _frames(2) + _frames(2, bitrate_index=9),
    ],
)
def test_every_frame_must_match_bitrate_and_channel_mode(tmp_path, payload):
    assert validated_playable_bytes(_install(tmp_path, payload), sample_rate=48000, bitrate=192) is None


def test_oversized_cart_is_rejected_before_frame_indexing(tmp_path):
    path = _install(tmp_path, b"x" * (boundary_glue.BOUNDARY_MAX_BYTES + 2))
    with patch.object(boundary_glue, "build_playable_mpeg1_layer3_frame_index") as index:
        assert validated_playable_bytes(path, sample_rate=48000, bitrate=192) is None
    index.assert_not_called()


def test_cart_read_is_bounded_and_transient_failure_is_retried(tmp_path, caplog):
    from unittest.mock import mock_open

    payload = _frames(4)
    path = _install(tmp_path, payload)
    with patch.object(Path, "open", side_effect=OSError("temporarily unavailable")):
        assert validated_playable_bytes(path, sample_rate=48000, bitrate=192) is None
        assert validated_playable_bytes(path, sample_rate=48000, bitrate=192) is None
    assert not boundary_glue._VALIDATION_CACHE
    assert caplog.text.count("Boundary imaging asset unusable") == 1
    opened = mock_open(read_data=payload)
    with patch.object(Path, "open", opened):
        assert validated_playable_bytes(path, sample_rate=48000, bitrate=192) == payload
    opened().read.assert_called_once_with(boundary_glue.BOUNDARY_MAX_BYTES + 1)


def test_repaired_invalid_cart_recovers_without_restart(tmp_path):
    path = _install(tmp_path, b"broken")
    assert validated_playable_bytes(path, sample_rate=48000, bitrate=192) is None
    path.write_bytes(_frames(4))
    assert validated_playable_bytes(path, sample_rate=48000, bitrate=192) == _frames(4)


def test_packaged_lookup_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(_frames(4))
    root = tmp_path / "pack"
    root.mkdir()
    (root / "escaped.mp3").symlink_to(outside)
    assert ImagingLibrary.packaged_boundary_asset("escaped.mp3", assets_dir=root) is None


@pytest.mark.parametrize("payload", [b"", b"not an mp3", b"ID3\x04\0\0\0\0\0\0", _frames(2)[:-1]])
def test_malformed_cart_is_a_clean_cut_and_warns_once(tmp_path, caplog, payload):
    path = _install(tmp_path, payload)
    for _ in range(2):
        assert validated_playable_bytes(path, sample_rate=48000, bitrate=192) is None
    warnings = [r for r in caplog.records if "Boundary imaging asset unusable" in r.message]
    assert len(warnings) == 1
