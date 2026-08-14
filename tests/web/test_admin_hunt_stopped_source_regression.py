"""Regression guards for stopped-state empty-pool recovery."""

import re
from pathlib import Path

ADMIN_HTML = Path(__file__).parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"


def test_available_source_actions_remain_usable_while_stopped() -> None:
    html = ADMIN_HTML.read_text(encoding="utf-8")

    # Jamendo left the quick-add row for the acknowledged media-source flow, so
    # its stopped-state exemption lives on the setup save button instead.
    for button_id in ("sourceChartsBtn", "jamendoSaveBtn"):
        button = re.search(rf'<button\b[^>]*\bid="{button_id}"[^>]*>', html)
        assert button is not None
        assert "data-stopped-exempt" in button.group(0)
