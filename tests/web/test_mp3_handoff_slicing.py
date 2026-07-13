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
_PCM_BYTES_PER_MPEG_FRAME = 1152 * 2 * 2  # samples * stereo channels * s16 bytes


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


def _syncsafe(size: int) -> bytes:
    return bytes(
        (
            (size >> 21) & 0x7F,
            (size >> 14) & 0x7F,
            (size >> 7) & 0x7F,
            size & 0x7F,
        )
    )


def _id3v24_with_footer() -> bytes:
    """One valid text frame wrapped in a mirrored ID3v2.4 header/footer."""

    title_frame = b"TIT2" + _syncsafe(1) + b"\0\0" + b"\0"
    header_tail = b"\x04\x00\x10" + _syncsafe(len(title_frame))
    return b"ID3" + header_tail + title_frame + b"3DI" + header_tail


def _id3v1() -> bytes:
    return b"TAG" + b"\0" * 125


def _info_frame() -> bytes:
    """A CBR Info frame at the exact offset consumed by the live player."""

    return _frame(b"\0" * 32 + b"Info" + b"\0" * 12)


def _audio_frames(count: int, *, bitrates: tuple[int, ...] = (11,)) -> bytes:
    return b"".join(
        _frame(f"frame-{ordinal:04d}".encode(), bitrate_index=bitrates[ordinal % len(bitrates)])
        for ordinal in range(count)
    )


def _owned_tail_bytes(raw: bytes, split) -> bytes:
    tail_input = split.tail_path.read_bytes()
    assert split.tail_decode_start_byte is not None
    assert split.tail_owned_offset_byte == split.head_end_byte - split.tail_decode_start_byte
    return tail_input[split.tail_owned_offset_byte :]


def _decode_s16le(path: Path, *, audio_filter: str | None = None) -> bytes:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path)]
    if audio_filter is not None:
        command.extend(["-af", audio_filter])
    command.extend(["-f", "s16le", "-acodec", "pcm_s16le", "-ac", "2", "-ar", str(_SAMPLE_RATE), "-"])
    return subprocess.run(command, check=True, capture_output=True).stdout


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
    tail_input = split.tail_path.read_bytes()
    assert split.playable_start_byte == len(raw) - len(playable)
    assert split.head_end_byte == split.tail_start_byte
    assert raw[split.playable_start_byte : split.head_end_byte] == head
    assert raw[split.head_end_byte : split.playable_end_byte] == _owned_tail_bytes(raw, split)
    assert split.tail_decode_path == split.tail_path
    assert split.tail_decode_start_byte == split.playable_start_byte
    assert split.tail_preroll_frame_count == 10
    assert split.tail_preroll_samples == 10 * 1152
    assert split.tail_sample_count == split.tail_frame_count * 1152
    assert tail_input == playable
    assert split.head_frame_count + split.tail_frame_count == split.frame_count == 12
    assert split.head_duration_sec + split.tail_duration_sec == pytest.approx(split.source_duration_sec)
    assert 0 < split.tail_duration_sec <= 2.5 * _FRAME_DURATION
    assert 2.5 * _FRAME_DURATION - split.tail_duration_sec < _FRAME_DURATION

    # The generated pieces contain no leading metadata frames. The playback
    # helper therefore starts at byte zero for both, as it will on air.
    for artifact in (head, tail_input):
        stream = io.BytesIO(artifact)
        _skip_id3_and_xing_header(stream)
        assert stream.tell() == 0

    head_index = build_playable_mpeg1_layer3_frame_index(head)
    tail_index = build_playable_mpeg1_layer3_frame_index(tail_input)
    assert len(head_index.frames) == split.head_frame_count
    assert len(tail_index.frames) == split.tail_preroll_frame_count + split.tail_frame_count
    for ordinal in range(split.tail_frame_count):
        marker = f"frame-{split.head_frame_count + ordinal:04d}".encode()
        assert _owned_tail_bytes(raw, split).count(marker) == 1


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
    tail_input = split.tail_path.read_bytes()
    assert head == raw[: split.head_end_byte]
    assert _owned_tail_bytes(raw, split) == raw[split.head_end_byte : split.playable_end_byte]
    assert len({frame.byte_end - frame.byte_start for frame in build_playable_mpeg1_layer3_frame_index(raw).frames}) > 1
    assert len(build_playable_mpeg1_layer3_frame_index(head).frames) == split.head_frame_count
    assert len(build_playable_mpeg1_layer3_frame_index(tail_input).frames) == (
        split.tail_preroll_frame_count + split.tail_frame_count
    )
    for ordinal in range(split.head_frame_count, 16):
        assert _owned_tail_bytes(raw, split).count(f"frame-{ordinal:04d}".encode()) == 1


