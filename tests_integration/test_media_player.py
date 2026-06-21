"""Media-player entity tests for the Mamma Mi Radio integration.

These are the hands-off gate: they assert (headlessly, no dev HA) that the
entity registers on the canonical id, maps state correctly, advertises only
the controls the back end can honor (and only when authed), drives the right
endpoints, and falls back to the station logo for art.
"""

from __future__ import annotations

import aiohttp
import pytest
from aioresponses import aioresponses
from homeassistant.components.media_player import (
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mammamiradio.const import (
    CONF_ADMIN_TOKEN,
    DOMAIN,
    NOW_PLAYING_PATH,
    STATION_LOGO_URL,
)

from .conftest import load_payload

ENTITY = "media_player.mammamiradio"
BASE = "http://local-mammamiradio:8000"
URL = f"{BASE}{NOW_PLAYING_PATH}"


async def _setup(hass: HomeAssistant, mock: aioresponses, payload: dict, *, token: str | None = None):
    data = {CONF_HOST: "local-mammamiradio", CONF_PORT: 8000}
    if token is not None:
        data[CONF_ADMIN_TOKEN] = token
    entry = MockConfigEntry(domain=DOMAIN, unique_id="local-mammamiradio:8000", data=data)
    entry.add_to_hass(hass)
    mock.get(URL, status=200, payload=payload, repeat=True)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entity_registers_on_canonical_id(hass: HomeAssistant) -> None:
    """The registered entity lands on media_player.mammamiradio (the freed ghost id)."""
    with aioresponses() as mock:
        await _setup(hass, mock, load_payload("music"))
    state = hass.states.get(ENTITY)
    assert state is not None
    assert state.state == MediaPlayerState.PLAYING


@pytest.mark.parametrize(
    ("payload_name", "expected"),
    [
        ("music", MediaPlayerState.PLAYING),
        ("banter", MediaPlayerState.PLAYING),
        ("ad", MediaPlayerState.PLAYING),
        ("stopped", MediaPlayerState.IDLE),
        ("post_restart", MediaPlayerState.IDLE),
        ("empty_queue", MediaPlayerState.BUFFERING),
    ],
)
async def test_state_mapping(hass: HomeAssistant, payload_name: str, expected: str) -> None:
    with aioresponses() as mock:
        await _setup(hass, mock, load_payload(payload_name))
    assert hass.states.get(ENTITY).state == expected


async def test_media_attributes(hass: HomeAssistant) -> None:
    with aioresponses() as mock:
        await _setup(hass, mock, load_payload("music"))
    attrs = hass.states.get(ENTITY).attributes
    assert attrs["media_title"] == "Volare"
    assert attrs["media_artist"] == "Domenico Modugno"
    assert attrs["entity_picture"] == "https://example.test/art.jpg"


async def test_artwork_falls_back_to_station_logo(hass: HomeAssistant) -> None:
    """A voice segment with no cover shows the station logo, never blank/stale art."""
    with aioresponses() as mock:
        await _setup(hass, mock, load_payload("banter"))
    assert hass.states.get(ENTITY).attributes["entity_picture"] == STATION_LOGO_URL


async def test_controls_advertised_when_reachable_without_token(hass: HomeAssistant) -> None:
    """On HA OS the add-on trusts the bridge, so controls work without a token --
    advertise them rather than hiding working buttons."""
    with aioresponses() as mock:
        await _setup(hass, mock, load_payload("music"), token=None)
    features = hass.states.get(ENTITY).attributes["supported_features"]
    expected = MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.STOP | MediaPlayerEntityFeature.NEXT_TRACK
    assert features == expected
    # Dead-feature guard: never advertise what the back end can't do.
    assert not features & MediaPlayerEntityFeature.VOLUME_SET
    assert not features & MediaPlayerEntityFeature.PAUSE
    assert not features & MediaPlayerEntityFeature.PREVIOUS_TRACK


async def test_token_does_not_change_features(hass: HomeAssistant) -> None:
    """A configured token changes auth, not which controls are offered."""
    with aioresponses() as mock:
        await _setup(hass, mock, load_payload("music"), token="secret")
    features = hass.states.get(ENTITY).attributes["supported_features"]
    expected = MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.STOP | MediaPlayerEntityFeature.NEXT_TRACK
    assert features == expected


@pytest.mark.parametrize("payload_name", ["stopped", "empty_queue"])
async def test_next_hidden_when_not_on_air(hass: HomeAssistant, payload_name: str) -> None:
    """Skip is meaningless while stopped or while the queue is still filling."""
    with aioresponses() as mock:
        await _setup(hass, mock, load_payload(payload_name))
    features = hass.states.get(ENTITY).attributes["supported_features"]
    assert features & MediaPlayerEntityFeature.PLAY
    assert features & MediaPlayerEntityFeature.STOP
    assert not features & MediaPlayerEntityFeature.NEXT_TRACK


@pytest.mark.parametrize(
    ("service", "path"),
    [("media_play", "/api/resume"), ("media_stop", "/api/stop"), ("media_next_track", "/api/skip")],
)
async def test_controls_post_with_token_header(hass: HomeAssistant, service: str, path: str) -> None:
    with aioresponses() as mock:
        await _setup(hass, mock, load_payload("music"), token="secret")
        mock.post(f"{BASE}{path}", status=200, repeat=True)
        mock.get(URL, status=200, payload=load_payload("music"), repeat=True)
        await hass.services.async_call("media_player", service, {"entity_id": ENTITY}, blocking=True)
        posted = [
            call
            for (method, url), calls in mock.requests.items()
            if method == "POST" and str(url).endswith(path)
            for call in calls
        ]
    assert posted, f"expected a POST to {path}"
    assert posted[0].kwargs["headers"]["X-Radio-Admin-Token"] == "secret"


async def test_coordinator_failure_marks_unavailable(hass: HomeAssistant) -> None:
    """A station that can't be reached at setup time leaves no usable entity."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="local-mammamiradio:8000",
        data={CONF_HOST: "local-mammamiradio", CONF_PORT: 8000},
    )
    entry.add_to_hass(hass)
    with aioresponses() as mock:
        mock.get(URL, exception=aiohttp.ClientConnectionError("down"), repeat=True)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    # First refresh failed -> ConfigEntryNotReady -> no live entity state.
    state = hass.states.get(ENTITY)
    assert state is None or state.state == "unavailable"


async def test_control_without_token_sends_no_header(hass: HomeAssistant) -> None:
    with aioresponses() as mock:
        await _setup(hass, mock, load_payload("music"), token=None)
        mock.post(f"{BASE}/api/resume", status=200, repeat=True)
        mock.get(URL, status=200, payload=load_payload("music"), repeat=True)
        await hass.services.async_call("media_player", "media_play", {"entity_id": ENTITY}, blocking=True)
        posted = [call for (method, _url), calls in mock.requests.items() if method == "POST" for call in calls]
    assert posted
    assert "X-Radio-Admin-Token" not in (posted[0].kwargs.get("headers") or {})


async def test_control_failure_raises_with_a_way_out(hass: HomeAssistant) -> None:
    """A failed control surfaces a clear error, never a silent no-op."""
    with aioresponses() as mock:
        await _setup(hass, mock, load_payload("music"), token="secret")
        mock.post(f"{BASE}/api/resume", status=500, repeat=True)
        mock.get(URL, status=200, payload=load_payload("music"), repeat=True)
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call("media_player", "media_play", {"entity_id": ENTITY}, blocking=True)


@pytest.mark.parametrize(("payload_name", "expected"), [("music", "music"), ("banter", "channel")])
async def test_media_content_type(hass: HomeAssistant, payload_name: str, expected: str) -> None:
    with aioresponses() as mock:
        await _setup(hass, mock, load_payload(payload_name))
    assert hass.states.get(ENTITY).attributes["media_content_type"] == expected


async def test_media_duration(hass: HomeAssistant) -> None:
    with aioresponses() as mock:
        await _setup(hass, mock, load_payload("music"))
    assert hass.states.get(ENTITY).attributes["media_duration"] == 210
