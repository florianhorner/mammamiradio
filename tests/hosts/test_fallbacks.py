"""Mode-selection coverage for deterministic host fallback copy."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import ChaosSubtype, StationState, Track
from mammamiradio.hosts.fallbacks import (
    CHAOS_NORMAL_STOCK_LINES,
    CHAOS_STOCK_LINES,
    chaos_solo_recovery_lines,
    chaos_stock_lines,
)
from mammamiradio.hosts.scriptwriter import _chaos_stock_exchange, write_banter


@pytest.fixture()
def config():
    return load_config()


@pytest.mark.parametrize("subtype", list(ChaosSubtype))
@pytest.mark.parametrize(
    ("super_italian_mode", "station_language", "expected_lines"),
    [
        (False, "it", CHAOS_NORMAL_STOCK_LINES),
        (True, "it", CHAOS_STOCK_LINES),
    ],
)
def test_chaos_stock_map_covers_each_subtype_in_the_active_spoken_mode(
    subtype,
    super_italian_mode,
    station_language,
    expected_lines,
):
    selected = chaos_stock_lines(
        super_italian_mode=super_italian_mode,
        station_language=station_language,
    )

    assert selected[subtype] == expected_lines[subtype]


@pytest.mark.parametrize("subtype", list(ChaosSubtype))
@pytest.mark.parametrize(
    ("super_italian_mode", "station_language", "expected_lines"),
    [
        (False, "it", CHAOS_NORMAL_STOCK_LINES),
        (True, "it", CHAOS_STOCK_LINES),
    ],
)
def test_chaos_stock_exchange_uses_each_subtype_in_the_active_spoken_mode(
    config,
    subtype,
    super_italian_mode,
    station_language,
    expected_lines,
):
    config.super_italian_mode = super_italian_mode
    config.station.language = station_language

    exchange = _chaos_stock_exchange(config, subtype)

    assert [text for _host, text in exchange] == expected_lines[subtype]


def test_chaos_stock_uses_normal_mode_when_super_italian_has_non_italian_station_language():
    selected = chaos_stock_lines(super_italian_mode=True, station_language="en")

    # Hot reload replaces the module-level dictionaries. The contract is the
    # selected copy, not object identity with a pre-reload import.
    assert selected == CHAOS_NORMAL_STOCK_LINES


@pytest.mark.parametrize(
    ("super_italian_mode", "station_language", "expected_lines"),
    [
        (False, "it", ["The chaos is real, but we can land this.", "Music. We keep moving."]),
        (True, "it", ["Il caos è reale, ma chiudiamo il punto.", "Musica. Continuiamo."]),
        (True, "en", ["The chaos is real, but we can land this.", "Music. We keep moving."]),
    ],
)
def test_single_host_chaos_recovery_follows_the_spoken_mode(
    config,
    super_italian_mode,
    station_language,
    expected_lines,
):
    config.super_italian_mode = super_italian_mode
    config.station.language = station_language
    config.hosts = [config.hosts[0]]

    # The existing solo path is needed when a stock exchange contains a
    # deliberate cut-in, which a single host cannot answer as another speaker.
    exchange = _chaos_stock_exchange(config, ChaosSubtype.ABANDONED_STORM)

    assert [text for _host, text in exchange] == expected_lines
    assert [text for _host, text in exchange] == chaos_solo_recovery_lines(
        super_italian_mode=super_italian_mode,
        station_language=station_language,
    )


@pytest.mark.asyncio
async def test_normal_mode_terminal_cutoff_recovers_with_english_chaos_stock(config):
    """A rejected generated Chaos exchange must preserve the active spoken mode."""
    config.super_italian_mode = False
    config.station.language = "it"
    config.anthropic_api_key = "test-key"
    config.openai_api_key = ""
    first_host, second_host = config.hosts[:2]
    state = StationState(playlist=[Track(title="Test", artist="Artist", duration_ms=180_000)])
    terminal_cutoff = {
        "lines": [
            {"host": first_host.name, "text": "This track had a real pulse."},
            {"host": second_host.name, "text": "But wait—"},
        ],
        "new_joke": None,
    }

    with (
        patch(
            "mammamiradio.hosts.scriptwriter._generate_json_response_with_language_guard",
            new_callable=AsyncMock,
            return_value=terminal_cutoff,
        ),
        patch(
            "mammamiradio.hosts.scriptwriter._chaos_stock_exchange",
            wraps=_chaos_stock_exchange,
        ) as chaos_stock_exchange,
    ):
        exchange, commit = await write_banter(state, config, chaos_subtype=ChaosSubtype.FOURTH_WALL)

    assert commit is None
    assert [text for _host, text in exchange] == CHAOS_NORMAL_STOCK_LINES[ChaosSubtype.FOURTH_WALL]
    chaos_stock_exchange.assert_called_once_with(config, ChaosSubtype.FOURTH_WALL)
    assert state.chaos_script_fallbacks == 1
    assert state.chaos_last_degraded_reason == "script_fallback"
