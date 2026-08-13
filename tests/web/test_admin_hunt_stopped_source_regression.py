"""Regression guards for stopped-state empty-pool recovery."""

import re
from pathlib import Path

ADMIN_HTML = Path(__file__).parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"


def test_available_source_actions_remain_usable_while_stopped() -> None:
    html = ADMIN_HTML.read_text(encoding="utf-8")

    for button_id in ("sourceChartsBtn", "sourceJamendoBtn"):
        button = re.search(rf'<button\b[^>]*\bid="{button_id}"[^>]*>', html)
        assert button is not None
        assert "data-stopped-exempt" in button.group(0)
