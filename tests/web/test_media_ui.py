"""Static contracts for rights-aware music controls and listener credits."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = REPO_ROOT / "mammamiradio" / "web"
LISTENER_HTML = WEB_ROOT / "templates" / "listener.html"
LISTENER_JS = WEB_ROOT / "static" / "listener.js"
LISTENER_CSS = WEB_ROOT / "static" / "listener.css"
ADMIN_HTML = WEB_ROOT / "templates" / "admin.html"


def _function_block(text: str, name: str) -> str:
    start = text.index(f"function {name}")
    next_function = re.search(r"\n\s*(?:async\s+)?function\s+", text[start + 1 :])
    end = start + 1 + next_function.start() if next_function else len(text)
    return text[start:end]


def test_listener_credits_use_one_dialog_from_two_entry_points() -> None:
    html = LISTENER_HTML.read_text(encoding="utf-8")

    assert html.count('id="music-credits-dialog"') == 1
    assert '<dialog class="mmr-credits-dialog"' in html
    assert 'id="music-credits-inline"' in html
    assert 'id="music-credits-inline"\n         href="#music-credits-dialog" hidden' in html
    assert 'id="music-credits-footer" href="#music-credits-dialog"' in html
    assert html.index('id="np-artist"') < html.index('id="music-credits-inline"')
    assert 'aria-labelledby="music-credits-title"' in html
    assert 'id="music-credits-current"' in html
    assert 'id="music-credits-catalog"' in html
    assert 'id="music-credits-announcement"' in html
    assert 'role="status" aria-live="polite" aria-atomic="true"' in html


def test_listener_credit_data_has_no_html_sink_and_links_fail_closed() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    block = js[js.index("function safeCreditUrl") : js.index("function _liveChipSuffix")]

    assert "url.protocol !== 'https:'" in block
    assert "url.username || url.password" in block
    for host in ("creativecommons.org", "incompetech.com", "jamendo.com"):
        assert host in block
    assert r"^\/licenses\/by\/(3\.0|4\.0)\/?$" in block
    assert "link.rel = 'noopener noreferrer'" in block
    assert "link.target = '_blank'" in block
    assert "document.createElement" in block
    assert "textContent" in block
    assert "replaceChildren" in block
    assert "innerHTML" not in block
    assert "Source information unavailable." in block
    assert "Mamma Mi Radio supplies no license information" in block
    assert "License information supplied by Jamendo." in block
    append_links = _function_block(js, "appendCreditLinks")
    assert "if (!source || !license) return false" in append_links
    assert "links.append(source, license)" in append_links


def test_listener_credit_dialog_is_keyboard_and_phone_safe() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    css = LISTENER_CSS.read_text(encoding="utf-8")
    open_block = _function_block(js, "openMusicCredits")
    trap_block = _function_block(js, "trapCreditsFocus")

    assert "dialog.showModal()" in open_block
    assert "$('music-credits-title')?.focus()" in open_block
    assert "event.key !== 'Tab'" in trap_block
    assert "document.activeElement === heading" in trap_block
    assert "document.activeElement === last" in trap_block
    assert "creditsDialog.addEventListener('close'" in js
    assert "creditsInvoker.focus()" in js

    dialog_rule = re.search(r"\.mmr-credits-dialog\s*\{([^}]*)\}", css, re.DOTALL)
    assert dialog_rule
    assert "max-width: 640px" in dialog_rule.group(1)
    assert "max-height: 80vh" in dialog_rule.group(1)
    assert re.search(r"\.mmr-credits-trigger\s*\{[^}]*min-height:\s*44px", css, re.DOTALL)
    assert re.search(r"\.mmr-credits-close\s*\{[^}]*min-width:\s*44px[^}]*min-height:\s*44px", css, re.DOTALL)
    phone = css[css.index("@media (max-width: 600px)") :]
    assert re.search(r"\.mmr-credits-dialog\s*\{[^}]*height:\s*100dvh", phone, re.DOTALL)
    assert re.search(r"\.mmr-credits-dialog\s*\{[^}]*border-radius:\s*0", phone, re.DOTALL)


def test_admin_has_persistent_source_rows_and_acknowledged_setup() -> None:
    html = ADMIN_HTML.read_text(encoding="utf-8")

    assert 'id="jamendoSourceRow"' in html
    assert 'id="starterSourceRow" data-state="blocked"' in html
    assert 'id="starterSetupRow" data-state="blocked"' in html
    assert "Starter collection on air" in html
    assert "Starter collection unavailable" in html
    assert "Twelve attributed bundled tracks are the offline rotation floor." in html
    assert 'id="sourceJamendoBtn"' not in html
    assert 'id="jamendoSetupHeading"' in html
    assert 'id="jamendoClientId" type="password"' in html
    assert 'id="jamendoEnabled"' in html
    assert 'id="jamendoNoncommercialAck"' in html
    assert "I confirm this station's Jamendo API use is non-commercial" in html
    assert "does not certify broadcast rights" in html
    assert "Provider confirmation for this station model is pending." in html
    assert 'href="https://devportal.jamendo.com/api_terms_of_use"' in html
    assert 'target="_blank" rel="noopener noreferrer"' in html
    assert 'id="jamendoFormMessage" role="status" aria-live="polite" aria-atomic="true"' in html
    assert 'id="jamendoSaveBtn"' in html and "Save and check" in html
    assert "Mamma Mi Radio supplies the access automatically" in html
    assert "no Jamendo account or client ID needed" in html
    assert 'id="jamendoOwnClientIdAction"' in html
    assert "replaceJamendoClientId()" in html
    assert 'id="jamendoClearClientId"' in html
    assert "clearJamendoClientId(this)" in html


def test_admin_maps_internal_jamendo_states_to_five_visible_states() -> None:
    html = ADMIN_HTML.read_text(encoding="utf-8")
    block = _function_block(html, "jamendoPresentation")

    for internal in (
        "'disabled'",
        "'needs_config'",
        "'idle'",
        "'discovering'",
        "'fetching'",
        "'normalizing'",
        "'queued'",
        "'playing'",
        "'consumed'",
        "'ready'",
        "'degraded'",
    ):
        assert internal in block
    for visible in ("state:'idle'", "state:'working'", "state:'ready'", "state:'degraded'", "state:'blocked'"):
        assert visible in block
    for label in (
        "Jamendo off",
        "Finish Jamendo setup",
        "Preparing one Jamendo track",
        "One Jamendo track ready",
        "Jamendo temporarily unavailable",
        "Jamendo track could not be used",
    ):
        assert label in block
    assert "renderJamendoStatus(_st.jamendo||{enabled:false,state:'disabled'})" in html
    assert "announcement:''" in block
    assert "internal==='playing'&&!s.enabled" in block
    assert block.index("internal==='playing'&&!s.enabled") < block.index("!s.enabled||internal==='disabled'")
    assert "Jamendo track finishing" in block
    assert "Jamendo is off. This track will finish and then be deleted." in block


def test_admin_derives_starter_readiness_from_verified_catalog() -> None:
    html = ADMIN_HTML.read_text(encoding="utf-8")
    block = _function_block(html, "renderStarterCatalogStatus")

    assert "Array.isArray(status&&status.starter_catalog)" in block
    assert "catalog.length===12" in block
    assert "Starter collection on air" in block
    assert "Starter collection unavailable" in block
    assert "renderStarterCatalogStatus(_st)" in html


def test_admin_uses_redacted_status_and_locked_configuration_routes() -> None:
    html = ADMIN_HTML.read_text(encoding="utf-8")
    save = _function_block(html, "saveJamendoSettings")
    clear = _function_block(html, "clearJamendoClientId")
    retry = _function_block(html, "retryJamendo")

    assert "const payload={enabled,noncommercial_acknowledged:acknowledged}" in save
    assert "if(clientId)payload.client_id=clientId" in save
    assert "'PUT','/api/media-sources/jamendo',payload" in save
    assert "clear_client_id:true" in clear
    assert "'POST','/api/media-sources/jamendo/retry'" in retry
    assert "client_id_configured" in html
    # The UI may read derived status fields, never the raw client ID.
    assert not re.search(r"status\.client_id(?!_configured|_source)", html)
    assert "_lastJamendoAnnouncementKey&&announcementKey!==_lastJamendoAnnouncementKey" in html


def test_admin_leads_with_included_access_and_only_clears_operator_owned_ids() -> None:
    html = ADMIN_HTML.read_text(encoding="utf-8")
    clear = _function_block(html, "clearJamendoClientId")

    assert re.search(r'id="jamendoClearClientId"[^>]*\bhidden\b', html)
    assert re.search(r'id="jamendoClientField"[^>]*\bhidden\b', html)
    assert "There is no client ID of yours to clear." in clear
    assert "Clear anyway?" not in clear
    assert "Your client ID was cleared" not in clear
    guard = clear.index("if(!ownsId){")
    assert guard < clear.index("confirm(prompt)")
    assert guard < clear.index("mediaSourceRequest(")
    assert "clear_client_id:true" in clear
