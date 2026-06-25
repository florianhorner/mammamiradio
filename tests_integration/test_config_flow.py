"""Config-flow tests for the Mamma Mi Radio integration."""

from __future__ import annotations

from unittest.mock import patch

import aiohttp
import voluptuous as vol
from aioresponses import aioresponses
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mammamiradio.const import CONF_ADMIN_TOKEN, DOMAIN, NOW_PLAYING_PATH

from .conftest import load_payload

URL = f"http://local-mammamiradio:8000{NOW_PLAYING_PATH}"
USER_INPUT = {CONF_HOST: "local-mammamiradio", CONF_PORT: 8000}


def _form_defaults(result: dict) -> dict:
    """Extract each field's default value from a shown form's schema."""
    out: dict[str, object] = {}
    for key in result["data_schema"].schema:
        default = getattr(key, "default", vol.UNDEFINED)
        if default is not vol.UNDEFINED:
            out[str(key.schema)] = default() if callable(default) else default
    return out


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


async def test_second_entry_aborts_single_instance(hass: HomeAssistant) -> None:
    """Only one station can be configured (single_config_entry in the manifest)."""
    MockConfigEntry(domain=DOMAIN, unique_id="local-mammamiradio:8000", data=USER_INPUT).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_reconfigure_updates_entry_and_schedules_reload(hass: HomeAssistant) -> None:
    """Reconfigure edits connection data without creating a second entry."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="local-mammamiradio:8000", data=USER_INPUT)
    entry.add_to_hass(hass)
    new_input = {CONF_HOST: "mammamiradio", CONF_PORT: 8010, CONF_ADMIN_TOKEN: "new-token"}
    with aioresponses() as mock, patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload:
        mock.get(f"http://mammamiradio:8010{NOW_PLAYING_PATH}", status=200, payload=load_payload("music"))
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": entry.entry_id},
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(result["flow_id"], new_input)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == new_input
    assert entry.unique_id == "mammamiradio:8010"
    schedule_reload.assert_called_once_with(entry.entry_id)


async def test_reconfigure_rejects_duplicate_station(hass: HomeAssistant) -> None:
    """Reconfigure cannot move an entry onto another configured station."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="local-mammamiradio:8000", data=USER_INPUT)
    other = MockConfigEntry(domain=DOMAIN, unique_id="other:9000", data={CONF_HOST: "other", CONF_PORT: 9000})
    entry.add_to_hass(hass)
    other.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "other", CONF_PORT: 9000},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure_cannot_connect_keeps_form(hass: HomeAssistant) -> None:
    """Reconfigure validates reachability before saving the new host."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="local-mammamiradio:8000", data=USER_INPUT)
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    with aioresponses() as mock:
        mock.get(f"http://mammamiradio:8010{NOW_PLAYING_PATH}", status=503)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: "mammamiradio", CONF_PORT: 8010},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data == USER_INPUT
    # The re-shown form keeps what the operator just typed, not the stored host.
    defaults = _form_defaults(result)
    assert defaults[CONF_HOST] == "mammamiradio"
    assert defaults[CONF_PORT] == 8010
