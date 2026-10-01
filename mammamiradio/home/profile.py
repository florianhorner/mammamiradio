"""Read verified private V1 Home profiles without shipping household mappings."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mammamiradio.home import migration

PROFILE_FILENAME = "home_profile_v1.json"
PROFILE_BINDING_TABLE = "_mammamiradio_home_profile_v1"
COMPATIBILITY_SNAPSHOT_SHA256 = "c49225bb57f04945c790c2c4883c6d6ee5edec5181761e3bce8f44f16577ecf3"
_MAX_PROFILE_BYTES = 128 * 1024
_LOCK = threading.Lock()
logger = logging.getLogger(__name__)


def _canonical(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class HomeProfileV1:
    """Immutable serialized snapshot with fresh containers for every reader."""

    profile_id: str
    content_digest: str
    _canonical_document: str = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._canonical_document)


def _profile(document: object) -> HomeProfileV1:
    if not isinstance(document, dict):
        raise ValueError("invalid profile document")
    identity = document.get("profile_id")
    if not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{32}", identity) is None:
        raise ValueError("invalid profile identity")
    canonical = _canonical(document)
    snapshot = dict(document)
    snapshot.pop("profile_id")
    if hashlib.sha256(_canonical(snapshot).encode()).hexdigest() != COMPATIBILITY_SNAPSHOT_SHA256:
        raise ValueError("profile does not match the compatibility snapshot")
    return HomeProfileV1(identity, hashlib.sha256(canonical.encode()).hexdigest(), canonical)


def _read_profile(path: Path, *, sync: bool = False) -> HomeProfileV1:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
            raise ValueError("profile must be an owner-only regular file")
        raw = handle.read(_MAX_PROFILE_BYTES + 1)
        if len(raw) > _MAX_PROFILE_BYTES:
            raise ValueError("profile exceeds size limit")
        result = _profile(json.loads(raw))
        if sync:
            os.fsync(handle.fileno())
            if not os.path.samestat(info, path.stat(follow_symlinks=False)):
                raise ValueError("profile replaced during synchronization")
        return result


def _binding(connection: sqlite3.Connection) -> tuple[str, str | None] | None:
    schema = connection.execute("SELECT type FROM sqlite_master WHERE name = ?", (PROFILE_BINDING_TABLE,)).fetchone()
    if schema is None:
        return None
    if schema != ("table",):
        raise ValueError("profile binding must be a table")
    rows = connection.execute(
        f"SELECT singleton, profile_id, content_digest FROM {PROFILE_BINDING_TABLE} LIMIT 2"
    ).fetchall()
    if not rows:
        raise ValueError("empty existing profile binding")
    if (
        len(rows) != 1
        or type(rows[0][0]) is not int
        or rows[0][0] != 1
        or not isinstance(rows[0][1], str)
        or re.fullmatch(r"[0-9a-f]{32}", rows[0][1]) is None
        or (rows[0][2] is not None and not isinstance(rows[0][2], str))
    ):
        raise ValueError("malformed profile binding")
    return rows[0][1], rows[0][2]


def load_home_profile_v1(state_dir: Path, db_path: Path) -> HomeProfileV1 | None:
    """Read-only readiness check; missing or conflicting evidence is unready."""
    try:
        if migration.load_legacy_home_provenance_v1(state_dir, db_path) is None:
            return None
        profile = _read_profile(state_dir / PROFILE_FILENAME)
        connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            if _binding(connection) == (profile.profile_id, profile.content_digest):
                return profile
        finally:
            connection.close()
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError, RecursionError):
        pass
    return None


def resume_home_profile_v1(state_dir: Path, db_path: Path) -> HomeProfileV1 | None:
    """Finish only a matching published file from an interrupted V1 export.

    No new profile or intent is created. Missing/conflicting evidence remains
    unready. This function runs outside the audio startup path.
    """
    try:
        with _LOCK:
            if migration.load_legacy_home_provenance_v1(state_dir, db_path) is None:
                return None
            connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=rw", uri=True)
            try:
                path = state_dir / PROFILE_FILENAME
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    binding = _binding(connection)
                    if binding is None:
                        return None
                if binding[1] is not None:
                    existing = load_home_profile_v1(state_dir, db_path)
                    if existing is None:
                        raise ValueError("unverifiable bound profile")
                    return existing
                # Bind only bytes validated and synced through the same safe FD.
                profile = _read_profile(path, sync=True)
                if profile.profile_id != binding[0]:
                    raise ValueError("profile belongs to another export intent")
                # Re-sync the directory too when recovering an interrupted export.
                fd = os.open(state_dir, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                if migration.load_legacy_home_provenance_v1(state_dir, db_path) is None:
                    return None
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        f"UPDATE {PROFILE_BINDING_TABLE} SET content_digest = ? "
                        "WHERE singleton = 1 AND profile_id = ? AND content_digest IS NULL",
                        (profile.content_digest, profile.profile_id),
                    )
                    if _binding(connection) != (profile.profile_id, profile.content_digest):
                        raise ValueError("conflicting profile binding")
            finally:
                connection.close()
            return load_home_profile_v1(state_dir, db_path)
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError, RecursionError):
        # Exception details can contain household values or local paths.
        logger.warning("Private Home compatibility snapshot is incomplete; will retry")
        return None
