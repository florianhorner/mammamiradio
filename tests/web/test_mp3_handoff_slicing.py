"""Frame-accurate music handoff artifacts keep every source frame on air once."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from mammamiradio.web.mp3_frames import (
    Mp3FrameIndexError,
    _skip_id3_and_xing_header,
    build_playable_mpeg1_layer3_frame_index,
    split_mpeg1_l3_handoff,
)

_SAMPLE_RATE = 48_000
_FRAME_DURATION = 1152 / _SAMPLE_RATE


def _header(bitrate_index: int = 11) -> bytes:
    """Return a valid MPEG-1 Layer III stereo header at 48 kHz."""

    return bytes((0xFF, 0xFB, (bitrate_index << 4) | 0x04, 0x00))


def _frame(marker: bytes, *, bitrate_index: int = 11) -> bytes:
    header = _header(bitrate_index)
    bitrate_kbps = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)[bitrate_index]
    frame_length = 144 * bitrate_kbps * 1000 // _SAMPLE_RATE
    assert len(marker) <= frame_length - 4
    return header + marker + b"\0" * (frame_length - 4 - len(marker))


def _id3v2(payload_size: int = 0) -> bytes:
    return (
        b"ID3\x04\x00\x00"
        + bytes(
            (
                (payload_size >> 21) & 0x7F,
                (payload_size >> 14) & 0x7F,
                (payload_size >> 7) & 0x7F,
                payload_size & 0x7F,
            )
        )
        + b"I" * payload_size
    )


def _info_frame() -> bytes:
    """A CBR Info frame at the exact offset consumed by the live player."""

    return _frame(b"\0" * 32 + b"Info" + b"\0" * 12)


def _audio_frames(count: int, *, bitrates: tuple[int, ...] = (11,)) -> bytes:
    return b"".join(
        _frame(f"frame-{ordinal:04d}".encode(), bitrate_index=bitrates[ordinal % len(bitrates)])
        for ordinal in range(count)
    )


def test_handoff_artifacts_partition_playable_id3_info_source_at_frame_boundaries(tmp_path: Path) -> None:
    """The player-visible run is split once, after its ID3/Info metadata is skipped."""

    playable = _audio_frames(12)
    raw = _id3v2(7) + _info_frame() + playable
    source = tmp_path / "source.mp3"
    source.write_bytes(raw)

    split = split_mpeg1_l3_handoff(
        source,
        tmp_path / "handoff",
        tail_seconds=2.5 * _FRAME_DURATION,
        stem="song-to-banter",
    )

    head = split.head_path.read_bytes()
    tail = split.tail_path.read_bytes()
    assert split.playable_start_byte == len(raw) - len(playable)
    assert split.head_end_byte == split.tail_start_byte
    assert raw[split.playable_start_byte : split.head_end_byte] == head
    assert raw[split.head_end_byte : split.playable_end_byte] == tail
    assert head + tail == playable
    assert split.head_frame_count + split.tail_frame_count == split.frame_count == 12
    assert split.head_duration_sec + split.tail_duration_sec == pytest.approx(split.source_duration_sec)
    assert 0 < split.tail_duration_sec <= 2.5 * _FRAME_DURATION
    assert 2.5 * _FRAME_DURATION - split.tail_duration_sec < _FRAME_DURATION

    # The generated pieces contain no leading metadata frames. The playback
    # helper therefore starts at byte zero for both, as it will on air.
    for artifact in (head, tail):
        stream = io.BytesIO(artifact)
        _skip_id3_and_xing_header(stream)
        assert stream.tell() == 0

    head_index = build_playable_mpeg1_layer3_frame_index(head)
    tail_index = build_playable_mpeg1_layer3_frame_index(tail)
    assert len(head_index.frames) == split.head_frame_count
    assert len(tail_index.frames) == split.tail_frame_count
    combined = head + tail
    for ordinal in range(12):
        assert combined.count(f"frame-{ordinal:04d}".encode()) == 1


def test_handoff_artifacts_keep_vbr_frame_lengths_and_order(tmp_path: Path) -> None:
    """Each frame supplies its own length; the split never estimates from bitrate."""

    bitrates = (9, 10, 12, 8)
    raw = _audio_frames(16, bitrates=bitrates)
    source = tmp_path / "vbr.mp3"
    source.write_bytes(raw)

    split = split_mpeg1_l3_handoff(
        source,
        tmp_path / "handoff",
        tail_seconds=4.2 * _FRAME_DURATION,
        stem="variable",
    )

    head = split.head_path.read_bytes()
    tail = split.tail_path.read_bytes()
    assert head + tail == raw
    assert len({frame.byte_end - frame.byte_start for frame in build_playable_mpeg1_layer3_frame_index(raw).frames}) > 1
    assert len(build_playable_mpeg1_layer3_frame_index(head).frames) == split.head_frame_count
    assert len(build_playable_mpeg1_layer3_frame_index(tail).frames) == split.tail_frame_count
    for ordinal in range(16):
        assert (head + tail).count(f"frame-{ordinal:04d}".encode()) == 1


@pytest.mark.requires_ffmpeg
@pytest.mark.parametrize("vbr", [False, True])
def test_handoff_artifacts_decode_and_partition_real_ffmpeg_mp3(tmp_path: Path, vbr: bool) -> None:
    """A real CBR or VBR source stays decodable after its final frames move."""

    source = tmp_path / ("vbr.mp3" if vbr else "cbr.mp3")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        "12",
        "-c:a",
        "libmp3lame",
    ]
    command.extend(["-q:a", "4"] if vbr else ["-b:a", "192k"])
    command.append(str(source))
    subprocess.run(command, check=True)

    split = split_mpeg1_l3_handoff(source, tmp_path / "handoff", tail_seconds=8.0, stem="real")
    for artifact in (split.head_path, split.tail_path):
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(artifact), "-f", "null", "-"],
            check=True,
        )
    source_data = source.read_bytes()
    assert (
        split.head_path.read_bytes() + split.tail_path.read_bytes()
        == source_data[split.playable_start_byte : split.playable_end_byte]
    )


@pytest.mark.parametrize("tail_seconds", [0, -1, 100.0])
def test_handoff_refuses_invalid_or_too_short_partition_without_artifacts(tmp_path: Path, tail_seconds: float) -> None:
    """A source that cannot keep both sides whole falls back before any file is published."""

    source = tmp_path / "short.mp3"
    source.write_bytes(_audio_frames(4))

    with pytest.raises(Mp3FrameIndexError):
        split_mpeg1_l3_handoff(source, tmp_path / "handoff", tail_seconds, stem="refuse")

    output_dir = tmp_path / "handoff"
    if output_dir.exists():
        assert not list(output_dir.glob("*.mp3"))


def test_handoff_refuses_malformed_playable_interior_without_artifacts(tmp_path: Path) -> None:
    """One broken source frame must never produce an uncertain head or tail."""

    raw = bytearray(_audio_frames(8))
    frame_length = len(_frame(b"marker"))
    raw[4 * frame_length : 4 * frame_length + 4] = b"BAD!"
    source = tmp_path / "broken.mp3"
    source.write_bytes(raw)

    with pytest.raises(Mp3FrameIndexError, match="malformed"):
        split_mpeg1_l3_handoff(source, tmp_path / "handoff", tail_seconds=2 * _FRAME_DURATION, stem="broken")

    assert not (tmp_path / "handoff").exists()
