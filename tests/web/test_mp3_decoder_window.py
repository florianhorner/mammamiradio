"""Pure decoder-window bounds for frame-indexed MPEG-1 Layer III audio."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from mammamiradio.web.mp3_frames import (
    Mp3DecoderWindow,
    Mp3Frame,
    Mp3FrameIndex,
    Mp3FrameIndexError,
    mpeg1_l3_decoder_window,
)

_SAMPLES_PER_FRAME = 1152


def _index(frame_count: int = 24) -> Mp3FrameIndex:
    byte_start = 101
    frame_bytes = 417
    sample_rate = 48_000
    duration = _SAMPLES_PER_FRAME / sample_rate
    frames = tuple(
        Mp3Frame(
            ordinal=ordinal,
            byte_start=byte_start + ordinal * frame_bytes,
            byte_end=byte_start + (ordinal + 1) * frame_bytes,
            duration_sec=duration,
            start_sec=ordinal * duration,
            end_sec=(ordinal + 1) * duration,
        )
        for ordinal in range(frame_count)
    )
    return Mp3FrameIndex(
        frames=frames,
        data_start=frames[0].byte_start,
        data_end=frames[-1].byte_end,
        sample_rate=sample_rate,
    )


def test_decoder_window_uses_at_most_ten_preroll_frames() -> None:
    index = _index()

    window = mpeg1_l3_decoder_window(index, 15, 20, lower_bound=0)

    assert window == Mp3DecoderWindow(
        decoder_byte_start=index.frames[5].byte_start,
        decoder_byte_end=index.frames[19].byte_end,
        preroll_samples=10 * _SAMPLES_PER_FRAME,
        audible_sample_count=5 * _SAMPLES_PER_FRAME,
        sample_rate=48_000,
    )
    with pytest.raises(FrozenInstanceError):
        window.preroll_samples = 0  # type: ignore[misc]


def test_decoder_window_never_crosses_caller_lower_bound() -> None:
    index = _index()

    window = mpeg1_l3_decoder_window(index, 15, 18, lower_bound=12)

    assert window.decoder_byte_start == index.frames[12].byte_start
    assert window.decoder_byte_end == index.frames[17].byte_end
    assert window.preroll_samples == 3 * _SAMPLES_PER_FRAME
    assert window.audible_sample_count == 3 * _SAMPLES_PER_FRAME


def test_decoder_window_can_start_at_first_audible_frame() -> None:
    index = _index()

    window = mpeg1_l3_decoder_window(index, 4, 7, lower_bound=4)

    assert window.decoder_byte_start == index.frames[4].byte_start
    assert window.preroll_samples == 0


@pytest.mark.parametrize(
    ("audible_first", "audible_end", "lower_bound"),
    [
        (-1, 2, 0),
        (2, 2, 0),
        (2, 25, 0),
        (2, 3, -1),
        (2, 3, 3),
        (True, 3, 0),
        (2, False, 0),
        (2, 3, 0.0),
    ],
)
def test_decoder_window_rejects_invalid_ordinals(
    audible_first: Any,
    audible_end: Any,
    lower_bound: Any,
) -> None:
    with pytest.raises(Mp3FrameIndexError, match="invalid decoder window frame range"):
        mpeg1_l3_decoder_window(
            _index(),
            audible_first,
            audible_end,
            lower_bound=lower_bound,
        )
