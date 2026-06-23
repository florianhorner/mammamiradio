"""Media Source support for Mamma Mi Radio.

This exposes the station's live MP3 stream to Home Assistant's media browser so
Music Assistant / Follow Me Music style automations can hand the radio stream to
real speaker entities.  The media_player entity remains the station control
surface; this module is just the playable stream source for other players.
"""

from __future__ import annotations

from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source import BrowseMediaSource, MediaSource, MediaSourceItem, PlayMedia
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from .const import DOMAIN, STREAM_PATH

LIVE_IDENTIFIER = "live"
LIVE_TITLE = "Mamma Mi Radio Live"


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Return the Mamma Mi Radio media source."""
    return MammaRadioMediaSource(hass)


class MammaRadioMediaSource(MediaSource):
    """Home Assistant media-source adapter for the live station stream."""

    name = "Mamma Mi Radio"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the media source."""
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve ``media-source://mammamiradio/live`` to the add-on stream URL."""
        if item.identifier != LIVE_IDENTIFIER:
            raise ValueError(f"Unknown Mamma Mi Radio media source: {item.identifier}")
        return PlayMedia(self._stream_url(), "audio/mpeg")

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Expose a one-item media browser tree containing the live stream."""
        if item.identifier not in (None, "", LIVE_IDENTIFIER):
            raise ValueError(f"Unknown Mamma Mi Radio media source: {item.identifier}")

        if item.identifier == LIVE_IDENTIFIER:
            return BrowseMediaSource(
                domain=DOMAIN,
                identifier=LIVE_IDENTIFIER,
                media_class=MediaClass.MUSIC,
                media_content_type=MediaType.MUSIC,
                title=LIVE_TITLE,
                can_play=True,
                can_expand=False,
                thumbnail=None,
                children_media_class=None,
                children=None,
            )

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.MUSIC,
            title="Mamma Mi Radio",
            can_play=False,
            can_expand=True,
            thumbnail=None,
            children_media_class=MediaClass.MUSIC,
            children=[
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=LIVE_IDENTIFIER,
                    media_class=MediaClass.MUSIC,
                    media_content_type=MediaType.MUSIC,
                    title=LIVE_TITLE,
                    can_play=True,
                    can_expand=False,
                    thumbnail=None,
                    children_media_class=None,
                    children=None,
                )
            ],
        )

    def _stream_url(self) -> str:
        """Return the first configured station stream URL.

        Multiple config entries are uncommon; media-source identifiers are
        intentionally stable and entry-independent, so the first loaded entry is
        the canonical source.  If HA asks before an entry is loaded, fall back to
        the add-on's Supervisor DNS defaults used by the config flow.
        """
        entries = self.hass.config_entries.async_entries(DOMAIN)
        if entries:
            entry = entries[0]
            host = entry.data.get(CONF_HOST)
            port = entry.data.get(CONF_PORT)
        else:
            from .const import DEFAULT_HOST, DEFAULT_PORT

            host = DEFAULT_HOST
            port = DEFAULT_PORT
        return f"http://{host}:{port}{STREAM_PATH}"
