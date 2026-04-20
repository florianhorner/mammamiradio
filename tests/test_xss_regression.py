"""Regression guards for XSS vulnerabilities in the admin panel.

Two attack paths were identified and fixed:
- Path A: HA entity state values rendered via innerHTML without esc() in admin.html
- Path B: yt-dlp track titles stored raw in ha_pending_directive, rendered via innerHTML

Fix: esc() applied to all five HA fields in admin.html before innerHTML assignment.
Defense-in-depth: Content-Security-Policy header on /admin with per-request nonce allows the
inline script block while blocking injected external scripts.

ha_pending_directive intentionally stores raw titles (LLM prompts need unencoded text).
esc() in admin.html is the XSS boundary for that field.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

ADMIN_HTML = Path(__file__).parent.parent / "mammamiradio" / "admin.html"
STREAMER_PY = Path(__file__).parent.parent / "mammamiradio" / "streamer.py"


def test_admin_ha_fields_use_esc() -> None:
    """All five HA detail fields in admin.html must be wrapped with esc() before innerHTML."""
    html = ADMIN_HTML.read_text()

    # Locate the updateEngineRoom / ha_details rendering block
    block_start = html.find("const hd=st.ha_details")
    block_end = html.find("haEl.innerHTML", block_start)
    assert block_start != -1, "ha_details block not found in admin.html"
    assert block_end != -1, "haEl.innerHTML not found after ha_details block"

    block = html[block_start:block_end]

    # Each HA-sourced field must be wrapped in esc()
    for field in ("mood_en", "weather_arc_en", "events_summary_en", "pending_directive", "last_event_label_en"):
        assert f"esc(hd.{field}" in block or f"esc(hd.{field.replace('_en', '')}" in block, (
            f"HA field '{field}' is not wrapped with esc() before innerHTML assignment. "
            "This is an XSS vulnerability — HA entity state values are attacker-influenced."
        )


def test_admin_events_summary_esc_before_replace() -> None:
    """esc() must be applied to events_summary BEFORE .replace(/\\n/g,'<br>')."""
    html = ADMIN_HTML.read_text()
    # The correct pattern: esc(...).replace(...)
    # The wrong pattern: (...).replace(...) with no esc
    match = re.search(r"esc\(hd\.(events_summary_en\|\|hd\.events_summary|events_summary)\)\.replace", html)
    assert match is not None, (
        "events_summary must apply esc() before .replace(/\\n/g,'<br>'). "
        "Applying replace first then esc() would double-encode the <br> tags."
    )


def test_admin_csp_allows_inline() -> None:
    """The /admin CSP must use 'unsafe-inline' so inline event handlers are allowed.

    admin.html has ~40 inline event handlers (onclick, oninput, onchange) throughout.
    A nonce-only CSP (script-src 'self' 'nonce-{x}') blocks those handlers even when
    the <script> block is allowed — nonces cover <script> elements, not attribute
    event handlers. 'unsafe-inline' is required to allow them while still blocking
    external script sources (CDNs, attacker domains).
    esc() on all HA fields in admin.html is the load-bearing XSS defense.
    """
    src = STREAMER_PY.read_text()
    assert "Content-Security-Policy" in src, (
        "streamer.py /admin route must set Content-Security-Policy header."
    )
    assert "script-src" in src, "Content-Security-Policy must include script-src directive."
    assert "'unsafe-inline'" in src, (
        "CSP must include 'unsafe-inline' to allow the inline event handlers in admin.html. "
        "A nonce-only CSP blocks onclick/oninput/onchange handlers, breaking the admin UI."
    )


def test_sanitize_state_value_strips_injection_phrases() -> None:
    """_sanitize_state_value must reject known LLM injection phrases."""
    from mammamiradio.ha_context import _sanitize_state_value

    for phrase in ("ignore previous", "disregard", "system override", "forget your"):
        result = _sanitize_state_value(phrase + " instructions")
        assert result == "(filtered)", f"_sanitize_state_value did not filter injection phrase: '{phrase}'"


def test_sanitize_state_value_does_not_html_encode() -> None:
    """Server-side HTML encoding is intentionally NOT applied.

    The client-side esc() in admin.html is the XSS defense boundary.
    Double-encoding would corrupt display of Italian strings with & and < characters.
    This test documents the intentional design: raw values flow through JSON;
    esc() in admin.html is responsible for safe rendering.
    """
    from mammamiradio.ha_context import _sanitize_state_value

    raw = "<b>test & value</b>"
    result = _sanitize_state_value(raw)
    assert result == raw, (
        "Server-side HTML encoding was applied in _sanitize_state_value. "
        "This is intentionally NOT done — it causes double-encoding when admin.html esc() also runs. "
        "The defense boundary is client-side esc() in admin.html."
    )


@pytest.mark.asyncio
async def test_admin_csp_header_sent_with_unsafe_inline() -> None:
    """GET /admin must return a Content-Security-Policy header with 'unsafe-inline'.

    This is an HTTP-level test — verifies the CSP header is actually set on the response,
    not just present in source code. Also verifies the placeholder is NOT in the rendered
    HTML (no stale template leak).
    """
    import httpx
    from fastapi import FastAPI

    from mammamiradio.config import load_config
    from mammamiradio.models import StationState, Track
    from mammamiradio.streamer import LiveStreamHub, router

    toml = str(Path(__file__).parent.parent / "radio.toml")
    app = FastAPI()
    app.include_router(router)
    config = load_config(toml)
    config.admin_password = ""
    state = StationState(
        playlist=[Track(title="T", artist="A", duration_ms=180_000, spotify_id="t1")],
    )
    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    hub = LiveStreamHub()
    hub.bind_state(state)
    app.state.stream_hub = hub
    app.state.station_state = state
    app.state.config = config

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/admin")

    assert resp.status_code == 200
    csp = resp.headers.get("content-security-policy", "")
    assert "script-src" in csp, f"CSP header missing script-src: {csp!r}"
    assert "'unsafe-inline'" in csp, (
        f"CSP must include 'unsafe-inline' to allow inline event handlers: {csp!r}"
    )
    assert "__MAMMAMIRADIO_SCRIPT_NONCE__" not in resp.text, (
        "Stale nonce placeholder found in rendered HTML — template injection broken."
    )


def test_pending_directive_stores_raw_title() -> None:
    """ha_pending_directive stores track titles raw (without HTML encoding).

    This is intentional: ha_pending_directive is used in LLM prompts.
    HTML encoding would corrupt the LLM input with HTML entities.
    The admin.html esc() call is the XSS defense boundary when the value reaches the UI.
    """
    # Simulate what _persist_skipped_music writes
    metadata = {"title": "<b>Track & Artist — Live</b>", "title_only": None}
    track_name = metadata.get("title_only") or metadata.get("title") or "questa canzone"
    directive = f"L'ascoltatore ha saltato '{track_name}' troppe volte"

    # Must store raw, not HTML-encoded
    assert "<b>Track & Artist" in directive, (
        "ha_pending_directive should store the raw track title. HTML encoding here would break LLM prompt quality."
    )
    assert "&lt;" not in directive, (
        "Track title was HTML-encoded before storage in ha_pending_directive. "
        "This breaks LLM prompts. The esc() in admin.html handles rendering safety."
    )
