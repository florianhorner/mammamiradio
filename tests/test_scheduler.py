from __future__ import annotations

import random

import pytest

from mammamiradio.config import PacingSection
from mammamiradio.models import SegmentType, StationState, Track


def _make_state(**kwargs) -> StationState:
    return StationState(
        playlist=[Track(title="Test", artist="Test", duration_ms=200000, spotify_id="test1")],
        **kwargs,
    )


def test_first_segment_is_music():
    """First segment produced should always be MUSIC."""
    from mammamiradio.scheduler import next_segment_type

    state = _make_state(segments_produced=0)
    pacing = PacingSection()
    assert next_segment_type(state, pacing) == SegmentType.MUSIC


def test_ad_triggers_after_threshold():
    """AD should trigger when songs_since_ad >= songs_between_ads."""
    from mammamiradio.scheduler import next_segment_type

    pacing = PacingSection(songs_between_ads=4, songs_between_banter=2)
    state = _make_state(segments_produced=5, songs_since_ad=4, songs_since_banter=0)
    assert next_segment_type(state, pacing) == SegmentType.AD


def test_banter_triggers_with_jitter():
    """BANTER should trigger when songs_since_banter >= threshold (with jitter)."""
    from mammamiradio.scheduler import next_segment_type

    pacing = PacingSection(songs_between_banter=2, songs_between_ads=10)
    # With songs_since_banter=2 and threshold=2+randint(-1,0), threshold is 1 or 2.
    # Either way, songs_since_banter(2) >= threshold(1 or 2), so BANTER.
    random.seed(42)
    state = _make_state(segments_produced=3, songs_since_banter=2, songs_since_ad=0)
    result = next_segment_type(state, pacing)
    assert result == SegmentType.BANTER


def test_default_is_music():
    """When no trigger is met, default to MUSIC."""
    from mammamiradio.scheduler import next_segment_type

    pacing = PacingSection(songs_between_banter=5, songs_between_ads=10)
    state = _make_state(segments_produced=2, songs_since_banter=1, songs_since_ad=1)
    assert next_segment_type(state, pacing) == SegmentType.MUSIC


def test_reserve_next_track_rotates_upcoming_playlist():
    state = StationState(
        playlist=[
            Track(title="One", artist="A", duration_ms=1, spotify_id="1"),
            Track(title="Two", artist="B", duration_ms=1, spotify_id="2"),
            Track(title="Three", artist="C", duration_ms=1, spotify_id="3"),
        ]
    )

    track = state.reserve_next_track()

    assert track.spotify_id == "1"
    assert [t.spotify_id for t in state.playlist] == ["2", "3", "1"]


def test_select_next_track_returns_from_pool():
    """select_next_track picks a track from the playlist pool."""
    tracks = [
        Track(title="A", artist="Art1", duration_ms=1, spotify_id="a"),
        Track(title="B", artist="Art2", duration_ms=1, spotify_id="b"),
        Track(title="C", artist="Art3", duration_ms=1, spotify_id="c"),
    ]
    state = StationState(playlist=tracks)
    pick = state.select_next_track()
    assert pick in tracks


def test_select_next_track_avoids_recently_played():
    """Tracks in the repeat_cooldown window should not be picked when alternatives exist."""
    tracks = [
        Track(title="A", artist="Art1", duration_ms=1, spotify_id="a"),
        Track(title="B", artist="Art2", duration_ms=1, spotify_id="b"),
        Track(title="C", artist="Art3", duration_ms=1, spotify_id="c"),
    ]
    state = StationState(
        playlist=tracks,
        played_tracks=[tracks[0], tracks[1]],
    )
    # With repeat_cooldown=2, tracks a and b were played in last 2 — only c eligible
    picks = {state.select_next_track(repeat_cooldown=2).spotify_id for _ in range(20)}
    assert picks == {"c"}


def test_select_next_track_avoids_same_artist():
    """Artist cooldown should prevent back-to-back same artist."""
    tracks = [
        Track(title="Hit1", artist="SameArtist", duration_ms=1, spotify_id="s1"),
        Track(title="Hit2", artist="SameArtist", duration_ms=1, spotify_id="s2"),
        Track(title="Other", artist="DiffArtist", duration_ms=1, spotify_id="d1"),
    ]
    state = StationState(
        playlist=tracks,
        played_tracks=[tracks[0]],  # SameArtist just played
    )
    picks = {state.select_next_track(artist_cooldown=1).spotify_id for _ in range(20)}
    assert picks == {"d1"}


def test_select_next_track_filters_explicit():
    """allow_explicit=False should skip explicit tracks."""
    tracks = [
        Track(title="Clean", artist="A", duration_ms=1, spotify_id="c1", explicit=False),
        Track(title="Dirty", artist="B", duration_ms=1, spotify_id="e1", explicit=True),
    ]
    state = StationState(playlist=tracks)
    picks = {state.select_next_track(allow_explicit=False).spotify_id for _ in range(20)}
    assert picks == {"c1"}


