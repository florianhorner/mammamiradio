"""Tests for playlist module: demo tracks, URL extraction."""

from __future__ import annotations

from fakeitaliradio.playlist import DEMO_TRACKS, _extract_playlist_id


def test_demo_tracks_has_entries():
    assert len(DEMO_TRACKS) == 10
    for t in DEMO_TRACKS:
        assert t.title
        assert t.artist
        assert t.duration_ms > 0
        assert t.spotify_id.startswith("demo")


def test_extract_playlist_id_from_url():
    url = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
    assert _extract_playlist_id(url) == "37i9dQZF1DXcBWIGoYBM5M"


def test_extract_playlist_id_with_query_params():
    url = "https://open.spotify.com/playlist/abc123?si=xyz"
    assert _extract_playlist_id(url) == "abc123"


def test_extract_playlist_id_returns_none_for_invalid():
    assert _extract_playlist_id("not a url") is None
    assert _extract_playlist_id("https://open.spotify.com/track/abc") is None
