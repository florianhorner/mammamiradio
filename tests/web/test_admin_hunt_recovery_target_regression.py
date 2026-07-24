"""Regression guard for empty-rotation recovery touch targets."""

import re
from pathlib import Path

ADMIN_HTML = Path(__file__).parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"


def test_empty_pool_recovery_actions_keep_full_touch_targets() -> None:
    html = ADMIN_HTML.read_text(encoding="utf-8")
    rule = re.search(r"\.empty-pool-recovery \.btn\s*\{([^}]*)\}", html, re.DOTALL)

    assert rule is not None
    assert "min-height: 44px" in rule.group(1)
