"""Media player entity for Mamma Mi Radio.

The entity models the station itself (the add-on owns the audio output, like the
VLC integration), not a speaker. It advertises only the three transports the
back end can honor -- play, stop, next -- whenever the station is reachable. On
Home Assistant OS the add-on trusts the Supervisor network, so controls work
without a token; a remote/Docker setup uses the admin token, and a hard failure
surfaces a clear error rather than a dead button. ``NEXT_TRACK`` is hidden while
not on air because ``/api/skip`` has no meaning then.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import aiohttp
from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import MammaRadioConfigEntry
from .const import (
    ADMIN_TOKEN_HEADER,
    CONF_ADMIN_TOKEN,
    DOMAIN,
    ENDPOINT_NEXT,
    ENDPOINT_PLAY,
    ENDPOINT_STOP,
    HTTP_TIMEOUT,
    ISSUE_ADMIN_TOKEN_REJECTED,
    STATION_LOGO_URL,
)
from .coordinator import MammaRadioCoordinator, RadioStatus
from .repairs import create_issue, delete_issue

_LOGGER = logging.getLogger(__name__)

# session_state (from the read contract) -> HA media-player state.
_STATE_MAP: dict[str, MediaPlayerState] = {
    "live": MediaPlayerState.PLAYING,
    "stopped": MediaPlayerState.IDLE,
    "empty_queue": MediaPlayerState.BUFFERING,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MammaRadioConfigEntry,
    async_add_entities: Callable[[list[MediaPlayerEntity]], None],
) -> None:
    """Set up the Mamma Mi Radio media player from a config entry."""
    async_add_entities([MammaRadioMediaPlayer(entry.runtime_data, entry)])


class MammaRadioMediaPlayer(CoordinatorEntity[MammaRadioCoordinator], MediaPlayerEntity):
    """The station as a Home Assistant media player."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.RECEIVER

    def __init__(self, coordinator: MammaRadioCoordinator, entry: MammaRadioConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = entry.entry_id
        # Suggest the canonical object_id so the entity lands on
        # media_player.mammamiradio (the id the legacy ghost used, now freed by
        # the add-on's delete-first purge) rather than HA's name-derived
        # media_player.mamma_mi_radio. has_entity_name still gives the friendly
        # name "Mamma Mi Radio". A second station instance falls back to _2.
        self.entity_id = f"media_player.{DOMAIN}"
        self._base_url = f"http://{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}"
        self._admin_token: str = entry.data.get(CONF_ADMIN_TOKEN) or ""
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Mamma Mi Radio",
            manufacturer="Mamma Mi Radio",
            model="AI Radio Station",
        )

    @property
    def _status(self) -> RadioStatus | None:
        return self.coordinator.data

    @property
    def state(self) -> MediaPlayerState:
        status = self._status
        if status is None:
            return MediaPlayerState.IDLE
        return _STATE_MAP.get(status.session_state, MediaPlayerState.IDLE)

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        # Advertise transports whenever the station is reachable. On Home
        # Assistant OS the add-on trusts the Supervisor network, so controls work
        # without a token; a remote/Docker setup needs the admin token, and a
        # missing/wrong one surfaces a clear error when a control is used (see
        # _post_control) rather than a dead button. NEXT only when on air -- skip
        # is meaningless while stopped or while the queue is still filling.
        if self._status is None:
            return MediaPlayerEntityFeature(0)
        features = MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.STOP
        if self.state is MediaPlayerState.PLAYING:
            features |= MediaPlayerEntityFeature.NEXT_TRACK
        return features

    @property
    def media_content_type(self) -> MediaType | str | None:
        status = self._status
        if status is None:
            return None
        return MediaType.MUSIC if status.segment_type == "music" else MediaType.CHANNEL

    @property
    def media_title(self) -> str | None:
        status = self._status
        if status is None:
            return None
        return status.title or status.station_name

    @property
    def media_artist(self) -> str | None:
        status = self._status
        if status is None:
            return None
        if status.segment_type == "music":
            return status.artist or status.station_name
        return status.station_name

    @property
    def media_image_url(self) -> str | None:
        # Artwork when the segment has a real cover; otherwise the station logo,
        # so a voice/ad/idle segment never leaves the prior track's art on screen.
        status = self._status
        artwork = (status.artwork or "").strip() if status else ""
        if artwork.startswith(("http://", "https://")):
            return artwork
        return STATION_LOGO_URL

    @property
    def media_image_remotely_accessible(self) -> bool:
        return True

    @property
    def media_duration(self) -> int | None:
        status = self._status
        if status is None or status.duration is None:
            return None
        return int(status.duration)

    @property
    def media_position(self) -> int | None:
        status = self._status
        if status is None or status.started_at is None:
            return None
        if self.state is not MediaPlayerState.PLAYING:
            return None
        elapsed = dt_util.utcnow().timestamp() - status.started_at
        return max(0, int(elapsed))

    @property
    def media_position_updated_at(self):
        if self.media_position is None:
            return None
        return dt_util.utcnow()

    async def async_media_play(self) -> None:
        await self._post_control(ENDPOINT_PLAY)

    async def async_media_stop(self) -> None:
        await self._post_control(ENDPOINT_STOP)

    async def async_media_next_track(self) -> None:
        await self._post_control(ENDPOINT_NEXT)

    async def _post_control(self, path: str) -> None:
        """POST an admin control, then refresh so the state catches up.

        Raises HomeAssistantError (with a way out) on a hard failure so the user
        sees why a tap did nothing instead of a silent no-op.
        """
        session = async_get_clientsession(self.hass)
        headers = {ADMIN_TOKEN_HEADER: self._admin_token} if self._admin_token else {}
        try:
            async with asyncio.timeout(HTTP_TIMEOUT):
                async with session.post(f"{self._base_url}{path}", headers=headers) as resp:
                    if resp.status in (401, 403):
                        create_issue(self.hass, ISSUE_ADMIN_TOKEN_REJECTED)
                        raise HomeAssistantError(
                            "The station did not accept that. Set the add-on's admin "
                            "token in the integration reconfigure screen, then try again.",
                            translation_domain=DOMAIN,
                            translation_key=ISSUE_ADMIN_TOKEN_REJECTED,
                        )
                    # Auth succeeded (any non-401/403 response), so clear a stale
                    # token-rejected repair even if the body status is an error.
                    delete_issue(self.hass, ISSUE_ADMIN_TOKEN_REJECTED)
                    if resp.status >= 400:
                        raise HomeAssistantError(
                            "The station could not do that just now. Give it a few seconds and try again.",
                            translation_domain=DOMAIN,
                            translation_key="control_failed",
                        )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HomeAssistantError(
                "Could not reach the station. Check the add-on is running, then try again.",
                translation_domain=DOMAIN,
                translation_key="station_unreachable_control",
            ) from err
        await self.coordinator.async_request_refresh()
