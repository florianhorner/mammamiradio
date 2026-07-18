"""Packaged first-listen mini-show selection.

The mini-show is a client-local prelude, not a station ``Segment``.  That keeps
the shared live queue, now-playing state, and existing listeners untouched while
giving a fresh install immediate station identity and stock-host personality.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from mammamiradio.core.first_listen import (
    FirstListenInstallOriginStatus,
    FirstListenReceiptLoadStatus,
)
from mammamiradio.core.packaged_assets import DEMO_ASSETS_DIR
from mammamiradio.core.spoken_assets import is_approved_spoken_asset

FIRST_LISTEN_SHOW_RELATIVE_PATH = Path("first_listen/first_listen_show.mp3")
FIRST_LISTEN_SHOW_CHUNK_BYTES = 16 * 1024


def first_listen_show_required(app_state: object) -> bool:
    """Return whether this client should hear the first-listen prelude.

    A cold-install fact is captured synchronously before SQLite initialization.
    Later boots rely on the durable fresh-install projection, but decline the
    prelude while the receipt load is still pending so a completed operator is
    never surprised by onboarding audio during a narrow startup race.
    """

    station_state = getattr(app_state, "station_state", None)
    if bool(getattr(station_state, "session_stopped", False)):
        return False

    cold_install = bool(getattr(app_state, "first_listen_cold_install", False))
    origin = getattr(app_state, "first_listen_install_origin", None)
    durable_fresh = getattr(origin, "status", None) is FirstListenInstallOriginStatus.FRESH
    if not cold_install and not durable_fresh:
        return False

    receipt = getattr(app_state, "first_listen_receipt", None)
    if bool(getattr(receipt, "audio_complete", False)):
        return False

    # A synchronously captured cold-install fact may select the packaged show
    # before receipt I/O finishes.  Later boots fail closed unless startup has
    # positively distinguished a missing receipt from an unreadable one.
    if cold_install:
        return True

    load_status = getattr(
        app_state,
        "first_listen_receipt_load_status",
        FirstListenReceiptLoadStatus.PENDING,
    )
    if load_status is FirstListenReceiptLoadStatus.MISSING:
        return True
    return load_status is FirstListenReceiptLoadStatus.PRESENT and receipt is not None


def approved_first_listen_show_path(*, assets_root: Path = DEMO_ASSETS_DIR) -> Path | None:
    """Return the reviewed, hash-bound mini-show, or fail closed."""

    path = Path(assets_root) / FIRST_LISTEN_SHOW_RELATIVE_PATH
    if is_approved_spoken_asset(path, assets_root=Path(assets_root)):
        return path
    return None


def first_listen_show_path(app_state: object, *, assets_root: Path = DEMO_ASSETS_DIR) -> Path | None:
    """Select the mini-show only for a fresh install awaiting audible proof."""

    if not first_listen_show_required(app_state):
        return None
    return approved_first_listen_show_path(assets_root=assets_root)


def iter_first_listen_show_chunks(
    path: Path,
    *,
    chunk_bytes: int = FIRST_LISTEN_SHOW_CHUNK_BYTES,
) -> Iterator[bytes]:
    """Yield bounded MP3 chunks from the immutable packaged prelude."""

    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            yield chunk
