"""Tests for mammamiradio.ha_context — Home Assistant context provider."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mammamiradio.ha_context import (
    HomeContext,
    _build_summary,
    _format_state,
    fetch_home_context,
)

# ---------------------------------------------------------------------------
# HomeContext dataclass
# ---------------------------------------------------------------------------


def test_age_seconds_returns_correct_value():
    ctx = HomeContext(timestamp=time.time() - 30.0)
    assert 29.0 <= ctx.age_seconds <= 31.0


def test_age_seconds_no_timestamp_returns_inf():
    ctx = HomeContext()
    assert ctx.age_seconds == float("inf")


# ---------------------------------------------------------------------------
# _format_state
# ---------------------------------------------------------------------------


def test_format_state_weather_includes_temperature():
    data = {
        "state": "cloudy",
        "attributes": {"temperature": 18, "temperature_unit": "°C"},
    }
    result = _format_state("weather.forecast_home", data)
    assert result is not None
    assert "18" in result
    assert "nuvoloso" in result


def test_format_state_climate_includes_current_and_target():
    data = {
        "state": "heat",
        "attributes": {"current_temperature": 20, "temperature": 22},
    }
    result = _format_state("climate.wohnzimmer_tado_heizung", data)
    assert result is not None
    assert "20" in result
    assert "22" in result
    assert "riscaldamento attivo" in result


def test_format_state_media_player_playing_includes_title_artist():
    data = {
        "state": "playing",
        "attributes": {
            "media_title": "Volare",
            "media_artist": "Dean Martin",
        },
    }
    result = _format_state("media_player.esszimmer", data)
    assert result is not None
    assert "Volare" in result
    assert "Dean Martin" in result
    assert "sta suonando" in result


def test_format_state_unavailable_returns_none():
    data = {"state": "unavailable", "attributes": {}}
    result = _format_state("switch.bar_kaffeemaschine_steckdose", data)
    assert result is None


def test_format_state_unknown_returns_none():
    data = {"state": "unknown", "attributes": {}}
    result = _format_state("lock.lock_ultra_8d3c", data)
    assert result is None


def test_format_state_standard_entity_uses_translations():
    data = {"state": "home", "attributes": {}}
    result = _format_state("person.florian_horner", data)
    assert result is not None
    assert "a casa" in result


def test_format_state_dad_joke_shows_text():
    data = {"state": "Why did the coffee file a police report? It got mugged!", "attributes": {}}
    result = _format_state("input_select.kaffee_dad_jokes", data)
    assert result is not None
    assert "mugged" in result
    assert '"' in result  # quoted


# ---------------------------------------------------------------------------
# _build_summary
# ---------------------------------------------------------------------------


def test_build_summary_includes_matching_entities():
    states = {
        "person.florian_horner": {"state": "home", "attributes": {}},
        "weather.forecast_home": {
            "state": "sunny",
            "attributes": {"temperature": 25, "temperature_unit": "°C"},
        },
    }
    result = _build_summary(states)
    assert "Florian" in result
    assert "Meteo" in result
    assert result.count("- ") == 2


def test_build_summary_excludes_non_matching_entities():
    states = {
        "sensor.random_thing_not_in_list": {"state": "42", "attributes": {}},
    }
    result = _build_summary(states)
    assert result == ""


def test_build_summary_empty_states():
    result = _build_summary({})
    assert result == ""


# ---------------------------------------------------------------------------
# fetch_home_context
# ---------------------------------------------------------------------------


def _mock_ha_response():
    """Build a mock HA API response with a couple of known entities."""
    return [
        {
            "entity_id": "person.florian_horner",
            "state": "home",
            "attributes": {"friendly_name": "Florian"},
        },
        {
            "entity_id": "weather.forecast_home",
            "state": "sunny",
            "attributes": {"temperature": 22, "temperature_unit": "°C"},
        },
    ]


@pytest.mark.asyncio
async def test_fetch_returns_cached_if_fresh():
    cache = HomeContext(
        raw_states={"person.florian_horner": {"state": "home", "attributes": {}}},
        summary="cached",
        timestamp=time.time(),
    )
    result = await fetch_home_context("http://ha:8123", "token", poll_interval=60.0, _cache=cache)
    assert result is cache


@pytest.mark.asyncio
async def test_fetch_calls_api_when_stale():
    stale_cache = HomeContext(summary="old", timestamp=time.time() - 120.0)

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = _mock_ha_response()

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("mammamiradio.ha_context.httpx.AsyncClient", return_value=mock_client):
        result = await fetch_home_context("http://ha:8123", "token", poll_interval=60.0, _cache=stale_cache)

    assert result is not stale_cache
    assert result.timestamp > stale_cache.timestamp
    assert "Florian" in result.summary


@pytest.mark.asyncio
async def test_fetch_returns_stale_cache_on_api_failure():
    stale_cache = HomeContext(summary="stale", timestamp=time.time() - 300.0)

    mock_client = AsyncMock()
    mock_client.get.side_effect = RuntimeError("connection refused")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("mammamiradio.ha_context.httpx.AsyncClient", return_value=mock_client):
        result = await fetch_home_context("http://ha:8123", "token", poll_interval=60.0, _cache=stale_cache)

    assert result is stale_cache


@pytest.mark.asyncio
async def test_fetch_returns_empty_on_failure_no_cache():
    mock_client = AsyncMock()
    mock_client.get.side_effect = RuntimeError("connection refused")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("mammamiradio.ha_context.httpx.AsyncClient", return_value=mock_client):
        result = await fetch_home_context("http://ha:8123", "token", poll_interval=60.0, _cache=None)

    assert result.summary == ""
    assert result.raw_states == {}
