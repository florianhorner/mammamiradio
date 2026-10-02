"""Synthetic private Home fixtures. Production has no demonstration override."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from mammamiradio.home import migration, profile
from mammamiradio.home.bindings import HomeBindings


def synthetic_home_document(prefix="example", profile_id="a" * 32):
    source = (Path(__file__).parent / "fixtures/home_profile.json").read_text()
    document = json.loads(source.replace(".example_", f".{prefix}_"))
    document["profile_id"] = profile_id
    return document


def allow_synthetic_snapshot(monkeypatch, document):
    snapshot = dict(document)
    snapshot.pop("profile_id")
    monkeypatch.setattr(
        profile, "COMPATIBILITY_SNAPSHOT_SHA256", hashlib.sha256(profile._canonical(snapshot).encode()).hexdigest()
    )


def write_compatibility_install(state_dir, db_path, document, *, binding="pending", write_profile=True):
    witness = migration.capture_legacy_home_preflight_v1(state_dir, database_preexisted=True)
    migration.persist_legacy_home_database_preflight_v1(db_path, witness)
    migration._atomic_write_json(
        migration.provenance_path(state_dir),
        {
            "manifest_version": 1,
            "manifest_digest": migration.LEGACY_HOME_MANIFEST_DIGEST,
            "bridge_app_version": "3.0.0",
            "observed_at": 1.0,
        },
    )
    canonical = profile._canonical(document)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    path = state_dir / profile.PROFILE_FILENAME
    if write_profile:
        path.write_text(canonical)
        os.chmod(path, 0o600)
    if binding is not None:
        connection = sqlite3.connect(db_path)
        try:
            with connection:
                connection.execute(
                    f"CREATE TABLE {profile.PROFILE_BINDING_TABLE} ("
                    "singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                    "profile_id TEXT NOT NULL, content_digest TEXT)"
                )
                connection.execute(
                    f"INSERT INTO {profile.PROFILE_BINDING_TABLE} VALUES(1, ?, ?)",
                    (document["profile_id"], digest if binding == "complete" else None),
                )
        finally:
            connection.close()
    return path, digest


SYNTHETIC_DOCUMENT = synthetic_home_document()
SYNTHETIC_BINDINGS = HomeBindings.from_document(SYNTHETIC_DOCUMENT, identity="synthetic-example")
ENTITY_LABELS = SYNTHETIC_BINDINGS.labels_it
ENTITY_LABELS_EN = SYNTHETIC_BINDINGS.labels_en
GOLD_ENTITIES = list(SYNTHETIC_BINDINGS.tier("gold"))
SILVER_ENTITIES = list(SYNTHETIC_BINDINGS.tier("silver"))
BRONZE_ENTITIES = list(SYNTHETIC_BINDINGS.tier("bronze"))
ALL_ENTITIES = [row.entity_id for row in SYNTHETIC_BINDINGS.entities]
REACTIVE_TRIGGERS = list(SYNTHETIC_BINDINGS.reactive_rows)
THRESHOLD_TRIGGERS = list(SYNTHETIC_BINDINGS.threshold_rows)

CURATED_COFFEE_ENTITY_IDS = frozenset(SYNTHETIC_BINDINGS.coffee_entities)
