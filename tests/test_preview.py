"""Tests for scheduler.preview_upcoming."""

from __future__ import annotations

from mammamiradio.config import PacingSection
from mammamiradio.models import StationState, Track
from mammamiradio.scheduler import preview_upcoming


def _tracks(n: int = 5) -> list[Track]:
    return [Track(title=f"Song {i}", artist=f"Artist {i}", duration_ms=200000, spotify_id=f"id{i}") for i in range(n)]


def test_preview_returns_correct_count():
    state = StationState(playlist=_tracks(), segments_produced=1)
    pacing = PacingSection(songs_between_banter=2, songs_between_ads=4)
    result = preview_upcoming(state, pacing, _tracks(), count=5)
    assert len(result) == 5


def test_preview_does_not_mutate_state():
    state = StationState(playlist=_tracks(), segments_produced=1, songs_since_banter=0, songs_since_ad=0)
    pacing = PacingSection(songs_between_banter=2, songs_between_ads=4)
    original_produced = state.segments_produced
    original_banter = state.songs_since_banter
    preview_upcoming(state, pacing, _tracks(), count=10)
    assert state.segments_produced == original_produced
    assert state.songs_since_banter == original_banter


def test_preview_includes_banter_and_ads():
    state = StationState(playlist=_tracks(), segments_produced=1, songs_since_banter=0, songs_since_ad=0)
    pacing = PacingSection(songs_between_banter=2, songs_between_ads=4)
    result = preview_upcoming(state, pacing, _tracks(), count=10)
    types = [r["type"] for r in result]
    assert "banter" in types
    assert "music" in types


def test_preview_music_has_track_labels():
    tracks = _tracks()
    state = StationState(playlist=tracks, segments_produced=1, songs_since_banter=0, songs_since_ad=0)
    pacing = PacingSection(songs_between_banter=10, songs_between_ads=20)
    result = preview_upcoming(state, pacing, tracks, count=3)
    for r in result:
        assert r["type"] == "music"
        assert "Artist" in r["label"]
