"""MPEG-1 Layer III frame parsing for streaming and frame-safe audio cuts.

The playback loop calls :func:`_skip_id3_and_xing_header` on every segment so
concatenated MP3s look like one continuous ICEcast feed.  The handoff helper
uses the same playable start and only exposes whole compatible audio frames,
which lets a song's final frames move into a host transition without being
streamed twice.
"""

from __future__ import annotations

import io
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

_MPEG1_L3_BITRATES_KBPS = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_MPEG1_SAMPLE_RATES = (44100, 48000, 32000)
_MAX_MPEG1_L3_FRAME_BYTES = 1441
_MPEG1_L3_SAMPLES_PER_FRAME = 1152
_MAX_DECODER_PREROLL_FRAMES = 10
_ID3V1_TAG_BYTES = 128
_ID3V2_HEADER_BYTES = 10
_ATOMIC_PART_PREFIX = ".mmr-atomic-"


class Mp3FrameIndexError(ValueError):
    """The source cannot be proven to be a complete compatible frame run."""


@dataclass(frozen=True)
class Mp3Frame:
    """One complete MPEG-1 Layer III frame in the indexed byte sequence."""

    ordinal: int
    byte_start: int
    byte_end: int
    duration_sec: float
    start_sec: float
    end_sec: float


@dataclass(frozen=True)
class Mp3FrameIndex:
    """Frame-accurate time/byte mapping for one compatible MPEG-1 Layer III run.

    ``byte_*`` offsets are relative to the original bytes passed to the index
    builder. Leading/trailing partial retained frames are intentionally absent;
    every exposed boundary is a whole frame.
    """

    frames: tuple[Mp3Frame, ...]
    data_start: int
    data_end: int
    sample_rate: int

    @property
    def duration_sec(self) -> float:
        return self.frames[-1].end_sec if self.frames else 0.0

    def frame_range(self, start_sec: float, end_sec: float) -> tuple[int, int] | None:
        """Return a whole-frame interval contained within ``[start_sec, end_sec]``.

        Start rounds forward and end rounds backward. That never expands a
        bounded replay because of frame quantization.
        """

        if end_sec <= start_sec:
            return None
        first = next((f.ordinal for f in self.frames if f.start_sec >= start_sec), None)
        last = next((f.ordinal for f in reversed(self.frames) if f.end_sec <= end_sec), None)
        if first is None or last is None or first > last:
            return None
        return first, last + 1

    def byte_range(self, first: int, end: int) -> tuple[int, int]:
        if first < 0 or end <= first or end > len(self.frames):
            raise Mp3FrameIndexError("invalid frame range")
        return self.frames[first].byte_start, self.frames[end - 1].byte_end

    def duration_for(self, first: int, end: int) -> float:
        if first < 0 or end <= first or end > len(self.frames):
            raise Mp3FrameIndexError("invalid frame range")
        return self.frames[end - 1].end_sec - self.frames[first].start_sec

    def start_for(self, first: int) -> float:
        if first < 0 or first >= len(self.frames):
            raise Mp3FrameIndexError("invalid frame ordinal")
        return self.frames[first].start_sec

    def end_for(self, end: int) -> float:
        if end <= 0 or end > len(self.frames):
            raise Mp3FrameIndexError("invalid frame ordinal")
        return self.frames[end - 1].end_sec


@dataclass(frozen=True)
class Mp3HandoffSplit:
    """A frame-aligned partition plus a decoder-safe tail input.

    The byte offsets describe ownership in the original source. ``head_path``
    contains only frames before ``head_end_byte``. ``tail_path`` is a private
    decoder input: it may prepend a bounded set of already-owned head frames so
    Layer III's bit reservoir and synthesis state are available, but consumers
    trim exactly ``tail_preroll_samples`` before the artifact can reach air.

    ``tail_decode_path`` names that intent explicitly. It currently aliases
    ``tail_path`` so older fixtures and cleanup code retain a single tail file.
    """

    head_path: Path
    tail_path: Path
    playable_start_byte: int
    head_end_byte: int
    playable_end_byte: int
    head_duration_sec: float
    tail_duration_sec: float
    source_duration_sec: float
    frame_count: int
    head_frame_count: int
    tail_frame_count: int
    tail_decode_path: Path | None = None
    tail_decode_start_byte: int | None = None
    tail_preroll_frame_count: int = 0
    tail_preroll_samples: int = 0
    tail_sample_count: int = 0

    @property
    def tail_start_byte(self) -> int:
        """The source offset of the first frame owned by the audible tail."""

        return self.head_end_byte

    @property
    def tail_owned_offset_byte(self) -> int:
        """Byte offset of the audible tail inside its decoder input."""

        decode_start = self.tail_decode_start_byte
        return self.head_end_byte - (self.head_end_byte if decode_start is None else decode_start)


def _is_mpeg1_l3_header(frame_header: bytes, *, allow_free_bitrate: bool) -> bool:
    """Return whether ``frame_header`` is a plausible MPEG-1 Layer III frame."""
    if len(frame_header) < 4 or frame_header[0] != 0xFF or (frame_header[1] & 0xE0) != 0xE0:
        return False

    version = (frame_header[1] >> 3) & 0x03
    layer = (frame_header[1] >> 1) & 0x03
    bitrate_idx = (frame_header[2] >> 4) & 0x0F
    sample_rate_idx = (frame_header[2] >> 2) & 0x03

    if version != 3 or layer != 1 or sample_rate_idx == 3 or bitrate_idx == 0x0F:
        return False
    return not (not allow_free_bitrate and bitrate_idx == 0)


def _decode_syncsafe_size(encoded: bytes) -> int | None:
    """Decode one ID3 syncsafe integer, rejecting reserved high bits."""

    if len(encoded) != 4 or any(byte & 0x80 for byte in encoded):
        return None
    return (encoded[0] << 21) | (encoded[1] << 14) | (encoded[2] << 7) | encoded[3]


def _parse_id3v2_header(header: bytes, *, identifier: bytes) -> tuple[int, bool] | None:
    """Return ``(payload_size, footer_present)`` for a supported ID3 header."""

    if len(header) != _ID3V2_HEADER_BYTES or header[:3] != identifier:
        return None
    major, revision, flags = header[3], header[4], header[5]
    allowed_flags = {2: 0xC0, 3: 0xE0, 4: 0xF0}.get(major)
    size = _decode_syncsafe_size(header[6:10])
    if allowed_flags is None or revision == 0xFF or flags & ~allowed_flags or size is None:
        return None
    footer_present = major == 4 and bool(flags & 0x10)
    return size, footer_present


def _trailing_metadata_start(data: bytes, *, lower_bound: int) -> int:
    """Return the first byte of structurally proven EOF metadata.

    Unknown trailers remain inside the strict frame run and therefore fail
    closed. ID3v1 is peeled first because an appended ID3v2.4 tag is specified
    to sit immediately before older tagging systems such as ID3v1.
    """

    end = len(data)
    if end - lower_bound >= _ID3V1_TAG_BYTES and data[end - _ID3V1_TAG_BYTES : end - 125] == b"TAG":
        end -= _ID3V1_TAG_BYTES

    if end - lower_bound < 2 * _ID3V2_HEADER_BYTES:
        return end
    footer = data[end - _ID3V2_HEADER_BYTES : end]
    parsed_footer = _parse_id3v2_header(footer, identifier=b"3DI")
    if parsed_footer is None or not parsed_footer[1]:
        return end
    payload_size, _ = parsed_footer
    tag_start = end - (2 * _ID3V2_HEADER_BYTES + payload_size)
    if tag_start < lower_bound:
        return end
    header = data[tag_start : tag_start + _ID3V2_HEADER_BYTES]
    parsed_header = _parse_id3v2_header(header, identifier=b"ID3")
    if parsed_header == parsed_footer and header[3:] == footer[3:]:
        return tag_start
    return end


def _skip_id3_and_xing_header(f) -> None:
    """Advance the file pointer past any leading ID3v2 tag and Xing/Info metadata frame.

    Safari's ``<audio>`` element honors the Xing/Info duration header of each
    concatenated segment as end-of-track, causing short segments (banter ~9 s,
    news flash ~6 s) to fire ``ended`` at the declared duration instead of
    playing through the ongoing stream. Long music segments (180 s+) don't
    trip this because the listener tops up buffered bytes before the counter
    expires. Stripping the tag on every segment makes the stream look like a
    continuous ICECast feed, which all browsers handle correctly.

    The helper is defensive: any unexpected header shape rewinds to the start,
    so the worst case is "did nothing" rather than "cut a real audio frame".
    """
    header = f.read(_ID3V2_HEADER_BYTES)
    if len(header) == _ID3V2_HEADER_BYTES and header[:3] == b"ID3":
        parsed_header = _parse_id3v2_header(header, identifier=b"ID3")
        if parsed_header is None:
            f.seek(0)
            return
        size, footer_present = parsed_header
        f.seek(_ID3V2_HEADER_BYTES + size)
        if footer_present:
            footer = f.read(_ID3V2_HEADER_BYTES)
            if _parse_id3v2_header(footer, identifier=b"3DI") != parsed_header or footer[3:] != header[3:]:
                f.seek(0)
                return
    else:
        f.seek(0)

    frame_start = f.tell()
    frame_header = f.read(4)
    if not _is_mpeg1_l3_header(frame_header, allow_free_bitrate=True):
        f.seek(frame_start)
        return

    bitrate_idx = (frame_header[2] >> 4) & 0x0F
    sample_rate_idx = (frame_header[2] >> 2) & 0x03
    padding = (frame_header[2] >> 1) & 0x01
    channel_mode = (frame_header[3] >> 6) & 0x03

    magic_offset = 21 if channel_mode == 3 else 36
    f.seek(frame_start + magic_offset)
    magic = f.read(4)
    if magic not in (b"Xing", b"Info"):
        f.seek(frame_start)
        return

    if bitrate_idx == 0:
        # VBR info frame (free-format): frame_length is unknown from the header alone.
        # Scan forward from just after the Xing magic and only accept plausible
        # MPEG-1 Layer III headers so sync-like metadata bytes are ignored.
        f.seek(frame_start + magic_offset + 4)
        data = f.read(8192)
        sync_pos = -1
        for i in range(len(data) - 3):
            if _is_mpeg1_l3_header(data[i : i + 4], allow_free_bitrate=False):
                sync_pos = i
                break
        if sync_pos >= 0:
            f.seek(frame_start + magic_offset + 4 + sync_pos)
        else:
            f.seek(frame_start)
        return

    bitrate_kbps = _MPEG1_L3_BITRATES_KBPS[bitrate_idx]
    sample_rate = _MPEG1_SAMPLE_RATES[sample_rate_idx]
    frame_length = (144 * bitrate_kbps * 1000 // sample_rate) + padding
    f.seek(frame_start + frame_length)


def _parse_mpeg1_l3_header(frame_header: bytes) -> tuple[int, int] | None:
    """Return ``(frame_length, sample_rate)`` for an exact supported header."""

    if not _is_mpeg1_l3_header(frame_header, allow_free_bitrate=False):
        return None
    bitrate_idx = (frame_header[2] >> 4) & 0x0F
    sample_rate_idx = (frame_header[2] >> 2) & 0x03
    emphasis = frame_header[3] & 0x03
    if emphasis == 0x02:  # reserved
        return None
    bitrate_kbps = _MPEG1_L3_BITRATES_KBPS[bitrate_idx]
    sample_rate = _MPEG1_SAMPLE_RATES[sample_rate_idx]
    padding = (frame_header[2] >> 1) & 0x01
    frame_length = (144 * bitrate_kbps * 1000 // sample_rate) + padding
    if frame_length < 4:
        return None
    return frame_length, sample_rate


def _leading_complete_frame_start(data: bytes) -> int:
    """Find one full first frame only inside a retained leading partial edge.

    A ring snapshot may begin in the middle of a frame. We inspect at most one
    maximum frame length to discard that edge and require a second exact frame
    boundary before accepting it; this is not a raw sync scan through the
    capture body.
    """

    max_start = min(len(data) - 8, _MAX_MPEG1_L3_FRAME_BYTES)
    for offset in range(max(0, max_start + 1)):
        header = _parse_mpeg1_l3_header(data[offset : offset + 4])
        if header is None:
            continue
        frame_length, sample_rate = header
        next_offset = offset + frame_length
        if next_offset + 4 > len(data):
            continue
        next_header = _parse_mpeg1_l3_header(data[next_offset : next_offset + 4])
        if next_header is not None and next_header[1] == sample_rate:
            return offset
    raise Mp3FrameIndexError("no complete compatible MPEG-1 Layer III frame at retained edge")


def _index_complete_mpeg1_l3_run(
    data: bytes,
    *,
    start: int,
    strict_end: bool,
    end: int | None = None,
) -> Mp3FrameIndex:
    """Index a compatible frame run beginning at a known frame boundary.

    ``strict_end`` is for a complete source file: any bytes after the first
    frame that are not a whole compatible frame make the proof fail. Retained
    ring snapshots set it false so a partial trailing edge can be discarded.
    """

    run_end = len(data) if end is None else end
    if start < 0 or run_end > len(data) or start >= run_end:
        raise Mp3FrameIndexError("no playable MPEG-1 Layer III frame")

    offset = start
    frames: list[Mp3Frame] = []
    sample_rate: int | None = None
    elapsed = 0.0
    while offset < run_end:
        remaining = run_end - offset
        if remaining < 4:
            if strict_end:
                raise Mp3FrameIndexError("trailing partial MPEG-1 Layer III header")
            break
        header = _parse_mpeg1_l3_header(data[offset : offset + 4])
        if header is None:
            raise Mp3FrameIndexError("malformed MPEG-1 Layer III frame")
        frame_length, current_sample_rate = header
        if sample_rate is None:
            sample_rate = current_sample_rate
        elif current_sample_rate != sample_rate:
            raise Mp3FrameIndexError("incompatible MPEG-1 Layer III sample rate")
        if offset + frame_length > run_end:
            if strict_end:
                raise Mp3FrameIndexError("trailing partial MPEG-1 Layer III frame")
            break
        duration = _MPEG1_L3_SAMPLES_PER_FRAME / current_sample_rate
        frames.append(
            Mp3Frame(
                ordinal=len(frames),
                byte_start=offset,
                byte_end=offset + frame_length,
                duration_sec=duration,
                start_sec=elapsed,
                end_sec=elapsed + duration,
            )
        )
        elapsed += duration
        offset += frame_length

    if len(frames) < 2 or sample_rate is None:
        raise Mp3FrameIndexError("not enough complete MPEG-1 Layer III frames")
    return Mp3FrameIndex(tuple(frames), frames[0].byte_start, frames[-1].byte_end, sample_rate)


def build_mpeg1_layer3_frame_index(data: bytes) -> Mp3FrameIndex:
    """Index a retained MPEG-1 Layer III run without bitrate/time guessing.

    VBR is supported naturally: every frame supplies its own bitrate-derived
    length while all compatible frames share the same sample rate and each is
    exactly 1152 samples. A malformed interior frame fails closed. Only partial
    frames at the two retained edges are discarded.
    """

    if not data:
        raise Mp3FrameIndexError("empty audio")
    return _index_complete_mpeg1_l3_run(data, start=_leading_complete_frame_start(data), strict_end=False)


def build_playable_mpeg1_layer3_frame_index(data: bytes) -> Mp3FrameIndex:
    """Index every playable frame of a complete streamable MP3 source.

    The start is determined by the exact same ID3/Xing stripping contract as
    the playback loop. Unlike a ring-buffer capture, no leading or trailing
    bytes are silently discarded once playback begins: an unsupported or
    malformed source fails closed instead of producing a potentially replaying
    handoff.
    """

    if not data:
        raise Mp3FrameIndexError("empty audio")
    with io.BytesIO(data) as source:
        _skip_id3_and_xing_header(source)
        playable_start = source.tell()
    playable_end = _trailing_metadata_start(data, lower_bound=playable_start)
    return _index_complete_mpeg1_l3_run(data, start=playable_start, end=playable_end, strict_end=True)


def _artifact_stem(source_path: Path, stem: str | None) -> str:
    """Return a safe, collision-resistant local basename for handoff artifacts."""

    if stem is None:
        return f"{source_path.stem}_handoff_{uuid4().hex}"
    if not stem or Path(stem).name != stem:
        raise Mp3FrameIndexError("invalid handoff artifact stem")
    return stem


def _write_bytes_atomically(destination: Path, payload: bytes) -> None:
    """Publish one generated artifact without exposing a partial MP3 file."""

    fd, temporary_name = tempfile.mkstemp(
        prefix=f"{_ATOMIC_PART_PREFIX}{destination.name}.",
        suffix=".part",
        dir=destination.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as artifact:
            artifact.write(payload)
            artifact.flush()
            os.fsync(artifact.fileno())
        os.replace(temporary_path, destination)
    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def split_mpeg1_l3_handoff(
    source_path: Path,
    output_dir: Path,
    tail_seconds: float,
    *,
    stem: str | None = None,
) -> Mp3HandoffSplit:
    """Write a playable head and a decoder-safe tail input for a handoff.

    The tail begins at the first complete frame at or after the requested tail
    window. It can therefore be up to one MPEG frame shorter than requested,
    never longer, and the preceding head remains a valid standalone playable
    stream. The private tail input prepends at most ten frames for Layer III
    bit-reservoir and synthesis context; its sample metadata makes that preroll
    removable before mixing. Invalid metadata, partial frames, unsupported MPEG
    variants, too-short sources, and write failures all raise
    :class:`Mp3FrameIndexError` without returning an artifact pair.
    """

    try:
        requested_tail_seconds = float(tail_seconds)
    except (TypeError, ValueError) as exc:
        raise Mp3FrameIndexError("invalid handoff tail duration") from exc
    if not math.isfinite(requested_tail_seconds) or requested_tail_seconds <= 0:
        raise Mp3FrameIndexError("invalid handoff tail duration")

    source_path = Path(source_path)
    output_dir = Path(output_dir)
    try:
        source_data = source_path.read_bytes()
    except OSError as exc:
        raise Mp3FrameIndexError("unable to read handoff source") from exc

    index = build_playable_mpeg1_layer3_frame_index(source_data)
    tail_first = next(
        (frame.ordinal for frame in index.frames if frame.start_sec >= index.duration_sec - requested_tail_seconds),
        None,
    )
    if tail_first is None or tail_first <= 0:
        raise Mp3FrameIndexError("source is too short for the requested handoff tail")

    playable_start_byte = index.data_start
    head_end_byte = index.frames[tail_first].byte_start
    playable_end_byte = index.data_end
    tail_decode_first = max(0, tail_first - _MAX_DECODER_PREROLL_FRAMES)
    tail_decode_start_byte = index.frames[tail_decode_first].byte_start
    tail_preroll_frame_count = tail_first - tail_decode_first
    head_payload = source_data[playable_start_byte:head_end_byte]
    tail_payload = source_data[tail_decode_start_byte:playable_end_byte]
    if not head_payload or not tail_payload:
        raise Mp3FrameIndexError("handoff must retain complete head and tail frames")

    artifact_stem = _artifact_stem(source_path, stem)
    head_path = output_dir / f"{artifact_stem}_head.mp3"
    tail_path = output_dir / f"{artifact_stem}_tail.mp3"
    if head_path == tail_path or head_path.exists() or tail_path.exists():
        raise Mp3FrameIndexError("handoff artifact path already exists")
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_bytes_atomically(head_path, head_payload)
        try:
            _write_bytes_atomically(tail_path, tail_payload)
        except BaseException:
            head_path.unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise Mp3FrameIndexError("unable to write handoff artifacts") from exc

    return Mp3HandoffSplit(
        head_path=head_path,
        tail_path=tail_path,
        playable_start_byte=playable_start_byte,
        head_end_byte=head_end_byte,
        playable_end_byte=playable_end_byte,
        head_duration_sec=index.duration_for(0, tail_first),
        tail_duration_sec=index.duration_for(tail_first, len(index.frames)),
        source_duration_sec=index.duration_sec,
        frame_count=len(index.frames),
        head_frame_count=tail_first,
        tail_frame_count=len(index.frames) - tail_first,
        tail_decode_path=tail_path,
        tail_decode_start_byte=tail_decode_start_byte,
        tail_preroll_frame_count=tail_preroll_frame_count,
        tail_preroll_samples=tail_preroll_frame_count * _MPEG1_L3_SAMPLES_PER_FRAME,
        tail_sample_count=(len(index.frames) - tail_first) * _MPEG1_L3_SAMPLES_PER_FRAME,
    )
