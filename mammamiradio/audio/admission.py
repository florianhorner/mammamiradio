"""Shared ffmpeg/ffprobe admission gates."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from threading import BoundedSemaphore

# Limit concurrent foreground + background ffmpeg/ffprobe runs across sync +
# executor call sites. Background prefetch additionally takes _BACKGROUND_SEM so
# it can use at most one of these two slots, leaving one slot for next-to-air
# work. Rescue audio bypasses both gates but is capped at one concurrent render,
# so the worst case is 2 ordinary/background ffmpeg jobs + 1 rescue job.
_NORM_SEM = BoundedSemaphore(2)
_BACKGROUND_SEM = BoundedSemaphore(1)
_RESCUE_SEM = BoundedSemaphore(1)

_DEFAULT_NORM_SEM = _NORM_SEM
_DEFAULT_BACKGROUND_SEM = _BACKGROUND_SEM
_DEFAULT_RESCUE_SEM = _RESCUE_SEM


def _maybe_normalizer_override(name: str, default):
    """Honor legacy tests that patch normalizer admission globals."""
    normalizer = sys.modules.get("mammamiradio.audio.normalizer")
    value = getattr(normalizer, name, default) if normalizer is not None else default
    if value is not default:
        return value
    return globals()[name]


@contextmanager
def ffmpeg_slot(*, rescue: bool = False, background: bool = False) -> Iterator[None]:
    """Reserve shared ffmpeg/ffprobe capacity for one leaf tool invocation."""
    if rescue:
        with _maybe_normalizer_override("_RESCUE_SEM", _DEFAULT_RESCUE_SEM):
            yield
        return

    norm_sem = _maybe_normalizer_override("_NORM_SEM", _DEFAULT_NORM_SEM)
    if background:
        background_sem = _maybe_normalizer_override("_BACKGROUND_SEM", _DEFAULT_BACKGROUND_SEM)
        with background_sem:
            with norm_sem:
                yield
        return

    with norm_sem:
        yield
