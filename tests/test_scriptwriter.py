"""Tests for scriptwriter module: prompt building, banter, and ad generation."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fakeitaliradio.config import load_config
from fakeitaliradio.models import (
    AdBrand,
    AdScript,
    AdVoice,
    HostPersonality,
    StationState,
    Track,
)
from fakeitaliradio.scriptwriter import _build_system_prompt, write_ad, write_banter


@pytest.fixture()
def config():
    cfg = load_config()
    cfg.anthropic_api_key = "test-key"
    return cfg


@pytest.fixture()
def state():
    return StationState(playlist=[Track(title="Test", artist="Artist", duration_ms=1000, spotify_id="test1")])


def _mock_anthropic_response(text: str):
    """Build a mock AsyncAnthropic whose messages.create returns the given text."""
    mock_content_block = MagicMock()
    mock_content_block.text = text

    mock_response = MagicMock()
    mock_response.content = [mock_content_block]

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    mock_cls = MagicMock(return_value=mock_client)
    return mock_cls


# --- _build_system_prompt tests ---


def test_system_prompt_includes_host_names(config):
    prompt = _build_system_prompt(config)
    for host in config.hosts:
        assert host.name in prompt


def test_system_prompt_includes_language(config):
    prompt = _build_system_prompt(config)
    assert config.station.language in prompt


def test_system_prompt_includes_theme(config):
    prompt = _build_system_prompt(config)
    assert config.station.theme in prompt


def test_system_prompt_includes_station_name(config):
    prompt = _build_system_prompt(config)
    assert config.station.name in prompt


# --- write_banter tests ---


@pytest.mark.asyncio
async def test_write_banter_parses_valid_json(config, state):
    host_name = config.hosts[0].name
    response_json = json.dumps(
        {
            "lines": [
                {"host": host_name, "text": "Ciao a tutti!"},
                {"host": host_name, "text": "Che bella giornata!"},
            ],
            "new_joke": None,
        }
    )
    mock_cls = _mock_anthropic_response(response_json)

    with patch("fakeitaliradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls):
        result = await write_banter(state, config)

    assert len(result) == 2
    assert result[0][0].name == host_name
    assert result[0][1] == "Ciao a tutti!"
    assert result[1][1] == "Che bella giornata!"


@pytest.mark.asyncio
async def test_write_banter_strips_markdown_fences(config, state):
    host_name = config.hosts[0].name
    response_text = (
        "```json\n"
        + json.dumps(
            {
                "lines": [{"host": host_name, "text": "Eccoci!"}],
                "new_joke": None,
            }
        )
        + "\n```"
    )
    mock_cls = _mock_anthropic_response(response_text)

    with patch("fakeitaliradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls):
        result = await write_banter(state, config)

    assert len(result) == 1
    assert result[0][1] == "Eccoci!"


@pytest.mark.asyncio
async def test_write_banter_adds_new_joke(config, state):
    host_name = config.hosts[0].name
    response_json = json.dumps(
        {
            "lines": [{"host": host_name, "text": "Haha!"}],
            "new_joke": "The traffic joke",
        }
    )
    mock_cls = _mock_anthropic_response(response_json)

    assert len(state.running_jokes) == 0
    with patch("fakeitaliradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls):
        await write_banter(state, config)

    assert "The traffic joke" in state.running_jokes


@pytest.mark.asyncio
async def test_write_banter_falls_back_on_api_exception(config, state):
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API down"))
    mock_cls = MagicMock(return_value=mock_client)

    with patch("fakeitaliradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls):
        result = await write_banter(state, config)

    assert len(result) == 1
    # Fallback text for Italian
    assert result[0][1] in ("E torniamo alla musica!", "And back to the music!")


@pytest.mark.asyncio
async def test_write_banter_falls_back_on_malformed_json(config, state):
    mock_cls = _mock_anthropic_response("this is not valid json {{{")

    with patch("fakeitaliradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls):
        result = await write_banter(state, config)

    assert len(result) == 1
    # Should be fallback copy
    assert isinstance(result[0][0], HostPersonality)
    assert isinstance(result[0][1], str)


# --- write_ad tests ---


@pytest.mark.asyncio
async def test_write_ad_returns_adscript(config, state):
    response_json = json.dumps(
        {
            "parts": [
                {"type": "sfx", "sfx": "chime"},
                {"type": "voice", "text": "Comprate ora!"},
            ],
            "mood": "upbeat",
            "summary": "A test ad for TestBrand",
        }
    )
    mock_cls = _mock_anthropic_response(response_json)

    brand = AdBrand(name="TestBrand", tagline="Il meglio del meglio", category="food")
    voice = AdVoice(name="Voce Uno", voice="it-IT-IsabellaNeural", style="enthusiastic")

    with patch("fakeitaliradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls):
        result = await write_ad(brand, voice, state, config)

    assert isinstance(result, AdScript)
    assert result.brand == "TestBrand"
    assert result.mood == "upbeat"
    assert result.summary == "A test ad for TestBrand"
    assert len(result.parts) == 2
    assert result.parts[0].type == "sfx"
    assert result.parts[1].type == "voice"
    assert result.parts[1].text == "Comprate ora!"


@pytest.mark.asyncio
async def test_write_ad_falls_back_on_api_exception(config, state):
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API down"))
    mock_cls = MagicMock(return_value=mock_client)

    brand = AdBrand(name="FallbackBrand", tagline="Sempre il top", category="tech")
    voice = AdVoice(name="Voce Due", voice="it-IT-DiegoNeural", style="calm")

    with patch("fakeitaliradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls):
        result = await write_ad(brand, voice, state, config)

    assert isinstance(result, AdScript)
    assert result.brand == "FallbackBrand"
    assert "Fallback" in result.summary
    assert len(result.parts) >= 1
    assert result.parts[0].type == "voice"


@pytest.mark.asyncio
async def test_write_ad_ensures_voice_part_when_llm_returns_none(config, state):
    """If the LLM returns no voice parts, write_ad should add at least one."""
    response_json = json.dumps(
        {
            "parts": [
                {"type": "sfx", "sfx": "chime"},
                {"type": "pause", "duration": 0.5},
            ],
            "mood": "mysterious",
            "summary": "An ad with no voice",
        }
    )
    mock_cls = _mock_anthropic_response(response_json)

    brand = AdBrand(name="SilentBrand", tagline="Silenzio è oro", category="luxury")
    voice = AdVoice(name="Voce Tre", voice="it-IT-ElsaNeural", style="whispery")

    with patch("fakeitaliradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls):
        result = await write_ad(brand, voice, state, config)

    assert isinstance(result, AdScript)
    voice_parts = [p for p in result.parts if p.type == "voice"]
    assert len(voice_parts) >= 1
