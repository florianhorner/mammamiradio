"""Persona milestone consumption boundaries for generated banter."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import StationState, Track
from mammamiradio.core.sync import init_db
from mammamiradio.hosts.persona import PersonaStore
from mammamiradio.hosts.scriptwriter import write_banter


@pytest.fixture()
def config(tmp_path):
    config = load_config()
    config.anthropic_api_key = "test-key"
    config.openai_api_key = ""
    config.super_italian_mode = True
    config.cache_dir = tmp_path
    init_db(config.cache_dir / "mammamiradio.db")
    return config


@pytest.fixture()
def persona_state(config):
    store = PersonaStore(config.cache_dir / "mammamiradio.db")
    state = StationState(
        playlist=[Track(title="Test", artist="Artist", duration_ms=1000, spotify_id="test1")],
        persona_store=store,
    )
    return state, store


async def _seed_session_five(store: PersonaStore) -> None:
    await store.update_persona({"new_theories": ["likes the late-night set"]})
    for _ in range(5):
        await store.increment_session()

    persona = await store.get_persona()
    assert persona.session_count == 5
    assert persona.pending_milestone == 5


async def _assert_session_five_milestone_pending(store: PersonaStore) -> None:
    persona = await store.get_persona()
    assert persona.pending_milestone == 5
    assert persona.arc_metadata.get("milestones_fired", []) == []


def _valid_exchange(config) -> dict:
    first_host, second_host = config.hosts[:2]
    return {
        "lines": [
            {"host": first_host.name, "text": "Questa notte ha una bella energia."},
            {"host": second_host.name, "text": "E noi la seguiamo fino all'ultima nota."},
        ],
        "new_joke": None,
        "home_fact_id": None,
    }


@pytest.mark.asyncio
async def test_write_banter_keeps_session_five_milestone_after_terminal_cutoff(config, persona_state):
    state, store = persona_state
    await _seed_session_five(store)

    response = _valid_exchange(config)
    response["lines"][-1]["text"] = "Aspetta—"
    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response_with_language_guard",
        new=AsyncMock(return_value=response),
    ):
        await write_banter(state, config)

    await _assert_session_five_milestone_pending(store)


@pytest.mark.asyncio
async def test_write_banter_keeps_session_five_milestone_after_malformed_response(config, persona_state):
    state, store = persona_state
    await _seed_session_five(store)

    malformed_response = {
        "lines": [{"host": config.hosts[0].name, "text": None}],
        "new_joke": None,
        "home_fact_id": None,
    }
    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response_with_language_guard",
        new=AsyncMock(return_value=malformed_response),
    ):
        await write_banter(state, config)

    await _assert_session_five_milestone_pending(store)


@pytest.mark.asyncio
async def test_write_banter_keeps_session_five_milestone_after_provider_failure(config, persona_state):
    state, store = persona_state
    await _seed_session_five(store)

    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response_with_language_guard",
        new=AsyncMock(side_effect=RuntimeError("provider unavailable")),
    ):
        await write_banter(state, config)

    await _assert_session_five_milestone_pending(store)


@pytest.mark.asyncio
async def test_write_banter_consumes_session_five_milestone_once_after_valid_exchange(config, persona_state):
    state, store = persona_state
    await _seed_session_five(store)

    consume_milestone = AsyncMock(wraps=store.consume_milestone)
    with (
        patch(
            "mammamiradio.hosts.scriptwriter._generate_json_response_with_language_guard",
            new=AsyncMock(return_value=_valid_exchange(config)),
        ),
        patch.object(store, "consume_milestone", new=consume_milestone),
    ):
        result, _ = await write_banter(state, config)

    assert len(result) == 2
    consume_milestone.assert_awaited_once_with()
    persona = await store.get_persona()
    assert persona.pending_milestone is None
    assert persona.arc_metadata["milestones_fired"] == [5]
