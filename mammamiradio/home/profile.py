"""Private update-N compatibility snapshot; not a runtime authorization source.

Only the compiled legacy configuration is exported, never observed HA data.
Schema 1 assigns semantic roles to the existing fixed mood/formatting rules;
it is not a new rules language or a grant of access to additional entities.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mammamiradio.home import migration

PROFILE_FILENAME = "home_profile_v1.json"
PROFILE_BINDING_TABLE = "_mammamiradio_home_profile_v1"
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


def _legacy_document(profile_id: str) -> dict[str, Any]:
    # Imports stay inside the one-time exporter, away from authorization startup.
    from mammamiradio.core.listener_truth import _AUTHORIZED_HOME_RETURN_SOURCES
    from mammamiradio.home import catalog, ha_context
    from mammamiradio.home.authorization import NARROW_DAYLIGHT_ENTITY_ID, NARROW_WEATHER_ENTITY_ID
    from mammamiradio.home.context_director import CURATED_COFFEE_ENTITY_IDS

    manifest = migration.LEGACY_HOME_MANIFEST_V1
    entries = manifest.entries
    tiers = (ha_context.GOLD_ENTITIES, ha_context.SILVER_ENTITIES, ha_context.BRONZE_ENTITIES)
    expected = [
        (priority, entity_id)
        for priority, tier in zip(("gold", "silver", "bronze"), tiers, strict=True)
        for entity_id in tier
    ]
    if expected != [(entry.priority, entry.entity_id) for entry in entries]:
        raise ValueError("legacy inventory drift")
    residents = [entry.entity_id for entry in entries if entry.role in {"resident_one", "resident_two"}]
    returns = [
        {"entity_id": source.removeprefix("ha:"), "alias": alias}
        for source, alias in _AUTHORIZED_HOME_RETURN_SOURCES.items()
    ]
    if residents != [row["entity_id"] for row in returns] or len(residents) != 2:
        raise ValueError("legacy resident binding drift")
    return {
        "schema_version": 1,
        "profile_id": profile_id,
        "manifest_digest": manifest.entity_id_digest,
        "entities": [
            {
                "entity_id": entry.entity_id,
                "role": entry.role,
                "priority": entry.priority,
                "scopes": list(entry.scopes),
                "label_it": catalog.ENTITY_LABELS[entry.entity_id],
                "label_en": catalog.ENTITY_LABELS_EN[entry.entity_id],
            }
            for entry in entries
        ],
        "ambient_labels": [
            {
                "entity_id": entity_id,
                "label_it": catalog.ENTITY_LABELS[entity_id],
                "label_en": catalog.ENTITY_LABELS_EN[entity_id],
            }
            for entity_id in (NARROW_WEATHER_ENTITY_ID, NARROW_DAYLIGHT_ENTITY_ID)
        ],
        "reactive_triggers": [
            {"entity_id": entity_id, "state": state, "directive": directive, "cooldown": cooldown}
            for entity_id, state, directive, cooldown in ha_context.REACTIVE_TRIGGERS
        ],
        "threshold_triggers": [dict(trigger) for trigger in ha_context.THRESHOLD_TRIGGERS],
        "resident_returns": returns,
        "empty_home_residents": residents,
        "director_coffee_entities": sorted(CURATED_COFFEE_ENTITY_IDS),
    }


def _profile(document: object) -> HomeProfileV1:
    if not isinstance(document, dict):
        raise ValueError("invalid profile document")
    identity = document.get("profile_id")
    if not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{32}", identity) is None:
        raise ValueError("invalid profile identity")
    canonical = _canonical(document)
    # N accepts only its complete compiled snapshot, including ordering/types.
    # N+1 will consume this versioned schema without the compiled source IDs.
    if canonical != _canonical(_legacy_document(identity)):
        raise ValueError("profile does not match the compatibility snapshot")
    return HomeProfileV1(identity, hashlib.sha256(canonical.encode()).hexdigest(), canonical)


def _read_profile(path: Path, *, sync: bool = False) -> HomeProfileV1:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
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
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (PROFILE_BINDING_TABLE,)).fetchone() is None:
        return None
    rows = connection.execute(
        f"SELECT singleton, profile_id, content_digest FROM {PROFILE_BINDING_TABLE} LIMIT 2"
    ).fetchall()
    if not rows:
        raise ValueError("empty existing profile binding")
    if (
        len(rows) != 1
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


def export_legacy_home_profile_v1(state_dir: Path, db_path: Path) -> HomeProfileV1 | None:
    """Prepare once, publish file before DB binding, and never repair conflicts.

    A pending DB intent reserves an installation-local identity. An interrupted
    export can finish only when the file matches that intent and the complete
    compiled snapshot. No DB write lock spans file I/O.
    Call off the audio/event-loop path; errors leave existing runtime untouched.
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
                        if path.exists() or path.is_symlink():
                            raise ValueError("profile without local export intent")
                        connection.execute(
                            f"CREATE TABLE {PROFILE_BINDING_TABLE} ("
                            "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                            "profile_id TEXT NOT NULL, content_digest TEXT)"
                        )
                        binding = (uuid.uuid4().hex, None)
                        connection.execute(f"INSERT INTO {PROFILE_BINDING_TABLE} VALUES (1, ?, NULL)", (binding[0],))
                if binding[1] is not None:
                    existing = load_home_profile_v1(state_dir, db_path)
                    if existing is None:
                        raise ValueError("unverifiable bound profile")
                    return existing
                try:
                    _read_profile(path)
                except FileNotFoundError:
                    profile = _profile(_legacy_document(binding[0]))
                    try:
                        migration._atomic_write_json(path, profile.to_dict(), replace_existing=False)
                    except FileExistsError:
                        pass  # Another exporter won publication; validate its complete file.
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
