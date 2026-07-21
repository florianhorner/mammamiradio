"""Behavior tests for the Regia admin UX polish pass.

Covers the new behaviors introduced by the regia-admin-ux-feedback branch:
undo toast (admin.js), Diretta drawer ARIA subgroups, Motore three-group split
with Setup auto-collapse, Archivio sessionStorage filters, Scaletta 3-tier
responsive, the combined mode chip, colorblind shape cues, and the
English-first localization sweep.

These are DOM/source-string parse tests in the same style as the sibling admin
invariant suites (no browser). Real-browser flows are covered by /qa.
"""

from __future__ import annotations

from pathlib import Path

ADMIN_HTML = Path(__file__).resolve().parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"
ADMIN_JS = Path(__file__).resolve().parents[2] / "mammamiradio" / "web" / "static" / "admin.js"


def _html() -> str:
    return ADMIN_HTML.read_text(encoding="utf-8")


def _js() -> str:
    return ADMIN_JS.read_text(encoding="utf-8")


# ── admin.js extraction + helpers (E1) ──────────────────────────────


def test_admin_js_is_loaded_by_template() -> None:
    assert '<script src="/static/admin.js" defer></script>' in _html()


def test_admin_js_exposes_expected_helpers() -> None:
    js = _js()
    for fn in (
        "window.undoableToast",
        "window.errorToast",
        "window.archivioFilterPersist",
        "window.archivioFilterRestore",
        "window.motoreSetupAutoCollapse",
        "window.modeChipRender",
    ):
        assert fn in js, f"admin.js must expose {fn}"


def test_undo_toast_window_and_stack_cap() -> None:
    js = _js()
    assert "const DEFAULT_TTL = 5000" in js, "undo window is 5s (D4)"
    assert "const MAX_TOASTS = 5" in js, "stack capped at 5 (E8)"
    # aria-live so screen readers announce removals.
    assert "aria-live" in js and "'status'" in js


def test_undo_toast_button_is_focusable_target() -> None:
    js = _js()
    assert "undo-toast-btn" in js
    assert "btn.focus(" in js


# ── Diretta drawer rename + ARIA subgroups (T4) ─────────────────────


def test_steer_panel_titled_diretta_not_regia() -> None:
    html = _html()
    # Concept B: Diretta is a tab + a single panel header (no drawer summary).
    assert 'data-tab="diretta">Diretta</button>' in html
    assert "<h2>Diretta</h2>" in html
    assert '<span class="drawer-title">Regia</span>' not in html
    # The sr-only / subtitle "Regia — Control Room" is a protected ingress label
    # and must survive.
    assert "Regia — Control Room" in html


def test_diretta_subgroups_have_role_group_and_labelledby() -> None:
    html = _html()
    # "Azioni immediate" (dg-now-h) moved to the pinned console; the Diretta tab
    # keeps Modalità live, Azioni rapide, and Cadenza.
    for hid in ("dg-modes-h", "dg-quick-h", "dg-pacing-h"):
        assert f'aria-labelledby="{hid}"' in html, f"subgroup must reference {hid}"
        assert f'id="{hid}"' in html, f"subgroup header {hid} must exist"
    assert html.count('class="drawer-subgroup"') >= 3


def test_super_italian_mode_lives_in_diretta_live_modes() -> None:
    html = _html()
    modes = html.index('id="dg-modes-h"')
    festival = html.index('id="festivalToggle"')
    super_italian = html.index('id="superItalianToggle"')
    quick_actions = html.index('id="dg-quick-h"')
    motore = html.index("<h2>Motore</h2>")

    assert html.count('id="superItalianToggle"') == 1
    assert modes < festival < super_italian < quick_actions, (
        "Super Italian Mode belongs in Diretta's live-mode group after Festival, before quick actions."
    )
    assert super_italian < motore, "Super Italian Mode must not drift back into Motore."


def test_mode_toggles_keep_shape_icons() -> None:
    """Chaos/Festival toggles pair color with a shape icon (colorblind safety)."""
    html = _html()
    assert 'class="chaos-bolt"' in html  # ⚡
    assert 'class="festival-star"' in html  # ★


