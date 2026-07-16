"""Extended tests for mammamiradio/core/sync.py — coverage sprint."""

from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from mammamiradio.core.sync import init_db, load_cached_tracks

# ---------------------------------------------------------------------------
# _resolve_cookies_arg
# ---------------------------------------------------------------------------


def _ensure_yt_dlp_mock():
    """Ensure yt_dlp is mockable in sys.modules."""
    if "yt_dlp" not in sys.modules:
        mod = types.ModuleType("yt_dlp")
        mod.YoutubeDL = MagicMock()
        sys.modules["yt_dlp"] = mod


def test_resolve_cookies_chrome(monkeypatch):
    """Returns chrome cookies when chrome is available."""
    _ensure_yt_dlp_mock()
    from mammamiradio.core.sync import _resolve_cookies_arg

    mock_ydl = MagicMock()
    mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
    mock_ydl.__exit__ = MagicMock(return_value=False)
    mock_ydl.cookiejar = MagicMock()

    with patch("yt_dlp.YoutubeDL", return_value=mock_ydl):
        result = _resolve_cookies_arg()

    assert result == ["--cookies-from-browser", "chrome"]


def test_resolve_cookies_none_available(monkeypatch):
    """Returns empty list when no browser cookies work."""
    _ensure_yt_dlp_mock()
    from mammamiradio.core.sync import _resolve_cookies_arg

    mock_ydl = MagicMock()
    mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
    mock_ydl.__exit__ = MagicMock(return_value=False)
    type(mock_ydl).cookiejar = property(lambda self: (_ for _ in ()).throw(Exception("no cookies")))

    with patch("yt_dlp.YoutubeDL", return_value=mock_ydl):
        result = _resolve_cookies_arg()

    assert result == []


# ---------------------------------------------------------------------------
# _sync_playlist_blocking — cached track hit
# ---------------------------------------------------------------------------


def test_sync_playlist_uses_cached_tracks(tmp_path):
    """When a track is already in the DB and file exists, it's used without re-download."""
    _ensure_yt_dlp_mock()
    from mammamiradio.core.sync import _sync_playlist_blocking

    db_path = tmp_path / "radio.db"
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    init_db(db_path)

    # Pre-populate DB with a cached track
    audio_file = cache_dir / "vid123.mp3"
    audio_file.write_bytes(b"fake mp3 data")
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tracks (youtube_id, title, artist, duration_s, file_path) VALUES (?,?,?,?,?)",
        ("vid123", "Bella Ciao", "Artista", 200, str(audio_file)),
    )
    conn.commit()
    conn.close()

    # Mock yt-dlp to return a playlist with the same video
    mock_ydl = MagicMock()
    mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
    mock_ydl.__exit__ = MagicMock(return_value=False)
    mock_ydl.extract_info.return_value = {"entries": [{"id": "vid123", "title": "Bella Ciao", "duration": 200}]}
    mock_ydl.cookiejar = MagicMock()
    mock_ydl.download = MagicMock()

    with patch("yt_dlp.YoutubeDL", return_value=mock_ydl):
        tracks = _sync_playlist_blocking(
            playlist_url="https://example.com/playlist",
            cache_dir=cache_dir,
            db_path=db_path,
        )

    assert len(tracks) == 1
    assert tracks[0].title == "Bella Ciao"
    # download was NOT called — track was cached
    mock_ydl.download.assert_not_called()


# ---------------------------------------------------------------------------
# _sync_playlist_blocking — Artist-Title parsing
# ---------------------------------------------------------------------------


