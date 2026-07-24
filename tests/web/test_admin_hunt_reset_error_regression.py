"""Regression guard for Record Hunt reset transport recovery."""

from pathlib import Path

ADMIN_HTML = Path(__file__).parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"


def _clear_heading_block() -> str:
    html = ADMIN_HTML.read_text(encoding="utf-8")
    start = html.index("async function clearHeading")
    end = html.index("function renderRecordHuntPending", start)
    return html[start:end]


def test_failed_hunt_reset_restores_the_control_without_false_auto_state() -> None:
    """A rejected reset request must not escape as an unhandled page error."""

    block = _clear_heading_block()

    assert "el.disabled=true" in block
    assert "el.setAttribute('aria-busy','true')" in block
    assert "api('POST','/api/heading/clear',{},15000)" in block
    assert "}catch(_){" in block
    assert "toast(offlineMsg())" in block
    assert "}finally{" in block
    assert "el.disabled=false" in block
    assert "el.removeAttribute('aria-busy')" in block
    assert block.index("renderRecordHuntDesk(false") < block.index("toast('Back to auto.')")
    assert block.index("renderRecordHuntDesk(false") < block.index("}catch(_){")
