"""Guards for the Concept B producer desk: pinned console, cooking feed in the
console, and a rotation selection that survives the 3s poll re-render.

Background: the rotation checkboxes used to live only in the DOM, so the 3s
`refreshFast()` rebuild wiped them ("tick a box, it un-ticks itself"). Selection
now lives in a JS Set keyed by a stable artist/title key and is re-applied on
every rebuild. These are source-level guards against reverting to DOM-only state.
"""

from __future__ import annotations

from pathlib import Path

ADMIN_HTML = Path(__file__).resolve().parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"


def _html() -> str:
    return ADMIN_HTML.read_text(encoding="utf-8")


def test_console_holds_triggers_and_cooking_feed() -> None:
    """The four air-next triggers and the live In-Produzione feed live in the console."""
    html = _html()
    console = html[html.index('class="mmr-console"') : html.index('class="mmr-tabbar"')]
    for trig in ("doTrigger('banter'", "doTrigger('ad'", "doTrigger('news_flash'", "doQuickAction('more_chaos'"):
        assert trig in console, f"console must hold the {trig} trigger"
    assert 'id="queue-production"' in console
    assert 'id="productionFeed"' in console


def test_meaningless_session_label_is_gone_cost_stays() -> None:
    """'Produced · Session N' was operator-noise; the token cost stays (protected)."""
    html = _html()
    assert "Produced · Session" not in html
    assert 'id="sidebarCost"' in html  # protected token-cost element
    # the dead, no-op sort label was removed
    assert "order: current rotation" not in html


def test_rotation_selection_survives_rerender() -> None:
    """Checkbox state is driven by a persistent Set, re-applied on every updatePl()."""
    html = _html()
    # stable key + persistent Set
    assert "let _plSelected=new Set();" in html
    assert "function _plKey(artist,title)" in html
    # the row template pre-checks from the Set so a poll rebuild can't wipe it
    assert "_plSelected.has(_plKey(t.artist,t.title))?' checked':''" in html
    # the click handler maintains the Set (not just the DOM)
    assert "_plSelected.add(k)" in html and "_plSelected.delete(k)" in html
    # bulk ban reads the Set, not just the visible checked rows
    assert "[..._plSelected].map(k=>JSON.parse(k))" in html


def test_pl_key_mirrors_server_normalized_track_key() -> None:
    """JS _plKey() must mirror playlist.normalized_track_key — bulk-ban selection
    keys songs the same way the backend blocklist does. If the two drift, a
    selected row maps to a different ban key than the server stores (silent
    mis-ban that only shows up in production)."""
    from mammamiradio.core.models import Track
    from mammamiradio.playlist.playlist import normalized_track_key

    # Server contract: strip + lowercase both fields.
    track = Track(title="  Thunder ", artist=" AC/DC ", duration_ms=1000, spotify_id="x")
    assert normalized_track_key(track) == ("ac/dc", "thunder")

    # JS mirror must apply the SAME transforms to BOTH fields.
    html = _html()
    assert "function _plKey(artist,title)" in html
    assert "(artist||'').trim().toLowerCase()" in html
    assert "(title||'').trim().toLowerCase()" in html


def test_console_and_tabbar_share_one_sticky_deck() -> None:
    """Console + tab bar must pin as a single sticky unit, not two competing
    stickies (which buried the tabs behind the console on scroll)."""
    html = _html()
    assert 'class="mmr-deck"' in html
    assert ".mmr-deck{position:sticky" in html
    # the individual elements must NOT each declare their own sticky/top
    assert ".mmr-console{position:sticky" not in html
    tabbar_rule_start = html.index(".mmr-tabbar{")
    tabbar_rule = html[tabbar_rule_start : tabbar_rule_start + 200]
    assert "position:sticky" not in tabbar_rule


def test_motore_tab_alert_is_wired() -> None:
    """The Motore tab alert dot must be driven by the same needs-action signal as
    the in-panel dot, so 'setup needs attention' is visible from any tab."""
    html = _html()
    assert "getElementById('motoreTabAlert')" in html


def test_empty_pending_requests_collapses() -> None:
    """The pending-requests card hides itself when there is nothing to show."""
    html = _html()
    block = html[html.index("function updateListenerRequests") :]
    block = block[: block.index("function ", 1)]
    assert "getElementById('queue-requests')" in block
    assert "wrap.style.display='none'" in block


def test_poll_renders_only_the_active_tab() -> None:
    """The 3s poll routes heavy list renders through renderActiveTab() (visible tab
    only). A regression that drops the dispatch (panels go stale) or reverts to
    rendering all three lists every poll (wasted work) must fail here."""
    html = _html()
    assert "function renderActiveTab()" in html
    refresh = html[html.index("async function refreshFast()") : html.index("async function refreshSlow()")]
    assert "renderActiveTab();" in refresh
    # old unconditional trio must NOT run every poll
    assert "renderProgramme(_st);" not in refresh
    assert "updatePl(_st.playlist" not in refresh
    # the gate maps each tab to its renderer
    rat = html[html.index("function renderActiveTab()") : html.index("async function refreshFast()")]
    assert "_activeTab==='rotazione'" in rat and "updatePl(" in rat
    assert "_activeTab==='archivio'" in rat and "updateLog(" in rat
    assert "_activeTab==='scaletta'" in rat and "renderProgramme(" in rat


