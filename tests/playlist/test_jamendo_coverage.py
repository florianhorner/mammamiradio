"""Coverage tests for Jamendo downloader and playlist paths.

Three-scenario discipline (per CLAUDE.md audio delivery test coverage rule):
  Scenario 1 — Normal: feature works as designed.
  Scenario 2 — Empty fallback: no network, no cached file, no assets.
  Scenario 3 — Post-restart: state from a prior session, must still deliver audio.
"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

from mammamiradio.core.models import PlaylistSource, Track

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _BytesResponse(BytesIO):
    """Minimal context-manager shim for urlopen responses."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@pytest.fixture()
def config():
    from mammamiradio.core.config import load_config

    return load_config()


# ---------------------------------------------------------------------------
# downloader: _validate_direct_url — SSRF guard (line 329, 332)
# ---------------------------------------------------------------------------


def test_validate_direct_url_accepts_jamendo_cdn():
    """Normal: https://*.jamendo.com URLs are accepted without raising."""
    from mammamiradio.playlist.downloader import _validate_direct_url

    _validate_direct_url("https://storage.jamendo.com/tracks/some_track.mp3")
    _validate_direct_url("https://cdn.jamendo.com/tracks/other.mp3")
    _validate_direct_url("https://jamendo.com/tracks/root.mp3")


def test_validate_direct_url_rejects_non_https():
    """SSRF guard: http:// scheme must be rejected (line 329)."""
    from mammamiradio.playlist.downloader import _validate_direct_url

    with pytest.raises(ValueError, match="https"):
        _validate_direct_url("http://storage.jamendo.com/tracks/track.mp3")


def test_validate_direct_url_rejects_non_jamendo_host():
    """SSRF guard: non-jamendo.com host must be rejected (line 332)."""
    from mammamiradio.playlist.downloader import _validate_direct_url

    with pytest.raises(ValueError, match="jamendo"):
        _validate_direct_url("https://evil.example.com/tracks/track.mp3")


def test_validate_direct_url_rejects_localhost():
    """SSRF guard: localhost is not a jamendo.com host."""
    from mammamiradio.playlist.downloader import _validate_direct_url

    with pytest.raises(ValueError, match="jamendo"):
        _validate_direct_url("https://localhost/tracks/track.mp3")


# ---------------------------------------------------------------------------
# downloader: _BlockRedirectHandler — redirect blocking (line 319)
# ---------------------------------------------------------------------------


def test_block_redirect_handler_raises_on_redirect():
    """Normal+Empty: redirect to any URL must raise URLError (line 319)."""
    from urllib.request import Request

    from mammamiradio.playlist.downloader import _BlockRedirectHandler

    handler = _BlockRedirectHandler()
    req = Request("https://storage.jamendo.com/tracks/original.mp3")
    with pytest.raises(URLError, match="redirect"):
        handler.redirect_request(
            req,
            fp=None,
            code=302,
            msg="Found",
            headers={},
            newurl="https://evil.internal.host/tracks/redirected.mp3",
        )


# ---------------------------------------------------------------------------
# downloader: _download_direct_url — success and failure paths (lines 337-352)
# ---------------------------------------------------------------------------


def test_download_direct_url_success(tmp_path):
    """Normal: valid Jamendo URL is downloaded, validated, and returned."""
    from mammamiradio.playlist.downloader import _download_direct_url

    out = tmp_path / "track.mp3"
    with (
        patch("mammamiradio.playlist.downloader._NO_REDIRECT_OPENER") as mock_opener,
        patch("mammamiradio.playlist.downloader.validate_download", return_value=(True, "ok")),
    ):
        mock_opener.open.return_value = _BytesResponse(b"fake mp3 bytes")
        result = _download_direct_url("https://storage.jamendo.com/tracks/t.mp3", out)

    assert result == out
    assert out.read_bytes() == b"fake mp3 bytes"


