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


def test_record_hunt_banner_has_phase_copy_and_wrapping_guard() -> None:
    html = _read_admin_html()
    block = _function_block(html, "updateHeadingBanner")
    style = re.search(r"\.course-banner\s*\{([^}]*)\}", html, re.DOTALL)

    assert style is not None
    assert "min-width: 0" in style.group(1)
    assert "overflow-wrap: break-word" in style.group(1)
    assert 'class="record-hunt-truth"' in html
    assert 'class="record-hunt-stage" aria-hidden="true"' in html
    assert "Record Hunt: <b>Auto rotation</b>" in html
    assert "Record Hunt:" in block
    assert "Record Hunt is searching for" in block
    assert "Record Hunt is opening the back room for" in html
    assert "is shaping the next stretch" in block
    assert "Hunt pick" in block
    assert "played through. Back on auto." in block
    assert "Course:" not in block


def test_record_hunt_matches_are_visible_in_rotation_rows() -> None:
    html = _read_admin_html()
    block = _function_block(html, "updatePl")

    assert "const activeHeadingId=_st?.heading?.active?_st.heading.id:''" in block
    assert "t.heading_id" in block
    assert "record-hunt-match" in block
    assert "Hunt pick" in block
    assert "Favored for the current Record Hunt" in block
    assert ".pl-row.record-hunt-match" in html
    assert ".pl-row.record-hunt-match .pl-a { opacity: 1; }" in html


def test_record_hunt_busywork_rotates_fake_back_room_status() -> None:
    html = _read_admin_html()

    for line in (
        "shopping for records...",
        "undusting the LPs...",
        "buying a new CD-RW writer...",
        "checking the bargain bin...",
        "reading suspicious liner notes...",
        "arguing with the jukebox...",
        "rewinding a mixtape nobody asked for...",
        "pricing imports with a tiny sticker gun...",
        "borrowing a crate from the night host...",
        "testing whether the B-side still has magic...",
    ):
        assert line in html
    assert "setInterval(()=>{" in html
    assert "},1800)" in html
    assert "prefers-reduced-motion: reduce" in html


def test_record_hunt_pending_guard_blocks_stale_auto_rotation_poll() -> None:
    block = _function_block(_read_admin_html(), "updateHeadingBanner")

    assert "}else if(_recordHuntOptimistic.active){" in block
    assert "Record Hunt is opening the back room for <b>${esc(_recordHuntOptimistic.label||'that vibe')}</b>" in block
    assert "renderRecordHuntDesk(false,'Record Hunt: <b>Auto rotation</b>')" in block
    assert block.index("_recordHuntOptimistic.active") < block.index("Record Hunt: <b>Auto rotation</b>")


def test_failed_direction_clears_pending_record_hunt_before_refresh() -> None:
    block = _function_block(_read_admin_html(), "setDirectionText")

    assert (
        "if(!r.ok){\n"
        "      clearRecordHuntOptimistic();\n"
        "      toast(r.message||wayOut('shape that set'));\n"
        "      await refreshFast();\n"
        "      return;\n"
        "    }"
    ) in block


def test_direction_timeout_clears_stale_pending_record_hunt_before_refresh() -> None:
    block = _function_block(_read_admin_html(), "setDirectionText")

    assert (
        "if(e&&e.name==='AbortError'){\n"
        "      clearRecordHuntOptimistic();\n"
        "      toast('Still hunting records - check the banner in a moment.');\n"
        "      try{await refreshFast();}catch(_){}\n"
        "    }"
    ) in block


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


def test_runtime_status_header_reads_station_on_air_not_health_state() -> None:
    block = _function_block(_read_admin_html(), "updateRuntimeStatus")

    assert "const stationOnAir=rs.station_on_air===true" in block
    assert "const taskBlocked=rs.health_state==='blocked'" in block
    assert "const headerState=taskBlocked?'blocked':stationOnAir?'ready':'degraded'" in block
    assert "const headerLabel=taskBlocked?'Error':stationOnAir?'On Air':'Paused'" in block
    assert "const headerDetail=rs.health_explanation?headerLabel+' · '+rs.health_explanation:headerLabel" in block
    assert "header.className='status-dot '+headerState" in block
    assert "header.setAttribute('aria-label',headerDetail)" in block
    assert "header.innerHTML='<span class=\"dot\"></span>'+esc(headerLabel)" in block
    assert "statusRow('Current health',headerState,headerLabel" in block
    assert "const state=rs.health_state||'ready'" not in block


def test_runtime_status_card_renders_queue_rescue_from_bridge_health() -> None:
    """#547: the Runtime Status card adds a Queue rescue row driven by
    rs.bridge_health, flipping to the colorblind-safe 'degraded' state when the
    station is running on rescue."""
    block = _function_block(_read_admin_html(), "updateRuntimeStatus")

    assert "const bh=rs.bridge_health" in block
    assert "statusRow('Queue rescue'" in block
    # Warning state must be the canonical (yellow) 'degraded', never green.
    assert "const rescueState=bh.unhealthy?'degraded':'ready'" in block
    assert "Running on rescue" in block
    # Row is wired into the rendered card array.
    assert "rescueRow," in block


