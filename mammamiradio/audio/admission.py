"""Shared ffmpeg/ffprobe admission gates."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from threading import BoundedSemaphore

logger = logging.getLogger(__name__)

# Limit concurrent foreground + background ffmpeg/ffprobe runs across sync +
# executor call sites. Background prefetch additionally takes _BACKGROUND_SEM so
# it can use at most one of these two slots, leaving one slot for next-to-air
# work. Rescue audio bypasses both gates and is normally capped at one
# concurrent render, so the steady-state worst case across gated call sites is
# 2 ordinary/background ffmpeg jobs + 1 rescue job. That rescue cap is
# best-effort, not hard: if a rescue render wedges past the 2s acquire
# timeout below, every subsequent rescue call also times out and proceeds
# ungated for as long as the wedge lasts (bounded only by the 180s ffmpeg
# timeout), so concurrent rescue jobs during a wedge are not capped at 1.
# Known exception: yt-dlp's FFmpegExtractAudio postprocessor spawns its own
# ffmpeg outside this gate (wrapping the download would hold a slot across a
# network fetch), so a chart download can add one more transient process on
# top of the gated ceiling.
_NORM_SEM = BoundedSemaphore(2)
_BACKGROUND_SEM = BoundedSemaphore(1)
_RESCUE_SEM = BoundedSemaphore(1)

# Emergency audio keeps its 1-wide cap in the normal case, but a wedged rescue
# render (bounded only by the 180s ffmpeg timeout) must never delay the next
# dead-air fill (#2 INSTANT AUDIO): after this wait the new rescue proceeds
# ungated instead of queueing behind the stuck one.
_RESCUE_ACQUIRE_TIMEOUT_SEC = 2.0


@contextmanager
def ffmpeg_slot(*, rescue: bool = False, background: bool = False) -> Iterator[None]:
    """Reserve shared ffmpeg/ffprobe capacity for one leaf tool invocation.

    ``rescue`` takes precedence when both flags are passed (emergency audio must
    never inherit background throttling); the combination is logged because no
    legitimate call site should produce it.
    """
    if rescue:
        if background:
            logger.warning("ffmpeg_slot got rescue=True and background=True — treating as rescue")
        acquired = _RESCUE_SEM.acquire(timeout=_RESCUE_ACQUIRE_TIMEOUT_SEC)
        if not acquired:
            logger.warning(
                "Rescue ffmpeg slot still held after %.1fs — proceeding ungated so emergency audio never waits",
                _RESCUE_ACQUIRE_TIMEOUT_SEC,
            )
        try:
            yield
        finally:
            if acquired:
                _RESCUE_SEM.release()
        return

    if background:
        with _BACKGROUND_SEM, _NORM_SEM:
            yield
        return

    with _NORM_SEM:
        yield
