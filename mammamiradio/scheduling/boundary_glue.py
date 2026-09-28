"""Packaged boundary cart between two aired programme segments.

The seam has two sides. The previous side contributes a cart only when that
segment actually aired and was not itself station imaging or a rescue fill.
The next side refuses a cart for a rescue fill, a failed render, or a reserved
music tail. Interrupts stay immediate; live ads already own their bumpers.
The four files are packaged
assets only: a missing or invalid file is a clean cut, never an ffmpeg synth.

Starter music and packaged ads stay byte-for-byte untouched. The cart is not
a queue segment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from mammamiradio.audio.imaging import ImagingLibrary
from mammamiradio.core.models import Segment, SegmentType
from mammamiradio.web.mp3_frames import (
    Mp3FrameIndexError,
    build_playable_mpeg1_layer3_frame_index,
    mpeg1_l3_bitrate_kbps,
)

logger = logging.getLogger(__name__)

BOUNDARY_MAX_DURATION_SEC = 1.5
BOUNDARY_MAX_BYTES = 1024 * 1024
MUSIC_TO_SPEECH = "stingers/music_to_speech.mp3"
SPEECH_TO_MUSIC = "stingers/speech_to_music.mp3"
AD_IN = "bumpers/ad_in.mp3"
AD_OUT = "bumpers/ad_out.mp3"

SKIP_PREV_NONE = "prev_none"
SKIP_PREV_IMAGING = "prev_imaging"
SKIP_PREV_RESCUE = "prev_rescue"
SKIP_INTERRUPT = "interrupt"
SKIP_LIVE_AD = "live_ad"
SKIP_NEXT_RESCUE = "next_rescue"
SKIP_NEXT_ERROR = "next_error"
SKIP_NEXT_MUSIC_TAIL = "next_music_tail"
SKIP_SWITCH_OFF = "switch_off"
SKIP_ASSET_MISSING = "asset_missing"
SKIP_GENERATION = "generation_changed"

_IMAGING_KINDS = frozenset({SegmentType.SWEEPER, SegmentType.STATION_ID, SegmentType.TIME_CHECK})
_GENERIC_SPEECH = (
    SegmentType.BANTER,
    SegmentType.NEWS_FLASH,
    SegmentType.STATION_ID,
    SegmentType.SWEEPER,
    SegmentType.TIME_CHECK,
)
_INCOMING_SPEECH = frozenset({*_GENERIC_SPEECH, SegmentType.AD})

_ASSET_TABLE: dict[tuple[SegmentType, SegmentType], str] = {
    (SegmentType.MUSIC, SegmentType.AD): AD_IN,
    (SegmentType.AD, SegmentType.MUSIC): AD_OUT,
}
for _speech in _GENERIC_SPEECH:
    _ASSET_TABLE[(SegmentType.MUSIC, _speech)] = MUSIC_TO_SPEECH
    _ASSET_TABLE[(_speech, SegmentType.MUSIC)] = SPEECH_TO_MUSIC

# File identity and format bind cached validation. Read failures are retried;
# the small bound also covers repeated operator replacements or pack changes.
_VALIDATION_CACHE: dict[tuple[str, int, int, int, int, int], bytes | None] = {}


@dataclass(frozen=True)
class AiredBoundary:
    """The last programme segment that actually sent audio, for one listener room."""

    kind: SegmentType
    rescue: bool
    generation: int
    packaged: bool = False
    interrupt: bool = False


@dataclass(frozen=True)
class SeamChoice:
    """A packaged relative path, a named skip, or neither when the seam is not a cart."""

    relative: str | None = None
    skip_reason: str | None = None


def _metadata(segment: Segment) -> dict:
    metadata = segment.metadata
    return metadata if isinstance(metadata, dict) else {}


def _next_skip_reason(metadata: dict) -> str | None:
    if metadata.get("rescue"):
        return SKIP_NEXT_RESCUE
    if "error" in metadata:
        return SKIP_NEXT_ERROR
    if metadata.get("has_music_tail"):
        return SKIP_NEXT_MUSIC_TAIL
    return None


def seam_choice(previous: AiredBoundary | None, next_segment: Segment) -> SeamChoice:
    """Decide whether this seam wants a packaged cart, and why it would not.

    ``relative`` and ``skip_reason`` are exclusive. A seam that is not a cart
    leaves both empty.
    """
    next_reason = _next_skip_reason(_metadata(next_segment))
    if previous is None:
        if next_segment.type not in _INCOMING_SPEECH:
            return SeamChoice()
        return SeamChoice(skip_reason=next_reason or SKIP_PREV_NONE)

    relative = _ASSET_TABLE.get((previous.kind, next_segment.type))
    if relative is None:
        return SeamChoice()
    if previous.interrupt or _metadata(next_segment).get("interrupt"):
        return SeamChoice(skip_reason=SKIP_INTERRUPT)
    if (previous.kind is SegmentType.AD and not previous.packaged) or (
        next_segment.type is SegmentType.AD and not _metadata(next_segment).get("packaged")
    ):
        return SeamChoice(skip_reason=SKIP_LIVE_AD)
    if next_reason is not None:
        return SeamChoice(skip_reason=next_reason)
    if previous.rescue:
        return SeamChoice(skip_reason=SKIP_PREV_RESCUE)
    if previous.kind in _IMAGING_KINDS:
        return SeamChoice(skip_reason=SKIP_PREV_IMAGING)
    return SeamChoice(relative=relative)


def _packaged_file(relative: str, *, assets_dir: Path) -> Path | None:
    """Resolve one already-chosen packaged path. Never synthesizes audio."""
    return ImagingLibrary.packaged_boundary_asset(relative, assets_dir=assets_dir)


def glue_for(previous: AiredBoundary | None, next_segment: Segment, *, assets_dir: Path) -> Path | None:
    """Return a packaged boundary file, or None for a clean cut."""
    choice = seam_choice(previous, next_segment)
    if choice.relative is None:
        return None
    return _packaged_file(choice.relative, assets_dir=assets_dir)


def _main_data_begin(frame: bytes) -> int | None:
    """MPEG-1 side-info main_data_begin: 9 bits at the first two side-info bytes."""
    if len(frame) < 4:
        return None
    offset = 4 if frame[1] & 1 else 6
    if len(frame) < offset + 2:
        return None
    return ((frame[offset] << 1) | (frame[offset + 1] >> 7)) & 0x1FF


@lru_cache(maxsize=32)
def warn_unusable(path: Path, reason: str, signature: tuple[int, int] | None = None) -> None:
    logger.warning("Boundary imaging asset unusable (%s): %s", reason, path)


def validated_playable_bytes(
    path: Path,
    *,
    sample_rate: int,
    bitrate: int,
    channels: int = 2,
) -> bytes | None:
    """Return the indexed playable range, or None when the asset must not air.

    Replaced files are validated again without restarting. I/O failures never
    enter the validation cache, so a transient unreadable file can recover.
    """
    try:
        path = path.resolve()
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        cache_key = (str(path), *signature, sample_rate, bitrate, channels)
    except OSError:
        warn_unusable(path, "unreadable")
        return None
    if cache_key in _VALIDATION_CACHE:
        return _VALIDATION_CACHE[cache_key]

    try:
        playable, reason = _playable_or_reason(path, sample_rate, bitrate, channels)
    except OSError:
        warn_unusable(path, "unreadable", signature)
        return None
    if len(_VALIDATION_CACHE) >= 16:
        _VALIDATION_CACHE.pop(next(iter(_VALIDATION_CACHE)))
    _VALIDATION_CACHE[cache_key] = playable
    if playable is None:
        warn_unusable(path, reason, signature)
    return playable


def _playable_or_reason(path: Path, sample_rate: int, bitrate: int, channels: int) -> tuple[bytes | None, str]:
    with path.open("rb") as source:
        data = source.read(BOUNDARY_MAX_BYTES + 1)
    if len(data) > BOUNDARY_MAX_BYTES:
        return None, "larger than 1 MiB"
    try:
        index = build_playable_mpeg1_layer3_frame_index(data)
        frame = data[index.frames[0].byte_start : index.frames[0].byte_end]
    except (Mp3FrameIndexError, ValueError):
        return None, "unreadable"
    if index.sample_rate != sample_rate or any(
        mpeg1_l3_bitrate_kbps(data[item.byte_start : item.byte_start + 4]) != bitrate for item in index.frames
    ):
        return None, "sample rate or bitrate does not match the stream"
    channel_mode = frame[3] >> 6
    if (1 if channel_mode == 3 else 2) != channels or any(
        data[item.byte_start + 3] >> 6 != channel_mode for item in index.frames
    ):
        return None, "channel mode does not match the stream"
    if index.duration_sec > BOUNDARY_MAX_DURATION_SEC:
        return None, f"longer than {BOUNDARY_MAX_DURATION_SEC:.1f}s"
    if _main_data_begin(frame) != 0:
        return None, "first frame depends on the previous file"
    playable = data[index.data_start : index.data_end]
    if not playable:
        return None, "no playable frames"
    return playable, ""
