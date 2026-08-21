"""Durable, policy-free primitives for the First Listen setup flow.

This module owns two deliberately separate contracts:

* :class:`FirstListenReceiptV1` records only factual setup events.  It is never
  consulted as runtime authorization.
* :class:`FirstListenInstallOriginV1` projects a feature-era witness pair.  It
  does not import or reuse the older Home-context migration boundary.

All public async filesystem mutations run off the event loop.  Missing or
malformed state projects to an incomplete receipt or an ``unknown`` origin; it
never blocks audio startup or widens privacy behavior.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

logger = logging.getLogger(__name__)

FIRST_LISTEN_RECEIPT_SCHEMA_VERSION = 1
FIRST_LISTEN_RECEIPT_FILENAME = "first_listen_receipt_v1.json"
FIRST_LISTEN_ORIGIN_SCHEMA_VERSION = 1
FIRST_LISTEN_ORIGIN_FILENAME = "first_listen_install_origin_v1.json"
FIRST_LISTEN_ORIGIN_TABLE = "_mammamiradio_first_listen_install_origin_v1"

_MAX_STATE_FILE_BYTES = 4096
_MAX_FUTURE_SKEW_SECONDS = 300.0
_MEDIA_PLAYER_ENTITY_ID_RE = re.compile(r"^media_player\.[a-z0-9_]+$")
_ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_MISSING = object()
_INVALID = object()

_T = TypeVar("_T")


class FirstListenReceiptError(RuntimeError):
    """Base class for safe receipt-store failures."""


class FirstListenReceiptUnavailableError(FirstListenReceiptError):
    """The receipt could not be durably persisted."""


class FirstListenAttemptMismatchError(FirstListenReceiptError):
    """Verification did not match the currently accepted playback attempt."""


class FirstListenReceiptLoadStatus(StrEnum):
    """Trust state for the asynchronous startup receipt load."""

    PENDING = "pending"
    MISSING = "missing"
    PRESENT = "present"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class FirstListenReceiptV1:
    """Policy-free facts recorded by the first-listen flow."""

    selected_entity_id: str | None = None
    accepted_attempt_id: str | None = None
    accepted_at: float | None = None
    heard_at: float | None = None
    privacy_reviewed_at: float | None = None

    @property
    def audio_complete(self) -> bool:
        """Return whether the operator confirmed hearing an accepted attempt."""
        return self.heard_at is not None

    @property
    def privacy_complete(self) -> bool:
        """Return whether the operator completed the privacy review step."""
        return self.privacy_reviewed_at is not None

    def to_dict(self) -> dict[str, object]:
        """Return the exact v1 persistence shape."""
        return {
            "schema_version": FIRST_LISTEN_RECEIPT_SCHEMA_VERSION,
            "selected_entity_id": self.selected_entity_id,
            "accepted_attempt_id": self.accepted_attempt_id,
            "accepted_at": self.accepted_at,
            "heard_at": self.heard_at,
            "privacy_reviewed_at": self.privacy_reviewed_at,
        }


@dataclass(frozen=True, slots=True)
class FirstListenReceiptLoadResult:
    """One explicit receipt-load outcome.

    ``MISSING`` is intentionally distinct from ``UNAVAILABLE``: only a proven
    missing file can make a later fresh-install boot eligible for the prelude.
    """

    status: FirstListenReceiptLoadStatus
    receipt: FirstListenReceiptV1 | None = None


def first_listen_receipt_path(cache_dir: Path) -> Path:
    """Return the receipt path under the existing private state directory."""
    return Path(cache_dir) / "state" / FIRST_LISTEN_RECEIPT_FILENAME


def _timestamp_or_none(value: object, *, now: float) -> float | object | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return _INVALID
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0 or parsed > now + _MAX_FUTURE_SKEW_SECONDS:
        return _INVALID
    return parsed


def _receipt_from_payload(payload: object, *, now: float) -> FirstListenReceiptV1 | None:
    if not isinstance(payload, dict):
        return None
    expected_keys = {
        "schema_version",
        "selected_entity_id",
        "accepted_attempt_id",
        "accepted_at",
        "heard_at",
        "privacy_reviewed_at",
    }
    schema_version = payload.get("schema_version")
    if (
        set(payload) != expected_keys
        or type(schema_version) is not int
        or schema_version != FIRST_LISTEN_RECEIPT_SCHEMA_VERSION
    ):
        return None

    entity_id = payload.get("selected_entity_id")
    attempt_id = payload.get("accepted_attempt_id")
    if entity_id is not None and (
        not isinstance(entity_id, str)
        or len(entity_id) > 255
        or _MEDIA_PLAYER_ENTITY_ID_RE.fullmatch(entity_id) is None
    ):
        return None
    if attempt_id is not None and (not isinstance(attempt_id, str) or _ATTEMPT_ID_RE.fullmatch(attempt_id) is None):
        return None

    accepted_at = _timestamp_or_none(payload.get("accepted_at"), now=now)
    heard_at = _timestamp_or_none(payload.get("heard_at"), now=now)
    privacy_reviewed_at = _timestamp_or_none(payload.get("privacy_reviewed_at"), now=now)
    if _INVALID in (accepted_at, heard_at, privacy_reviewed_at):
        return None
    if accepted_at is not None and not isinstance(accepted_at, float):
        return None
    if heard_at is not None and not isinstance(heard_at, float):
        return None
    if privacy_reviewed_at is not None and not isinstance(privacy_reviewed_at, float):
        return None

    attempt_fields = (entity_id, attempt_id, accepted_at)
    if any(value is None for value in attempt_fields) and any(value is not None for value in attempt_fields):
        return None
    if heard_at is not None and accepted_at is None:
        return None
    if heard_at is not None and accepted_at is not None and heard_at < accepted_at:
        return None

    return FirstListenReceiptV1(
        selected_entity_id=entity_id,
        accepted_attempt_id=attempt_id,
        accepted_at=accepted_at,
        heard_at=heard_at,
        privacy_reviewed_at=privacy_reviewed_at,
    )


def _read_bounded_json(path: Path) -> object:
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(_MAX_STATE_FILE_BYTES + 1)
    except FileNotFoundError:
        return _MISSING
    except OSError:
        logger.warning("First-listen state is unreadable: %s", path)
        return _INVALID
    if len(raw) > _MAX_STATE_FILE_BYTES:
        logger.warning("Ignoring oversized first-listen state: %s", path)
        return _INVALID
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Ignoring malformed first-listen state: %s", path)
        return _INVALID


def load_first_listen_receipt_result(path: Path, now: float | None = None) -> FirstListenReceiptLoadResult:
    """Load a bounded v1 receipt while preserving its trust outcome."""
    payload = _read_bounded_json(Path(path))
    if payload is _MISSING:
        return FirstListenReceiptLoadResult(FirstListenReceiptLoadStatus.MISSING)
    if payload is _INVALID:
        return FirstListenReceiptLoadResult(FirstListenReceiptLoadStatus.UNAVAILABLE)
    receipt = _receipt_from_payload(payload, now=time.time() if now is None else float(now))
    if receipt is None:
        logger.warning("Ignoring invalid first-listen receipt schema: %s", path)
        return FirstListenReceiptLoadResult(FirstListenReceiptLoadStatus.UNAVAILABLE)
    return FirstListenReceiptLoadResult(FirstListenReceiptLoadStatus.PRESENT, receipt)


def load_first_listen_receipt(path: Path, now: float | None = None) -> FirstListenReceiptV1 | None:
    """Load a receipt, preserving the original incomplete-on-failure API."""
    return load_first_listen_receipt_result(path, now).receipt


def _atomic_write_json(path: Path, payload: Mapping[str, object], *, overwrite: bool = True) -> None:
    """Publish one owner-only JSON object atomically and durably."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # First-listen receipts and origin witnesses are private setup metadata.
    # Tighten an existing directory as well as one created under a permissive
    # process umask before publishing either file.
    os.chmod(path.parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(tmp_path, path)
        else:
            # link() is an atomic no-overwrite publish on the same filesystem.
            os.link(tmp_path, path)
            tmp_path.unlink()
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


async def _run_serialized_io(function: Callable[..., _T], *args: object) -> _T:
    """Run blocking I/O while making cancellation wait for the worker to finish."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # ``to_thread`` keeps running after waiter cancellation.  Drain it before
        # the caller's mutation lock can be released, otherwise a later mutation
        # could race the still-active atomic writer.
        try:
            await task
        except Exception:
            pass
        raise


class FirstListenReceiptStore:
    """App-scoped receipt store with one serialized mutation boundary."""

    def __init__(self, cache_dir: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = first_listen_receipt_path(cache_dir)
        self._clock = clock
        self._mutation_lock = asyncio.Lock()

    def _fact_timestamp(self, supplied: float | None) -> float:
        now = float(self._clock())
        value = now if supplied is None else supplied
        parsed = _timestamp_or_none(value, now=now)
        if parsed is _INVALID or parsed is None or not isinstance(parsed, float):
            raise ValueError("first-listen event timestamp must be finite, positive, and not in the future")
        return parsed

    async def load(self) -> FirstListenReceiptV1 | None:
        """Return the current receipt without ever treating corruption as completion."""
        return (await self.load_result()).receipt

    async def load_result(self) -> FirstListenReceiptLoadResult:
        """Return the receipt and whether it was missing or unavailable."""
        async with self._mutation_lock:
            return await _run_serialized_io(load_first_listen_receipt_result, self.path, self._clock())

    async def _persist(self, receipt: FirstListenReceiptV1) -> None:
        try:
            await _run_serialized_io(_atomic_write_json, self.path, receipt.to_dict())
        except OSError as exc:
            raise FirstListenReceiptUnavailableError("first-listen receipt could not be persisted") from exc

    async def record_accepted(
        self,
        entity_id: str,
        *,
        accepted_at: float | None = None,
    ) -> FirstListenReceiptV1:
        """Persist one HA-accepted playback and supersede older audio proof."""
        if (
            not isinstance(entity_id, str)
            or len(entity_id) > 255
            or _MEDIA_PLAYER_ENTITY_ID_RE.fullmatch(entity_id) is None
        ):
            raise ValueError("entity_id must be a media_player entity id")
        timestamp = self._fact_timestamp(accepted_at)
        attempt_id = secrets.token_urlsafe(24)
        async with self._mutation_lock:
            current = await _run_serialized_io(load_first_listen_receipt, self.path, self._clock())
            receipt = FirstListenReceiptV1(
                selected_entity_id=entity_id,
                accepted_attempt_id=attempt_id,
                accepted_at=timestamp,
                heard_at=None,
                privacy_reviewed_at=current.privacy_reviewed_at if current is not None else None,
            )
            await self._persist(receipt)
            return receipt

    async def verify(
        self,
        attempt_id: str,
        *,
        heard: bool,
        verified_at: float | None = None,
    ) -> FirstListenReceiptV1:
        """Compare-and-swap audible confirmation for the accepted attempt."""
        if not isinstance(heard, bool):
            raise ValueError("heard must be a boolean")
        if not isinstance(attempt_id, str) or _ATTEMPT_ID_RE.fullmatch(attempt_id) is None:
            raise FirstListenAttemptMismatchError("first-listen attempt is stale or unavailable")
        timestamp = self._fact_timestamp(verified_at) if heard else None
        async with self._mutation_lock:
            current = await _run_serialized_io(load_first_listen_receipt, self.path, self._clock())
            if current is None or not secrets.compare_digest(current.accepted_attempt_id or "", attempt_id):
                raise FirstListenAttemptMismatchError("first-listen attempt is stale or unavailable")
            if heard:
                if current.heard_at is not None:
                    return current
                if current.accepted_at is not None and timestamp is not None and timestamp < current.accepted_at:
                    raise ValueError("verification timestamp cannot precede playback acceptance")
                updated = replace(current, heard_at=timestamp)
            else:
                if current.heard_at is None:
                    return current
                updated = replace(current, heard_at=None)
            await self._persist(updated)
            return updated

    async def record_privacy_reviewed(
        self,
        *,
        reviewed_at: float | None = None,
    ) -> FirstListenReceiptV1:
        """Record only that the privacy step completed, never its policy choice."""
        timestamp = self._fact_timestamp(reviewed_at)
        async with self._mutation_lock:
            current = await _run_serialized_io(load_first_listen_receipt, self.path, self._clock())
            current = current or FirstListenReceiptV1()
            if current.privacy_reviewed_at is not None:
                return current
            updated = replace(current, privacy_reviewed_at=timestamp)
            await self._persist(updated)
            return updated


class FirstListenInstallOriginStatus(StrEnum):
    """Conservative feature-era install classification."""

    FRESH = "fresh"
    EXISTING = "existing"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FirstListenInstallOriginV1:
    """Projection produced only when both feature-specific witnesses agree."""

    status: FirstListenInstallOriginStatus

    @property
    def database_preexisted(self) -> bool | None:
        if self.status is FirstListenInstallOriginStatus.EXISTING:
            return True
        if self.status is FirstListenInstallOriginStatus.FRESH:
            return False
        return None


@dataclass(frozen=True, slots=True)
class FirstListenInstallOriginCapture:
    """In-memory database-existence fact captured before schema initialization.

    ``database_bare`` distinguishes a real prior install (a populated schema)
    from the file a crashed first boot leaves behind before any table commits.
    An unreadable preexisting file cannot prove a completed install and also
    reads as bare.
    """

    db_path: Path
    database_preexisted: bool
    database_bare: bool = False


class FirstListenOriginSentinelState(StrEnum):
    """Whether this process created the empty feature sentinel table."""

    NEWLY_CREATED = "newly_created"
    PREEXISTING = "preexisting"


_ORIGIN_TABLE_SQL = f"""
CREATE TABLE {FIRST_LISTEN_ORIGIN_TABLE} (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = {FIRST_LISTEN_ORIGIN_SCHEMA_VERSION}),
    database_preexisted INTEGER NOT NULL CHECK (database_preexisted IN (0, 1))
)
"""


@dataclass(frozen=True, slots=True)
class _InstallOriginWitnessV1:
    database_preexisted: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": FIRST_LISTEN_ORIGIN_SCHEMA_VERSION,
            "database_preexisted": self.database_preexisted,
        }


# Every completed post-R0 boot persists this witness table (DATABASE_ORIGIN_TABLE
# in home/migration.py — duplicated here because core must not import home; a
# drift-guard test asserts the two spellings stay identical).
_COMPLETED_BOOT_MARKER_TABLE = "_mammamiradio_home_install_origin_v1"


def capture_first_listen_install_origin(db_path: Path) -> FirstListenInstallOriginCapture:
    """Capture the only database-existence facts eligible for first-boot migration."""
    path = Path(db_path)
    preexisted = path.exists()
    bare = False
    if preexisted:
        bare = True
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0)
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (_COMPLETED_BOOT_MARKER_TABLE,),
            ).fetchone()
            bare = row is None
        except (OSError, sqlite3.Error, ValueError, TypeError):
            bare = True
        finally:
            if conn is not None:
                conn.close()
    return FirstListenInstallOriginCapture(db_path=path, database_preexisted=preexisted, database_bare=bare)


def ensure_first_listen_origin_sentinel(conn: sqlite3.Connection) -> FirstListenOriginSentinelState:
    """Create only the empty sentinel table and report whether it already existed."""
    try:
        conn.execute(_ORIGIN_TABLE_SQL)
    except sqlite3.OperationalError as exc:
        if "already exists" in str(exc).lower():
            return FirstListenOriginSentinelState.PREEXISTING
        raise
    return FirstListenOriginSentinelState.NEWLY_CREATED


def first_listen_origin_path(state_dir: Path) -> Path:
    """Return the feature-era install-origin sidecar path."""
    return Path(state_dir) / FIRST_LISTEN_ORIGIN_FILENAME


def _origin_witness_from_payload(payload: object) -> _InstallOriginWitnessV1 | object:
    if not isinstance(payload, dict):
        return _INVALID
    if set(payload) != {"schema_version", "database_preexisted"}:
        return _INVALID
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int or schema_version != FIRST_LISTEN_ORIGIN_SCHEMA_VERSION:
        return _INVALID
    database_preexisted = payload.get("database_preexisted")
    if not isinstance(database_preexisted, bool):
        return _INVALID
    return _InstallOriginWitnessV1(database_preexisted=database_preexisted)


def _load_origin_sidecar(state_dir: Path) -> _InstallOriginWitnessV1 | object:
    payload = _read_bounded_json(first_listen_origin_path(state_dir))
    if payload is _MISSING or payload is _INVALID:
        return payload
    return _origin_witness_from_payload(payload)


def _origin_table_has_expected_schema(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(f"PRAGMA table_info({FIRST_LISTEN_ORIGIN_TABLE})").fetchall()
    if len(rows) != 3:
        return False
    expected = (
        ("singleton", "INTEGER", 0, 1),
        ("schema_version", "INTEGER", 1, 0),
        ("database_preexisted", "INTEGER", 1, 0),
    )
    actual = tuple((str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5])) for row in rows)
    return actual == expected


def _load_database_origin(db_path: Path) -> _InstallOriginWitnessV1 | object:
    path = Path(db_path)
    if not path.is_file():
        return _MISSING
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0)
        table = conn.execute(
            "SELECT type FROM sqlite_master WHERE name = ?",
            (FIRST_LISTEN_ORIGIN_TABLE,),
        ).fetchone()
        if table is None:
            return _MISSING
        if table[0] != "table" or not _origin_table_has_expected_schema(conn):
            return _INVALID
        rows = conn.execute(
            f"SELECT singleton, schema_version, database_preexisted FROM {FIRST_LISTEN_ORIGIN_TABLE} LIMIT 2"
        ).fetchall()
    except (OSError, sqlite3.Error):
        return _INVALID
    finally:
        if conn is not None:
            conn.close()
    if len(rows) != 1:
        return _MISSING if not rows else _INVALID
    singleton, schema_version, database_preexisted = rows[0]
    if singleton != 1 or schema_version != FIRST_LISTEN_ORIGIN_SCHEMA_VERSION:
        return _INVALID
    if type(database_preexisted) is not int or database_preexisted not in (0, 1):
        return _INVALID
    return _InstallOriginWitnessV1(database_preexisted=bool(database_preexisted))


def load_first_listen_install_origin(state_dir: Path, db_path: Path) -> FirstListenInstallOriginV1:
    """Project fresh/existing only when both strict feature witnesses agree."""
    sidecar = _load_origin_sidecar(state_dir)
    database = _load_database_origin(db_path)
    if not isinstance(sidecar, _InstallOriginWitnessV1) or not isinstance(database, _InstallOriginWitnessV1):
        return FirstListenInstallOriginV1(FirstListenInstallOriginStatus.UNKNOWN)
    if sidecar.database_preexisted != database.database_preexisted:
        logger.warning("First-listen install-origin witnesses disagree; preserving unknown classification")
        return FirstListenInstallOriginV1(FirstListenInstallOriginStatus.UNKNOWN)
    status = (
        FirstListenInstallOriginStatus.EXISTING if sidecar.database_preexisted else FirstListenInstallOriginStatus.FRESH
    )
    return FirstListenInstallOriginV1(status)


def _migrate_first_listen_install_origin_blocking(
    capture: FirstListenInstallOriginCapture,
    sentinel_state: FirstListenOriginSentinelState,
    state_dir: Path,
) -> FirstListenInstallOriginV1:
    if sentinel_state is not FirstListenOriginSentinelState.NEWLY_CREATED:
        return load_first_listen_install_origin(state_dir, capture.db_path)

    sidecar = _load_origin_sidecar(state_dir)
    database = _load_database_origin(capture.db_path)
    # A transplanted, malformed, or concurrently populated witness is never
    # overwritten.  Only the pristine sentinel created on this feature boot may
    # receive the pre-init in-memory fact.
    if sidecar is not _MISSING or database is not _MISSING:
        return load_first_listen_install_origin(state_dir, capture.db_path)

    witness = _InstallOriginWitnessV1(capture.database_preexisted)
    path = first_listen_origin_path(state_dir)
    sidecar_created = False
    try:
        _atomic_write_json(path, witness.to_dict(), overwrite=False)
        sidecar_created = True
        conn = sqlite3.connect(str(capture.db_path), timeout=1.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not _origin_table_has_expected_schema(conn):
                raise sqlite3.DatabaseError("first-listen origin sentinel schema is malformed")
            existing = conn.execute(f"SELECT 1 FROM {FIRST_LISTEN_ORIGIN_TABLE} LIMIT 1").fetchone()
            if existing is not None:
                raise sqlite3.IntegrityError("first-listen origin sentinel is not empty")
            conn.execute(
                f"INSERT INTO {FIRST_LISTEN_ORIGIN_TABLE} "
                "(singleton, schema_version, database_preexisted) VALUES (1, ?, ?)",
                (FIRST_LISTEN_ORIGIN_SCHEMA_VERSION, int(capture.database_preexisted)),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    except FileExistsError:
        return load_first_listen_install_origin(state_dir, capture.db_path)
    except (OSError, sqlite3.Error) as exc:
        if sidecar_created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        logger.warning("First-listen install-origin migration deferred after local persistence failure: %s", exc)
        return FirstListenInstallOriginV1(FirstListenInstallOriginStatus.UNKNOWN)
    return load_first_listen_install_origin(state_dir, capture.db_path)


async def migrate_first_listen_install_origin(
    capture: FirstListenInstallOriginCapture,
    sentinel_state: FirstListenOriginSentinelState,
    state_dir: Path,
) -> FirstListenInstallOriginV1:
    """Persist the first feature-era fact off-loop after audio tasks have started."""
    return await _run_serialized_io(
        _migrate_first_listen_install_origin_blocking,
        capture,
        sentinel_state,
        Path(state_dir),
    )