def test_select_next_track_relaxes_on_small_pool():
    """When all tracks are filtered out, fallback should still return something."""
    tracks = [
        Track(title="Only", artist="Solo", duration_ms=1, spotify_id="only"),
    ]
    state = StationState(
        playlist=tracks,
        played_tracks=[tracks[0]],  # Just played the only track
    )
    # Despite repeat_cooldown, should still return the track (relaxed filters)
    pick = state.select_next_track(repeat_cooldown=5)
    assert pick.spotify_id == "only"


def test_select_next_track_does_not_mutate_playlist():
    """select_next_track should not modify the playlist (unlike reserve_next_track)."""
    tracks = [
        Track(title="A", artist="A", duration_ms=1, spotify_id="a"),
        Track(title="B", artist="B", duration_ms=1, spotify_id="b"),
    ]
    state = StationState(playlist=list(tracks))
    state.select_next_track()
    assert len(state.playlist) == 2
    assert [t.spotify_id for t in state.playlist] == ["a", "b"]


def test_preview_upcoming_uses_current_playlist_order():
    from mammamiradio.scheduler import preview_upcoming

    tracks = [
        Track(title="One", artist="A", duration_ms=1, spotify_id="1"),
        Track(title="Two", artist="B", duration_ms=1, spotify_id="2"),
        Track(title="Three", artist="C", duration_ms=1, spotify_id="3"),
    ]
    state = StationState(
        playlist=tracks,
        segments_produced=1,
        songs_since_banter=0,
        songs_since_ad=0,
        current_track=Track(title="Old", artist="Z", duration_ms=1, spotify_id="old"),
    )
    pacing = PacingSection(songs_between_banter=99, songs_between_ads=99)

    preview = preview_upcoming(state, pacing, state.playlist, count=3)

    assert [item["playlist_index"] for item in preview] == [0, 1, 2]
    assert [item["label"] for item in preview] == [
        "A – One",
        "B – Two",
        "C – Three",
    ]


def test_select_next_track_uses_cache_key_not_spotify_id():
    """Two tracks with same spotify_id='' but different titles have distinct cache_keys."""
    tracks = [
        Track(title="Song Alpha", artist="Art1", duration_ms=1, spotify_id=""),
        Track(title="Song Beta", artist="Art2", duration_ms=1, spotify_id=""),
    ]
    state = StationState(
        playlist=tracks,
        played_tracks=[tracks[0]],  # Only "Song Alpha" played
    )
    # repeat_cooldown=1 means last 1 played track is excluded by cache_key.
    # If identity were spotify_id (both ""), BOTH would be excluded. With cache_key, only Alpha is.
    picks = {state.select_next_track(repeat_cooldown=1, artist_cooldown=0).cache_key for _ in range(20)}
    assert tracks[1].cache_key in picks


def test_select_next_track_max_artist_per_hour():
    """max_artist_per_hour caps how often one artist appears in the rolling hour window."""
    tracks = [
        Track(title="A1", artist="ArtistA", duration_ms=1, spotify_id="a1"),
        Track(title="A2", artist="ArtistA", duration_ms=1, spotify_id="a2"),
        Track(title="A3", artist="ArtistA", duration_ms=1, spotify_id="a3"),
        Track(title="B1", artist="ArtistB", duration_ms=1, spotify_id="b1"),
    ]
    # Simulate 3 ArtistA tracks already played in the hour window
    state = StationState(
        playlist=tracks,
        played_tracks=[
            Track(title="A1", artist="ArtistA", duration_ms=1, spotify_id="a1"),
            Track(title="A2", artist="ArtistA", duration_ms=1, spotify_id="a2"),
            Track(title="A3", artist="ArtistA", duration_ms=1, spotify_id="a3"),
        ],
    )
    # With max_artist_per_hour=3, ArtistA is at the cap → only ArtistB eligible
    picks = {
        state.select_next_track(repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=3).spotify_id
        for _ in range(30)
    }
    assert picks == {"b1"}


def test_select_next_track_never_played_bonus():
    """Never-played tracks get a 1.2x bonus and should be picked more often."""
    random.seed(12345)
    played_a = Track(title="Played A", artist="X", duration_ms=1, spotify_id="pa")
    played_b = Track(title="Played B", artist="Y", duration_ms=1, spotify_id="pb")
    fresh = Track(title="Fresh One", artist="Z", duration_ms=1, spotify_id="fr")
    tracks = [played_a, played_b, fresh]
    # Build played history: both A and B played multiple times recently
    played_history = [played_a, played_b] * 5
    state = StationState(playlist=tracks, played_tracks=played_history)

    counts: dict[str, int] = {}
    for _ in range(200):
        pick = state.select_next_track(repeat_cooldown=0, artist_cooldown=0)
        counts[pick.spotify_id] = counts.get(pick.spotify_id, 0) + 1

    # The fresh track should dominate due to 1.2x bonus + recency penalty on played tracks
    assert counts.get("fr", 0) > 120, f"Expected fresh track picked >60%, got {counts}"


