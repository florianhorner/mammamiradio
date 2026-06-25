"""The Mamma Mi Radio integration.

Exposes the AI radio station running as a separate add-on as a first-class
Home Assistant ``media_player`` entity: live now-playing state plus the three
transport controls the back end can actually honor (play / stop / next).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeAlias

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    ISSUE_ADMIN_TOKEN_REJECTED,
    ISSUE_LEGACY_MEDIA_PLAYER_PUSH_CONFLICT,
    ISSUE_STATION_UNREACHABLE,
)
from .coordinator import MammaRadioCoordinator
from .repairs import create_issue, delete_issue

PLATFORMS: list[Platform] = [Platform.MEDIA_PLAYER]

# Plain alias (not the 3.12 `type` statement) so the repo's 3.11 tooling parses
# it; functionally identical on HA's 3.13 runtime.
if TYPE_CHECKING:
    MammaRadioConfigEntry: TypeAlias = ConfigEntry[MammaRadioCoordinator]
else:
    MammaRadioConfigEntry = ConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: MammaRadioConfigEntry) -> bool:
    """Set up Mamma Mi Radio from a config entry."""
    base_url = f"http://{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}"
    coordinator = MammaRadioCoordinator(hass, entry, base_url)

    ghost_state = hass.states.get(f"media_player.{DOMAIN}")
    if ghost_state is not None:
        create_issue(hass, ISSUE_LEGACY_MEDIA_PLAYER_PUSH_CONFLICT)
    else:
        # Clear a stale conflict before the first refresh: that call raises
        # ConfigEntryNotReady while the add-on is still booting, which would
        # otherwise skip the clear on every retry and leave a false warning up.
        delete_issue(hass, ISSUE_LEGACY_MEDIA_PLAYER_PUSH_CONFLICT)

    # Raises ConfigEntryNotReady if the add-on isn't up yet, so HA retries
    # setup rather than failing permanently (the add-on can boot after Core).
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MammaRadioConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: MammaRadioConfigEntry) -> None:
    """Clear our Repairs issues when the integration is removed.

    HA does not auto-delete an integration's issues on entry removal, and ours
    are non-fixable, so without this an active warning would linger forever with
    no entity behind it and no way for the operator to dismiss it.
    """
    for issue_id in (
        ISSUE_STATION_UNREACHABLE,
        ISSUE_ADMIN_TOKEN_REJECTED,
        ISSUE_LEGACY_MEDIA_PLAYER_PUSH_CONFLICT,
    ):
        delete_issue(hass, issue_id)
