"""Extended tests for mammamiradio/web/streamer.py -- coverage sprint.

Covers: LiveStreamHub, auth helpers, CSRF enforcement, golden path,
        ingress prefix sanitization, utility routes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from mammamiradio.web.streamer import (
    LiveStreamHub,
    _get_csrf_token,
    _golden_path_status,
    _has_any_mp3,
    _inject_csrf_token,
    _is_hassio_or_loopback,
    _is_loopback_client,
    _is_private_network,
    _preview_tracks,
    _purge_segment_queue,
    _same_origin,
    _sanitize_ingress_prefix,
    _serialize_source,
    _tail_log,
)

# ---------------------------------------------------------------------------
# LiveStreamHub
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hub_subscribe_unsubscribe():
    hub = LiveStreamHub()
    lid, _q = hub.subscribe()
    assert hub.has_listener(lid)
    hub.unsubscribe(lid)
    assert not hub.has_listener(lid)


@pytest.mark.asyncio
async def test_hub_broadcast():
    hub = LiveStreamHub()
    lid, q = hub.subscribe()
    await hub.broadcast(b"chunk1")
    assert q.get_nowait() == b"chunk1"
    hub.unsubscribe(lid)


@pytest.mark.asyncio
async def test_hub_drops_slow_listeners():
    hub = LiveStreamHub(listener_queue_size=1)
    lid, _q = hub.subscribe()
    await hub.broadcast(b"chunk1")
    await hub.broadcast(b"chunk2")  # should drop the listener
    assert not hub.has_listener(lid)


@pytest.mark.asyncio
async def test_hub_close():
    hub = LiveStreamHub()
    lid, q = hub.subscribe()
    hub.close()
    assert not hub.has_listener(lid)
    # None sentinel should be in queue
    assert q.get_nowait() is None


@pytest.mark.asyncio
async def test_hub_double_unsubscribe():
    hub = LiveStreamHub()
    lid, _q = hub.subscribe()
    hub.unsubscribe(lid)
    hub.unsubscribe(lid)  # should not raise


# ---------------------------------------------------------------------------
# _purge_segment_queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_segment_queue():
    q = asyncio.Queue()
    seg = MagicMock()
    seg.path = MagicMock()
    q.put_nowait(seg)
    purged = _purge_segment_queue(q)
    assert purged == 1
    assert q.empty()


@pytest.mark.asyncio
async def test_purge_empty_queue():
    q = asyncio.Queue()
    assert _purge_segment_queue(q) == 0


# ---------------------------------------------------------------------------
# _has_any_mp3
# ---------------------------------------------------------------------------


def test_has_any_mp3_true(tmp_path):
    (tmp_path / "song.mp3").write_bytes(b"data")
    assert _has_any_mp3(tmp_path) is True


def test_has_any_mp3_false(tmp_path):
    assert _has_any_mp3(tmp_path) is False


def test_has_any_mp3_nonexistent(tmp_path):
    assert _has_any_mp3(tmp_path / "nope") is False


# ---------------------------------------------------------------------------
# _serialize_source / _preview_tracks
# ---------------------------------------------------------------------------


def test_serialize_source_none():
    assert _serialize_source(None) is None


def test_serialize_source():
    from mammamiradio.core.models import PlaylistSource

    src = PlaylistSource(kind="url", url="https://open.spotify.com/playlist/abc", label="Test")
    result = _serialize_source(src)
    assert result["kind"] == "url"
    assert result["label"] == "Test"


def test_preview_tracks():
    tracks = [MagicMock(title=f"Song {i}", artist=f"Artist {i}") for i in range(5)]
    result = _preview_tracks(tracks, limit=2)
    assert result["track_count"] == 5
    assert len(result["tracks"]) == 2


# ---------------------------------------------------------------------------
# _sanitize_ingress_prefix
# ---------------------------------------------------------------------------


def test_sanitize_valid_prefix():
    assert _sanitize_ingress_prefix("/api/hassio_ingress/abc123") == "/api/hassio_ingress/abc123"


def test_sanitize_empty():
    assert _sanitize_ingress_prefix("") == ""


def test_sanitize_xss():
    assert _sanitize_ingress_prefix('"><script>alert(1)</script>') == ""


def test_sanitize_trailing_slash():
    assert _sanitize_ingress_prefix("/prefix/") == "/prefix"


# ---------------------------------------------------------------------------
# _is_loopback_client / _is_hassio_or_loopback
# ---------------------------------------------------------------------------


def test_is_loopback_ipv4():
    req = MagicMock()
    req.client.host = "127.0.0.1"
    assert _is_loopback_client(req) is True


def test_is_loopback_localhost():
    req = MagicMock()
    req.client.host = "localhost"
    assert _is_loopback_client(req) is True


def test_is_loopback_external():
    req = MagicMock()
    req.client.host = "192.168.1.100"
    assert _is_loopback_client(req) is False


def test_is_loopback_no_client():
    req = MagicMock()
    req.client = None
    assert _is_loopback_client(req) is False


def test_is_loopback_invalid_ip():
    req = MagicMock()
    req.client.host = "not-an-ip"
    assert _is_loopback_client(req) is False


def test_is_hassio_network():
    req = MagicMock()
    req.client.host = "172.30.32.5"
    assert _is_hassio_or_loopback(req) is True


def test_is_hassio_external():
    req = MagicMock()
    req.client.host = "203.0.113.1"
    assert _is_hassio_or_loopback(req) is False


def test_is_hassio_no_client():
    req = MagicMock()
    req.client = None
    assert _is_hassio_or_loopback(req) is False


def test_is_private_network_rfc1918():
    for ip in ("10.0.0.1", "172.16.0.1", "192.168.1.100"):
        req = MagicMock()
        req.client.host = ip
        assert _is_private_network(req) is True, f"{ip} should be private"


def test_is_private_network_tailscale_cgnat():
    req = MagicMock()
    req.client.host = "100.98.177.107"
    assert _is_private_network(req) is True


def test_is_private_network_loopback():
    req = MagicMock()
    req.client.host = "127.0.0.1"
    assert _is_private_network(req) is True


def test_is_private_network_link_local():
    req = MagicMock()
    req.client.host = "169.254.10.20"
    assert _is_private_network(req) is True


def test_is_private_network_public_ip():
    req = MagicMock()
    req.client.host = "203.0.113.50"
    assert _is_private_network(req) is False


# ---------------------------------------------------------------------------
# _same_origin
# ---------------------------------------------------------------------------


def test_same_origin_match():
    req = MagicMock()
    req.url.scheme = "https"
    req.url.hostname = "example.com"
    req.url.port = 443
    assert _same_origin(req, "https://example.com/path") is True


def test_same_origin_no_scheme():
    req = MagicMock()
    assert _same_origin(req, "/relative/path") is False


def test_same_origin_different_host():
    req = MagicMock()
    req.url.scheme = "https"
    req.url.hostname = "example.com"
    req.url.port = 443
    assert _same_origin(req, "https://evil.com/path") is False


def test_same_origin_default_ports():
    """HTTP port 80 and HTTPS port 443 treated as defaults."""
    req = MagicMock()
    req.url.scheme = "https"
    req.url.hostname = "example.com"
    req.url.port = None
    assert _same_origin(req, "https://example.com:443/path") is True


# ---------------------------------------------------------------------------
# CSRF token helpers
# ---------------------------------------------------------------------------


def test_get_csrf_token_creates():
    app = MagicMock()
    app.state.csrf_token = ""
    token = _get_csrf_token(app)
    assert len(token) > 20
    assert app.state.csrf_token == token


def test_get_csrf_token_reuses():
    app = MagicMock()
    app.state.csrf_token = "existing-token"
    assert _get_csrf_token(app) == "existing-token"


def test_inject_csrf_token():
    html = '<meta name="csrf" content="__MAMMAMIRADIO_CSRF_TOKEN__">'
    result = _inject_csrf_token(html, "abc123")
    assert "abc123" in result
    assert "__MAMMAMIRADIO_CSRF_TOKEN__" not in result


# ---------------------------------------------------------------------------
# _golden_path_status
# ---------------------------------------------------------------------------


def test_golden_path_demo():
    config = MagicMock()
    config.anthropic_api_key = ""
    config.openai_api_key = ""
    state = MagicMock()
    result = _golden_path_status(config, state)
    assert result["stage"] in ("music_available", "needs_music_source")
    assert "blocking" in result
    assert "headline" in result
    assert "fallback_sources" in result


# ---------------------------------------------------------------------------
# _tail_log
# ---------------------------------------------------------------------------


def test_tail_log_exists(tmp_path):
    log = tmp_path / "test.log"
    log.write_text("\n".join(f"line {i}" for i in range(100)))
    lines = _tail_log(str(log), 5)
    assert len(lines) == 5
    assert "line 99" in lines[-1]


def test_tail_log_missing():
    assert _tail_log("/nonexistent/path.log") == []


@pytest.mark.asyncio
async def test_purge_segment_queue_ephemeral_unlinks(tmp_path):
    """Ephemeral segments have their file unlinked during purge."""
    from mammamiradio.core.models import Segment, SegmentType

    audio = tmp_path / "seg.mp3"
    audio.write_bytes(b"\x00" * 64)
    q = asyncio.Queue()
    seg = Segment(type=SegmentType.BANTER, path=audio, metadata={}, ephemeral=True)
    q.put_nowait(seg)
    purged = _purge_segment_queue(q)
    assert purged == 1
    assert not audio.exists()


def test_golden_path_with_local_music(tmp_path, monkeypatch):
    """When local music/ directory contains MP3s, golden path shows music_available."""
    import mammamiradio.web.streamer as streamer_mod

    monkeypatch.setattr(streamer_mod, "_golden_path_cache", None)
    monkeypatch.setattr(streamer_mod, "_golden_path_cache_ts", 0.0)

    config = MagicMock()
    config.anthropic_api_key = "key"
    config.openai_api_key = ""
    state = MagicMock()

    music_dir = tmp_path / "music"
    music_dir.mkdir()
    (music_dir / "song.mp3").write_bytes(b"data")

    with (
        patch("mammamiradio.web.streamer._has_any_mp3", side_effect=lambda p: "music" in str(p)),
    ):
        result = _golden_path_status(config, state)

    assert result["stage"] == "music_available"


def test_golden_path_with_ytdlp(monkeypatch):
    """When yt-dlp is enabled in env, it appears in fallback_sources."""
    import mammamiradio.web.streamer as streamer_mod

    monkeypatch.setattr(streamer_mod, "_golden_path_cache", None)
    monkeypatch.setattr(streamer_mod, "_golden_path_cache_ts", 0.0)
    monkeypatch.setenv("MAMMAMIRADIO_ALLOW_YTDLP", "true")

    config = MagicMock()
    config.anthropic_api_key = ""
    config.openai_api_key = ""
    state = MagicMock()

    with patch("mammamiradio.web.streamer._has_any_mp3", return_value=False):
        result = _golden_path_status(config, state)

    assert "yt-dlp downloads" in result["fallback_sources"]


def test_source_options_reason():
    """_source_options_reason formats a readable error string."""
    from mammamiradio.web.streamer import _source_options_reason

    msg = _source_options_reason(None, ValueError("something broke"))
    assert "something broke" in msg


def test_is_private_network_no_client():
    """_is_private_network returns False when request has no client."""
    req = MagicMock()
    req.client = None
    req.headers = {}
    # _is_loopback_client needs client attribute — mock to return False
    with patch("mammamiradio.web.streamer._is_loopback_client", return_value=False):
        result = _is_private_network(req)
    assert result is False


def test_is_private_network_invalid_ip():
    """_is_private_network returns False for an invalid IP address string."""
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = "not-an-ip"
    with patch("mammamiradio.web.streamer._is_loopback_client", return_value=False):
        result = _is_private_network(req)
    assert result is False


def test_is_hassio_or_loopback_no_client():
    """_is_hassio_or_loopback returns False when request has no client."""
    req = MagicMock()
    req.client = None
    with patch("mammamiradio.web.streamer._is_loopback_client", return_value=False):
        result = _is_hassio_or_loopback(req)
    assert result is False


def test_is_hassio_or_loopback_invalid_ip():
    """_is_hassio_or_loopback returns False for invalid IP."""
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = "bad-ip"
    with patch("mammamiradio.web.streamer._is_loopback_client", return_value=False):
        result = _is_hassio_or_loopback(req)
    assert result is False


@pytest.mark.asyncio
async def test_hub_close_queue_full():
    """close() swallows QueueFull when a listener's queue is already full."""
    hub = LiveStreamHub()
    hub._listener_queue_size = 1
    _lid, q = hub.subscribe()
    # Fill the queue to capacity so put_nowait raises QueueFull
    q.put_nowait(b"chunk")
    # Should not raise even though QueueFull will be triggered
    hub.close()


def test_hub_unsubscribe_updates_state():
    """unsubscribe updates state.listeners_active when state is attached."""
    from mammamiradio.core.models import StationState

    hub = LiveStreamHub()
    state = StationState()
    hub._state = state
    lid, _q = hub.subscribe()
    hub.unsubscribe(lid)
    assert state.listeners_active == 0
