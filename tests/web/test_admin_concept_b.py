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