def test_download_direct_url_network_error_raises(tmp_path):
    """Empty fallback: network failure raises RuntimeError and cleans up tmp file."""
    from mammamiradio.playlist.downloader import _download_direct_url

    out = tmp_path / "track.mp3"
    with (
        patch("mammamiradio.playlist.downloader._NO_REDIRECT_OPENER") as mock_opener,
        pytest.raises(RuntimeError, match="direct-url fetch failed"),
    ):
        mock_opener.open.side_effect = URLError("connection refused")
        _download_direct_url("https://storage.jamendo.com/tracks/t.mp3", out)

    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], f"tmp file not cleaned up: {tmp_files}"


def test_download_direct_url_validation_failure_removes_file(tmp_path):
    """Empty fallback: downloaded file that fails validation is removed, raises RuntimeError."""
    from mammamiradio.playlist.downloader import _download_direct_url

    out = tmp_path / "track.mp3"
    with (
        patch("mammamiradio.playlist.downloader._NO_REDIRECT_OPENER") as mock_opener,
        patch("mammamiradio.playlist.downloader.validate_download", return_value=(False, "duration too short")),
        pytest.raises(RuntimeError, match="direct-url validation failed"),
    ):
        mock_opener.open.return_value = _BytesResponse(b"fake mp3 bytes")
        _download_direct_url("https://storage.jamendo.com/tracks/t.mp3", out)

    assert not out.exists(), "file must be removed after validation failure"


def test_download_direct_url_rejects_non_jamendo_before_fetching(tmp_path):
    """SSRF guard: _validate_direct_url raises before any network call."""
    from mammamiradio.playlist.downloader import _download_direct_url

    out = tmp_path / "track.mp3"
    with (
        patch("mammamiradio.playlist.downloader._NO_REDIRECT_OPENER") as mock_opener,
        pytest.raises(ValueError, match="jamendo"),
    ):
        _download_direct_url("https://evil.example.com/tracks/t.mp3", out)

    mock_opener.open.assert_not_called()


# ---------------------------------------------------------------------------
# downloader: _download_sync — direct_url fallback paths (lines 392-393)
# ---------------------------------------------------------------------------


def test_download_sync_jamendo_direct_url_failure_returns_skip_marker(tmp_path):
    """Empty fallback: Jamendo direct_url failure returns a skip marker, never substitute audio."""
    from mammamiradio.playlist.downloader import _download_sync

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(
        title="Diretta Rotta",
        artist="Jamendo Fail",
        duration_ms=180000,
        spotify_id="jamendo_fail_1",
        youtube_id="",
        direct_url="https://storage.jamendo.com/tracks/broken.mp3",
        source="jamendo",
    )

    with (
        patch("mammamiradio.playlist.downloader._download_direct_url", side_effect=RuntimeError("fetch failed")),
        patch("mammamiradio.playlist.downloader._ytdlp_enabled", return_value=False),
    ):
        result = _download_sync(track, cache_dir, music_dir)

    assert result.name == f"_failed_{track.cache_key}.mp3"


def test_download_sync_jamendo_direct_url_failure_with_ytdlp_enabled_skips_ytdlp(tmp_path):
    """Post-restart: persisted Jamendo tracks still block yt-dlp fallback after direct_url failure."""
    from mammamiradio.playlist.downloader import _download_sync

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(
        title="Diretta Rotta Ytdlp",
        artist="Jamendo Fail Two",
        duration_ms=180000,
        spotify_id="jamendo_fail_2",
        youtube_id="",
        direct_url="https://storage.jamendo.com/tracks/broken2.mp3",
        source="jamendo",
    )

    with (
        patch("mammamiradio.playlist.downloader._download_direct_url", side_effect=RuntimeError("fetch failed")),
        patch("mammamiradio.playlist.downloader._ytdlp_enabled", return_value=True),
        patch("mammamiradio.playlist.downloader._download_ytdlp") as mock_ytdlp,
    ):
        result = _download_sync(track, cache_dir, music_dir)

    assert result.name == f"_failed_{track.cache_key}.mp3"
    mock_ytdlp.assert_not_called()


# ---------------------------------------------------------------------------
# downloader: _download_external_sync — yt-dlp success path (line 427)
# ---------------------------------------------------------------------------


