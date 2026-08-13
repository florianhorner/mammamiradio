"""Regression guards for empty-pool Setup navigation."""

from pathlib import Path

ADMIN_HTML = Path(__file__).parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"


def test_empty_pool_setup_navigation_transfers_focus_without_forced_motion() -> None:
    html = ADMIN_HTML.read_text(encoding="utf-8")
    start = html.index("function openSetupPanel")
    end = html.index("function renderGuidedSetupStrip", start)
    block = html[start:end]

    assert 'aria-controls="setupGroup" id="emptyPoolSetupBtn"' in html
    assert "const summary=details?.querySelector('summary')" in block
    assert "summary?.focus({preventScroll:true})" in block
    assert "recordHuntReducedMotion()?'auto':'smooth'" in block
