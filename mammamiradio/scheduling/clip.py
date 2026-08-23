"""Clip extraction from the live stream ring buffer.

When a listener hears something wild, they press a button and the last ~30
seconds of audio is trimmed into a shareable MP3 clip.  Since the ring buffer
already contains raw MP3 frames, no re-encoding is needed.

Everything else the station records is deleted on a timer: a clip after
``CLIP_TTL_SECONDS``, the provenance-ledger row holding its script after
``MAMMAMIRADIO_LEDGER_RETENTION_DAYS``.  A segment worth keeping is therefore
destroyed twice over by default.  A keepsake is the exception: a voice-only
export that no retention window collects.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

CLIP_TTL_SECONDS = 24 * 60 * 60  # 24 hours

# Keepsakes live beside `clips/` rather than inside it, which is what makes them
# durable: `cleanup_old_clips` and the CLIP_MAX_SAVED cap both operate on the
# clips directory alone, and the cache evictor globs `cache_dir/*.mp3` without
# recursing, so nothing that prunes the cache can see this directory at all.
KEEPSAKES_DIRNAME = "keepsakes"

# Segment types whose audio is wholly the station's own work: generated script,
# generated speech, synthesized bed (`generate_music_bed`), own foley and SFX.
# A keepsake is publishable by construction, so it may only ever be cut from one
# of these.  Music segments carry third-party recordings, and a keepsake is
# durable and shareable, so cutting one from a song would publish somebody
# else's master.
KEEPSAKE_SEGMENT_TYPES = frozenset({"banter", "ad", "news_flash", "station_id", "sweeper", "time_check"})


def extract_clip(
    ring_buffer: deque[bytes],
    *,
    duration_seconds: int = 30,
    bitrate_kbps: int = 192,
) -> bytes | None:
    """Extract the last *duration_seconds* of audio from the ring buffer.

    Returns raw MP3 bytes, or ``None`` if the buffer is empty.
    """
    if not ring_buffer:
        return None

    bytes_needed = (bitrate_kbps * 1000 // 8) * duration_seconds

    # Walk backwards through the deque to collect the tail
    chunks: list[bytes] = []
    total = 0
    for chunk in reversed(ring_buffer):
        chunks.append(chunk)
        total += len(chunk)
        if total >= bytes_needed:
            break

    if not chunks:
        return None

    chunks.reverse()
    # Return whole chunks to avoid cutting into MP3 frames.
    # The result may be slightly longer than requested but stays frame-aligned.
    return b"".join(chunks)


def extract_segment_audio(ring_buffer: deque[bytes], chunk_count: int) -> bytes | None:
    """Return exactly the last *chunk_count* chunks, and nothing older.

    ``extract_clip`` asks the buffer for a duration and lets it answer with
    whatever whole chunks cover it, which is right for "the last 30 seconds" and
    wrong for "this segment".  A duration rounded up past what has actually
    aired reaches backwards across the segment boundary, and the ring holds
    every voice segment back to back because music never enters it, so the bytes
    it hands back belong to an earlier segment while the caller labels them with
    the current one.

    Counting chunks instead removes the arithmetic.  Each chunk is read from
    exactly one segment file, so a count kept by the playback loop is an exact
    boundary rather than an estimate, and the segment's first chunk starts at
    its first MPEG frame (the playback loop skips ID3/Xing before sending), so
    a cut that begins there is frame-aligned by construction.

    *chunk_count* is clamped to what the ring still holds: on a segment long
    enough to have aged out of the buffer the remainder is still wholly that
    segment's own audio.
    """
    if not ring_buffer or chunk_count <= 0:
        return None
    take = min(int(chunk_count), len(ring_buffer))
    chunks = list(ring_buffer)[-take:]
    return b"".join(chunks) or None


def save_clip(clip_data: bytes, clips_dir: Path) -> str:
    """Write clip bytes to disk and return the clip_id."""
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip_id = uuid.uuid4().hex[:12]
    clip_path = clips_dir / f"{clip_id}.mp3"
    clip_path.write_bytes(clip_data)
    logger.info("Saved clip %s (%d bytes)", clip_id, len(clip_data))
    return clip_id


def is_keepsake_eligible(segment_type: object, metadata: object = None) -> bool:
    """Whether this segment may be kept durably. Both gates must pass.

    The type gate is voice-only by construction (see
    ``KEEPSAKE_SEGMENT_TYPES``). Anything unrecognised, including a future
    segment type, ``None`` and a non-string, is refused, so a new segment type
    has to be reviewed and added deliberately rather than inheriting publish
    rights by default.

    The music-tail gate exists because type alone stopped being proof of
    provenance when the music-to-speech handoff landed: `commit_music_handoff`
    crossfades the outgoing song's real master under the opening seconds of the
    next BANTER/AD/NEWS_FLASH segment and marks it ``has_music_tail``. That
    segment is still voice by type and now contains someone else's recording.
    A keepsake never expires and is served unauthenticated, so a tailed segment
    is refused outright rather than trimmed; guessing where the master ends is
    a near-miss this feature cannot afford.
    """
    if not (isinstance(segment_type, str) and segment_type in KEEPSAKE_SEGMENT_TYPES):
        return False
    meta = metadata if isinstance(metadata, dict) else {}
    return not meta.get("has_music_tail")


def save_keepsake(
    clip_data: bytes,
    keepsakes_dir: Path,
    *,
    sidecar: dict | None = None,
) -> str:
    """Write keepsake bytes plus its sidecar durably and return the keepsake id.

    No TTL and no eviction: nothing that prunes the cache can see this
    directory. The count ceiling and the free-space check live at the route, so
    a refusal can name the situation and offer a way out instead of failing
    here with an errno.

    The sidecar write is best-effort: losing the metadata costs a title on the
    share page, so a sidecar error never discards audio that is already safely
    on disk.
    """
    keepsakes_dir.mkdir(parents=True, exist_ok=True)
    keepsake_id = uuid.uuid4().hex[:12]
    keepsake_path = keepsakes_dir / f"{keepsake_id}.mp3"
    # Atomic publish. A plain write leaves a truncated file behind if the process
    # is killed mid-write (an add-on update, which this repo designs around) or
    # the disk fills, and in this directory alone there is no TTL, no cap and no
    # evictor to ever collect it: a permanent file that looks like a keepsake and
    # plays as garbage. Same mkstemp-then-replace shape as `restart_handoff`.
    tmp_fd, tmp_name = tempfile.mkstemp(dir=keepsakes_dir, prefix=".keepsake-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "wb") as handle:
            handle.write(clip_data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, keepsake_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    if sidecar is not None:
        # Catch the serializer too, not just the disk. `json.dumps` raises
        # TypeError on a value nobody expected to be there, and with only OSError
        # caught here that escaped the route's own `except OSError` as an
        # uncaught 500 — with the audio already safely on disk and its id thrown
        # away. That is the exact outcome the docstring promises cannot happen.
        try:
            (keepsakes_dir / f"{keepsake_id}.json").write_text(json.dumps(sidecar))
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("keepsake sidecar write failed for %s: %s", keepsake_id, exc)
    logger.info("Saved keepsake %s (%d bytes)", keepsake_id, len(clip_data))
    return keepsake_id


def prune_stale_keepsake_tmp_files(keepsakes_dir: Path, max_age_hours: float = 6) -> int:
    """Prune ``.keepsake-*.tmp`` scratch left by a kill mid-write. Returns the count.

    ``save_keepsake`` publishes atomically, so a hard kill between ``mkstemp``
    and ``os.replace`` — an add-on update, which this repo designs around —
    leaves scratch behind instead of a half-published keepsake. Nothing else
    would ever collect it: every cache pruner globs one level, the count ceiling
    and both API routes glob ``*.mp3``, and this directory has no evictor by
    design. Same shape as ``prune_stale_handoff_tmp_files``.

    Best-effort and age-gated, so a write in flight is never the thing pruned.
    """
    if not keepsakes_dir.is_dir():
        return 0
    if not (max_age_hours > 0):  # also rejects NaN
        logger.warning("Ignoring keepsake scratch cleanup: max_age_hours must be positive, got %r", max_age_hours)
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for f in keepsakes_dir.glob(".keepsake-*.tmp"):
        try:
            if f.is_symlink() or not f.is_file() or f.stat().st_mtime >= cutoff:
                continue
            f.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("keepsake scratch cleanup failed for %s: %s", f.name, exc)
    return removed


def cleanup_old_clips(clips_dir: Path, max_age_hours: int = 24) -> int:
    """Delete clips older than *max_age_hours*. Returns count of MP3s removed.

    Also prunes the matching ``{clip_id}.json`` sidecar so metadata does not
    accumulate after the audio is gone.

    Refuses to run against the keepsakes directory. Keepsakes are durable by
    living somewhere this function is never pointed at, which is a property of
    the call sites rather than of the data; this guard makes one wrong argument
    a no-op instead of the silent deletion of the only copy of a moment.
    """
    if not clips_dir.is_dir():
        return 0
    if clips_dir.name == KEEPSAKES_DIRNAME:
        logger.error("refusing to expire keepsakes: %s", clips_dir)
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for f in clips_dir.glob("*.mp3"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                f.with_suffix(".json").unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed
