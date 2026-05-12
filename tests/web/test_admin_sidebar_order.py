"""Regression guards for the admin sidebar order."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

ADMIN_HTML = Path(__file__).resolve().parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"


class _AdminOrderParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_admin_nav = False
        self._in_admin_content = False
        self._main_depth = 0
        self._nav_depth = 0
        self.sidebar_targets: list[str] = []
        self.content_sections: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        classes = set((attr.get("class") or "").split())

        if tag == "main" and "a-content" in classes:
            self._in_admin_content = True
            self._main_depth = 1
            return

        if tag == "nav" and "a-nav" in classes:
            self._in_admin_nav = True
            self._nav_depth = 1
            return

        if self._in_admin_content:
            if tag == "main":
                self._main_depth += 1
            if tag == "section" and "a-panel" in classes and attr.get("id"):
                self.content_sections.append(attr["id"])

        if self._in_admin_nav:
            if tag == "nav":
                self._nav_depth += 1
            if tag == "a":
                href = attr.get("href") or ""
                if href.startswith("#"):
                    self.sidebar_targets.append(href.removeprefix("#"))

    def handle_endtag(self, tag: str) -> None:
        if self._in_admin_content and tag == "main":
            self._main_depth -= 1
            if self._main_depth == 0:
                self._in_admin_content = False

        if self._in_admin_nav and tag == "nav":
            self._nav_depth -= 1
            if self._nav_depth == 0:
                self._in_admin_nav = False


def test_admin_sidebar_links_follow_visual_section_order() -> None:
    """The sidebar order must match the built page's top-level panel order."""
    parser = _AdminOrderParser()
    parser.feed(ADMIN_HTML.read_text(encoding="utf-8"))

    assert not parser._in_admin_content
    assert not parser._in_admin_nav
    assert parser.content_sections == [
        "programme",
        "triggers",
        "pacing",
        "hosts",
        "log",
        "music",
        "engine",
    ]
    assert parser.sidebar_targets == parser.content_sections
