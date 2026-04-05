"""Tests for downloader module: fallback chain from cache to local to yt-dlp to placeholder."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from mammamiradio.models import Track


@pytest.fixture()
def track():
    return Track(title="Volare", artist="Domenico Modugno", duration_ms=210000, spotify_id="test1")


@pytest.fixture()
def cache_dir(tmp_path):
    d = tmp_path / "cache"
    d.mkdir()
    return d


@pytest.fixture()
def music_dir(tmp_path):
    d = tmp_path / "music"
    d.mkdir()
    return d


# --- _find_local tests ---


def test_find_local_returns_none_when_dir_missing(track, tmp_path):
    from mammamiradio.downloader import _find_local

    result = _find_local(track, tmp_path / "nonexistent")
    assert result is None


def test_find_local_returns_none_when_no_match(track, music_dir):
    from mammamiradio.downloader import _find_local

    (music_dir / "unrelated_song.mp3").touch()
    result = _find_local(track, music_dir)
    assert result is None


def test_find_local_matches_by_cache_key(track, music_dir):
    from mammamiradio.downloader import _find_local

    # Create a file whose name contains the cache_key
    mp3 = music_dir / f"{track.cache_key}.mp3"
    mp3.touch()
    result = _find_local(track, music_dir)
    assert result == mp3


def test_find_local_matches_by_title(track, music_dir):
    from mammamiradio.downloader import _find_local

    mp3 = music_dir / f"{track.title.lower()}.mp3"
    mp3.touch()
    result = _find_local(track, music_dir)
    assert result == mp3


# --- _download_sync: cache hit ---


def test_cache_hit_returns_immediately(track, cache_dir, music_dir):
    from mammamiradio.downloader import _download_sync

    cached = cache_dir / f"{track.cache_key}.mp3"
    cached.write_text("fake audio")

    result = _download_sync(track, cache_dir, music_dir)
    assert result == cached


# --- _download_sync: local file found ---


def test_local_file_found(track, cache_dir, music_dir):
    from mammamiradio.downloader import _download_sync

    local_mp3 = music_dir / f"{track.cache_key}.mp3"
    local_mp3.write_text("local audio")

    result = _download_sync(track, cache_dir, music_dir)
    assert result == local_mp3


# --- _download_sync: yt-dlp success ---


def test_ytdlp_disabled_by_default(track, cache_dir, music_dir):
    """yt-dlp should NOT run when MAMMAMIRADIO_ALLOW_YTDLP is unset."""
    import os

    from mammamiradio.downloader import _download_sync

    env = os.environ.copy()
    env.pop("MAMMAMIRADIO_ALLOW_YTDLP", None)

    with (
        patch.dict(os.environ, env, clear=True),
        patch("mammamiradio.downloader._run_ffmpeg") as mock_ffmpeg,
    ):
        _download_sync(track, cache_dir, music_dir)

    # Should fall through to silence, never touching yt-dlp
    mock_ffmpeg.assert_called_once()


def test_ytdlp_success_when_enabled(track, cache_dir, music_dir):
    """yt-dlp runs when MAMMAMIRADIO_ALLOW_YTDLP=true."""
    import os

    from mammamiradio.downloader import _download_sync

    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)

    def fake_download(queries):
        # Simulate yt-dlp creating the output file
        out = cache_dir / f"{track.cache_key}.mp3"
        out.write_text("downloaded audio")

    mock_ydl_instance.download = fake_download

    mock_yt_dlp = MagicMock()
    mock_yt_dlp.YoutubeDL.return_value = mock_ydl_instance

    with (
        patch.dict(os.environ, {"MAMMAMIRADIO_ALLOW_YTDLP": "true"}),
        patch.dict(sys.modules, {"yt_dlp": mock_yt_dlp}),
    ):
        result = _download_sync(track, cache_dir, music_dir)

    assert result == cache_dir / f"{track.cache_key}.mp3"
    assert result.exists()


# --- _download_sync: yt-dlp failure falls back to placeholder ---


def test_ytdlp_failure_falls_back_to_placeholder(track, cache_dir, music_dir):
    import os

    from mammamiradio.downloader import _download_sync

    mock_yt_dlp = MagicMock()
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.download.side_effect = Exception("Download failed")
    mock_yt_dlp.YoutubeDL.return_value = mock_ydl_instance

    with (
        patch.dict(os.environ, {"MAMMAMIRADIO_ALLOW_YTDLP": "true"}),
        patch.dict(sys.modules, {"yt_dlp": mock_yt_dlp}),
        patch("mammamiradio.downloader._run_ffmpeg") as mock_ffmpeg,
    ):
        result = _download_sync(track, cache_dir, music_dir)

    mock_ffmpeg.assert_called_once()
    expected_path = cache_dir / f"{track.cache_key}.mp3"
    assert result == expected_path


# --- _download_sync: yt-dlp not installed falls back to placeholder ---


def test_ytdlp_import_error_falls_back_to_placeholder(track, cache_dir, music_dir):
    import os

    from mammamiradio.downloader import _download_sync

    # Remove yt_dlp from sys.modules if present so the lazy import triggers ImportError
    with (
        patch.dict(os.environ, {"MAMMAMIRADIO_ALLOW_YTDLP": "true"}),
        patch.dict(sys.modules, {"yt_dlp": None}),
        patch("mammamiradio.downloader._run_ffmpeg") as mock_ffmpeg,
    ):
        result = _download_sync(track, cache_dir, music_dir)

    mock_ffmpeg.assert_called_once()
    expected_path = cache_dir / f"{track.cache_key}.mp3"
    assert result == expected_path


# --- _generate_placeholder ---


def test_generate_silence_calls_ffmpeg(track, tmp_path):
    from mammamiradio.downloader import _generate_silence

    out_path = tmp_path / "silence.mp3"

    with patch("mammamiradio.downloader._run_ffmpeg") as mock_ffmpeg:
        result = _generate_silence(track, out_path)

    assert result == out_path
    mock_ffmpeg.assert_called_once()
    cmd = mock_ffmpeg.call_args[0][0]
    assert "ffmpeg" in cmd[0]
    assert "anullsrc" in " ".join(cmd)
    assert str(out_path) in cmd


# --- download_track async wrapper ---


@pytest.mark.asyncio
async def test_download_track_async(track, cache_dir, music_dir):
    from mammamiradio.downloader import download_track

    # Put a file in cache so the sync function returns immediately
    cached = cache_dir / f"{track.cache_key}.mp3"
    cached.write_text("cached audio")

    result = await download_track(track, cache_dir, music_dir)
    assert result == cached
