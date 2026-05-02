"""Tests for playlist loading behavior."""

from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import PlaylistSource, Track
from mammamiradio.playlist.playlist import (
    DEMO_TRACKS,
    fetch_chart_refresh,
    fetch_startup_playlist,
    load_explicit_source,
    read_persisted_source,
)


class _BytesResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@pytest.fixture()
def config():
    return load_config()


# --- No credentials -> demo tracks ---


def test_no_credentials_returns_demo_tracks(config, monkeypatch):
    # Ensure yt-dlp is disabled, music/ is empty, demo_assets/music/ is empty
    # so we get DEMO_TRACKS
    monkeypatch.delenv("MAMMAMIRADIO_ALLOW_YTDLP", raising=False)
    config.allow_ytdlp = False
    with (
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=[]),
        patch("mammamiradio.playlist.playlist._load_demo_asset_tracks", return_value=[]),
    ):
        tracks, _, _ = fetch_startup_playlist(config)
    assert len(tracks) == len(DEMO_TRACKS)
    demo_titles = {t.title for t in DEMO_TRACKS}
    for t in tracks:
        assert t.title in demo_titles


def test_no_credentials_shuffles_when_configured(config):
    config.playlist.shuffle = True
    # Run multiple times -- at least one ordering should differ (probabilistic but near-certain)
    results = [tuple(t.title for t in fetch_startup_playlist(config)[0]) for _ in range(10)]
    # With 10 tracks shuffled 10 times, extremely unlikely all orderings are identical
    assert len(set(results)) > 1


def test_no_credentials_uses_live_charts_when_ytdlp_enabled(config, monkeypatch):
    chart_tracks = [Track(title="Chart One", artist="Artist One", duration_ms=210000, spotify_id="c1")]
    config.allow_ytdlp = True

    with patch("mammamiradio.playlist.playlist._fetch_current_italy_charts", return_value=chart_tracks):
        tracks, source, _err = fetch_startup_playlist(config)

    assert len(tracks) == 1
    assert tracks[0].title == "Chart One"
    assert source.kind == "charts"
    assert source.label == "Current Italian charts"


# --- _fetch_current_italy_charts ---


def test_fetch_current_italy_charts_success():
    """Parses tracks from Apple Music charts RSS response."""
    from mammamiradio.playlist.playlist import _fetch_current_italy_charts

    payload = {
        "feed": {
            "results": [
                {"name": "Song One", "artistName": "Artist A", "id": "1"},
                {"name": "Song Two", "artistName": "Artist B", "id": "2"},
                {"name": "", "artistName": "Artist C", "id": "3"},  # skipped: no title
            ]
        }
    }

    with patch("mammamiradio.playlist.playlist.urlopen") as mock_urlopen:
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
    from mammamiradio.playlist.playlist import _fetch_current_italy_charts

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

    with patch("mammamiradio.playlist.playlist.urlopen") as mock_urlopen:
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

    from mammamiradio.playlist.playlist import _fetch_current_italy_charts

    with patch("mammamiradio.playlist.playlist.urlopen", side_effect=URLError("network down")):
        tracks = _fetch_current_italy_charts()

    assert tracks == []


