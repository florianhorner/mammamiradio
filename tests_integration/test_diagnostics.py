"""Diagnostics tests for the Mamma Mi Radio integration."""

from __future__ import annotations

from aioresponses import aioresponses
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mammamiradio.const import CONF_ADMIN_TOKEN, DOMAIN, NOW_PLAYING_PATH
from custom_components.mammamiradio.diagnostics import async_get_config_entry_diagnostics

from .conftest import load_payload

BASE = "http://local-mammamiradio:8000"


async def test_diagnostics_redacts_admin_token_and_includes_status(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="local-mammamiradio:8000",
        data={CONF_HOST: "local-mammamiradio", CONF_PORT: 8000, CONF_ADMIN_TOKEN: "secret"},
    )
    entry.add_to_hass(hass)
    with aioresponses() as mock:
        mock.get(f"{BASE}{NOW_PLAYING_PATH}", status=200, payload=load_payload("music"), repeat=True)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entry"]["data"][CONF_ADMIN_TOKEN] == "**REDACTED**"
    assert diagnostics["station"]["title"] == "Volare"
    assert diagnostics["station"]["station_name"] == "Mamma Mi Radio"
    assert diagnostics["coordinator"]["consecutive_failures"] == 0