def test_sync_parses_artist_title_format(tmp_path):
    """Correctly splits 'Artist - Title' format."""
    _ensure_yt_dlp_mock()
    from mammamiradio.core.sync import _sync_playlist_blocking

    db_path = tmp_path / "radio.db"
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    init_db(db_path)

    mock_ydl = MagicMock()
    mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
    mock_ydl.__exit__ = MagicMock(return_value=False)
    mock_ydl.extract_info.return_value = {
        "entries": [
            {
                "id": "vid456",
                "title": "Domenico Modugno - Volare",
                "uploader": "Domenico Modugno",
                "duration": 180,
            }
        ]
    }
    mock_ydl.cookiejar = MagicMock()

    # Simulate download: create the output file
    def fake_download(urls):
        (cache_dir / "vid456.part.mp3").write_bytes(b"fake audio")

    mock_ydl.download = fake_download

    def fake_normalize(src, dst, config=None):
        Path(dst).write_bytes(Path(src).read_bytes())

    with (
        patch("yt_dlp.YoutubeDL", return_value=mock_ydl),
        patch("mammamiradio.core.sync.normalize", side_effect=fake_normalize),
    ):
        tracks = _sync_playlist_blocking(
            playlist_url="https://example.com/playlist",
            cache_dir=cache_dir,
            db_path=db_path,
        )

    assert len(tracks) == 1
    assert tracks[0].artist == "Domenico Modugno"
    assert tracks[0].title == "Volare"


# ---------------------------------------------------------------------------
# _sync_playlist_blocking — download failure
# ---------------------------------------------------------------------------


def test_sync_skips_failed_downloads(tmp_path):
    """Skips tracks that fail to download."""
    _ensure_yt_dlp_mock()
    from mammamiradio.core.sync import _sync_playlist_blocking

    db_path = tmp_path / "radio.db"
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    init_db(db_path)

    mock_ydl = MagicMock()
    mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
    mock_ydl.__exit__ = MagicMock(return_value=False)
    mock_ydl.extract_info.return_value = {"entries": [{"id": "fail1", "title": "Bad Track", "duration": 60}]}
    mock_ydl.cookiejar = MagicMock()
    mock_ydl.download = MagicMock(side_effect=Exception("download failed"))

    with patch("yt_dlp.YoutubeDL", return_value=mock_ydl):
        tracks = _sync_playlist_blocking(
            playlist_url="https://example.com/playlist",
            cache_dir=cache_dir,
            db_path=db_path,
        )

    assert tracks == []


def test_sync_rejects_when_external_media_capability_is_unavailable(tmp_path):
    from mammamiradio.core.sync import _sync_playlist_blocking

    with (
        patch("mammamiradio.core.sync.external_media_enabled", return_value=False),
        patch("mammamiradio.core.sync._load_external_media_module") as load_external_media,
        pytest.raises(RuntimeError, match="external media is unavailable"),
    ):
        _sync_playlist_blocking(
            playlist_url="https://example.com/playlist",
            cache_dir=tmp_path / "cache",
            db_path=tmp_path / "radio.db",
        )

    load_external_media.assert_not_called()


def test_sync_without_browser_cookies_skips_empty_entry_and_missing_output(tmp_path):
    from mammamiradio.core.sync import _sync_playlist_blocking

    db_path = tmp_path / "radio.db"
    cache_dir = tmp_path / "cache"
    init_db(db_path)
    cookie_attempts: list[str] = []
    download_urls: list[str] = []

    class FakeYDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @property
        def cookiejar(self):
            browser = self.options["cookiesfrombrowser"][0]
            cookie_attempts.append(browser)
            raise RuntimeError("cookies unavailable")

        def extract_info(self, _url, *, download):
            assert download is False
            return {"entries": [None, {"id": "no-output", "title": "No Output", "duration": 30}]}

        def download(self, urls):
            download_urls.extend(urls)

    with (
        patch("mammamiradio.core.sync.external_media_enabled", return_value=True),
        patch(
            "mammamiradio.core.sync._load_external_media_module",
            return_value=SimpleNamespace(YoutubeDL=FakeYDL),
        ),
    ):
        tracks = _sync_playlist_blocking(
            playlist_url="https://example.com/playlist",
            cache_dir=cache_dir,
            db_path=db_path,
        )

    assert tracks == []
    assert cookie_attempts == ["chrome", "firefox", "safari"]
    assert download_urls == ["https://www.youtube.com/watch?v=no-output"]