# ── Combined mode chip (T9) ─────────────────────────────────────────


def test_mode_chip_element_and_wiring() -> None:
    html = _html()
    assert 'id="modeChip"' in html
    assert "modeChipRender" in html  # wired in chaos/festival state updaters
    js = _js()
    assert "MODES: " in js


# ── Undo safety + restore ordering (Codex review regressions) ───────


def test_queue_removal_defers_by_stable_id_not_index() -> None:
    """removeQueueItem must defer using the stable queue id, never a row index —
    deferred index-based commits delete the wrong row after the list shifts."""
    html = _html()
    block = html[html.index("async function removeQueueItem") : html.index("function statusDot")]
    assert "undoableToast" in block
    assert "/api/queue/remove" in block and "{id}" in block


def test_rotation_removal_commits_immediately() -> None:
    """Rotation removal commits its revision/index/id target immediately.

    Deferring even a guarded snapshot would guarantee a stale conflict after an
    earlier mutation. The optional undo instead lifts the committed ban by the
    canonical artist/title key, which remains safe after the playlist shifts.
    """
    html = _html()
    block = html[html.index("async function removeTr") : html.index("// --- Bulk select")]
    assert "/api/playlist/remove" in block
    remove_at = block.index("/api/playlist/remove")
    toast_at = block.find("undoableToast")
    if toast_at != -1:
        # The guarded delete commits BEFORE the undo toast — not deferred into it.
        assert remove_at < toast_at, "the guarded delete must commit before any undo toast"
        # Undo reverses by key, never by re-posting a now-stale row snapshot.
        undo_region = block[toast_at:]
        assert "/api/playlist/remove" not in undo_region, "undo must not defer a row-target delete"
        assert "/api/track/unban" in undo_region


def test_archivio_filters_restored_after_deferred_helpers_load() -> None:
    """admin.js is deferred, so the inline restore must run on DOMContentLoaded
    (after defer), not at parse time when window.archivioFilterRestore is still
    undefined (Codex P2)."""
    html = _html()
    # Parse-time init is plain defaults, not a restore call.
    assert "let _archivioFilters={q:'',type:'all',scope:'all'};" in html
    assert "_initArchivioFilters" in html
    init_at = html.index("_initArchivioFilters")
    assert "DOMContentLoaded" in html[init_at - 400 : init_at + 400]


# ── Motore three-group split + Setup auto-collapse (T5) ─────────────


def test_motore_three_groups_present() -> None:
    html = _html()
    for hid in ("eg-status-h", "eg-costs-h", "eg-setup-h"):
        assert f'id="{hid}"' in html


def test_setup_group_is_collapsible_with_ready_badge() -> None:
    html = _html()
    assert 'id="setupGroup"' in html
    assert "setup-group" in html
    assert 'id="setupReadyBadge"' in html
    assert "All ready" in html


def test_golden_path_setup_strip_sits_between_console_and_tabs() -> None:
    html = _html()
    assert 'id="setupStrip"' in html
    assert 'id="setupStripChips"' in html
    assert 'id="setupStripActions"' in html
    assert "openListener()" in html
    assert 'id="on-air"' in html and 'id="adminTabs"' in html
    assert html.index('id="on-air"') < html.index('id="setupStrip"') < html.index('id="adminTabs"')
    assert 'data-tab="motore">Motore' in html
    assert 'data-tab="setup"' not in html


def test_setup_strip_renders_api_primary_action_not_static_dual_buttons() -> None:
    html = _html()
    strip = html[html.index('id="setupStrip"') : html.index("</section>", html.index('id="setupStrip"'))]
    assert 'id="setupStripActions"' in strip
    assert 'id="setupStripListenerBtn"' not in strip
    assert 'id="setupStripReviewBtn"' not in strip
    block = html[html.index("function renderGuidedSetupStrip") : html.index("function shouldShowHomeContextPreview")]
    assert "stripData=setup?.guided_setup?.strip" in block
    assert "stripData.primary_action" in block
    assert "primary.label" in block
    assert "primary.target" in block
    assert "guided.stream" not in block
    assert "guidedShape" not in block
    assert "setupStripListenerBtn" not in block
    assert "setupStripReviewBtn" not in block