def test_select_all_is_data_driven() -> None:
    """Select-all iterates the loaded data (_plRows), not just DOM-rendered rows, so
    the count and ban scope match what 'Select all' means even under a filter."""
    html = _html()
    line_start = html.index("function selectAllPl()")
    sel = html[line_start : html.index("\n", line_start)]
    assert "_plRows.forEach" in sel
    assert "_plSelected.add(_plKey(" in sel


def test_active_tab_persists_in_sessionstorage() -> None:
    """The chosen tab persists across reloads; an unknown stored tab falls back to
    Scaletta so the work area is never blank."""
    html = _html()
    init = html[html.index("function initTabs()") : html.index("initTabs();")]
    assert "sessionStorage.setItem('adminTab'" in init
    assert "sessionStorage.getItem('adminTab')" in init
    assert "name='scaletta'" in init  # unknown-tab fallback


def test_ban_trigger_is_always_visible_not_hover_gated() -> None:
    """The per-row ban control must be reachable on touch (no :hover). It carries a
    dedicated .pl-ban class OUTSIDE the hover-gated .pl-a actions block, and the CSS
    keeps it at opacity:1. Regression: dropping it back into .pl-a (hover-only) makes
    it untappable on tablet/phone — the exact bug that made the feature unusable."""
    html = _html()
    update = html[html.index("function updatePl(") : html.index("async function loadMorePlaylist")]
    # ban button is its own labeled, always-visible control, separate from .pl-a
    assert 'class="pl-btn pl-ban"' in update
    assert 'aria-label="Ban from rotation"' in update
    assert "removeTr(${idx})" in update
    # the ban button is a SIBLING after .pl-a closes (Next stays in the hover block,
    # Ban sits outside it) — proven by the close-tag immediately preceding .pl-ban
    assert '</div><button class="pl-btn pl-ban"' in update
    pl_a_open = update.index('<div class="pl-a">')
    pl_a = update[pl_a_open : update.index("</div>", pl_a_open)]
    assert "removeTr(" not in pl_a, "ban button must not live in the hover-gated .pl-a"
    assert "moveNext(" in pl_a
    # CSS keeps the ban control visible without hover
    assert ".pl-ban {" in html and "opacity: 1" in html[html.index(".pl-ban {") : html.index(".pl-ban {") + 120]


def test_banned_manager_shows_count_and_open_state() -> None:
    """The 'Banned' button reads as a manager: a live count badge and an
    open/closed affordance, not another silent 'Add' chip."""
    html = _html()
    assert 'id="banlistToggle"' in html
    assert 'id="banlistCount"' in html
    assert 'aria-expanded="false"' in html[html.index('id="banlistToggle"') : html.index('id="banlistToggle"') + 250]
    # count helper drives badge + first-use hint, and is fetched on load
    assert "async function refreshBanCount()" in html
    assert "refreshBanCount();" in html
    # setBanCount must actually gate BOTH the badge and the hint on n>0 — not just
    # exist. The whole UX promise (badge appears, hint disappears after first ban)
    # lives in this branch; an inverted condition would otherwise pass silently.
    setcount = html[html.index("function setBanCount(n)") : html.index("async function refreshBanCount()")]
    assert "n>0" in setcount
    assert "badge.style.display" in setcount
    assert "hint.style.display=n>0?'none':''" in setcount
    # the badge must refresh after BOTH ban paths, or it goes stale post-ban
    remove_fn = html[html.index("async function removeTr(") : html.index("// --- Bulk select")]
    assert "refreshBanCount()" in remove_fn, "removeTr must refresh the ban count"
    bulk_fn = html[html.index("async function banSelected()") : html.index("function setBanCount(n)")]
    assert "refreshBanCount()" in bulk_fn, "banSelected must refresh the ban count"
    # toggle flips the open-state affordance both ways (open AND close)
    toggle = html[html.index("async function toggleBanlist()") : html.index("async function renderBanlist()")]
    assert "aria-expanded','true'" in toggle and "aria-expanded','false'" in toggle
    assert "classList.add('active')" in toggle and "classList.remove('active')" in toggle


def test_single_ban_is_undoable_by_key() -> None:
    """A single ✕ Ban shows an undo toast that lifts the ban by (artist,title) key —
    index-shift-safe, since the row index changes the moment the ban lands."""
    html = _html()
    fn = html[html.index("async function removeTr(") : html.index("// --- Bulk select")]
    assert "window.undoableToast" in fn
    assert "/api/track/unban" in fn
    assert "data-artist" in fn and "data-title" in fn


def test_rotation_pool_has_first_use_ban_hint() -> None:
    """The rotation pool carries inline guidance on how to ban/manage, shown until
    the operator has banned at least one song (state-driven first-use)."""
    html = _html()
    assert 'id="banHint"' in html
    hint = html[html.index('id="banHint"') : html.index('id="banHint"') + 220]
    assert "Ban" in hint and "Banned" in hint
