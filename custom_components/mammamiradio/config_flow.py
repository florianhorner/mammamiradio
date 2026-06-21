"""Config flow for Mamma Mi Radio."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ADMIN_TOKEN,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DOMAIN,
    HTTP_TIMEOUT,
    NOW_PLAYING_PATH,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Optional(CONF_ADMIN_TOKEN): str,
    }
)


class MammaRadioConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Mamma Mi Radio."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step: ask for host/port and an optional admin token."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()
            try:
                await self._validate(host, port)
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating the station")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=f"Mamma Mi Radio ({host})", data=user_input)

        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors)

    async def _validate(self, host: str, port: int) -> None:
        """Confirm the station's read contract is reachable (unauthenticated GET)."""
        session = async_get_clientsession(self.hass)
        url = f"http://{host}:{port}{NOW_PLAYING_PATH}"
        try:
            async with asyncio.timeout(HTTP_TIMEOUT):
                async with session.get(url) as resp:
                    if resp.status != 200:
                        raise CannotConnectError
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnectError from err


class CannotConnectError(Exception):
    """Error to indicate the station's read endpoint is unreachable."""
