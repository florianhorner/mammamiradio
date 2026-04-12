"""Clip extraction from the live stream ring buffer.

When a listener hears something wild, they press a button and the last ~30
seconds of audio is trimmed into a shareable MP3 clip.  Since the ring buffer
already contains raw MP3 frames, no re-encoding is needed.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

CLIP_TTL_SECONDS = 24 * 60 * 60  # 24 hours


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
    data = b"".join(chunks)
    # Trim to exactly the requested duration
    if len(data) > bytes_needed:
        data = data[-bytes_needed:]
    return data


def save_clip(clip_data: bytes, clips_dir: Path) -> str:
    """Write clip bytes to disk and return the clip_id."""
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip_id = uuid.uuid4().hex[:12]
    clip_path = clips_dir / f"{clip_id}.mp3"
    clip_path.write_bytes(clip_data)
    logger.info("Saved clip %s (%d bytes)", clip_id, len(clip_data))
    return clip_id


def cleanup_old_clips(clips_dir: Path, max_age_hours: int = 24) -> int:
    """Delete clips older than *max_age_hours*. Returns count removed."""
    if not clips_dir.is_dir():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for f in clips_dir.glob("*.mp3"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed
