"""Unit tests for mammamiradio/web/auth.py — request-layer admin auth helpers.

This is the unit-test half of the admin-access contract: the authoritative
matrix is the "Admin access model" section in docs/operations.md, the
request-layer enforcement is web/auth.py, and the app-level integration tests
that pin every matrix row stay in tests/web/test_streamer_routes.py (plus the
extended file). Pure helper behavior — network classification, same-origin,
CSRF token plumbing — is pinned here.

Relocated from tests/web/test_streamer_coverage.py in the auth keystone cut;
one duplicate (patched no-client hassio test re-pinning the unpatched one)
was collapsed during the move.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mammamiradio.web.auth import (
    _get_csrf_token,
    _inject_csrf_token,
    _is_hassio_or_loopback,
    _is_loopback_client,
    _is_private_network,
    _same_origin,
)

# ---------------------------------------------------------------------------
# Facade identity guard
# ---------------------------------------------------------------------------


def test_auth_facade_reexport_identity():
    """Every auth symbol the streamer facade re-exports must resolve to the SAME
    object as its new home in web/auth.

    Route decorators (Depends(require_admin_access)) and the render helpers
    still read these by bare name through the streamer namespace, so the
    re-export must point at the moved implementation, not a forked copy. The
    streamer import is local so the pure-auth tests below don't load the
    god-module.
    """
    import mammamiradio.web.auth as auth
    import mammamiradio.web.streamer as streamer

    for name in (
        "require_admin_access",
        "security",
        "_get_csrf_token",
        "_inject_csrf_token",
        "_same_origin",
        "_enforce_csrf_for_basic_auth",
        "_is_loopback_client",
        "_is_private_network",
        "_is_hassio_or_loopback",
        "_enforce_csrf_for_private_network",
        "_MUTATING_METHODS",
        "_CSRF_TOKEN_PLACEHOLDER",
        "_HASSIO_NETWORK",
        "_TRUSTED_NETWORKS",
    ):
        assert getattr(streamer, name) is getattr(auth, name), name


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


def test_is_hassio_or_loopback_invalid_ip():
    """_is_hassio_or_loopback returns False for invalid IP."""
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = "bad-ip"
    with patch("mammamiradio.web.auth._is_loopback_client", return_value=False):
        result = _is_hassio_or_loopback(req)
    assert result is False


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


def test_is_private_network_ipv6_lan_ranges():
    for ip in ("fd00::1234", "fc00::1", "fe80::1"):
        req = MagicMock()
        req.client.host = ip
        assert _is_private_network(req) is True, f"{ip} should be trusted"


def test_is_private_network_public_ip():
    for ip in ("203.0.113.50", "2001:4860:4860::8888"):
        req = MagicMock()
        req.client.host = ip
        assert _is_private_network(req) is False, f"{ip} should not be trusted"


def test_is_private_network_no_client():
    """_is_private_network returns False when request has no client."""
    req = MagicMock()
    req.client = None
    req.headers = {}
    # _is_loopback_client needs client attribute — mock to return False
    with patch("mammamiradio.web.auth._is_loopback_client", return_value=False):
        result = _is_private_network(req)
    assert result is False


def test_is_private_network_invalid_ip():
    """_is_private_network returns False for an invalid IP address string."""
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = "not-an-ip"
    with patch("mammamiradio.web.auth._is_loopback_client", return_value=False):
        result = _is_private_network(req)
    assert result is False


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
