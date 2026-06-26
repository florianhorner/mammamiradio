"""Regression guards for the Producer Desk admin information architecture.

Concept B (2026-06-17): a pinned live console (transport + triggers + cooking
feed) above a tabbed work area. Sections are tab panels, swapped one at a time,
not a single vertical scroll of drawers.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

ADMIN_HTML = Path(__file__).resolve().parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"


class _ProducerDeskParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_admin_content = False
        self._main_depth = 0
        self._in_tabbar = False
        self.console_present = False
        self.tab_order: list[str] = []
        self.panel_ids: list[str] = []
        self.panel_names: list[str] = []
        self.drawer_section_ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        classes = set((attr.get("class") or "").split())

        if tag == "main" and {"a-content", "producer-main"}.issubset(classes):
            self._in_admin_content = True
            self._main_depth = 1
            return

        if self._in_admin_content:
            if tag == "main":
                self._main_depth += 1
            if "mmr-console" in classes:
                self.console_present = True
            if tag == "nav" and "mmr-tabbar" in classes:
                self._in_tabbar = True
            tab = attr.get("data-tab")
            if self._in_tabbar and tag == "button" and tab:
                self.tab_order.append(tab)
            if tag == "section" and "mmr-tabpanel" in classes:
                pid = attr.get("id")
                pname = attr.get("data-panel")
                if pid:
                    self.panel_ids.append(pid)
                if pname:
                    self.panel_names.append(pname)
            sid = attr.get("id")
            if tag == "section" and "drawer-section" in classes and sid:
                self.drawer_section_ids.append(sid)

    def handle_endtag(self, tag: str) -> None:
        if self._in_tabbar and tag == "nav":
            self._in_tabbar = False
        if self._in_admin_content and tag == "main":
            self._main_depth -= 1
            if self._main_depth == 0:
                self._in_admin_content = False


def test_admin_has_pinned_console_and_full_tabbar() -> None:
    """The live console must be pinned above a tab bar that names every section."""
    parser = _ProducerDeskParser()
    parser.feed(ADMIN_HTML.read_text(encoding="utf-8"))

    assert not parser._in_admin_content
    assert parser.console_present, "pinned live console (.mmr-console) must be present"
    assert parser.tab_order == [
        "scaletta",
        "diretta",
        "rotazione",
        "conduttori",
        "archivio",
        "motore",
    ]


def test_admin_tabpanels_cover_every_section() -> None:
    """Every section is a tab panel; the work-area content sections survive."""
    parser = _ProducerDeskParser()
    parser.feed(ADMIN_HTML.read_text(encoding="utf-8"))

    # Panels are emitted in DOM order (Scaletta is first / default-active); the
    # tab bar orders them for the operator independently.
    assert parser.panel_names == [
        "scaletta",
        "rotazione",
        "diretta",
        "conduttori",
        "archivio",
        "motore",
    ]
    assert parser.panel_ids == [
        "live-queue",
        "rotation-pool",
        "drawer-steer",
        "drawer-hosts",
        "drawer-history",
        "drawer-diagnostics",
    ]
    # The work-area content sections still live inside their panels.
    assert parser.drawer_section_ids == ["triggers", "hosts", "log", "engine"]


def test_admin_tab_js_shows_one_panel_at_a_time() -> None:
    """The tab controller must activate exactly one panel and persist the choice."""
    html = ADMIN_HTML.read_text(encoding="utf-8")

    assert "function initTabs()" in html
    assert "document.querySelectorAll('.mmr-tab[data-tab]')" in html
    assert "p.classList.toggle('is-active',p.dataset.panel===name)" in html


def test_live_queue_renderer_is_forward_only() -> None:
    """Scaletta must not mix now-playing or played history into the forward queue."""
    html = ADMIN_HTML.read_text(encoding="utf-8")
    block = html[html.index("function renderProgramme") : html.index("async function removeQueueItem")]

    assert "st?.upcoming" in block
    assert "stream_log" not in block
    assert "now_streaming" not in block
    # Relative labels are English-first now (localization sweep T1/E5).
    assert "'next'" in block and "'later'" in block