def test_select_next_track_empty_playlist_raises():
    """Empty playlist should raise RuntimeError."""
    state = StationState(playlist=[])
    with pytest.raises(RuntimeError, match="Playlist is empty"):
        state.select_next_track()


def test_news_flash_triggers_when_songs_since_news_threshold_met():
    """NEWS_FLASH is returned when songs_since_news >= 6 and banter threshold is also met."""
    from mammamiradio.scheduler import _decide

    pacing = PacingSection(songs_between_banter=2, songs_between_ads=10)
    result = _decide(
        segments_produced=5,
        songs_since_ad=1,
        songs_since_banter=5,
        pacing=pacing,
        deterministic=True,
        songs_since_news=6,
    )
    assert result == SegmentType.NEWS_FLASH


def test_news_flash_not_triggered_below_threshold():
    """NEWS_FLASH is NOT returned when songs_since_news < 6 (falls through to BANTER)."""
    from mammamiradio.scheduler import _decide

    pacing = PacingSection(songs_between_banter=2, songs_between_ads=10)
    result = _decide(
        segments_produced=5,
        songs_since_ad=1,
        songs_since_banter=5,
        pacing=pacing,
        deterministic=True,
        songs_since_news=3,
    )
    assert result == SegmentType.BANTER


def test_station_id_triggers_deterministically():
    """STATION_ID fires deterministically when segments_since_station_id >= 5 and last_micro >= 2."""
    from mammamiradio.scheduler import _decide

    pacing = PacingSection(songs_between_banter=5, songs_between_ads=10)
    result = _decide(
        segments_produced=10,
        songs_since_ad=1,
        songs_since_banter=1,
        pacing=pacing,
        deterministic=True,
        songs_since_news=0,
        segments_since_station_id=5,
        segments_since_time_check=2,
    )
    assert result == SegmentType.STATION_ID


def test_time_check_triggers_deterministically():
    """TIME_CHECK fires when segments_since_time_check >= 8 and station_id guard doesn't fire first."""
    from mammamiradio.scheduler import _decide

    pacing = PacingSection(songs_between_banter=5, songs_between_ads=10)
    result = _decide(
        segments_produced=10,
        songs_since_ad=1,
        songs_since_banter=1,
        pacing=pacing,
        deterministic=True,
        songs_since_news=0,
        segments_since_station_id=2,  # below station_id threshold of 5
        segments_since_time_check=8,
    )
    assert result == SegmentType.TIME_CHECK


def test_preview_upcoming_includes_news_flash():
    """preview_upcoming simulation produces a NEWS_FLASH when songs_since_news is primed."""
    from mammamiradio.scheduler import preview_upcoming

    pacing = PacingSection(songs_between_banter=2, songs_between_ads=10)
    tracks = [Track(title=f"T{i}", artist="A", duration_ms=200000, spotify_id=str(i)) for i in range(10)]
    state = _make_state(
        segments_produced=5,
        songs_since_banter=0,
        songs_since_ad=0,
        songs_since_news=6,
    )
    state.playlist = tracks
    preview = preview_upcoming(state, pacing, tracks, count=12)
    types = [p["type"] for p in preview]
    assert "news_flash" in types


def test_preview_upcoming_includes_time_check():
    """preview_upcoming simulation produces a TIME_CHECK when the cadence counter is primed.

    Station ID check (>= 5 segments) must NOT fire first, so set
    segments_since_station_id low (2) but segments_since_time_check high (8).
    The deterministic path always fires TIME_CHECK at 8+ segments, skipping
    the 25% probability gate.
    """
    from mammamiradio.scheduler import preview_upcoming

    pacing = PacingSection(songs_between_banter=5, songs_between_ads=10)
    tracks = [Track(title=f"T{i}", artist="A", duration_ms=200000, spotify_id=str(i)) for i in range(10)]
    state = _make_state(
        segments_produced=1,
        songs_since_banter=0,
        songs_since_ad=0,
        songs_since_news=0,
        segments_since_station_id=2,  # below STATION_ID threshold (5)
        segments_since_time_check=8,  # at TIME_CHECK threshold (8)
    )
    state.playlist = tracks
    preview = preview_upcoming(state, pacing, tracks, count=5)
    types = [p["type"] for p in preview]
    assert "time_check" in types, f"Expected time_check in preview but got: {types}"
