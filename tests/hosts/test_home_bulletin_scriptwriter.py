"""Focused contracts for the no-fallback Casa bulletin writer."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import StationState, Track
from mammamiradio.home.context_director import PromptFact
from mammamiradio.hosts.scriptwriter import (
    HOME_BULLETIN_OPENING,
    count_italian_words,
    home_bulletin_provider_ready,
    write_home_bulletin,
)


@pytest.fixture()
def config():
    cfg = load_config()
    cfg.anthropic_api_key = "test-key"
    cfg.openai_api_key = ""
    cfg.homeassistant.enabled = True
    cfg.homeassistant.context_enabled = True
    return cfg


@pytest.fixture()
def state():
    return StationState(playlist=[Track(title="Test", artist="Artist", duration_ms=1000)])


@pytest.fixture()
def prompt_fact():
    return PromptFact(
        fact_id="fact-casa-1",
        entity_id="weather.home",
        topic_key="weather",
        fingerprint="safe-fingerprint",
        prompt="Fuori piove e la temperatura è di quindici gradi.",
        policy_revision=1,
    )


def _body(word_count: int) -> str:
    return " ".join(["casa"] * word_count)


def test_count_italian_words_keeps_straight_and_curly_elisions_whole():
    assert count_italian_words("L'aria cambia e l’ora arriva") == 5


@pytest.mark.parametrize(
    ("final_word_count", "accepted"),
    [(69, False), (70, True), (95, True), (96, False)],
)
@pytest.mark.asyncio
async def test_write_home_bulletin_enforces_final_word_boundaries(
    config,
    state,
    prompt_fact,
    final_word_count,
    accepted,
):
    response = {"text": _body(final_word_count - 4), "home_fact_id": prompt_fact.fact_id}

    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new=AsyncMock(return_value=response),
    ):
        result = await write_home_bulletin(state, config, prompt_fact=prompt_fact)

    assert (result is not None) is accepted
    if result is not None:
        _host, text = result
        assert text.startswith(HOME_BULLETIN_OPENING)
        assert count_italian_words(text) == final_word_count


@pytest.mark.asyncio
async def test_write_home_bulletin_strips_repeated_opening_and_stage_directions(config, state, prompt_fact):
    response = {
        "text": f"{HOME_BULLETIN_OPENING}: [warmly] {_body(66)}",
        "home_fact_id": prompt_fact.fact_id,
    }

    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new=AsyncMock(return_value=response),
    ):
        result = await write_home_bulletin(state, config, prompt_fact=prompt_fact)

    assert result is not None
    _host, text = result
    assert text.count(HOME_BULLETIN_OPENING) == 1
    assert "[warmly]" not in text
    assert count_italian_words(text) == 70


@pytest.mark.asyncio
async def test_write_home_bulletin_uses_italian_casa_prompt_in_default_mode(config, state, prompt_fact):
    """Casa must not inherit Normal Mode's English-led system instruction."""
    response = {"text": _body(66), "home_fact_id": prompt_fact.fact_id}

    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new=AsyncMock(return_value=response),
    ) as generate:
        result = await write_home_bulletin(state, config, prompt_fact=prompt_fact)

    assert result is not None
    system_prompt = generate.await_args.kwargs["system_prompt_override"]
    assert "LANGUAGE — CASA" in system_prompt
    assert "100% natural spoken Italian" in system_prompt
    assert "75% English" not in system_prompt


@pytest.mark.asyncio
async def test_write_home_bulletin_accepts_italian_radio_noun(config, state, prompt_fact):
    """The Italian noun ``radio`` must not be rejected as an English token."""
    response = {
        "text": "radio " + _body(65),
        "home_fact_id": prompt_fact.fact_id,
    }

    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new=AsyncMock(return_value=response),
    ):
        result = await write_home_bulletin(state, config, prompt_fact=prompt_fact)

    assert result is not None


@pytest.mark.asyncio
async def test_write_home_bulletin_has_no_provider_fallback(config, state, prompt_fact):
    config.anthropic_api_key = ""
    config.openai_api_key = ""

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", new=AsyncMock()) as generate:
        result = await write_home_bulletin(state, config, prompt_fact=prompt_fact)

    assert result is None
    assert not home_bulletin_provider_ready(config)
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_home_bulletin_fails_closed_after_context_revocation(config, state, prompt_fact):
    config.homeassistant.context_enabled = False

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", new=AsyncMock()) as generate:
        result = await write_home_bulletin(state, config, prompt_fact=prompt_fact)

    assert result is None
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_home_bulletin_rejects_wrong_fact_contract(config, state, prompt_fact):
    response = {"text": _body(66), "home_fact_id": "different-fact"}

    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new=AsyncMock(return_value=response),
    ):
        result = await write_home_bulletin(state, config, prompt_fact=prompt_fact)

    assert result is None


@pytest.mark.asyncio
async def test_write_home_bulletin_returns_none_on_generation_failure(config, state, prompt_fact):
    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new=AsyncMock(side_effect=RuntimeError("provider unavailable")),
    ):
        result = await write_home_bulletin(state, config, prompt_fact=prompt_fact)

    assert result is None


@pytest.mark.asyncio
async def test_write_home_bulletin_rejects_known_english_copy(config, state, prompt_fact):
    response = {
        "text": "the " + _body(65),
        "home_fact_id": prompt_fact.fact_id,
    }

    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new=AsyncMock(return_value=response),
    ):
        result = await write_home_bulletin(state, config, prompt_fact=prompt_fact)

    assert result is None