def test_fetch_current_italy_charts_invalid_json():
    """Returns empty list on JSON decode error."""
    from mammamiradio.playlist.playlist import _fetch_current_italy_charts

    with patch("mammamiradio.playlist.playlist.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = b"not json at all"
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tracks = _fetch_current_italy_charts()

    assert tracks == []


def test_fetch_jamendo_playlist_success(config):
    from mammamiradio.playlist.playlist import _fetch_jamendo_playlist

    config.playlist.jamendo_client_id = "Jamendo123"
    config.playlist.jamendo_tags = "pop"
    payload = {
        "results": [
            {
                "id": "42",
                "name": "Canzone Libera",
                "artist_name": "Artista Aperto",
                "duration": 183,
                "audiodownload": "https://cdn.example.test/jamendo-42.mp3",
                "album_name": "Estate",
                "image": "https://cdn.example.test/jamendo-42.jpg",
            }
        ]
    }

    with patch("mammamiradio.playlist.playlist.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(payload).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tracks = _fetch_jamendo_playlist(config)

    assert len(tracks) == 1
    assert tracks[0].title == "Canzone Libera"
    assert tracks[0].artist == "Artista Aperto"
    assert tracks[0].spotify_id == "jamendo_42"
    assert tracks[0].youtube_id == ""
    assert tracks[0].direct_url == "https://cdn.example.test/jamendo-42.mp3"


def test_fetch_jamendo_playlist_url_includes_required_cc_params(config):
    from mammamiradio.playlist.playlist import _fetch_jamendo_playlist

    config.playlist.jamendo_client_id = "Jamendo123"
    config.playlist.jamendo_tags = "indie pop"
    with patch("mammamiradio.playlist.playlist.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"results": []}).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        _fetch_jamendo_playlist(config)

    called_url = mock_urlopen.call_args.args[0]
    assert "cc_commercial=1" in called_url
    assert "cc_sharealike=0" in called_url


def test_fetch_current_italy_charts_filters_non_music_entries():
    """WS5: BBC comedy / podcast / audiobook entries must be dropped at ingest.

    Apple's Italian chart has surfaced entries like
    'BBC Studios - Do You Speak English? - Big Train - BBC comedy' which play
    as dead-eye audio and break the radio illusion harder than anything else.
    """
    from mammamiradio.playlist.playlist import _fetch_current_italy_charts

    payload = {
        "feed": {
            "results": [
                {"name": "Real Song", "artistName": "Real Artist", "id": "1"},
                {
                    "name": "Do You Speak English? - Big Train",
                    "artistName": "BBC Studios",
                    "id": "2",
                },
                {
                    "name": "Morning News Briefing",
                    "artistName": "Some Outlet",
                    "id": "3",
                },
                {
                    "name": "Harry Potter Audiobook Ch. 1",
                    "artistName": "Narrator X",
                    "id": "4",
                },
                {"name": "Another Real Song", "artistName": "Other Artist", "id": "5"},
            ]
        }
    }

    with patch("mammamiradio.playlist.playlist.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(payload).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tracks = _fetch_current_italy_charts()

    titles = {t.title for t in tracks}
    assert titles == {"Real Song", "Another Real Song"}, f"non-music markers must be filtered; got {titles}"


def test_is_plausible_music_title_keeps_legitimate_songs():
    """Filter must never drop canonical Italian song titles."""
    from mammamiradio.playlist.playlist import _is_plausible_music_title

    legitimate = [
        ("Volare", "Domenico Modugno"),
        ("Bella Ciao", "Partisan Choir"),
        ("L'Italiano", "Toto Cutugno"),
        ("Caruso", "Lucio Dalla"),
        ("News For You", "Some Band"),
    ]
    for title, artist in legitimate:
        assert _is_plausible_music_title(title, artist), f"{title!r} by {artist!r} incorrectly rejected"


def test_is_plausible_music_title_rejects_empty_inputs():
    """Empty title or artist fails the plausibility check."""
    from mammamiradio.playlist.playlist import _is_plausible_music_title

    assert not _is_plausible_music_title("", "Artist")
    assert not _is_plausible_music_title("Title", "")
    assert not _is_plausible_music_title("", "")


def test_is_plausible_music_title_rejects_oversize_strings():
    """Titles or artist names wildly longer than any real song are rejected."""
    from mammamiradio.playlist.playlist import _is_plausible_music_title

    assert not _is_plausible_music_title("x" * 200, "Artist")
    assert not _is_plausible_music_title("Title", "x" * 120)


# ---------------------------------------------------------------------------
# read_persisted_source / write_persisted_source
# ---------------------------------------------------------------------------


def test_read_persisted_source_os_error(tmp_path):
    """Returns None when file is unreadable due to OSError."""
    from mammamiradio.playlist.playlist import PERSISTED_SOURCE_FILENAME, read_persisted_source

    path = tmp_path / PERSISTED_SOURCE_FILENAME
    # Write a file that raises OSError on read (simulate with invalid JSON)
    path.write_bytes(b"\x00\x00\x00\x00")  # invalid JSON -> JSONDecodeError

    result = read_persisted_source(tmp_path)
    assert result is None


def test_read_persisted_source_missing_kind(tmp_path):
    """Returns None when 'kind' is missing from persisted data."""
    from mammamiradio.playlist.playlist import PERSISTED_SOURCE_FILENAME, read_persisted_source

    path = tmp_path / PERSISTED_SOURCE_FILENAME
    path.write_text(json.dumps({"source_id": "abc", "label": "Test"}))

    result = read_persisted_source(tmp_path)
    assert result is None


def test_write_persisted_source_roundtrip(tmp_path):
    """write_persisted_source creates a file that read_persisted_source can load."""
    from mammamiradio.playlist.playlist import read_persisted_source, write_persisted_source

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
    """demo kind returns DEMO_TRACKS when demo_assets/music/ is empty."""
    with patch("mammamiradio.playlist.playlist._load_demo_asset_tracks", return_value=[]):
        tracks, source = load_explicit_source(
            config,
            PlaylistSource(kind="demo", source_id="", label="Demo"),
        )
    assert len(tracks) == len(DEMO_TRACKS)
    assert source.kind == "demo"


def test_load_explicit_demo_source_prefers_demo_assets(config, tmp_path):
    """demo kind must prefer bundled MP3s over the static DEMO_TRACKS placeholder list."""
    asset_mp3 = tmp_path / "Pino Daniele - Napule E.mp3"
    asset_mp3.write_bytes(b"fake")
    with patch("mammamiradio.playlist.playlist._DEMO_ASSETS_MUSIC_DIR", tmp_path):
        tracks, source = load_explicit_source(
            config,
            PlaylistSource(kind="demo", source_id="", label="Demo"),
        )
    assert len(tracks) == 1
    assert tracks[0].title == "Napule E"
    assert tracks[0].artist == "Pino Daniele"
    assert source.kind == "demo"


def test_load_explicit_charts_source_success(config):
    chart_tracks = [Track(title="Chart Three", artist="Artist Three", duration_ms=210000, spotify_id="c3")]
    with (
        patch("mammamiradio.playlist.playlist._fetch_current_italy_charts", return_value=chart_tracks),
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=[]),
    ):
        tracks, source = load_explicit_source(
            config,
            PlaylistSource(kind="charts", source_id="apple_music_it_top_50", label="Current Italian charts"),
        )

    assert len(tracks) == 1
    assert tracks[0].title == "Chart Three"
    assert source.kind == "charts"
    assert source.label == "Current Italian charts"


def test_load_explicit_jamendo_source_success(config):
    config.playlist.jamendo_client_id = "Jamendo123"
    config.playlist.shuffle = False
    jamendo_tracks = [
        Track(
            title="Libera",
            artist="CC Band",
            duration_ms=180000,
            spotify_id="jamendo_7",
            youtube_id="",
            direct_url="https://cdn.example.test/jamendo-7.mp3",
            source="jamendo",
        )
    ]

    with patch("mammamiradio.playlist.playlist._fetch_jamendo_playlist", return_value=jamendo_tracks):
        tracks, source = load_explicit_source(
            config,
            PlaylistSource(kind="jamendo", source_id="pop", label="Jamendo CC Music"),
        )

    assert tracks == jamendo_tracks
    assert source.kind == "jamendo"
    assert source.source_id == "pop"
    assert source.url == "jamendo://playlist?tags=pop"


def test_load_explicit_charts_source_raises_when_unavailable(config):
    with (
        patch("mammamiradio.playlist.playlist._fetch_current_italy_charts", return_value=[]),
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=[]),
        pytest.raises(Exception, match="temporarily unavailable"),
    ):
        load_explicit_source(
            config,
            PlaylistSource(kind="charts", source_id="apple_music_it_top_50", label="Current Italian charts"),
        )


def test_load_explicit_charts_blends_local_tracks_and_dedupes_by_artist_title(config):
    chart_tracks = [
        Track(title="Emozioni", artist="Lucio Battisti", duration_ms=210000, spotify_id="chart_1"),
        Track(title="Chart Three", artist="Artist Three", duration_ms=210000, spotify_id="c3"),
    ]
    local_tracks = [
        Track(
            title="Emozioni",
            artist="lucio battisti",
            duration_ms=210000,
            spotify_id="local_battisti_emozioni",
        ),
        Track(
            title="Grande Grande Grande",
            artist="Mina",
            duration_ms=210000,
            spotify_id="local_mina_grande",
        ),
    ]

    with (
        patch("mammamiradio.playlist.playlist._fetch_current_italy_charts", return_value=chart_tracks),
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=local_tracks),
    ):
        tracks, source = load_explicit_source(
            config,
            PlaylistSource(kind="charts", source_id="apple_music_it_top_50", label="Current Italian charts"),
        )

    assert source.kind == "charts"
    by_key = {(t.artist.strip().lower(), t.title.strip().lower()): t for t in tracks}
    assert ("lucio battisti", "emozioni") in by_key
    assert ("artist three", "chart three") in by_key
    assert ("mina", "grande grande grande") in by_key
    assert len(tracks) == 3


def test_load_explicit_source_unsupported_kind_raises(config):
    """Unsupported source kind raises ExplicitSourceError."""
    with pytest.raises(Exception, match="Unsupported source kind"):
        load_explicit_source(
            config,
            PlaylistSource(kind="unsupported_kind", source_id="", label="Bad"),
        )


# ---------------------------------------------------------------------------
# fetch_chart_refresh
# ---------------------------------------------------------------------------


def test_fetch_chart_refresh_filters_existing():
    """Tracks already in the playlist are excluded from the refresh."""
    tracks = [
        Track(title="A", artist="X", spotify_id="id_a", duration_ms=210000),
        Track(title="B", artist="Y", spotify_id="id_b", duration_ms=210000),
        Track(title="C", artist="Z", spotify_id="id_c", duration_ms=210000),
    ]
    with patch("mammamiradio.playlist.playlist._fetch_current_italy_charts", return_value=tracks):
        result = fetch_chart_refresh({"id_a", "id_c"})
    assert len(result) == 1
    assert result[0].spotify_id == "id_b"


def test_fetch_chart_refresh_returns_empty_on_failure():
    """When the chart fetch fails, an empty list is returned."""
    with patch("mammamiradio.playlist.playlist._fetch_current_italy_charts", return_value=[]):
        result = fetch_chart_refresh(set())
    assert result == []


def test_fetch_chart_refresh_returns_all_when_no_overlap():
    """When none of the chart tracks are in the existing set, all are returned."""
    tracks = [
        Track(title="A", artist="X", spotify_id="id_a", duration_ms=210000),
        Track(title="B", artist="Y", spotify_id="id_b", duration_ms=210000),
    ]
    with patch("mammamiradio.playlist.playlist._fetch_current_italy_charts", return_value=tracks):
        result = fetch_chart_refresh({"id_z"})
    assert len(result) == 2


# ---------------------------------------------------------------------------
# fetch_startup_playlist -- local music/ blending
# ---------------------------------------------------------------------------


def test_local_music_merged_into_chart_playlist(config, monkeypatch, tmp_path):
    """Local music/ files are appended to chart tracks when both exist."""
    from mammamiradio.playlist.playlist import _load_local_music_tracks

    # Create two fake MP3 stubs
    (tmp_path / "Lucio Battisti - Emozioni.mp3").write_bytes(b"")
    (tmp_path / "Mina - Grande Grande Grande.mp3").write_bytes(b"")

    chart_tracks = [Track(title="Chart Hit", artist="Pop Star", duration_ms=210000, spotify_id="c_hit")]
    config.allow_ytdlp = True

    with (
        patch("mammamiradio.playlist.playlist._fetch_current_italy_charts", return_value=chart_tracks),
        patch(
            "mammamiradio.playlist.playlist._load_local_music_tracks", return_value=_load_local_music_tracks(tmp_path)
        ),
    ):
        tracks, source, _err = fetch_startup_playlist(config)

    titles = {t.title for t in tracks}
    assert "Chart Hit" in titles
    assert "Emozioni" in titles
    assert "Grande Grande Grande" in titles
    assert len(tracks) == 3
    assert source.kind == "charts"
    assert source.track_count == 3


def test_local_music_skipped_when_dir_missing(config, monkeypatch):
    """When music/ dir does not exist, chart-only playlist is returned without error."""
    chart_tracks = [Track(title="Solo Chart", artist="Solo Artist", duration_ms=210000, spotify_id="c_solo")]
    config.allow_ytdlp = True

    with (
        patch("mammamiradio.playlist.playlist._fetch_current_italy_charts", return_value=chart_tracks),
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=[]),
    ):
        tracks, _source, _err = fetch_startup_playlist(config)

    assert len(tracks) == 1
    assert tracks[0].title == "Solo Chart"


