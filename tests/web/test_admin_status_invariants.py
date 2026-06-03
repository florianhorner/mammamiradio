"""Admin status-chip migration invariants."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_HTML = REPO_ROOT / "mammamiradio" / "web" / "templates" / "admin.html"
_ADMIN_HTML_TEXT = ADMIN_HTML.read_text(encoding="utf-8")
CANONICAL_STATUS_STATES = {"ready", "working", "degraded", "blocked", "idle"}


def _read_admin_html() -> str:
    return _ADMIN_HTML_TEXT


def _function_block(html: str, name: str) -> str:
    start = html.find(f"function {name}")
    assert start != -1, f"could not locate {name}() in admin.html"
    next_function = re.search(r"\n(?:async\s+)?function\s+", html[start + 1 :])
    next_function_start = start + 1 + next_function.start() if next_function is not None else len(html)
    return html[start:next_function_start]


def _class_tokens(html: str) -> set[str]:
    tokens: set[str] = set()
    for match in re.finditer(r"""class=(["'])(.*?)\1""", html, re.DOTALL):
        tokens.update(match.group(2).split())
    return tokens


def _css_selectors(html: str) -> set[str]:
    return set(re.findall(r"(?<![-\w])\.([a-zA-Z][-\w]*)(?![-\w])", html))


def _literal_status_states(html: str) -> list[str]:
    states: list[str] = []
    for helper in ("statusChip", "statusInline"):
        states.extend(re.findall(rf"{helper}\('([^']+)'", html))
    states.extend(re.findall(r"statusRow\('[^']+'\s*,\s*'([^']+)'", html))
    return states


def test_legacy_status_pill_classes_are_gone_from_admin_html() -> None:
    html = _read_admin_html()

    assert not re.search(r"(?<![-\w])\.chip(?![-\w])", html), (
        "admin.html must not keep a standalone .chip CSS selector; use .status-chip."
    )
    assert "chip" not in _class_tokens(html), (
        "admin.html must not emit class token 'chip'; btn-chip/pl-chips remain separate patterns."
    )
    for legacy in ("now-type-pill", "seg-pill", "lr-pill"):
        assert legacy not in _class_tokens(html), f"admin.html must not emit legacy {legacy} class tokens."
        assert legacy not in _css_selectors(html), f"admin.html must not keep legacy .{legacy} CSS selectors."

    assert "btn-chip" in html
    assert "pl-chips" in html
    assert "sourceChip(" in html


def test_status_helpers_emit_canonical_classes_and_aria_labels() -> None:
    html = _read_admin_html()

    assert "const STATUS_STATES=['ready','working','degraded','blocked','idle']" in html
    assert "function statusState(state)" in html
    assert "function statusChip(state,label,title='')" in html
    assert "_statusSpan('status-chip',state,label,title)" in html
    assert "function statusInline(state,label,title='')" in html
    assert "_statusSpan('status-inline',state,label,title)" in html
    assert 'aria-label="${esc(label)}: status ${esc(safeState)}"' in html
    assert 'aria-label="status: ${esc(state)}"' not in html


def test_status_helper_call_sites_use_canonical_literal_states() -> None:
    states = _literal_status_states(_read_admin_html())
    unknown = sorted(set(states) - CANONICAL_STATUS_STATES)

    assert states, "expected status helper call sites in admin.html"
    assert not unknown, f"Unknown status helper states in admin.html: {unknown}"


def test_pipeline_status_uses_canonical_status_chips() -> None:
    block = _function_block(_read_admin_html(), "updatePipelineStatus")

    assert 'class="chip ${state}"' not in block
    for expected in (
        "statusChip('working','Checking…')",
        "statusChip('degraded','Anthropic')",
        "statusChip('ready','Anthropic')",
        "statusChip('blocked','Anthropic')",
        "statusChip('ready','Stream')",
        "statusChip('idle','HA: off')",
    ):
        assert expected in block


def test_setup_keys_banner_includes_voice_provider_credentials() -> None:
    block = _function_block(_read_admin_html(), "renderSetup")

    assert "Provider keys configured" in _read_admin_html()
    assert "e.key==='llm_keys'||e.key==='tts_keys'" in block
    assert "configuredKeys=[...new Set(keyEssentials.flatMap(e=>e.configured_keys||[]))]" in block


def test_segment_labels_use_canonical_status_surfaces() -> None:
    html = _read_admin_html()

    assert "function segmentInline(type)" in html
    assert "function segmentClass(type)" in html
    assert 'class="segment-inline segment-${sKey}"' in html  # sKey = segmentClass(typeKey), not esc()-wrapped
    assert 'aria-label="segment: ${esc(sText)}"' in html
    assert ".segment-inline { color: var(--muted); }" in html
    assert ".a-now-compact .segment-inline {" in html
    assert (
        "text-transform: uppercase"
        in html[html.index(".a-now-compact .segment-inline {") : html.index(".a-now-compact .title")]
    )
    assert "statusInline('idle',segmentBadge(typeKey)" not in html
    assert "segmentInline(typeKey)" in _function_block(html, "renderProgramme")
    # Archivio rendering split out of updateLog into _renderArchivio (T6).
    assert "segmentInline(typeKey)" in _function_block(html, "_renderArchivio")


def test_now_type_status_does_not_mark_stopped_or_skipping_ready() -> None:
    html = _read_admin_html()
    block = _function_block(html, "nowTypeStatus")
    update_now = _function_block(html, "updateNow")

    assert "typeKey==='stopped'" in block
    assert "cls:'status-chip idle'" in block
    assert "typeKey==='skipping'" in block
    assert "cls:'status-chip working'" in block
    assert "segment-stopped" not in block
    assert "segment-skipping" not in block
    # playing segments use segment-inline, not status-chip
    assert "segment-inline segment-" in block
    assert "nowTypeStatus(typeKey)" in update_now
    assert "ty.className=typeStatus.cls" in update_now
    assert "ty.setAttribute('aria-label',typeStatus.ariaLabel)" in update_now
    # label must not include the segment glyph (no double-glyph with ::before)
    assert "segmentBadge(typeKey)" in block  # badge used for text content (glyph+text)
    assert "segmentBadge" not in update_now.replace("nowTypeStatus", "")


def test_engine_room_capability_lines_use_status_helpers() -> None:
    block = _function_block(_read_admin_html(), "updateEngineRoom")

    assert "anthropicLine=statusInline('degraded','suspended'+retry" in block
    assert "anthropicLine=statusInline('ready','connected')" in block
    assert "anthropicLine=statusInline('idle','not configured')" in block
    # Auth-rejected key reads as a persistent not-working state (red ✗ blocked chip),
    # for both Anthropic and OpenAI — distinct from the transient amber "suspended".
    assert "anthropicLine=statusInline('blocked','key not working'" in block
    assert "openaiLine=statusInline('blocked','key not working'" in block
    assert "openaiLine=statusInline('ready','available')" in block
    assert "OpenAI: '+openaiLine" in block
    assert "Home Assistant: '+statusInline(c.ha?'ready':'idle'" in block


def test_engine_room_ha_observability_escapes_home_assistant_values() -> None:
    block = _function_block(_read_admin_html(), "updateEngineRoom")

    assert "Array.isArray(hd.scored_entities)" in block
    assert "esc(e.label||e.entity_id||'Entity')" in block
    assert "hd.denylist_hits&&Object.keys(hd.denylist_hits).length" in block
    assert "esc(k)+': <strong>'" in block


def test_system_health_rows_use_canonical_status_helpers() -> None:
    html = _read_admin_html()
    status_row = _function_block(html, "statusRow")
    update_systems = _function_block(html, "updateSystems")

    assert 'class="status-chip ${state}"' not in status_row
    assert "statusChip(state,label)" in status_row
    assert "aiState='working'" in update_systems
    assert "aiState='blocked'" in update_systems
    assert "aiState='ready'" in update_systems
    assert "musicState='ready'" in update_systems
    assert "musicState='working'" in update_systems
    assert "musicState='blocked'" in update_systems
    assert "statusRow('Scrittura AI',aiState,aiLabel,aiDetail)" in update_systems
    assert "statusRow('Fonti musica',musicState,musicLabel,musicDetail)" in update_systems


def test_listener_request_statuses_map_to_canonical_states() -> None:
    block = _function_block(_read_admin_html(), "updateListenerRequests")

    for expected in (
        "statusInline('ready',r.song_track||'ready')",  # no ▶ prefix — ::before adds ✓
        "statusInline('blocked','not found')",
        "statusInline('working','searching…')",
        "statusInline('working','shoutout')",  # shoutout is pending, not idle
    ):
        assert expected in block
    # ensure double-glyph pattern is gone
    assert "'▶ '" not in block


def test_all_segment_types_have_css_rules() -> None:
    """Every SegmentType enum value must have a .segment-inline.segment-* CSS rule."""
    from mammamiradio.core.models import SegmentType

    html = _read_admin_html()
    missing = []
    for seg in SegmentType:
        css_class = seg.value.replace("-", "_").lower()
        rule = f".segment-inline.segment-{css_class}"
        if rule not in html:
            missing.append(rule)
    assert not missing, f"Missing CSS rules in admin.html: {missing}"
