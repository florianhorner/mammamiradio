"""Regression guards for canonical Record Hunt status presentation."""

from pathlib import Path

ADMIN_HTML = Path(__file__).parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"


def _function_block(name: str, next_name: str) -> str:
    html = ADMIN_HTML.read_text(encoding="utf-8")
    start = html.index(f"function {name}")
    end = html.index(f"function {next_name}", start)
    return html[start:end]


def test_hunt_phases_map_to_canonical_status_shapes() -> None:
    block = _function_block("renderRecordHuntDesk", "setDirectionText")

    assert "busy?'working':active?'ready':'idle'" in block
    assert "{ready:'✓',working:'●',idle:'○'}" in block
    assert "shape.setAttribute('aria-label',`status: ${canonicalStatus}`)" in block
    assert "for(const state of STATUS_STATES)" in block


def test_completed_hunt_is_idle_and_has_an_explicit_clear_action() -> None:
    block = _function_block("updateHeadingBanner", "trackThumb")

    assert "played through. Back on auto." in block
    assert "status:'idle'" in block
    assert "resetLabel:'Clear completed Hunt'" in block
