"""Admin Producer Desk responsive layout invariants.

The /admin Scaletta is rendered as a six-column forward queue table on desktop.
On iPhone portrait widths it must collapse into compact row cards; otherwise
the table exceeds the panel and creates horizontal overflow.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_HTML = REPO_ROOT / "mammamiradio" / "web" / "templates" / "admin.html"

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_STYLE_RE = re.compile(r"<style>\s*(.*?)</style>", re.DOTALL)
_MEDIA_START_RE = re.compile(r"@media\s*\(\s*max-width\s*:\s*(\d+)px\s*\)\s*\{")
_CSS_DECL_RE = re.compile(r"([\w-]+)\s*:\s*([^;]+)")
_PX_RE = re.compile(r"(-?\d+(?:\.\d+)?)px")
_CLASS_RE = re.compile(r"\.([\w-]+)")
_ID_RE = re.compile(r"#([\w-]+)")
_TAG_RE = re.compile(r"^[a-zA-Z][\w-]*")
_PSEUDO_RE = re.compile(r":{1,2}[\w-]+(?:\([^)]*\))?")


@functools.cache
def _read_admin_html() -> str:
    return _COMMENT_RE.sub("", ADMIN_HTML.read_text(encoding="utf-8"))


def _admin_css() -> str:
    match = _STYLE_RE.search(_read_admin_html())
    assert match, "admin.html must include an inline <style> block."
    return match.group(1)


def _phone_css() -> str:
    text = _admin_css()
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


def _iter_top_level_css_rules(text: str) -> list[tuple[str, str]]:
    """Return only top-level rules; nested @media/keyframes blocks are separate contexts."""
    rules: list[tuple[str, str]] = []
    cursor = 0
    while True:
        opening_brace = text.find("{", cursor)
        if opening_brace == -1:
            return rules
        selector_block = text[cursor:opening_brace].strip()
        body = _read_balanced_block(text, opening_brace)
        closing_brace = opening_brace + len(body) + 1
        if selector_block and not selector_block.startswith("@"):
            rules.append((selector_block, body))
        cursor = closing_brace + 1


def _selector_tokens(selector: str) -> list[tuple[str, frozenset[str], frozenset[str]]]:
    tokens: list[tuple[str, frozenset[str], frozenset[str]]] = []
    for raw_token in selector.replace(">", " ").replace("+", " ").replace("~", " ").split():
        token = _PSEUDO_RE.sub("", raw_token.strip())
        if not token or token == "*":
            continue
        tag_match = _TAG_RE.match(token)
        tag = tag_match.group(0) if tag_match else ""
        classes = frozenset(_CLASS_RE.findall(token))
        ids = frozenset(_ID_RE.findall(token))
        if tag or classes or ids:
            tokens.append((tag, classes, ids))
    return tokens


def _simple_selector_matches(
    rule_token: tuple[str, frozenset[str], frozenset[str]],
    target_token: tuple[str, frozenset[str], frozenset[str]],
) -> bool:
    rule_tag, rule_classes, rule_ids = rule_token
    target_tag, target_classes, target_ids = target_token
    return (
        (not rule_tag or rule_tag == target_tag)
        and rule_classes.issubset(target_classes)
        and rule_ids.issubset(target_ids)
    )


def _selector_matches(rule_selector: str, target_selector: str) -> bool:
    if _PSEUDO_RE.search(rule_selector):
        return False
    rule_tokens = _selector_tokens(rule_selector)
    target_tokens = _selector_tokens(target_selector)
    if not rule_tokens or not target_tokens:
        return rule_selector.strip() == target_selector.strip()

    rule_idx = len(rule_tokens) - 1
    for target_token in reversed(target_tokens):
        if _simple_selector_matches(rule_tokens[rule_idx], target_token):
            rule_idx -= 1
            if rule_idx < 0:
                return True
    return False


def _selector_specificity(selector: str) -> tuple[int, int, int]:
    tokens = _selector_tokens(selector)
    ids = sum(len(token_ids) for _, _, token_ids in tokens)
    classes = sum(len(token_classes) for _, token_classes, _ in tokens)
    tags = sum(1 for token_tag, _, _ in tokens if token_tag)
    return (ids, classes, tags)


def _declarations_for_selector(text: str, selector: str) -> dict[str, str]:
    cascaded: dict[str, tuple[tuple[int, int, int], int, str]] = {}
    for rule_order, (selector_block, body) in enumerate(_iter_top_level_css_rules(text)):
        selectors = [s.strip() for s in selector_block.split(",")]
        matching_selectors = [candidate for candidate in selectors if _selector_matches(candidate, selector)]
        if not matching_selectors:
            continue

        specificity = max(_selector_specificity(candidate) for candidate in matching_selectors)
        for prop, value in _CSS_DECL_RE.findall(body):
            prop = prop.strip()
            current = cascaded.get(prop)
            if current is None or (specificity, rule_order) >= (current[0], current[1]):
                cascaded[prop] = (specificity, rule_order, value.strip())

    declarations = {prop: value for prop, (_, _, value) in cascaded.items()}
    assert declarations, f"admin.html must style {selector}."
    return declarations


def _effective_px(declarations: dict[str, str], *properties: str) -> float:
    values: list[float] = []
    for prop in properties:
        value = declarations.get(prop)
        if not value:
            continue
        match = _PX_RE.search(value)
        if match:
            values.append(float(match.group(1)))
    return max(values, default=0.0)


def _assert_touch_target(selector: str) -> None:
    declarations = _declarations_for_selector(_admin_css(), selector)
    width = _effective_px(declarations, "width", "min-width")
    height = _effective_px(declarations, "height", "min-height")
    assert width >= 44, f"{selector} must expose at least a 44px wide touch target; got {width}px."
    assert height >= 44, f"{selector} must expose at least a 44px tall touch target; got {height}px."


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


def test_admin_transport_buttons_have_44px_touch_targets() -> None:
    """On Air transport controls must remain tap-friendly on touch devices."""
    _assert_touch_target(".on-air-buttons .btn-icon")
    _assert_touch_target(".btn-primary-sm")


def test_producer_desk_drawers_have_responsive_grid_rules() -> None:
    """Drawers are 4-up on desktop, 2-up on tablet, and stacked on phone."""
    css = _admin_css()
    phone_css = _phone_css()

    assert re.search(r"\.producer-drawers\s*\{[^}]*repeat\(4,\s*minmax\(0,\s*1fr\)\)", css, re.DOTALL)
    assert "@media (max-width: 960px)" in css
    assert ".producer-drawers { grid-template-columns: repeat(2, minmax(0, 1fr)); }" in css
    assert re.search(r"\.producer-drawers\s*\{[^}]*grid-template-columns\s*:\s*1fr", phone_css, re.DOTALL)


def test_on_air_sticky_strip_survives_scrolling() -> None:
    """The compact On Air strip appears only after the full On Air zone scrolls away."""
    text = _read_admin_html()
    css = _admin_css()
    strip = text[text.index('class="a-topbar producer-sticky-strip"') : text.index("<!-- Scaletta zone -->")]

    assert 'class="a-topbar producer-sticky-strip"' in text
    assert 'id="topBarCost"' in strip
    assert "a-topbar-tools" not in strip
    assert "function initOnAirSticky()" in text
    assert "IntersectionObserver" in text
    assert "data-sticky-onair" in text
    assert re.search(r"\.producer-sticky-strip\s*\{[^}]*position\s*:\s*sticky", css, re.DOTALL)
    assert re.search(r"\.producer-sticky-strip\s*\{[^}]*display\s*:\s*none", css, re.DOTALL)
    assert re.search(
        r'body\[data-sticky-onair="true"\]\s+\.producer-sticky-strip\s*\{[^}]*display\s*:\s*flex',
        css,
        re.DOTALL,
    )


def test_on_air_zone_renders_ai_cost_counter() -> None:
    """The On Air zone must carry the AI cost counter (`sidebarCost`).

    The token-cost counter has silently disappeared in two prior admin
    refactors (see memory `feedback_token_cost_counter.md`). The sticky strip
    test guards `topBarCost`; this guards the always-visible On Air copy so a
    future zone rewrite cannot drop it without a red test.
    """
    text = _read_admin_html()
    zone = text[text.index('id="on-air"') : text.index('class="a-topbar producer-sticky-strip"')]
    assert 'id="sidebarCost"' in zone, 'On Air zone must contain the AI cost counter <b id="sidebarCost">.'
    assert "getElementById('sidebarCost')" in text, "updateEngineRoom() must write the cost into sidebarCost."


def test_programme_action_buttons_have_44px_touch_targets() -> None:
    """Scaletta action buttons may be visually compact, but the button box must be tappable."""
    _assert_touch_target(".a-programme .ac button")
    action_col = _declarations_for_selector(_admin_css(), ".a-programme .col-action")
    width = _effective_px(action_col, "width", "min-width")
    assert width >= 60, ".a-programme .col-action must leave room for a 44px action button plus cell padding."


def test_admin_checkbox_and_toggle_hit_areas_have_44px_touch_targets() -> None:
    """Switch labels and the Super Italian checkbox wrapper are the tappable targets."""
    for selector in (".chaos-control .chaos-switch", ".festival-control .festival-switch", ".super-italian-toggle"):
        _assert_touch_target(selector)


def test_admin_checkboxes_do_not_pin_small_inline_touch_targets() -> None:
    """Checkbox hit areas should come from CSS classes, not hard-coded 16px inline styles."""
    text = _read_admin_html()
    offenders: list[str] = []
    for match in re.finditer(r"<input\b[^>]*\btype=[\"']checkbox[\"'][^>]*>", text):
        tag = match.group(0)
        style_match = re.search(r"\bstyle=[\"']([^\"']*)[\"']", tag)
        if not style_match:
            continue
        declarations = {prop.strip(): value.strip() for prop, value in _CSS_DECL_RE.findall(style_match.group(1))}
        width = _effective_px(declarations, "width", "min-width")
        height = _effective_px(declarations, "height", "min-height")
        if 0 < width < 44 or 0 < height < 44:
            offenders.append(tag)
    assert not offenders, "Admin checkboxes must not use inline styles that pin touch targets below 44px."


def test_touch_target_css_helper_ignores_non_default_rule_contexts() -> None:
    """The static cascade helper should model default top-level CSS, not media/keyframe/state rules."""
    css = """
    .tap { width: 44px; height: 44px; }
    @keyframes pulse { 50% { width: 1px; height: 1px; opacity: 0.5; } }
    @media (max-width: 768px) { .tap { width: 1px; height: 1px; } }
    .tap:hover { width: 1px; height: 1px; }
    .tap:focus-visible { min-width: 1px; min-height: 1px; }
    """
    declarations = _declarations_for_selector(css, ".tap")
    assert declarations["width"] == "44px"
    assert declarations["height"] == "44px"
    assert "opacity" not in declarations


def test_programme_table_collapses_to_cards_on_phone() -> None:
    """Phone breakpoint must stop using the wide six-column Scaletta table shape."""
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
    """Scheduler pool internals must not leak into the normal admin Scaletta."""
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
    """Era/Jamendo controls should enrich rotation, not replace the live queue."""
    text = _read_admin_html()
    assert "enrichPlaylistSource(" in text
    assert "loadPlaylistSource(" not in text
    assert "'/api/playlist/enrich'" in text


def test_playlist_and_search_have_load_more_controls() -> None:
    """Admin playlist/search rendering must expose lazy-load controls backed by paginated APIs."""
    text = _read_admin_html()
    assert "loadMorePlaylist()" in text
    assert "searchMore()" in text
    assert "/api/playlist?offset=" in text
    assert "playlist_page" in text
    assert "has_more" in text


def test_playlist_pagination_keeps_accessible_absolute_index_rows() -> None:
    """Paginated rows must preserve full-playlist indices and existing accessible controls."""
    text = _read_admin_html()
    update_block = text[text.index("function updatePl") : text.index("async function loadMorePlaylist")]

    assert re.search(r"idx\s*:\s*\(\s*_plPage\.offset\s*\|\|\s*0\s*\)\s*\+\s*i", update_block)
    assert "incomingRevision===previousRevision" in update_block
    assert "loadedEnd<total" in update_block
    assert 'data-i="${idx}"' in update_block
    assert 'tabindex="0"' in update_block
    assert 'role="button"' in update_block
    assert 'aria-label="Drag to reorder"' in update_block
    assert 'aria-label="Move to next"' in update_block
    assert 'aria-label="Remove from rotation"' in update_block
    assert "moveNext(${idx})" in update_block
    assert "removeTr(${idx})" in update_block


def test_search_external_queue_posts_album_art() -> None:
    """Web result artwork must survive the admin queue-from-search boundary."""
    text = _read_admin_html()
    add_external_block = text[text.index("async function addExternal") : text.index("// ── Drag & Drop")]
    assert "album_art:t.album_art" in add_external_block


def test_load_more_buttons_reset_on_error_paths() -> None:
    """Playlist/search load-more controls must not remain stuck in loading state."""
    text = _read_admin_html()
    playlist_block = text[text.index("async function loadMorePlaylist") : text.index("function focusPlaylistTrack")]
    search_block = text[text.index("async function doSearch") : text.index("async function addTr")]

    assert "Playlist load-more failed" in playlist_block
    assert "Playlist load-more error" in playlist_block
    assert "expectedRevision=_plPage.revision" in playlist_block
    assert "revisionChanged" in playlist_block
    assert "Playlist changed while loading more; refreshing first page." in playlist_block
    assert "/api/playlist?offset=0&limit=${PLAYLIST_PAGE_SIZE}" in playlist_block
    assert playlist_block.index("revisionChanged") < playlist_block.index("_plRows=_plRows.concat")
    assert "btn.classList.remove('loading')" in playlist_block
    assert "btn.textContent='Load more tracks'" in playlist_block
    assert "include_external:String(!isAppend||_sExtPage.has_more)" in search_block
    assert "prevR=_sR.slice()" in search_block
    assert "renderSearchResults(q)" in search_block
    assert "btn.textContent='Load more results'" in search_block


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


def test_scaletta_actions_only_apply_to_rendered_queue_rows() -> None:
    """Predicted rows are read-only; only rendered queue rows call /api/queue/remove."""
    text = _read_admin_html()
    render_block = text[text.index("function renderProgramme") : text.index("async function removeQueueItem")]

    assert "const actionable=source==='rendered_queue'" in render_block
    # Removal targets the stable queue id, not the row index — the index shifts
    # whenever the streamer consumes the head segment.
    assert "removeQueueItem('${escJs(it.id||'')}',this)" in render_block
    assert "${it._queueIndex}" not in render_block.split("removeQueueItem")[1].split(")")[0]
    # Predicted rows render NO action control (design review Pass 7 — clickable
    # things must look clickable; the old disabled "·" placeholder was removed).
    assert "disabled>·</button>" not in render_block
    assert "const action=actionable" in render_block
    assert "      : '';" in render_block
    assert "'/api/queue/remove'" in text


def test_scaletta_relative_labels_use_actual_queue_position() -> None:
    """Filtered Scaletta rows must not relabel a later item as the real next item."""
    text = _read_admin_html()
    render_block = text[text.index("function renderProgramme") : text.index("async function removeQueueItem")]

    assert "relLabel(it._queueIndex)" in render_block
    assert "it._queueIndex===0?'next'" in render_block
