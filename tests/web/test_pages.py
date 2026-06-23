"""Tests for web/pages.py — HA Ingress prefix injection + streamer facade re-export.

Home for the ingress helpers' unit tests (relocated from test_streamer.py and
test_streamer_coverage.py when the cluster moved out of the streamer god-module)
plus the facade-identity guard and a cache test for _get_injected_html.
"""

from __future__ import annotations

from mammamiradio.web.pages import (
    _get_injected_html,
    _inject_ingress_prefix,
    _injected_html_cache,
    _sanitize_ingress_prefix,
)


def test_pages_facade_reexport_identity():
    """Every ingress symbol the streamer facade re-exports must resolve to the SAME
    object as its new home in web/pages.

    Routes (_listener_context, _render_admin_response) still call these by
    bare name through the streamer namespace, so the re-export must point at the moved
    implementation, not a forked copy. The streamer import is local so the pure-pages
    tests below don't load the god-module.
    """
    import mammamiradio.web.streamer as streamer

    assert streamer._get_injected_html is _get_injected_html
    assert streamer._sanitize_ingress_prefix is _sanitize_ingress_prefix


# --- Ingress prefix injection tests ---


def test_inject_ingress_prefix_empty():
    """Empty prefix should return HTML unchanged."""
    html = """<script>fetch('/stream')</script>"""
    assert _inject_ingress_prefix(html, "") is html


def test_inject_ingress_prefix_rewrites_html_attributes():
    """Non-empty prefix should rewrite static HTML attributes only."""
    prefix = "/api/hassio_ingress/abc123"
    # Static HTML attributes are rewritten
    assert f'href="{prefix}/listen"' in _inject_ingress_prefix('href="/listen"', prefix)
    assert f'src="{prefix}/stream"' in _inject_ingress_prefix('src="/stream"', prefix)


def test_inject_ingress_prefix_does_not_rewrite_js_strings():
    """Single-quoted JS strings must NOT be rewritten — _base handles them."""
    prefix = "/api/hassio_ingress/abc123"
    # JS patterns like _base + '/stream' must stay untouched
    js = "_base + '/stream'"
    assert _inject_ingress_prefix(js, prefix) == js
    js2 = "_base + '/status'"
    assert _inject_ingress_prefix(js2, prefix) == js2
    js3 = "fetch(_base + '/api/skip')"
    assert _inject_ingress_prefix(js3, prefix) == js3


def test_inject_ingress_prefix_no_false_positives():
    """Prefix injection should not affect non-matching patterns."""
    html = "some random text with /stream in prose"
    result = _inject_ingress_prefix(html, "/prefix")
    assert result == html


def test_inject_ingress_prefix_rewrites_static_paths():
    """Ingress prefix should rewrite /static/ asset references."""
    prefix = "/api/hassio_ingress/abc123"
    assert f'"{prefix}/static/manifest.json"' in _inject_ingress_prefix('href="/static/manifest.json"', prefix)
    assert f'"{prefix}/static/icon-192.svg"' in _inject_ingress_prefix('href="/static/icon-192.svg"', prefix)


def test_inject_ingress_prefix_rewrites_script_src_static():
    """Ingress prefix must rewrite <script src="/static/..."> alongside href= attributes.

    Guards the listener site-v1 split that serves its client code from
    /static/listener.js. Without this, HA Ingress users hit dead <script> tags
    and the listener loses its runtime wiring under the Supervisor proxy.
    """
    prefix = "/api/hassio_ingress/abc123"
    html = '<script src="/static/listener.js" defer></script>'
    expected = f'<script src="{prefix}/static/listener.js" defer></script>'
    assert _inject_ingress_prefix(html, prefix) == expected


def test_inject_ingress_prefix_rewrites_sw_path():
    """Ingress prefix should rewrite /sw.js reference."""
    prefix = "/api/hassio_ingress/abc123"
    assert f"'{prefix}/sw.js'" in _inject_ingress_prefix("register('/sw.js')", prefix)


# --- Ingress prefix sanitization tests ---


def test_sanitize_ingress_prefix_valid():
    assert _sanitize_ingress_prefix("/api/hassio_ingress/abc123") == "/api/hassio_ingress/abc123"


def test_sanitize_ingress_prefix_strips_trailing_slash():
    assert _sanitize_ingress_prefix("/prefix/") == "/prefix"


def test_sanitize_ingress_prefix_rejects_xss():
    assert _sanitize_ingress_prefix('"><script>alert(1)</script>') == ""


def test_sanitize_ingress_prefix_empty():
    assert _sanitize_ingress_prefix("") == ""


# --- Injected-HTML cache ---


def test_get_injected_html_caches_by_page_and_prefix():
    """_get_injected_html memoizes per (html_id, prefix) in _injected_html_cache."""
    _injected_html_cache.clear()
    html = 'href="/listen"'
    first = _get_injected_html("admin", html, "/p")
    second = _get_injected_html("admin", html, "/p")
    assert first == second == 'href="/p/listen"'
    assert ("admin", "/p") in _injected_html_cache
    _injected_html_cache.clear()
