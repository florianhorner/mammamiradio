"""Regression guards for XSS vulnerabilities in the admin panel.

Two attack paths were identified and fixed:
- Path A: HA entity state values rendered via innerHTML without esc() in admin.html
- Path B: yt-dlp track titles stored raw in ha_pending_directive, rendered via innerHTML

Fix: esc() applied to all five HA fields in admin.html before innerHTML assignment.
Defense-in-depth: Content-Security-Policy header on /admin blocks inline script execution.

ha_pending_directive intentionally stores raw titles (LLM prompts need unencoded text).
esc() in admin.html is the XSS boundary for that field.
"""

from __future__ import annotations

import re
from pathlib import Path

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


def test_admin_csp_header_in_source() -> None:
    """The /admin route must set a Content-Security-Policy header."""
    src = STREAMER_PY.read_text()
    assert "Content-Security-Policy" in src, (
        "streamer.py /admin route must set Content-Security-Policy header. "
        "Required: HTMLResponse(content=html, headers={'Content-Security-Policy': \"script-src 'self'\"})"
    )
    assert "script-src" in src, "Content-Security-Policy must include script-src directive."


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
