"""Pure seam picker and packaged-asset proof for the playback cart."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest

from mammamiradio.audio.imaging import ImagingLibrary
from mammamiradio.core.config import StationConfig, load_config
from mammamiradio.core.models import Segment, SegmentType, StationState
from mammamiradio.scheduling import boundary_glue
from mammamiradio.scheduling.boundary_glue import (
    AD_IN,
    AD_OUT,
    MUSIC_TO_SPEECH,
    SKIP_NEXT_ERROR,
    SKIP_NEXT_LATCHED,
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
from mammamiradio.scheduling.producer import _maybe_add_transition_sting

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")
_BITRATES = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)


def _frames(
    count: int,
    marker: bytes = b"CART",
    *,
    bitrate_index: int = 11,
    sample_rate_idx: int = 1,
    reservoir: int = 0,
) -> bytes:
    bitrate = _BITRATES[bitrate_index]
    sample_rate = (44100, 48000, 32000)[sample_rate_idx]
    frame_length = 144 * bitrate * 1000 // sample_rate
    header = bytes((0xFF, 0xFB, (bitrate_index << 4) | (sample_rate_idx << 2), 0x00))
    side = bytes(((reservoir >> 1) & 0xFF, (reservoir & 1) << 7))
    payload = side + marker
    padding = frame_length - 4 - len(payload)
    assert padding >= 0
    return (header + payload + b"\0" * padding) * count


def _segment(kind: SegmentType, **metadata) -> Segment:
    return Segment(type=kind, path=Path("programme.mp3"), metadata=metadata, ephemeral=False)


def _aired(kind: SegmentType, *, rescue: bool = False, generation: int = 1) -> AiredBoundary:
    return AiredBoundary(kind=kind, rescue=rescue, generation=generation)


@pytest.fixture(autouse=True)
def _clear_asset_cache():
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
        (_aired(SegmentType.MUSIC), _segment(SegmentType.AD), AD_IN),
        (_aired(SegmentType.AD), _segment(SegmentType.MUSIC), AD_OUT),
        (_aired(SegmentType.MUSIC), _segment(SegmentType.NEWS_FLASH), MUSIC_TO_SPEECH),
    )
    for previous, nxt, relative in cases:
        found = glue_for(previous, nxt, assets_dir=tmp_path)
        assert found == (tmp_path / relative).resolve()


def test_glue_for_missing_asset_is_a_clean_cut(tmp_path: Path):
    assert glue_for(_aired(SegmentType.MUSIC), _segment(SegmentType.BANTER), assets_dir=tmp_path) is None


def test_packaged_boundary_asset_rejects_missing_and_unsafe_paths(tmp_path: Path):
    library = ImagingLibrary([], tmp_path, assets_dir=tmp_path)
    assert library.packaged_boundary_asset(MUSIC_TO_SPEECH) is None
    (tmp_path / "stingers").mkdir()
    assert library.packaged_boundary_asset(MUSIC_TO_SPEECH) is None
    assert library.packaged_boundary_asset("../secrets.mp3") is None
    assert library.packaged_boundary_asset("") is None


def test_seam_skips_previous_none_imaging_and_rescue():
    speech = _segment(SegmentType.BANTER)
    music = _segment(SegmentType.MUSIC)
    assert seam_choice(None, speech).skip_reason == SKIP_PREV_NONE
    assert seam_choice(_aired(SegmentType.SWEEPER), music).skip_reason == SKIP_PREV_IMAGING
    assert seam_choice(_aired(SegmentType.STATION_ID), music).skip_reason == SKIP_PREV_IMAGING
    assert seam_choice(_aired(SegmentType.TIME_CHECK), music).skip_reason == SKIP_PREV_IMAGING
    assert seam_choice(_aired(SegmentType.MUSIC, rescue=True), speech).skip_reason == SKIP_PREV_RESCUE
    assert glue_for(None, speech, assets_dir=Path(".")) is None


def test_seam_skips_next_latch_rescue_error_and_music_tail():
    previous = _aired(SegmentType.MUSIC)
    latched = seam_choice(previous, _segment(SegmentType.BANTER, boundary_sting_merged=True))
    assert latched.skip_reason == SKIP_NEXT_LATCHED
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


def test_validation_cache_survives_a_deleted_file(tmp_path: Path):
    path = _install(tmp_path, _frames(4))
    first = validated_playable_bytes(path, sample_rate=48000, bitrate=192)
    path.unlink()
    assert validated_playable_bytes(path, sample_rate=48000, bitrate=192) == first


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


@pytest.mark.asyncio
async def test_latch_is_set_only_on_the_merged_return(tmp_path: Path):
    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"voice")
    segment = Segment(type=SegmentType.BANTER, path=voice, metadata={"title": "Talk"}, ephemeral=False)
    config = cast(StationConfig, SimpleNamespace(tmp_dir=tmp_path))
    state = StationState()

    def _pick(_from_seg, _to_seg, output_path: Path) -> Path:
        output_path.write_bytes(b"sting")
        return output_path

    def _concat(_paths, output_path: Path, _fade, _flag) -> None:
        output_path.write_bytes(b"merged")

    async def _inline(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    library = SimpleNamespace(pick_stinger=_pick)
    with (
        patch("mammamiradio.scheduling.producer._make_imaging_lib", return_value=library),
        patch("mammamiradio.scheduling.producer.concat_files", _concat),
        patch("mammamiradio.scheduling.producer._run_owned_thread", _inline),
    ):
        merged = await _maybe_add_transition_sting(segment, SegmentType.MUSIC, config, state)
    assert merged.metadata["boundary_sting_merged"] is True
    assert "boundary_sting_merged" not in segment.metadata
    assert merged.path != segment.path

    untouched = await _maybe_add_transition_sting(segment, None, config, state)
    assert "boundary_sting_merged" not in untouched.metadata

    rescue = Segment(type=SegmentType.BANTER, path=voice, metadata={"rescue": True}, ephemeral=False)
    rescued = await _maybe_add_transition_sting(rescue, SegmentType.MUSIC, config, state)
    assert "boundary_sting_merged" not in rescued.metadata

    def _boom(*_args, **_kwargs):
        raise RuntimeError("synth failed")

    library.pick_stinger = _boom
    clean = Segment(type=SegmentType.BANTER, path=voice, metadata={}, ephemeral=False)
    with (
        patch("mammamiradio.scheduling.producer._make_imaging_lib", return_value=library),
        patch("mammamiradio.scheduling.producer.concat_files", _concat),
        patch("mammamiradio.scheduling.producer._run_owned_thread", _inline),
    ):
        failed = await _maybe_add_transition_sting(clean, SegmentType.MUSIC, config, state)
    assert failed is clean
    assert "boundary_sting_merged" not in failed.metadata
