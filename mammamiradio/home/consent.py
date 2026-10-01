"""Durable ambient-only consent for an installation leaving legacy Home mode."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

CONSENT_TABLE = "_mammamiradio_home_ambient_consent_v1"


@dataclass(frozen=True)
class AmbientConsent:
    capped: bool = False
    granted: bool = False
    readable: bool = True


def _read_consent(connection: sqlite3.Connection) -> AmbientConsent:
    object_type = connection.execute("SELECT type FROM sqlite_master WHERE name=?", (CONSENT_TABLE,)).fetchone()
    if object_type is None:
        return AmbientConsent()
    if object_type != ("table",):
        raise ValueError("conflicting Home consent object")
    rows = connection.execute(f"SELECT singleton, scope_cap, granted FROM {CONSENT_TABLE} LIMIT 2").fetchall()
    if (
        len(rows) != 1
        or type(rows[0][0]) is not int
        or rows[0][:2] != (1, "ambient-v1")
        or type(rows[0][2]) is not int
        or rows[0][2] not in (0, 1)
    ):
        raise ValueError("conflicting Home consent")
    return AmbientConsent(capped=True, granted=bool(rows[0][2]))


def load_ambient_consent(db_path: Path) -> AmbientConsent:
    """Read without creating state; malformed evidence can never grant access."""
    try:
        with closing(sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
            return _read_consent(connection)
    except (OSError, sqlite3.Error, ValueError):
        return AmbientConsent(capped=True, readable=False)


def save_ambient_consent(db_path: Path, *, granted: bool) -> None:
    """Commit one permanent scope cap; revocation never removes the row."""
    if type(granted) is not bool:
        raise ValueError("consent must be a boolean")
    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=rw", uri=True)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _read_consent(connection)
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {CONSENT_TABLE} ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                "scope_cap TEXT NOT NULL CHECK(scope_cap='ambient-v1'), "
                "granted INTEGER NOT NULL CHECK(granted IN (0,1)))"
            )
            connection.execute(
                f"INSERT INTO {CONSENT_TABLE} VALUES (1, 'ambient-v1', ?) "
                "ON CONFLICT(singleton) DO UPDATE SET granted=excluded.granted",
                (int(granted),),
            )
    finally:
        connection.close()
