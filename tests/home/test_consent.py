"""Permanent narrow scope and serialized Home choice persistence."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mammamiradio.core.first_listen import FirstListenReceiptV1
from mammamiradio.core.models import StationState
from mammamiradio.home import migration
from mammamiradio.home.compatibility import read_home_compatibility
from mammamiradio.home.consent import CONSENT_TABLE, load_ambient_consent, save_ambient_consent
from tests.home_fixtures import allow_synthetic_snapshot, synthetic_home_document, write_compatibility_install


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "mammamiradio.db"
    sqlite3.connect(path).close()
    return path


def test_scope_survives_revocation_and_restart(database):
    assert not load_ambient_consent(database).capped
    save_ambient_consent(database, granted=True)
    assert load_ambient_consent(database).granted
    save_ambient_consent(database, granted=False)
    consent = load_ambient_consent(database)
    assert consent.capped and consent.readable and not consent.granted


@pytest.mark.parametrize(
    "definition",
    [
        f"CREATE VIEW {CONSENT_TABLE} AS SELECT 1 AS singleton, 'ambient-v1' AS scope_cap, 1 AS granted",
        f"CREATE TABLE {CONSENT_TABLE} (singleton, scope_cap, granted)",
        f"CREATE TABLE {CONSENT_TABLE} AS SELECT 1 AS singleton, 'ambient-v1' AS scope_cap, 1.0 AS granted",
        f"CREATE TABLE {CONSENT_TABLE} AS SELECT 2 AS singleton, 'ambient-v1' AS scope_cap, 1 AS granted",
        f"CREATE TABLE {CONSENT_TABLE} AS SELECT 1 AS singleton, 'broad' AS scope_cap, 1 AS granted",
    ],
)
def test_malformed_evidence_stays_capped_and_cannot_be_repaired(database, definition):
    with sqlite3.connect(database) as connection:
        connection.execute(definition)
    before = database.read_bytes()
    consent = load_ambient_consent(database)
    assert consent.capped and not consent.granted and not consent.readable
    with pytest.raises((ValueError, sqlite3.Error)):
        save_ambient_consent(database, granted=True)
    assert database.read_bytes() == before


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    assert not load_ambient_consent(path).granted
    with pytest.raises(sqlite3.Error):
        save_ambient_consent(path, granted=True)
    assert not path.exists()


@pytest.mark.parametrize("choice", [None, False, True])
def test_profile_restore_before_choice_and_scope_cap_after_choice(tmp_path, monkeypatch, choice):
    document = synthetic_home_document()
    allow_synthetic_snapshot(monkeypatch, document)
    state_dir, db_path = tmp_path / "state", tmp_path / "mammamiradio.db"
    path, _ = write_compatibility_install(state_dir, db_path, document, binding="complete")
    original = path.read_bytes()
    path.unlink()
    missing = read_home_compatibility(state_dir, db_path)
    assert missing.requires_consent and not missing.permits_saved_choice
    assert not load_ambient_consent(db_path).capped
    if choice is not None:
        save_ambient_consent(db_path, granted=choice)
    path.write_bytes(original)
    path.chmod(0o600)
    restored = read_home_compatibility(state_dir, db_path)
    assert restored.authorization.allows_household_moments is (choice is None)
    assert restored.requires_consent is (choice is not None)
    assert restored.permits_saved_choice is (choice is not False)


def test_already_narrow_upgrade_keeps_saved_consent_scope(database):
    state_dir = database.parent / "state"
    witness = migration.capture_legacy_home_preflight_v1(state_dir, database_preexisted=False)
    migration.persist_legacy_home_database_preflight_v1(database, witness)
    decision = read_home_compatibility(state_dir, database)
    assert decision.status == "narrow"
    assert decision.permits_saved_choice
    assert not decision.authorization.allows_household_moments


@pytest.fixture
def choice_app(database, monkeypatch):
    from mammamiradio.web import streamer

    state = StationState(home_ambient_consent_required=True, home_compatibility_status="needs_consent")
    app = SimpleNamespace(
        config=SimpleNamespace(cache_dir=database.parent, homeassistant=SimpleNamespace(context_enabled=False)),
        station_state=state,
        home_context_choice_lock=asyncio.Lock(),
        preview_valid=True,
        home_context_preview_proof=object(),
        first_listen_receipt=None,
        first_listen_store=SimpleNamespace(
            record_privacy_reviewed=AsyncMock(return_value=FirstListenReceiptV1(privacy_reviewed_at=100.0))
        ),
        option=False,
        writes=[],
    )
    monkeypatch.setattr(streamer, "_first_listen_audio_gate_open", AsyncMock(return_value=True))
    monkeypatch.setattr(streamer, "_home_context_preview_proof_valid", lambda app, proof: app.preview_valid)

    async def persist(config, enabled):
        app.writes.append(enabled)
        app.option = enabled

    monkeypatch.setattr(streamer, "_persist_home_context_choice", persist)

    async def disable(app):
        app.config.homeassistant.context_enabled = False
        app.station_state.home_context_requested = False
        app.station_state.home_ambient_consent_granted = False
        app.home_consent_choice = False
        return 0

    monkeypatch.setattr(streamer, "_disable_home_context_runtime", disable)
    return app


@pytest.mark.asyncio
async def test_enable_order_option_preview_consent_runtime(choice_app, database, monkeypatch):
    from mammamiradio.web import streamer

    real_save = streamer.save_ambient_consent

    def save(path, *, granted):
        assert choice_app.option is True
        assert choice_app.config.homeassistant.context_enabled is False
        real_save(path, granted=granted)

    monkeypatch.setattr(streamer, "save_ambient_consent", save)
    result = await streamer._apply_home_context_choice(choice_app, enabled=True)
    assert result["enabled"]
    assert load_ambient_consent(database).granted
    assert choice_app.config.homeassistant.context_enabled


@pytest.mark.asyncio
async def test_stale_preview_after_option_write_cannot_grant(choice_app, database, monkeypatch):
    from mammamiradio.web import streamer

    async def persist(config, enabled):
        choice_app.option = enabled
        choice_app.preview_valid = False

    monkeypatch.setattr(streamer, "_persist_home_context_choice", persist)
    result = await streamer._apply_home_context_choice(choice_app, enabled=True)
    assert result.status_code == 409
    assert not choice_app.option and not load_ambient_consent(database).granted
    assert not choice_app.config.homeassistant.context_enabled


@pytest.mark.asyncio
async def test_off_attempts_option_write_even_when_consent_fails(choice_app, monkeypatch):
    from mammamiradio.web import streamer

    monkeypatch.setattr(streamer, "save_ambient_consent", lambda *a, **kw: (_ for _ in ()).throw(OSError("synthetic")))
    choice_app.config.homeassistant.context_enabled = True
    result = await streamer._apply_home_context_choice(choice_app, enabled=False)
    assert result.status_code >= 400
    assert choice_app.writes == [False]
    assert not choice_app.config.homeassistant.context_enabled
    assert json.loads(result.body)["persisted"] is False


@pytest.mark.asyncio
async def test_cancelled_request_keeps_lock_until_choice_and_following_off_finish(choice_app, database, monkeypatch):
    from mammamiradio.web import streamer

    entered, release = asyncio.Event(), asyncio.Event()

    async def persist(config, enabled):
        if enabled:
            entered.set()
            await release.wait()
        choice_app.writes.append(enabled)
        choice_app.option = enabled

    monkeypatch.setattr(streamer, "_persist_home_context_choice", persist)
    monkeypatch.setattr(streamer, "_strict_setup_json", AsyncMock(return_value=({"enabled": True}, None)))
    request = SimpleNamespace(app=SimpleNamespace(state=choice_app))
    enabling = asyncio.create_task(streamer.setup_home_context_choice(request))
    await entered.wait()
    enabling.cancel()
    disabling = asyncio.create_task(streamer._apply_home_context_choice(choice_app, enabled=False))
    await asyncio.sleep(0)
    assert not disabling.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await enabling
    await disabling
    assert choice_app.writes == [True, False]
    assert not choice_app.option and not load_ambient_consent(database).granted
    assert not choice_app.config.homeassistant.context_enabled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("granted", "fresh", "present", "expected"),
    [(False, False, True, False), (False, True, True, True), (True, False, True, True), (False, True, False, False)],
)
async def test_legacy_saved_receipt_does_not_replace_fresh_sound(granted, fresh, present, expected):
    from mammamiradio.core.first_listen import (
        FirstListenInstallOriginStatus,
        FirstListenInstallOriginV1,
        FirstListenReceiptLoadResult,
        FirstListenReceiptLoadStatus,
    )
    from mammamiradio.web.streamer import _first_listen_audio_gate_open

    receipt = FirstListenReceiptV1(accepted_attempt_id="listener_oldproof", accepted_at=100.0, heard_at=100.0)
    app = SimpleNamespace(
        station_state=StationState(home_ambient_consent_required=True, home_ambient_consent_granted=granted),
        first_listen_install_origin=FirstListenInstallOriginV1(FirstListenInstallOriginStatus.EXISTING),
        first_listen_store=SimpleNamespace(
            load_result=AsyncMock(
                return_value=FirstListenReceiptLoadResult(
                    FirstListenReceiptLoadStatus.PRESENT if present else FirstListenReceiptLoadStatus.MISSING,
                    receipt if present else None,
                )
            )
        ),
        home_narrow_audio_confirmed=fresh,
    )
    assert await _first_listen_audio_gate_open(app) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["option", "consent", "postcommit_preview"])
async def test_enable_failure_revokes_before_runtime(choice_app, database, monkeypatch, failure):
    from mammamiradio.web import streamer

    real_save = streamer.save_ambient_consent
    original_persist = streamer._persist_home_context_choice

    async def persist(config, enabled):
        if failure == "option" and enabled:
            choice_app.writes.append(True)
            raise OSError("synthetic option failure")
        await original_persist(config, enabled)

    def save(path, *, granted):
        if failure == "consent" and granted:
            raise OSError("synthetic consent failure")
        real_save(path, granted=granted)
        if failure == "postcommit_preview" and granted:
            choice_app.preview_valid = False

    monkeypatch.setattr(streamer, "_persist_home_context_choice", persist)
    monkeypatch.setattr(streamer, "save_ambient_consent", save)
    result = await streamer._apply_home_context_choice(choice_app, enabled=True)
    assert result.status_code >= 400
    assert choice_app.writes == [True, False]
    assert not choice_app.option and not choice_app.config.homeassistant.context_enabled
    assert load_ambient_consent(database).capped and not load_ambient_consent(database).granted
