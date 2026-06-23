"""HTML page-render helpers: HA Ingress prefix injection.

Extracted verbatim from ``web/streamer.py`` (god-module split). Behind Home
Assistant Ingress, the admin page is served under a per-session path prefix
(the ``X-Ingress-Path`` header); these helpers sanitize that prefix and rewrite
static HTML attribute URLs (``href=``/``src=``) so assets resolve through the
Supervisor proxy. JS API calls use the client-side ``_base`` variable, so JS
string literals are deliberately NOT rewritten here (that would double-prefix).

This is the designated home for page-render helpers. The CSRF primitives
(``_get_csrf_token``/``_inject_csrf_token``) now live in ``web/auth.py``; the
admin render closure (``_render_admin_response``) stays in ``streamer`` until
the routes cut and calls them through the streamer facade. Pure string/regex
logic here — no CSRF, no template/asset-dir deps.
"""

from __future__ import annotations

import re as _re

_INGRESS_PREFIX_RE = _re.compile(r"^/[a-zA-Z0-9/_-]+$")

# Cache ingress-injected HTML to avoid repeated string replacements on every request.
# Key: (html_id, prefix) → injected HTML. Typically 1-2 entries per page.
_injected_html_cache: dict[tuple[str, str], str] = {}


def _sanitize_ingress_prefix(prefix: str) -> str:
    """Validate and sanitize the X-Ingress-Path header to prevent XSS."""
    prefix = prefix.rstrip("/")
    if not prefix or not _INGRESS_PREFIX_RE.match(prefix):
        return ""
    return prefix


def _inject_ingress_prefix(html: str, prefix: str) -> str:
    """Rewrite static HTML attribute URLs to work behind HA Ingress proxy.

    Only rewrites HTML attributes (href=, src=) — JavaScript API calls use the
    client-side ``_base`` variable derived from ``window.location.pathname``,
    so JS string literals must NOT be replaced here to avoid double-prefixing.
    """
    prefix = _sanitize_ingress_prefix(prefix)
    if not prefix:
        return html
    # Only rewrite HTML attributes (double-quoted href=, src=) and standalone JS
    # paths without _base. NEVER rewrite single-quoted JS strings that use _base
    # (e.g. _base + '/api/hosts') — that causes double-prefixing.
    html = html.replace('href="/static/', f'href="{prefix}/static/')
    html = html.replace('src="/static/', f'src="{prefix}/static/')
    html = html.replace('href="/listen"', f'href="{prefix}/listen"')
    html = html.replace('href="/dashboard"', f'href="{prefix}/dashboard"')
    html = html.replace('href="/admin"', f'href="{prefix}/admin"')
    html = html.replace('src="/stream"', f'src="{prefix}/stream"')
    # Service worker registration is standalone (no _base), needs rewriting
    html = html.replace("'/sw.js'", f"'{prefix}/sw.js'")
    return html


def _get_injected_html(html_id: str, html: str, prefix: str) -> str:
    """Return ingress-injected HTML, cached by (page, prefix)."""
    key = (html_id, prefix)
    if key not in _injected_html_cache:
        _injected_html_cache[key] = _inject_ingress_prefix(html, prefix)
    return _injected_html_cache[key]
