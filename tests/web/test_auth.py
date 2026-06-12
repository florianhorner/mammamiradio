"""Unit tests for mammamiradio/web/auth.py — request-layer admin auth helpers.

This is the unit-test half of the admin-access contract: the authoritative
matrix is the "Admin access model" section in docs/operations.md, the
request-layer enforcement is web/auth.py, and the app-level integration tests
that pin every matrix row stay in tests/web/test_streamer_routes.py (plus the
extended file). Pure helper behavior — network classification, same-origin,
CSRF token plumbing — is pinned here.

Relocated from tests/web/test_streamer_coverage.py in the auth keystone cut,
plus new pins for the CSRF-exemption branches that route-level tests cannot
reach (loopback/ingress/token/referer shortcuts in the enforcement helpers).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mammamiradio.web.auth import (
    _enforce_csrf_for_basic_auth,
    _enforce_csrf_for_private_network,
    _get_csrf_token,
    _inject_csrf_token,
    _is_hassio_or_loopback,
    _is_loopback_client,
    _is_private_network,
    _same_origin,
)


def _mock_request(
    *,
    method: str = "POST",
    client_host: str | None = "203.0.113.50",
    headers: dict | None = None,
    url_scheme: str = "https",
    url_hostname: str = "example.com",
    url_port: int | None = 443,
    csrf_token: str = "session-token",
):
    """Minimal Request stand-in for the enforcement helpers."""
    req = MagicMock()
    req.method = method
    if client_host is None:
        req.client = None
    else:
        req.client.host = client_host
    req.headers = headers or {}
    req.url.scheme = url_scheme
    req.url.hostname = url_hostname
    req.url.port = url_port
    req.app.state.csrf_token = csrf_token
    return req

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


def test_is_hassio_or_loopback_no_client_own_guard():
    """_is_hassio_or_loopback's own no-client guard, isolated from the loopback
    helper — pins the branch even if a refactor changes _is_loopback_client's
    handling of client=None."""
    req = MagicMock()
    req.client = None
    with patch("mammamiradio.web.auth._is_loopback_client", return_value=False):
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


def test_same_origin_scheme_mismatch():
    """An http origin never matches an https request, even with same host."""
    req = MagicMock()
    req.url.scheme = "https"
    req.url.hostname = "example.com"
    req.url.port = 443
    assert _same_origin(req, "http://example.com/path") is False


def test_same_origin_explicit_port_mismatch():
    """An explicit non-default port must match the request port exactly."""
    req = MagicMock()
    req.url.scheme = "https"
    req.url.hostname = "example.com"
    req.url.port = 443
    assert _same_origin(req, "https://example.com:8443/path") is False


# ---------------------------------------------------------------------------
# CSRF enforcement exemption branches
# ---------------------------------------------------------------------------


def _mock_config(*, is_addon: bool = False, admin_token: str = "", admin_password: str = ""):
    config = MagicMock()
    config.is_addon = is_addon
    config.admin_token = admin_token
    config.admin_password = admin_password
    return config


def test_enforce_basic_auth_skips_non_mutating():
    """GET requests bypass CSRF enforcement entirely."""
    req = _mock_request(method="GET")
    assert _enforce_csrf_for_basic_auth(req, MagicMock(), _mock_config()) is None


def test_enforce_basic_auth_loopback_short_circuit():
    """Loopback clients are exempt from basic-auth CSRF checks."""
    req = _mock_request(client_host="127.0.0.1")
    assert _enforce_csrf_for_basic_auth(req, MagicMock(), _mock_config(admin_password="pw")) is None


def test_enforce_basic_auth_ingress_hassio_exemption():
    """Addon-mode requests from the Supervisor network with an ingress prefix skip CSRF."""
    req = _mock_request(client_host="172.30.32.5", headers={"X-Ingress-Path": "/api/hassio_ingress/x"})
    config = _mock_config(is_addon=True, admin_password="pw")
    assert _enforce_csrf_for_basic_auth(req, MagicMock(), config) is None


def test_enforce_basic_auth_admin_token_exemption():
    """A valid X-Radio-Admin-Token header skips the CSRF requirement."""
    req = _mock_request(headers={"X-Radio-Admin-Token": "tok"})
    config = _mock_config(admin_token="tok", admin_password="pw")
    assert _enforce_csrf_for_basic_auth(req, MagicMock(), config) is None


def test_enforce_basic_auth_no_password_or_credentials():
    """Without a configured password (or without credentials) there is nothing to protect."""
    req = _mock_request()
    assert _enforce_csrf_for_basic_auth(req, MagicMock(), _mock_config()) is None
    assert _enforce_csrf_for_basic_auth(req, None, _mock_config(admin_password="pw")) is None


def test_enforce_basic_auth_referer_same_origin_accepts():
    """A same-origin Referer satisfies CSRF when token and Origin are absent."""
    req = _mock_request(headers={"Referer": "https://example.com/admin"})
    config = _mock_config(admin_password="pw")
    assert _enforce_csrf_for_basic_auth(req, MagicMock(), config) is None


def test_enforce_private_network_csrf_token_accepts():
    """A matching X-Radio-CSRF-Token header authorizes a LAN mutation — the path
    the admin UI relies on when a browser strips Origin/Referer."""
    req = _mock_request(headers={"X-Radio-CSRF-Token": "session-token"})
    assert _enforce_csrf_for_private_network(req) is None


def test_enforce_private_network_referer_accepts():
    """A same-origin Referer authorizes a LAN mutation when token and Origin are absent."""
    req = _mock_request(headers={"Referer": "https://example.com/admin"})
    assert _enforce_csrf_for_private_network(req) is None


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