def test_load_local_music_tracks_parses_artist_title(tmp_path):
    """Artist and title are split on ' - ' delimiter in filename."""
    from mammamiradio.playlist.playlist import _load_local_music_tracks

    (tmp_path / "Lucio Battisti - Emozioni.mp3").write_bytes(b"")
    (tmp_path / "NoHyphen.mp3").write_bytes(b"")

    tracks = _load_local_music_tracks(tmp_path)
    by_title = {t.title: t for t in tracks}

    assert "Emozioni" in by_title
    assert by_title["Emozioni"].artist == "Lucio Battisti"
    assert by_title["Emozioni"].spotify_id.startswith("local_")

    assert "NoHyphen" in by_title
    assert by_title["NoHyphen"].artist == "Unknown"


def test_load_local_music_tracks_missing_dir(tmp_path):
    """Returns empty list when the directory does not exist."""
    from mammamiradio.playlist.playlist import _load_local_music_tracks

    result = _load_local_music_tracks(tmp_path / "nonexistent")
    assert result == []


def test_local_music_deduplicates_against_chart_artist_title(config, monkeypatch, tmp_path):
    """A local file with same artist+title as a chart track is not double-added."""
    from mammamiradio.playlist.playlist import _load_local_music_tracks

    # Same logical song as chart track, but local spotify_id format differs
    (tmp_path / "Battisti - Emozioni.mp3").write_bytes(b"")
    (tmp_path / "Mina - Grande Grande Grande.mp3").write_bytes(b"")

    chart_tracks = [Track(title="Emozioni", artist="Battisti", duration_ms=210000, spotify_id="chart_77")]
    config.allow_ytdlp = True

    local = _load_local_music_tracks(tmp_path)
    assert len(local) == 2

    with (
        patch("mammamiradio.playlist.playlist._fetch_current_italy_charts", return_value=chart_tracks),
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=local),
    ):
        tracks, _source, _err = fetch_startup_playlist(config)

    normalized_keys = {(t.artist.strip().lower(), t.title.strip().lower()) for t in tracks}
    assert ("battisti", "emozioni") in normalized_keys
    assert ("mina", "grande grande grande") in normalized_keys
    assert len(tracks) == 2


