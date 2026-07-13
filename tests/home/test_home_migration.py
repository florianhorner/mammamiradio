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
    LEGACY_HOME_MANIFEST_V1,
    PREFLIGHT_FILENAME,
    PROVENANCE_FILENAME,
    LegacyHomePreflightV1,
    capture_legacy_home_preflight_v1,
    load_legacy_home_database_preflight_v1,
    load_legacy_home_preflight_v1,
    load_legacy_home_provenance_v1,
    observes_legacy_home_manifest_v1,
    persist_legacy_home_database_preflight_v1,
    preflight_path,
    provenance_path,
    rewrite_legacy_home_preflight_cold_v1,
    seal_legacy_home_provenance_v1,
)


def _exact_states() -> dict[str, dict[str, object]]:
    return {
        entity_id: {
            "state": f"PRIVATE RAW VALUE FOR {entity_id}",
            "attributes": {"friendly_name": f"PRIVATE LABEL FOR {entity_id}"},
        }
        for entity_id in LEGACY_HOME_MANIFEST_V1.entity_ids
    }


def _persist_database_witness(state_dir: Path) -> Path:
    preflight = load_legacy_home_preflight_v1(state_dir)
    assert preflight is not None and preflight.durable
    db_path = state_dir / "mammamiradio.db"
    persist_legacy_home_database_preflight_v1(db_path, preflight)
    return db_path


def test_manifest_is_stable_exact_and_carries_future_profile_intent():
    from mammamiradio.home.ha_context import ALL_ENTITIES

    assert LEGACY_HOME_MANIFEST_V1.version == 1
    assert len(LEGACY_HOME_MANIFEST_V1.entries) == 35
    assert len(LEGACY_HOME_MANIFEST_V1.entity_ids) == 35
    assert LEGACY_HOME_MANIFEST_V1.entity_ids == frozenset(ALL_ENTITIES)
    assert (
        LEGACY_HOME_MANIFEST_V1.entity_id_digest == "72201ec2e2b10ec6d9c594cae11d5cb5a5da6e11d744229f8f2a53cdf4c6613a"
    )

    by_id = {entry.entity_id: entry for entry in LEGACY_HOME_MANIFEST_V1.entries}
    assert by_id["weather.forecast_home"].scopes == ("ambient_context", "weather")
    assert all("resident" in by_id[entity_id].scopes for entity_id in by_id if entity_id.startswith("person."))
    assert "moment" in by_id["switch.bar_kaffeemaschine_steckdose"].scopes
    assert by_id["binary_sensor.buro_9_ring_intercom_klingelt"].priority == "bronze"


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
    sealed = seal_legacy_home_provenance_v1(
        tmp_path,
        _exact_states(),
        db_path=db_path,
        bridge_app_version="1.2.3",
        observed_at=100.0,
    )

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
    assert (
        seal_legacy_home_provenance_v1(
            tmp_path,
            _exact_states(),
            db_path=tmp_path / "mammamiradio.db",
            bridge_app_version="1.2.3",
        )
        is None
    )


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


def test_observation_requires_every_exact_id_but_ignores_unrelated_entities():
    exact = _exact_states()
    missing = dict(exact)
    missing.pop("weather.forecast_home")

    assert observes_legacy_home_manifest_v1(missing) is False
    assert observes_legacy_home_manifest_v1(exact | {"light.some_other_home": {}}) is True
    assert observes_legacy_home_manifest_v1(LEGACY_HOME_MANIFEST_V1.entity_ids) is True
    assert observes_legacy_home_manifest_v1("weather.forecast_home") is False


def test_preexisting_exact_home_seals_metadata_only_provenance(tmp_path):
    capture_legacy_home_preflight_v1(tmp_path, database_preexisted=True)
    db_path = _persist_database_witness(tmp_path)

    provenance = seal_legacy_home_provenance_v1(
        tmp_path,
        _exact_states(),
        db_path=db_path,
        bridge_app_version=" 1.2.3 ",
        observed_at=1234.5,
    )

    assert provenance is not None
    assert provenance.bridge_app_version == "1.2.3"
    assert provenance.observed_at == 1234.5
    assert load_legacy_home_provenance_v1(tmp_path, db_path) == provenance
    persisted = json.loads(provenance_path(tmp_path).read_text(encoding="utf-8"))
    assert set(persisted) == {
        "manifest_version",
        "manifest_digest",
        "bridge_app_version",
        "observed_at",
    }
    assert persisted == provenance.to_dict()
    serialized = provenance_path(tmp_path).read_text(encoding="utf-8")
    assert "PRIVATE RAW VALUE" not in serialized
    assert "PRIVATE LABEL" not in serialized
    assert "entity_id" not in serialized
    assert "prior_app_version" not in serialized
    assert provenance_path(tmp_path).stat().st_mode & 0o777 == 0o600