def test_handoff_decoder_preroll_uses_all_available_head_frames_near_start(tmp_path: Path) -> None:
    raw = _audio_frames(6)
    source = tmp_path / "short-context.mp3"
    source.write_bytes(raw)

    split = split_mpeg1_l3_handoff(
        source,
        tmp_path / "handoff",
        tail_seconds=4.2 * _FRAME_DURATION,
        stem="short-context",
    )

    assert split.head_frame_count == 2
    assert split.tail_preroll_frame_count == split.head_frame_count
    assert split.tail_preroll_samples == 2 * 1152
    assert split.tail_decode_start_byte == split.playable_start_byte == 0
    assert split.tail_path.read_bytes() == raw


def test_playable_index_accepts_valid_id3v24_footer_before_audio() -> None:
    tag = _id3v24_with_footer()
    playable = _audio_frames(4)
    stream = io.BytesIO(tag + playable)

    _skip_id3_and_xing_header(stream)
    index = build_playable_mpeg1_layer3_frame_index(tag + playable)

    assert stream.tell() == len(tag)
    assert index.data_start == len(tag)
    assert index.data_end == len(tag) + len(playable)


def test_playable_index_rejects_mismatched_id3v24_footer() -> None:
    tag = bytearray(_id3v24_with_footer())
    tag[-10:-7] = b"BAD"

    stream = io.BytesIO(bytes(tag) + _audio_frames(4))
    _skip_id3_and_xing_header(stream)

    assert stream.tell() == 0
    with pytest.raises(Mp3FrameIndexError, match="malformed"):
        build_playable_mpeg1_layer3_frame_index(bytes(tag) + _audio_frames(4))


def test_truncated_appended_id3v24_footer_fails_closed_without_artifacts(tmp_path: Path) -> None:
    """An incomplete EOF tag is unknown trailer data, never removable metadata."""

    truncated_tag = _id3v24_with_footer()[:-5]
    raw = _audio_frames(12) + truncated_tag
    source = tmp_path / "truncated-appended-tag.mp3"
    source.write_bytes(raw)

    with pytest.raises(Mp3FrameIndexError, match="malformed"):
        build_playable_mpeg1_layer3_frame_index(raw)
    with pytest.raises(Mp3FrameIndexError, match="malformed"):
        split_mpeg1_l3_handoff(
            source,
            tmp_path / "handoff",
            tail_seconds=2.5 * _FRAME_DURATION,
            stem="truncated-appended-tag",
        )

    assert not (tmp_path / "handoff").exists()


@pytest.mark.parametrize(
    ("malformed_field", "header_tail"),
    [
        ("reserved flags", b"\x04\x00\x01" + _syncsafe(0)),
        ("non-syncsafe size", b"\x04\x00\x00\x80\x00\x00\x00"),
    ],
)
def test_malformed_id3v24_header_fails_closed_without_artifacts(
    tmp_path: Path,
    malformed_field: str,
    header_tail: bytes,
) -> None:
    """Reserved flags and size high bits cannot be masked into a valid tag."""

    raw = b"ID3" + header_tail + _audio_frames(12)
    stream = io.BytesIO(raw)
    source = tmp_path / f"malformed-v24-{malformed_field.replace(' ', '-')}.mp3"
    source.write_bytes(raw)

    _skip_id3_and_xing_header(stream)

    assert stream.tell() == 0
    with pytest.raises(Mp3FrameIndexError, match="malformed"):
        build_playable_mpeg1_layer3_frame_index(raw)
    with pytest.raises(Mp3FrameIndexError, match="malformed"):
        split_mpeg1_l3_handoff(
            source,
            tmp_path / "handoff",
            tail_seconds=2.5 * _FRAME_DURATION,
            stem="malformed-v24",
        )
    assert not (tmp_path / "handoff").exists()


