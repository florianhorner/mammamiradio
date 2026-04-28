"""Guard: admin.html must not contain a global keydown shortcut listener.

Regression for the bug where pressing s/b/a/n anywhere on the page (including
while typing in the search box) triggered doSkip / doTrigger commands.
The shortcuts were removed entirely — this test ensures they don't come back.
"""

from __future__ import annotations

from pathlib import Path

ADMIN_HTML = Path(__file__).resolve().parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"


def test_no_global_keydown_shortcut_listener() -> None:
    """admin.html must not contain a global keydown listener that fires skip/trigger."""
    html = ADMIN_HTML.read_text()
    # The shortcut block triggered doSkip/doTrigger from keydown events.
    # If either of these appears inside a keydown handler, the bug is back.
    assert "Keyboard shortcuts" not in html, (
        "Keyboard shortcuts block was re-added to admin.html. "
        "It was removed because it fired doSkip/doTrigger when typing in search."
    )
    # Belt-and-suspenders: make sure no keydown listener calls the action functions.
    # (keydown listeners for non-shortcut purposes, e.g. Enter in search, are fine.)
    import re

    keydown_blocks = re.findall(
        r"addEventListener\(['\"]keydown['\"].*?(?=addEventListener|\Z)",
        html,
        re.DOTALL,
    )
    for block in keydown_blocks:
        assert "doSkip" not in block, "doSkip called from a keydown listener"
        assert "doTrigger" not in block, "doTrigger called from a keydown listener"
