"""Unit tests for track_rules.py — per-track personality rules."""

from __future__ import annotations

from pathlib import Path

from mammamiradio.core.sync import init_db
from mammamiradio.playlist.track_rules import add_rule, get_rules


def _fresh_db(tmp_path: Path) -> Path:
    db = tmp_path / "mammamiradio.db"
    init_db(db)
    return db


def test_get_rules_returns_empty_when_db_missing(tmp_path: Path):
    db = tmp_path / "nonexistent.db"
    result = get_rules(db, "dQw4w9WgXcQ")
    assert result == []


def test_get_rules_returns_empty_for_unknown_track(tmp_path: Path):
    db = _fresh_db(tmp_path)
    result = get_rules(db, "unknown_id")
    assert result == []


def test_add_and_get_rules_roundtrip(tmp_path: Path):
    db = _fresh_db(tmp_path)
    add_rule(db, "dQw4w9WgXcQ", "plays too often")
    add_rule(db, "dQw4w9WgXcQ", "skip after chorus")
    rules = get_rules(db, "dQw4w9WgXcQ")
    assert len(rules) == 2
    assert "plays too often" in rules
    assert "skip after chorus" in rules


def test_add_rule_truncates_long_text(tmp_path: Path):
    db = _fresh_db(tmp_path)
    long_rule = "x" * 300
    add_rule(db, "abc", long_rule)
    rules = get_rules(db, "abc")
    assert len(rules) == 1
    assert len(rules[0]) == 200


def test_add_rule_handles_db_error(tmp_path: Path):
    # Pass a path that exists but is a directory, not a file — triggers SQLite error.
    bad_db = tmp_path  # directory, not a file
    add_rule(bad_db, "abc", "some rule")  # must not raise


def test_get_rules_handles_db_error(tmp_path: Path):
    # Create a non-SQLite file to trigger a DB error on read.
    bad_db = tmp_path / "bad.db"
    bad_db.write_bytes(b"not a sqlite file")
    result = get_rules(bad_db, "abc")
    assert result == []
