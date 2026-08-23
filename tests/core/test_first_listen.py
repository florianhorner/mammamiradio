"""Focused contracts for First Listen receipts and feature-era install origin."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import threading
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from mammamiradio.core import first_listen
from mammamiradio.core.first_listen import (
    FIRST_LISTEN_ORIGIN_TABLE,
    FirstListenAttemptMismatchError,
    FirstListenInstallOriginStatus,
    FirstListenInstallOriginV1,
    FirstListenOriginSentinelState,
    FirstListenReceiptLoadStatus,
    FirstListenReceiptStore,
    FirstListenReceiptUnavailableError,
    capture_first_listen_install_origin,
    first_listen_origin_path,
    first_listen_receipt_path,
    load_first_listen_install_origin,
    load_first_listen_receipt,
    load_first_listen_receipt_result,
    migrate_first_listen_install_origin,
)
from mammamiradio.core.sync import init_db


def _valid_receipt(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "selected_entity_id": "media_player.kitchen",
        "accepted_attempt_id": "opaque_attempt_token_1234",
        "accepted_at": 100.0,
        "heard_at": 110.0,
        "privacy_reviewed_at": 120.0,
    }
    payload.update(overrides)
    return payload


def _origin_rows(db_path: Path) -> list[tuple[int, int, int]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            f"SELECT singleton, schema_version, database_preexisted FROM {FIRST_LISTEN_ORIGIN_TABLE}"
        ).fetchall()


def test_receipt_load_is_bounded_strict_and_corrupt_tolerant(tmp_path):
    path = first_listen_receipt_path(tmp_path)
    assert load_first_listen_receipt(path, 200.0) is None

    invalid_payloads = [
        [],
        _valid_receipt(schema_version=2),
        _valid_receipt(schema_version=True),
        _valid_receipt(schema_version=1.0),
        {**_valid_receipt(), "extra": True},
        _valid_receipt(selected_entity_id="light.kitchen"),
        _valid_receipt(accepted_attempt_id="short"),
        _valid_receipt(accepted_at=None),
        _valid_receipt(heard_at=99.0),
        _valid_receipt(heard_at=float("nan")),
        _valid_receipt(privacy_reviewed_at=True),
        _valid_receipt(accepted_at=501.0, heard_at=501.0),
    ]
    path.parent.mkdir(parents=True)
    for payload in invalid_payloads:
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert load_first_listen_receipt(path, 200.0) is None

    path.write_bytes(b"{" + b"x" * 4096)
    assert load_first_listen_receipt(path, 200.0) is None


def test_receipt_load_accepts_listener_and_existing_ha_proof(tmp_path):
    path = first_listen_receipt_path(tmp_path)
    path.parent.mkdir(parents=True)

    listener_payload = _valid_receipt(
        selected_entity_id=None,
        accepted_attempt_id="listener_opaque_attempt_token_1234",
        accepted_at=110.0,
        heard_at=110.0,
    )
    path.write_text(json.dumps(listener_payload), encoding="utf-8")
    listener = load_first_listen_receipt(path, 200.0)

    assert listener is not None
    assert listener.selected_entity_id is None
    assert listener.audio_complete is True
    assert listener.to_dict() == listener_payload

    path.write_text(json.dumps(_valid_receipt()), encoding="utf-8")
    ha_receipt = load_first_listen_receipt(path, 200.0)

    assert ha_receipt is not None
    assert ha_receipt.selected_entity_id == "media_player.kitchen"
    assert ha_receipt.heard_at == 110.0


@pytest.mark.parametrize("privacy_reviewed_at", [None, 120.0])
def test_receipt_load_keeps_empty_and_privacy_only_v1_shapes_valid(tmp_path, privacy_reviewed_at):
    path = first_listen_receipt_path(tmp_path)
    path.parent.mkdir(parents=True)
    payload = _valid_receipt(
        selected_entity_id=None,
        accepted_attempt_id=None,
        accepted_at=None,
        heard_at=None,
        privacy_reviewed_at=privacy_reviewed_at,
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    receipt = load_first_listen_receipt(path, 200.0)

    assert receipt is not None
    assert receipt.to_dict() == payload


@pytest.mark.parametrize(
    "payload",
    [
        _valid_receipt(selected_entity_id=None),
        _valid_receipt(
            accepted_attempt_id=None,
            accepted_at=None,
            heard_at=None,
        ),
        _valid_receipt(
            selected_entity_id=None,
            accepted_attempt_id=None,
            heard_at=None,
        ),
        _valid_receipt(
            selected_entity_id=None,
            accepted_attempt_id=None,
            accepted_at=None,
        ),
        _valid_receipt(
            selected_entity_id="media_player.kitchen",
            accepted_attempt_id="listener_opaque_attempt_token_1234",
        ),
        _valid_receipt(
            selected_entity_id=None,
            accepted_attempt_id="listener_opaque_attempt_token_1234",
            heard_at=None,
        ),
        _valid_receipt(
            selected_entity_id=None,
            accepted_attempt_id="listener_opaque_attempt_token_1234",
            heard_at=111.0,
        ),
    ],
)
def test_receipt_load_rejects_ambiguous_or_malformed_listener_proof(tmp_path, payload):
    path = first_listen_receipt_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_first_listen_receipt(path, 200.0) is None


def test_receipt_load_result_distinguishes_missing_present_and_unavailable(tmp_path):
    path = first_listen_receipt_path(tmp_path)

    missing = load_first_listen_receipt_result(path, 200.0)
    assert missing.status is FirstListenReceiptLoadStatus.MISSING
    assert missing.receipt is None

    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_valid_receipt()), encoding="utf-8")
    present = load_first_listen_receipt_result(path, 200.0)
    assert present.status is FirstListenReceiptLoadStatus.PRESENT
    assert present.receipt is not None
    assert present.receipt.audio_complete is True

    path.write_text("not json", encoding="utf-8")
    unavailable = load_first_listen_receipt_result(path, 200.0)
    assert unavailable.status is FirstListenReceiptLoadStatus.UNAVAILABLE
    assert unavailable.receipt is None


@pytest.mark.asyncio
async def test_receipt_store_exposes_explicit_load_result(tmp_path):
    store = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)
    assert (await store.load_result()).status is FirstListenReceiptLoadStatus.MISSING

    await store.record_privacy_reviewed(reviewed_at=120.0)
    result = await store.load_result()

    assert result.status is FirstListenReceiptLoadStatus.PRESENT
    assert result.receipt is not None
    assert result.receipt.privacy_reviewed_at == 120.0


@pytest.mark.asyncio
async def test_receipt_store_rejects_invalid_public_facts(tmp_path):
    store = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)

    with pytest.raises(ValueError, match="event timestamp"):
        await store.record_listener_heard(heard_at=0.0)
    with pytest.raises(ValueError, match="media_player entity id"):
        await store.record_accepted("light.kitchen", accepted_at=100.0)
    with pytest.raises(ValueError, match="heard must be a boolean"):
        await store.verify("valid_attempt_token_1234", heard=cast(bool, "yes"))
    with pytest.raises(FirstListenAttemptMismatchError):
        await store.verify("short", heard=True)

    assert await store.load() is None


@pytest.mark.asyncio
async def test_incomplete_ha_facts_enforce_time_order_and_idempotence(tmp_path):
    store = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)
    accepted = await store.record_accepted("media_player.kitchen", accepted_at=100.0)

    with pytest.raises(ValueError, match="cannot precede playback acceptance"):
        await store.verify(accepted.accepted_attempt_id or "", heard=True, verified_at=99.0)

    still_unheard = await store.verify(accepted.accepted_attempt_id or "", heard=False)
    first_review = await store.record_privacy_reviewed(reviewed_at=120.0)
    repeated_review = await store.record_privacy_reviewed(reviewed_at=150.0)

    assert still_unheard == accepted
    assert repeated_review == first_review
    assert repeated_review.privacy_reviewed_at == 120.0


@pytest.mark.asyncio
async def test_receipt_records_facts_with_owner_only_atomic_persistence(tmp_path):
    store = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)
    with patch("mammamiradio.core.first_listen.os.fsync", wraps=os.fsync) as fsync:
        accepted = await store.record_accepted("media_player.kitchen", accepted_at=100.0)

    path = first_listen_receipt_path(tmp_path)
    assert accepted.selected_entity_id == "media_player.kitchen"
    assert accepted.accepted_attempt_id
    assert accepted.heard_at is None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert fsync.call_count >= 2
    assert json.loads(path.read_text(encoding="utf-8")) == accepted.to_dict()


@pytest.mark.asyncio
async def test_generated_ha_attempt_namespace_round_trips_reserved_token_output(tmp_path):
    store = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)

    with patch("mammamiradio.core.first_listen.secrets.token_urlsafe", return_value="listener_reserved_collision"):
        accepted = await store.record_accepted("media_player.kitchen", accepted_at=100.0)

    assert accepted.accepted_attempt_id == "ha_listener_reserved_collision"
    assert await FirstListenReceiptStore(tmp_path, clock=lambda: 200.0).load() == accepted


@pytest.mark.asyncio
async def test_listener_heard_round_trips_and_is_idempotent(tmp_path):
    store = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)

    first = await store.record_listener_heard(heard_at=100.0)
    second = await store.record_listener_heard(heard_at=150.0)

    assert first == second
    assert first.selected_entity_id is None
    assert first.accepted_attempt_id is not None
    assert first.accepted_attempt_id.startswith("listener_")
    assert first.accepted_at == 100.0
    assert first.heard_at == 100.0
    assert await store.load() == first


@pytest.mark.asyncio
async def test_listener_heard_supersedes_incomplete_ha_attempt(tmp_path):
    store = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)
    accepted = await store.record_accepted("media_player.kitchen", accepted_at=90.0)
    await store.record_privacy_reviewed(reviewed_at=95.0)

    listener = await store.record_listener_heard(heard_at=100.0)

    assert listener.accepted_attempt_id != accepted.accepted_attempt_id
    assert listener.selected_entity_id is None
    assert listener.accepted_at == listener.heard_at == 100.0
    assert listener.privacy_reviewed_at == 95.0


@pytest.mark.asyncio
async def test_listener_heard_preserves_concurrent_privacy_fact(tmp_path):
    store = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)

    await asyncio.gather(
        store.record_listener_heard(heard_at=100.0),
        store.record_privacy_reviewed(reviewed_at=120.0),
    )

    final = await store.load()
    assert final is not None
    assert final.accepted_at == final.heard_at == 100.0
    assert final.privacy_reviewed_at == 120.0


@pytest.mark.asyncio
async def test_listener_heard_keeps_completed_ha_proof_unchanged(tmp_path):
    store = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)
    accepted = await store.record_accepted("media_player.kitchen", accepted_at=90.0)
    completed = await store.verify(accepted.accepted_attempt_id or "", heard=True, verified_at=100.0)

    listener = await store.record_listener_heard(heard_at=120.0)

    assert listener == completed


@pytest.mark.asyncio
async def test_legacy_verify_not_yet_cannot_invalidate_listener_proof_across_restart(tmp_path):
    store = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)
    listener = await store.record_listener_heard(heard_at=100.0)

    unchanged = await store.verify(listener.accepted_attempt_id or "", heard=False)
    restarted = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)

    assert unchanged == listener
    assert await restarted.load() == listener


@pytest.mark.asyncio
async def test_legacy_ha_acceptance_cannot_supersede_listener_proof_across_restart(tmp_path):
    store = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)
    listener = await store.record_listener_heard(heard_at=100.0)

    unchanged = await store.record_accepted("media_player.kitchen", accepted_at=120.0)
    restarted = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)

    assert unchanged == listener
    assert await restarted.load() == listener


@pytest.mark.asyncio
async def test_new_ha_acceptance_still_supersedes_completed_ha_proof(tmp_path):
    store = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)
    first = await store.record_accepted("media_player.kitchen", accepted_at=90.0)
    completed = await store.verify(first.accepted_attempt_id or "", heard=True, verified_at=100.0)

    second = await store.record_accepted("media_player.bedroom", accepted_at=120.0)

    assert second.accepted_attempt_id != completed.accepted_attempt_id
    assert second.selected_entity_id == "media_player.bedroom"
    assert second.heard_at is None
    assert await FirstListenReceiptStore(tmp_path, clock=lambda: 200.0).load() == second


@pytest.mark.asyncio
async def test_attempt_compare_and_swap_survives_restart_and_rejects_stale_tab(tmp_path):
    store = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)
    first = await store.record_accepted("media_player.kitchen", accepted_at=100.0)
    second = await store.record_accepted("media_player.bedroom", accepted_at=110.0)

    with pytest.raises(FirstListenAttemptMismatchError):
        await store.verify(first.accepted_attempt_id or "", heard=True, verified_at=120.0)
    assert (await store.load()) == second

    restarted = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)
    heard = await restarted.verify(second.accepted_attempt_id or "", heard=True, verified_at=120.0)
    assert heard.heard_at == 120.0
    idempotent = await restarted.verify(second.accepted_attempt_id or "", heard=True, verified_at=130.0)
    assert idempotent.heard_at == 120.0
    not_yet = await restarted.verify(second.accepted_attempt_id or "", heard=False)
    assert not_yet.heard_at is None
    assert not_yet.accepted_at == 110.0


@pytest.mark.asyncio
async def test_concurrent_verify_and_privacy_mutations_preserve_both_fields(tmp_path):
    store = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)
    accepted = await store.record_accepted("media_player.kitchen", accepted_at=100.0)

    verified, reviewed = await asyncio.gather(
        store.verify(accepted.accepted_attempt_id or "", heard=True, verified_at=110.0),
        store.record_privacy_reviewed(reviewed_at=120.0),
    )

    assert verified.accepted_attempt_id == reviewed.accepted_attempt_id
    final = await store.load()
    assert final is not None
    assert final.heard_at == 110.0
    assert final.privacy_reviewed_at == 120.0


@pytest.mark.asyncio
async def test_corrupt_receipt_is_incomplete_and_a_new_fact_repairs_it(tmp_path):
    path = first_listen_receipt_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    store = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)

    assert await store.load() is None
    reviewed = await store.record_privacy_reviewed(reviewed_at=120.0)

    assert reviewed.privacy_reviewed_at == 120.0
    assert reviewed.accepted_attempt_id is None
    assert await store.load() == reviewed


@pytest.mark.asyncio
async def test_receipt_write_failure_is_typed_and_leaves_attempt_incomplete(tmp_path):
    store = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)
    with (
        patch("mammamiradio.core.first_listen._atomic_write_json", side_effect=OSError("disk full")),
        pytest.raises(FirstListenReceiptUnavailableError),
    ):
        await store.record_accepted("media_player.kitchen", accepted_at=100.0)

    assert not first_listen_receipt_path(tmp_path).exists()
    assert await store.load() is None


@pytest.mark.asyncio
async def test_receipt_writes_run_off_the_event_loop(tmp_path):
    store = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)
    event_loop_thread = threading.get_ident()
    writer_threads: list[int] = []
    real_write = first_listen._atomic_write_json

    def recording_write(path, payload):
        writer_threads.append(threading.get_ident())
        real_write(path, payload)

    with patch("mammamiradio.core.first_listen._atomic_write_json", side_effect=recording_write):
        await store.record_accepted("media_player.kitchen", accepted_at=100.0)

    assert writer_threads
    assert all(thread_id != event_loop_thread for thread_id in writer_threads)


@pytest.mark.asyncio
async def test_cancelled_mutation_holds_lock_until_worker_finishes(tmp_path):
    store = FirstListenReceiptStore(tmp_path, clock=lambda: 200.0)
    started = threading.Event()
    release = threading.Event()
    real_write = first_listen._atomic_write_json
    call_count = 0

    def blocking_first_write(path, payload):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            started.set()
            assert release.wait(timeout=2.0)
        real_write(path, payload)

    with patch("mammamiradio.core.first_listen._atomic_write_json", side_effect=blocking_first_write):
        accepted_task = asyncio.create_task(store.record_accepted("media_player.kitchen", accepted_at=100.0))
        assert await asyncio.to_thread(started.wait, 1.0)
        accepted_task.cancel()
        privacy_task = asyncio.create_task(store.record_privacy_reviewed(reviewed_at=120.0))
        await asyncio.sleep(0.02)
        assert not privacy_task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await accepted_task
        reviewed = await privacy_task

    assert reviewed.accepted_at == 100.0
    assert reviewed.privacy_reviewed_at == 120.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database_preexisted", "expected"),
    [
        (False, FirstListenInstallOriginStatus.FRESH),
        (True, FirstListenInstallOriginStatus.EXISTING),
    ],
)
async def test_origin_migration_projects_only_agreeing_feature_witnesses(
    tmp_path,
    database_preexisted,
    expected,
):
    db_path = tmp_path / "radio.db"
    if database_preexisted:
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE pre_feature_data (id INTEGER PRIMARY KEY)")
    capture = capture_first_listen_install_origin(db_path)

    with patch("mammamiradio.core.first_listen._atomic_write_json", wraps=first_listen._atomic_write_json) as write:
        sentinel_state = init_db(db_path)
        write.assert_not_called()

    assert sentinel_state is FirstListenOriginSentinelState.NEWLY_CREATED
    assert _origin_rows(db_path) == []
    assert (
        load_first_listen_install_origin(tmp_path / "state", db_path).status is FirstListenInstallOriginStatus.UNKNOWN
    )

    origin = await migrate_first_listen_install_origin(capture, sentinel_state, tmp_path / "state")

    assert origin.status is expected
    assert origin.database_preexisted is database_preexisted
    assert _origin_rows(db_path) == [(1, 1, int(database_preexisted))]
    assert stat.S_IMODE(first_listen_origin_path(tmp_path / "state").stat().st_mode) == 0o600
    assert stat.S_IMODE(first_listen_origin_path(tmp_path / "state").parent.stat().st_mode) == 0o700
    assert init_db(db_path) is FirstListenOriginSentinelState.PREEXISTING


@pytest.mark.asyncio
async def test_preexisting_empty_sentinel_stays_unknown_and_is_never_recaptured(tmp_path):
    db_path = tmp_path / "radio.db"
    first_capture = capture_first_listen_install_origin(db_path)
    assert init_db(db_path) is FirstListenOriginSentinelState.NEWLY_CREATED
    assert first_capture.database_preexisted is False

    # Simulate a process exit before the post-audio witness migration.
    restart_capture = capture_first_listen_install_origin(db_path)
    restart_state = init_db(db_path)
    origin = await migrate_first_listen_install_origin(restart_capture, restart_state, tmp_path / "state")

    assert restart_capture.database_preexisted is True
    assert restart_state is FirstListenOriginSentinelState.PREEXISTING
    assert origin.status is FirstListenInstallOriginStatus.UNKNOWN
    assert _origin_rows(db_path) == []
    assert not first_listen_origin_path(tmp_path / "state").exists()


@pytest.mark.asyncio
async def test_origin_first_write_failure_then_restart_never_reclassifies(tmp_path):
    db_path = tmp_path / "radio.db"
    capture = capture_first_listen_install_origin(db_path)
    sentinel_state = init_db(db_path)
    with patch("mammamiradio.core.first_listen._atomic_write_json", side_effect=OSError("read only")):
        first = await migrate_first_listen_install_origin(capture, sentinel_state, tmp_path / "state")
    assert first.status is FirstListenInstallOriginStatus.UNKNOWN

    restart_capture = capture_first_listen_install_origin(db_path)
    restart_state = init_db(db_path)
    restarted = await migrate_first_listen_install_origin(restart_capture, restart_state, tmp_path / "state")

    assert restarted.status is FirstListenInstallOriginStatus.UNKNOWN
    assert _origin_rows(db_path) == []
    assert not first_listen_origin_path(tmp_path / "state").exists()


@pytest.mark.asyncio
async def test_origin_database_write_failure_rolls_back_new_sidecar(tmp_path):
    db_path = tmp_path / "radio.db"
    capture = capture_first_listen_install_origin(db_path)
    sentinel_state = init_db(db_path)

    with patch(
        "mammamiradio.core.first_listen._origin_table_has_expected_schema",
        side_effect=[True, False],
    ):
        origin = await migrate_first_listen_install_origin(capture, sentinel_state, tmp_path / "state")

    assert origin.status is FirstListenInstallOriginStatus.UNKNOWN
    assert _origin_rows(db_path) == []
    assert not first_listen_origin_path(tmp_path / "state").exists()


@pytest.mark.asyncio
async def test_preexisting_malformed_sentinel_stays_unknown_without_repair(tmp_path):
    db_path = tmp_path / "radio.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"CREATE TABLE {FIRST_LISTEN_ORIGIN_TABLE} (wrong_column INTEGER)")
    capture = capture_first_listen_install_origin(db_path)

    sentinel_state = init_db(db_path)
    origin = await migrate_first_listen_install_origin(capture, sentinel_state, tmp_path / "state")

    assert sentinel_state is FirstListenOriginSentinelState.PREEXISTING
    assert origin.status is FirstListenInstallOriginStatus.UNKNOWN
    assert not first_listen_origin_path(tmp_path / "state").exists()
    with sqlite3.connect(db_path) as conn:
        assert [row[1] for row in conn.execute(f"PRAGMA table_info({FIRST_LISTEN_ORIGIN_TABLE})")] == ["wrong_column"]


@pytest.mark.asyncio
async def test_existing_sidecar_or_disagreement_is_never_overwritten(tmp_path):
    db_path = tmp_path / "radio.db"
    capture = capture_first_listen_install_origin(db_path)
    sentinel_state = init_db(db_path)
    sidecar = first_listen_origin_path(tmp_path / "state")
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(
        json.dumps({"schema_version": 1, "database_preexisted": True}),
        encoding="utf-8",
    )

    origin = await migrate_first_listen_install_origin(capture, sentinel_state, tmp_path / "state")

    assert origin.status is FirstListenInstallOriginStatus.UNKNOWN
    assert json.loads(sidecar.read_text(encoding="utf-8"))["database_preexisted"] is True
    assert _origin_rows(db_path) == []


@pytest.mark.asyncio
async def test_disagreeing_witnesses_project_unknown_and_never_reclassify(tmp_path, caplog):
    """Sidecar and database both valid but conflicting must stay UNKNOWN, loudly."""
    db_path = tmp_path / "radio.db"
    state_dir = tmp_path / "state"
    capture = capture_first_listen_install_origin(db_path)
    sentinel_state = init_db(db_path)
    await migrate_first_listen_install_origin(capture, sentinel_state, state_dir)
    sidecar = first_listen_origin_path(state_dir)
    sidecar.write_text(
        json.dumps({"schema_version": 1, "database_preexisted": True}),
        encoding="utf-8",
    )

    with caplog.at_level("WARNING"):
        origin = load_first_listen_install_origin(state_dir, db_path)

    assert origin.status is FirstListenInstallOriginStatus.UNKNOWN
    assert any("witnesses disagree" in record.message for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1, "database_preexisted": False, "extra": 1},
        {"schema_version": True, "database_preexisted": False},
        {"schema_version": 2, "database_preexisted": False},
        {"schema_version": 1, "database_preexisted": "no"},
        ["schema_version", 1],
    ],
)
async def test_malformed_sidecar_payload_projects_unknown(tmp_path, payload):
    """Any off-schema sidecar reads as invalid; classification fails closed."""
    db_path = tmp_path / "radio.db"
    state_dir = tmp_path / "state"
    capture = capture_first_listen_install_origin(db_path)
    sentinel_state = init_db(db_path)
    await migrate_first_listen_install_origin(capture, sentinel_state, state_dir)
    first_listen_origin_path(state_dir).write_text(json.dumps(payload), encoding="utf-8")

    assert load_first_listen_install_origin(state_dir, db_path).status is FirstListenInstallOriginStatus.UNKNOWN


def test_capture_reports_bare_preexisting_database(tmp_path):
    """A file without the completed-boot witness is the crash-artifact signature."""
    db_path = tmp_path / "radio.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE partial_cold_boot (id INTEGER PRIMARY KEY)")

    capture = capture_first_listen_install_origin(db_path)

    assert capture.database_preexisted is True
    assert capture.database_bare is True


def test_capture_reports_completed_prior_install_as_not_bare(tmp_path):
    """The persisted R0 origin witness proves a completed prior boot."""
    from mammamiradio.home.migration import LegacyHomePreflightV1, persist_legacy_home_database_preflight_v1

    db_path = tmp_path / "radio.db"
    init_db(db_path)
    persist_legacy_home_database_preflight_v1(db_path, LegacyHomePreflightV1(database_preexisted=False, durable=True))

    capture = capture_first_listen_install_origin(db_path)

    assert capture.database_preexisted is True
    assert capture.database_bare is False


def test_completed_boot_marker_matches_home_migration_table():
    from mammamiradio.home.migration import DATABASE_ORIGIN_TABLE

    assert first_listen._COMPLETED_BOOT_MARKER_TABLE == DATABASE_ORIGIN_TABLE


def test_capture_missing_database_is_not_bare(tmp_path):
    capture = capture_first_listen_install_origin(tmp_path / "radio.db")

    assert capture.database_preexisted is False
    assert capture.database_bare is False


def test_capture_unreadable_database_reads_bare(tmp_path):
    """An unreadable preexisting file cannot prove a completed install."""
    db_path = tmp_path / "radio.db"
    db_path.write_bytes(b"not a sqlite file, definitely long enough to look wrong")

    capture = capture_first_listen_install_origin(db_path)

    assert capture.database_preexisted is True
    assert capture.database_bare is True


@pytest.mark.asyncio
async def test_legacy_r0_sidecar_cannot_classify_first_listen_origin(tmp_path):
    db_path = tmp_path / "radio.db"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "legacy_home_preflight_v1.json").write_text(
        json.dumps({"schema_version": 1, "database_preexisted": True}),
        encoding="utf-8",
    )
    init_db(db_path)

    assert load_first_listen_install_origin(state_dir, db_path).status is FirstListenInstallOriginStatus.UNKNOWN


@pytest.mark.asyncio
async def test_origin_migration_runs_off_the_event_loop(tmp_path):
    db_path = tmp_path / "radio.db"
    capture = capture_first_listen_install_origin(db_path)
    sentinel_state = init_db(db_path)
    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []
    real_migrate = first_listen._migrate_first_listen_install_origin_blocking

    def recording_migrate(*args):
        worker_threads.append(threading.get_ident())
        return real_migrate(*args)

    with patch(
        "mammamiradio.core.first_listen._migrate_first_listen_install_origin_blocking",
        side_effect=recording_migrate,
    ):
        origin = await migrate_first_listen_install_origin(capture, sentinel_state, tmp_path / "state")

    assert origin.status is FirstListenInstallOriginStatus.FRESH
    assert worker_threads and worker_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_origin_migration_cancellation_drains_the_filesystem_worker(tmp_path):
    db_path = tmp_path / "radio.db"
    capture = capture_first_listen_install_origin(db_path)
    sentinel_state = init_db(db_path)
    started = threading.Event()
    release = threading.Event()

    def blocking_migrate(*_args):
        started.set()
        release.wait(timeout=1.0)
        return FirstListenInstallOriginV1(FirstListenInstallOriginStatus.FRESH)

    with patch(
        "mammamiradio.core.first_listen._migrate_first_listen_install_origin_blocking",
        side_effect=blocking_migrate,
    ):
        migration = asyncio.create_task(
            migrate_first_listen_install_origin(capture, sentinel_state, tmp_path / "state")
        )
        assert await asyncio.to_thread(started.wait, 1.0)
        migration.cancel()
        await asyncio.sleep(0.02)
        assert not migration.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await migration