def test_id3v23_footer_flag_is_illegal_and_fails_closed_without_artifacts(tmp_path: Path) -> None:
    """The v2.4 footer bit is reserved in v2.3 and cannot authorize a skip."""

    raw = b"ID3\x03\x00\x10" + _syncsafe(0) + _audio_frames(12)
    stream = io.BytesIO(raw)
    source = tmp_path / "illegal-v23-footer.mp3"
    source.write_bytes(raw)

    _skip_id3_and_xing_header(stream)

    assert stream.tell() == 0
    with pytest.raises(Mp3FrameIndexError, match="malformed"):
        build_playable_mpeg1_layer3_frame_index(raw)
    with pytest.raises(Mp3FrameIndexError, match="malformed"):
        split_mpeg1_l3_handoff(
            source,
            tmp_path / "handoff",
            tail_seconds=2.5 * _FRAME_DURATION,
            stem="illegal-v23-footer",
        )
    assert not (tmp_path / "handoff").exists()


def test_playable_index_excludes_id3v1_and_appended_id3v24_trailers(tmp_path: Path) -> None:
    playable = _audio_frames(12)
    appended_v2 = _id3v24_with_footer()
    raw = playable + appended_v2 + _id3v1()
    source = tmp_path / "tagged.mp3"
    source.write_bytes(raw)

    index = build_playable_mpeg1_layer3_frame_index(raw)
    split = split_mpeg1_l3_handoff(
        source,
        tmp_path / "handoff",
        tail_seconds=2.5 * _FRAME_DURATION,
        stem="tagged",
    )

    assert index.data_end == len(playable)
    assert split.playable_end_byte == len(playable)
    assert _owned_tail_bytes(raw, split) == playable[split.head_end_byte :]
    assert appended_v2 not in split.tail_path.read_bytes()
    assert _id3v1() not in split.tail_path.read_bytes()


def test_playable_index_keeps_unknown_trailer_strict() -> None:
    with pytest.raises(Mp3FrameIndexError, match="malformed"):
        build_playable_mpeg1_layer3_frame_index(_audio_frames(4) + b"NOPE")


@pytest.mark.requires_ffmpeg
@pytest.mark.parametrize(
    ("encoding", "encoder_args"),
    [
        ("cbr", ["-b:a", "192k"]),
        ("vbr", ["-q:a", "4"]),
        ("minimum-bitrate", ["-b:a", "32k"]),
    ],
)
def test_handoff_artifacts_decode_and_partition_real_ffmpeg_mp3(
    tmp_path: Path,
    encoding: str,
    encoder_args: list[str],
) -> None:
    """CBR, VBR, and minimum-bitrate tails decode like the continuous source."""

    source = tmp_path / f"{encoding}.mp3"
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
        "-ac",
        "2",
        "-c:a",
        "libmp3lame",
    ]
    command.extend(encoder_args)
    command.append(str(source))
    subprocess.run(command, check=True)

    split = split_mpeg1_l3_handoff(source, tmp_path / "handoff", tail_seconds=8.0, stem="real")
    for artifact in (split.head_path, split.tail_path):
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(artifact), "-f", "null", "-"],
            check=True,
        )
    source_data = source.read_bytes()
    assert split.head_path.read_bytes() == source_data[split.playable_start_byte : split.head_end_byte]
    assert _owned_tail_bytes(source_data, split) == source_data[split.head_end_byte : split.playable_end_byte]

    continuous_path = tmp_path / "continuous-playable.mp3"
    continuous_path.write_bytes(source_data[split.playable_start_byte : split.playable_end_byte])
    standalone_tail = tmp_path / "standalone-tail.mp3"
    standalone_tail.write_bytes(source_data[split.head_end_byte : split.playable_end_byte])

    continuous_pcm = _decode_s16le(continuous_path)
    expected_tail_pcm = continuous_pcm[split.head_frame_count * _PCM_BYTES_PER_MPEG_FRAME :]
    standalone_pcm = _decode_s16le(standalone_tail)
    trimmed_decoder_pcm = _decode_s16le(
        split.tail_path,
        audio_filter=(
            f"atrim=start_sample={split.tail_preroll_samples}:"
            f"end_sample={split.tail_preroll_samples + split.tail_sample_count},"
            "asetpts=PTS-STARTPTS"
        ),
    )

    assert standalone_pcm[: 2 * _PCM_BYTES_PER_MPEG_FRAME] != expected_tail_pcm[: 2 * _PCM_BYTES_PER_MPEG_FRAME]
    assert trimmed_decoder_pcm == expected_tail_pcm


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
