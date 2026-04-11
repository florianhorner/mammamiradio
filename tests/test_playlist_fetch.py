"""Tests for playlist loading behavior."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from mammamiradio.config import load_config
from mammamiradio.models import PlaylistSource, Track
from mammamiradio.playlist import (
    DEMO_TRACKS,
    fetch_playlist,
    fetch_startup_playlist,
    load_explicit_source,
    read_persisted_source,
)


@pytest.fixture()
def config():
    return load_config()


# --- No credentials -> demo tracks ---


def test_no_credentials_returns_demo_tracks(config):
    result = fetch_playlist(config)
    assert len(result) == len(DEMO_TRACKS)
    demo_titles = {t.title for t in DEMO_TRACKS}
    for t in result:
        assert t.title in demo_titles


def test_no_credentials_shuffles_when_configured(config):
    config.playlist.shuffle = True
    # Run multiple times -- at least one ordering should differ (probabilistic but near-certain)
    results = [tuple(t.title for t in fetch_playlist(config)) for _ in range(10)]
    # With 10 tracks shuffled 10 times, extremely unlikely all orderings are identical
    assert len(set(results)) > 1


def test_no_credentials_uses_live_charts_when_ytdlp_enabled(config, monkeypatch):
    chart_tracks = [Track(title="Chart One", artist="Artist One", duration_ms=210000, spotify_id="c1")]
    monkeypatch.setenv("MAMMAMIRADIO_ALLOW_YTDLP", "true")

    with patch("mammamiradio.playlist._fetch_current_italy_charts", return_value=chart_tracks):
        tracks, source, _err = fetch_startup_playlist(config)

    assert len(tracks) == 1
    assert tracks[0].title == "Chart One"
    assert source.kind == "charts"
    assert source.label == "Current Italian charts"


# --- _fetch_current_italy_charts ---


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


def test_fetch_current_italy_charts_per_artist_cap():
    """No artist appears more than max_per_artist times in the result."""
    from mammamiradio.playlist import _fetch_current_italy_charts

    # 5 Shiva tracks + 2 from other artists in the chart
    payload = {
        "feed": {
            "results": [
                {"name": "Shiva Track 1", "artistName": "Shiva", "id": "1"},
                {"name": "Shiva Track 2", "artistName": "Shiva", "id": "2"},
                {"name": "Shiva Track 3", "artistName": "Shiva", "id": "3"},
                {"name": "Shiva Track 4", "artistName": "Shiva", "id": "4"},
                {"name": "Shiva Track 5", "artistName": "Shiva", "id": "5"},
                {"name": "Other Song", "artistName": "Geolier", "id": "6"},
                {"name": "Another Song", "artistName": "Tiziano Ferro", "id": "7"},
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

    shiva_tracks = [t for t in tracks if t.artist == "Shiva"]
    assert len(shiva_tracks) <= 2, f"Expected at most 2 Shiva tracks, got {len(shiva_tracks)}"
    assert len(tracks) == 4  # 2 Shiva + 1 Geolier + 1 Tiziano Ferro


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
    path.write_bytes(b"\x00\x00\x00\x00")  # invalid JSON -> JSONDecodeError

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


# ---------------------------------------------------------------------------
# load_explicit_source -- demo kind
# ---------------------------------------------------------------------------


def test_load_explicit_demo_source(config):
    """demo kind returns DEMO_TRACKS without any external call."""
    tracks, source = load_explicit_source(
        config,
        PlaylistSource(kind="demo", source_id="", label="Demo"),
    )
    assert len(tracks) == len(DEMO_TRACKS)
    assert source.kind == "demo"


def test_load_explicit_charts_source_success(config):
    chart_tracks = [Track(title="Chart Three", artist="Artist Three", duration_ms=210000, spotify_id="c3")]
    with patch("mammamiradio.playlist._fetch_current_italy_charts", return_value=chart_tracks):
        tracks, source = load_explicit_source(
            config,
            PlaylistSource(kind="charts", source_id="apple_music_it_top_50", label="Current Italian charts"),
        )

    assert len(tracks) == 1
    assert tracks[0].title == "Chart Three"
    assert source.kind == "charts"
    assert source.label == "Current Italian charts"


def test_load_explicit_charts_source_raises_when_unavailable(config):
    with (
        patch("mammamiradio.playlist._fetch_current_italy_charts", return_value=[]),
        pytest.raises(Exception, match="temporarily unavailable"),
    ):
        load_explicit_source(
            config,
            PlaylistSource(kind="charts", source_id="apple_music_it_top_50", label="Current Italian charts"),
        )


def test_load_explicit_source_unsupported_kind_raises(config):
    """Unsupported source kind raises ExplicitSourceError."""
    with pytest.raises(Exception, match="Unsupported source kind"):
        load_explicit_source(
            config,
            PlaylistSource(kind="unsupported_kind", source_id="", label="Bad"),
        )
