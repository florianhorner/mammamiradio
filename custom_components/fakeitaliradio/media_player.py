"""Media player entity for Fake Italian Radio."""

from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import urlparse

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=10)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the media player from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [FakeItaliRadioPlayer(hass, data["host"], data["port"], data.get("is_addon", False), entry)]
    )


class FakeItaliRadioPlayer(MediaPlayerEntity):
    """Representation of the Fake Italian Radio station as a media player."""

    _attr_has_entity_name = True
    _attr_name = "Radio Italì"
    _attr_icon = "mdi:radio"
    _attr_media_content_type = MediaType.MUSIC
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.PAUSE
    )

    def __init__(
        self, hass: HomeAssistant, host: str, port: int, is_addon: bool, entry: ConfigEntry
    ) -> None:
        """Initialize the media player."""
        self.hass = hass
        self._host = host
        self._port = port
        self._is_addon = is_addon
        self._attr_unique_id = f"fakeitaliradio_{entry.entry_id}"
        self._status: dict | None = None
        self._available = False

    @property
    def available(self) -> bool:
        """Return if the player is available."""
        return self._available

    @property
    def state(self) -> MediaPlayerState:
        """Return the state of the player."""
        if not self._available or not self._status:
            return MediaPlayerState.OFF
        now = self._status.get("now_streaming")
        if now and now.get("type") not in (None, "skipping"):
            return MediaPlayerState.PLAYING
        return MediaPlayerState.IDLE

    @property
    def media_title(self) -> str | None:
        """Return the title of the current media."""
        if not self._status:
            return None
        now = self._status.get("now_streaming")
        if now:
            return now.get("label", "")
        return None

    @property
    def media_content_id(self) -> str | None:
        """Return the stream URL for use with media_player.play_media on other players."""
        if self._is_addon:
            # Use HA's configured URL with direct mapped port for external players
            base = getattr(self.hass.config, "internal_url", None) or ""
            if base:
                parsed = urlparse(base)
                return f"http://{parsed.hostname}:8099/stream"
            return None
        if "://" in self._host:
            return f"{self._host}/stream"
        return f"http://{self._host}:{self._port}/stream"

    async def async_update(self) -> None:
        """Fetch latest status from the radio server."""
        try:
            url = self._host if "://" in self._host else f"http://{self._host}:{self._port}"
            session = async_get_clientsession(self.hass)
            async with session.get(
                f"{url}/public-status", timeout=5.0
            ) as resp:
                if resp.status == 200:
                    self._status = await resp.json()
                    self._available = True
                else:
                    self._available = False
        except Exception:
            self._available = False

    async def async_media_play(self) -> None:
        """Play is a no-op — the station is always streaming."""

    async def async_media_stop(self) -> None:
        """Stop is a no-op — the station is always streaming."""

    async def async_media_pause(self) -> None:
        """Pause is a no-op — the station is always streaming."""
