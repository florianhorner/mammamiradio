"""Tests for StationState lifecycle methods and Track properties."""

from __future__ import annotations

import random
from pathlib import Path
from unittest.mock import patch

import pytest

from mammamiradio.models import ListenerProfile, Segment, SegmentType, StationState, Track


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
        metadata={"title": "Test Song"},
    )
    state.on_stream_segment(seg)

    assert state.now_streaming["type"] == "music"
    assert state.now_streaming["label"] == "Test Song"
    assert len(state.stream_log) == 1


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

    with patch("mammamiradio.models.time.time", return_value=130.0):
        state.on_stream_segment(seg)

    assert state.listener.songs_played == 1
    assert state.listener.segments_since_taste_mirror == 1


def test_track_cache_key():
    t = Track(title="Con te partirò!", artist="Andrea Bocelli", duration_ms=250000, spotify_id="x")
    key = t.cache_key
    assert key == "andrea_bocelli_con_te_partir"
    assert len(key) <= 80


def test_track_display():
    t = _track()
    assert t.display == "Artist 1 – Song 1"


def test_switch_playlist_clears_listener_request_state():
    state = StationState(playlist=[_track(1)])
    state.pending_requests.append({"name": "Luca", "message": "ciao", "type": "shoutout"})
    state._listener_request_rl = {"127.0.0.1": 123.0}
    state.pinned_track = _track(99)
    state.force_next = SegmentType.BANTER

    state.switch_playlist([_track(2)])

    assert state.pending_requests == []
    assert state._listener_request_rl == {}
    assert state.pinned_track is None
    assert state.force_next is None


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
