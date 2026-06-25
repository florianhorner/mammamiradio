"""Config flow for Mamma Mi Radio."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow
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

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigFlowResult
else:
    ConfigFlowResult = dict[str, Any]


def _schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build the setup/reconfigure schema with current values as defaults."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, DEFAULT_HOST)): str,
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): int,
            vol.Optional(CONF_ADMIN_TOKEN, default=defaults.get(CONF_ADMIN_TOKEN, "")): str,
        }
    )


STEP_USER_DATA_SCHEMA = _schema()


def _entry_unique_id(data: dict[str, Any]) -> str:
    """Return the stable-enough unique id used by the existing integration."""
    return f"{data[CONF_HOST]}:{data[CONF_PORT]}"


def _entry_title(data: dict[str, Any]) -> str:
    """Return the visible config-entry title."""
    return f"Mamma Mi Radio ({data[CONF_HOST]})"


def _normalized_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Normalize form input before persisting it."""
    data = {
        CONF_HOST: str(user_input[CONF_HOST]).strip(),
        CONF_PORT: int(user_input[CONF_PORT]),
    }
    token = str(user_input.get(CONF_ADMIN_TOKEN) or "").strip()
    if token:
        data[CONF_ADMIN_TOKEN] = token
    return data


def _reconfigure_entry(flow: ConfigFlow) -> ConfigEntry:
    """Return the config entry being reconfigured across HA versions."""
    if hasattr(flow, "_get_reconfigure_entry"):
        return flow._get_reconfigure_entry()  # type: ignore[attr-defined]
    entry_id = flow.context.get("entry_id")
    entry = flow.hass.config_entries.async_get_entry(entry_id) if entry_id else None
    if entry is None:
        raise ValueError("Missing config entry for reconfigure flow")
    return entry


class MammaRadioConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Mamma Mi Radio."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step: ask for host/port and an optional admin token."""
        errors: dict[str, str] = {}

        if user_input is not None:
            data = _normalized_input(user_input)
            await self.async_set_unique_id(_entry_unique_id(data))
            self._abort_if_unique_id_configured()
            try:
                await self._validate(data[CONF_HOST], data[CONF_PORT])
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating the station")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=_entry_title(data), data=data)

        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Let the user update station connection details without deleting the entry."""
        entry = _reconfigure_entry(self)
        errors: dict[str, str] = {}

        if user_input is not None:
            data = _normalized_input(user_input)
            unique_id = _entry_unique_id(data)
            duplicate = next(
                (
                    current
                    for current in self._async_current_entries()
                    if current.entry_id != entry.entry_id and current.unique_id == unique_id
                ),
                None,
            )
            if duplicate is not None:
                return self.async_abort(reason="already_configured")
            try:
                await self._validate(data[CONF_HOST], data[CONF_PORT])
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating the station")
                errors["base"] = "unknown"
            else:
                updated = self.hass.config_entries.async_update_entry(
                    unique_id=unique_id,
                    entry=entry,
                    title=_entry_title(data),
                    data=data,
                )
                if updated:
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

        # On a failed submit re-show what the operator just typed (not the stored
        # values), so a transient cannot_connect doesn't silently revert their edit.
        defaults = user_input if user_input is not None else dict(entry.data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(defaults),
            errors=errors,
        )

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