def test_download_external_sync_calls_ytdlp_when_enabled(tmp_path):
    """Normal: when cache misses and yt-dlp is enabled, _download_ytdlp is called (line 427)."""
    import sys

    from mammamiradio.playlist.downloader import _download_external_sync

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(title="External Song Ext", artist="Artist Ext", duration_ms=180000, spotify_id="ext_1_unique")
    expected_out = cache_dir / f"{track.cache_key}.mp3"

    class _FakeYoutubeDL:
        def __init__(self, _opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def download(self, _queries):
            expected_out.write_text("ytdlp audio")

    mock_yt_dlp = MagicMock()
    mock_yt_dlp.YoutubeDL = _FakeYoutubeDL

    with (
        patch.dict("os.environ", {"MAMMAMIRADIO_ALLOW_YTDLP": "true"}),
        patch.dict(sys.modules, {"yt_dlp": mock_yt_dlp}),
    ):
        result = _download_external_sync(track, cache_dir, music_dir)

    assert result == expected_out


# ---------------------------------------------------------------------------
# downloader: evict_cache_lru — protected_paths branch (line 164/166)
# ---------------------------------------------------------------------------


def test_evict_cache_lru_skips_protected_paths(tmp_path):
    """Normal: files in protected_paths set are never evicted even when over budget."""
    from mammamiradio.playlist.downloader import evict_cache_lru

    d = tmp_path / "cache"
    d.mkdir()
    f = d / "queued_track.mp3"
    f.write_bytes(b"x" * (700 * 1024))

    evict_cache_lru(d, max_size_mb=0, protected_paths={f})
    assert f.exists(), "queued file in protected_paths must not be evicted"


# ---------------------------------------------------------------------------
# downloader: validate_download — short duration rejection (line 102)
# ---------------------------------------------------------------------------


def test_validate_download_rejects_short_duration(tmp_path):
    """Normal: files with duration < 30s are rejected by validate_download (line 102)."""
    from mammamiradio.playlist.downloader import validate_download

    p = tmp_path / "short.mp3"
    p.write_bytes(b"x" * (600 * 1024))
    result = MagicMock()
    result.returncode = 0
    result.stdout = '{"format": {"duration": "15.3"}}'

    with patch("mammamiradio.playlist.downloader.subprocess.run", return_value=result):
        ok, reason = validate_download(p)

    assert ok is False
    assert "duration too short" in reason


# ---------------------------------------------------------------------------
# downloader: _find_demo_asset — cache dir mismatch forces re-glob (line 218->216)
# ---------------------------------------------------------------------------


def test_find_demo_asset_cache_dir_mismatch_forces_reglob(tmp_path):
    """Post-restart: stale cache key causes re-glob on new _DEMO_ASSETS_DIR (branch 218->216)."""
    import mammamiradio.playlist.downloader as _dl
    from mammamiradio.playlist.downloader import _find_demo_asset

    track = Track(title="Forced Reglob Song", artist="Artist", duration_ms=180000)
    old_dir = tmp_path / "old_dir"
    old_dir.mkdir()
    new_dir = tmp_path / "new_dir"
    new_dir.mkdir()
    mp3 = new_dir / f"{track.title.lower()}.mp3"
    mp3.touch()

    # Prime cache with a stale key (different dir)
    _dl._demo_files_cache = (str(old_dir), [])

    with patch("mammamiradio.playlist.downloader._DEMO_ASSETS_DIR", new_dir):
        result = _find_demo_asset(track)

    assert result == mp3


# ---------------------------------------------------------------------------
# playlist: _load_local_music_tracks — over-200 cap (lines 131-136)
# ---------------------------------------------------------------------------


def test_load_local_music_tracks_caps_at_200(tmp_path):
    """Normal: when more than 200 MP3s are present, only the first 200 are returned."""
    from mammamiradio.playlist.playlist import _load_local_music_tracks

    for i in range(205):
        (tmp_path / f"song_{i:03d}.mp3").write_bytes(b"")

    tracks = _load_local_music_tracks(tmp_path)
    assert len(tracks) == 200


# ---------------------------------------------------------------------------
# playlist: _fetch_current_italy_charts — limit reached (line 246)
# ---------------------------------------------------------------------------


def test_fetch_current_italy_charts_respects_limit():
    """Normal: result is capped at the given limit (line 246)."""
    from mammamiradio.playlist.playlist import _fetch_current_italy_charts

    results = [{"name": f"Song {i}", "artistName": f"Artist {i}", "id": str(i)} for i in range(30)]
    payload = {"feed": {"results": results}}

    with patch("mammamiradio.playlist.playlist.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(payload).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tracks = _fetch_current_italy_charts(limit=5)

    assert len(tracks) == 5


# ---------------------------------------------------------------------------
# playlist: _jamendo_tags — URL fallback path (lines 279-283)
# ---------------------------------------------------------------------------


def test_jamendo_tags_reads_from_persisted_url(config):
    """Normal: when source_id is empty, tags are extracted from the persisted URL (lines 279-283)."""
    from mammamiradio.playlist.playlist import _jamendo_tags

    source = PlaylistSource(
        kind="jamendo",
        source_id="",
        label="Jamendo",
        url="jamendo://playlist?tags=jazz+italiano",
    )
    tags = _jamendo_tags(config, source)
    assert tags == "jazz italiano"


def test_jamendo_tags_falls_back_to_config_when_url_has_no_tags(config):
    """Normal: when source URL has no tags param, fall back to config.playlist.jamendo_tags."""
    from mammamiradio.playlist.playlist import _jamendo_tags

    config.playlist.jamendo_tags = "indie"
    source = PlaylistSource(kind="jamendo", source_id="", label="Jamendo", url="jamendo://playlist?")
    tags = _jamendo_tags(config, source)
    assert tags == "indie"


def test_jamendo_tags_uses_source_id_when_present(config):
    """Normal: non-empty source_id takes priority over URL and config."""
    from mammamiradio.playlist.playlist import _jamendo_tags

    config.playlist.jamendo_tags = "pop"
    source = PlaylistSource(kind="jamendo", source_id="rock italiano", label="Jamendo", url="")
    tags = _jamendo_tags(config, source)
    assert tags == "rock italiano"


# ---------------------------------------------------------------------------
# playlist: _fetch_jamendo_playlist — edge cases (lines 302, 309-311, 323, 325-330)
# ---------------------------------------------------------------------------


def test_fetch_jamendo_playlist_with_explicit_tags_override(config):
    """Normal: explicit tags= argument overrides config value (line 302)."""
    from mammamiradio.playlist.playlist import _fetch_jamendo_playlist

    config.playlist.jamendo_client_id = "cid123"
    config.playlist.jamendo_tags = "pop"

    payload = {
        "results": [
            {
                "id": "77",
                "name": "Jazz Track",
                "artist_name": "CC Jazz",
                "duration": 200,
                "audiodownload": "https://storage.jamendo.com/tracks/77.mp3",
                "album_name": "",
                "image": "",
            }
        ]
    }

    with patch("mammamiradio.playlist.playlist.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(payload).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        tracks = _fetch_jamendo_playlist(config, tags="jazz")

    assert len(tracks) == 1
    called_url = mock_urlopen.call_args.args[0]
    assert "tags=jazz" in called_url


def test_fetch_jamendo_playlist_network_error_returns_empty(config):
    """Empty fallback: network failure returns empty list without raising (lines 309-311)."""
    from mammamiradio.playlist.playlist import _fetch_jamendo_playlist

    config.playlist.jamendo_client_id = "cid123"
    with patch("mammamiradio.playlist.playlist.urlopen", side_effect=URLError("timeout")):
        tracks = _fetch_jamendo_playlist(config)
    assert tracks == []


def test_fetch_jamendo_playlist_skips_non_https_audiodownload(config):
    """Normal: tracks whose audiodownload URL is not https are silently skipped (line 323)."""
    from mammamiradio.playlist.playlist import _fetch_jamendo_playlist

    config.playlist.jamendo_client_id = "cid123"
    payload = {
        "results": [
            {
                "id": "10",
                "name": "Bad URL Track",
                "artist_name": "Artist",
                "duration": 180,
                "audiodownload": "http://storage.jamendo.com/tracks/10.mp3",
                "album_name": "",
                "image": "",
            },
            {
                "id": "11",
                "name": "Good Track",
                "artist_name": "Artist",
                "duration": 180,
                "audiodownload": "https://storage.jamendo.com/tracks/11.mp3",
                "album_name": "",
                "image": "",
            },
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
    assert tracks[0].title == "Good Track"


def test_fetch_jamendo_playlist_zero_duration_fallback(config):
    """Normal: zero duration falls back to 210000ms default (lines 325-330)."""
    from mammamiradio.playlist.playlist import _fetch_jamendo_playlist

    config.playlist.jamendo_client_id = "cid123"
    payload = {
        "results": [
            {
                "id": "20",
                "name": "No Duration",
                "artist_name": "Artist",
                "duration": 0,
                "audiodownload": "https://storage.jamendo.com/tracks/20.mp3",
                "album_name": "",
                "image": "",
            },
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
    assert tracks[0].duration_ms == 210000


def test_fetch_jamendo_playlist_none_duration_fallback(config):
    """Normal: None duration falls back to 210000ms default (lines 328-330)."""
    from mammamiradio.playlist.playlist import _fetch_jamendo_playlist

    config.playlist.jamendo_client_id = "cid123"
    payload = {
        "results": [
            {
                "id": "21",
                "name": "None Duration",
                "artist_name": "Artist",
                "duration": None,
                "audiodownload": "https://storage.jamendo.com/tracks/21.mp3",
                "album_name": "",
                "image": "",
            },
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
    assert tracks[0].duration_ms == 210000


# ---------------------------------------------------------------------------
# playlist: read_persisted_source — OSError branch (line 356)
# ---------------------------------------------------------------------------


def test_read_persisted_source_oserror_on_read(tmp_path):
    """Post-restart: when the persisted file raises OSError on read, returns None (line 356)."""
    from mammamiradio.playlist.playlist import PERSISTED_SOURCE_FILENAME, read_persisted_source

    path = tmp_path / PERSISTED_SOURCE_FILENAME
    path.write_text('{"kind": "charts"}')

    with patch("pathlib.Path.read_text", side_effect=OSError("disk error")):
        result = read_persisted_source(tmp_path)

    assert result is None


# ---------------------------------------------------------------------------
# playlist: load_explicit_source — jamendo URL kind + empty error (lines 412, 416)
# ---------------------------------------------------------------------------


def test_load_explicit_source_jamendo_url_kind_resolves(config):
    """Normal: source.kind='url' with jamendo:// scheme is treated as Jamendo (line 412)."""
    from mammamiradio.playlist.playlist import load_explicit_source

    config.playlist.jamendo_client_id = "cid123"
    config.playlist.shuffle = False
    jamendo_tracks = [
        Track(
            title="CC Song URL Kind",
            artist="CC Artist URL",
            duration_ms=180000,
            spotify_id="jamendo_url_99",
            youtube_id="",
            direct_url="https://storage.jamendo.com/tracks/99.mp3",
        )
    ]

    with patch("mammamiradio.playlist.playlist._fetch_jamendo_playlist", return_value=jamendo_tracks):
        tracks, source = load_explicit_source(
            config,
            PlaylistSource(kind="url", source_id="", label="Jamendo", url="jamendo://playlist?tags=pop"),
        )

    assert len(tracks) == 1
    assert tracks[0].title == jamendo_tracks[0].title
    assert tracks[0].artist == jamendo_tracks[0].artist
    assert tracks[0].source == "jamendo"
    assert source.kind == "jamendo"


def test_load_explicit_source_jamendo_empty_raises(config):
    """Empty fallback: Jamendo returning zero tracks raises ExplicitSourceError (line 416)."""
    from mammamiradio.playlist.playlist import ExplicitSourceError, load_explicit_source

    config.playlist.jamendo_client_id = "cid123"
    with (
        patch("mammamiradio.playlist.playlist._fetch_jamendo_playlist", return_value=[]),
        pytest.raises(ExplicitSourceError, match="temporarily unavailable"),
    ):
        load_explicit_source(
            config,
            PlaylistSource(kind="jamendo", source_id="pop", label="Jamendo CC Music"),
        )


# ---------------------------------------------------------------------------
# playlist: fetch_startup_playlist — persisted source failure (lines 439-441)
# ---------------------------------------------------------------------------


def test_fetch_startup_playlist_persisted_jamendo_fails_falls_back_to_demo(config):
    """Post-restart: when persisted Jamendo source fails, fall back to demo (lines 439-441)."""
    from mammamiradio.playlist.playlist import ExplicitSourceError, fetch_startup_playlist

    config.allow_ytdlp = False

    with (
        patch(
            "mammamiradio.playlist.playlist.load_explicit_source",
            side_effect=ExplicitSourceError("Jamendo temporarily unavailable"),
        ),
        patch("mammamiradio.playlist.playlist._load_demo_asset_tracks", return_value=[]),
    ):
        tracks, source, error = fetch_startup_playlist(
            config,
            PlaylistSource(kind="jamendo", source_id="pop", label="Jamendo CC Music"),
        )

    assert source.kind == "demo"
    assert tracks
    assert all(track.source == "demo" for track in tracks)
    assert "temporarily unavailable" in error


# ---------------------------------------------------------------------------
# playlist: fetch_startup_playlist — Jamendo as startup source (lines 458-459)
# ---------------------------------------------------------------------------


def test_fetch_startup_playlist_jamendo_used_as_startup_source(config):
    """Normal: when ytdlp disabled and jamendo_client_id is set, Jamendo is used (lines 458-459)."""
    from mammamiradio.playlist.playlist import fetch_startup_playlist

    config.allow_ytdlp = False
    config.playlist.jamendo_client_id = "cid_startup"
    config.playlist.jamendo_tags = "pop"
    config.playlist.shuffle = False
    jamendo_tracks = [
        Track(
            title="Startup CC Song",
            artist="CC Band",
            duration_ms=180000,
            spotify_id="jamendo_startup_unique_1",
            youtube_id="",
            direct_url="https://storage.jamendo.com/tracks/s1.mp3",
        )
    ]

    with patch("mammamiradio.playlist.playlist._fetch_jamendo_playlist", return_value=jamendo_tracks):
        tracks, source, _error = fetch_startup_playlist(config)

    assert len(tracks) == 1
    assert tracks[0].title == jamendo_tracks[0].title
    assert tracks[0].artist == jamendo_tracks[0].artist
    assert tracks[0].source == "jamendo"
    assert source.kind == "jamendo"
    assert _error == ""


# ---------------------------------------------------------------------------
# playlist: fetch_startup_playlist — local music warning (line 463)
# ---------------------------------------------------------------------------


def test_fetch_startup_playlist_uses_local_music_when_ytdlp_disabled_and_no_jamendo(config, tmp_path):
    """Operator-honesty: when music/ has MP3s, yt-dlp is off, and Jamendo isn't
    configured, the startup must use the operator's local files — NOT silently
    fall through to bundled demo assets.

    yt-dlp is only needed for downloading chart tracks; local MP3s already exist
    on disk and don't need it. The previous behavior warn-and-skipped, which
    contradicted the operator's stated intent (they put MP3s in music/).
    """
    from mammamiradio.core.models import Track
    from mammamiradio.playlist.playlist import fetch_startup_playlist

    config.allow_ytdlp = False
    config.playlist.jamendo_client_id = ""

    fake_local_track = Track(
        title="Emozioni",
        artist="Lucio Battisti",
        duration_ms=210000,
        spotify_id="local_lucio_battisti_-_emozioni",
        source="local",
    )

    with (
        patch("mammamiradio.playlist.playlist._load_demo_asset_tracks", return_value=[]),
        patch(
            "mammamiradio.playlist.playlist._load_local_music_tracks",
            return_value=[fake_local_track],
        ),
    ):
        tracks, source, _error = fetch_startup_playlist(config)

    assert source.kind == "local"
    assert source.source_id == "local_music_dir"
    assert source.track_count == 1
    assert len(tracks) == 1
    assert tracks[0].artist == "Lucio Battisti"
    assert tracks[0].source == "local"
