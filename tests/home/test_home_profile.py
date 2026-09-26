"""Update-N private snapshot and crash recovery, using synthetic home IDs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace

import pytest

from mammamiradio.core import listener_truth
from mammamiradio.home import catalog, context_director, ha_context, migration, profile


@pytest.fixture(autouse=True, params=("example", "sample"))
def synthetic_home(monkeypatch, request):
    manifest = migration.LEGACY_HOME_MANIFEST_V1
    remap = {
        entry.entity_id: f"{entry.entity_id.split('.')[0]}.{request.param}_{entry.role}" for entry in manifest.entries
    }
    entries = tuple(replace(entry, entity_id=remap[entry.entity_id]) for entry in manifest.entries)
    monkeypatch.setattr(migration, "LEGACY_HOME_MANIFEST_V1", replace(manifest, entries=entries))
    for name in ("GOLD_ENTITIES", "SILVER_ENTITIES", "BRONZE_ENTITIES", "ALL_ENTITIES"):
        monkeypatch.setattr(ha_context, name, [remap[entity_id] for entity_id in getattr(ha_context, name)])
    for name in ("ENTITY_LABELS", "ENTITY_LABELS_EN"):
        monkeypatch.setattr(
            catalog, name, {remap.get(key, key): value for key, value in getattr(catalog, name).items()}
        )
    monkeypatch.setattr(
        ha_context,
        "REACTIVE_TRIGGERS",
        [
            (remap[entity_id], state, directive, cooldown)
            for entity_id, state, directive, cooldown in ha_context.REACTIVE_TRIGGERS
        ],
    )
    monkeypatch.setattr(
        ha_context,
        "THRESHOLD_TRIGGERS",
        [{**row, "entity_id": remap[row["entity_id"]]} for row in ha_context.THRESHOLD_TRIGGERS],
    )
    monkeypatch.setattr(
        listener_truth,
        "_AUTHORIZED_HOME_RETURN_SOURCES",
        {
            f"ha:{remap[source.removeprefix('ha:')]}": alias
            for source, alias in listener_truth._AUTHORIZED_HOME_RETURN_SOURCES.items()
        },
    )
    monkeypatch.setattr(
        context_director,
        "CURATED_COFFEE_ENTITY_IDS",
        frozenset(remap[entity_id] for entity_id in context_director.CURATED_COFFEE_ENTITY_IDS),
    )


@pytest.fixture
def eligible(tmp_path):
    state_dir = tmp_path / "cache ?#" / "state"
    db_path = state_dir.parent / "station ?#.db"
    witness = migration.capture_legacy_home_preflight_v1(state_dir, database_preexisted=True)
    migration.persist_legacy_home_database_preflight_v1(db_path, witness)
    assert (
        migration.seal_legacy_home_provenance_v1(
            state_dir, migration.LEGACY_HOME_MANIFEST_V1.entity_ids, db_path=db_path, bridge_app_version="0+test"
        )
        is not None
    )
    return state_dir, db_path


def _binding(db_path):
    with sqlite3.connect(db_path) as connection:
        return profile._binding(connection)


def test_round_trip_preserves_every_compiled_binding_and_is_immutable(eligible):
    state_dir, db_path = eligible
    result = profile.export_legacy_home_profile_v1(*eligible)
    assert result is not None
    assert profile.load_home_profile_v1(*eligible) == result
    assert profile.export_legacy_home_profile_v1(*eligible) == result
    path = state_dir / profile.PROFILE_FILENAME
    assert path.stat().st_mode & 0o777 == 0o600
    document = result.to_dict()
    assert document == json.loads(path.read_text())
    assert result.content_digest == hashlib.sha256(profile._canonical(document).encode()).hexdigest()
    assert _binding(db_path) == (result.profile_id, result.content_digest)
    assert [row["entity_id"] for row in document["entities"]] == ha_context.ALL_ENTITIES
    for entry, row in zip(migration.LEGACY_HOME_MANIFEST_V1.entries, document["entities"], strict=True):
        assert row == {
            "entity_id": entry.entity_id,
            "role": entry.role,
            "priority": entry.priority,
            "scopes": list(entry.scopes),
            "label_it": catalog.ENTITY_LABELS[entry.entity_id],
            "label_en": catalog.ENTITY_LABELS_EN[entry.entity_id],
        }
    assert [
        tuple(row[key] for key in ("entity_id", "state", "directive", "cooldown"))
        for row in document["reactive_triggers"]
    ] == ha_context.REACTIVE_TRIGGERS
    assert document["threshold_triggers"] == ha_context.THRESHOLD_TRIGGERS
    residents = document["empty_home_residents"]
    assert len(residents) == 2
    assert all(row["entity_id"] not in residents for row in document["entities"] if row["role"] == "pet")
    assert {
        f"ha:{row['entity_id']}": row["alias"] for row in document["resident_returns"]
    } == listener_truth._AUTHORIZED_HOME_RETURN_SOURCES
    assert set(document["director_coffee_entities"]) == context_director.CURATED_COFFEE_ENTITY_IDS
    assert {row["entity_id"] for row in document["ambient_labels"]} == {"sun.ambient", "weather.ambient"}
    assert "attributes" not in path.read_text() and "friendly_name" not in path.read_text()
    document["entities"].clear()
    assert len(result.to_dict()["entities"]) == 35
    with pytest.raises(FrozenInstanceError):
        result.profile_id = "changed"


@pytest.mark.parametrize(
    "damage", ("sidecar_missing", "sidecar_corrupt", "seal_missing", "seal_corrupt", "cold", "lost_db")
)
def test_ineligible_evidence_never_exports_or_promotes_on_later_boot(eligible, damage):
    state_dir, db_path = eligible
    if damage == "lost_db":
        db_path.unlink()
    elif damage == "cold":
        with sqlite3.connect(db_path) as connection:
            connection.execute(f"UPDATE {migration.DATABASE_ORIGIN_TABLE} SET database_preexisted = 0")
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
        assert profile.export_legacy_home_profile_v1(*eligible) is None
        assert profile.load_home_profile_v1(*eligible) is None
        assert not (state_dir / profile.PROFILE_FILENAME).exists()
    if damage == "lost_db":
        assert not db_path.exists()


@pytest.mark.parametrize(
    "field",
    (
        "entities",
        "reactive_triggers",
        "threshold_triggers",
        "resident_returns",
        "empty_home_residents",
        "ambient_labels",
        "director_coffee_entities",
        "profile_id",
        "schema_version",
        "manifest_digest",
        "extra",
    ),
)
def test_any_changed_profile_field_is_rejected_without_repair(eligible, field):
    state_dir, db_path = eligible
    result = profile.export_legacy_home_profile_v1(*eligible)
    document = result.to_dict()
    document[field] = [] if isinstance(document.get(field), list) else "changed"
    path = state_dir / profile.PROFILE_FILENAME
    path.write_text(json.dumps(document))
    damaged = path.read_bytes()
    assert profile.load_home_profile_v1(*eligible) is None
    assert profile.export_legacy_home_profile_v1(*eligible) is None
    assert path.read_bytes() == damaged
    assert _binding(db_path) == (result.profile_id, result.content_digest)


@pytest.mark.parametrize("damage", ("json", "array", "nested", "large", "mode", "symlink", "directory"))
def test_unsafe_or_malformed_orphan_is_not_overwritten(eligible, damage):
    state_dir, db_path = eligible
    profile.export_legacy_home_profile_v1(*eligible)
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"UPDATE {profile.PROFILE_BINDING_TABLE} SET content_digest = NULL")
    pending = _binding(db_path)
    path = state_dir / profile.PROFILE_FILENAME
    path.write_text("{" if damage == "json" else "[]")
    path.chmod(0o600)
    if damage == "large":
        path.write_bytes(b" " * (profile._MAX_PROFILE_BYTES + 1))
    elif damage == "nested":
        path.write_text("[" * 2000 + "]" * 2000)
    elif damage == "mode":
        path.write_text(json.dumps(profile._legacy_document(pending[0])))
        path.chmod(0o644)
    elif damage in {"symlink", "directory"}:
        path.unlink()
        if damage == "symlink":
            target = state_dir / "detached-profile.json"
            target.write_text(json.dumps(profile._legacy_document(pending[0])))
            target.chmod(0o600)
            path.symlink_to(target)
        else:
            path.mkdir()
    assert profile.export_legacy_home_profile_v1(*eligible) is None
    assert profile.load_home_profile_v1(*eligible) is None
    assert _binding(db_path) == pending
    assert path.exists()


@pytest.mark.parametrize("failure", ("file_fsync", "directory_fsync", "binding"))
def test_interrupted_export_retries_only_exact_orphan_and_syncs_before_binding(eligible, monkeypatch, failure):
    state_dir, db_path = eligible
    original_sync, original_binding = os.fsync, profile._binding
    syncs = 0

    def fail_sync(fd):
        nonlocal syncs
        syncs += 1
        if syncs == (1 if failure == "file_fsync" else 2):
            raise OSError("synthetic disk failure")
        original_sync(fd)

    def fail_binding(connection):
        result = original_binding(connection)
        if result is not None:
            raise sqlite3.DatabaseError("synthetic commit failure")
        return result

    with monkeypatch.context() as patch:
        if failure == "binding":
            patch.setattr(profile, "_binding", fail_binding)
        else:
            patch.setattr(os, "fsync", fail_sync)
        assert profile.export_legacy_home_profile_v1(*eligible) is None
    pending = _binding(db_path)
    assert pending is not None and pending[1] is None
    path = state_dir / profile.PROFILE_FILENAME
    orphan = path.read_bytes() if path.exists() else None
    assert not list(state_dir.glob(".*.tmp"))
    with monkeypatch.context() as patch:
        syncs = 0

        def record_sync(fd):
            nonlocal syncs
            syncs += 1
            original_sync(fd)

        def assert_binding_order(connection):
            result = original_binding(connection)
            if result is not None and result[1] is not None:
                assert syncs >= 2
            return result

        patch.setattr(os, "fsync", record_sync)
        patch.setattr(profile, "_binding", assert_binding_order)
        assert profile.export_legacy_home_profile_v1(*eligible) is not None
    if orphan is not None:
        assert path.read_bytes() == orphan


def test_transplant_rejected_but_matching_complete_backup_restores(eligible, tmp_path):
    state_dir, db_path = eligible
    original = profile.export_legacy_home_profile_v1(*eligible)
    backup = tmp_path / "backup"
    shutil.copytree(state_dir.parent, backup)
    restored = (backup / "state", backup / db_path.name)
    assert profile.load_home_profile_v1(*restored) == original
    path = state_dir / profile.PROFILE_FILENAME
    document = original.to_dict()
    document["profile_id"] = "b" * 32
    path.write_text(json.dumps(document))
    assert profile.export_legacy_home_profile_v1(*eligible) is None
    db_path.unlink()
    assert profile.load_home_profile_v1(*eligible) is None
    assert profile.export_legacy_home_profile_v1(*eligible) is None
    assert not db_path.exists()
    assert migration.load_legacy_home_provenance_v1(*restored) is not None  # N rollback still recognizes the seal.


@pytest.mark.parametrize(
    "sql",
    (
        "DELETE FROM {table}",
        "UPDATE {table} SET content_digest = 'different'",
        "DROP TABLE {table}; CREATE TABLE {table} (wrong TEXT)",
    ),
)
def test_conflicting_database_binding_is_never_repaired(eligible, sql):
    _, db_path = eligible
    profile.export_legacy_home_profile_v1(*eligible)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(sql.format(table=profile.PROFILE_BINDING_TABLE))
    before = db_path.read_bytes()
    assert profile.export_legacy_home_profile_v1(*eligible) is None
    assert profile.load_home_profile_v1(*eligible) is None
    assert db_path.read_bytes() == before


def test_duplicate_exporters_share_one_profile(eligible):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: profile.export_legacy_home_profile_v1(*eligible), range(2)))
    assert results[0] is not None and results[0] == results[1]


def test_publication_race_validates_winner_without_overwriting(eligible, monkeypatch):
    real_write = migration._atomic_write_json

    def competing_write(path, payload, **kwargs):
        real_write(path, payload, **kwargs)
        real_write(path, payload, **kwargs)  # No-replace publication must lose.

    monkeypatch.setattr(migration, "_atomic_write_json", competing_write)
    result = profile.export_legacy_home_profile_v1(*eligible)
    assert result is not None and result == profile.load_home_profile_v1(*eligible)


@pytest.mark.parametrize("target_status", ("absent", "pending", "bound"))
def test_foreign_profile_never_adopted_by_another_eligible_database(eligible, tmp_path, target_status):
    source_state, _ = eligible
    donor = profile.export_legacy_home_profile_v1(*eligible)
    target_state, target_db = tmp_path / "target" / "state", tmp_path / "target" / "station.db"
    witness = migration.capture_legacy_home_preflight_v1(target_state, database_preexisted=True)
    migration.persist_legacy_home_database_preflight_v1(target_db, witness)
    migration.seal_legacy_home_provenance_v1(
        target_state, migration.LEGACY_HOME_MANIFEST_V1.entity_ids, db_path=target_db, bridge_app_version="0+test"
    )
    if target_status != "absent":
        own = profile.export_legacy_home_profile_v1(target_state, target_db)
        assert own is not None and own.profile_id != donor.profile_id
        if target_status == "pending":
            with sqlite3.connect(target_db) as connection:
                connection.execute(f"UPDATE {profile.PROFILE_BINDING_TABLE} SET content_digest = NULL")
    before = target_db.read_bytes()
    shutil.copy2(source_state / profile.PROFILE_FILENAME, target_state / profile.PROFILE_FILENAME)
    assert profile.export_legacy_home_profile_v1(target_state, target_db) is None
    assert profile.load_home_profile_v1(target_state, target_db) is None
    assert target_db.read_bytes() == before
    assert (target_state / profile.PROFILE_FILENAME).read_bytes() == (
        source_state / profile.PROFILE_FILENAME
    ).read_bytes()


@pytest.mark.parametrize("drift", ("order", "priority", "residents", "label"))
def test_compiled_configuration_drift_cannot_create_incomplete_snapshot(eligible, monkeypatch, drift):
    if drift == "order":
        monkeypatch.setattr(ha_context, "GOLD_ENTITIES", list(reversed(ha_context.GOLD_ENTITIES)))
    elif drift == "priority":
        manifest = migration.LEGACY_HOME_MANIFEST_V1
        entries = (replace(manifest.entries[0], priority="bronze"), *manifest.entries[1:])
        monkeypatch.setattr(migration, "LEGACY_HOME_MANIFEST_V1", replace(manifest, entries=entries))
    elif drift == "residents":
        monkeypatch.setattr(listener_truth, "_AUTHORIZED_HOME_RETURN_SOURCES", {})
    else:
        monkeypatch.setattr(catalog, "ENTITY_LABELS", {})
    assert profile.export_legacy_home_profile_v1(*eligible) is None