# ---------------------------------------------------------------------------
# _load_demo_asset_tracks
# ---------------------------------------------------------------------------


def test_load_demo_asset_tracks_empty_when_dir_missing(tmp_path):
    """Returns empty list when demo_assets/music/ does not exist."""
    from mammamiradio.playlist.playlist import _load_demo_asset_tracks

    with patch("mammamiradio.playlist.playlist._DEMO_ASSETS_MUSIC_DIR", tmp_path / "nonexistent"):
        tracks = _load_demo_asset_tracks()
    assert tracks == []


def test_load_demo_asset_tracks_parses_artist_title(tmp_path):
    """Parses Artist - Title.mp3 filenames into Track objects."""
    from mammamiradio.playlist.playlist import _load_demo_asset_tracks

    (tmp_path / "Pino Daniele - Napule E.mp3").write_bytes(b"")
    (tmp_path / "NoHyphen.mp3").write_bytes(b"")

    with patch("mammamiradio.playlist.playlist._DEMO_ASSETS_MUSIC_DIR", tmp_path):
        tracks = _load_demo_asset_tracks()

    by_title = {t.title: t for t in tracks}
    assert "Napule E" in by_title
    assert by_title["Napule E"].artist == "Pino Daniele"
    assert by_title["Napule E"].spotify_id.startswith("demo_asset_")

    assert "NoHyphen" in by_title
    assert by_title["NoHyphen"].artist == "Unknown"


