"""Static listener guards for the removable Carosello runtime experiment."""

from __future__ import annotations

import re
from pathlib import Path

from mammamiradio.web.ui_copy import COPY

ROOT = Path(__file__).resolve().parents[2]
LISTENER_JS = ROOT / "mammamiradio" / "web" / "static" / "listener.js"
LISTENER_CSS = ROOT / "mammamiradio" / "web" / "static" / "listener.css"
LISTENER_HTML = ROOT / "mammamiradio" / "web" / "templates" / "listener.html"


def _function_block(source: str, name: str, next_name: str) -> str:
    start = source.index(f"function {name}(")
    end = source.index(f"function {next_name}(", start)
    return source[start:end]


def test_ad_roster_prefers_plural_then_singular_and_keeps_generic_fallback() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    normalize = _function_block(js, "normalizeAdBrandNames", "adNowPlayingText")
    display = _function_block(js, "adNowPlayingText", "segmentPillClass")

    assert "Array.isArray(metadata.brands)" in normalize
    assert "normalize([metadata.brand])" in normalize
    assert "source.slice(0, 20)" in normalize
    assert "typeof candidate !== 'string'" in normalize
    assert "candidate.replace(/\\s+/g, ' ').trim().slice(0, 120)" in normalize
    assert "seen.has" not in normalize

    assert "brands.join(' · ')" in display
    assert "_t('np_ad_break', 'This ad break')" in display
    assert "_t('np_ad_message', 'Sponsored message')" in display
    assert "_t('seg_ad', 'Sponsored')" in display


def test_visible_and_media_session_ad_copy_share_one_formatter() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    media = _function_block(js, "updateMediaSession", "renderNowPlayingStrip")
    visible = _function_block(js, "renderNowPlayingStrip", "renderAdExperiment")

    for block in (media, visible):
        assert "const adText = adNowPlayingText(np);" in block
        assert "adText.title" in block
        assert "adText.secondary" in block

    assert js.count("const adText = adNowPlayingText(np);") == 2


def test_carosello_copy_exists_in_both_listener_modes() -> None:
    assert COPY["en"]["np_ad_break"] == "This ad break"
    assert COPY["it"]["np_ad_break"] == "Carosello in onda"
    assert COPY["en"]["ad_session_summary"] == "This session · {n} completed spots"
    assert COPY["it"]["ad_session_summary"] == "Questa diretta · {n} spot completati"
    assert COPY["en"]["ad_session_airings"] == "{n} completed airings"
    assert COPY["it"]["ad_session_airings"] == "{n} passaggi completati"


def test_session_receipt_is_collapsed_bounded_and_clears_on_empty_poll() -> None:
    html = LISTENER_HTML.read_text(encoding="utf-8")
    js = LISTENER_JS.read_text(encoding="utf-8")
    css = LISTENER_CSS.read_text(encoding="utf-8")
    render = _function_block(js, "renderAdExperiment", "renderProgress")
    fetch = _function_block(js, "fetchStatus", "fetchRequests")

    details = re.search(
        r'<details class="mmr-ad-session" id="ad-session-receipt"([^>]*)>',
        html,
    )
    assert details
    assert "hidden" in details.group(1)
    assert "open" not in details.group(1)
    assert 'id="ad-session-summary"' in html
    assert 'aria-live="polite"' in html
    assert 'aria-atomic="true"' in html
    assert 'id="ad-session-brands"' in html

    assert "details.hidden = true;" in render
    assert "list.replaceChildren();" in render
    assert "details.open = false;" in render
    assert "summary.textContent = '';" in render
    assert "completedSpots <= 0" in render
    assert "delete details.dataset.receiptKey;" in render
    assert "details.dataset.receiptKey === receiptKey" in render
    assert render.count("list.replaceChildren();") == 2
    assert render.index("details.dataset.receiptKey === receiptKey") < render.rindex("list.replaceChildren();")
    assert "details.hidden = false;" in render
    assert "renderAdExperiment(status);" in fetch
    assert fetch.index("renderAdExperiment(status);") > fetch.index("if (status.now_streaming)")

    assert re.search(r"\.mmr-ad-session ul\s*\{[^}]*max-height:\s*180px", css, re.DOTALL)
    assert re.search(r"\.mmr-ad-session ul\s*\{[^}]*overflow-y:\s*auto", css, re.DOTALL)
    assert re.search(r"\.mmr-ad-session summary\s*\{[^}]*list-style:\s*none", css, re.DOTALL)


def test_session_receipt_uses_safe_dom_apis_for_wire_data() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    render = _function_block(js, "renderAdExperiment", "renderProgress")

    assert "innerHTML" not in render
    assert "document.createElement('li')" in render
    assert render.count("document.createElement('span')") == 2
    assert "name.textContent = entry.brand;" in render
    assert "airings.textContent = entry.airings;" in render
    assert "row.append(name, airings);" in render
    assert "list.appendChild(row);" in render


def test_session_receipt_cannot_widen_the_mobile_stage() -> None:
    css = LISTENER_CSS.read_text(encoding="utf-8")

    assert re.search(r"\.mmr-ad-session\s*\{[^}]*width:\s*100%", css, re.DOTALL)
    assert re.search(r"\.mmr-ad-session\s*\{[^}]*max-width:\s*420px", css, re.DOTALL)
    assert re.search(
        r"\.mmr-ad-session li\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)\s+auto",
        css,
        re.DOTALL,
    )
    assert re.search(r"\.mmr-ad-session-brand\s*\{[^}]*min-width:\s*0", css, re.DOTALL)
    assert re.search(r"\.mmr-ad-session-brand\s*\{[^}]*overflow-wrap:\s*anywhere", css, re.DOTALL)
