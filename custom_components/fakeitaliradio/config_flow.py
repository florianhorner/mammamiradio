"""Config flow for Fake Italian Radio integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8099

_ADDON_SLUG = "fakeitaliradio"


class FakeItaliRadioConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Fake Italian Radio."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        # Deduplicate: only one instance allowed
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        # Try auto-detecting the addon first
        if user_input is None:
            addon_url = await self._discover_addon_url()
            if addon_url and await self._try_connection(addon_url):
                return self.async_create_entry(
                    title="Radio Italì (addon)",
                    data={"host": addon_url, "port": 8099, "is_addon": True},
                )

        if user_input is not None:
            host = user_input["host"]
            port = user_input["port"]
            url = f"http://{host}:{port}" if "://" not in host else host
            if await self._try_connection(url):
                return self.async_create_entry(
                    title="Radio Italì",
                    data={"host": url, "port": port, "is_addon": False},
                )
            errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("host", default=DEFAULT_HOST): str,
                    vol.Required("port", default=DEFAULT_PORT): int,
                }
            ),
            errors=errors,
        )

    async def _discover_addon_url(self) -> str | None:
        """Discover the addon URL via the Supervisor API."""
        try:
            hassio = self.hass.components.hassio
            addon_info = await hassio.async_get_addon_info(self.hass, _ADDON_SLUG)
            if addon_info and addon_info.get("state") == "started":
                hostname = addon_info.get("hostname", "")
                if hostname:
                    return f"http://{hostname}:8099"
        except Exception:
            _LOGGER.debug("Could not discover addon via Supervisor API")
        return None

    async def _try_connection(self, url: str) -> bool:
        """Test connection to a fakeitaliradio instance."""
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(
                f"{url}/public-status", timeout=5.0
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