def test_load_demo_asset_tracks_empty_dir(tmp_path):
    """Returns empty list when directory exists but has no MP3s."""
    from mammamiradio.playlist.playlist import _load_demo_asset_tracks

    with patch("mammamiradio.playlist.playlist._DEMO_ASSETS_MUSIC_DIR", tmp_path):
        tracks = _load_demo_asset_tracks()
    assert tracks == []


# ---------------------------------------------------------------------------
# fetch_startup_playlist — demo asset preference
# ---------------------------------------------------------------------------


def test_fetch_startup_prefers_demo_assets_over_demo_tracks_list(config, tmp_path):
    """When demo_assets/music/ has MP3s and local music/ is empty, use the
    bundled demo assets instead of metadata-only DEMO_TRACKS."""
    (tmp_path / "Pino Daniele - Napule E.mp3").write_bytes(b"")
    (tmp_path / "Lucio Battisti - Emozioni.mp3").write_bytes(b"")
    config.allow_ytdlp = False

    with (
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=[]),
        patch("mammamiradio.playlist.playlist._DEMO_ASSETS_MUSIC_DIR", tmp_path),
    ):
        tracks, source, _err = fetch_startup_playlist(config)

    assert len(tracks) == 2
    titles = {t.title for t in tracks}
    assert "Napule E" in titles
    assert "Emozioni" in titles
    assert source.kind == "demo"
    # Tracks come from actual files, not the metadata placeholder list
    for t in tracks:
        assert t.spotify_id.startswith("demo_asset_")


