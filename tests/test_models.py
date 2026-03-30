"""Tests for StationState lifecycle methods and Track properties."""

from __future__ import annotations

from pathlib import Path

from mammamiradio.models import Segment, SegmentType, StationState, Track


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


def test_track_cache_key():
    t = Track(title="Con te partirò!", artist="Andrea Bocelli", duration_ms=250000, spotify_id="x")
    key = t.cache_key
    assert key == "andrea_bocelli_con_te_partir"
    assert len(key) <= 80


def test_track_display():
    t = _track()
    assert t.display == "Artist 1 – Song 1"