def test_sync_adopts_alternate_postprocessor_extension(tmp_path):
    from mammamiradio.core.sync import _sync_playlist_blocking

    db_path = tmp_path / "radio.db"
    cache_dir = tmp_path / "cache"
    init_db(db_path)

    class FakeYDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @property
        def cookiejar(self):
            return object()

        def extract_info(self, _url, *, download):
            assert download is False
            return {"entries": [{"id": "alternate", "title": "Alternate", "duration": 42}]}

        def download(self, _urls):
            (cache_dir / "alternate.part.webm").write_bytes(b"alternate audio")

    with (
        patch("mammamiradio.core.sync.external_media_enabled", return_value=True),
        patch(
            "mammamiradio.core.sync._load_external_media_module",
            return_value=SimpleNamespace(YoutubeDL=FakeYDL),
        ),
    ):
        tracks = _sync_playlist_blocking(
            playlist_url="https://example.com/playlist",
            cache_dir=cache_dir,
            db_path=db_path,
        )

    assert len(tracks) == 1
    assert tracks[0].local_path == cache_dir / "alternate.mp3"
    assert tracks[0].local_path.read_bytes() == b"alternate audio"


def test_sync_uses_raw_download_when_normalization_fails(tmp_path):
    from mammamiradio.core.sync import _sync_playlist_blocking

    db_path = tmp_path / "radio.db"
    cache_dir = tmp_path / "cache"
    init_db(db_path)

    class FakeYDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @property
        def cookiejar(self):
            return object()

        def extract_info(self, _url, *, download):
            assert download is False
            return {"entries": [{"id": "raw", "title": "Raw", "duration": 12}]}

        def download(self, _urls):
            (cache_dir / "raw.part.mp3").write_bytes(b"raw audio")

    with (
        patch("mammamiradio.core.sync.external_media_enabled", return_value=True),
        patch(
            "mammamiradio.core.sync._load_external_media_module",
            return_value=SimpleNamespace(YoutubeDL=FakeYDL),
        ),
        patch("mammamiradio.core.sync.normalize", side_effect=RuntimeError("normalizer unavailable")),
    ):
        tracks = _sync_playlist_blocking(
            playlist_url="https://example.com/playlist",
            cache_dir=cache_dir,
            db_path=db_path,
        )

    assert len(tracks) == 1
    assert tracks[0].local_path == cache_dir / "raw.mp3"
    assert tracks[0].local_path.read_bytes() == b"raw audio"


@pytest.mark.asyncio
async def test_sync_playlist_runs_blocking_work_in_executor(tmp_path):
    from mammamiradio.core.sync import sync_playlist

    expected = [MagicMock()]
    with patch("mammamiradio.core.sync._sync_playlist_blocking", return_value=expected) as blocking:
        result = await sync_playlist(
            "https://example.com/playlist",
            tmp_path / "cache",
            tmp_path / "radio.db",
            config=None,
        )

    assert result == expected
    blocking.assert_called_once_with(
        "https://example.com/playlist",
        tmp_path / "cache",
        tmp_path / "radio.db",
        None,
    )


# ---------------------------------------------------------------------------
# load_cached_tracks — nonexistent DB
# ---------------------------------------------------------------------------


def test_load_cached_tracks_nonexistent_db(tmp_path):
    """Returns empty list when DB file doesn't exist."""
    assert load_cached_tracks(tmp_path / "nonexistent.db") == []


# ---------------------------------------------------------------------------
# load_cached_tracks — null artist
# ---------------------------------------------------------------------------


def test_load_cached_tracks_null_artist(tmp_path):
    """Falls back to 'Unknown' when artist is NULL."""
    db_path = tmp_path / "radio.db"
    init_db(db_path)

    audio_file = tmp_path / "song.mp3"
    audio_file.write_bytes(b"audio")

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tracks (youtube_id, title, artist, duration_s, file_path) VALUES (?,?,?,?,?)",
        ("yt_null", "No Artist Song", None, 120.0, str(audio_file)),
    )
    conn.commit()
    conn.close()

    tracks = load_cached_tracks(db_path)
    assert len(tracks) == 1
    assert tracks[0].artist == "Unknown"


# ---------------------------------------------------------------------------
# DB schema
# ---------------------------------------------------------------------------


def test_db_schema_has_listener_persona_default(tmp_path):
    """The listener_persona table is seeded with a default row."""
    db_path = tmp_path / "radio.db"
    init_db(db_path)

    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT id, session_count FROM listener_persona").fetchone()
    conn.close()

    assert row is not None
    assert row[0] == 1
    assert row[1] == 0
