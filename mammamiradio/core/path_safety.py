"""Best-effort filesystem containment helpers for cache-owned paths."""

from __future__ import annotations

from pathlib import Path


def safe_path_within(path: Path, root: Path, *, reject_symlinks: bool = False) -> Path | None:
    """Return a resolved *path* when it remains inside *root*, else ``None``.

    Callers use this around cleanup and admission paths, where malformed cache
    state must degrade to a skipped candidate rather than escape its owning
    directory or interrupt startup.
    """
    try:
        if reject_symlinks and path.is_symlink():
            return None
        resolved_root = root.resolve(strict=False)
        resolved_path = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    return resolved_path if resolved_path.is_relative_to(resolved_root) else None
