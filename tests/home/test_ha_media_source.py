"""Contract tests for the HACS media-source glue.

Home Assistant is not installed in the repo test environment, so these tests
load the integration module with minimal HA stubs and exercise its behavior.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web as aiohttp_web

ROOT = Path(__file__).resolve().parents[2]
COMPONENT = ROOT / "custom_components" / "mammamiradio"
MEDIA_SOURCE = COMPONENT / "media_source.py"
CONST = COMPONENT / "const.py"
DOC = ROOT / "docs" / "integrations" / "ha-integration.md"


class _BrowseError(Exception):
    pass


class _UnresolvableError(Exception):
    pass


class _MediaClass:
    DIRECTORY = "directory"
    MUSIC = "music"


class _MediaType:
    MUSIC = "music"


class _HomeAssistantView:
    pass


class _MediaSource:
    name: str | None = None

    def __init__(self, domain: str) -> None:
        self.domain = domain
        if self.name is None:
            self.name = domain


class _BrowseMediaSource:
    def __init__(
        self,
        *,
        domain: str,
        identifier: str | None,
        media_class: str,
        media_content_type: str,
        title: str,
        can_play: bool,
        can_expand: bool,
        thumbnail: str | None = None,
        children_media_class: str | None = None,
        children: list[Any] | None = None,
    ) -> None:
        self.domain = domain
        self.identifier = identifier
        self.media_content_id = f"media-source://{domain}"
        if identifier:
            self.media_content_id += f"/{identifier}"
        self.media_class = media_class
        self.media_content_type = media_content_type
        self.title = title
        self.can_play = can_play
        self.can_expand = can_expand
        self.thumbnail = thumbnail
        self.children_media_class = children_media_class
        self.children = children


@dataclass
class _MediaSourceItem:
    hass: Any
    domain: str | None
    identifier: str
    target_media_player: str | None


@dataclass
class _PlayMedia:
    url: str
    mime_type: str


@dataclass
class _ConfigEntry:
    data: dict[str, Any]


class _ConfigEntries:
    def __init__(self, entries: list[_ConfigEntry] | None = None) -> None:
        self._entries = entries or []

    def async_entries(self, domain: str) -> list[_ConfigEntry]:
        assert domain == "mammamiradio"
        return self._entries


class _Http:
    def __init__(self) -> None:
        self.views: list[Any] = []

    def register_view(self, view: Any) -> None:
        self.views.append(view)


class _Hass:
    def __init__(
        self,
        entries: list[_ConfigEntry] | None = None,
        session: Any = None,
    ) -> None:
        self.config_entries = _ConfigEntries(entries)
        self.data: dict[str, Any] = {}
        self.http = _Http()
        self.session: Any = session


def _install_homeassistant_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    modules = {
        "homeassistant": types.ModuleType("homeassistant"),
        "homeassistant.components": types.ModuleType("homeassistant.components"),
        "homeassistant.components.http": types.ModuleType("homeassistant.components.http"),
        "homeassistant.components.media_player": types.ModuleType("homeassistant.components.media_player"),
        "homeassistant.components.media_source": types.ModuleType("homeassistant.components.media_source"),
        "homeassistant.const": types.ModuleType("homeassistant.const"),
        "homeassistant.core": types.ModuleType("homeassistant.core"),
        "homeassistant.helpers": types.ModuleType("homeassistant.helpers"),
        "homeassistant.helpers.aiohttp_client": types.ModuleType("homeassistant.helpers.aiohttp_client"),
    }

    modules["homeassistant.components.http"].HomeAssistantView = _HomeAssistantView  # type: ignore[attr-defined]
    modules["homeassistant.components.media_player"].BrowseError = _BrowseError  # type: ignore[attr-defined]
    modules["homeassistant.components.media_player"].MediaClass = _MediaClass  # type: ignore[attr-defined]
    modules["homeassistant.components.media_player"].MediaType = _MediaType  # type: ignore[attr-defined]
    modules["homeassistant.components.media_source"].BrowseMediaSource = _BrowseMediaSource  # type: ignore[attr-defined]
    modules["homeassistant.components.media_source"].MediaSource = _MediaSource  # type: ignore[attr-defined]
    modules["homeassistant.components.media_source"].MediaSourceItem = _MediaSourceItem  # type: ignore[attr-defined]
    modules["homeassistant.components.media_source"].PlayMedia = _PlayMedia  # type: ignore[attr-defined]
    modules["homeassistant.components.media_source"].Unresolvable = _UnresolvableError  # type: ignore[attr-defined]
    modules["homeassistant.const"].CONF_HOST = "host"  # type: ignore[attr-defined]
    modules["homeassistant.const"].CONF_PORT = "port"  # type: ignore[attr-defined]
    modules["homeassistant.core"].HomeAssistant = object  # type: ignore[attr-defined]
    modules["homeassistant.helpers.aiohttp_client"].async_get_clientsession = lambda hass: hass.session  # type: ignore[attr-defined]

    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def _load_media_source_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    _install_homeassistant_stubs(monkeypatch)

    package_name = f"_mammamiradio_media_source_test_{id(monkeypatch)}"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT)]
    monkeypatch.setitem(sys.modules, package_name, package)

    const_spec = importlib.util.spec_from_file_location(f"{package_name}.const", CONST)
    assert const_spec is not None
    const_module = importlib.util.module_from_spec(const_spec)
    monkeypatch.setitem(sys.modules, f"{package_name}.const", const_module)
    assert const_spec.loader is not None
    const_spec.loader.exec_module(const_module)

    source_spec = importlib.util.spec_from_file_location(f"{package_name}.media_source", MEDIA_SOURCE)
    assert source_spec is not None
    source_module = importlib.util.module_from_spec(source_spec)
    monkeypatch.setitem(sys.modules, f"{package_name}.media_source", source_module)
    assert source_spec.loader is not None
    source_spec.loader.exec_module(source_module)
    return source_module


def _item(module: types.ModuleType, identifier: str) -> Any:
    return module.MediaSourceItem(None, module.DOMAIN, identifier, None)


def test_async_get_media_source_registers_stream_proxy_once(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_media_source_module(monkeypatch)
    hass = _Hass()

    source = asyncio.run(module.async_get_media_source(hass))
    asyncio.run(module.async_get_media_source(hass))

    assert source.domain == "mammamiradio"
    assert len(hass.http.views) == 1
    assert hass.http.views[0].url == "/api/mammamiradio/stream"
    assert hass.http.views[0].requires_auth is True
    head = asyncio.run(hass.http.views[0].head(None))
    assert head.content_type == "audio/mpeg"


def test_browse_and_resolve_live_stream_use_audio_mpeg_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_media_source_module(monkeypatch)
    source = module.MammaRadioMediaSource(_Hass())

    root = asyncio.run(source.async_browse_media(_item(module, "")))
    child = root.children[0]
    live = asyncio.run(source.async_browse_media(_item(module, "live")))
    playable = asyncio.run(source.async_resolve_media(_item(module, "live")))

    assert root.media_content_id == "media-source://mammamiradio"
    assert child.media_content_id == "media-source://mammamiradio/live"
    assert child.can_play is True
    assert child.can_expand is False
    assert child.media_content_type == "audio/mpeg"
    assert live.media_content_type == "audio/mpeg"
    assert playable == _PlayMedia("/api/mammamiradio/stream", "audio/mpeg")


def test_stream_proxy_fetches_configured_entry_or_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_media_source_module(monkeypatch)

    configured = module.MammaRadioMediaSource(_Hass([_ConfigEntry({"host": "radio.local", "port": 9000})]))
    fallback = module.MammaRadioMediaSource(_Hass())

    assert configured._stream_url() == "http://radio.local:9000/stream"
    assert fallback._stream_url() == "http://local-mammamiradio:8000/stream"


def test_invalid_media_identifiers_raise_home_assistant_contract_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_media_source_module(monkeypatch)
    source = module.MammaRadioMediaSource(_Hass())

    with pytest.raises(module.Unresolvable):
        asyncio.run(source.async_resolve_media(_item(module, "missing")))

    with pytest.raises(module.BrowseError):
        asyncio.run(source.async_browse_media(_item(module, "missing")))


def test_ha_docs_no_longer_defer_media_source() -> None:
    doc = DOC.read_text(encoding="utf-8")
    assert "media-source://mammamiradio/live" in doc
    assert "Home Assistant stream proxy" in doc
    assert "Follow Me Music" in doc
    assert "`media_source.py` (casting the stream to other HA speakers)" not in doc


# --- Stream-proxy view (MammaRadioStreamView.get) -------------------------------
#
# The proxy is the byte pipe between the add-on stream and HA speakers, so the
# project's audio-delivery rule applies: cover Normal, unavailable, and degraded.
# HA's real aiohttp web.StreamResponse needs a live transport to prepare(), so we
# swap a recording fake in for it and inject a fake client session.


class _FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size: int) -> Any:
        for chunk in self._chunks:
            yield chunk


class _RaisingContent:
    """Yields one chunk, then drops like a client hanging up mid-stream."""

    async def iter_chunked(self, _size: int) -> Any:
        yield b"first"
        raise aiohttp.ClientError("client went away")


class _FakeUpstream:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        content: Any = None,
    ) -> None:
        self.status = status
        self.headers = headers or {"Content-Type": "audio/mpeg"}
        self.content: Any = content if content is not None else _FakeContent([b"audio"])
        self.released = False

    def release(self) -> None:
        self.released = True


class _FakeSession:
    def __init__(self, *, upstream: Any = None, error: Exception | None = None) -> None:
        self._upstream = upstream
        self._error = error
        self.requested_url: str | None = None

    async def get(self, url: str, *, timeout: Any) -> Any:
        self.requested_url = url
        if self._error is not None:
            raise self._error
        return self._upstream


class _FakeStreamResponse:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers: dict[str, str] = dict(headers or {})
        self.prepared = False
        self.written: list[bytes] = []

    async def prepare(self, _request: Any) -> None:
        self.prepared = True

    async def write(self, data: bytes) -> None:
        self.written.append(data)


def _patch_stream_response(monkeypatch: pytest.MonkeyPatch, module: types.ModuleType) -> None:
    """Swap the recording fake in for aiohttp's StreamResponse (keeps real errors)."""
    fake_web = types.SimpleNamespace(
        StreamResponse=_FakeStreamResponse,
        Response=aiohttp_web.Response,
        HTTPBadGateway=aiohttp_web.HTTPBadGateway,
        Request=aiohttp_web.Request,
    )
    monkeypatch.setattr(module, "web", fake_web)


