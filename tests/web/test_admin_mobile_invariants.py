"""Admin mobile layout invariants.

The /admin Palinsesto programme is rendered as a six-column table on desktop.
On iPhone portrait widths it must collapse into compact row cards; otherwise
the table exceeds the panel and creates horizontal overflow.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_HTML = REPO_ROOT / "mammamiradio" / "web" / "templates" / "admin.html"

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_MEDIA_START_RE = re.compile(r"@media\s*\(\s*max-width\s*:\s*(\d+)px\s*\)\s*\{")


def _read_admin_html() -> str:
    return _COMMENT_RE.sub("", ADMIN_HTML.read_text(encoding="utf-8"))


def _phone_css() -> str:
    text = _read_admin_html()
    phone_blocks = [
        _read_balanced_block(text, match.end() - 1)
        for match in _MEDIA_START_RE.finditer(text)
        if int(match.group(1)) <= 768
    ]
    assert phone_blocks, "admin.html has no <=768px mobile breakpoint for tablet/phone layouts."
    return "\n".join(phone_blocks)


def _read_balanced_block(text: str, opening_brace: int) -> str:
    depth = 0
    for idx in range(opening_brace, len(text)):
        char = text[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[opening_brace + 1 : idx]
    raise AssertionError("CSS media block is missing its closing brace.")


def test_programme_list_contains_horizontal_overflow() -> None:
    """The programme wrapper must keep table internals inside the panel."""
    text = _read_admin_html()
    wrapper = re.search(r"#programmeList\s*\{([^}]*)\}", text, re.DOTALL)
    assert wrapper, "admin.html must style #programmeList as the programme table wrapper."
    body = wrapper.group(1)
    assert re.search(r"max-width\s*:\s*100%", body), "#programmeList must cap at panel width."
    assert re.search(r"min-width\s*:\s*0", body), "#programmeList must allow grid/flex shrink."
    assert re.search(r"overflow-x\s*:\s*hidden", body), "#programmeList must prevent page-wide horizontal overflow."


def test_programme_table_has_fixed_desktop_columns() -> None:
    """Desktop table layout should remain table-based but constrained."""
    text = _read_admin_html()
    assert re.search(r"\.a-programme\s*\{[^}]*table-layout\s*:\s*fixed", text, re.DOTALL), (
        ".a-programme must use table-layout: fixed so desktop columns stay inside the panel."
    )
    assert "<colgroup>" in text and 'class="col-title"' in text and 'class="col-duration"' in text, (
        "renderProgramme() must emit a colgroup with stable programme column widths."
    )


def test_programme_table_collapses_to_cards_on_phone() -> None:
    """Phone breakpoint must stop using the wide six-column table shape."""
    css = _phone_css()
    assert re.search(r"\.a-programme\s+thead\s*\{[^}]*display\s*:\s*none", css, re.DOTALL), (
        "Phone CSS must hide the table header."
    )
    assert re.search(r"\.a-programme\s+tbody\s*\{[^}]*display\s*:\s*flex", css, re.DOTALL), (
        "Phone CSS must render programme rows as a vertical card stack."
    )
    assert re.search(r"\.a-programme\s+tr\s*\{[^}]*display\s*:\s*grid", css, re.DOTALL), (
        "Phone CSS must render each programme row as a compact grid card."
    )
    assert re.search(r"\.a-programme\s+td\s*\{[^}]*display\s*:\s*block", css, re.DOTALL), (
        "Phone CSS must switch cells away from table-cell layout."
    )


def test_pool_debug_annotations_hidden_from_operator_programme() -> None:
    """Scheduler pool internals must not leak into the normal admin programme."""
    text = _read_admin_html()
    assert "pool[" not in text
    assert "pool wrapped around" not in text
    assert "not selected this pass" not in text


def test_colgroup_hidden_on_phone() -> None:
    """Colgroup must be hidden on phone so it doesn't render as empty rows."""
    css = _phone_css()
    assert re.search(
        r"\.a-programme\s+colgroup[^{]*\{[^}]*display\s*:\s*none",
        css,
        re.DOTALL,
    ), "Phone CSS must hide <colgroup> (display: none) to prevent empty row artefacts."


def test_action_column_hidden_on_phone() -> None:
    """The sixth td (skip button) must be hidden on phone — no space for it in the 3-column grid."""
    css = _phone_css()
    assert re.search(
        r"\.a-programme\s+td:nth-child\(6\)\s*\{[^}]*display\s*:\s*none",
        css,
        re.DOTALL,
    ), "Phone CSS must hide td:nth-child(6) (skip action) which has no slot in the card grid."


def test_more_upcoming_row_not_card_styled_on_phone() -> None:
    """The '+ N more upcoming' summary row must render flat, not as a tappable card."""
    text = _read_admin_html()
    assert 'class="prog-more"' in text, 'renderProgramme() must emit <tr class="prog-more"> for the truncation row.'
    css = _phone_css()
    assert re.search(
        r"\.a-programme\s+tr\.prog-more\s*\{[^}]*display\s*:\s*block",
        css,
        re.DOTALL,
    ), "Phone CSS must render tr.prog-more flat (display: block, no card chrome)."


def test_playlist_source_controls_are_non_destructive_by_default() -> None:
    """Era/Jamendo controls should enrich rotation, not replace programme."""
    text = _read_admin_html()
    assert "enrichPlaylistSource(" in text
    assert "loadPlaylistSource(" not in text
    assert "'/api/playlist/enrich'" in text


def test_playlist_and_search_have_load_more_controls() -> None:
    """Admin playlist/search rendering must expose lazy-load controls backed by paginated APIs."""
    text = _read_admin_html()
    assert "loadMorePlaylist()" in text
    assert "searchMore()" in text
    assert "'/api/playlist?offset=" in text or "`/api/playlist?offset=" in text
    assert "has_more" in text


def test_empty_playlist_art_is_compact_placeholder() -> None:
    """Missing artwork should not reserve a full empty album-art square."""
    text = _read_admin_html()
    match = re.search(r"\.pl-art-empty\s*\{([^}]*)\}", text, re.DOTALL)
    assert match, "admin.html must style .pl-art-empty."
    body = match.group(1)
    assert re.search(r"width\s*:\s*18px", body), ".pl-art-empty should be a compact marker, not album-art sized."
    assert re.search(r"height\s*:\s*18px", body), ".pl-art-empty should be a compact marker, not album-art sized."


def test_programme_table_desktop_colgroup_has_all_columns() -> None:
    """renderProgramme() must emit all six column classes so fixed-layout widths apply."""
    text = _read_admin_html()
    for col in ("col-time", "col-type", "col-title", "col-host", "col-duration", "col-action"):
        assert f'class="{col}"' in text, f'renderProgramme() must emit <col class="{col}"> inside the colgroup.'
