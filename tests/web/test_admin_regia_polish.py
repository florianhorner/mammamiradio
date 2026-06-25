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
    """Rotation removal is index-based (/api/playlist/remove) with no id variant,
    so the DELETE must NOT be deferred — a deferred index goes stale once an
    earlier commit shifts the list (Codex P2). The ban commits immediately; an
    optional undo toast lifts it by (artist,title) KEY via /api/track/unban, which
    is index-shift-safe and therefore allowed."""
    html = _html()
    block = html[html.index("async function removeTr") : html.index("// --- Bulk select")]
    assert "/api/playlist/remove" in block
    remove_at = block.index("/api/playlist/remove")
    toast_at = block.find("undoableToast")
    if toast_at != -1:
        # the index delete commits BEFORE the undo toast — not deferred into it
        assert remove_at < toast_at, "the index delete must commit before any undo toast"
        # undo reverses by key, never by re-posting a (now-stale) index delete
        undo_region = block[toast_at:]
        assert "/api/playlist/remove" not in undo_region, "undo must not defer an index delete"
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
)


def test_no_italian_utility_strings_remain() -> None:
    html = _html()
    offenders = [s for s in _ITALIAN_UTILITY_FORBIDDEN if s in html]
    assert not offenders, f"Italian utility copy must be swept to English: {offenders}"


def test_setup_controls_are_english() -> None:
    html = _html()
    for s in ("Save Keys", "Re-check", "Replace", "Runtime Status", "Home Assistant Add-on Snippet"):
        assert s in html


def test_structural_italian_flair_preserved() -> None:
    """The sweep must keep structural section names + on-air flair."""
    html = _html()
    for flair in ("Diretta", "Scaletta", "Rotazione", "Conduttori", "Motore", "Archivio", "In onda"):
        assert flair in html