def test_fetch_startup_falls_back_to_demo_tracks_when_demo_assets_empty(config, tmp_path):
    """When local music/ is empty and demo_assets/music/ exists but is empty,
    fall back to DEMO_TRACKS."""
    config.allow_ytdlp = False

    with (
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=[]),
        patch("mammamiradio.playlist.playlist._DEMO_ASSETS_MUSIC_DIR", tmp_path),
    ):
        tracks, source, _err = fetch_startup_playlist(config)

    assert len(tracks) == len(DEMO_TRACKS)
    demo_titles = {t.title for t in DEMO_TRACKS}
    for t in tracks:
        assert t.title in demo_titles
    assert source.kind == "demo"


def test_fetch_startup_without_jamendo_client_id_skips_jamendo_and_returns_demo(config):
    config.allow_ytdlp = False
    config.playlist.jamendo_client_id = ""

    with (
        patch(
            "mammamiradio.playlist.playlist._fetch_jamendo_playlist",
            side_effect=AssertionError("jamendo should not run"),
        ),
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=[]),
        patch("mammamiradio.playlist.playlist._load_demo_asset_tracks", return_value=[]),
    ):
        tracks, source, _err = fetch_startup_playlist(config)

    assert len(tracks) == len(DEMO_TRACKS)
    assert source.kind == "demo"


def test_fetch_startup_jamendo_zero_tracks_falls_back_to_demo(config):
    config.allow_ytdlp = False
    config.playlist.jamendo_client_id = "Jamendo123"

    with (
        patch("mammamiradio.playlist.playlist._fetch_jamendo_playlist", return_value=[]),
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=[]),
        patch("mammamiradio.playlist.playlist._load_demo_asset_tracks", return_value=[]),
    ):
        tracks, source, _err = fetch_startup_playlist(config)

    assert len(tracks) == len(DEMO_TRACKS)
    assert source.kind == "demo"


def test_fetch_startup_uses_local_music_when_ytdlp_off_and_no_jamendo(config):
    """Operator-honesty: local music/ MP3s are loaded as the startup source when
    yt-dlp is disabled and Jamendo isn't configured. Local files don't need
    yt-dlp; the previous behavior warn-and-skipped them in this configuration,
    silently ignoring the operator's actual MP3s."""
    config.allow_ytdlp = False
    config.playlist.jamendo_client_id = ""

    fake_local = [
        Track(
            title="Emozioni",
            artist="Lucio Battisti",
            duration_ms=210000,
            spotify_id="local_lucio_battisti_-_emozioni",
            source="local",
        ),
        Track(
            title="Napule E",
            artist="Pino Daniele",
            duration_ms=210000,
            spotify_id="local_pino_daniele_-_napule_e",
            source="local",
        ),
    ]

    with (
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=fake_local),
        # Demo asset loader and DEMO_TRACKS path must not run — local has priority now.
        patch(
            "mammamiradio.playlist.playlist._load_demo_asset_tracks",
            side_effect=AssertionError("demo asset loader should not run when local music is present"),
        ),
    ):
        tracks, source, _err = fetch_startup_playlist(config)

    assert source.kind == "local"
    assert source.source_id == "local_music_dir"
    assert source.track_count == 2
    assert len(tracks) == 2
    assert {t.title for t in tracks} == {"Emozioni", "Napule E"}
    for t in tracks:
        assert t.source == "local"


