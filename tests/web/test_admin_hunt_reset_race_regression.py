"""Regression guard for competing Hunt and reset responses."""

from pathlib import Path

ADMIN_HTML = Path(__file__).parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"


def _function_block(name: str, next_name: str) -> str:
    html = ADMIN_HTML.read_text(encoding="utf-8")
    start = html.index(f"async function {name}")
    end = html.index(f"async function {next_name}", start)
    return html[start:end]


def test_reset_invalidates_late_hunt_feedback() -> None:
    html = ADMIN_HTML.read_text(encoding="utf-8")
    submit = _function_block("setDirectionText", "clearHeading")
    clear_start = html.index("async function clearHeading")
    clear = html[clear_start : html.index("function renderRecordHuntPending", clear_start)]

    assert "let _headingMutationGeneration=0" in html
    assert "const mutationGeneration=++_headingMutationGeneration" in submit
    assert submit.count("if(mutationGeneration!==_headingMutationGeneration)return;") == 2
    assert "_headingMutationGeneration+=1" in clear
    assert submit.index("if(mutationGeneration!==_headingMutationGeneration)return;") < submit.index("if(!r.ok)")
