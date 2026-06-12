"""Request-layer admin auth: credentials, CSRF, and trusted-network checks.

This module is the single source of truth for the request-layer half of the
admin-access matrix (the authoritative matrix lives in the "Admin access
model" section of docs/operations.md; the boot-layer half is _validate() in
core/config.py). Routes depend on this module via Depends(require_admin_access);
nothing here imports from web/streamer.py.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

logger = logging.getLogger(__name__)

security = HTTPBasic(auto_error=False)

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_CSRF_TOKEN_PLACEHOLDER = "__MAMMAMIRADIO_CSRF_TOKEN__"


def _get_csrf_token(app) -> str:
    token = getattr(app.state, "csrf_token", "")
    if not token:
        token = secrets.token_urlsafe(32)
        app.state.csrf_token = token
    return token


def _inject_csrf_token(html: str, token: str) -> str:
    return html.replace(_CSRF_TOKEN_PLACEHOLDER, token)


def _same_origin(request: Request, candidate: str) -> bool:
    parsed = urlparse(candidate)
    if not parsed.scheme or not parsed.netloc:
        return False
    request_url = request.url

    # Normalize ports: None means the default for the scheme (80/443)
    def _effective_port(port, scheme: str) -> int:
        if port is not None:
            return port
        return 443 if scheme == "https" else 80

    return (
        parsed.scheme == request_url.scheme
        and parsed.hostname == request_url.hostname
        and _effective_port(parsed.port, parsed.scheme) == _effective_port(request_url.port, request_url.scheme)
    )


def _enforce_csrf_for_basic_auth(request: Request, credentials: HTTPBasicCredentials | None, config) -> None:
    if request.method.upper() not in _MUTATING_METHODS:
        return
    if _is_loopback_client(request):
        return

    ingress_prefix = request.headers.get("X-Ingress-Path", "")
    if config.is_addon and ingress_prefix and _is_hassio_or_loopback(request):
        return
    admin_token_header = request.headers.get("X-Radio-Admin-Token", "")
    if config.admin_token and admin_token_header and secrets.compare_digest(admin_token_header, config.admin_token):
        return
    if not config.admin_password or not credentials:
        return

    csrf_token = request.headers.get("X-Radio-CSRF-Token", "")
    if csrf_token and secrets.compare_digest(csrf_token, _get_csrf_token(request.app)):
        return

    origin = request.headers.get("Origin", "")
    if origin and _same_origin(request, origin):
        return

    referer = request.headers.get("Referer", "")
    if referer and _same_origin(request, referer):
        return

    raise HTTPException(
        status_code=403,
        detail="Cross-site admin write blocked. Reload the dashboard and retry.",
    )


_HASSIO_NETWORK = ipaddress.ip_network("172.30.32.0/23")

# Private/trusted networks: loopback, RFC1918, IPv4/IPv6 link-local,
# IPv6 unique-local, HA Supervisor, and Tailscale/CGNAT (100.64.0.0/10).
# A self-hosted radio station trusts its own LAN — the operator installed it
# themselves.
_TRUSTED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT / Tailscale
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique-local
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    _HASSIO_NETWORK,
]


def _is_loopback_client(request: Request) -> bool:
    """Return whether the current request originated from localhost."""
    if not request.client:
        return False
    host = request.client.host
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_private_network(request: Request) -> bool:
    """Return True for loopback, LAN, Tailscale CGNAT, or HA Supervisor."""
    if _is_loopback_client(request):
        return True
    if not request.client:
        return False
    try:
        addr = ipaddress.ip_address(request.client.host)
    except ValueError:
        return False
    return any(addr in net for net in _TRUSTED_NETWORKS)


def _is_hassio_or_loopback(request: Request) -> bool:
    """Return True for loopback or the Hassio internal network."""
    if _is_loopback_client(request):
        return True
    if not request.client:
        return False
    try:
        return ipaddress.ip_address(request.client.host) in _HASSIO_NETWORK
    except ValueError:
        return False


def _enforce_csrf_for_private_network(request: Request) -> None:
    """Block cross-site mutating requests from private networks.

    LAN trust skips credential checks but a browser on the LAN could still
    be tricked into a cross-site POST. Require same-origin or CSRF token
    on mutating methods.
    """
    if request.method.upper() not in _MUTATING_METHODS:
        return

    csrf_token = request.headers.get("X-Radio-CSRF-Token", "")
    if csrf_token and secrets.compare_digest(csrf_token, _get_csrf_token(request.app)):
        return

    origin = request.headers.get("Origin", "")
    if origin and _same_origin(request, origin):
        return

    referer = request.headers.get("Referer", "")
    if referer and _same_origin(request, referer):
        return

    raise HTTPException(
        status_code=403,
        detail="Cross-site admin write blocked. Reload the dashboard and retry.",
    )


# Admin-access contract: the authoritative matrix is the "Admin access model"
# section in docs/operations.md. This function (web/auth.py) is the request-layer
# half; the boot-layer half is _validate() in core/config.py. Keep this function,
# that check, and the doc in sync — the tests/web/test_auth.py unit group, the
# tests/web/test_streamer_routes.py admin-access integration group, and the
# tests/core/test_config.py bind tests pin every row.
def require_admin_access(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> None:
    """Authorize admin-only routes using configured credentials or local trust."""
    config = request.app.state.config

    # Loopback is fully trusted — same machine, no CSRF risk.
    if _is_loopback_client(request):
        return

    # HA Supervisor network is Docker-internal (not user-accessible), so
    # CSRF from a browser on that network is not a real threat. Fully trust
    # it in addon mode so HA automations (rest_command, etc.) work without tokens.
    if config.is_addon and _is_hassio_or_loopback(request):
        return

    # Explicit auth for all non-local traffic when credentials are configured.
    if config.admin_token:
        token = request.headers.get("X-Radio-Admin-Token")
        if token and secrets.compare_digest(token, config.admin_token):
            return

    if config.admin_password:
        username = credentials.username if credentials else ""
        password = credentials.password if credentials else ""
        if secrets.compare_digest(username, config.admin_username) and secrets.compare_digest(
            password, config.admin_password
        ):
            _enforce_csrf_for_basic_auth(request, credentials, config)
            return
        client_ip = request.client.host if request.client else "unknown"
        logger.warning("Failed admin auth attempt from %s", client_ip)
        raise HTTPException(
            status_code=401,
            detail="Admin authentication required",
            headers={"WWW-Authenticate": 'Basic realm="mammamiradio admin"'},
        )

    if config.admin_token:
        client_ip = request.client.host if request.client else "unknown"
        logger.warning("Missing admin token from %s", client_ip)
        raise HTTPException(
            status_code=401,
            detail="X-Radio-Admin-Token required",
        )

    # Backward-compatible fallback when no admin credentials are configured.
    # In standalone mode load_config() now rejects a non-loopback bind without
    # creds, so in production this only fires for loopback binds (already
    # short-circuited above). Reachable here mainly via test apps built without
    # creds — kept so credential-less LAN deployments keep working with CSRF.
    if _is_private_network(request):
        _enforce_csrf_for_private_network(request)
        return

    raise HTTPException(
        status_code=403,
        detail="Admin endpoints are only available from private networks unless admin auth is configured",
    )