def test_home_context_preview_loads_only_when_guided_action_is_reviewable() -> None:
    html = _html()
    assert 'id="homeContextPreviewSection"' in html
    gate = html[html.index("function shouldShowHomeContextPreview") : html.index("function renderHomeContextPreview")]
    assert "review_home_context" in gate
    assert "home.status==='ready'" in gate
    assert "home.status==='empty'" in gate
    render_block = html[html.index("function renderSetup") : html.index("async function setupRecheck")]
    assert "renderHomeContextPreviewGate(setup)" in render_block
    assert "loadHomeContextPreview();" not in render_block


def test_setup_strip_treats_not_configured_home_context_as_done_not_a_todo() -> None:
    """Home Assistant is optional — a standalone station without it must not
    show a permanent 'needs attention' banner (regression: it used to render
    'blocked', an error-styled state that never resolved for non-HA users)."""
    html = _html()
    block = html[html.index("function renderGuidedSetupStrip") : html.index("async function setupRecheck")]
    assert "stripData.attention_required&&entries.length" in block
    assert "item.status!=='not_configured'" not in block
    strip_chip_error_rule = next(
        line for line in html.splitlines() if line.strip().startswith('.setup-strip-chip[data-s="blocked"]')
    )
    strip_chip_warn_rule = next(
        line for line in html.splitlines() if line.strip().startswith('.setup-strip-chip[data-s="missing"]')
    )
    assert "not_configured" not in strip_chip_error_rule
    assert "not_configured" not in strip_chip_warn_rule


def test_setup_keys_prioritize_one_ai_host_key_and_collapse_premium_voices() -> None:
    html = _html()
    assert "One key is enough" in html
    assert "AI hosts" in html
    assert 'id="setupPremiumVoices"' in html
    assert "Premium voices" in html
    assert html.index('id="setupOpenaiKey"') < html.index('id="setupPremiumVoices"')
    assert html.index('id="setupAzureSpeechKey"') > html.index('id="setupPremiumVoices"')


def test_home_context_preview_is_mute_only_and_uses_sanitized_endpoint() -> None:
    html = _html()
    assert 'id="haContextPreview"' in html
    assert "/api/homeassistant/context-candidates" in html
    assert "/api/homeassistant/entity-policy" in html
    block = html[html.index("function renderHomeContextPreview") : html.index("async function loadHomeContextPreview")]
    assert "Mute for future host use" in block
    assert "aria-label" in block
    assert "Unmute" in block
    for forbidden in ("Approve", "Prefer", "Whitelist", "ranking", "score"):
        assert forbidden not in block


def test_setup_auto_collapse_wired_into_render() -> None:
    html = _html()
    assert "motoreSetupAutoCollapse(!needsAction)" in html
    js = _js()
    assert "details.dataset.userPinned" in js, "manual pin must override auto-collapse"


def test_token_cost_counter_survives_in_costs_group() -> None:
    """Protected element: token cost (engineRuntime) must stay visible, not in
    the collapsible Setup group."""
    html = _html()
    assert 'id="engineRuntime"' in html
    assert "api_cost_estimate_usd" in html or "apiCostEl" in html
    # engineRuntime must appear before the collapsible setupGroup (i.e. in the
    # always-visible Costi group).
    assert html.index('id="engineRuntime"') < html.index('id="setupGroup"')


def test_cost_split_survives_in_costs_group_before_segment_counts() -> None:
    html = _html()
    assert 'id="engineCostSplit"' in html
    assert "cost_breakdown" in html
    assert "Cost split" in html
    assert "Host scripts" in html
    assert "Ad scripts" in html
    assert "Memory" in html
    assert "Voice synthesis" in html
    assert html.index('id="engineCostSplit"') < html.index('id="engineSegments"') < html.index('id="setupGroup"')


def test_session_cost_reframe_template_invariants() -> None:
    """Protected cost display: the token-cost estimate copy and render targets
    survive the Concept B console rewrite. The 'Produced · Session N' segment
    counter was intentionally dropped (operator-noise); the cost stays."""
    html = _html()

    assert "AI cost · 24h" not in html
    # Token cost render targets survive (protected element).
    for target in ("sidebarCost", "topBarCost", "apiCostEl"):
        assert target in html
    # The meaningless segment-count headline + its writer were retired.
    assert "sidebarSegments" not in html

    assert ".toFixed(4)" not in html
    assert "'<$1'" in html
    assert "'~$'+Math.round(_rawCost)" in html


