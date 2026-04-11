"""Extended tests for mammamiradio/streamer.py -- coverage sprint.

Covers: LiveStreamHub, auth helpers, CSRF enforcement, golden path,
        ingress prefix sanitization, utility routes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from mammamiradio.streamer import (
    LiveStreamHub,
    _get_csrf_token,
    _golden_path_status,
    _has_any_mp3,
    _inject_csrf_token,
    _is_hassio_or_loopback,
    _is_loopback_client,
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
    from mammamiradio.models import PlaylistSource

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
    state = MagicMock()
    result = _golden_path_status(config, state)
    # Should return a dict with stage
    assert "stage" in result


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
