"""Tests for StationState lifecycle methods and Track properties."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import pytest

from mammamiradio.core.models import (
    HEADING_MAX_LIFT,
    HEADING_MIN_LIFT,
    HEADING_TARGET_SHARE,
    SEGMENT_PLAYLIST_SOURCE_KIND_KEY,
    ChaosSubtype,
    DialogueLine,
    GenerationWasteReason,
    Heading,
    HostPersonality,
    ListenerProfile,
    MediaAttribution,
    PlaylistSource,
    Segment,
    SegmentType,
    SourceReadinessEvidence,
    StarterCycleReservationPendingError,
    StationState,
    Track,
    normalized_track_key,
    safe_media_attribution_dict,
    segment_track_key,
)
from mammamiradio.playlist.preferences import PREFERENCE_UP_WEIGHT


def _track(n: int = 1) -> Track:
    return Track(title=f"Song {n}", artist=f"Artist {n}", duration_ms=200000, spotify_id=f"id{n}")


def test_dialogue_line_keeps_delivery_sidecar_out_of_legacy_tuple_contract():
    host = HostPersonality(name="Marco", voice="voice", style="energetic")
    line = DialogueLine(host=host, text="Ciao Windor", delivery="energetic")

    assert tuple(line) == (host, "Ciao Windor")
    assert line[0] is host
    assert line[1] == "Ciao Windor"
    assert len(line) == 2
    assert line.delivery == "energetic"
    with pytest.raises(FrozenInstanceError):
        line.delivery = "neutral"  # type: ignore[misc]


def test_source_readiness_ignores_unknown_sources_and_bad_counts():
    evidence = SourceReadinessEvidence()

    evidence.configure("unknown")
    evidence.mark_attempted(None)
    evidence.mark_candidates("custom", 4)
    evidence.mark_attempted("charts", failure="  download\nfailed  ")
    evidence.mark_candidates("charts", object())
    evidence.mark_candidates("charts", "not-a-number")
    evidence.observe_tracks(None)

    charts = evidence.entries["charts"]
    assert charts.configured is True
    assert charts.attempted is True
    assert charts.candidates == 0
    assert charts.failure == "download failed"
    assert evidence.advanced is None


def test_source_readiness_tracks_advanced_rotation_without_promoting_recovery():
    evidence = SourceReadinessEvidence()
    evidence.set_current_rotation("classic", "  Classic rotation  ")

    evidence.mark_playable("classic", "2")
    assert evidence.advanced is not None
    assert evidence.advanced.playable == 2
    assert evidence.advanced.failure == ""

    evidence.mark_failure("classic", "  temporarily\nmissing  ")
    assert evidence.advanced.failure == "temporarily missing"

    evidence.mark_playable("another-custom-source")
    evidence.mark_failure("another-custom-source", "ignored")
    evidence.mark_playable("recovery", 9)

    assert evidence.advanced.playable == 2
    assert evidence.advanced.failure == "temporarily missing"
    assert evidence.entries["recovery"].playable == 0


def test_source_readiness_distinguishes_candidate_failure_from_terminal_exhaustion():
    evidence = SourceReadinessEvidence()
    evidence.mark_candidates("charts", 2)

    evidence.mark_failure("youtube", "One candidate failed")
    assert evidence.entries["charts"].exhausted is False

    evidence.mark_playable("charts")
    evidence.mark_exhausted("charts", "No found track could be prepared")
    assert evidence.entries["charts"].exhausted is True
    assert evidence.entries["charts"].playable == 0
    assert evidence.entries["charts"].failure == "No found track could be prepared"

    evidence.clear_exhausted("charts")
    assert evidence.entries["charts"].exhausted is False

    evidence.mark_exhausted("charts", "No found track could be prepared")
    evidence.mark_playable("youtube")
    assert evidence.entries["charts"].exhausted is False
    assert evidence.entries["charts"].failure == ""


def test_source_readiness_reconciles_policy_filtered_and_removed_tracks():
    charts = Track(title="Chart", artist="Artist", duration_ms=180_000, source="youtube")
    local = Track(title="Local", artist="Artist", duration_ms=180_000, source="local")
    evidence = SourceReadinessEvidence()
    evidence.observe_tracks([charts, local])
    evidence.mark_playable("charts")
    evidence.mark_playable("local")

    evidence.reconcile_active_tracks([local], removed_tracks=[charts])

    assert evidence.entries["charts"].candidates == 0
    assert evidence.entries["charts"].playable == 0
    assert evidence.entries["charts"].exhausted is True
    assert evidence.entries["local"].candidates == 1
    assert evidence.entries["local"].playable == 1

    evidence.observe_tracks([charts])
    assert evidence.entries["charts"].candidates == 1
    assert evidence.entries["charts"].exhausted is False
    assert evidence.entries["charts"].failure == ""


def test_source_readiness_reconciles_advanced_rotation_after_policy_filters():
    survivor = Track(title="Classic A", artist="Artist", duration_ms=180_000, source="classic")
    removed = Track(title="Classic B", artist="Artist", duration_ms=180_000, source="classic")
    evidence = SourceReadinessEvidence()
    evidence.set_current_rotation("classic", "Classic rotation")
    evidence.mark_advanced_candidates(2)
    evidence.mark_playable("classic", 2)

    evidence.reconcile_active_tracks([survivor], removed_tracks=[removed])

    assert evidence.advanced is not None
    assert evidence.advanced.candidates == 1
    # Playable evidence belonged to the pre-filter pool, so the surviving track
    # must prove itself instead of inheriting the removed track's preparation.
    assert evidence.advanced.playable == 0
    assert evidence.advanced.exhausted is False
    assert evidence.advanced.failure == ""

    evidence.mark_playable("classic")
    evidence.reconcile_active_tracks([], removed_tracks=[survivor])

    assert evidence.advanced.candidates == 0
    assert evidence.advanced.playable == 0
    assert evidence.advanced.exhausted is True
    assert evidence.advanced.failure == ("No found track remains in the active rotation after local policy filters.")


def test_station_state_reconciles_loader_evidence_after_policy_filters_and_switch():
    chart_evidence = SourceReadinessEvidence()
    chart_evidence.set_current_rotation("charts", "Live charts")
    chart_evidence.mark_candidates("charts", 1)
    charts = PlaylistSource(
        kind="charts",
        label="Live charts",
        track_count=1,
        readiness_evidence=chart_evidence,
    )

    # Models the startup doorway after the only loader candidate was removed by
    # the operator blocklist before StationState adopted the active rotation.
    state = StationState(playlist=[], playlist_source=charts)

    assert charts.track_count == 0
    assert state.source_readiness.entries["charts"].candidates == 0
    assert state.source_readiness.entries["charts"].exhausted is True

    jamendo_track = Track(title="CC song", artist="Artist", duration_ms=180_000, source="jamendo")
    jamendo_evidence = SourceReadinessEvidence()
    jamendo_evidence.set_current_rotation("jamendo", "Jamendo")
    jamendo_evidence.mark_candidates("jamendo", 1)
    jamendo = PlaylistSource(
        kind="jamendo",
        label="Jamendo",
        track_count=1,
        readiness_evidence=jamendo_evidence,
    )

    state.switch_playlist([jamendo_track], jamendo)

    assert state.source_readiness.entries["charts"].exhausted is False
    assert state.source_readiness.entries["jamendo"].candidates == 1
    assert state.playlist_source is jamendo


def test_source_readiness_advanced_exhaustion_reopens_on_candidates():
    evidence = SourceReadinessEvidence()
    evidence.set_current_rotation("classic", "Classic rotation")

    evidence.mark_exhausted("classic", "  No selectable\ntrack remains  ")
    assert evidence.advanced is not None
    assert evidence.advanced.attempted is True
    assert evidence.advanced.exhausted is True
    assert evidence.advanced.failure == "No selectable track remains"

    evidence.clear_exhausted("classic")
    assert evidence.advanced.exhausted is False

    evidence.mark_exhausted("classic", "Still empty")
    evidence.mark_advanced_candidates("not-a-count")
    assert evidence.advanced.candidates == 0
    assert evidence.advanced.exhausted is True
    assert evidence.advanced.failure == "Still empty"

    evidence.mark_advanced_candidates("3")
    assert evidence.advanced.candidates == 3
    assert evidence.advanced.exhausted is False
    assert evidence.advanced.failure == ""


def test_source_readiness_exhaustion_ignores_recovery_and_absent_advanced_rotation():
    evidence = SourceReadinessEvidence()

    evidence.mark_exhausted("recovery", "Continuity is not a music source")
    evidence.mark_exhausted("unknown", "ignored")
    evidence.clear_exhausted("unknown")
    evidence.mark_advanced_candidates(4)

    assert evidence.entries["recovery"].exhausted is False
    assert evidence.entries["recovery"].failure == ""
    assert evidence.advanced is None


def test_after_music_updates_counters():
    state = StationState()
    t = _track()
    state.after_music(t)

    assert state.segments_produced == 1
    assert state.songs_since_banter == 1
    assert state.songs_since_ad == 1
    assert state.current_track == t
    assert t in state.played_tracks
    assert len(state.segment_log) == 1
    assert state.segment_log[0].type == "music"


def test_after_banter_resets_counter():
    state = StationState(songs_since_banter=3, segments_produced=3)
    state.after_banter()

    assert state.songs_since_banter == 0
    assert state.segments_produced == 4


def test_after_ad_resets_counter_and_tracks_history():
    state = StationState(songs_since_ad=4, segments_produced=5)
    state.record_ad_spot("TestBrand", "A test ad")
    state.after_ad(brands=["TestBrand"])

    assert state.songs_since_ad == 0
    assert state.segments_produced == 6
    assert len(state.ad_history) == 1
    assert state.ad_history[0].brand == "TestBrand"


def test_ad_history_capped_at_20():
    state = StationState()
    for i in range(25):
        state.record_ad_spot(brand=f"Brand{i}", summary=f"Ad {i}")
        state.after_ad(brands=[f"Brand{i}"])
    assert len(state.ad_history) == 20
    assert state.ad_history[0].brand == "Brand5"


def test_ad_experiment_snapshot_is_runtime_only_sorted_and_fresh_by_default():
    state = StationState()
    assert state.ad_experiment_snapshot() == {
        "scope": "runtime",
        "completed_breaks": 0,
        "completed_spots": 0,
        "brands": [],
    }

    state.record_completed_ad_break([" Bravo ", "Alfa"])
    state.record_completed_ad_break(["alfa", "Bravo", "Bravo"])
    state.record_completed_ad_break(["", "   "])

    assert state.ad_experiment_snapshot() == {
        "scope": "runtime",
        "completed_breaks": 2,
        "completed_spots": 5,
        "brands": [
            {"brand": "Bravo", "completed_airings": 3},
            {"brand": "Alfa", "completed_airings": 1},
            {"brand": "alfa", "completed_airings": 1},
        ],
    }


def test_segment_log_capped_at_50():
    state = StationState()
    for i in range(60):
        state.after_music(_track(i))
    assert len(state.segment_log) == 50


def test_render_timings_are_bounded_newest_first_and_sanitized():
    state = StationState()
    for index in range(22):
        state.record_render_timing(
            kind="banter",
            outcome="produced" if index == 21 else "discarded",
            total_elapsed_ms=12.7 + index,
            stages_ms={"script": 5.6, "unknown": 99.0},
            reason="stale_playlist",
            timestamp=float(index),
        )

    assert len(state.render_timings) == 20
    assert state.render_timings[0]["timestamp"] == 21.0
    assert state.render_timings[0]["stages_ms"] == {"script": 6}
    assert "reason" not in state.render_timings[0]
    assert "reason" in state.render_timings[-1]


def test_stream_delivery_diagnostics_coalesce_and_keep_only_anonymous_bounded_values():
    """The private egress diagnostics aggregate timing without retaining identity."""
    state = StationState()
    state.listeners_active = 3
    state.gen_phase = "mastering"
    state.gen_kind = "ad"
    state.ha_context_refresh_in_flight = True
    state.ha_context_refresh_active_foreground_timed_out = True
    state.set_ha_context_refresh_stage("projection", started=10.0)

    state.record_stream_pacing_event(
        "not-a-real-kind",
        lateness_ms=1,
        remaining_lead_ms=1,
        segment_type="music",
        timestamp=99.0,
        monotonic_now=10.0,
    )
    state.record_stream_pacing_event(
        "late",
        lateness_ms=-2,
        remaining_lead_ms=-3,
        deficit_ms=-4,
        segment_type="",
        timestamp=100.0,
        monotonic_now=10.1,
    )
    state.record_stream_pacing_event(
        "late",
        lateness_ms=25,
        remaining_lead_ms=200,
        deficit_ms=0,
        segment_type="",
        timestamp=100.5,
        # The coarse HA snapshot is deliberately part of the coalescing key.
        # Keep it identical here so this proves the aggregation path itself.
        monotonic_now=10.1,
    )
    # A later event must not be folded into the earlier one merely because it
    # has the same context; the rolling counters retain both sends.
    state.record_stream_pacing_event(
        "late",
        lateness_ms=30,
        remaining_lead_ms=150,
        segment_type="music",
        timestamp=102.0,
        monotonic_now=10.3,
    )
    state.record_stream_pacing_event(
        "underrun",
        lateness_ms=600,
        remaining_lead_ms=0,
        deficit_ms=100,
        segment_type="music",
        timestamp=102.5,
        monotonic_now=10.4,
    )
    state.record_stream_outcome(
        segment_type="",
        result="",
        bytes_sent=-1,
        starting_listener_count=-2,
        terminal_reason="not-a-real-reason",
        timestamp=102.5,
    )
    state.record_slow_listener_drops(0, timestamp=100.0)
    state.record_slow_listener_drops(2, timestamp=100.0)
    state.record_slow_listener_drops(3, timestamp=100.5)

    snapshot = state.stream_delivery_snapshot(now=102.5, monotonic_now=11.0)

    # Deliberate literal pin: retuning the lead must fail here first.
    assert snapshot["target_lead_ms"] == 4_000
    assert snapshot["session"] == {"late": 3, "underrun": 1, "overrun_rebased": 0, "total": 4}
    assert snapshot["window_15m"] == snapshot["session"]
    assert [event["count"] for event in snapshot["recent"]] == [2, 1, 1]
    assert snapshot["recent"][0]["segment_type"] == "unknown"
    assert snapshot["recent"][0]["lateness_ms"] == 25
    assert snapshot["recent"][0]["remaining_lead_ms"] == 0
    assert snapshot["recent"][0]["generator"] == {"phase": "mastering", "kind": "ad"}
    assert snapshot["recent"][0]["ha_refresh"] == {
        "in_flight": True,
        "foreground_timed_out": True,
        "stage": "projection",
        "stage_elapsed_ms": 100,
    }
    assert snapshot["ha_refresh"]["stage_elapsed_ms"] == 1000
    assert snapshot["recent_stream_outcomes"] == [
        {
            "timestamp": 102.5,
            "segment_type": "unknown",
            "result": "not_streamed",
            "bytes_sent": 0,
            "starting_listener_count": 0,
            "accepted_listener_count": 0,
            "terminal_reason": "file_error",
        }
    ]
    assert snapshot["slow_listener_drops"] == {"session": 5, "window_15m": 5, "last_drop_at": 100.5}


def test_new_render_attempt_records_an_abandoned_previous_attempt():
    state = StationState()
    state.begin_render_timing("banter", started=10.0)
    state.add_render_stage_timing("script", 15.0)
    state.begin_render_timing("music", started=20.0)

    assert state.render_timings[0]["kind"] == "banter"
    assert state.render_timings[0]["outcome"] == "failed"
    assert state.render_timings[0]["reason"] == "abandoned"
    assert state.render_timings[0]["stages_ms"] == {"script": 15}
    # The abandoned attempt is closed at the new attempt's start (20.0), not the
    # wall clock, so its elapsed time reflects real work: 20.0 - 10.0 = 10s.
    assert state.render_timings[0]["total_elapsed_ms"] == 10000


def test_render_timing_emits_structured_safe_log(caplog):
    state = StationState()

    with caplog.at_level("INFO", logger="mammamiradio.render_timing"):
        state.record_render_timing(
            kind="banter",
            outcome="discarded",
            total_elapsed_ms=262_000,
            stages_ms={"tts": 121_000, "mix": 96_000},
            reason=GenerationWasteReason.STALE_PLAYLIST,
        )

    message = caplog.messages[-1]
    assert "render_timing kind=banter outcome=discarded" in message
    assert "total_elapsed_ms=262000" in message
    assert "reason=stale_playlist" in message


def test_generation_display_can_delegate_stage_timing_to_parallel_workers():
    state = StationState()
    state.begin_render_timing("ad")

    state.set_gen("writing", "ad", "Writing an ad break", track_timing=False)
    state.add_render_stage_timing("script", 12.0)
    state.end_gen()
    state.finish_render_timing("produced")

    assert state.gen_recent[0]["phase"] == "writing"
    assert state.render_timings[0]["stages_ms"] == {"script": 12}


def test_malformed_render_diagnostics_never_escape_into_audio_path():
    state = StationState()

    # Inactive and invalid observations are no-ops, including malformed values.
    state.add_render_stage_timing("tts", object())  # type: ignore[arg-type]
    state.finish_render_timing("produced")
    state.record_render_timing(kind="banter", outcome="unknown", total_elapsed_ms=1.0)

    state.begin_render_timing("banter", started=10.0)
    state.add_render_stage_timing("tts", object())  # type: ignore[arg-type]
    state.record_render_timing(kind="banter", outcome="produced", total_elapsed_ms=object())  # type: ignore[arg-type]

    assert list(state.render_timings) == []


def test_add_joke_capped_at_5():
    state = StationState()
    for i in range(8):
        state.add_joke(f"joke {i}")
    assert len(state.running_jokes) == 5
    assert state.running_jokes[0] == "joke 3"


def test_on_stream_segment_updates_now_streaming():
    state = StationState()
    seg = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/fake.mp3"),
        duration_sec=5.0,
        metadata={"title": "Test Song"},
    )
    state.on_stream_segment(seg)

    assert state.now_streaming["type"] == "music"
    assert state.now_streaming["label"] == "Test Song"
    assert state.now_streaming["duration_sec"] == 5.0
    assert len(state.stream_log) == 1
    assert state.stream_log[0].duration_sec == 5.0


def test_selected_segment_does_not_commit_listener_audible_bookkeeping():
    state = StationState(playlist_source=PlaylistSource(kind="charts", label="Charts"))
    seg = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/readable.mp3"),
        duration_sec=180.0,
        metadata={
            "title": "Artist – Readable",
            "title_only": "Readable",
            "artist": "Artist",
            "duration_ms": 180_000,
            "audio_source": "download",
        },
    )

    selected_epoch = state.on_stream_segment_selected(seg)

    assert selected_epoch == state.playback_epoch == 1
    assert state.now_streaming["label"] == "Artist – Readable"
    assert state.current_stream_audible is False
    assert state.audible_playback_epoch == 0
    assert state.runtime_provider_state == {}
    assert list(state.played_track_log) == []


def test_audible_segment_commit_is_exactly_once():
    state = StationState(playlist_source=PlaylistSource(kind="charts", label="Charts"))
    seg = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/audible.mp3"),
        duration_sec=180.0,
        metadata={
            "title": "Artist – Audible",
            "title_only": "Audible",
            "artist": "Artist",
            "duration_ms": 180_000,
            "audio_source": "download",
        },
    )
    state.on_stream_segment_selected(seg)

    assert state.on_stream_segment_audible(seg) is True
    assert state.on_stream_segment_audible(seg) is False
    assert state.current_stream_audible is True
    assert state.audible_playback_epoch == state.playback_epoch == 1
    assert len(state.played_track_log) == 1
    assert state.runtime_provider_state["audio_source"]["current_provider"] == "charts"


def test_audible_music_keeps_render_bound_source_after_source_swap():
    state = StationState(playlist_source=PlaylistSource(kind="charts", label="Charts"))
    seg = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/audible.mp3"),
        duration_sec=180.0,
        metadata={
            "title": "Artist – Audible",
            "title_only": "Audible",
            "artist": "Artist",
            "duration_ms": 180_000,
            "audio_source": "download",
            SEGMENT_PLAYLIST_SOURCE_KIND_KEY: "charts",
        },
    )
    state.on_stream_segment_selected(seg)
    state.playlist_source = PlaylistSource(kind="local", label="Local files")

    assert state.on_stream_segment_audible(seg) is True
    provider = state.runtime_provider_state["audio_source"]
    assert provider["current_provider"] == "charts"
    assert provider["primary_provider"] == "charts"


def test_unheard_selection_never_becomes_a_listener_outcome():
    state = StationState(playlist_source=PlaylistSource(kind="charts", label="Charts"))
    audible_music = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/audible.mp3"),
        duration_sec=180.0,
        metadata={
            "title": "Artist – Audible",
            "title_only": "Audible",
            "artist": "Artist",
            "duration_ms": 180_000,
            "audio_source": "download",
        },
    )
    unheard_music = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/unheard.mp3"),
        duration_sec=180.0,
        metadata={
            "title": "Artist – Unheard",
            "title_only": "Unheard",
            "artist": "Artist",
            "duration_ms": 180_000,
            "audio_source": "download",
        },
    )
    later_banter = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/later.mp3"),
        duration_sec=10.0,
        metadata={"title": "Later banter"},
    )

    state.on_stream_segment_selected(audible_music)
    assert state.on_stream_segment_audible(audible_music) is True
    state.on_stream_segment_selected(unheard_music)
    state.on_stream_segment_selected(later_banter)
    assert state.on_stream_segment_audible(later_banter) is True

    assert state.listener.songs_played == 1
    assert [outcome["track"] for outcome in state.listener.recent_outcomes] == ["Artist – Audible"]
    assert all(outcome["track"] != "Artist – Unheard" for outcome in state.listener.recent_outcomes)


def test_provider_observation_reason_does_not_rewrite_switch_history():
    state = StationState()

    first = state.update_runtime_provider(
        "audio_source",
        current_provider="norm_cache",
        primary_provider="charts",
        fallback_active=True,
        reason="Chart download failed",
        timestamp=10.0,
    )
    same_route = state.update_runtime_provider(
        "audio_source",
        current_provider="norm_cache",
        primary_provider="charts",
        fallback_active=True,
        reason="Serving the reserved cache runway",
        timestamp=20.0,
    )

    saved = state.runtime_provider_state["audio_source"]
    assert first is not None
    assert same_route is None
    assert saved["current_reason"] == "Serving the reserved cache runway"
    assert saved["last_switch_reason"] == "Chart download failed"
    assert saved["last_switch_timestamp"] == 10.0
    assert len(state.runtime_events) == 1


def test_provider_observation_updates_current_without_switch_history():
    state = StationState()

    observation = state.observe_runtime_provider(
        "script_provider",
        current_provider="openai",
        primary_provider="anthropic",
        fallback_active=True,
        reason="anthropic_exception",
        timestamp=10.0,
    )

    saved = state.runtime_provider_state["script_provider"]
    assert observation.current_provider == "openai"
    assert saved["current_provider"] == "openai"
    assert saved["current_reason"] == "anthropic_exception"
    assert saved["last_switch_timestamp"] is None
    assert saved["last_switch_reason"] is None
    assert list(state.runtime_events) == []


def test_provider_observation_scope_returns_and_collects_render_ownership():
    state = StationState()
    scope = state.bind_runtime_provider_observation_scope("render-123")
    try:
        observation = state.observe_runtime_provider(
            "script_provider",
            current_provider="openai",
            primary_provider="anthropic",
            fallback_active=True,
            reason="anthropic_exception",
        )
    finally:
        state.reset_runtime_provider_observation_scope(scope)

    assert observation.observation_token == "render-123"
    snapshot = state.snapshot_runtime_provider_observations("render-123")
    assert snapshot == {"script_provider": observation}
    snapshot.clear()
    assert state.snapshot_runtime_provider_observations("render-123") == {
        "script_provider": observation,
    }
    assert state.take_runtime_provider_observations("render-123") == {
        "script_provider": observation,
    }
    assert state.snapshot_runtime_provider_observations("render-123") == {}
    assert state.take_runtime_provider_observations("render-123") == {}


def test_provider_observation_preserves_legacy_audible_baseline():
    state = StationState()
    state.runtime_provider_state["audio_source"] = {
        "current_provider": "norm_cache",
        "primary_provider": "charts",
        "fallback_active": True,
        "reason": "Chart download failed",
        "last_switch_timestamp": 5.0,
    }

    event = state.update_runtime_provider(
        "audio_source",
        current_provider="norm_cache",
        primary_provider="charts",
        fallback_active=True,
        reason="Serving the reserved cache runway",
        timestamp=10.0,
    )

    saved = state.runtime_provider_state["audio_source"]
    assert event is None
    assert saved["last_audible_provider"] == "norm_cache"
    assert saved["last_audible_primary_provider"] == "charts"
    assert saved["last_audible_fallback_active"] is True
    assert saved["last_audible_reason"] == "Serving the reserved cache runway"
    assert saved["last_switch_timestamp"] == 5.0
    assert saved["last_switch_reason"] == "Chart download failed"


@pytest.mark.parametrize("provider_class", ["script_provider", "tts_provider"])
def test_audible_provider_commit_is_once_and_preserves_newer_observation(provider_class):
    state = StationState()
    audible_observation = state.observe_runtime_provider(
        provider_class,
        current_provider="fallback",
        primary_provider="primary",
        fallback_active=True,
        reason="primary unavailable",
        timestamp=10.0,
    )
    segment = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/provider-observation.mp3"),
        metadata={"title": "Provider observation"},
        runtime_provider_observations={provider_class: audible_observation},
    )
    state.on_stream_segment_selected(segment)
    state.observe_runtime_provider(
        provider_class,
        current_provider="primary",
        primary_provider="primary",
        fallback_active=False,
        reason="primary recovered",
        timestamp=20.0,
    )

    assert state.on_stream_segment_audible(segment) is True
    assert state.on_stream_segment_audible(segment) is False

    saved = state.runtime_provider_state[provider_class]
    assert saved["current_provider"] == "primary"
    assert saved["current_reason"] == "primary recovered"
    assert saved["last_audible_provider"] == "fallback"
    assert saved["last_audible_primary_provider"] == "primary"
    assert saved["last_audible_reason"] == "primary unavailable"
    assert saved["last_switch_reason"] == "primary unavailable"
    assert len(state.runtime_events) == 1
    assert state.runtime_events[0].provider_class == provider_class


def test_chaos_subtypes_are_not_segment_types():
    assert {item.value for item in ChaosSubtype} == {
        "chaos_fourth_wall",
        "chaos_abandoned_storm",
        "chaos_impossible_recall",
        "chaos_icon_moment",
        "urgent_interrupt",
    }
    assert not ({item.value for item in ChaosSubtype} & {item.value for item in SegmentType})


def test_on_stream_segment_records_played_track_log_at_play_time():
    state = StationState()
    queued_track = _track(1)
    state.after_music(queued_track)
    assert list(state.played_track_log) == []

    seg = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/fake.mp3"),
        duration_sec=180.0,
        metadata={
            "title": queued_track.display,
            "title_only": queued_track.title,
            "artist": queued_track.artist,
            "spotify_id": queued_track.spotify_id,
            "duration_ms": queued_track.duration_ms,
        },
    )
    state.on_stream_segment(seg)

    assert len(state.played_track_log) == 1
    assert state.played_track_log[0].track.display == queued_track.display
    assert state.played_track_log[0].played_at > 0


def test_on_stream_segment_skips_degraded_music_in_played_track_log():
    state = StationState()
    seg = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/silence.mp3"),
        duration_sec=180.0,
        metadata={"error": "ffmpeg died with SIGABRT"},
    )

    state.on_stream_segment(seg)

    assert list(state.played_track_log) == []


@pytest.mark.parametrize(
    "metadata",
    [
        {
            "title": "Station continuity",
            "artist": "",
            "audio_source": "emergency_tone",
            "error_recovery": True,
            "rescue": True,
        },
        {
            "title": "Rescue Song",
            "artist": "Test Artist",
            "audio_source": "norm_cache",
            "queue_drain_recovery": True,
            "rescue": True,
        },
        {
            "title": "Rescue Song",
            "artist": "Test Artist",
            "audio_source": "fallback_norm_cache",
            "fallback": True,
        },
    ],
)
def test_on_stream_segment_skips_rescue_music_in_played_track_log(metadata):
    state = StationState()
    seg = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/recovery_tone.mp3"),
        duration_sec=2.0,
        metadata=metadata,
    )

    state.on_stream_segment(seg)

    assert list(state.played_track_log) == []


def test_on_stream_segment_skips_placeholder_music_in_played_track_log():
    state = StationState()
    seg = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/fake.mp3"),
        duration_sec=180.0,
        metadata={"title": "music", "duration_ms": 180_000},
    )

    state.on_stream_segment(seg)

    assert list(state.played_track_log) == []


def test_switch_playlist_clears_played_track_log():
    state = StationState()
    state.on_stream_segment(
        Segment(
            type=SegmentType.MUSIC,
            path=Path("/tmp/fake.mp3"),
            metadata={
                "title": "Artist – Old Song",
                "artist": "Artist",
                "title_only": "Old Song",
                "duration_ms": 180_000,
            },
        )
    )
    assert state.played_track_log

    state.switch_playlist([_track(2)])

    assert list(state.played_track_log) == []


def test_on_stream_segment_records_previous_music_as_completed():
    state = StationState()
    previous = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/previous.mp3"),
        metadata={"title": "Prev Song", "duration_ms": 180_000},
    )
    seg = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/fake2.mp3"),
        metadata={"title": "Banter"},
    )

    with patch("mammamiradio.core.models.time.time", return_value=100.0):
        state.on_stream_segment(previous)
    with patch("mammamiradio.core.models.time.time", return_value=130.0):
        state.on_stream_segment(seg)

    assert state.listener.songs_played == 1
    assert state.listener.segments_since_taste_mirror == 1


def test_track_cache_key():
    t = Track(title="Con te partirò!", artist="Andrea Bocelli", duration_ms=250000, spotify_id="x")
    key = t.cache_key
    assert key == "andrea_bocelli_con_te_partir_youtube"
    assert len(key) <= 80


def test_track_display():
    t = _track()
    assert t.display == "Artist 1 – Song 1"


def test_media_attribution_object_serializes_through_the_public_validator() -> None:
    attribution = MediaAttribution(
        provider="incompetech",
        license_id="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        source_url="https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1400037",
        credit="Carefree by Kevin MacLeod",
        modified=True,
        basis="bundled_manifest",
    )

    assert safe_media_attribution_dict(attribution) == attribution.to_dict()


@pytest.mark.parametrize(
    "override",
    [
        {"basis": "unknown"},
        {"provider": None},
        {"license_url": None},
        {"license_url": "https://creativecommons.org:bad/licenses/by/4.0/"},
        {"source_url": "https://incompetech.com/%2e%2e/private"},
        {"source_url": "https://incompetech.com/music/royalty-free/index.html?token=secret"},
        # A bundled Jamendo row must still point at a jamendo.com track page:
        # the provider/basis pair is now legitimate, the mismatched host is not.
        {"provider": "jamendo", "basis": "bundled_manifest"},
        {
            "provider": "jamendo",
            "basis": "bundled_manifest",
            "source_url": "https://evil.example.com/track/1",
        },
    ],
)
def test_media_attribution_rejects_malformed_or_mismatched_public_facts(override) -> None:
    raw = {
        "provider": "incompetech",
        "license_id": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "source_url": "https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1400037",
        "credit": "Carefree by Kevin MacLeod",
        "modified": True,
        "basis": "bundled_manifest",
        **override,
    }

    assert safe_media_attribution_dict(raw) is None


def test_track_cache_keys_cover_transient_identity_and_url_normalization() -> None:
    identified = Track(
        title="Transient",
        artist="Artist",
        duration_ms=180_000,
        source="jamendo",
        provider_track_id="track-123",
    )
    url_only = Track(
        title="Transient",
        artist="Artist",
        duration_ms=180_000,
        source="jamendo",
        direct_url="HTTPS://storage.jamendo.com:443/path/",
    )
    root_url = Track(
        title="Transient",
        artist="Artist",
        duration_ms=180_000,
        source="jamendo",
        direct_url="https://storage.jamendo.com/",
    )

    assert identified.cache_key == "jamendo_track_123"
    assert url_only.cache_key == "jamendo_https_storage_jamendo_com_443_path"
    assert root_url.cache_key == "jamendo_https_storage_jamendo_com"


def test_segment_release_callback_runs_exactly_once():
    events: list[str] = []

    def admit() -> bool:
        events.append("started")
        return True

    segment = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/single-use.mp3"),
        playback_start_callback=admit,
        release_callback=lambda: events.append("released"),
    )

    assert segment.mark_playback_started() is True
    assert segment.mark_playback_started() is True
    segment.release()
    segment.release()
    assert segment.mark_playback_started() is False

    assert events == ["started", "released"]


def test_segment_denied_playback_releases_provider_resource():
    events: list[str] = []

    def deny() -> bool:
        events.append("denied")
        return False

    segment = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/denied-single-use.mp3"),
        playback_start_callback=deny,
        release_callback=lambda: events.append("released"),
    )

    assert segment.mark_playback_started() is False
    assert segment.mark_playback_started() is False
    segment.release()

    assert events == ["denied", "released"]


def test_segment_provider_callback_exceptions_fail_closed_without_escaping() -> None:
    def reject_with_exception() -> bool:
        raise RuntimeError("provider admission failed")

    def release_with_exception() -> None:
        raise OSError("provider cleanup failed")

    segment = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/provider-callback-failure.mp3"),
        playback_start_callback=reject_with_exception,
        release_callback=release_with_exception,
    )

    assert segment.mark_playback_started() is False
    assert segment.mark_playback_started() is False
    segment.release()


def _starter_track(index: int) -> Track:
    return Track(
        title=f"Starter {index:02d}",
        artist="Catalog Artist",
        duration_ms=240_000,
        source="starter",
        local_path=Path(f"/catalog/starter-{index:02d}.mp3"),
    )


def test_starter_reservations_fill_lookahead_without_counting_as_air() -> None:
    tracks = [_starter_track(index) for index in range(12)]
    state = StationState(playlist=tracks, playlist_source=PlaylistSource(kind="starter"))

    reserved: list[Track] = []
    for index in range(12):
        track = state.select_next_track()
        assert state.reserve_music_admission(f"queue-{index}", track) is True
        reserved.append(track)

    assert len({track.cache_key for track in reserved}) == 12
    assert list(state.played_tracks) == []
    assert state.current_track is None
    assert state.jamendo_base_music_since_last == 0
    with pytest.raises(StarterCycleReservationPendingError):
        state.select_next_track()

    for index in range(11):
        assert state.commit_music_admission(f"queue-{index}") is True
    with pytest.raises(StarterCycleReservationPendingError):
        state.select_next_track()

    assert state.commit_music_admission("queue-11") is True
    assert state.select_next_track().cache_key == tracks[0].cache_key


def test_starter_reservation_rollback_restores_identity_without_advancing_cycle() -> None:
    tracks = [_starter_track(0), _starter_track(1)]
    state = StationState(playlist=tracks, playlist_source=PlaylistSource(kind="starter"))
    selected = state.select_next_track()

    assert state.reserve_music_admission("removed-before-playback", selected) is True
    assert state.rollback_music_admission("removed-before-playback") is True
    assert state.rollback_music_admission("removed-before-playback") is False

    assert state.select_next_track() is selected
    assert state.jamendo_base_music_since_last == 0
    assert list(state.played_tracks) == []


def test_jamendo_cadence_counts_playback_starts_not_queue_reservations() -> None:
    starter = _starter_track(0)
    local = Track(
        title="Operator Local",
        artist="Operator",
        duration_ms=180_000,
        source="local",
        local_path=Path("/music/local.mp3"),
    )
    jamendo = Track(
        title="Transient",
        artist="Provider Artist",
        duration_ms=180_000,
        source="jamendo",
        provider_track_id="jamendo-1",
    )
    state = StationState(playlist=[starter, local])

    assert state.reserve_music_admission("starter", starter) is True
    assert state.reserve_music_admission("local", local) is True
    assert state.jamendo_insert_eligible() is False

    assert state.commit_music_admission("starter") is True
    assert state.jamendo_insert_eligible() is False
    assert state.commit_music_admission("local") is True
    assert state.jamendo_insert_eligible() is True

    assert state.reserve_music_admission("jamendo-removed", jamendo) is True
    assert state.jamendo_insert_eligible() is False
    assert state.rollback_music_admission("jamendo-removed") is True
    assert state.jamendo_insert_eligible() is True

    assert state.reserve_music_admission("jamendo-started", jamendo) is True
    assert state.commit_music_admission("jamendo-started") is True
    assert state.jamendo_base_music_since_last == 0
    assert state.jamendo_insert_eligible() is False


def test_jamendo_insert_eligible_when_rotation_crate_is_empty() -> None:
    jamendo = Track(
        title="Transient",
        artist="Provider Artist",
        duration_ms=180_000,
        source="jamendo",
        provider_track_id="jamendo-1",
    )
    state = StationState(playlist=[])

    assert state.jamendo_insert_eligible() is True
    assert state.reserve_music_admission("jamendo-only", jamendo) is True
    assert state.jamendo_insert_eligible() is False
    assert state.rollback_music_admission("jamendo-only") is True
    assert state.jamendo_insert_eligible() is True


def test_restore_playlist_if_still_empty_preserves_operator_intent() -> None:
    """Unlike switch_playlist, this recovery must not wipe operator state."""
    pinned = Track(title="Pinned Song", artist="Pinned Artist", duration_ms=180_000, source="local")
    heading = Heading(id="h1", seed="italo disco", label="Italo Disco", set_at=1.0, set_by="operator")
    state = StationState(
        playlist=[],
        heading=heading,
        pinned_track=pinned,
        force_next=SegmentType.AD,
        operator_force_pending=SegmentType.NEWS_FLASH,
        songs_since_banter=3,
    )
    state.pending_actions.append({"type": "note", "detail": "operator note"})
    previously_played = Track(title="Some Title", artist="Some Artist", duration_ms=180_000, source="local")
    state.played_tracks.append(previously_played)
    starting_heading_revision = state.heading_revision

    recovered = Track(title="Recovered", artist="Operator", duration_ms=180_000, source="local")
    source = PlaylistSource(kind="local", source_id="local_music_dir", label="Local music/ files", track_count=1)

    assert state.restore_playlist_if_still_empty([recovered], source) is True
    assert state.playlist == [recovered]
    assert state.playlist_source is source
    assert state.heading is heading
    assert state.heading_revision == starting_heading_revision
    assert state.pinned_track is pinned
    assert state.force_next == SegmentType.AD
    assert state.operator_force_pending == SegmentType.NEWS_FLASH
    assert state.songs_since_banter == 3
    assert len(state.pending_actions) == 1
    assert previously_played in state.played_tracks


def test_restore_playlist_if_still_empty_backs_off_when_no_longer_empty() -> None:
    """A concurrent writer (e.g. an admin source switch) must win — this
    recovery must never clobber a playlist that filled in the meantime."""
    admin_track = Track(title="Admin Pick", artist="Admin", duration_ms=180_000, source="youtube")
    admin_source = PlaylistSource(kind="charts", source_id="apple_music_it_top_100", label="Charts", track_count=1)
    state = StationState(playlist=[admin_track], playlist_source=admin_source)

    recovered = Track(title="Recovered", artist="Operator", duration_ms=180_000, source="local")
    local_source = PlaylistSource(kind="local", source_id="local_music_dir", label="Local music/ files", track_count=1)

    assert state.restore_playlist_if_still_empty([recovered], local_source) is False
    assert state.playlist == [admin_track]
    assert state.playlist_source is admin_source


def test_music_admission_rejects_invalid_duplicate_and_missing_reservations() -> None:
    local = Track(
        title="Operator Local",
        artist="Operator",
        duration_ms=180_000,
        source="local",
        local_path=Path("/music/local.mp3"),
    )
    other_local = Track(
        title="Other Local",
        artist="Operator",
        duration_ms=180_000,
        source="local",
        local_path=Path("/music/other.mp3"),
    )
    jamendo_one = Track(
        title="Transient One",
        artist="Provider Artist",
        duration_ms=180_000,
        source="jamendo",
        provider_track_id="jamendo-1",
    )
    jamendo_two = Track(
        title="Transient Two",
        artist="Provider Artist",
        duration_ms=180_000,
        source="jamendo",
        provider_track_id="jamendo-2",
    )
    ordinary = _track(99)
    state = StationState(playlist=[local, other_local])

    assert state.reserve_music_admission("   ", local) is False
    assert state.reserve_music_admission("local", local) is True
    assert state.reserve_music_admission("local", local) is True
    assert state.reserve_music_admission("local", other_local) is False
    assert state.commit_music_admission("missing") is False

    assert state.reserve_music_admission("jamendo-one", jamendo_one) is True
    assert state.reserve_music_admission("jamendo-two", jamendo_two) is False
    assert state.rollback_music_admission("jamendo-one") is True

    state.jamendo_base_music_since_last = 2
    assert state.reserve_music_admission("ordinary", ordinary) is True
    assert state.commit_music_admission("ordinary") is True
    assert state.jamendo_base_music_since_last == 2


@pytest.mark.asyncio
async def test_music_admission_wait_returns_on_capacity_signal_and_timeout() -> None:
    starter = _starter_track(0)
    state = StationState(playlist=[starter], playlist_source=PlaylistSource(kind="starter"))

    await state.wait_for_music_admission_change(timeout=0)
    assert state.reserve_music_admission("starter", starter) is True

    asyncio.get_running_loop().call_soon(state.music_admission_changed.set)
    await state.wait_for_music_admission_change(timeout=0.1)
    await state.wait_for_music_admission_change(timeout=0)


def test_reserved_pinned_starter_falls_through_to_local_track() -> None:
    starter = _starter_track(0)
    local = Track(
        title="Operator Local",
        artist="Operator",
        duration_ms=180_000,
        source="local",
        local_path=Path("/music/local.mp3"),
    )
    state = StationState(playlist=[starter, local], playlist_source=PlaylistSource(kind="starter"))
    state.pinned_track = starter
    assert state.reserve_music_admission("starter", starter) is True

    assert state.select_next_track() is local
    assert state.pinned_track is starter


def test_exhausted_starter_cycle_without_available_reservation_fails_closed() -> None:
    starter = _starter_track(0)
    state = StationState(playlist=[starter], playlist_source=PlaylistSource(kind="starter"))
    state.starter_cycle_catalog = {starter.cache_key}
    state.starter_cycle_remaining.clear()
    state.starter_cycle_reserved = {starter.cache_key}

    with pytest.raises(RuntimeError, match="current starter cycle"):
        state.select_next_track()


def test_switch_playlist_clears_listener_request_state():
    state = StationState(playlist=[_track(1)])
    state.pending_requests.append({"request_id": "req-1", "name": "Luca", "message": "ciao", "type": "shoutout"})
    state.pending_actions.append({"type": "skip_bridge"})
    state._listener_request_rl = {"127.0.0.1": 123.0}
    state.pinned_track = _track(99)
    state.force_next = SegmentType.BANTER

    state.switch_playlist([_track(2)])

    assert state.pending_requests == []
    assert len(state.recently_consumed_requests) == 1
    consumed = state.recently_consumed_requests[0]
    assert consumed["id"] == "req-1"
    assert consumed["name"] == "Luca"
    assert consumed["message"] == "ciao"
    assert consumed["type"] == "shoutout"
    assert consumed["status"] == "source_changed"
    assert consumed["song_error_reason"] == ""
    assert list(state.pending_actions) == []
    assert state._listener_request_rl == {}
    assert state.pinned_track is None
    assert state.force_next is None


def test_pending_actions_are_bounded():
    state = StationState()

    for i in range(250):
        state.pending_actions.append({"n": i})

    assert len(state.pending_actions) == 200
    assert state.pending_actions[0] == {"n": 50}
    assert state.pending_actions[-1] == {"n": 249}


def test_select_next_track_consumes_pinned_track():
    state = StationState(playlist=[_track(1), _track(2)])
    pinned = _track(99)
    state.pinned_track = pinned
    state.song_preferences[normalized_track_key(pinned)] = {"score": -1}

    picked = state.select_next_track()

    assert picked is pinned
    assert state.pinned_track is None


def test_select_next_track_excluded_keys_raise_when_pool_empty():
    track = _track(1)
    state = StationState(playlist=[track])

    with pytest.raises(RuntimeError, match="Playlist has no eligible tracks"):
        state.select_next_track(excluded_cache_keys={track.cache_key})


def test_select_next_track_excluded_pinned_track_raises_when_no_eligible_tracks():
    track = _track(1)
    state = StationState(playlist=[track], pinned_track=track)

    with pytest.raises(RuntimeError, match="Playlist has no eligible tracks"):
        state.select_next_track(excluded_cache_keys={track.cache_key})

    assert state.pinned_track is None


def test_select_next_track_skips_excluded_pinned_track_for_eligible_pool():
    rejected_pin = _track(1)
    eligible = _track(2)
    state = StationState(playlist=[eligible], pinned_track=rejected_pin)

    def _choose(candidates, **kwargs):
        return [candidates[0]]

    with patch("mammamiradio.core.models.random.choices", side_effect=_choose):
        picked = state.select_next_track(excluded_cache_keys={rejected_pin.cache_key})

    assert picked is eligible
    assert state.pinned_track is None


def test_select_next_track_most_stale_fallback():
    stale = _track(1)
    recent = _track(2)
    state = StationState(playlist=[stale, recent])
    # Ensure repeat cooldown excludes the whole pool, forcing fallback.
    state.played_tracks.extend([stale, recent, recent])

    picked = state.select_next_track()

    assert picked == stale


def test_select_next_track_applies_operator_preference_weights():
    liked = _track(1)
    neutral = _track(2)
    disliked = _track(3)
    state = StationState(
        playlist=[liked, neutral, disliked],
        song_preferences={
            normalized_track_key(liked): {"score": 1},
            normalized_track_key(disliked): {"score": -1},
        },
    )
    captured = {}

    def _choose(candidates, **kwargs):
        captured["candidates"] = candidates
        captured["weights"] = kwargs["weights"]
        return [neutral]

    with patch("mammamiradio.core.models.random.choices", side_effect=_choose):
        picked = state.select_next_track(repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0)

    assert picked is neutral
    liked_idx = captured["candidates"].index(liked)
    neutral_idx = captured["candidates"].index(neutral)
    disliked_idx = captured["candidates"].index(disliked)
    assert captured["weights"][liked_idx] > captured["weights"][neutral_idx]
    assert 0 < captured["weights"][disliked_idx] < captured["weights"][neutral_idx]


def test_select_next_track_cooldowns_still_override_liked_songs():
    liked_recent = _track(1)
    other = _track(2)
    state = StationState(
        playlist=[liked_recent, other],
        song_preferences={normalized_track_key(liked_recent): {"score": 1}},
    )
    state.played_tracks.append(liked_recent)
    captured = {}

    def _choose(candidates, **kwargs):
        captured["candidates"] = candidates
        return [other]

    with patch("mammamiradio.core.models.random.choices", side_effect=_choose):
        picked = state.select_next_track(repeat_cooldown=8, artist_cooldown=0, max_artist_per_hour=0)

    assert picked is other
    assert liked_recent not in captured["candidates"]


def test_select_next_track_prefers_active_heading_candidates():
    normal = _track(1)
    tagged = _track(2)
    heading = Heading(
        id="heading-1",
        seed="direction://2000s",
        label="2000s female vocals",
        set_at=1.0,
        set_by="operator",
        selection_budget=2,
    )
    tagged.heading_id = heading.id
    state = StationState(playlist=[normal, tagged], heading=heading)

    captured = {}

    def _choose(candidates, **kwargs):
        captured["candidates"] = candidates
        captured["weights"] = kwargs["weights"]
        return [tagged]

    with patch("mammamiradio.core.models.random.choices", side_effect=_choose):
        picked = state.select_next_track(repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0)

    assert picked is tagged
    normal_idx = captured["candidates"].index(normal)
    tagged_idx = captured["candidates"].index(tagged)
    assert captured["weights"][tagged_idx] > captured["weights"][normal_idx]


def test_select_next_track_heading_bias_still_beats_liked_non_heading_track():
    liked_normal = _track(1)
    tagged = _track(2)
    heading = Heading(
        id="heading-1",
        seed="direction://2000s",
        label="2000s female vocals",
        set_at=1.0,
        set_by="operator",
        selection_budget=2,
    )
    tagged.heading_id = heading.id
    state = StationState(
        playlist=[liked_normal, tagged],
        heading=heading,
        song_preferences={normalized_track_key(liked_normal): {"score": 1}},
    )
    captured = {}

    def _choose(candidates, **kwargs):
        captured["candidates"] = candidates
        captured["weights"] = kwargs["weights"]
        return [tagged]

    with patch("mammamiradio.core.models.random.choices", side_effect=_choose):
        picked = state.select_next_track(repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0)

    assert picked is tagged
    normal_idx = captured["candidates"].index(liked_normal)
    tagged_idx = captured["candidates"].index(tagged)
    assert captured["weights"][tagged_idx] > captured["weights"][normal_idx]


def test_select_next_track_stacks_like_with_heading_bias():
    liked_tagged = _track(1)
    neutral_tagged = _track(2)
    heading = Heading(
        id="heading-1",
        seed="direction://2000s",
        label="2000s female vocals",
        set_at=1.0,
        set_by="operator",
        selection_budget=2,
    )
    liked_tagged.heading_id = heading.id
    neutral_tagged.heading_id = heading.id
    state = StationState(
        playlist=[liked_tagged, neutral_tagged],
        heading=heading,
        song_preferences={normalized_track_key(liked_tagged): {"score": 1}},
    )
    captured = {}

    def _choose(candidates, **kwargs):
        captured["candidates"] = candidates
        captured["weights"] = kwargs["weights"]
        return [liked_tagged]

    with patch("mammamiradio.core.models.random.choices", side_effect=_choose):
        picked = state.select_next_track(repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0)

    assert picked is liked_tagged
    liked_idx = captured["candidates"].index(liked_tagged)
    neutral_idx = captured["candidates"].index(neutral_tagged)
    assert captured["weights"][liked_idx] == pytest.approx(captured["weights"][neutral_idx] * PREFERENCE_UP_WEIGHT)


def test_select_next_track_heading_bias_still_beats_disliked_heading_track():
    neutral = _track(1)
    disliked_tagged = _track(2)
    heading = Heading(
        id="heading-1",
        seed="direction://2000s",
        label="2000s female vocals",
        set_at=1.0,
        set_by="operator",
        selection_budget=2,
    )
    disliked_tagged.heading_id = heading.id
    state = StationState(
        playlist=[neutral, disliked_tagged],
        heading=heading,
        song_preferences={normalized_track_key(disliked_tagged): {"score": -1}},
    )
    captured = {}

    def _choose(candidates, **kwargs):
        captured["candidates"] = candidates
        captured["weights"] = kwargs["weights"]
        return [disliked_tagged]

    with patch("mammamiradio.core.models.random.choices", side_effect=_choose):
        picked = state.select_next_track(repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0)

    assert picked is disliked_tagged
    neutral_idx = captured["candidates"].index(neutral)
    tagged_idx = captured["candidates"].index(disliked_tagged)
    assert captured["weights"][tagged_idx] > captured["weights"][neutral_idx]


def test_select_next_track_heading_bias_persists_after_budget_spent():
    normal = _track(1)
    tagged = _track(2)
    heading = Heading(
        id="heading-1",
        seed="direction://2000s",
        label="2000s female vocals",
        set_at=1.0,
        set_by="operator",
        selection_budget=1,
        selection_spent=1,
    )
    tagged.heading_id = heading.id
    state = StationState(playlist=[normal, tagged], heading=heading)
    captured = {}

    def _choose(candidates, **kwargs):
        captured["candidates"] = candidates
        captured["weights"] = kwargs["weights"]
        return [tagged]

    with patch("mammamiradio.core.models.random.choices", side_effect=_choose):
        picked = state.select_next_track(repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0)

    assert picked is tagged
    normal_idx = captured["candidates"].index(normal)
    tagged_idx = captured["candidates"].index(tagged)
    assert captured["weights"][tagged_idx] > captured["weights"][normal_idx]


def _capture_selection_weights(state: StationState, **kwargs) -> tuple[list[Track], list[float]]:
    """Run select_next_track once and return its (candidates, weights) without randomness."""
    captured: dict = {}

    def _choose(candidates, **choose_kwargs):
        captured["candidates"] = candidates
        captured["weights"] = choose_kwargs["weights"]
        return [candidates[0]]

    with patch("mammamiradio.core.models.random.choices", side_effect=_choose):
        state.select_next_track(**kwargs)
    return captured["candidates"], captured["weights"]


def _heading(heading_id: str = "heading-1") -> Heading:
    return Heading(
        id=heading_id,
        seed="direction://2000s",
        label="2000s female vocals",
        set_at=1.0,
        set_by="operator",
    )


def test_select_next_track_heading_lift_is_adaptive_in_large_pool():
    # ~8 hunt tracks against a ~150-track pool: the fixed x4 lift only bought ~18% share;
    # the adaptive lift rebalances so the hunt set lands near HEADING_TARGET_SHARE.
    heading = _heading()
    hunt = [_track(n) for n in range(8)]
    for track in hunt:
        track.heading_id = heading.id
    rest = [_track(1000 + n) for n in range(150)]
    state = StationState(playlist=hunt + rest, heading=heading)

    candidates, weights = _capture_selection_weights(state, repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0)

    hunt_keys = {t.cache_key for t in hunt}
    total = sum(weights)
    hunt_mass = sum(w for c, w in zip(candidates, weights, strict=False) if c.cache_key in hunt_keys)
    share = hunt_mass / total

    assert share == pytest.approx(HEADING_TARGET_SHARE, abs=0.05)
    # Materially above the old fixed-x4 behaviour (~0.18 for this pool shape).
    assert share > 0.35


def test_select_next_track_heading_lift_floors_at_min_in_small_pool():
    # 1 hunt + 1 normal: the computed adaptive multiplier is <1, so it clamps up to the
    # historical x4 floor — small pools behave exactly as before.
    heading = _heading()
    tagged = _track(2)
    tagged.heading_id = heading.id
    normal = _track(1)
    state = StationState(playlist=[normal, tagged], heading=heading)

    candidates, weights = _capture_selection_weights(state, repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0)

    tagged_w = weights[candidates.index(tagged)]
    normal_w = weights[candidates.index(normal)]
    assert tagged_w == pytest.approx(normal_w * HEADING_MIN_LIFT)


def test_select_next_track_heading_lift_caps_in_lopsided_pool():
    # A single hunt track against a huge non-heading pool would need a >MAX multiplier to
    # reach the target share — the cap stops one song from dominating the station.
    heading = _heading()
    tagged = _track(2)
    tagged.heading_id = heading.id
    rest = [_track(1000 + n) for n in range(400)]
    state = StationState(playlist=[tagged, *rest], heading=heading)

    candidates, weights = _capture_selection_weights(state, repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0)

    tagged_w = weights[candidates.index(tagged)]
    normal_w = weights[candidates.index(rest[0])]
    assert tagged_w == pytest.approx(normal_w * HEADING_MAX_LIFT)


def test_select_next_track_all_hunt_pool_does_not_zero_weights():
    # Every candidate matches the heading (sum_other_base == 0): the lift must not divide
    # by zero or collapse weights — a hunt track still airs.
    heading = _heading()
    hunt = [_track(n) for n in range(4)]
    for track in hunt:
        track.heading_id = heading.id
    state = StationState(playlist=hunt, heading=heading)

    _candidates, weights = _capture_selection_weights(
        state, repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0
    )

    assert all(w > 0 for w in weights)
    picked = state.select_next_track(repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0)
    assert picked in hunt


def test_select_next_track_heading_all_cooldown_filtered_picks_non_heading():
    # Heading active but every tagged track sits in the repeat-cooldown window: selection
    # must fall through to a non-heading track without raising.
    heading = _heading()
    hunt = [_track(0), _track(2)]
    for track in hunt:
        track.heading_id = heading.id
    normals = [_track(10), _track(11)]
    state = StationState(playlist=hunt + normals, heading=heading, played_tracks=list(hunt))

    picked = state.select_next_track()
    assert picked in normals


def test_select_next_track_no_heading_leaves_weights_unlifted():
    # Regression: with no active heading, never-played neutral tracks keep the plain
    # never-played base weight (1.2) — no lift leaks in. A stale heading_id tag on a
    # track must not lift it when state.heading is None (the `heading is not None`
    # short-circuit guards this).
    stale = _track(1)
    stale.heading_id = "stale-heading"
    state = StationState(playlist=[stale, _track(2)], heading=None)

    _candidates, weights = _capture_selection_weights(
        state, repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0
    )

    assert all(w == pytest.approx(1.2) for w in weights)


def test_select_next_track_empty_heading_id_does_not_lift():
    # A Heading with an empty id must force heading_match=False for every track
    # (the `and heading.id` sub-clause), so a tagged track gets no lift.
    heading = _heading("")
    tagged = _track(2)
    tagged.heading_id = heading.id  # "" — matches nothing
    normal = _track(1)
    state = StationState(playlist=[normal, tagged], heading=heading)

    candidates, weights = _capture_selection_weights(state, repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0)

    assert weights[candidates.index(tagged)] == pytest.approx(weights[candidates.index(normal)])


def test_course_track_waits_for_the_rest_of_the_set():
    # Three of four course tracks have aired, so only the fourth is still eligible.
    heading = _heading()
    course = [_track(n) for n in range(4)]
    for track in course:
        track.heading_id = heading.id
    normals = [_track(100 + n) for n in range(4)]
    state = StationState(playlist=course + normals, heading=heading)
    state.played_tracks = [course[0], course[1], course[2]]

    candidates, _weights = _capture_selection_weights(
        state, repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0
    )

    assert [c for c in candidates if c.heading_id == heading.id] == [course[3]]
    # Non-course tracks are untouched by the course cooldown.
    assert all(n in candidates for n in normals)


def test_course_track_blocked_past_the_plain_repeat_cooldown():
    """The shape seen on air: a course track returned about ten picks later.

    The plain cooldown only looks at the last five plays, and a course takes a
    large share of picks from a small found set, so the gap between two airings of
    the same course track lands outside that window. Cooling against the rest of
    the set is what closes it.
    """
    heading = _heading()
    course = [_track(n) for n in range(4)]
    for track in course:
        track.heading_id = heading.id
    normals = [_track(100 + n) for n in range(8)]
    state = StationState(playlist=course + normals, heading=heading)
    # course[0] aired nine picks ago, well clear of a five-play cooldown, but only
    # one other course track has aired since, so the set has not cycled.
    state.played_tracks = [course[0], *normals[:4], course[1], *normals[4:]]

    candidates, _weights = _capture_selection_weights(
        state, repeat_cooldown=5, artist_cooldown=0, max_artist_per_hour=0
    )

    eligible_course = [c for c in candidates if c.heading_id == heading.id]
    assert course[0] not in eligible_course
    assert course[2] in eligible_course
    assert course[3] in eligible_course


def test_course_cooldown_ignores_a_course_track_no_longer_in_the_pool():
    # A banned or dropped course track cannot be cycled back to, so it must not
    # consume a cooldown slot and let a current track return before the set has
    # actually worked through.
    heading = _heading()
    course = [_track(n) for n in range(3)]
    for track in course:
        track.heading_id = heading.id
    dropped = _track(50)
    dropped.heading_id = heading.id  # tagged, but never in the playlist
    normals = [_track(100 + n) for n in range(3)]
    state = StationState(playlist=course + normals, heading=heading)
    # The stale track sits between two current course plays.
    state.played_tracks = [course[0], dropped, course[1]]

    candidates, _weights = _capture_selection_weights(
        state, repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0
    )

    # course_keys is 3, so the last 2 *current* course plays are excluded. Without
    # filtering history to the pool the stale track fills a slot and course[0] returns.
    assert [c for c in candidates if c.heading_id == heading.id] == [course[2]]


def test_course_cooldown_sizes_itself_to_selectable_tracks_only():
    # Under allow_explicit=False an explicit course track is not selectable. Counting
    # it inflates the set to 3, so the cooldown excludes the last 2 course plays —
    # every track that could actually air — and steering silently stops.
    heading = _heading()
    clean = [_track(n) for n in range(2)]
    explicit = _track(9)
    explicit.explicit = True
    for track in [*clean, explicit]:
        track.heading_id = heading.id
    normals = [_track(100 + n) for n in range(2)]
    state = StationState(playlist=[*clean, explicit, *normals], heading=heading)
    state.played_tracks = [clean[0], clean[1]]

    candidates, _weights = _capture_selection_weights(
        state, allow_explicit=False, repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0
    )

    # Sized to the 2 selectable tracks, only the most recent is cooled, so the course
    # still has a runner. Sized to 3, both are excluded and only normals remain.
    assert [c for c in candidates if c.heading_id == heading.id] == [clean[0]]


def test_single_track_course_is_not_cooled_against_itself():
    # One found track cannot cycle, so the course cooldown must not apply and the
    # plain repeat cooldown stays the only guard.
    heading = _heading()
    solo = _track(2)
    solo.heading_id = heading.id
    normal = _track(1)
    state = StationState(playlist=[solo, normal], heading=heading, played_tracks=[solo])

    candidates, _weights = _capture_selection_weights(
        state, repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0
    )

    assert solo in candidates


def test_course_cooldown_always_leaves_one_course_track():
    # It excludes at most len(set) - 1, so the least recently aired one always
    # survives and a course can never starve its own selection.
    heading = _heading()
    course = [_track(n) for n in range(5)]
    for track in course:
        track.heading_id = heading.id
    state = StationState(playlist=list(course), heading=heading, played_tracks=list(course))

    candidates, _weights = _capture_selection_weights(
        state, repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0
    )

    assert [c for c in candidates if c.heading_id == heading.id] == [course[0]]
    assert state.select_next_track(repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0) is course[0]


def test_after_music_spends_heading_budget_only_for_matching_track():
    heading = Heading(
        id="heading-1",
        seed="direction://2000s",
        label="2000s female vocals",
        set_at=1.0,
        set_by="operator",
        selection_budget=2,
    )
    normal = _track(1)
    tagged = _track(2)
    tagged.heading_id = heading.id
    state = StationState(heading=heading)

    state.after_music(normal)
    assert heading.selection_spent == 0

    state.after_music(tagged)
    assert heading.selection_spent == 1


def test_after_music_persists_heading_budget_spend():
    heading = Heading(
        id="heading-1",
        seed="direction://2000s",
        label="2000s female vocals",
        set_at=1.0,
        set_by="operator",
        selection_budget=2,
    )
    tagged = _track(2)
    tagged.heading_id = heading.id
    persisted: list[Heading] = []
    state = StationState(heading=heading, heading_persist_callback=persisted.append)

    state.after_music(tagged)

    assert heading.selection_spent == 1
    assert persisted == [heading]


def test_after_music_heading_persist_callback_failure_is_non_fatal():
    heading = Heading(
        id="heading-1",
        seed="direction://2000s",
        label="2000s female vocals",
        set_at=1.0,
        set_by="operator",
        selection_budget=2,
    )
    tagged = _track(2)
    tagged.heading_id = heading.id

    def fail_persist(_heading: Heading) -> None:
        raise OSError("disk full")

    state = StationState(heading=heading, heading_persist_callback=fail_persist)

    state.after_music(tagged)

    assert state.current_track is tagged
    assert heading.selection_spent == 1


def test_after_music_counts_heading_tracks_beyond_legacy_budget():
    """selection_spent is telemetry now; it must not retire the heading bias."""
    heading = Heading(
        id="heading-1",
        seed="direction://2000s",
        label="2000s female vocals",
        set_at=1.0,
        set_by="operator",
        selection_budget=1,
    )
    tagged = _track(2)
    tagged.heading_id = heading.id
    state = StationState(heading=heading)

    state.after_music(tagged)
    state.after_music(tagged)

    assert heading.selection_spent == 2


def test_on_stream_segment_counts_canned_clips():
    """Canned banter clips are counted at stream time for shareware trial."""
    state = StationState()

    # Non-canned segment should not increment
    seg1 = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/tts.mp3"),
        metadata={"type": "banter", "canned": False},
    )
    state.on_stream_segment(seg1)
    assert state.canned_clips_streamed == 0

    # Canned segment should increment
    seg2 = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/canned.mp3"),
        metadata={"type": "banter", "canned": True},
    )
    state.on_stream_segment(seg2)
    assert state.canned_clips_streamed == 1

    # Another canned
    state.on_stream_segment(seg2)
    assert state.canned_clips_streamed == 2

    # Packaged continuity speech is a rescue rung, not a shareware banter use.
    rescue = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/continuity.mp3"),
        metadata={"type": "banter", "canned": True, "rescue": True},
    )
    state.on_stream_segment(rescue)
    assert state.canned_clips_streamed == 2


def test_on_stream_segment_adds_generated_banter_to_bleed_pool():
    state = StationState()
    seg = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/generated-banter.mp3"),
        metadata={"type": "banter", "canned": False},
    )

    state.on_stream_segment(seg)

    assert list(state.recent_banter_paths) == [Path("/tmp/generated-banter.mp3")]


def test_on_stream_segment_does_not_add_canned_banter_to_bleed_pool():
    state = StationState()
    seg = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/canned-banter.mp3"),
        metadata={"type": "banter", "canned": True},
    )

    state.on_stream_segment(seg)

    assert list(state.recent_banter_paths) == []


def test_on_stream_segment_label_falls_back_to_seg_type_when_no_title():
    # Regression: ISSUE-004 — failed normalization produces metadata={"error": "..."}
    # with no "title" key. label must fall back to seg_type.value, not crash or return None.
    # Found by /qa on 2026-04-14
    # Report: .gstack/qa-reports/qa-report-localhost-8200-2026-04-14.md
    state = StationState()
    seg = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/silence.mp3"),
        metadata={"error": "ffmpeg died with SIGABRT"},
    )
    state.on_stream_segment(seg)

    assert state.now_streaming["type"] == "music"
    assert state.now_streaming["label"] == "music"  # raw fallback — UI masks it as "Preparing..."


def test_on_stream_segment_uses_brand_for_ad_when_no_title():
    # Regression: ad segments use "brand" field as label when no "title" present.
    # Found by /qa on 2026-04-14
    state = StationState()
    seg = Segment(
        type=SegmentType.AD,
        path=Path("/tmp/ad.mp3"),
        metadata={"brand": "Acqua di Fuoco"},
    )
    state.on_stream_segment(seg)

    assert state.now_streaming["label"] == "Acqua di Fuoco"


def test_after_sweeper_logs_and_increments_segments():
    state = StationState()
    state.after_sweeper()
    assert state.segments_produced == 1
    assert state.segment_log[-1].type == "sweeper"


# ---------------------------------------------------------------------------
# ListenerProfile tests
# ---------------------------------------------------------------------------


def test_skip_rate_zero_on_no_plays():
    p = ListenerProfile()
    assert p.skip_rate == 0.0


def test_skip_rate_all_skipped():
    p = ListenerProfile(songs_played=5, songs_skipped=5)
    assert p.skip_rate == 1.0


def test_skip_rate_partial():
    p = ListenerProfile(songs_played=10, songs_skipped=3)
    assert abs(p.skip_rate - 0.3) < 0.001


def test_patterns_empty_below_three_outcomes():
    p = ListenerProfile()
    p.record_outcome(skipped=False, listen_sec=200)
    p.record_outcome(skipped=True, listen_sec=10)
    assert p.patterns == []


def test_patterns_restless_skipper():
    p = ListenerProfile()
    for _ in range(5):
        p.record_outcome(skipped=True, listen_sec=60)
    assert "restless_skipper" in p.patterns


def test_patterns_rides_every_song():
    p = ListenerProfile()
    for _ in range(6):
        p.record_outcome(skipped=False, listen_sec=200)
    assert "rides_every_song" in p.patterns


def test_patterns_bails_on_intros():
    p = ListenerProfile()
    for _ in range(3):
        p.record_outcome(skipped=False, listen_sec=200)
    for _ in range(3):
        p.record_outcome(skipped=True, listen_sec=15)
    assert "bails_on_intros" in p.patterns


def test_patterns_ballad_lover():
    p = ListenerProfile()
    for _ in range(3):
        p.record_outcome(skipped=False, listen_sec=200)
    for _ in range(3):
        p.record_outcome(skipped=False, listen_sec=200, energy_hint="low")
    assert "ballad_lover" in p.patterns


def test_patterns_energy_seeker():
    p = ListenerProfile()
    for _ in range(3):
        p.record_outcome(skipped=False, listen_sec=200)
    for _ in range(4):
        p.record_outcome(skipped=False, listen_sec=240, energy_hint="high")
    assert "energy_seeker" in p.patterns


def test_record_outcome_increments_counters():
    p = ListenerProfile()
    p.record_outcome(skipped=False, listen_sec=200)
    p.record_outcome(skipped=True, listen_sec=20)
    assert p.songs_played == 2
    assert p.songs_skipped == 1


def test_record_outcome_caps_recent_at_twenty():
    p = ListenerProfile()
    for i in range(25):
        p.record_outcome(skipped=False, listen_sec=float(i * 10))
    assert len(p.recent_outcomes) == 20


def test_describe_for_prompt_empty_on_no_patterns():
    p = ListenerProfile()
    assert p.describe_for_prompt() == ""


def test_describe_for_prompt_includes_pattern_description():
    p = ListenerProfile()
    for _ in range(5):
        p.record_outcome(skipped=True, listen_sec=60)
    desc = p.describe_for_prompt()
    assert "restless_skipper" not in desc  # internal key not exposed
    assert "salta" in desc  # Italian description


def test_describe_for_prompt_correct_prediction_callback():
    p = ListenerProfile()
    for _ in range(5):
        p.record_outcome(skipped=True, listen_sec=60)
    p.last_prediction = "salterà il prossimo"
    p.last_prediction_correct = True
    desc = p.describe_for_prompt()
    assert "PREDIZIONE PRECEDENTE CORRETTA" in desc
    assert "salterà il prossimo" in desc


def test_describe_for_prompt_wrong_prediction_callback():
    p = ListenerProfile()
    for _ in range(5):
        p.record_outcome(skipped=True, listen_sec=60)
    p.last_prediction = "rimarrà fino alla fine"
    p.last_prediction_correct = False
    desc = p.describe_for_prompt()
    assert "PREDIZIONE PRECEDENTE SBAGLIATA" in desc


def test_describe_for_prompt_unknown_pattern_returns_empty():
    """Patterns that exist but have no description entry return empty string."""
    p = ListenerProfile()
    # Inject a pattern that is not in the descriptions dict
    p.patterns.append("unknown_pattern_xyz")
    assert p.describe_for_prompt() == ""


def test_reserve_next_track_raises_on_empty_playlist():
    state = StationState(playlist=[])
    with pytest.raises(RuntimeError, match="Playlist is empty"):
        state.reserve_next_track()


def test_select_next_track_artist_over_represented():
    """Track with artist appearing >=2 times in recent 10 gets near-zero weight."""
    import random

    t1 = Track(title="Song A", artist="TestArtist", duration_ms=180000)
    t2 = Track(title="Song B", artist="OtherArtist", duration_ms=180000)
    state = StationState(playlist=[t1, t2])
    # Put TestArtist in played_tracks 3 times recently to trigger near-zero weight
    for _ in range(3):
        state.played_tracks.append(t1)

    # With TestArtist heavily penalized, OtherArtist should win consistently
    random.seed(42)
    results = [state.select_next_track() for _ in range(10)]
    assert all(r.artist == "OtherArtist" for r in results)


def test_select_next_track_popularity_boost():
    """Tracks with popularity score use popularity weight branch."""
    t_pop = Track(title="Popular", artist="A", duration_ms=180000, popularity=80)
    t_nop = Track(title="Obscure", artist="B", duration_ms=180000, popularity=0)
    state = StationState(playlist=[t_pop, t_nop])
    # Just verify selection works without error and picks one
    result = state.select_next_track()
    assert result in (t_pop, t_nop)


def test_add_joke_duplicate_not_added():
    state = StationState()
    state.add_joke("same joke")
    state.add_joke("same joke")
    assert state.running_jokes.count("same joke") == 1


def test_select_next_track_no_hourly_cap_branch():
    """Calling with max_artist_per_hour=0 skips the hourly-cap filter branch."""
    t1 = _track(1)
    t2 = _track(2)
    state = StationState(playlist=[t1, t2])
    result = state.select_next_track(max_artist_per_hour=0)
    assert result in (t1, t2)


def test_select_next_track_hour_window_branches():
    """With >17 played tracks, some are outside the hour window (i < hour_start)
    and >10 played tracks means some are outside the artist_10 window (i < artist_10_start)."""
    t_filler = Track(title="Filler", artist="FillArtist", duration_ms=180000, spotify_id="fill")
    t_new = _track(99)
    state = StationState(playlist=[t_filler, t_new])
    # 20 played entries: only last 17 in the hour window, only last 10 in artist_10 window
    for _ in range(20):
        state.played_tracks.append(t_filler)
    result = state.select_next_track()
    assert result in (t_filler, t_new)


def test_select_next_track_artist_over_represented_as_candidate():
    """An artist in the candidate pool that appeared >=2 times in recent 10 gets w*=0.05."""
    import random

    filler = [
        Track(title=f"Filler{i}", artist="FillArtist", duration_ms=180000, spotify_id=f"fill{i}") for i in range(8)
    ]
    t_pop = Track(title="PopSong", artist="PopArtist", duration_ms=180000, spotify_id="pop1")
    t_other = Track(title="Other", artist="OtherArtist", duration_ms=180000, spotify_id="oth1")
    state = StationState(playlist=[t_pop, t_other])
    # t_pop played at positions 0-1 (not in recent_keys for cooldown=8 with 10 total),
    # but within recent_artist_10 (all 10 plays) → recent_artist_10[PopArtist] = 2 ≥ 2
    state.played_tracks.extend([t_pop, t_pop, *filler])  # 10 total
    # Both t_pop and t_other are candidates (t_pop not in recent_keys).
    # t_pop weight should be near-zero so t_other wins consistently.
    random.seed(0)
    results = [state.select_next_track() for _ in range(10)]
    assert all(r.artist == "OtherArtist" for r in results)


def test_select_next_track_explicit_filter_in_relaxed_fallback():
    """allow_explicit=False filters explicit tracks from relaxed candidates (lines 585, 590)."""
    t_normal = Track(title="Normal", artist="A", duration_ms=180000, spotify_id="n1")
    t_explicit = Track(title="Explicit", artist="B", duration_ms=180000, spotify_id="ex1", explicit=True)
    state = StationState(playlist=[t_normal, t_explicit])
    # Play t_normal enough to push it into recent_keys AND recent_artist_set
    for _ in range(10):
        state.played_tracks.append(t_normal)
    # Strict filter: t_normal filtered by repeat; t_explicit by allow_explicit=False
    # Relax 1 (drop hourly cap): t_normal filtered by repeat+artist; t_explicit filtered by explicit
    # Relax 2 (drop artist): t_normal filtered by repeat; t_explicit filtered by explicit → empty
    # Final fallback: pool = [t_normal, t_explicit], t_explicit never played → staleness n_played+1
    result = state.select_next_track(allow_explicit=False)
    # With both tracks failing all explicit-aware relaxes, final fallback picks highest staleness.
    # t_explicit was never played → staleness = n_played + 1 (highest). But allow_explicit filter
    # does NOT apply in final fallback → t_explicit may be picked despite being explicit.
    assert result in (t_normal, t_explicit)


# ---------------------------------------------------------------------------
# Producer rescue-bridge telemetry (#547 observability)
# ---------------------------------------------------------------------------


def test_record_bridge_fire_counts_total_by_type_and_event():
    state = StationState()
    state.record_bridge_fire("drain", "canned", timestamp=100.0)

    assert state.bridge_fires_total == 1
    assert state.bridge_fires_by_type == {"drain": 1, "resume": 0, "idle": 0, "continuity": 0}
    assert list(state.bridge_events) == [{"bridge_type": "drain", "source": "canned", "timestamp": 100.0}]


def test_record_bridge_fire_accumulates_across_types():
    state = StationState()
    state.record_bridge_fire("drain", "canned", timestamp=1.0)
    state.record_bridge_fire("resume", "norm_cache", timestamp=2.0)
    state.record_bridge_fire("idle", "canned", timestamp=3.0)
    state.record_bridge_fire("drain", "emergency_tone", timestamp=4.0)
    state.record_bridge_fire("continuity", "norm_cache", timestamp=5.0)

    assert state.bridge_fires_total == 5
    assert state.bridge_fires_by_type == {"drain": 2, "resume": 1, "idle": 1, "continuity": 1}
    # last_fire is the deque tail
    assert state.bridge_events[-1] == {
        "bridge_type": "continuity",
        "source": "norm_cache",
        "timestamp": 5.0,
    }


def test_record_bridge_fire_total_survives_deque_eviction():
    """bridge_events is bounded (maxlen=50) but bridge_fires_total is the true
    session lifetime count — it must keep climbing past the deque cap."""
    state = StationState()
    for i in range(120):
        state.record_bridge_fire("drain", "norm_cache", timestamp=float(i))

    assert state.bridge_fires_total == 120
    assert state.bridge_fires_by_type["drain"] == 120
    assert len(state.bridge_events) == 50  # deque cap, oldest evicted
    assert state.bridge_events[0]["timestamp"] == 70.0  # 120 - 50


def test_record_bridge_fire_defaults_timestamp_to_now():
    state = StationState()
    with patch("mammamiradio.core.models.time.time", return_value=1234.5):
        state.record_bridge_fire("idle", "norm_cache")

    assert state.bridge_events[-1]["timestamp"] == 1234.5


def test_record_bridge_fire_ignores_unknown_bridge_type_in_by_type():
    """An unexpected bridge_type still counts toward the total and the event
    trail, it just does not create a stray by_type bucket."""
    state = StationState()
    state.record_bridge_fire("mystery", "canned", timestamp=1.0)

    assert state.bridge_fires_total == 1
    assert state.bridge_fires_by_type == {"drain": 0, "resume": 0, "idle": 0, "continuity": 0}
    assert state.bridge_events[-1]["bridge_type"] == "mystery"


# ---------------------------------------------------------------------------
# Generated segment waste telemetry (#397 observability)
# ---------------------------------------------------------------------------


def test_record_discard_counts_total_duration_reason_and_type(tmp_path):
    state = StationState()
    segment = Segment(type=SegmentType.BANTER, path=tmp_path / "b.mp3", duration_sec=12.5)

    state.record_discard(segment, reason="stale_source", timestamp=100.0)

    assert state.discarded_segments_total == 1
    assert state.discarded_duration_total_sec == 12.5
    assert state.discard_by_reason == {"stale_source": 1}
    assert state.discard_by_type == {"banter": 1}
    assert list(state.discard_events) == [
        {
            "reason": "stale_source",
            "type": "banter",
            "duration_sec": 12.5,
            "timestamp": 100.0,
            "already_counted_in_produced": False,
        }
    ]
    assert state.discarded_unproduced_segments_total == 1


def test_record_discard_tracks_when_segment_was_already_counted_as_produced(tmp_path):
    state = StationState(segments_produced=1)
    segment = Segment(type=SegmentType.MUSIC, path=tmp_path / "m.mp3", duration_sec=30.0)

    state.record_discard(
        segment,
        reason="source_switch",
        timestamp=100.0,
        already_counted_in_produced=True,
    )

    assert state.discarded_segments_total == 1
    assert state.discarded_unproduced_segments_total == 0
    assert state.discard_events[-1]["already_counted_in_produced"] is True


def test_record_discard_survives_release_campaign_exception(tmp_path):
    """record_queue_discard()/save_if_dirty() are wrapped in a bare `except
    Exception: pass` — a raising campaign must not break the discard
    bookkeeping that runs alongside it."""

    class _BoomCampaign:
        def record_queue_discard(self, metadata):
            raise RuntimeError("ledger corrupt")

    state = StationState(release_campaign=_BoomCampaign())
    segment = Segment(type=SegmentType.BANTER, path=tmp_path / "b.mp3", duration_sec=12.5)

    state.record_discard(segment, reason="operator_purge", timestamp=100.0)

    assert state.discarded_segments_total == 1
    assert state.discard_events[-1]["reason"] == "operator_purge"


def test_record_discard_survives_release_campaign_save_exception(tmp_path):
    """Same guard, but the failure lands one call later: record_queue_discard()
    succeeds and only save_if_dirty() raises — a separate code path from the
    record_queue_discard()-raises case above."""

    class _BoomOnSaveCampaign:
        def record_queue_discard(self, metadata):
            return True

        def save_if_dirty(self):
            raise RuntimeError("disk full")

    state = StationState(release_campaign=_BoomOnSaveCampaign())
    segment = Segment(type=SegmentType.BANTER, path=tmp_path / "b.mp3", duration_sec=12.5)

    state.record_discard(segment, reason="operator_purge", timestamp=100.0)

    assert state.discarded_segments_total == 1
    assert state.discard_events[-1]["reason"] == "operator_purge"


def test_record_discard_tolerates_zero_duration_and_never_raises():
    state = StationState()
    bad_segment = Segment(type=SegmentType.MUSIC, path=Path("/tmp/x.mp3"), duration_sec=0.0)

    state.record_discard(bad_segment, reason="session_stopped")
    state.record_discard(bad_segment, reason="session_stopped")

    assert state.discarded_segments_total == 2
    assert state.discarded_duration_total_sec == 0.0


def test_record_discard_total_survives_deque_eviction(tmp_path):
    state = StationState()
    segment = Segment(type=SegmentType.MUSIC, path=tmp_path / "m.mp3", duration_sec=1.0)

    for i in range(120):
        state.record_discard(segment, reason="operator_stop", timestamp=float(i))

    assert state.discarded_segments_total == 120
    assert len(state.discard_events) == 100
    assert state.discard_events[0]["timestamp"] == 20.0


def test_record_discard_defaults_timestamp_to_now(tmp_path):
    state = StationState()
    segment = Segment(type=SegmentType.AD, path=tmp_path / "a.mp3", duration_sec=5.0)

    with patch("mammamiradio.core.models.time.time", return_value=999.0):
        state.record_discard(segment, reason="operator_panic")

    assert state.discard_events[-1]["timestamp"] == 999.0


def test_generation_waste_reason_string_values_are_stable():
    # These strings are persisted in discard_events, surfaced on /api/status, and
    # mapped to operator-friendly labels in admin.html — they must not drift (#397).
    from mammamiradio.core.models import GenerationWasteReason

    assert GenerationWasteReason.QUALITY_GATE_REJECT == "quality_gate_reject"
    assert GenerationWasteReason.STALE_PLAYLIST == "stale_playlist"
    assert GenerationWasteReason.STALE_SOURCE == "stale_source"


# ---------------------------------------------------------------------------
# segment_track_key — the segment-side mirror of normalized_track_key.
# ---------------------------------------------------------------------------


def test_segment_track_key_matches_normalized_track_key_for_the_same_song():
    """The invariant the rotation-membership check rests on.

    A rendered music Segment carries `artist` + `title_only` verbatim from its
    Track, so the two identity functions must agree — otherwise a finished
    render could be judged "no longer in the rotation" while its Track sits
    right there in the pool.
    """
    track = Track(title="Dont Lose Your Way", artist="Fleece", duration_ms=211_000, spotify_id="x")
    segment = Segment(
        type=SegmentType.MUSIC,
        path=Path("/cache/norm_x.mp3"),
        metadata={"title": track.display, "title_only": track.title, "artist": track.artist},
    )

    assert segment_track_key(segment) == normalized_track_key(track)


def test_segment_track_key_normalizes_case_and_whitespace_and_survives_junk():
    """Same shape as Track.normalized_key, and it never raises on odd metadata."""
    padded = Segment(
        type=SegmentType.MUSIC,
        path=Path("/cache/x.mp3"),
        metadata={"title_only": "  Dont Lose Your WAY ", "artist": " Fleece  "},
    )
    assert segment_track_key(padded) == ("fleece", "dont lose your way")

    # Norm-cache bridges and rescue fills stamp only `title`.
    title_only_fallback = Segment(
        type=SegmentType.MUSIC, path=Path("/cache/x.mp3"), metadata={"title": "Io Vagabondo", "artist": "Nomadi"}
    )
    assert segment_track_key(title_only_fallback) == ("nomadi", "io vagabondo")

    # Missing / non-dict metadata degrades to empty rather than raising into
    # the audio path.
    assert segment_track_key(Segment(type=SegmentType.BANTER, path=Path("/x.mp3"), metadata={})) == ("", "")
    bad = Segment(type=SegmentType.BANTER, path=Path("/x.mp3"))
    bad.metadata = "not a dict"  # type: ignore[assignment]
    assert segment_track_key(bad) == ("", "")


def test_segment_track_key_coalesces_an_explicit_none_artist():
    """An explicit ``artist: None`` keys as "", never the string "none".

    No site stamps a null artist today — every construction omits the key when
    it is falsy — so this pins the contract rather than a live bug. It matters
    because the hand-rolled copy in ``_apply_ban`` used ``.get("artist", "")``,
    where a null artist becomes ``str(None)`` -> "none" and the segment stops
    matching any ban. That copy now delegates here; this keeps the canonical
    definition safe for whichever site stamps a null artist first.
    """
    nulled = Segment(
        type=SegmentType.MUSIC,
        path=Path("/cache/x.mp3"),
        metadata={"title_only": "Senza Nome", "artist": None},
    )
    assert segment_track_key(nulled) == ("", "senza nome")
    assert "none" not in segment_track_key(nulled)