def test_runtime_status_card_renders_generated_waste_from_generation_waste() -> None:
    """#397: the Runtime Status card adds a Generated waste row driven by
    rs.generation_waste, flipping to the colorblind-safe 'degraded' state when
    recent discards exceed the threshold."""
    block = _function_block(_read_admin_html(), "updateRuntimeStatus")

    assert "const gw=rs.generation_waste" in block
    assert "statusRow('Generated waste'" in block
    assert "const wasteState=gw.degraded?'degraded':'ready'" in block
    assert "wasteRow," in block


def test_runtime_provider_row_handles_recovery_mode() -> None:
    block = _function_block(_read_admin_html(), "runtimeProviderRow")

    assert "item?.recovery_mode==='circuit_breaker'||item?.recovery_mode==='action_required'" in block
    assert "state='degraded'" in block
    assert "label='Backup active'" in block
    assert "item?.recovery_mode==='transient'" in block
    assert "state='working'" in block
    assert "label='Auto-recovering'" in block
    assert "const reasonLine=item?.action_guidance||item?.switch_reason||''" in block
    assert (
        "const retryLine=item?.retry_in_seconds>0?'Retrying in '+Math.ceil(item.retry_in_seconds/60)+' min':''"
    ) in block
    assert "reasonLine" in block
    assert "retryLine" in block


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
    assert "const needsMusicSource=gp.stage==='needs_music_source'" in update_systems
    assert "const hasMusicSource=!needsMusicSource&&!!(st?.current_source||st?.playlist_source)" in update_systems
    assert "if(needsMusicSource||!hasMusicSource)" in update_systems
    assert "musicLabel='Needs source'" in update_systems
    assert "Add a source from Rotazione to build the rundown." in update_systems
    assert "else if(st?.session_stopped===true)" in update_systems
    assert "else if(st?.upcoming_mode==='building')" in update_systems
    assert update_systems.index("st?.session_stopped===true") < update_systems.index("st?.upcoming_mode==='building'")
    assert "statusRow('Scrittura AI',aiState,aiLabel,aiDetail)" in update_systems
    assert "statusRow('Fonti musica',musicState,musicLabel,musicDetail)" in update_systems


def test_listener_request_statuses_map_to_canonical_states() -> None:
    block = _function_block(_read_admin_html(), "updateListenerRequests")

    for expected in (
        "statusInline('ready',r.song_track||'ready')",  # no ▶ prefix — ::before adds ✓
        "statusInline('blocked',listenerSongErrorLabel(r.song_error_reason))",
        "statusInline('working','searching…')",
        "statusInline('working','shoutout')",  # shoutout is pending, not idle
        "listenerSongErrorBadge(r.song_error_reason)",
    ):
        assert expected in block
    html = _read_admin_html()
    assert "not a single-track song" in html
    assert "not a song" in html
    assert "Not a song" in html
    assert "Banned song" in html
    assert "download failed" in html
    assert "Download failed" in html
    assert "cancelled" in html
    assert "Cancelled" in html
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


def test_production_feed_surfaces_operator_trigger() -> None:
    """The trigger row reads the operator-attributed field (set only by
    /api/trigger), so internal forces — the 60s-silence rescue, stop/skip/resume —
    never false-light "Triggered" during an incident. Pins the full guard so a
    reorder/inversion breaks the test (no JS runtime in this repo).
    """
    block = _function_block(_read_admin_html(), "renderProduction")

    assert "const fp=st&&st.operator_force_pending;" in block
    # Must NOT key off the un-attributed force_next mirror, or rescue/internal
    # forces would falsely render as operator triggers.
    assert "st.force_pending" not in block
    # Full conditional pinned: dedup guard against double-rendering while the
    # trigger hands off to the live "building" row.
    assert "if(fp&&!(p.current&&segmentTypeKey(p.current.kind)===segmentTypeKey(fp))){" in block
    assert "Triggered — building next" in block
    # Reuses the canonical (colorblind-safe) status pill, not a bespoke color.
    assert "statusInline('working','',segmentText(segmentTypeKey(fp)))" in block


def test_buffered_ready_readout_shows_airtime_not_item_count() -> None:
    """The buffered readout surfaces SECONDS of audio (not an item count), is
    wired into the fast poll, blanks (not '0s') when empty, and rounds to whole
    seconds before splitting so it can never print an impossible '1m60s'.
    """
    html = _read_admin_html()

    assert 'id="bufferedReady"' in html
    block = _function_block(html, "updateBufferedReady")
    assert "st.buffered_audio_sec" in block
    assert "if(!(sec>0)){el.textContent='';return;}" in block  # no dead "0s" box
    # Carry-safe: round to whole seconds, THEN split — guards the 1m60s boundary.
    assert "const total=Math.round(sec),m=Math.floor(total/60),s=total%60;" in block
    assert "el.textContent='· ~'+(m>0?m+'m'+(s<10?'0':'')+s+'s':s+'s')+' ready'" in block
    assert "updateBufferedReady(_st)" in _function_block(html, "refreshFast")
