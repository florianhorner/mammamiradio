"""Tests for StationState lifecycle methods and Track properties."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from mammamiradio.core.models import ChaosSubtype, Heading, ListenerProfile, Segment, SegmentType, StationState, Track


def _track(n: int = 1) -> Track:
    return Track(title=f"Song {n}", artist=f"Artist {n}", duration_ms=200000, spotify_id=f"id{n}")


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


def test_segment_log_capped_at_50():
    state = StationState()
    for i in range(60):
        state.after_music(_track(i))
    assert len(state.segment_log) == 50


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
    state.now_streaming = {
        "type": "music",
        "label": "Prev Song",
        "started": 100.0,
    }
    seg = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/fake2.mp3"),
        metadata={"title": "Banter"},
    )

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


def test_switch_playlist_clears_listener_request_state():
    state = StationState(playlist=[_track(1)])
    state.pending_requests.append({"name": "Luca", "message": "ciao", "type": "shoutout"})
    state.pending_actions.append({"type": "skip_bridge"})
    state._listener_request_rl = {"127.0.0.1": 123.0}
    state.pinned_track = _track(99)
    state.force_next = SegmentType.BANTER

    state.switch_playlist([_track(2)])

    assert state.pending_requests == []
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

    picked = state.select_next_track()

    assert picked is pinned
    assert state.pinned_track is None


def test_select_next_track_most_stale_fallback():
    stale = _track(1)
    recent = _track(2)
    state = StationState(playlist=[stale, recent])
    # Ensure repeat cooldown excludes the whole pool, forcing fallback.
    state.played_tracks.extend([stale, recent, recent])

    picked = state.select_next_track()

    assert picked == stale


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

    with patch("mammamiradio.core.models.random.choices", side_effect=lambda candidates, **_: [candidates[0]]):
        picked = state.select_next_track(repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0)

    assert picked is tagged


def test_select_next_track_heading_bias_decays_after_budget_spent():
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

    with patch("mammamiradio.core.models.random.choices", side_effect=lambda candidates, **_: [candidates[0]]):
        picked = state.select_next_track(repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0)

    assert picked is normal


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


def test_after_music_never_exceeds_heading_selection_budget():
    """selection_spent stops at selection_budget even if the same tagged track airs
    again — the budget cap is what retires the heading bias, so it must never overrun."""
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

    assert heading.selection_spent == 1


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
    assert state.bridge_fires_by_type == {"drain": 1, "resume": 0, "idle": 0}
    assert list(state.bridge_events) == [{"bridge_type": "drain", "source": "canned", "timestamp": 100.0}]


def test_record_bridge_fire_accumulates_across_types():
    state = StationState()
    state.record_bridge_fire("drain", "canned", timestamp=1.0)
    state.record_bridge_fire("resume", "norm_cache", timestamp=2.0)
    state.record_bridge_fire("idle", "canned", timestamp=3.0)
    state.record_bridge_fire("drain", "emergency_tone", timestamp=4.0)

    assert state.bridge_fires_total == 4
    assert state.bridge_fires_by_type == {"drain": 2, "resume": 1, "idle": 1}
    # last_fire is the deque tail
    assert state.bridge_events[-1] == {
        "bridge_type": "drain",
        "source": "emergency_tone",
        "timestamp": 4.0,
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
    assert state.bridge_fires_by_type == {"drain": 0, "resume": 0, "idle": 0}
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
