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
    duration_index = cmd.index("-t") + 1
    assert cmd[duration_index] == "210"
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


# --- _is_silence_placeholder tests ---


def test_is_silence_placeholder_returns_false_for_normal_file(cache_dir):
    """Non-silence files should not be flagged as placeholders."""
    from mammamiradio.downloader import _is_silence_placeholder

    fake = cache_dir / "normal.mp3"
    fake.write_bytes(b"\xff\xfb\x90\x00" * 100)  # fake MP3 data

    # Mock ffmpeg to report normal volume
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            stderr="mean_volume: -14.2 dB\n",
            returncode=0,
        )
        assert _is_silence_placeholder(fake) is False


def test_is_silence_placeholder_returns_true_for_silence(cache_dir):
    """Silence files (< -60dB) should be detected."""
    from mammamiradio.downloader import _is_silence_placeholder

    fake = cache_dir / "silence.mp3"
    fake.write_bytes(b"\x00" * 100)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            stderr="mean_volume: -91.0 dB\n",
            returncode=0,
        )
        assert _is_silence_placeholder(fake) is True


def test_is_silence_placeholder_returns_false_on_error(cache_dir):
    """If ffmpeg fails, assume file is real audio (safe default)."""
    from mammamiradio.downloader import _is_silence_placeholder

    fake = cache_dir / "broken.mp3"
    fake.write_bytes(b"\x00" * 10)

    with patch("subprocess.run", side_effect=OSError("ffmpeg not found")):
        assert _is_silence_placeholder(fake) is False


# --- purge_silence_cache tests ---


def test_purge_silence_cache_removes_silence_files(cache_dir):
    """Startup purge should remove silence placeholders."""
    from mammamiradio.downloader import purge_silence_cache

    real = cache_dir / "real_track.mp3"
    real.write_bytes(b"\xff\xfb" * 100)
    silence = cache_dir / "silence_track.mp3"
    silence.write_bytes(b"\x00" * 100)

    call_count = 0

    def _mock_silence_check(path):
        nonlocal call_count
        call_count += 1
        return path == silence

    with patch("mammamiradio.downloader._is_silence_placeholder", side_effect=_mock_silence_check):
        purged = purge_silence_cache(cache_dir)

    assert purged == 1
    assert real.exists()
    assert not silence.exists()


def test_purge_silence_cache_handles_empty_dir(tmp_path):
    """Purge on empty or missing dir should return 0."""
    from mammamiradio.downloader import purge_silence_cache

    assert purge_silence_cache(tmp_path / "nonexistent") == 0
    empty = tmp_path / "empty_cache"
    empty.mkdir()
    assert purge_silence_cache(empty) == 0


# --- cache hit with silence detection ---


def test_cache_hit_purges_silence_when_ytdlp_enabled(track, cache_dir, music_dir):
    """If a cached file is silence and yt-dlp is enabled, purge and re-download."""
    import os

    from mammamiradio.downloader import _download_sync

    cached = cache_dir / f"{track.cache_key}.mp3"
    cached.write_bytes(b"\x00" * 100)

    with (
        patch.dict(os.environ, {"MAMMAMIRADIO_ALLOW_YTDLP": "true"}),
        patch("mammamiradio.downloader._is_silence_placeholder", return_value=True),
        patch("mammamiradio.downloader._download_ytdlp", return_value=cached) as mock_ytdlp,
    ):
        result = _download_sync(track, cache_dir, music_dir)

    mock_ytdlp.assert_called_once()
    assert result == cached


def test_cache_hit_keeps_real_audio_when_ytdlp_enabled(track, cache_dir, music_dir):
    """If a cached file has real audio, return it even when yt-dlp is enabled."""
    import os

    from mammamiradio.downloader import _download_sync

    cached = cache_dir / f"{track.cache_key}.mp3"
    cached.write_bytes(b"\xff\xfb" * 100)

    with (
        patch.dict(os.environ, {"MAMMAMIRADIO_ALLOW_YTDLP": "true"}),
        patch("mammamiradio.downloader._is_silence_placeholder", return_value=False),
    ):
        result = _download_sync(track, cache_dir, music_dir)

    assert result == cached