def test_tts_only_session_keeps_a_single_session_estimate_row() -> None:
    """Paid TTS must not disappear just because the LLM call count is zero."""
    html = _html()

    assert "const hasPaidUsage=apiCalls>0||ttsCh>0;" in html
    assert 'Session estimate: <strong id="apiCostEl">' in html
    assert html.count('id="apiCostEl"') == 1


def test_generated_waste_cost_zero_state_renders_dollar_zero() -> None:
    """A clean session (waste cost 0.0) must render "$0", never "<$1" — the
    waste row must never imply spend when nothing was wasted (#397)."""
    html = _html()
    # The zero-state branch is the protected invariant: $0 at exactly zero,
    # "<$1" only for a positive sub-dollar cost, "~$N" at a dollar or more.
    assert "wasteCost<=0?'$0'" in html


# ── Archivio filters + sessionStorage (T6) ──────────────────────────


def test_archivio_filter_controls_present() -> None:
    html = _html()
    assert 'id="logSearch"' in html
    for label in ("All", "Music", "Hosts", "Ads", "News"):
        assert f">{label}</button>" in html
    for label in ("Last hour", "Today", "All available"):
        assert label in html


def test_archivio_sessionstorage_key() -> None:
    js = _js()
    assert "mmr.admin.archivio.filters" in js
    assert "sessionStorage" in js


def test_archivio_empty_states() -> None:
    html = _html()
    assert "Nothing logged yet." in html
    assert "No matches." in html
    assert "clearArchivioFilters()" in html


# ── Scaletta 3-tier responsive (T3) ─────────────────────────────────


def test_scaletta_tablet_breakpoints() -> None:
    html = _html()
    assert "@media (min-width: 769px) and (max-width: 1023px)" in html
    assert "@media (min-width: 769px) and (max-width: 880px)" in html


def test_scaletta_no_global_overflow_wrap_anywhere() -> None:
    html = _html()
    assert ".a-programme th, .a-programme td { overflow-wrap: anywhere; }" not in html
    assert ".a-programme td.ti, .a-programme td.ho { overflow-wrap: anywhere; }" in html


def test_predicted_rows_render_no_action_button() -> None:
    html = _html()
    block = html[html.index("function renderProgramme") : html.index("async function removeQueueItem")]
    assert "disabled>·</button>" not in block
    assert "const action=actionable" in block


# ── Conduttori preset chip colorblind cue (T10) ─────────────────────


def test_active_preset_chip_has_checkmark_cue() -> None:
    html = _html()
    assert 'class="preset-check"' in html
    assert ".host-preset.active .preset-check { display: inline;" in html


# ── Rotation drag handle (T7) ───────────────────────────────────────


def test_rotation_drag_handle_is_six_dot_and_focusable() -> None:
    html = _html()
    assert 'class="grip-dots"' in html
    assert html.count('<circle cx="3"') >= 1 and html.count('<circle cx="7"') >= 1
    assert 'class="pl-grip"' in html
    assert ".pl-grip:focus-visible" in html


# ── English-first localization sweep (T1/E5) ────────────────────────

# Utility strings that must NOT reappear in admin.html. Structural Italian
# flair (Diretta, Scaletta, Rotazione, Conduttori, Motore, Archivio, the
# "In onda"/"Fermo" on-air badge, Anni '70/'80/'90 era chips) is allowed.
_ITALIAN_UTILITY_FORBIDDEN = (
    "Caricamento",
    "Controllo configurazione",
    "Salva chiavi",
    "Sostituisci",
    "Ricontrolla",
    "Cadenza salvata",
    "Cadenza non salvata",
    "Coda svuotata",
    "Salto preparato",
    "Nessuna richiesta",
    "Nessun risultato",
    "Nessun brano",
    "Stazione in pausa",
    "Costruzione scaletta",
    "Sto preparando",
    "Aggiungi classifiche",
    "Cerca musica nella rotazione",
    "Modalità italiana",
    "sconosciuto",
    "Errore di rete",
    "rotazione corrente",
    "Chiavi AI configurate",
    "prossimo':idx===1?'poi",
    "segmenti pronti",
    "— prossimo",
    " aggiornato'",
    "'Saltato'",
    "applicato",
    ">Classifiche<",
    "Rotazione svuotata",
    "può tornare dopo un riavvio",
    "Svuota tutto?",
    ">Svuota tutto<",
)


