"""Setup/unload/remove lifecycle tests for the Mamma Mi Radio integration."""

from __future__ import annotations

from aioresponses import aioresponses
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mammamiradio.const import (
    DOMAIN,
    ISSUE_ADMIN_TOKEN_REJECTED,
    ISSUE_LEGACY_MEDIA_PLAYER_PUSH_CONFLICT,
    NOW_PLAYING_PATH,
)
from custom_components.mammamiradio.repairs import create_issue

from .conftest import load_payload

URL = f"http://local-mammamiradio:8000{NOW_PLAYING_PATH}"


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="local-mammamiradio:8000",
        data={CONF_HOST: "local-mammamiradio", CONF_PORT: 8000},
    )
    entry.add_to_hass(hass)
    return entry


async def test_remove_entry_clears_repairs(hass: HomeAssistant) -> None:
    """Removing the integration clears any Repairs issue it raised (no orphan card)."""
    registry = ir.async_get(hass)
    entry = _entry(hass)
    with aioresponses() as mock:
        mock.get(URL, status=200, payload=load_payload("music"), repeat=True)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    create_issue(hass, ISSUE_ADMIN_TOKEN_REJECTED)
    assert registry.async_get_issue(DOMAIN, ISSUE_ADMIN_TOKEN_REJECTED) is not None

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get_issue(DOMAIN, ISSUE_ADMIN_TOKEN_REJECTED) is None


async def test_stale_legacy_issue_cleared_when_station_down(hass: HomeAssistant) -> None:
    """A stale ghost-conflict issue clears before first refresh even if the add-on is down."""
    registry = ir.async_get(hass)
    create_issue(hass, ISSUE_LEGACY_MEDIA_PLAYER_PUSH_CONFLICT)
    entry = _entry(hass)

    with aioresponses() as mock:
        mock.get(URL, status=503, repeat=True)
        # Setup does not complete (ConfigEntryNotReady), but the pre-refresh clear runs.
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert registry.async_get_issue(DOMAIN, ISSUE_LEGACY_MEDIA_PLAYER_PUSH_CONFLICT) is None
        # Stop the scheduled retry so test teardown stays clean.
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
