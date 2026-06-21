"""Coordinator + RadioStatus parsing tests."""

from __future__ import annotations

import aiohttp
import pytest
from aioresponses import aioresponses
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mammamiradio.const import DOMAIN, NOW_PLAYING_PATH
from custom_components.mammamiradio.coordinator import (
    MammaRadioCoordinator,
    RadioStatus,
    _as_float,
)

from .conftest import load_payload

URL = f"http://h:8000{NOW_PLAYING_PATH}"


def _coordinator(hass: HomeAssistant) -> MammaRadioCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    return MammaRadioCoordinator(hass, entry, "http://h:8000")


async def test_update_failed_on_http_error(hass: HomeAssistant) -> None:
    with aioresponses() as mock:
        mock.get(URL, status=503)
        with pytest.raises(UpdateFailed):
            await _coordinator(hass)._async_update_data()


async def test_update_failed_on_non_dict_payload(hass: HomeAssistant) -> None:
    with aioresponses() as mock:
        mock.get(URL, status=200, payload=[1, 2, 3])
        with pytest.raises(UpdateFailed):
            await _coordinator(hass)._async_update_data()


async def test_update_failed_on_network_error(hass: HomeAssistant) -> None:
    with aioresponses() as mock:
        mock.get(URL, exception=aiohttp.ClientConnectionError("down"))
        with pytest.raises(UpdateFailed):
            await _coordinator(hass)._async_update_data()


def test_as_float_coercion() -> None:
    assert _as_float(True) is None  # bool is not a real number here
    assert _as_float(None) is None
    assert _as_float("abc") is None
    assert _as_float("3.5") == 3.5
    assert _as_float(7) == 7.0


def test_from_payload_defends_every_field() -> None:
    empty = RadioStatus.from_payload({})
    assert empty.session_state == "stopped"
    assert empty.station_name == "Mamma Mi Radio"
    assert empty.title is None

    music = RadioStatus.from_payload(load_payload("music"))
    assert music.session_state == "live"
    assert music.title == "Volare"
    assert music.artist == "Domenico Modugno"
    assert music.duration == 210.0
