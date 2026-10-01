"""Read the immutable installation witnesses and V1 compatibility provenance.

The original curated entity inventory is no longer shipped. Existing metadata
retains its original digest and may authorize only a matching private profile.
Cold installation witnesses remain immutable across restarts.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

PREFLIGHT_FILENAME = "legacy_home_preflight_v1.json"
PROVENANCE_FILENAME = "legacy_home_provenance_v1.json"
LEGACY_HOME_MANIFEST_VERSION = 1
DATABASE_ORIGIN_TABLE = "_mammamiradio_home_install_origin_v1"

LEGACY_HOME_MANIFEST_DIGEST = "72201ec2e2b10ec6d9c594cae11d5cb5a5da6e11d744229f8f2a53cdf4c6613a"


@dataclass(frozen=True)
class LegacyHomePreflightV1:
    """The first durable pre-database existence observation."""

    database_preexisted: bool
    durable: bool = True


@dataclass(frozen=True)
class LegacyHomeProvenanceV1:
    """Validated, metadata-only proof that the legacy bridge may migrate."""

    manifest_version: int
    manifest_digest: str
    bridge_app_version: str
    observed_at: float

    def to_dict(self) -> dict[str, object]:
        """Return the exact privacy-bounded persistence shape."""
        return {
            "manifest_version": self.manifest_version,
            "manifest_digest": self.manifest_digest,
            "bridge_app_version": self.bridge_app_version,
            "observed_at": self.observed_at,
        }


_MISSING = object()
_INVALID = object()
_LOCK = threading.RLock()


def preflight_path(state_dir: Path) -> Path:
    """Return the immutable original-install witness path."""
    return Path(state_dir) / PREFLIGHT_FILENAME


def provenance_path(state_dir: Path) -> Path:
    """Return the sealed legacy-home provenance path."""
    return Path(state_dir) / PROVENANCE_FILENAME


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _MISSING
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("Cannot trust legacy-home bridge state %s: %s", path, exc)
        return _INVALID


def _parse_preflight(data: object) -> LegacyHomePreflightV1 | None:
    if not isinstance(data, dict) or set(data) != {"database_preexisted"}:
        return None
    value = data.get("database_preexisted")
    if type(value) is not bool:
        return None
    return LegacyHomePreflightV1(database_preexisted=value)


def load_legacy_home_preflight_v1(state_dir: Path) -> LegacyHomePreflightV1 | None:
    """Load the immutable first preflight fact, returning ``None`` on doubt."""
    data = _read_json(preflight_path(state_dir))
    if data is _MISSING or data is _INVALID:
        return None
    preflight = _parse_preflight(data)
    if preflight is None:
        logger.warning("Cannot trust malformed legacy-home preflight %s", preflight_path(state_dir))
    return preflight


def load_legacy_home_database_preflight_v1(db_path: Path) -> LegacyHomePreflightV1 | None:
    """Read the redundant install-origin witness without creating or migrating the DB.

    Older databases legitimately lack this R0 table on their first upgraded
    boot, so a missing table returns ``None``. Malformed or unreadable existing
    table state returns an explicit non-durable sentinel, letting startup fail
    narrow without treating the database as a first R0 upgrade or repairing it.
    """
    path = Path(db_path)
    if not path.is_file():
        return None
    connection: sqlite3.Connection | None = None
    try:
        # Build the read-only URI via as_uri() so a cache path containing a URI
        # metacharacter (``?``/``#``/space) is percent-encoded — a raw f-string
        # would truncate the filename at the first ``?`` and open the wrong DB,
        # stranding a genuine legacy install in narrow mode forever.
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        row = connection.execute(
            f"SELECT database_preexisted FROM {DATABASE_ORIGIN_TABLE} WHERE singleton = 1"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        logger.warning("Cannot trust database Home install origin %s: %s", path, exc)
        return LegacyHomePreflightV1(database_preexisted=False, durable=False)
    except (OSError, sqlite3.DatabaseError) as exc:
        logger.warning("Cannot trust database Home install origin %s: %s", path, exc)
        return LegacyHomePreflightV1(database_preexisted=False, durable=False)
    finally:
        if connection is not None:
            connection.close()
    if row is None or len(row) != 1 or type(row[0]) is not int or row[0] not in (0, 1):
        logger.warning("Cannot trust malformed database Home install origin %s", path)
        return LegacyHomePreflightV1(database_preexisted=False, durable=False)
    return LegacyHomePreflightV1(database_preexisted=bool(row[0]))


def load_authoritative_legacy_home_preflight_v1(
    state_dir: Path,
    db_path: Path,
) -> LegacyHomePreflightV1 | None:
    """Return a legacy-eligible preflight only when both durable witnesses agree."""
    sidecar = load_legacy_home_preflight_v1(state_dir)
    database = load_legacy_home_database_preflight_v1(db_path)
    if (
        sidecar is None
        or database is None
        or not sidecar.durable
        or not database.durable
        or not sidecar.database_preexisted
        or not database.database_preexisted
    ):
        return None
    return sidecar if sidecar == database else None


def persist_legacy_home_database_preflight_v1(
    db_path: Path,
    preflight: LegacyHomePreflightV1,
) -> LegacyHomePreflightV1:
    """Persist an immutable DB-local copy of a durable first-run observation.

    The DB copy lets a cold install recover safely if the sidecar witness is
    accidentally deleted. Existing values are never overwritten, and a
    disagreement is surfaced so startup can fail narrow rather than choosing.
    """
    if not preflight.durable:
        raise ValueError("database Home install origin requires a durable preflight")
    path = Path(db_path)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DATABASE_ORIGIN_TABLE} (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                database_preexisted INTEGER NOT NULL CHECK (database_preexisted IN (0, 1))
            )
            """
        )
        connection.execute(
            f"INSERT OR IGNORE INTO {DATABASE_ORIGIN_TABLE} (singleton, database_preexisted) VALUES (1, ?)",
            (int(preflight.database_preexisted),),
        )
        row = connection.execute(
            f"SELECT database_preexisted FROM {DATABASE_ORIGIN_TABLE} WHERE singleton = 1"
        ).fetchone()
        if row != (int(preflight.database_preexisted),):
            raise RuntimeError("database Home install origin conflicts with sidecar preflight")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return preflight


