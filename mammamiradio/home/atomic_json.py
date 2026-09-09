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
import os
import tempfile
from pathlib import Path
from typing import Any


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
        os.fchmod(fd, 0o600)
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
