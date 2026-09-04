"""Best-effort filesystem containment helpers for cache-owned paths."""

from __future__ import annotations

import errno
from pathlib import Path


def safe_path_within(path: Path, root: Path, *, reject_symlinks: bool = False) -> Path | None:
    """Return a resolved *path* when it remains inside *root*, else ``None``.

    Callers use this around cleanup and admission paths, where malformed cache
    state must degrade to a skipped candidate rather than escape its owning
    directory or interrupt startup.

    Three ways out of *root*, and the third needs its own probe::

        path
          |
          +-- resolves outside root      --> is_relative_to False   REJECT
          +-- resolution itself fails    --> OSError/RuntimeError    REJECT
          +-- symlink cycle              --> stat() ELOOP            REJECT
          +-- missing / unreadable       --> not a containment
                                             failure; left to the
                                             caller's own checks

    The cycle probe is load-bearing, not defensive padding. Through Python
    3.13 ``Path.resolve(strict=False)`` raised ``RuntimeError`` on a symlink
    cycle and the ``except`` below caught it for free. Python 3.14 returns the
    unresolved path and raises nothing, so a cycle satisfies
    ``is_relative_to`` and this function hands back a path it should have
    refused. ``stat()`` reports ``ELOOP`` on every supported interpreter.

    ``reject_symlinks=True`` already covers the cycle case by refusing any
    symlink up front, so only callers that deliberately allow symlinks depend
    on the probe.
    """
    try:
        if reject_symlinks and path.is_symlink():
            return None
        resolved_root = root.resolve(strict=False)
        resolved_path = path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        # ValueError is an embedded null byte. Like the others it is malformed
        # input, not containment, and this function promises to skip a bad
        # candidate rather than interrupt the caller.
        return None
    # This function promises a Path or None. A caller may hand in a test double
    # or other path-like whose resolve() answers with something else, and an
    # unchecked mock result satisfies is_relative_to() truthily -- reporting
    # containment for an object that was never a path.
    if not isinstance(resolved_path, Path) or not isinstance(resolved_root, Path):
        return None
    if not resolved_path.is_relative_to(resolved_root):
        return None
    try:
        path.stat()
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None
    except ValueError:
        return None
    return resolved_path