def test_no_italian_utility_strings_remain() -> None:
    html = _html()
    offenders = [s for s in _ITALIAN_UTILITY_FORBIDDEN if s in html]
    assert not offenders, f"Italian utility copy must be swept to English: {offenders}"


def test_admin_host_controls_are_english_first() -> None:
    html = _html()
    host_block = html[html.index("// ── Hosts ──") : html.index("function syncHostPresetActive")]

    assert '<html lang="en">' in html
    for expected in (
        "Energy",
        "Warmth",
        "Verbosity",
        "Nostalgia",
        "Ordered",
        "Spontaneous",
        "Anarchic",
        "Talkative",
        "Nonstop",
        "Time travel",
        "Balanced",
        "Calm",
        "Hype",
        "Swerves mid-sentence",
        "Listeners feel personally pulled into the room.",
        "Expect Sanremo callbacks between songs.",
    ):
        assert expected in host_block

    forbidden = (
        "Energia",
        "Calore",
        "Verbosità",
        "Accesa",
        "Anarchico",
        "Caldo",
        "Loquace",
        "EQUILIBRATO",
        "CALMO",
        "Virate a metà frase",
        "Gli ascoltatori",
        "Aspettati riferimenti",
    )
    offenders = [s for s in forbidden if s in host_block]
    assert not offenders, f"Host controls must stay English-first: {offenders}"


def test_setup_controls_are_english() -> None:
    html = _html()
    for s in ("Save AI key", "Re-check", "Replace", "Runtime Status", "Home Assistant secrets.env Snippet"):
        assert s in html


def test_structural_italian_flair_preserved() -> None:
    """The sweep must keep structural section names + on-air flair."""
    html = _html()
    for flair in ("Diretta", "Scaletta", "Rotazione", "Conduttori", "Motore", "Archivio", "In onda"):
        assert flair in html


def test_motore_runtime_groups_precede_setup() -> None:
    html = _html()
    header = html[html.index("<h2>Motore</h2>") : html.index('id="pipelineStatus"')]
    assert "pipeline · runtime · costi" in header
    assert 'id="engineAlertDot"' in header
    pipeline = html.index('id="pipelineStatus"')
    status = html.index('id="eg-status-h"')
    costs = html.index('id="eg-costs-h"')
    setup = html.index('id="setupGroup"')
    assert pipeline < status < costs < setup, (
        "Motore must show Pipeline, Status, and Costi before the collapsible Setup group."
    )
    config = html.index('id="eg-config-h"')
    quality = html.index('id="qualityProfile"')
    broadcast_chain = html.index('id="broadcastChainToggle"')
    assert costs < config < quality < broadcast_chain < setup, (
        "Motore configuration controls must keep AI Quality and On-Air Sound grouped "
        "after runtime Costi and before Setup."
    )


def test_home_moment_drop_reasons_explain_changed_show_state() -> None:
    """The admin-only receipt trail explains why a matched moment was held back.

    These are operator explanations, never raw discard codes or the generic
    fallback label: a full directive slot and a changing playlist/source/Chaos
    mode each need an actionable, human-readable account.
    """
    html = _html()
    block = html[html.index("function momentStatusLabel") : html.index("function updateMoments")]

    for reason, label in (
        ("directive_slot_busy", "stepped aside — another moment was already lined up"),
        ("interrupt_slot_busy", "stepped aside — another moment was already lined up"),
        ("stale_playlist", "set aside when the playlist changed — the hosts are following the new rundown"),
        ("stale_source", "set aside when the music source changed — the hosts are following the new source"),
        ("source_switch", "set aside when the music source changed — the hosts are following the new source"),
        ("stale_chaos", "set aside when Chaos Mode changed — the hosts are following the new direction"),
    ):
        assert reason in block
        assert label in block
    assert (
        "if(r==='stale_source'||r==='source_switch')"
        "return{text:'set aside when the music source changed — "
        "the hosts are following the new source',color:'var(--muted)'};"
    ) in block


