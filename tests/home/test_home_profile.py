"""Consume historical profile bytes; never reconstruct missing private evidence."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import pytest

from mammamiradio.home import migration, profile
from mammamiradio.home.bindings import HomeBindings
from tests.home_fixtures import allow_synthetic_snapshot, synthetic_home_document, write_compatibility_install


@pytest.fixture(params=("example_a", "example_b"))
def document(monkeypatch, request):
    result = synthetic_home_document(request.param)
    allow_synthetic_snapshot(monkeypatch, result)
    return result


@pytest.fixture
def eligible(tmp_path, document):
    state_dir = tmp_path / "cache ?#" / "state"
    db_path = state_dir.parent / "station ?#.db"
    write_compatibility_install(state_dir, db_path, document)
    return state_dir, db_path


def _binding(db_path):
    connection = sqlite3.connect(db_path)
    try:
        return profile._binding(connection)
    finally:
        connection.close()


def test_unchanged_published_file_resumes_and_is_immutable(eligible, document):
    state_dir, db_path = eligible
    path = state_dir / profile.PROFILE_FILENAME
    before = path.read_bytes()
    assert profile.load_home_profile_v1(*eligible) is None
    result = profile.resume_home_profile_v1(*eligible)
    assert result is not None
    assert profile.load_home_profile_v1(*eligible) == result
    assert profile.resume_home_profile_v1(*eligible) == result
    assert path.read_bytes() == before
    assert path.stat().st_mode & 0o777 == 0o600
    assert result.to_dict() == document
    assert result.content_digest == hashlib.sha256(profile._canonical(document).encode()).hexdigest()
    assert _binding(db_path) == (result.profile_id, result.content_digest)
    bindings = HomeBindings.from_document(result.to_dict(), identity=result.content_digest)
    assert len(bindings.entities) == 35
    assert list(bindings.reactive_rows) == [
        tuple(row[key] for key in ("entity_id", "state", "directive", "cooldown"))
        for row in document["reactive_triggers"]
    ]
    assert bindings.resident_returns == tuple((r["entity_id"], r["alias"]) for r in document["resident_returns"])
    copy = result.to_dict()
    copy["entities"].clear()
    assert len(result.to_dict()["entities"]) == 35
    with pytest.raises(FrozenInstanceError):
        result.profile_id = "changed"


@pytest.mark.parametrize(
    "damage", ["sidecar_missing", "sidecar_corrupt", "seal_missing", "seal_corrupt", "cold", "lost_db"]
)
def test_ineligible_evidence_remains_closed_without_repair(eligible, damage):
    state_dir, db_path = eligible
    profile_path = state_dir / profile.PROFILE_FILENAME
    before = profile_path.read_bytes()
    if damage == "lost_db":
        db_path.unlink()
    elif damage == "cold":
        with sqlite3.connect(db_path) as connection:
            connection.execute(f"UPDATE {migration.DATABASE_ORIGIN_TABLE} SET database_preexisted=0")
    else:
        path = (
            migration.preflight_path(state_dir)
            if damage.startswith("sidecar")
            else migration.provenance_path(state_dir)
        )
        if damage.endswith("missing"):
            path.unlink()
        else:
            path.write_text("{}")
    for _ in range(2):
        assert profile.resume_home_profile_v1(*eligible) is None
        assert profile.load_home_profile_v1(*eligible) is None
        assert profile_path.read_bytes() == before
    if damage == "lost_db":
        assert not db_path.exists()


@pytest.mark.parametrize(
    "field",
    [
        "entities",
        "reactive_triggers",
        "threshold_triggers",
        "resident_returns",
        "empty_home_residents",
        "ambient_labels",
        "director_coffee_entities",
        "manifest_digest",
        "schema_version",
    ],
)
def test_any_changed_snapshot_field_is_rejected_without_repair(eligible, document, field):
    state_dir, _ = eligible
    path = state_dir / profile.PROFILE_FILENAME
    document[field] = [] if isinstance(document[field], list) else "changed"
    path.write_text(json.dumps(document))
    before = path.read_bytes()
    assert profile.resume_home_profile_v1(*eligible) is None
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "damage", ["missing", "corrupt", "oversized", "deep", "permission", "symlink", "directory", "foreign_id"]
)
def test_unsafe_files_cannot_complete_binding(eligible, document, damage):
    state_dir, db_path = eligible
    path = state_dir / profile.PROFILE_FILENAME
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_text("{broken")
    elif damage == "oversized":
        path.write_bytes(b" " * (profile._MAX_PROFILE_BYTES + 1))
    elif damage == "deep":
        path.write_text("[" * 2000 + "]" * 2000)
    elif damage == "permission":
        path.chmod(0o644)
    elif damage == "symlink":
        target = path.with_suffix(".backup")
        path.rename(target)
        path.symlink_to(target)
    elif damage == "directory":
        path.unlink()
        path.mkdir()
    else:
        document["profile_id"] = "b" * 32
        path.write_text(json.dumps(document))
    before = _binding(db_path)
    assert profile.resume_home_profile_v1(*eligible) is None
    assert _binding(db_path) == before
    if damage == "missing":
        assert not path.exists()


@pytest.mark.parametrize("binding", [None, "pending"])
@pytest.mark.parametrize("write_profile", [False, True])
def test_no_export_or_intent_is_created(tmp_path, document, binding, write_profile):
    state_dir, db_path = tmp_path / "state", tmp_path / "test.db"
    path, _ = write_compatibility_install(state_dir, db_path, document, binding=binding, write_profile=write_profile)
    before = path.read_bytes() if path.exists() else None
    result = profile.resume_home_profile_v1(state_dir, db_path)
    assert (result is not None) is (binding == "pending" and write_profile)
    assert (path.read_bytes() if path.exists() else None) == before
    if binding is None:
        assert _binding(db_path) is None


@pytest.mark.parametrize(
    "sql",
    ["DELETE FROM {table}", "UPDATE {table} SET profile_id='foreign'", "UPDATE {table} SET content_digest='conflict'"],
)
def test_conflicting_binding_is_never_repaired(eligible, sql):
    state_dir, db_path = eligible
    with sqlite3.connect(db_path) as connection:
        connection.execute(sql.format(table=profile.PROFILE_BINDING_TABLE))
    before = db_path.read_bytes()
    assert profile.resume_home_profile_v1(state_dir, db_path) is None
    assert db_path.read_bytes() == before


def test_transplant_rejected_but_complete_backup_restores(eligible, tmp_path, document):
    state_dir, db_path = eligible
    result = profile.resume_home_profile_v1(*eligible)
    target_state, target_db = tmp_path / "other/state", tmp_path / "other/test.db"
    foreign = dict(document, profile_id="b" * 32)
    write_compatibility_install(target_state, target_db, foreign)
    shutil.copyfile(state_dir / profile.PROFILE_FILENAME, target_state / profile.PROFILE_FILENAME)
    assert profile.resume_home_profile_v1(target_state, target_db) is None
    shutil.copyfile(db_path, target_db)
    assert profile.load_home_profile_v1(target_state, target_db) == result


@pytest.mark.parametrize("failure_call", [1, 2])
def test_fsync_failure_leaves_pending_evidence_retryable(eligible, monkeypatch, failure_call):
    state_dir, db_path = eligible
    before = (state_dir / profile.PROFILE_FILENAME).read_bytes()
    real_fsync = os.fsync
    count = 0

    def fail(fd):
        nonlocal count
        count += 1
        if count == failure_call:
            raise OSError("synthetic fsync failure")
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(profile.os, "fsync", fail)
        assert profile.resume_home_profile_v1(*eligible) is None
    assert _binding(db_path)[1] is None
    assert (state_dir / profile.PROFILE_FILENAME).read_bytes() == before
    assert profile.resume_home_profile_v1(*eligible) is not None


def test_replaced_file_cannot_be_bound(eligible, document, monkeypatch):
    state_dir, db_path = eligible
    path = state_dir / profile.PROFILE_FILENAME
    real_fsync = os.fsync
    replaced = False

    def replace_during_sync(fd):
        nonlocal replaced
        real_fsync(fd)
        if not replaced:
            replaced = True
            replacement = path.with_suffix(".replacement")
            replacement.write_text(json.dumps(document))
            replacement.chmod(0o600)
            replacement.replace(path)

    monkeypatch.setattr(profile.os, "fsync", replace_during_sync)
    assert profile.resume_home_profile_v1(*eligible) is None
    assert _binding(db_path)[1] is None


def test_concurrent_resumes_complete_one_binding(eligible):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: profile.resume_home_profile_v1(*eligible), range(8)))
    assert results[0] is not None
    assert all(result == results[0] for result in results)


def test_production_rejects_synthetic_profile_without_test_hash_override():
    with pytest.raises(ValueError, match="compatibility snapshot"):
        profile._profile(synthetic_home_document())


def test_foreign_file_owner_is_rejected(eligible, monkeypatch):
    monkeypatch.setattr(profile.os, "geteuid", lambda: os.stat(eligible[0]).st_uid + 1)
    assert profile.resume_home_profile_v1(*eligible) is None


@pytest.mark.parametrize("damage", ["view", "float"])
def test_binding_schema_and_singleton_types_are_strict(eligible, document, damage):
    _, db_path = eligible
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"DROP TABLE {profile.PROFILE_BINDING_TABLE}")
        if damage == "view":
            connection.execute(
                f"CREATE VIEW {profile.PROFILE_BINDING_TABLE} AS SELECT 1 AS singleton, "
                f"'{document['profile_id']}' AS profile_id, NULL AS content_digest"
            )
        else:
            connection.execute(f"CREATE TABLE {profile.PROFILE_BINDING_TABLE} (singleton, profile_id, content_digest)")
            connection.execute(
                f"INSERT INTO {profile.PROFILE_BINDING_TABLE} VALUES (1.0, ?, NULL)", (document["profile_id"],)
            )
    assert profile.resume_home_profile_v1(*eligible) is None
