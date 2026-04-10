"""Tests for playlist loading behavior."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from mammamiradio.config import load_config
from mammamiradio.models import PlaylistSource, Track
from mammamiradio.playlist import (
    DEMO_TRACKS,
    _track_from_spotify_item,
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
    cfg.playlist.shuffle = False
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


def test_no_credentials_uses_live_charts_when_ytdlp_enabled(config, monkeypatch):
    config.spotify_client_id = ""
    config.spotify_client_secret = ""
    chart_tracks = [Track(title="Chart One", artist="Artist One", duration_ms=210000, spotify_id="c1")]
    monkeypatch.setenv("MAMMAMIRADIO_ALLOW_YTDLP", "true")

    with patch("mammamiradio.playlist._fetch_current_italy_charts", return_value=chart_tracks):
        tracks, source, err = fetch_startup_playlist(config)

    assert len(tracks) == 1
    assert tracks[0].title == "Chart One"
    assert source.kind == "charts"
    assert source.label == "Current Italian charts"
    assert "Spotify credentials are missing" in err


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


def test_liked_fallback_uses_live_charts_when_enabled(config_with_spotify, monkeypatch):
    mock_sp = MagicMock()
    mock_sp.playlist_tracks.side_effect = Exception("Playlist error")
    mock_sp.current_user_saved_tracks.return_value = {"items": [], "next": None}
    chart_tracks = [Track(title="Chart Two", artist="Artist Two", duration_ms=210000, spotify_id="c2")]
    monkeypatch.setenv("MAMMAMIRADIO_ALLOW_YTDLP", "true")

    with (
        patch("mammamiradio.playlist.get_spotify_client", return_value=mock_sp),
        patch("mammamiradio.playlist._fetch_current_italy_charts", return_value=chart_tracks),
    ):
        tracks, source, error = fetch_startup_playlist(config_with_spotify)

    assert len(tracks) == 1
    assert tracks[0].title == "Chart Two"
    assert source.kind == "charts"
    assert error


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


def test_load_explicit_charts_source_success(config_with_spotify):
    chart_tracks = [Track(title="Chart Three", artist="Artist Three", duration_ms=210000, spotify_id="c3")]
    with patch("mammamiradio.playlist._fetch_current_italy_charts", return_value=chart_tracks):
        tracks, source = load_explicit_source(
            config_with_spotify,
            PlaylistSource(kind="charts", source_id="apple_music_it_top_50", label="Current Italian charts"),
        )

    assert len(tracks) == 1
    assert tracks[0].title == "Chart Three"
    assert source.kind == "charts"
    assert source.label == "Current Italian charts"


def test_load_explicit_charts_source_raises_when_unavailable(config_with_spotify):
    with (
        patch("mammamiradio.playlist._fetch_current_italy_charts", return_value=[]),
        pytest.raises(Exception, match="temporarily unavailable"),
    ):
        load_explicit_source(
            config_with_spotify,
            PlaylistSource(kind="charts", source_id="apple_music_it_top_50", label="Current Italian charts"),
        )


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


# --- _track_from_spotify_item tests ---


class TestTrackFromSpotifyItem:
    """Unit tests for the _track_from_spotify_item helper."""

    def test_track_from_spotify_item_extracts_album(self):
        item = {
            "track": {
                "id": "abc",
                "name": "Song",
                "duration_ms": 200000,
                "artists": [{"name": "Artist"}],
                "album": {"name": "Great Album"},
                "explicit": False,
                "popularity": 50,
            }
        }
        track = _track_from_spotify_item(item)
        assert track is not None
        assert track.album == "Great Album"

    def test_track_from_spotify_item_extracts_explicit(self):
        item = {
            "track": {
                "id": "abc",
                "name": "Song",
                "duration_ms": 200000,
                "artists": [{"name": "Artist"}],
                "album": {"name": "Album"},
                "explicit": True,
                "popularity": 50,
            }
        }
        track = _track_from_spotify_item(item)
        assert track is not None
        assert track.explicit is True

    def test_track_from_spotify_item_explicit_missing_defaults_false(self):
        item = {
            "track": {
                "id": "abc",
                "name": "Song",
                "duration_ms": 200000,
                "artists": [{"name": "Artist"}],
                "album": {"name": "Album"},
            }
        }
        track = _track_from_spotify_item(item)
        assert track is not None
        assert track.explicit is False

    def test_track_from_spotify_item_extracts_popularity(self):
        item = {
            "track": {
                "id": "abc",
                "name": "Song",
                "duration_ms": 200000,
                "artists": [{"name": "Artist"}],
                "album": {"name": "Album"},
                "explicit": False,
                "popularity": 75,
            }
        }
        track = _track_from_spotify_item(item)
        assert track is not None
        assert track.popularity == 75

    def test_track_from_spotify_item_popularity_missing_defaults_zero(self):
        item = {
            "track": {
                "id": "abc",
                "name": "Song",
                "duration_ms": 200000,
                "artists": [{"name": "Artist"}],
            }
        }
        track = _track_from_spotify_item(item)
        assert track is not None
        assert track.popularity == 0

    def test_track_from_spotify_item_album_missing(self):
        item = {
            "track": {
                "id": "abc",
                "name": "Song",
                "duration_ms": 200000,
                "artists": [{"name": "Artist"}],
            }
        }
        track = _track_from_spotify_item(item)
        assert track is not None
        assert track.album == ""

    def test_track_from_spotify_item_returns_none_for_none(self):
        assert _track_from_spotify_item(None) is None

    def test_track_from_spotify_item_returns_none_for_no_id(self):
        item = {
            "track": {
                "name": "Song",
                "duration_ms": 200000,
                "artists": [{"name": "Artist"}],
            }
        }
        assert _track_from_spotify_item(item) is None


# ---------------------------------------------------------------------------
# _fetch_current_italy_charts
# ---------------------------------------------------------------------------


def test_fetch_current_italy_charts_success():
    """Parses tracks from Apple Music charts RSS response."""
    from mammamiradio.playlist import _fetch_current_italy_charts

    payload = {
        "feed": {
            "results": [
                {"name": "Song One", "artistName": "Artist A", "id": "1"},
                {"name": "Song Two", "artistName": "Artist B", "id": "2"},
                {"name": "", "artistName": "Artist C", "id": "3"},  # skipped: no title
            ]
        }
    }

    with patch("mammamiradio.playlist.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(payload).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tracks = _fetch_current_italy_charts()

    assert len(tracks) == 2
    assert tracks[0].title == "Song One"
    assert tracks[0].spotify_id == "chart_1"
    assert tracks[1].title == "Song Two"
    assert tracks[1].spotify_id == "chart_2"


def test_fetch_current_italy_charts_network_error():
    """Returns empty list on network failure."""
    from urllib.error import URLError

    from mammamiradio.playlist import _fetch_current_italy_charts

    with patch("mammamiradio.playlist.urlopen", side_effect=URLError("network down")):
        tracks = _fetch_current_italy_charts()

    assert tracks == []


def test_fetch_current_italy_charts_invalid_json():
    """Returns empty list on JSON decode error."""
    from mammamiradio.playlist import _fetch_current_italy_charts

    with patch("mammamiradio.playlist.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = b"not json at all"
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tracks = _fetch_current_italy_charts()

    assert tracks == []


# ---------------------------------------------------------------------------
# read_persisted_source / write_persisted_source
# ---------------------------------------------------------------------------


def test_read_persisted_source_os_error(tmp_path):
    """Returns None when file is unreadable due to OSError."""
    from mammamiradio.playlist import PERSISTED_SOURCE_FILENAME, read_persisted_source

    path = tmp_path / PERSISTED_SOURCE_FILENAME
    # Write a file that raises OSError on read (simulate with invalid JSON)
    path.write_bytes(b"\x00\x00\x00\x00")  # invalid JSON → JSONDecodeError

    result = read_persisted_source(tmp_path)
    assert result is None


def test_read_persisted_source_missing_kind(tmp_path):
    """Returns None when 'kind' is missing from persisted data."""
    from mammamiradio.playlist import PERSISTED_SOURCE_FILENAME, read_persisted_source

    path = tmp_path / PERSISTED_SOURCE_FILENAME
    path.write_text(json.dumps({"source_id": "abc", "label": "Test"}))

    result = read_persisted_source(tmp_path)
    assert result is None


def test_write_persisted_source_roundtrip(tmp_path):
    """write_persisted_source creates a file that read_persisted_source can load."""
    from mammamiradio.playlist import read_persisted_source, write_persisted_source

    source = PlaylistSource(
        kind="playlist",
        source_id="test123",
        url="https://open.spotify.com/playlist/test123",
        label="Test Playlist",
        track_count=42,
        selected_at=1234567890.0,
    )
    write_persisted_source(tmp_path, source)

    result = read_persisted_source(tmp_path)
    assert result is not None
    assert result.kind == "playlist"
    assert result.source_id == "test123"
    assert result.track_count == 42


# ---------------------------------------------------------------------------
# load_explicit_source — demo kind
# ---------------------------------------------------------------------------


def test_load_explicit_demo_source(config):
    """demo kind returns DEMO_TRACKS without any Spotify call."""
    tracks, source = load_explicit_source(
        config,
        PlaylistSource(kind="demo", source_id="", label="Demo"),
    )
    assert len(tracks) == len(DEMO_TRACKS)
    assert source.kind == "demo"


def test_load_explicit_source_invalid_url_raises(config_with_spotify):
    """url kind raises ExplicitSourceError for invalid Spotify URLs."""
    with pytest.raises(Exception, match="not a valid"):
        load_explicit_source(
            config_with_spotify,
            PlaylistSource(kind="url", url="https://not-spotify.com/something", label="Bad URL"),
        )


def test_load_explicit_source_unsupported_kind_raises(config_with_spotify):
    """Unsupported source kind raises ExplicitSourceError."""
    with pytest.raises(Exception, match="Unsupported source kind"):
        load_explicit_source(
            config_with_spotify,
            PlaylistSource(kind="unsupported_kind", source_id="", label="Bad"),
        )


def test_load_explicit_source_playlist_missing_id_raises(config_with_spotify):
    """playlist kind raises ExplicitSourceError when source_id is missing."""
    with pytest.raises(Exception, match="source_id is required"):
        load_explicit_source(
            config_with_spotify,
            PlaylistSource(kind="playlist", source_id="", label="Empty"),
        )


# ---------------------------------------------------------------------------
# fetch_startup_playlist — failed persisted source restore
# ---------------------------------------------------------------------------


def test_fetch_startup_playlist_failed_restore_falls_back(config_with_spotify):
    """When persisted source restore fails, startup falls back to Spotify."""
    persisted = PlaylistSource(kind="playlist", source_id="gone123", label="Deleted Playlist")
    mock_sp = MagicMock()
    # Persisted playlist raises → fallback to liked songs
    mock_sp.playlist.side_effect = Exception("not found")
    mock_sp.current_user_saved_tracks.return_value = {
        "items": [_make_spotify_track("Liked Song", "Liked Artist", "liked1")],
        "next": None,
    }

    with patch("mammamiradio.playlist.get_spotify_client", return_value=mock_sp):
        tracks, _source, error = fetch_startup_playlist(config_with_spotify, persisted)

    assert len(tracks) == 1
    assert tracks[0].title == "Liked Song"
    assert "not found" in error or error != ""


# ---------------------------------------------------------------------------
# list_user_playlists — empty next page
# ---------------------------------------------------------------------------


def test_list_user_playlists_stops_when_no_next(config_with_spotify):
    """list_user_playlists stops pagination when next is None."""
    mock_sp = MagicMock()
    mock_sp.current_user_playlists.return_value = {
        "items": [{"id": "abc", "name": "Playlist A", "tracks": {"total": 10}}],
        "next": None,
    }
    mock_sp.next.return_value = None

    with patch("mammamiradio.playlist.get_spotify_client", return_value=mock_sp):
        playlists = list_user_playlists(config_with_spotify)

    assert len(playlists) == 1


def test_list_user_playlists_skips_none_items(config_with_spotify):
    """list_user_playlists skips items without an id."""
    mock_sp = MagicMock()
    mock_sp.current_user_playlists.return_value = {
        "items": [
            None,  # invalid
            {"id": "", "name": "No ID", "tracks": {"total": 5}},  # no id
            {"id": "valid_id", "name": "Valid Playlist", "tracks": {"total": 8}},
        ],
        "next": None,
    }
    mock_sp.next.return_value = None

    with patch("mammamiradio.playlist.get_spotify_client", return_value=mock_sp):
        playlists = list_user_playlists(config_with_spotify)

    assert len(playlists) == 1
    assert playlists[0]["id"] == "valid_id"