def test_read_persisted_source_migrates_apple_music_top_50_to_top_100(tmp_path):
    """Backwards-compat migration: a persisted charts source written before the
    apple_music_it_top_50 → top_100 rename is transparently remapped on load.
    No warning, no fall-through to demo, no error to the operator."""
    from mammamiradio.playlist.playlist import (
        PERSISTED_SOURCE_FILENAME,
        read_persisted_source,
    )

    legacy_payload = {
        "kind": "charts",
        "source_id": "apple_music_it_top_50",
        "url": "https://example.invalid/charts.json",
        "label": "Current Italian charts",
        "track_count": 100,
        "selected_at": 1700000000.0,
    }
    (tmp_path / PERSISTED_SOURCE_FILENAME).write_text(json.dumps(legacy_payload))

    restored = read_persisted_source(tmp_path)

    assert restored is not None
    assert restored.kind == "charts"
    assert restored.source_id == "apple_music_it_top_100"
    # Other fields preserved
    assert restored.track_count == 100
    assert restored.label == "Current Italian charts"


def test_fetch_startup_restores_persisted_jamendo_source(config):
    config.playlist.jamendo_client_id = "Jamendo123"
    config.playlist.shuffle = False
    jamendo_tracks = [
        Track(
            title="Riparte",
            artist="Licenza Aperta",
            duration_ms=190000,
            spotify_id="jamendo_11",
            youtube_id="",
            direct_url="https://storage.jamendo.com/tracks/jamendo-11.mp3",
            source="jamendo",
        )
    ]

    with patch("mammamiradio.playlist.playlist._fetch_jamendo_playlist", return_value=jamendo_tracks):
        tracks, source, error = fetch_startup_playlist(
            config,
            PlaylistSource(kind="jamendo", source_id="italian pop", label="Jamendo CC Music"),
        )

    assert error == ""
    assert tracks == jamendo_tracks
    assert source.kind == "jamendo"
    assert source.source_id == "italian pop"


def test_download_sync_uses_direct_url_for_jamendo_track(tmp_path):
    from mammamiradio.playlist.downloader import _download_sync

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(
        title="Diretta",
        artist="Jamendo Artist",
        duration_ms=180000,
        spotify_id="jamendo_3",
        youtube_id="",
        direct_url="https://storage.jamendo.com/tracks/jamendo-3.mp3",
    )

    with (
        patch("mammamiradio.playlist.downloader._NO_REDIRECT_OPENER") as mock_opener,
        patch("mammamiradio.playlist.downloader.validate_download", return_value=(True, "ok")),
    ):
        mock_opener.open.return_value = _BytesResponse(b"jamendo mp3 bytes")
        result = _download_sync(track, cache_dir, music_dir)

    assert result == cache_dir / f"{track.cache_key}.mp3"
    assert result.read_bytes() == b"jamendo mp3 bytes"


def test_download_sync_jamendo_cache_hit_skips_direct_url_fetch(tmp_path):
    from mammamiradio.playlist.downloader import _download_sync

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(
        title="Gia Scaricata",
        artist="Jamendo Artist",
        duration_ms=180000,
        spotify_id="jamendo_9",
        youtube_id="",
        direct_url="https://cdn.example.test/jamendo-9.mp3",
    )
    cached = cache_dir / f"{track.cache_key}.mp3"
    cached.write_text("cached audio")

    with patch("mammamiradio.playlist.downloader._download_direct_url") as mock_direct:
        result = _download_sync(track, cache_dir, music_dir)

    assert result == cached
    mock_direct.assert_not_called()


def test_download_sync_direct_url_failure_falls_back_to_silence(tmp_path):
    from mammamiradio.playlist.downloader import _download_sync

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(
        title="Fallback Test",
        artist="Jamendo Artist",
        duration_ms=180000,
        spotify_id="jamendo_fail",
        youtube_id="",
        direct_url="https://storage.jamendo.com/fail.mp3",
    )

    with (
        patch(
            "mammamiradio.playlist.downloader._download_direct_url",
            side_effect=RuntimeError("network error"),
        ),
        patch("mammamiradio.playlist.downloader._ytdlp_enabled", return_value=False),
    ):
        result = _download_sync(track, cache_dir, music_dir)

    assert result.name.startswith("_silence_")
    assert result.exists()


def test_playlist_is_demo_false_for_jamendo_tracks():
    from mammamiradio.core.setup_status import _playlist_is_demo

    state = SimpleNamespace(
        playlist=[
            Track(
                title="Creative Commons Hit",
                artist="Jamendo Artist",
                duration_ms=180000,
                spotify_id="jamendo_77",
                youtube_id="",
                direct_url="https://cdn.example.test/jamendo-77.mp3",
            )
        ]
    )

    assert _playlist_is_demo(state) is False
