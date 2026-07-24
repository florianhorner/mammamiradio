"""Regression guard for a late reset racing a newer Hunt."""

from pathlib import Path

ADMIN_HTML = Path(__file__).parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"


def test_late_reset_cannot_overwrite_newer_hunt_feedback() -> None:
    html = ADMIN_HTML.read_text(encoding="utf-8")
    start = html.index("async function clearHeading")
    clear = html[start : html.index("function renderRecordHuntPending", start)]

    advance = "_headingMutationGeneration+=1"
    capture = "const mutationGeneration=_headingMutationGeneration"
    fence = "if(mutationGeneration!==_headingMutationGeneration)return;"

    assert advance in clear
    assert capture in clear
    assert clear.index(advance) < clear.index(capture)
    assert clear.count(fence) == 2
    assert clear.index(fence) < clear.index("if(!r.ok)")
    assert clear.rindex(fence) < clear.index("toast(offlineMsg())")
