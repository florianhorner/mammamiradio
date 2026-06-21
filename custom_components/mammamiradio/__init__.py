"""The Mamma Mi Radio integration.

Exposes the AI radio station running as a separate add-on as a first-class
Home Assistant ``media_player`` entity: live now-playing state plus the three
transport controls the back end can actually honor (play / stop / next).
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant

from .coordinator import MammaRadioCoordinator

PLATFORMS: list[Platform] = [Platform.MEDIA_PLAYER]

# Plain alias (not the 3.12 `type` statement) so the repo's 3.11 tooling parses
# it; functionally identical on HA's 3.13 runtime.
MammaRadioConfigEntry = ConfigEntry[MammaRadioCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: MammaRadioConfigEntry) -> bool:
    """Set up Mamma Mi Radio from a config entry."""
    base_url = f"http://{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}"
    coordinator = MammaRadioCoordinator(hass, entry, base_url)

    # Raises ConfigEntryNotReady if the add-on isn't up yet, so HA retries
    # setup rather than failing permanently (the add-on can boot after Core).
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MammaRadioConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