def _view(module: types.ModuleType, hass: _Hass) -> Any:
    return module.MammaRadioStreamView(module.MammaRadioMediaSource(hass))


def test_stream_proxy_get_streams_chunks_and_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scenario 1 (normal): chunks proxy through, headers pass, connection released."""
    module = _load_media_source_module(monkeypatch)
    _patch_stream_response(monkeypatch, module)
    upstream = _FakeUpstream(
        headers={"Content-Type": "audio/mpeg; bitrate=128"},
        content=_FakeContent([b"a", b"b", b"c"]),
    )
    session = _FakeSession(upstream=upstream)
    view = _view(module, _Hass(session=session))

    response = asyncio.run(view.get(object()))

    assert isinstance(response, _FakeStreamResponse)
    assert response.prepared is True
    assert response.written == [b"a", b"b", b"c"]
    assert response.headers["Content-Type"] == "audio/mpeg"  # params stripped
    assert response.headers["Cache-Control"] == "no-store"
    assert upstream.released is True
    assert session.requested_url == "http://local-mammamiradio:8000/stream"


def test_stream_proxy_get_bad_gateway_when_addon_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scenario 2 (unavailable): a connect failure surfaces a 502, not a hang."""
    module = _load_media_source_module(monkeypatch)
    _patch_stream_response(monkeypatch, module)
    session = _FakeSession(error=aiohttp.ClientConnectionError("refused"))
    view = _view(module, _Hass(session=session))

    with pytest.raises(aiohttp_web.HTTPBadGateway):
        asyncio.run(view.get(object()))


def test_stream_proxy_get_bad_gateway_on_upstream_error_status_and_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 3 (degraded): an upstream 5xx becomes a 502 and still releases."""
    module = _load_media_source_module(monkeypatch)
    _patch_stream_response(monkeypatch, module)
    upstream = _FakeUpstream(status=503)
    session = _FakeSession(upstream=upstream)
    view = _view(module, _Hass(session=session))

    with pytest.raises(aiohttp_web.HTTPBadGateway):
        asyncio.run(view.get(object()))

    assert upstream.released is True


def test_stream_proxy_get_swallows_midstream_drop_and_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client/upstream drop mid-stream is swallowed; the connection still releases."""
    module = _load_media_source_module(monkeypatch)
    _patch_stream_response(monkeypatch, module)
    upstream = _FakeUpstream(content=_RaisingContent())
    session = _FakeSession(upstream=upstream)
    view = _view(module, _Hass(session=session))

    response = asyncio.run(view.get(object()))  # must not raise

    assert response.written == [b"first"]
    assert upstream.released is True
