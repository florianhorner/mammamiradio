"""Bounded bridge provenance for the legacy developer-home configuration.

This module deliberately does not detect an installation from labels, areas, or
the presence of a database *after* startup.  The caller captures whether the
station database existed before database initialization.  That first durable
answer is immutable, so a database created by a cold start cannot become a
legacy installation on a later restart.

The exact entity IDs below are migration-only input.  They must never be used as
runtime defaults for a fresh installation.  Once a pre-existing installation
has observed every exact ID, :func:`seal_legacy_home_provenance_v1` persists only
the manifest version and digest, the bridge release that made the observation,
and the observation time.  No Home Assistant state or label is written.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import tempfile
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

PREFLIGHT_FILENAME = "legacy_home_preflight_v1.json"
PROVENANCE_FILENAME = "legacy_home_provenance_v1.json"
LEGACY_HOME_MANIFEST_VERSION = 1
DATABASE_ORIGIN_TABLE = "_mammamiradio_home_install_origin_v1"

LegacyPriority = Literal["gold", "silver", "bronze"]


@dataclass(frozen=True)
class LegacyHomeManifestEntryV1:
    """One exact legacy entity and its future profile-migration intent."""

    entity_id: str
    priority: LegacyPriority
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class LegacyHomeManifestV1:
    """Immutable migration-only manifest for the previously curated home."""

    version: int
    entries: tuple[LegacyHomeManifestEntryV1, ...]

    def __post_init__(self) -> None:
        entity_ids = [entry.entity_id for entry in self.entries]
        if self.version != LEGACY_HOME_MANIFEST_VERSION:
            raise ValueError("legacy manifest version must match the v1 bridge")
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("legacy manifest entity IDs must be unique")

    @property
    def entity_ids(self) -> frozenset[str]:
        """Return the exact identifiers required for a legacy observation."""
        return frozenset(entry.entity_id for entry in self.entries)

    @property
    def entity_id_digest(self) -> str:
        """Return a stable SHA-256 digest of the exact sorted ID set."""
        canonical = json.dumps(sorted(self.entity_ids), ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_AMBIENT = ("ambient_context",)
_PRESENCE = ("ambient_context", "presence")
_MOMENT = ("ambient_context", "moment")
_RESIDENT = ("ambient_context", "moment", "presence", "resident")
_RESIDENT_CONTEXT = ("ambient_context", "presence", "resident")
_WEATHER = ("ambient_context", "weather")


def _entry(
    entity_id: str,
    priority: LegacyPriority,
    scopes: tuple[str, ...] = _AMBIENT,
) -> LegacyHomeManifestEntryV1:
    return LegacyHomeManifestEntryV1(entity_id=entity_id, priority=priority, scopes=scopes)


# The mapping intentionally duplicates the final developer-home defaults.  Keeping
# it here lets later releases remove those IDs from live selection code without
# destroying the one-time continuity bridge.
LEGACY_HOME_MANIFEST_V1 = LegacyHomeManifestV1(
    version=LEGACY_HOME_MANIFEST_VERSION,
    entries=(
        _entry("switch.bar_kaffeemaschine_steckdose", "gold", _MOMENT),
        _entry("input_select.kaffee_dad_jokes", "gold"),
        _entry("vacuum.goldstaubsucher", "gold", _MOMENT),
        _entry("vacuum.matrix10_ultra", "gold", _MOMENT),
        _entry("weather.forecast_home", "gold", _WEATHER),
        _entry("person.florian_horner", "gold", _RESIDENT),
        _entry("person.sabrina", "gold", _RESIDENT),
        _entry("person.schnuffi", "gold", _RESIDENT_CONTEXT),
        _entry("lock.lock_ultra_8d3c", "gold", _MOMENT),
        _entry("input_button.foyer_fahrstuhl_fingerbot_push_button", "gold"),
        _entry("binary_sensor.8_stockwerk_group_sensor_wohnzimmer_esszimmer_bar", "silver", _PRESENCE),
        _entry("input_select.bedroom_occupancy_state", "silver", _PRESENCE),
        _entry("switch.bad_gross_waschmaschine_steckdose", "silver"),
        _entry("media_player.samsung_s95ca_65", "silver"),
        _entry("media_player.wohnzimmer_sonos_arc_lautsprecher", "silver"),
        _entry("media_player.esszimmer", "silver"),
        _entry("climate.wohnzimmer_tado_heizung", "silver"),
        _entry("climate.schlafzimmer", "silver"),
        _entry("sun.sun", "silver"),
        _entry("fan.bad_gross_lufter_shelly", "silver"),
        _entry("fan.bad_klein_lufter", "silver"),
        _entry("fan.kuche_lufter", "silver"),
        _entry("light.magic_areas_light_groups_wohnzimmer_all_lights", "silver"),
        _entry("light.magic_areas_light_groups_schlafzimmer_all_lights", "silver"),
        _entry("light.magic_areas_light_groups_kuche_all_lights", "silver"),
        _entry("light.magic_areas_light_groups_esszimmer_all_lights", "silver"),
        _entry("sensor.bar_bali_boot_steckdose_power", "silver"),
        _entry("sensor.kuche_kaffeemaschine_steckdose_power", "silver", _MOMENT),
        _entry("light.schlafzimmer_sternenlicht_projektor_2", "silver"),
        _entry("light.kleiderschrank_sternenlicht_projektor", "silver"),
        _entry("light.terrasse_9_outdoor_lichtschlauch", "silver", _MOMENT),
        _entry("sensor.haushalt_stromverbrauch_gesamt", "silver"),
        _entry("input_datetime.last_sleep_time", "bronze"),
        _entry("input_datetime.last_wake_time", "bronze"),
        _entry("binary_sensor.buro_9_ring_intercom_klingelt", "bronze"),
    ),
)


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
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
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


def observes_legacy_home_manifest_v1(observed: Mapping[str, object] | Iterable[str]) -> bool:
    """Return whether every exact migration-only ID has been observed.

    A full Home Assistant state mapping naturally contains unrelated entities;
    those extras are ignored.  Names, labels, areas, and state values never
    contribute to the decision.
    """
    if isinstance(observed, Mapping):
        observed_ids = {entity_id for entity_id in observed if isinstance(entity_id, str)}
    elif isinstance(observed, str | bytes):
        return False
    else:
        observed_ids = {entity_id for entity_id in observed if isinstance(entity_id, str)}
    return LEGACY_HOME_MANIFEST_V1.entity_ids.issubset(observed_ids)


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
    if manifest_digest != LEGACY_HOME_MANIFEST_V1.entity_id_digest:
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


def seal_legacy_home_provenance_v1(
    state_dir: Path,
    observed: Mapping[str, object] | Iterable[str],
    *,
    db_path: Path,
    bridge_app_version: str,
    observed_at: float | None = None,
) -> LegacyHomeProvenanceV1 | None:
    """Seal provenance for an eligible pre-existing exact legacy home.

    The first valid marker is immutable and returned on repeat calls.  Missing,
    cold, corrupt, or incomplete evidence returns ``None`` and writes nothing.
    """
    clean_version = _clean_bridge_app_version(bridge_app_version)
    timestamp = time.time() if observed_at is None else observed_at
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, int | float)
        or not math.isfinite(float(timestamp))
        or float(timestamp) < 0
    ):
        raise ValueError("observed_at must be a finite non-negative timestamp")

    path = provenance_path(state_dir)
    with _LOCK:
        if load_authoritative_legacy_home_preflight_v1(state_dir, db_path) is None:
            return None
        existing_data = _read_json(path)
        if existing_data is _INVALID:
            return None
        if existing_data is not _MISSING:
            existing = _parse_provenance(existing_data)
            if existing is None:
                logger.warning("Cannot trust malformed legacy-home provenance %s", path)
            return existing

        if not observes_legacy_home_manifest_v1(observed):
            return None

        provenance = LegacyHomeProvenanceV1(
            manifest_version=LEGACY_HOME_MANIFEST_VERSION,
            manifest_digest=LEGACY_HOME_MANIFEST_V1.entity_id_digest,
            bridge_app_version=clean_version,
            observed_at=float(timestamp),
        )
        _atomic_write_json(path, provenance.to_dict())
        return provenance


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    """Write one owner-only JSON object and atomically publish it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
