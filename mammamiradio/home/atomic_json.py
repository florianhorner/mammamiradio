"""Owner-only atomic JSON replace for household cache files.

Four writers under ``home/`` used to leave household-derived JSON readable by
other local users: either they chmod 0600 only after :meth:`Path.write_text`
(umask window), or they never chmod and shared a fixed ``<name>.json.tmp``.

This helper is the catalog-shaped contract without catalog's mkdir or
fail-soft bool:

* serialize before any file exists (``TypeError`` / ``ValueError`` never
  strand scratch)
* :func:`tempfile.mkstemp` plus :func:`os.fchmod` ``0o600`` before content
* :func:`os.replace` (POSIX keeps the 0600 inode; no post-replace chmod)
* unlink the scratch only if replace did not succeed
* prune age-gated crash scratch at startup, before any of the four writers run
* raise (``OSError`` / ``TypeError`` / ``ValueError``); callers keep their
  except clauses
* do not mkdir — moments and ledger tests rely on a missing parent failing
  softly

``catalog.py::_atomic_write_json`` swallows and returns bool, and mkdir's.
``migration.py::_atomic_write_json`` fsyncs and re-raises. Do not fold those
here. Playlist modules must not import this module; a follow-up can lift a
copy into ``core/`` if those writers need it.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mammamiradio.core.path_safety import safe_path_within

logger = logging.getLogger(__name__)

_MAX_SCRATCH_PRUNE_PER_DESTINATION = 500
_MAX_SCRATCH_GLOB_CANDIDATES = 5000


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def prune_stale_atomic_json_tmp_files(
    cache_dir: Path,
    destinations: Iterable[Path],
    *,
    max_age_hours: float = 6,
) -> int:
    """Best-effort prune of crash scratch for the household JSON writers.

    Startup calls this before any writer can be in flight. The positive age
    gate is still load-bearing: it protects scratch from another process using
    the same cache directory. Only each destination's unique dotted scratch
    and the old fixed ``<name>.json.tmp`` form are eligible.
    """
    if not math.isfinite(max_age_hours) or max_age_hours <= 0:
        logger.warning(
            "Ignoring household JSON scratch cleanup: max_age_hours must be positive, got %r",
            max_age_hours,
        )
        return 0

    cache_root = Path(cache_dir)
    if cache_root.is_symlink():
        logger.warning("Skipping household JSON scratch cleanup: cache_dir is a symlink: %s", cache_root)
        return 0
    if not cache_root.is_dir():
        return 0

    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for destination in destinations:
        destination = Path(destination)
        parent = destination.parent
        resolved_parent = safe_path_within(parent, cache_root, reject_symlinks=True)
        if resolved_parent is None:
            logger.warning("Skipping household JSON scratch cleanup outside cache_dir: %s", parent)
            continue
        if not parent.is_dir():
            continue

        candidates: list[Path] = []
        overflowed = False
        try:
            for candidate in parent.glob(f".{destination.name}.*.tmp"):
                if len(candidates) >= _MAX_SCRATCH_GLOB_CANDIDATES:
                    overflowed = True
                    break
                candidates.append(candidate)
        except OSError as exc:
            logger.warning("Failed to scan household JSON scratch in %s: %s", parent, exc)
            continue

        legacy_fixed = destination.with_suffix(".json.tmp")
        if legacy_fixed.exists() or legacy_fixed.is_symlink():
            candidates.append(legacy_fixed)
        if overflowed:
            logger.warning(
                "Household JSON scratch cleanup for %s exceeded %d raw candidates; "
                "the remainder will be retried on a future boot",
                destination.name,
                _MAX_SCRATCH_GLOB_CANDIDATES,
            )
        if len(candidates) > _MAX_SCRATCH_PRUNE_PER_DESTINATION:
            logger.warning(
                "Household JSON scratch cleanup for %s found %d candidates; capping this pass at %d",
                destination.name,
                len(candidates),
                _MAX_SCRATCH_PRUNE_PER_DESTINATION,
            )
            candidates = sorted(candidates, key=_safe_mtime)[:_MAX_SCRATCH_PRUNE_PER_DESTINATION]

        for candidate in candidates:
            try:
                if safe_path_within(candidate, resolved_parent, reject_symlinks=True) is None:
                    continue
                if not candidate.is_file() or candidate.stat().st_mtime >= cutoff:
                    continue
                candidate.unlink()
                removed += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.warning("Failed to prune household JSON scratch file %s: %s", candidate, exc)
    return removed


def chmod_owner_only(path: Path) -> None:
    """Best-effort: make an existing file owner-only. Never raises."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def unlink_legacy_fixed_tmp(path: Path) -> None:
    """Best-effort unlink of the old shared ``<name>.json.tmp`` scratch."""
    try:
        path.with_suffix(".json.tmp").unlink(missing_ok=True)
    except OSError:
        pass


def atomic_write_json(
    path: Path,
    payload: dict[str, Any],
    *,
    ensure_ascii: bool,
    indent: int | None = 2,
    sort_keys: bool = True,
) -> None:
    """Write ``payload`` to ``path`` via a unique owner-only temp, then replace.

    ``ensure_ascii`` is required so every caller states its on-disk bytes.
    """
    body = json.dumps(payload, ensure_ascii=ensure_ascii, indent=indent, sort_keys=sort_keys)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    replaced = False
    try:
        fchmod = getattr(os, "fchmod", None)
        if fchmod is None:
            raise OSError("owner-only atomic JSON writes require os.fchmod")
        fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(body)
        os.replace(tmp_path, path)
        replaced = True
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if not replaced:
            try:
                tmp_path.unlink()
            except OSError:
                pass
