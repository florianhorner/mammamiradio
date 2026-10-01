"""Tests for the bounded legacy-home provenance bridge."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from mammamiradio.home.migration import (
    DATABASE_ORIGIN_TABLE,
    LEGACY_HOME_MANIFEST_DIGEST,
    PREFLIGHT_FILENAME,
    PROVENANCE_FILENAME,
    LegacyHomePreflightV1,
    capture_legacy_home_preflight_v1,
    load_legacy_home_database_preflight_v1,
    load_legacy_home_preflight_v1,
    load_legacy_home_provenance_v1,
    persist_legacy_home_database_preflight_v1,
    preflight_path,
    provenance_path,
    rewrite_legacy_home_preflight_cold_v1,
)


def _persist_database_witness(state_dir: Path) -> Path:
    preflight = load_legacy_home_preflight_v1(state_dir)
    assert preflight is not None and preflight.durable
    db_path = state_dir / "mammamiradio.db"
    persist_legacy_home_database_preflight_v1(db_path, preflight)
    return db_path


def test_capture_preflight_is_owner_only_and_idempotently_keeps_first_fact(tmp_path):
    first = capture_legacy_home_preflight_v1(tmp_path, database_preexisted=True)
    repeated = capture_legacy_home_preflight_v1(tmp_path, database_preexisted=False)

    assert first.database_preexisted is True
    assert repeated == first
    assert load_legacy_home_preflight_v1(tmp_path) == first
    assert json.loads(preflight_path(tmp_path).read_text(encoding="utf-8")) == {"database_preexisted": True}
    assert preflight_path(tmp_path).stat().st_mode & 0o777 == 0o600


def test_capture_fsyncs_file_and_containing_directory(tmp_path):
    with patch("mammamiradio.home.migration.os.fsync", wraps=os.fsync) as fsync:
        capture_legacy_home_preflight_v1(tmp_path, database_preexisted=False)

    assert fsync.call_count >= 2


def test_cold_preflight_remains_ineligible_after_database_exists_on_restart(tmp_path):
    capture_legacy_home_preflight_v1(tmp_path, database_preexisted=False)
    db_path = _persist_database_witness(tmp_path)

    restarted = capture_legacy_home_preflight_v1(tmp_path, database_preexisted=True)
    sealed = load_legacy_home_provenance_v1(tmp_path, db_path)

    assert restarted.database_preexisted is False
    assert sealed is None
    assert not provenance_path(tmp_path).exists()


def test_database_preflight_copy_is_immutable_and_disagreement_is_rejected(tmp_path):
    db_path = tmp_path / "mammamiradio.db"
    cold = capture_legacy_home_preflight_v1(tmp_path / "cold", database_preexisted=False)
    persist_legacy_home_database_preflight_v1(db_path, cold)

    assert load_legacy_home_database_preflight_v1(db_path) == cold
    legacy = capture_legacy_home_preflight_v1(tmp_path / "legacy", database_preexisted=True)
    with pytest.raises(RuntimeError, match="conflicts with sidecar preflight"):
        persist_legacy_home_database_preflight_v1(db_path, legacy)
    assert load_legacy_home_database_preflight_v1(db_path) == cold


@pytest.mark.parametrize(
    "schema_sql",
    [
        f"CREATE TABLE {DATABASE_ORIGIN_TABLE} (wrong_column INTEGER)",
        (f"CREATE TABLE {DATABASE_ORIGIN_TABLE} (singleton INTEGER PRIMARY KEY, database_preexisted INTEGER)"),
        (
            f"CREATE TABLE {DATABASE_ORIGIN_TABLE} "
            "(singleton INTEGER PRIMARY KEY, database_preexisted INTEGER); "
            f"INSERT INTO {DATABASE_ORIGIN_TABLE} VALUES (1, 2)"
        ),
    ],
)
def test_existing_malformed_database_origin_is_explicitly_nondurable(tmp_path, schema_sql):
    db_path = tmp_path / "mammamiradio.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(schema_sql)
        connection.commit()
    finally:
        connection.close()

    result = load_legacy_home_database_preflight_v1(db_path)

    assert result is not None
    assert result.durable is False
    assert result.database_preexisted is False


def test_corrupt_preflight_fails_closed_and_is_not_repaired(tmp_path):
    path = preflight_path(tmp_path)
    path.write_text('{"database_preexisted":"yes"}', encoding="utf-8")

    result = capture_legacy_home_preflight_v1(tmp_path, database_preexisted=True)

    assert result.database_preexisted is False
    assert result.durable is False
    assert load_legacy_home_preflight_v1(tmp_path) is None
    assert path.read_text(encoding="utf-8") == '{"database_preexisted":"yes"}'
    assert load_legacy_home_provenance_v1(tmp_path, tmp_path / "test.db") is None


def test_capture_write_failure_cleans_temp_and_does_not_claim_durability(tmp_path):
    with (
        patch("mammamiradio.home.migration.os.replace", side_effect=OSError("disk full")),
        pytest.raises(
            OSError,
            match="disk full",
        ),
    ):
        capture_legacy_home_preflight_v1(tmp_path, database_preexisted=False)

    assert not preflight_path(tmp_path).exists()
    assert list(tmp_path.glob(f".{PREFLIGHT_FILENAME}.*.tmp")) == []


@pytest.mark.parametrize(
    "payload",
    [
        "not-an-object",
        {},
        {
            "manifest_version": 1,
            "manifest_digest": "0" * 64,
            "bridge_app_version": "1.2.3",
            "observed_at": 10.0,
        },
        {
            "manifest_version": 1,
            "manifest_digest": LEGACY_HOME_MANIFEST_DIGEST,
            "bridge_app_version": "1.2.3",
            "observed_at": 10.0,
            "raw_states": {},
        },
    ],
)
def test_malformed_or_mismatched_provenance_fails_closed(tmp_path, payload):
    capture_legacy_home_preflight_v1(tmp_path, database_preexisted=True)
    db_path = _persist_database_witness(tmp_path)
    path = provenance_path(tmp_path)
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_legacy_home_provenance_v1(tmp_path, db_path) is None


def test_corrupt_existing_provenance_is_not_overwritten(tmp_path):
    capture_legacy_home_preflight_v1(tmp_path, database_preexisted=True)
    db_path = _persist_database_witness(tmp_path)
    path = provenance_path(tmp_path)
    path.write_text("{broken", encoding="utf-8")

    result = load_legacy_home_provenance_v1(tmp_path, db_path)

    assert result is None
    assert path.read_text(encoding="utf-8") == "{broken"


def test_transplanted_sidecar_and_valid_provenance_are_rejected_by_cold_database_witness(tmp_path):
    legacy_dir = tmp_path / "legacy"
    cold_dir = tmp_path / "cold"
    capture_legacy_home_preflight_v1(legacy_dir, database_preexisted=True)
    legacy_db = _persist_database_witness(legacy_dir)
    provenance_path(legacy_dir).write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "manifest_digest": LEGACY_HOME_MANIFEST_DIGEST,
                "bridge_app_version": "3.0.0",
                "observed_at": 1.0,
            }
        )
    )
    original = load_legacy_home_provenance_v1(legacy_dir, legacy_db)
    assert original is not None

    capture_legacy_home_preflight_v1(cold_dir, database_preexisted=False)
    cold_db = _persist_database_witness(cold_dir)
    preflight_path(cold_dir).write_bytes(preflight_path(legacy_dir).read_bytes())
    transplanted = provenance_path(cold_dir)
    transplanted.write_bytes(provenance_path(legacy_dir).read_bytes())

    assert load_legacy_home_provenance_v1(cold_dir, cold_db) is None
    assert load_legacy_home_provenance_v1(cold_dir, cold_db) is None
    assert transplanted.read_bytes() == provenance_path(legacy_dir).read_bytes()


def test_paths_are_directly_below_caller_provided_state_dir(tmp_path):
    assert preflight_path(tmp_path) == tmp_path / PREFLIGHT_FILENAME
    assert provenance_path(tmp_path) == tmp_path / PROVENANCE_FILENAME
    assert os.path.dirname(preflight_path(tmp_path)) == str(tmp_path)


def test_rewrite_cold_corrects_a_poisoned_true_sidecar(tmp_path):
    preflight_path(tmp_path).write_text('{"database_preexisted": true}\n', encoding="utf-8")

    corrected = rewrite_legacy_home_preflight_cold_v1(tmp_path)

    assert corrected.database_preexisted is False
    assert corrected.durable is True
    reloaded = load_legacy_home_preflight_v1(tmp_path)
    assert reloaded is not None and reloaded.database_preexisted is False


def test_rewrite_cold_corrects_a_malformed_sidecar(tmp_path):
    preflight_path(tmp_path).write_text("{ this is not valid json", encoding="utf-8")

    corrected = rewrite_legacy_home_preflight_cold_v1(tmp_path)

    assert corrected.database_preexisted is False
    reloaded = load_legacy_home_preflight_v1(tmp_path)
    assert reloaded is not None and reloaded.database_preexisted is False


def test_rewrite_cold_leaves_a_valid_cold_sidecar_untouched(tmp_path):
    capture_legacy_home_preflight_v1(tmp_path, database_preexisted=False)
    before = preflight_path(tmp_path).read_bytes()

    corrected = rewrite_legacy_home_preflight_cold_v1(tmp_path)

    assert corrected.database_preexisted is False
    assert preflight_path(tmp_path).read_bytes() == before


def test_database_witness_reads_back_through_uri_special_char_path(tmp_path):
    # A cache path containing a URI metacharacter must not truncate the filename
    # and open the wrong DB (which would strand a legacy install in narrow mode).
    weird_dir = tmp_path / "cache?with#meta chars"
    weird_dir.mkdir()
    db_path = weird_dir / "mammamiradio.db"
    persist_legacy_home_database_preflight_v1(db_path, LegacyHomePreflightV1(database_preexisted=True))

    witness = load_legacy_home_database_preflight_v1(db_path)

    assert witness is not None
    assert witness.durable is True
    assert witness.database_preexisted is True


@pytest.mark.parametrize("replace_existing", (True, False))
def test_atomic_write_failure_does_not_close_recycled_fd(tmp_path, monkeypatch, replace_existing):
    from mammamiradio.home import migration

    temp_path = tmp_path / "temp"
    fd = os.open(temp_path, os.O_CREAT | os.O_RDWR, 0o600)
    monkeypatch.setattr(migration.tempfile, "mkstemp", lambda **_: (fd, str(temp_path)))
    with (tmp_path / "other").open("wb") as other:

        def fail_publish(*_):
            os.dup2(other.fileno(), fd)
            raise FileExistsError("competing publication")

        monkeypatch.setattr(os, "replace" if replace_existing else "link", fail_publish)
        try:
            with pytest.raises(FileExistsError):
                migration._atomic_write_json(tmp_path / "profile", {}, replace_existing=replace_existing)
            assert os.fstat(fd) == os.fstat(other.fileno())
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


def test_compatibility_hashes_remain_pinned_without_compiled_household_map():
    from mammamiradio.home import migration, profile

    assert migration.LEGACY_HOME_MANIFEST_DIGEST == "72201ec2e2b10ec6d9c594cae11d5cb5a5da6e11d744229f8f2a53cdf4c6613a"
    assert profile.COMPATIBILITY_SNAPSHOT_SHA256 == "c49225bb57f04945c790c2c4883c6d6ee5edec5181761e3bce8f44f16577ecf3"
    assert not hasattr(migration, "LEGACY_HOME_MANIFEST_V1")
    assert not hasattr(profile, "export_legacy_home_profile_v1")


def test_missing_provenance_is_never_created(tmp_path):
    capture_legacy_home_preflight_v1(tmp_path, database_preexisted=True)
    db_path = _persist_database_witness(tmp_path)
    assert load_legacy_home_provenance_v1(tmp_path, db_path) is None
    assert not provenance_path(tmp_path).exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("observed_at", -1),
        ("observed_at", True),
        ("observed_at", float("inf")),
        ("bridge_app_version", ""),
        ("bridge_app_version", "a" * 81),
        ("bridge_app_version", "a\nb"),
        ("manifest_version", True),
    ],
)
def test_invalid_historical_metadata_is_rejected(tmp_path, field, value):
    capture_legacy_home_preflight_v1(tmp_path, database_preexisted=True)
    db_path = _persist_database_witness(tmp_path)
    document = {
        "manifest_version": 1,
        "manifest_digest": LEGACY_HOME_MANIFEST_DIGEST,
        "bridge_app_version": "3.0.0",
        "observed_at": 1.0,
    }
    document[field] = value
    path = provenance_path(tmp_path)
    path.write_text(json.dumps(document))
    before = path.read_bytes()
    assert load_legacy_home_provenance_v1(tmp_path, db_path) is None
    assert path.read_bytes() == before
