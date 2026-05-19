"""Regression guards for the Producer Desk admin information architecture."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

ADMIN_HTML = Path(__file__).resolve().parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"


class _ProducerDeskParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_admin_content = False
        self._in_drawers = False
        self._main_depth = 0
        self._drawers_depth = 0
        self.content_sections: list[str] = []
        self.drawer_ids: list[str] = []
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
            section_id = attr.get("id")
            if tag == "section" and "producer-zone" in classes and section_id:
                self.content_sections.append(section_id)
            if tag == "section" and section_id == "producer-drawers":
                self._in_drawers = True
                self._drawers_depth = 1
                return
            if tag == "details" and "producer-drawer" in classes and section_id:
                self.drawer_ids.append(section_id)

        if self._in_drawers and tag == "section":
            self._drawers_depth += 1
            section_id = attr.get("id")
            if "drawer-section" in classes and section_id:
                self.drawer_section_ids.append(section_id)

    def handle_endtag(self, tag: str) -> None:
        if self._in_drawers and tag == "section":
            self._drawers_depth -= 1
            if self._drawers_depth == 0:
                self._in_drawers = False

        if self._in_admin_content and tag == "main":
            self._main_depth -= 1
            if self._main_depth == 0:
                self._in_admin_content = False


def test_admin_producer_desk_sections_follow_visual_order() -> None:
    """Default `/admin` view must be the three-zone Producer Desk."""
    parser = _ProducerDeskParser()
    parser.feed(ADMIN_HTML.read_text(encoding="utf-8"))

    assert not parser._in_admin_content
    assert not parser._in_drawers
    assert parser.content_sections == [
        "on-air",
        "live-queue",
        "rotation-pool",
    ]


def test_admin_drawers_follow_producer_desk_order() -> None:
    """Occasional controls must live behind the four labeled drawers."""
    parser = _ProducerDeskParser()
    parser.feed(ADMIN_HTML.read_text(encoding="utf-8"))

    assert parser.drawer_ids == [
        "drawer-steer",
        "drawer-hosts",
        "drawer-history",
        "drawer-diagnostics",
    ]
    assert parser.drawer_section_ids == [
        "triggers",
        "pacing",
        "hosts",
        "log",
        "engine",
    ]


def test_admin_drawer_js_closes_siblings() -> None:
    """The native details accordion must keep only one drawer open at a time."""
    html = ADMIN_HTML.read_text(encoding="utf-8")

    assert "function initProducerDrawers()" in html
    assert "document.querySelectorAll('.producer-drawer').forEach(drawer=>" in html
    assert "if(other!==drawer)other.open=false;" in html


def test_live_queue_renderer_is_forward_only() -> None:
    """Scaletta must not mix now-playing or played history into the forward queue."""
    html = ADMIN_HTML.read_text(encoding="utf-8")
    block = html[html.index("function renderProgramme") : html.index("async function removeQueueItem")]

    assert "st?.upcoming" in block
    assert "stream_log" not in block
    assert "now_streaming" not in block
    assert "prossimo" in block and "più tardi" in block
