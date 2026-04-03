"""Tests for playlist loading behavior."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from mammamiradio.config import load_config
from mammamiradio.models import PlaylistSource
from mammamiradio.playlist import (
    DEMO_TRACKS,
    fetch_playlist,
    fetch_startup_playlist,
    list_user_playlists,
    load_explicit_source,
    read_persisted_source,
)


@pytest.fixture()
def config():
    return load_config()


@pytest.fixture()
def config_with_spotify():
    cfg = load_config()
    cfg.spotify_client_id = "test-client-id"
    cfg.spotify_client_secret = "test-client-secret"
    cfg.playlist.spotify_url = "https://open.spotify.com/playlist/abc123"
    return cfg


def _make_spotify_track(name: str, artist: str, track_id: str, duration_ms: int = 200000):
    """Build a Spotify API track item dict."""
    return {
        "track": {
            "name": name,
            "artists": [{"name": artist}],
            "duration_ms": duration_ms,
            "id": track_id,
        }
    }


def _make_spotify_item_track(name: str, artist: str, track_id: str, duration_ms: int = 200000):
    """Build a Spotify playlist item that returns data under `track`."""
    return {
        "track": {
            "name": name,
            "artists": [{"name": artist}],
            "duration_ms": duration_ms,
            "id": track_id,
        }
    }


# --- No credentials → demo tracks ---


def test_no_credentials_returns_demo_tracks(config):
    config.spotify_client_id = ""
    config.spotify_client_secret = ""
    result = fetch_playlist(config)
    assert len(result) == len(DEMO_TRACKS)
    # All titles should come from the demo set
    demo_titles = {t.title for t in DEMO_TRACKS}
    for t in result:
        assert t.title in demo_titles


def test_no_credentials_shuffles_when_configured(config):
    config.spotify_client_id = ""
    config.spotify_client_secret = ""
    config.playlist.shuffle = True
    # Run multiple times — at least one ordering should differ (probabilistic but near-certain)
    results = [tuple(t.title for t in fetch_playlist(config)) for _ in range(10)]
    # With 10 tracks shuffled 10 times, extremely unlikely all orderings are identical
    assert len(set(results)) > 1


# --- With credentials + playlist URL → fetches from Spotify ---


def test_fetches_playlist_from_spotify(config_with_spotify):
    mock_sp = MagicMock()
    mock_sp.playlist_tracks.return_value = {
        "items": [
            _make_spotify_track("Canzone Uno", "Artista A", "id1"),
            _make_spotify_track("Canzone Due", "Artista B", "id2"),
        ],
        "next": None,
    }
    mock_sp.next.return_value = None

    with patch("mammamiradio.playlist.get_spotify_client", return_value=mock_sp):
        result = fetch_playlist(config_with_spotify)

    assert len(result) == 2
    assert result[0].title == "Canzone Uno"
    assert result[0].artist == "Artista A"
    assert result[1].title == "Canzone Due"
    mock_sp.playlist_tracks.assert_called_once_with("abc123")


def test_fetches_playlist_paginated(config_with_spotify):
    """Playlist with multiple pages of results."""
    page1 = {
        "items": [_make_spotify_track("Track 1", "Artist 1", "id1")],
        "next": "https://api.spotify.com/v1/next",
    }
    page2 = {
        "items": [_make_spotify_track("Track 2", "Artist 2", "id2")],
        "next": None,
    }

    mock_sp = MagicMock()
    mock_sp.playlist_tracks.return_value = page1
    mock_sp.next.side_effect = [page2, None]

    with patch("mammamiradio.playlist.get_spotify_client", return_value=mock_sp):
        result = fetch_playlist(config_with_spotify)

    assert len(result) == 2
    assert result[0].title == "Track 1"
    assert result[1].title == "Track 2"


def test_fetches_playlist_when_spotify_returns_item_shape(config_with_spotify):
    mock_sp = MagicMock()
    mock_sp.playlist.return_value = {"name": "Roadtrip Italia"}
    mock_sp.playlist_tracks.return_value = {
        "items": [
            _make_spotify_item_track("OSSESSIONE", "Samurai Jay", "track1"),
            _make_spotify_item_track("DAVVERODAVVERO", "Artie 5ive", "track2"),
        ],
        "next": None,
    }
    mock_sp.next.return_value = None

    with patch("mammamiradio.playlist.get_spotify_client", return_value=mock_sp):
        result = fetch_playlist(config_with_spotify)

    assert len(result) == 2
    assert result[0].title == "OSSESSIONE"
    assert result[1].spotify_id == "track2"


# --- Playlist fetch fails → falls back to liked songs ---


def test_playlist_fails_falls_back_to_liked_songs(config_with_spotify):
    mock_sp = MagicMock()
    mock_sp.playlist_tracks.side_effect = Exception("Playlist not found")
    mock_sp.current_user_saved_tracks.return_value = {
        "items": [
            _make_spotify_track("Liked Song", "Liked Artist", "liked1"),
        ],
        "next": None,
    }

    with patch("mammamiradio.playlist.get_spotify_client", return_value=mock_sp):
        result = fetch_playlist(config_with_spotify)

    assert len(result) == 1
    assert result[0].title == "Liked Song"
    mock_sp.current_user_saved_tracks.assert_called()


# --- Liked songs fails → returns demo tracks ---


def test_liked_songs_fails_returns_demo(config_with_spotify):
    mock_sp = MagicMock()
    mock_sp.playlist_tracks.side_effect = Exception("Playlist error")
    mock_sp.current_user_saved_tracks.return_value = {
        "items": [],
        "next": None,
    }

    with patch("mammamiradio.playlist.get_spotify_client", return_value=mock_sp):
        result = fetch_playlist(config_with_spotify)

    assert len(result) == len(DEMO_TRACKS)


# --- Empty playlist from API → returns demo tracks ---


def test_empty_playlist_returns_demo(config_with_spotify):
    mock_sp = MagicMock()
    mock_sp.playlist_tracks.return_value = {
        "items": [],
        "next": None,
    }
    mock_sp.current_user_saved_tracks.return_value = {
        "items": [],
        "next": None,
    }

    with patch("mammamiradio.playlist.get_spotify_client", return_value=mock_sp):
        result = fetch_playlist(config_with_spotify)

    assert len(result) == len(DEMO_TRACKS)


# --- Skips tracks with no ID ---


def test_skips_tracks_without_id(config_with_spotify):
    mock_sp = MagicMock()
    mock_sp.playlist_tracks.return_value = {
        "items": [
            {"track": {"name": "Good", "artists": [{"name": "A"}], "duration_ms": 100, "id": "ok1"}},
            {"track": {"name": "Bad", "artists": [{"name": "B"}], "duration_ms": 100, "id": None}},
            {"track": None},
        ],
        "next": None,
    }
    mock_sp.next.return_value = None

    with patch("mammamiradio.playlist.get_spotify_client", return_value=mock_sp):
        result = fetch_playlist(config_with_spotify)

    assert len(result) == 1
    assert result[0].title == "Good"


def test_load_explicit_playlist_source_success(config_with_spotify):
    mock_sp = MagicMock()
    mock_sp.playlist.return_value = {"name": "Roadtrip Italia"}
    mock_sp.playlist_tracks.return_value = {
        "items": [_make_spotify_track("Canzone Uno", "Artista A", "id1")],
        "next": None,
    }

    with patch("mammamiradio.playlist.get_spotify_client", return_value=mock_sp):
        tracks, source = load_explicit_source(
            config_with_spotify,
            PlaylistSource(kind="playlist", source_id="abc123", label="Roadtrip Italia"),
        )

    assert len(tracks) == 1
    assert source.kind == "playlist"
    assert source.source_id == "abc123"
    assert source.track_count == 1


def test_load_explicit_liked_songs_success(config_with_spotify):
    mock_sp = MagicMock()
    mock_sp.current_user_saved_tracks.return_value = {
        "items": [_make_spotify_track("Liked Song", "Liked Artist", "liked1")],
        "next": None,
    }

    with patch("mammamiradio.playlist.get_spotify_client", return_value=mock_sp):
        tracks, source = load_explicit_source(
            config_with_spotify,
            PlaylistSource(kind="liked_songs", label="Liked Songs"),
        )

    assert len(tracks) == 1
    assert source.kind == "liked_songs"
    assert source.track_count == 1


def test_explicit_source_does_not_fall_back_on_failure(config_with_spotify):
    mock_sp = MagicMock()
    mock_sp.playlist.return_value = {"name": "Private Playlist"}
    mock_sp.playlist_tracks.side_effect = Exception("403")
    mock_sp.current_user_saved_tracks.return_value = {
        "items": [_make_spotify_track("Liked Song", "Liked Artist", "liked1")],
        "next": None,
    }

    with (
        patch("mammamiradio.playlist.get_spotify_client", return_value=mock_sp),
        pytest.raises(Exception, match="Failed to load selected playlist"),
    ):
        load_explicit_source(
            config_with_spotify,
            PlaylistSource(kind="playlist", source_id="abc123", label="Private Playlist"),
        )

    mock_sp.current_user_saved_tracks.assert_not_called()


def test_fetch_startup_playlist_restores_persisted_source(config_with_spotify):
    mock_sp = MagicMock()
    mock_sp.playlist.return_value = {"name": "Roadtrip Italia"}
    mock_sp.playlist_tracks.return_value = {
        "items": [_make_spotify_track("Canzone Uno", "Artista A", "id1")],
        "next": None,
    }
    persisted = PlaylistSource(kind="playlist", source_id="persisted999", label="Roadtrip Italia")

    with patch("mammamiradio.playlist.get_spotify_client", return_value=mock_sp):
        tracks, source, error = fetch_startup_playlist(config_with_spotify, persisted)

    assert len(tracks) == 1
    assert source.kind == "playlist"
    assert source.source_id == "persisted999"
    assert error == ""
    mock_sp.playlist_tracks.assert_called_once_with("persisted999")


def test_read_persisted_source_ignores_invalid_numeric_fields(tmp_path):
    payload = {
        "kind": "playlist",
        "source_id": "abc123",
        "label": "Roadtrip Italia",
        "track_count": "not-a-number",
        "selected_at": 1.0,
    }
    (tmp_path / "playlist_source.json").write_text(json.dumps(payload))

    assert read_persisted_source(tmp_path) is None


def test_list_user_playlists_reads_item_totals_from_current_spotify_shape(config_with_spotify):
    mock_sp = MagicMock()
    mock_sp.current_user_playlists.return_value = {
        "items": [
            {"id": "abc123", "name": "mamma mi radio", "items": {"total": 50}},
            {"id": "def456", "name": "Late Night Drive", "tracks": {"total": 12}},
        ],
        "next": None,
    }
    mock_sp.next.return_value = None

    with patch("mammamiradio.playlist.get_spotify_client", return_value=mock_sp):
        playlists = list_user_playlists(config_with_spotify)

    assert playlists == [
        {"id": "abc123", "label": "mamma mi radio", "track_count": 50},
        {"id": "def456", "label": "Late Night Drive", "track_count": 12},
    ]
