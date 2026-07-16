"""Regression guard for plain-language rotation recovery copy."""

from pathlib import Path

ADMIN_HTML = Path(__file__).parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"


def test_rotation_recovery_keeps_backend_configuration_detail_in_setup() -> None:
    html = ADMIN_HTML.read_text(encoding="utf-8")
    start = html.index("function emptyPoolRecoveryState")
    end = html.index("function updateSourceControls", start)
    block = html[start:end]

    assert "Boolean(String(goldenPath.detail||'').trim())" in block
    assert "No music source is configured. Open setup for configuration details." in block
    assert "No music source is configured. Open setup to add one." in block
    assert "${goldenPath.detail}" not in block
