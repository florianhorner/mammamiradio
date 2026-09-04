"""Helpers for packaged demo assets that must survive cleanup paths."""

from __future__ import annotations

from pathlib import Path

from mammamiradio.core.path_safety import safe_path_within

DEMO_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "demo"


def is_packaged_asset(path: Path, assets_dir: Path | None = None) -> bool:
    """True if path lives under the read-only packaged demo assets tree.

    Containment is delegated so this answer cannot diverge from the cache,
    handoff, and manifest paths asking the same question.

    The previous inline version caught ``OSError`` but not ``RuntimeError``,
    which is what Python 3.12 and earlier raised on a symlink cycle. Callers in
    ``producer`` use this to decide whether an ephemeral segment may be
    deleted after air, so the raise escaped into the audio path rather than
    returning a verdict. On 3.14 the same cycle raised nothing and reported as
    a packaged asset. Both are now a plain ``False``.

    ``AttributeError`` / ``TypeError`` stay caught here because callers may
    hand in something that is not a path at all. The shared helper refuses a
    non-``Path`` resolution result, which is what keeps a test double from
    being reported as a protected packaged asset.
    """
    try:
        return safe_path_within(path, assets_dir or DEMO_ASSETS_DIR) is not None
    except (AttributeError, TypeError):
        return False