def capture_legacy_home_preflight_v1(
    state_dir: Path,
    *,
    database_preexisted: bool,
) -> LegacyHomePreflightV1:
    """Durably capture the first database-existence fact before DB creation.

    A valid existing witness always wins over the new observation.  A malformed
    witness is never repaired automatically: returning an ineligible, non-durable
    result is the privacy-safe interpretation.  Failure to create a first witness
    raises, so callers cannot safely proceed to database initialization as though
    the cold/pre-existing distinction had been recorded.
    """
    if type(database_preexisted) is not bool:
        raise ValueError("database_preexisted must be a boolean")

    path = preflight_path(state_dir)
    with _LOCK:
        data = _read_json(path)
        if data is _INVALID:
            return LegacyHomePreflightV1(database_preexisted=False, durable=False)
        if data is not _MISSING:
            existing = _parse_preflight(data)
            if existing is None:
                logger.warning("Cannot trust malformed legacy-home preflight %s", path)
                return LegacyHomePreflightV1(database_preexisted=False, durable=False)
            return existing

        _atomic_write_json(path, {"database_preexisted": database_preexisted})
        return LegacyHomePreflightV1(database_preexisted=database_preexisted)


def rewrite_legacy_home_preflight_cold_v1(state_dir: Path) -> LegacyHomePreflightV1:
    """Force the sidecar witness to the only truthful cold value, overwriting poison.

    Called when the database provably did not exist at process start: the sole
    correct durable answer is ``database_preexisted=False``.  A transplanted or
    malformed sidecar claiming a pre-existing database is demoted in memory by the
    startup guards, but unless it is also corrected on disk the *next* boot — where
    the database now exists — trusts the stale witness and self-agrees into legacy.

    Overwriting here does not violate first-answer immutability: that rule exists to
    stop a *legacy* claim from being silently rewritten.  Writing ``False`` can only
    ever narrow, so this is privacy fail-closed.  A witness that already parses as a
    valid cold answer is left untouched (idempotent, no needless disk churn).
    """
    path = preflight_path(state_dir)
    with _LOCK:
        data = _read_json(path)
        existing = _parse_preflight(data) if isinstance(data, dict) else None
        if existing is not None and existing.database_preexisted is False:
            return existing
        _atomic_write_json(path, {"database_preexisted": False})
        return LegacyHomePreflightV1(database_preexisted=False)


def _clean_bridge_app_version(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("bridge_app_version must be a string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("bridge_app_version must be a short printable version")
    clean = value.strip()
    if not clean or len(clean) > 80:
        raise ValueError("bridge_app_version must be a short printable version")
    return clean


def _parse_provenance(data: object) -> LegacyHomeProvenanceV1 | None:
    expected_keys = {"manifest_version", "manifest_digest", "bridge_app_version", "observed_at"}
    if not isinstance(data, dict) or set(data) != expected_keys:
        return None
    manifest_version = data.get("manifest_version")
    manifest_digest = data.get("manifest_digest")
    bridge_app_version = data.get("bridge_app_version")
    observed_at = data.get("observed_at")
    if type(manifest_version) is not int or manifest_version != LEGACY_HOME_MANIFEST_VERSION:
        return None
    if manifest_digest != LEGACY_HOME_MANIFEST_DIGEST:
        return None
    try:
        clean_version = _clean_bridge_app_version(bridge_app_version)
    except ValueError:
        return None
    if (
        isinstance(observed_at, bool)
        or not isinstance(observed_at, int | float)
        or not math.isfinite(float(observed_at))
        or float(observed_at) < 0
    ):
        return None
    return LegacyHomeProvenanceV1(
        manifest_version=manifest_version,
        manifest_digest=manifest_digest,
        bridge_app_version=clean_version,
        observed_at=float(observed_at),
    )


def load_legacy_home_provenance_v1(
    state_dir: Path,
    db_path: Path,
) -> LegacyHomeProvenanceV1 | None:
    """Load only provenance that exactly matches the current v1 manifest."""
    if load_authoritative_legacy_home_preflight_v1(state_dir, db_path) is None:
        return None
    data = _read_json(provenance_path(state_dir))
    if data is _MISSING or data is _INVALID:
        return None
    provenance = _parse_provenance(data)
    if provenance is None:
        logger.warning("Cannot trust malformed legacy-home provenance %s", provenance_path(state_dir))
    return provenance


def _atomic_write_json(path: Path, payload: Mapping[str, object], *, replace_existing: bool = True) -> None:
    """Write one owner-only JSON object and atomically publish it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1  # The file object now owns and closes the descriptor.
        with handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        if replace_existing:
            os.replace(tmp_path, path)
        else:
            # Link publishes a complete owner-only inode without overwriting a
            # profile another process may already have committed or prepared.
            os.link(tmp_path, path)
            tmp_path.unlink()
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            if fd >= 0:
                os.close(fd)
        except OSError:
            pass
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
