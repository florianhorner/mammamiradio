"""Guard: admin.html must not contain a global keydown shortcut listener.

Regression for the bug where pressing s/b/a/n anywhere on the page (including
while typing in the search box) triggered doSkip / doTrigger commands.
The shortcuts were removed entirely — this test ensures they don't come back.
"""

from __future__ import annotations

import re
from pathlib import Path

from mammamiradio.web import assets

ADMIN_HTML = Path(__file__).resolve().parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"
ADMIN_JS = Path(__file__).resolve().parents[2] / "mammamiradio" / "web" / "static" / "admin.js"


def _admin_html() -> str:
    return ADMIN_HTML.read_text(encoding="utf-8")


def _admin_js() -> str:
    return ADMIN_JS.read_text(encoding="utf-8")


def _keydown_blocks(source: str) -> list[str]:
    return re.findall(
        r"addEventListener\(['\"]keydown['\"].*?(?=addEventListener|\Z)",
        source,
        re.DOTALL,
    )


def test_no_global_keydown_shortcut_listener() -> None:
    """admin.html must not contain a global keydown listener that fires skip/trigger."""
    html = _admin_html()
    # The shortcut block triggered doSkip/doTrigger from keydown events.
    # If either of these appears inside a keydown handler, the bug is back.
    assert "Keyboard shortcuts" not in html, (
        "Keyboard shortcuts block was re-added to admin.html. "
        "It was removed because it fired doSkip/doTrigger when typing in search."
    )
    # Belt-and-suspenders: make sure no keydown listener calls the action functions.
    # (keydown listeners for non-shortcut purposes, e.g. Enter in search, are fine.)
    keydown_blocks = _keydown_blocks(html) + _keydown_blocks(_admin_js())
    for block in keydown_blocks:
        assert "doSkip" not in block, "doSkip called from a keydown listener"
        assert "doTrigger" not in block, "doTrigger called from a keydown listener"


def test_admin_js_bridges_home_assistant_quick_bar_shortcut() -> None:
    """Cmd/Ctrl+K inside HA ingress must reach Home Assistant's parent shell."""
    js = _admin_js()
    start = js.index("function isHomeAssistantQuickBarShortcut")
    end = js.index("document.addEventListener('keydown', forwardHomeAssistantQuickBarShortcut")
    bridge = js[start:end]

    assert "forwardHomeAssistantQuickBarShortcut" in bridge
    assert "(event.metaKey || event.ctrlKey)" in bridge
    assert "event.altKey || event.shiftKey" in bridge
    assert "key === 'k' || code === 'keyk'" in bridge
    assert "window.parent" in bridge
    assert "parentWindow === window" in bridge
    assert "new ParentKeyboardEvent('keydown'" in bridge
    assert "parentWindow.dispatchEvent(forwarded)" in bridge
    assert "event.preventDefault()" in bridge
    assert "stopPropagation" not in bridge
    assert "doSkip" not in bridge
    assert "doTrigger" not in bridge


def test_static_asset_digest_includes_admin_js(tmp_path, monkeypatch) -> None:
    """Changing admin.js must change the cache-busting asset digest."""
    monkeypatch.setattr(assets, "_STATIC_DIR", tmp_path)

    admin_js = tmp_path / "admin.js"
    admin_js.write_text("one", encoding="utf-8")
    first = assets._static_asset_digest()

    admin_js.write_text("two", encoding="utf-8")
    second = assets._static_asset_digest()

    assert first != second
