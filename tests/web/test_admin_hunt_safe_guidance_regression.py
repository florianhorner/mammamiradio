"""Regression guard for safe empty-pool setup guidance."""

from pathlib import Path

ADMIN_HTML = Path(__file__).parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"


def test_empty_pool_keeps_plain_guidance_and_filters_configuration_syntax() -> None:
    html = ADMIN_HTML.read_text(encoding="utf-8")
    start = html.index("function emptyPoolRecoveryState")
    end = html.index("function updateSourceControls", start)
    block = html[start:end]

    assert "const rawDetail=hasSetupGuidance?String(goldenPath.detail||'').trim():''" in block
    assert "const detailLooksTechnical=" in block
    assert "MAMMAMIRADIO_" in block
    assert "hasSetupGuidance&&!detailLooksTechnical" in block
    assert "?rawDetail" in block