def test_incomplete_observation_writes_nothing(tmp_path):
    capture_legacy_home_preflight_v1(tmp_path, database_preexisted=True)
    db_path = _persist_database_witness(tmp_path)
    states = _exact_states()
    states.pop(next(iter(LEGACY_HOME_MANIFEST_V1.entity_ids)))

    assert (
        seal_legacy_home_provenance_v1(
            tmp_path,
            states,
            db_path=db_path,
            bridge_app_version="1.2.3",
            observed_at=123.0,
        )
        is None
    )
    assert not provenance_path(tmp_path).exists()


def test_seal_is_idempotent_and_preserves_first_bridge_observation(tmp_path):
    capture_legacy_home_preflight_v1(tmp_path, database_preexisted=True)
    db_path = _persist_database_witness(tmp_path)
    first = seal_legacy_home_provenance_v1(
        tmp_path,
        _exact_states(),
        db_path=db_path,
        bridge_app_version="1.2.3",
        observed_at=100.0,
    )
    second = seal_legacy_home_provenance_v1(
        tmp_path,
        {},
        db_path=db_path,
        bridge_app_version="9.9.9",
        observed_at=999.0,
    )

    assert second == first
    assert second is not None
    assert second.bridge_app_version == "1.2.3"
    assert second.observed_at == 100.0


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
            "manifest_digest": LEGACY_HOME_MANIFEST_V1.entity_id_digest,
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

    result = seal_legacy_home_provenance_v1(
        tmp_path,
        _exact_states(),
        db_path=db_path,
        bridge_app_version="1.2.3",
    )

    assert result is None
    assert path.read_text(encoding="utf-8") == "{broken"


def test_transplanted_sidecar_and_valid_provenance_are_rejected_by_cold_database_witness(tmp_path):
    legacy_dir = tmp_path / "legacy"
    cold_dir = tmp_path / "cold"
    capture_legacy_home_preflight_v1(legacy_dir, database_preexisted=True)
    legacy_db = _persist_database_witness(legacy_dir)
    original = seal_legacy_home_provenance_v1(
        legacy_dir,
        _exact_states(),
        db_path=legacy_db,
        bridge_app_version="1.2.3",
        observed_at=100.0,
    )
    assert original is not None

    capture_legacy_home_preflight_v1(cold_dir, database_preexisted=False)
    cold_db = _persist_database_witness(cold_dir)
    preflight_path(cold_dir).write_bytes(preflight_path(legacy_dir).read_bytes())
    transplanted = provenance_path(cold_dir)
    transplanted.write_bytes(provenance_path(legacy_dir).read_bytes())

    assert load_legacy_home_provenance_v1(cold_dir, cold_db) is None
    assert (
        seal_legacy_home_provenance_v1(
            cold_dir,
            _exact_states(),
            db_path=cold_db,
            bridge_app_version="9.9.9",
        )
        is None
    )
    assert transplanted.read_bytes() == provenance_path(legacy_dir).read_bytes()


def test_provenance_replace_failure_cleans_unique_temp_file(tmp_path):
    capture_legacy_home_preflight_v1(tmp_path, database_preexisted=True)
    db_path = _persist_database_witness(tmp_path)

    with (
        patch("mammamiradio.home.migration.os.replace", side_effect=OSError("disk full")),
        pytest.raises(
            OSError,
            match="disk full",
        ),
    ):
        seal_legacy_home_provenance_v1(
            tmp_path,
            _exact_states(),
            db_path=db_path,
            bridge_app_version="1.2.3",
        )

    assert not provenance_path(tmp_path).exists()
    assert list(tmp_path.glob(f".{PROVENANCE_FILENAME}.*.tmp")) == []


@pytest.mark.parametrize("bad_value", [1, None, "", "\n1.2.3", "x" * 81])
def test_bridge_app_version_must_be_explicit_and_bounded(tmp_path, bad_value):
    capture_legacy_home_preflight_v1(tmp_path, database_preexisted=True)
    db_path = _persist_database_witness(tmp_path)
    with pytest.raises(ValueError, match="bridge_app_version"):
        seal_legacy_home_provenance_v1(
            tmp_path,
            _exact_states(),
            db_path=db_path,
            bridge_app_version=bad_value,  # type: ignore[arg-type]
        )


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
