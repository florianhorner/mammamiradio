"""Tests for sync.py — SQLite init and playlist loading."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from mammamiradio.sync import _migrate_schema, init_db, load_cached_tracks


def test_init_db_creates_tables(tmp_path):
    db_path = tmp_path / "radio.db"
    init_db(db_path)
    assert db_path.exists()
    conn = sqlite3.connect(str(db_path))
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}
    conn.close()
    assert "tracks" in tables
    assert "play_history" in tables
    assert "listener_persona" in tables


def test_init_db_is_idempotent(tmp_path):
    """Calling init_db twice on the same path should not raise."""
    db_path = tmp_path / "radio.db"
    init_db(db_path)
    init_db(db_path)  # should not raise
    assert db_path.exists()


def test_migrate_schema_ignores_duplicate_column_errors():
    conn = MagicMock()
    conn.execute.side_effect = [
        sqlite3.OperationalError("duplicate column name: arc_metadata"),
        sqlite3.OperationalError("duplicate column name: skipped"),
        sqlite3.OperationalError("duplicate column name: listen_duration_s"),
    ]

    _migrate_schema(conn)


def test_migrate_schema_reraises_unexpected_operational_error():
    conn = MagicMock()
    conn.execute.side_effect = sqlite3.OperationalError("no such table: listener_persona")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        _migrate_schema(conn)


def test_init_db_creates_parent_directory(tmp_path):
    """init_db creates the parent directory if it doesn't exist."""
    db_path = tmp_path / "nested" / "dir" / "radio.db"
    assert not db_path.parent.exists()
    init_db(db_path)
    assert db_path.exists()


def test_load_cached_tracks_empty_on_fresh_db(tmp_path):
    db_path = tmp_path / "radio.db"
    init_db(db_path)
    tracks = load_cached_tracks(db_path)
    assert tracks == []


def test_load_cached_tracks_returns_tracks_after_insert(tmp_path):
    db_path = tmp_path / "radio.db"
    init_db(db_path)

    audio_file = tmp_path / "song.mp3"
    audio_file.write_bytes(b"fake audio")

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tracks (youtube_id, title, artist, duration_s, file_path) VALUES (?,?,?,?,?)",
        ("yt123", "Volare", "Modugno", 180.0, str(audio_file)),
    )
    conn.commit()
    conn.close()

    tracks = load_cached_tracks(db_path)
    assert len(tracks) == 1
    assert tracks[0].title == "Volare"
    assert tracks[0].artist == "Modugno"
    assert tracks[0].youtube_id == "yt123"


def test_load_cached_tracks_skips_missing_files(tmp_path):
    """Tracks whose file_path no longer exists are filtered out."""
    db_path = tmp_path / "radio.db"
    init_db(db_path)

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tracks (youtube_id, title, artist, duration_s, file_path) VALUES (?,?,?,?,?)",
        ("yt456", "Missing Song", "Artist", 120.0, "/nonexistent/path/song.mp3"),
    )
    conn.commit()
    conn.close()

    tracks = load_cached_tracks(db_path)
    assert tracks == []


def _ensure_yt_dlp_mock():
    """Install a mock yt_dlp module in sys.modules so ``import yt_dlp`` succeeds
    even when the real package is not installed (e.g. CI)."""
    import sys
    import types

    if "yt_dlp" not in sys.modules:
        mod = types.ModuleType("yt_dlp")
        mod.YoutubeDL = MagicMock()  # type: ignore[attr-defined]
        sys.modules["yt_dlp"] = mod


def test_sync_playlist_blocking_returns_empty_on_no_entries(tmp_path):
    """_sync_playlist_blocking returns [] when yt-dlp info has no 'entries' key."""
    _ensure_yt_dlp_mock()
    from mammamiradio.sync import _sync_playlist_blocking

    db_path = tmp_path / "radio.db"
    init_db(db_path)

    mock_ydl = MagicMock()
    mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
    mock_ydl.__exit__ = MagicMock(return_value=False)
    mock_ydl.extract_info = MagicMock(return_value={"title": "My Playlist"})  # no 'entries'
    mock_ydl.cookiejar = MagicMock()

    with patch("yt_dlp.YoutubeDL", return_value=mock_ydl):
        result = _sync_playlist_blocking(
            playlist_url="https://example.com/playlist",
            cache_dir=tmp_path / "cache",
            db_path=db_path,
        )

    assert result == []


def test_sync_playlist_blocking_returns_empty_on_null_info(tmp_path):
    """_sync_playlist_blocking returns [] when yt-dlp returns None."""
    _ensure_yt_dlp_mock()
    from mammamiradio.sync import _sync_playlist_blocking

    db_path = tmp_path / "radio.db"
    init_db(db_path)

    mock_ydl = MagicMock()
    mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
    mock_ydl.__exit__ = MagicMock(return_value=False)
    mock_ydl.extract_info = MagicMock(return_value=None)
    mock_ydl.cookiejar = MagicMock()

    with patch("yt_dlp.YoutubeDL", return_value=mock_ydl):
        result = _sync_playlist_blocking(
            playlist_url="https://example.com/playlist",
            cache_dir=tmp_path / "cache",
            db_path=db_path,
        )

    assert result == []
