"""Config-flow tests for the Mamma Mi Radio integration."""

from __future__ import annotations

from unittest.mock import patch

import aiohttp
from aioresponses import aioresponses
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mammamiradio.const import DOMAIN, NOW_PLAYING_PATH

from .conftest import load_payload

URL = f"http://local-mammamiradio:8000{NOW_PLAYING_PATH}"
USER_INPUT = {CONF_HOST: "local-mammamiradio", CONF_PORT: 8000}


async def test_user_flow_success(hass: HomeAssistant) -> None:
    """A reachable station creates a config entry."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with aioresponses() as mock, patch("custom_components.mammamiradio.async_setup_entry", return_value=True):
        mock.get(URL, status=200, payload=load_payload("music"))
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "local-mammamiradio"
    assert result["result"].unique_id == "local-mammamiradio:8000"


async def test_user_flow_cannot_connect_http_error(hass: HomeAssistant) -> None:
    """A non-200 read endpoint surfaces a recoverable cannot_connect error."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with aioresponses() as mock:
        mock.get(URL, status=503)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_cannot_connect_network_error(hass: HomeAssistant) -> None:
    """A transport error surfaces cannot_connect (not a crash)."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with aioresponses() as mock:
        mock.get(URL, exception=aiohttp.ClientConnectionError("down"))
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_unknown_error(hass: HomeAssistant) -> None:
    """An unexpected validation error surfaces as a generic, recoverable error."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with patch(
        "custom_components.mammamiradio.config_flow.MammaRadioConfigFlow._validate",
        side_effect=ValueError("boom"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_duplicate_aborts(hass: HomeAssistant) -> None:
    """The same host:port cannot be configured twice."""
    MockConfigEntry(domain=DOMAIN, unique_id="local-mammamiradio:8000", data=USER_INPUT).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