def test_format_request_age_guards_invalid_values() -> None:
    html = _html()
    assert "function formatRequestAge" in html
    block = html[html.index("function formatRequestAge") : html.index("function _statusSpan")]
    assert "Number.isFinite" in block
    assert "'s ago'" in block
    assert "'m ago'" in block
    assert " fa" not in block
    listener_block = html[html.index("function updateListenerRequests") : html.index("let _lrReqs")]
    assert "formatRequestAge(r.age_s)" in listener_block


def test_conduttori_presets_sync_active_state_from_slider_values() -> None:
    html = _html()
    assert "function syncHostPresetActive" in html
    assert "HOST_PRESETS" in html
    assert "b.classList.toggle('active',b.dataset.preset===match)" in html.replace(" ", "")


def test_undo_toast_reserves_mobile_safe_space() -> None:
    html = _html()
    assert "body.undo-toast-active" in html
    assert ".undo-stack" in html
    js = _js()
    assert "undo-toast-active" in js
    assert "_syncToastBodyClass" in js


def test_undo_toast_cap_only_commits_older_undo_toasts() -> None:
    js = _js()
    block = js[js.index("function undoableToast") : js.index("/**\n   * Show a plain error toast")]
    assert "_countToastsOfKind('undo') >= MAX_TOASTS" in block
    assert "const oldestUndo = _oldestToastOfKind('undo')" in block
    assert "_dismiss(oldestUndo, { runCommit: true })" in block
    assert "kind: 'undo'" in block


def test_error_toast_uses_same_safe_space_accounting_as_undo_toast() -> None:
    js = _js()
    block = js[js.index("function errorToast") : js.index("// ── Archivio")]
    assert "const MAX_ERROR_TOASTS = 2" in js
    assert "_countToastsOfKind('error') >= MAX_ERROR_TOASTS" in block
    assert "const oldestError = _oldestToastOfKind('error')" in block
    assert "_dismiss(oldestError, { runCommit: false })" in block
    assert "runCommit: true" not in block
    assert "kind: 'error'" in block
    assert "const entry" in block
    assert "_live.push(entry)" in block
    assert "_syncToastBodyClass()" in block
    assert "_dismiss(entry, { runCommit: false })" in block


# ── Admin a11y structure: page h1 + ARIA tab pattern ────────────────


def test_admin_has_h1_with_brand_accent() -> None:
    """Admin must declare an <h1> so headings don't start at <h2> (WCAG 2.4.6).

    The brand wordmark carries it, keeping the gold "Mi" protected accent inside
    the document title (docs/design/admin-panel.md protected-elements list)."""
    html = _html()
    assert '<h1 class="wm">' in html, "the brand wordmark must be the page <h1>."
    h1 = html[html.index('<h1 class="wm">') : html.index("</h1>")]
    assert 'class="mi"' in h1, "the gold Mi accent must live inside the <h1>."


def test_admin_tabs_use_aria_tab_pattern() -> None:
    """Section tabs must implement the ARIA tablist/tab/tabpanel pattern so screen
    readers announce selection state and panel association (WCAG 4.1.2)."""
    html = _html()
    assert 'role="tablist"' in html, "the tab bar must be a role=tablist."
    assert html.count('role="tab"') >= 6, "each section tab must declare role=tab."
    assert html.count('role="tabpanel"') >= 6, "each section panel must declare role=tabpanel."
    # Cross-linking: a tab points at its panel and the panel back at the tab.
    assert 'aria-controls="live-queue"' in html and 'aria-labelledby="tab-scaletta"' in html, (
        "tabs and panels must be cross-linked via aria-controls / aria-labelledby."
    )
    js = _html()  # inline tab JS lives in admin.html
    assert "setAttribute('aria-selected'" in js, "active tab must toggle aria-selected."
