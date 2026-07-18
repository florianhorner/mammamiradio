"""Admin status-chip migration invariants."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_HTML = REPO_ROOT / "mammamiradio" / "web" / "templates" / "admin.html"
BASE_CSS = REPO_ROOT / "mammamiradio" / "web" / "static" / "base.css"
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


def test_record_hunt_card_has_phase_copy_and_wrapping_guard() -> None:
    html = _read_admin_html()
    block = _function_block(html, "updateHeadingBanner")
    card = re.search(r"\.record-hunt\s*\{([^}]*)\}", html, re.DOTALL)
    status = re.search(r"\.record-hunt-status-copy\s*\{([^}]*)\}", html, re.DOTALL)

    assert card is not None
    assert "background: var(--surface-strong)" in card.group(1)
    assert status is not None
    assert "min-width: 0" in status.group(1)
    assert "overflow-wrap: break-word" in status.group(1)
    assert 'class="record-hunt-status-copy" id="courseBanner"' in html
    assert 'class="record-hunt-truth" id="recordHuntTruth"' in html
    assert 'class="record-hunt-stage" id="recordHuntStage" aria-hidden="true"' in html
    assert "Auto rotation is ready for a new direction." in html
    assert "Record Hunt:" in block
    assert "Hunting for" in block
    assert "Steering toward" in block
    assert "Hunt pick" in block
    assert "played through. Back on auto." in block
    assert "Course:" not in block


def test_record_hunt_pulse_is_scoped_without_overriding_global_status_pulse() -> None:
    """Record Hunt must not redefine the animation used by global status chips."""
    html = _read_admin_html()
    base_css = BASE_CSS.read_text(encoding="utf-8")

    assert ".record-hunt-status-shape.working { animation: record-hunt-pulse" in html
    assert "@keyframes record-hunt-pulse" in html
    assert "@keyframes status-pulse" not in html
    assert "@keyframes status-pulse" in base_css
    assert "0%, 100% { opacity: 0.4; }" in base_css


def test_record_hunt_reset_is_active_only_and_survives_status_renders() -> None:
    html = _read_admin_html()
    desk = _function_block(html, "renderRecordHuntDesk")

    assert 'id="clearHeadingBtn" onclick="clearHeading(this)" hidden>Back to auto</button>' in html
    assert "const reset=document.getElementById('clearHeadingBtn');" in desk
    assert "if(reset)reset.hidden=!active;" in desk
    assert "banner.innerHTML" not in desk
    assert ".record-hunt-reset[hidden] { display: none; }" in html


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
    assert "Hunting for <b>${esc(_recordHuntOptimistic.label||'that vibe')}</b>" in block
    assert "renderRecordHuntDesk(false,'Auto rotation is ready for a new direction.')" in block
    assert block.index("_recordHuntOptimistic.active") < block.index("Auto rotation is ready for a new direction.")


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


def test_runtime_status_card_renders_cached_rotation_from_rotation_status() -> None:
    """The admin card exposes the cached-music rest window, not just raw JSON."""
    block = _function_block(_read_admin_html(), "updateRuntimeStatus")

    assert "const rr=rs.rescue_rotation" in block
    assert "statusRow('Cached rotation'" in block
    assert "No cached music rescue has reached a listener this session" in block
    assert "Rotating" in block
    assert "rotationRow," in block


def test_runtime_status_card_projects_capacity_exempt_continuity() -> None:
    """The safety slot is visible to admins without joining the real queue shadow."""
    block = _function_block(_read_admin_html(), "updateRuntimeStatus")

    assert "const continuity=rs.continuity_slot" in block
    assert "statusRow('Protected continuity'" in block
    assert "No out-of-band safety audio reserved" in block
    assert "continuityRow," in block


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
    assert "const retrySeconds=Math.max(0,Number(item?.retry_in_seconds)||0)" in block
    assert "retrySeconds<60?retrySeconds+' sec':Math.ceil(retrySeconds/60)+' min'" in block
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
    assert "Temporarily unavailable; OpenAI fallback" in block
    assert "Auth failed; OpenAI fallback" not in block
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


def test_engine_room_ha_refresh_states_are_truthful_and_human() -> None:
    html = _read_admin_html()
    presentation = _function_block(html, "homeSnapshotPresentation")
    refresh_result = _function_block(html, "homeRefreshResultLine")
    engine = _function_block(html, "updateEngineRoom")

    assert "function formatHomeSnapshotAge" in html
    assert "function formatHomeSnapshotTimestamp" in html
    for state in ("'fresh'", "'stale'", "'working'", "'degraded'", "'idle'"):
        assert state in presentation
    for phrase in ("current", "catching up", "waiting for its first update"):
        assert phrase in presentation
    assert "background_timeout:'took too long to finish'" in refresh_result
    assert "stale:'arrived too late to use'" in refresh_result
    assert "continued after the audio deadline" in refresh_result
    assert "if(r.adoption_pending)" in _function_block(html, "homeSnapshotPresentation")
    assert "update ready" in _function_block(html, "homeSnapshotPresentation")
    assert presentation.index("if(r.adoption_pending)") < presentation.index("if(r.freshness==='stale')")
    assert presentation.index("if(r.freshness==='stale')") < presentation.index("if(r.in_flight)")
    assert "The old snapshot is withheld; hosts are waiting for its replacement." in presentation
    assert "Hosts are using a snapshot from '+age+'." in presentation
    assert "const refresh=hd.refresh||{}" in engine
    assert "homeSnapshotPresentation(refresh)" in engine
    assert "formatHomeSnapshotTimestamp(refresh.last_success_at)" in engine
    assert "formatHomeSnapshotAge(refresh.age_seconds)" in engine
    assert "homeRefreshResultLine(refresh)" in engine
    assert "statusInline(snapshot.state,snapshot.label)" in engine


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
        "r.status==='source_changed'",
        "Playlist changed",
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


def test_production_unavailable_uses_approved_update_delayed_copy() -> None:
    """The status fallback must use the approved 'update delayed' copy, keep the old
    producer-jargon copy gone, announce politely on entry only, and wire a retry
    control to the existing refreshFast() path that survives a paused station."""
    html = _read_admin_html()
    block = _function_block(html, "renderProductionUnavailable")

    # Exact approved strings (verbatim).
    assert "In produzione · update delayed" in block
    assert "statusInline('working','Status update delayed','Status update delayed')" in block
    assert "Can't update this panel right now. We'll keep trying automatically." in block
    assert ">Try again now</button>" in block

    # Old producer-jargon copy is gone from the whole template.
    assert "In produzione · reconnecting" not in html
    assert "The producer desk will retry automatically." not in html
    assert "Producer desk reconnecting" not in html
    assert "Reconnecting" not in block

    # A persistent polite atomic region exists before the outage; populating it on
    # entry is reliable across screen readers and avoids repeated poll announcements.
    live_region = (
        'id="productionStatusAnnouncement" class="sr-only" role="status" aria-live="polite" aria-atomic="true"'
    )
    assert live_region in html
    assert "const alreadyUnavailable=_productionUnavailable" in block
    assert "let _productionRetryInFlight=false" in html
    assert "if(!alreadyUnavailable){" in block
    assert "const announcement=document.getElementById('productionStatusAnnouncement')" in block
    announcement_copy = (
        "announcement.textContent=\"Status update delayed. Can't update this panel "
        "right now. We'll keep trying automatically.\""
    )
    assert announcement_copy in block

    # Retry control reuses the existing poll path with a visible pending/busy state.
    assert 'onclick="retryProductionNow(this)"' in block
    retry = _function_block(html, "retryProductionNow")
    assert "await refreshFast()" in retry
    assert "if(_productionRetryInFlight)return" in retry
    assert "_productionRetryInFlight=true" in retry
    assert "_productionRetryInFlight=false" in retry
    busy = _function_block(html, "_setProductionRetryBusy")
    assert "btn.disabled=busy" in busy
    assert "'Trying…'" in busy

    # A successfully rendered poll clears the latch so a later outage re-announces.
    refresh_fast = _function_block(html, "refreshFast")
    assert refresh_fast.index("renderProduction(_st);") < refresh_fast.index("_productionUnavailable=false")
    assert "_productionUnavailable=false" in refresh_fast
    assert "productionAnnouncement.textContent=''" in refresh_fast

    # The retry button is NOT a producer-action control, so it stays available while
    # the station is paused (updateStopState only inerts the producer-action set).
    update_stop = _function_block(html, "updateStopState")
    assert "prod-retry" not in update_stop
    assert "productionRetryBtn" not in update_stop


def test_unrenderable_production_status_replaces_stale_rows_with_fallback() -> None:
    """A malformed production block must never leave an older live-work row visible."""
    refresh_fast = _function_block(_read_admin_html(), "refreshFast")
    production_guard = refresh_fast[
        refresh_fast.index("try{\n      renderProduction(_st);") : refresh_fast.index("if (_st.station)")
    ]

    assert "console.error('refreshFast production',e);" in production_guard
    assert "renderProductionUnavailable();" in production_guard
    assert production_guard.index("console.error('refreshFast production',e);") < production_guard.index(
        "renderProductionUnavailable();"
    )


def test_scaletta_runway_translates_rendered_audio_into_host_progress() -> None:
    """Scaletta turns real rendered-audio seconds into the producer-facing
    promise that the hosts are ahead, rather than exposing a cold buffer metric.
    """
    html = _read_admin_html()

    assert 'id="programmeRunway"' in html
    assert 'id="programmeAhead"' in html
    block = _function_block(html, "updateProgrammeRunway")
    assert "st.buffered_audio_sec" in block
    assert "wrap.hidden=true;ahead.textContent='';detail.textContent='';" in block
    assert "The hosts are " in block
    assert "aheadLabel+' ahead'" in block
    # Sub-minute precision: a near-starvation buffer must read in seconds, not
    # get rounded up to a falsely-reassuring "1 minute ahead".
    assert "totalSec<60" in block
    assert "totalSec+' '+(totalSec===1?'second':'seconds')" in block
    assert "The next record is already cued." in block
    assert "The next '+records+' records are already cued." in block
    assert "The next set is already cued." in block
    # Filter-aware detail: an active filter pill (Music/Banter/Ads) must narrow
    # the count instead of always reporting the total queue size.
    assert "filterActive" in block
    assert "filteredCount+' of '+records+' cued.'" in block
    # aria-live="polite" must not re-announce identical text on every ~2s poll —
    # a hash guard skips the DOM write when nothing actually changed.
    assert "_lastRunwayHash" in block
    assert "updateProgrammeRunway(_st)" in _function_block(html, "refreshFast")
