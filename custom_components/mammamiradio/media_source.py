"""Media Source support for Mamma Mi Radio.

This exposes the station's live MP3 stream to Home Assistant's media browser so
Music Assistant / Follow Me Music style automations can hand the radio stream to
real speaker entities.  The media_player entity remains the station control
surface; this module is just the playable stream source for other players.
"""

from __future__ import annotations

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.media_player import BrowseError, MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, HTTP_TIMEOUT, STREAM_PATH

AUDIO_MPEG = "audio/mpeg"
STREAM_PROXY_PATH = f"/api/{DOMAIN}/stream"
STREAM_VIEW_REGISTERED = f"{DOMAIN}_stream_view_registered"
STREAM_CHUNK_SIZE = 64 * 1024
LIVE_IDENTIFIER = "live"
LIVE_TITLE = "Mamma Mi Radio Live"


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Return the Mamma Mi Radio media source."""
    source = MammaRadioMediaSource(hass)
    if not hass.data.get(STREAM_VIEW_REGISTERED):
        hass.http.register_view(MammaRadioStreamView(source))
        hass.data[STREAM_VIEW_REGISTERED] = True
    return source


class MammaRadioMediaSource(MediaSource):
    """Home Assistant media-source adapter for the live station stream."""

    name = "Mamma Mi Radio"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the media source."""
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve ``media-source://mammamiradio/live`` to a HA-served stream URL."""
        if item.identifier != LIVE_IDENTIFIER:
            raise Unresolvable(f"Unknown Mamma Mi Radio media source: {item.identifier}")
        return PlayMedia(STREAM_PROXY_PATH, AUDIO_MPEG)

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Expose a one-item media browser tree containing the live stream."""
        if item.identifier not in (None, "", LIVE_IDENTIFIER):
            raise BrowseError(f"Unknown Mamma Mi Radio media source: {item.identifier}")

        if item.identifier == LIVE_IDENTIFIER:
            return BrowseMediaSource(
                domain=DOMAIN,
                identifier=LIVE_IDENTIFIER,
                media_class=MediaClass.MUSIC,
                media_content_type=AUDIO_MPEG,
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
                    media_content_type=AUDIO_MPEG,
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
            host = entry.data[CONF_HOST]
            port = entry.data[CONF_PORT]
        else:
            from .const import DEFAULT_HOST, DEFAULT_PORT

            host = DEFAULT_HOST
            port = DEFAULT_PORT
        return f"http://{host}:{port}{STREAM_PATH}"


class MammaRadioStreamView(HomeAssistantView):
    """Proxy the station stream through Home Assistant for speaker playback."""

    url = STREAM_PROXY_PATH
    name = f"api:{DOMAIN}:stream"
    requires_auth = True

    def __init__(self, source: MammaRadioMediaSource) -> None:
        """Initialize the stream proxy."""
        self._source = source

    async def head(self, request: web.Request) -> web.Response:
        """Handle renderer probes before playback."""
        return web.Response(
            content_type=AUDIO_MPEG,
            headers={"Cache-Control": "no-store"},
        )

    async def get(self, request: web.Request) -> web.StreamResponse:
        """Stream the add-on MP3 endpoint through a signed Home Assistant URL."""
        session = async_get_clientsession(self._source.hass)
        try:
            upstream = await session.get(
                self._source._stream_url(),
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=HTTP_TIMEOUT),
            )
        except (TimeoutError, aiohttp.ClientError) as err:
            raise web.HTTPBadGateway(reason="Could not reach Mamma Mi Radio stream") from err

        if upstream.status >= 400:
            upstream.release()
            raise web.HTTPBadGateway(reason="Mamma Mi Radio stream is not available")

        content_type = upstream.headers.get("Content-Type", AUDIO_MPEG).split(";", 1)[0]
        response = web.StreamResponse(
            headers={
                "Cache-Control": "no-store",
                "Content-Type": content_type or AUDIO_MPEG,
            }
        )

        try:
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(STREAM_CHUNK_SIZE):
                await response.write(chunk)
        except (ConnectionResetError, TimeoutError, aiohttp.ClientError):
            pass
        finally:
            upstream.release()

        return response
