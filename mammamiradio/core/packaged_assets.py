"""Helpers for packaged demo assets that must survive cleanup paths."""

from __future__ import annotations

from pathlib import Path

DEMO_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "demo"


def is_packaged_asset(path: Path, assets_dir: Path | None = None) -> bool:
    """True if path lives under the read-only packaged demo assets tree."""
    try:
        resolved = path.resolve()
    except (AttributeError, OSError, TypeError):
        return False
    if not isinstance(resolved, Path):
        return False
    try:
        return resolved.is_relative_to((assets_dir or DEMO_ASSETS_DIR).resolve())
    except OSError:
        return False
