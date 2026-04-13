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


def test_validate_download_rejects_small_file(tmp_path):
    from mammamiradio.downloader import validate_download

    file_path = tmp_path / "tiny.mp3"
    file_path.write_bytes(b"x" * 100)

    with patch("mammamiradio.downloader.subprocess.run") as mock_run:
        ok, reason = validate_download(file_path)

    assert ok is False
    assert "too small" in reason
    mock_run.assert_not_called()


def test_validate_download_accepts_valid_duration(tmp_path):
    from mammamiradio.downloader import validate_download

    file_path = tmp_path / "good.mp3"
    file_path.write_bytes(b"x" * (600 * 1024))
    result = MagicMock()
    result.returncode = 0
    result.stdout = '{"format":{"duration":"180.2"}}'

    with patch("mammamiradio.downloader.subprocess.run", return_value=result) as mock_run:
        ok, reason = validate_download(file_path)

    assert ok is True
    assert reason == "ok"
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[:5] == ["ffprobe", "-v", "quiet", "-print_format", "json"]
    assert str(file_path) in cmd


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


def test_find_demo_asset_matches_by_cache_key(track, tmp_path):
    from mammamiradio.downloader import _find_demo_asset

    demo_dir = tmp_path / "demo_assets" / "music"
    demo_dir.mkdir(parents=True)
    demo_file = demo_dir / f"{track.cache_key}_demo.mp3"
    demo_file.touch()

    with patch("mammamiradio.downloader._DEMO_ASSETS_DIR", demo_dir):
        result = _find_demo_asset(track)

    assert result == demo_file


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


def test_download_sync_prefers_demo_asset(track, cache_dir, music_dir, tmp_path):
    from mammamiradio.downloader import _download_sync

    demo_dir = tmp_path / "demo_assets" / "music"
    demo_dir.mkdir(parents=True)
    demo_file = demo_dir / f"{track.cache_key}.mp3"
    demo_file.write_text("demo audio")
    (music_dir / f"{track.cache_key}.mp3").write_text("local audio")

    with patch("mammamiradio.downloader._DEMO_ASSETS_DIR", demo_dir):
        result = _download_sync(track, cache_dir, music_dir)

    assert result == demo_file


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


def test_ytdlp_uses_no_progress_options(track, cache_dir):
    """yt-dlp is configured to avoid progress-bar noise in logs."""
    from mammamiradio.downloader import _download_ytdlp

    captured_opts = {}

    class _FakeYoutubeDL:
        def __init__(self, opts):
            captured_opts.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def download(self, queries):
            (cache_dir / f"{track.cache_key}.mp3").write_text("downloaded audio")

    mock_yt_dlp = MagicMock()
    mock_yt_dlp.YoutubeDL = _FakeYoutubeDL

    with patch.dict(sys.modules, {"yt_dlp": mock_yt_dlp}):
        out = _download_ytdlp(track, cache_dir)

    assert out == cache_dir / f"{track.cache_key}.mp3"
    assert captured_opts["quiet"] is True
    assert captured_opts["no_warnings"] is True
    assert captured_opts["noprogress"] is True
    assert captured_opts["abort_on_unavailable_fragments"] is True
    assert captured_opts["throttled_rate"] == 100_000
    assert captured_opts["check_formats"] is True
    assert captured_opts["concurrent_fragment_downloads"] == 2
    assert "temp" in captured_opts.get("paths", {})


def test_download_ytdlp_uses_exact_watch_url_when_youtube_id(cache_dir):
    from mammamiradio.downloader import _download_ytdlp

    track = Track(
        title="Albachiara",
        artist="Vasco Rossi",
        duration_ms=300000,
        spotify_id="x1",
        youtube_id="abc123",
    )
    captured_queries = []

    class _FakeYoutubeDL:
        def __init__(self, _opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def download(self, queries):
            captured_queries.extend(queries)
            (cache_dir / f"{track.cache_key}.mp3").write_text("downloaded audio")

    mock_yt_dlp = MagicMock()
    mock_yt_dlp.YoutubeDL = _FakeYoutubeDL

    with patch.dict(sys.modules, {"yt_dlp": mock_yt_dlp}):
        out = _download_ytdlp(track, cache_dir)

    assert out.exists()
    assert captured_queries == ["https://www.youtube.com/watch?v=abc123"]


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


def test_download_external_sync_raises_when_ytdlp_disabled(track, cache_dir, music_dir):
    import os

    from mammamiradio.downloader import _download_external_sync

    env = os.environ.copy()
    env.pop("MAMMAMIRADIO_ALLOW_YTDLP", None)

    with patch.dict(os.environ, env, clear=True), pytest.raises(RuntimeError, match="yt-dlp is disabled"):
        _download_external_sync(track, cache_dir, music_dir)


# --- evict_cache_lru ---


def test_evict_cache_lru_zero_limit_is_noop(cache_dir):
    from mammamiradio.downloader import evict_cache_lru

    (cache_dir / "a.mp3").write_bytes(b"x" * 1024 * 1024)
    evict_cache_lru(cache_dir, 0)
    assert (cache_dir / "a.mp3").exists()


def test_evict_cache_lru_under_limit_noop(cache_dir):
    from mammamiradio.downloader import evict_cache_lru

    (cache_dir / "a.mp3").write_bytes(b"x" * 1024 * 1024)
    evict_cache_lru(cache_dir, 100)
    assert (cache_dir / "a.mp3").exists()


def test_evict_cache_lru_over_limit_removes_oldest(cache_dir):
    import time

    from mammamiradio.downloader import evict_cache_lru

    old = cache_dir / "old.mp3"
    new = cache_dir / "new.mp3"
    old.write_bytes(b"x" * 1024 * 1024)
    time.sleep(0.02)
    new.write_bytes(b"x" * 1024 * 1024)
    # 2 MB total, limit 1 MB → should evict the older file
    evict_cache_lru(cache_dir, 1)
    assert not old.exists()
    assert new.exists()


def test_evict_cache_lru_protects_db_and_json(cache_dir):
    from mammamiradio.downloader import evict_cache_lru

    protected = ["mammamiradio.db", "playlist_source.json", "session_stopped.flag"]
    for name in protected:
        (cache_dir / name).write_bytes(b"x" * 1024 * 1024)
    (cache_dir / "track.mp3").write_bytes(b"x" * 1024 * 1024)
    evict_cache_lru(cache_dir, 0)  # even with 0 limit, protected files survive
    for name in protected:
        assert (cache_dir / name).exists()


def test_evict_cache_lru_handles_oserror(cache_dir):
    from unittest.mock import patch

    from mammamiradio.downloader import evict_cache_lru

    f = cache_dir / "broken.mp3"
    f.write_bytes(b"x" * 1024 * 1024)
    with patch("pathlib.Path.unlink", side_effect=OSError("permission denied")):
        # Should not raise — logs warning and continues
        evict_cache_lru(cache_dir, 0.0001)


def test_evict_cache_lru_evicts_regular_before_norm(cache_dir):
    """Regular files are evicted before norm cache; norm evicted if still over budget."""
    from mammamiradio.downloader import evict_cache_lru

    norm = cache_dir / "norm_track_192k.mp3"
    regular = cache_dir / "regular.mp3"
    norm.write_bytes(b"x" * 700 * 1024)
    regular.write_bytes(b"x" * 700 * 1024)

    # Budget allows ~700 KB — regular evicted first, norm survives
    evict_cache_lru(cache_dir, 1)
    assert norm.exists()
    assert not regular.exists()


def test_evict_cache_lru_evicts_norm_when_over_budget(cache_dir):
    """Norm files are evicted too when the cache is still over budget after regular eviction."""
    from mammamiradio.downloader import evict_cache_lru

    norm = cache_dir / "norm_track_192k.mp3"
    norm.write_bytes(b"x" * 700 * 1024)

    # Budget is tiny — norm must be evicted
    evict_cache_lru(cache_dir, 0.1)
    assert not norm.exists()


# --- search_ytdlp_metadata ---


def test_search_ytdlp_metadata_disabled_returns_empty():
    from mammamiradio.downloader import search_ytdlp_metadata

    with patch.dict("os.environ", {"MAMMAMIRADIO_ALLOW_YTDLP": "false"}):
        assert search_ytdlp_metadata("vasco", max_results=3) == []


def test_search_ytdlp_metadata_import_error_returns_empty():
    from mammamiradio.downloader import search_ytdlp_metadata

    with (
        patch.dict("os.environ", {"MAMMAMIRADIO_ALLOW_YTDLP": "true"}),
        patch.dict(sys.modules, {"yt_dlp": None}),
    ):
        assert search_ytdlp_metadata("vasco", max_results=3) == []


def test_search_ytdlp_metadata_success_parses_entries():
    from mammamiradio.downloader import search_ytdlp_metadata

    class _FakeYoutubeDL:
        def __init__(self, _opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, query, download=False):
            assert query == "ytsearch2:vasco"
            assert download is False
            return {
                "entries": [
                    None,
                    {"id": ""},
                    {"id": "yt1", "title": "Albachiara", "uploader": "Vasco Rossi", "duration": 123},
                    {"id": "yt2", "title": "Volare", "channel": "Modugno Channel", "duration": 0},
                ]
            }

    mock_yt_dlp = MagicMock()
    mock_yt_dlp.YoutubeDL = _FakeYoutubeDL

    with (
        patch.dict("os.environ", {"MAMMAMIRADIO_ALLOW_YTDLP": "true"}),
        patch.dict(sys.modules, {"yt_dlp": mock_yt_dlp}),
    ):
        results = search_ytdlp_metadata("vasco", max_results=2)

    assert len(results) == 2
    assert results[0]["youtube_id"] == "yt1"
    assert results[0]["artist"] == "Vasco Rossi"
    assert results[0]["duration_ms"] == 123000
    assert results[1]["youtube_id"] == "yt2"
    assert results[1]["artist"] == "Modugno Channel"


# --- purge_suspect_cache_files ---


def test_purge_suspect_cache_files_empty_dir(tmp_path):
    from mammamiradio.downloader import purge_suspect_cache_files

    d = tmp_path / "cache"
    d.mkdir()
    assert purge_suspect_cache_files(d) == 0


def test_purge_suspect_cache_files_nonexistent_dir(tmp_path):
    from mammamiradio.downloader import purge_suspect_cache_files

    assert purge_suspect_cache_files(tmp_path / "nope") == 0


def test_purge_suspect_cache_files_removes_small_files(tmp_path):
    from mammamiradio.downloader import purge_suspect_cache_files

    d = tmp_path / "cache"
    d.mkdir()
    small = d / "bad_download.mp3"
    small.write_bytes(b"x" * 100)  # well below 10240
    assert purge_suspect_cache_files(d) == 1
    assert not small.exists()


def test_purge_suspect_cache_files_keeps_large_files(tmp_path):
    from mammamiradio.downloader import purge_suspect_cache_files

    d = tmp_path / "cache"
    d.mkdir()
    big = d / "good_track.mp3"
    big.write_bytes(b"x" * 10240)  # exactly at threshold
    assert purge_suspect_cache_files(d) == 0
    assert big.exists()


def test_purge_suspect_cache_files_skips_protected(tmp_path):
    from mammamiradio.downloader import purge_suspect_cache_files

    d = tmp_path / "cache"
    d.mkdir()
    # Create protected files that are small .mp3 — they would match the glob
    # only if they end in .mp3, but _CACHE_PROTECTED names don't end in .mp3
    # so let's test with a non-mp3 extension and also with a small mp3
    small = d / "tiny.mp3"
    small.write_bytes(b"x" * 10)
    # Protected files aren't .mp3 so they won't be globbed, but test the logic
    # by creating an .mp3 with a protected name (edge case)
    for name in ["mammamiradio.db", "playlist_source.json", "session_stopped.flag"]:
        # These don't end in .mp3 so glob("*.mp3") won't match them anyway
        (d / name).write_bytes(b"x" * 10)
    assert purge_suspect_cache_files(d) == 1  # only tiny.mp3
    assert not small.exists()


def test_purge_suspect_cache_files_oserror_on_stat(tmp_path):
    from pathlib import Path

    from mammamiradio.downloader import purge_suspect_cache_files

    d = tmp_path / "cache"
    d.mkdir()
    f = d / "broken.mp3"
    f.write_bytes(b"x" * 10)
    original_stat = Path.stat

    def _stat_that_fails(self, *args, **kwargs):
        if self.name == "broken.mp3":
            raise OSError("permission denied")
        return original_stat(self, *args, **kwargs)

    with patch.object(Path, "stat", _stat_that_fails):
        assert purge_suspect_cache_files(d) == 0


def test_purge_suspect_cache_files_skips_protected_mp3_names(tmp_path):
    """If a file with a protected name appears in the glob, it should be skipped."""
    from mammamiradio.downloader import _CACHE_PROTECTED, purge_suspect_cache_files

    d = tmp_path / "cache"
    d.mkdir()
    protected_name = next(iter(_CACHE_PROTECTED))
    fake_file = d / protected_name
    fake_file.write_bytes(b"x" * 10)

    with patch.object(type(d), "glob", return_value=[fake_file]):
        assert purge_suspect_cache_files(d) == 0
        assert fake_file.exists()


def test_purge_suspect_cache_files_purges_tiny_norm_cache_files(tmp_path):
    from mammamiradio.downloader import purge_suspect_cache_files

    d = tmp_path / "cache"
    d.mkdir()
    norm = d / "norm_song_192k.mp3"
    tiny = d / "tiny.mp3"
    norm.write_bytes(b"x" * 100)
    tiny.write_bytes(b"x" * 100)

    purged = purge_suspect_cache_files(d)
    assert purged == 2
    assert not norm.exists()
    assert not tiny.exists()


def test_purge_suspect_cache_files_keeps_large_norm_cache_files(tmp_path):
    from mammamiradio.downloader import purge_suspect_cache_files

    d = tmp_path / "cache"
    d.mkdir()
    norm = d / "norm_song_192k.mp3"
    norm.write_bytes(b"x" * 20000)

    purged = purge_suspect_cache_files(d)
    assert purged == 0
    assert norm.exists()


def test_purge_suspect_cache_files_custom_threshold(tmp_path):
    from mammamiradio.downloader import purge_suspect_cache_files

    d = tmp_path / "cache"
    d.mkdir()
    f = d / "medium.mp3"
    f.write_bytes(b"x" * 500)
    # With higher threshold, this should be purged
    assert purge_suspect_cache_files(d, min_size_bytes=1000) == 1
    assert not f.exists()


def test_download_ytdlp_raises_when_no_output_file(cache_dir):
    """_download_ytdlp raises FileNotFoundError when yt-dlp doesn't create the output."""
    from mammamiradio.downloader import _download_ytdlp

    track = Track(title="Missing", artist="Nobody", duration_ms=100000, spotify_id="x")

    class _FakeYoutubeDL:
        def __init__(self, _opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def download(self, queries):
            pass  # Deliberately don't create the output file

    mock_yt_dlp = MagicMock()
    mock_yt_dlp.YoutubeDL = _FakeYoutubeDL

    with patch.dict(sys.modules, {"yt_dlp": mock_yt_dlp}), pytest.raises(FileNotFoundError):
        _download_ytdlp(track, cache_dir)


def test_search_ytdlp_metadata_returns_empty_on_extract_exception():
    from mammamiradio.downloader import search_ytdlp_metadata

    class _FailingYoutubeDL:
        def __init__(self, _opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, _query, download=False):
            raise RuntimeError("yt-dlp failed")

    mock_yt_dlp = MagicMock()
    mock_yt_dlp.YoutubeDL = _FailingYoutubeDL

    with (
        patch.dict("os.environ", {"MAMMAMIRADIO_ALLOW_YTDLP": "true"}),
        patch.dict(sys.modules, {"yt_dlp": mock_yt_dlp}),
    ):
        assert search_ytdlp_metadata("vasco", max_results=3) == []


# ---------------------------------------------------------------------------
# validate_download: error paths
# ---------------------------------------------------------------------------


def test_validate_download_oserror_on_stat(tmp_path):
    from pathlib import Path

    from mammamiradio.downloader import validate_download

    p = tmp_path / "ghost.mp3"
    p.touch()
    original_stat = Path.stat

    def _stat_raises(self, *args, **kwargs):
        if self.name == "ghost.mp3":
            raise OSError("permission denied")
        return original_stat(self, *args, **kwargs)

    with patch.object(Path, "stat", _stat_raises):
        ok, reason = validate_download(p)
    assert ok is False
    assert "stat failed" in reason


def test_validate_download_ffprobe_timeout(tmp_path):
    from mammamiradio.downloader import validate_download

    p = tmp_path / "track.mp3"
    p.write_bytes(b"x" * (600 * 1024))

    with patch("mammamiradio.downloader.subprocess.run", side_effect=__import__("subprocess").TimeoutExpired("ffprobe", 30)):
        ok, reason = validate_download(p)
    assert ok is False
    assert "timed out" in reason


def test_validate_download_ffprobe_oserror(tmp_path):
    from mammamiradio.downloader import validate_download

    p = tmp_path / "track.mp3"
    p.write_bytes(b"x" * (600 * 1024))

    with patch("mammamiradio.downloader.subprocess.run", side_effect=OSError("not found")):
        ok, reason = validate_download(p)
    assert ok is False
    assert "failed to start" in reason


def test_validate_download_ffprobe_nonzero_returncode(tmp_path):
    from mammamiradio.downloader import validate_download

    p = tmp_path / "track.mp3"
    p.write_bytes(b"x" * (600 * 1024))
    result = MagicMock()
    result.returncode = 1
    result.stdout = ""

    with patch("mammamiradio.downloader.subprocess.run", return_value=result):
        ok, reason = validate_download(p)
    assert ok is False
    assert "ffprobe failed" in reason


def test_validate_download_json_decode_error(tmp_path):
    from mammamiradio.downloader import validate_download

    p = tmp_path / "track.mp3"
    p.write_bytes(b"x" * (600 * 1024))
    result = MagicMock()
    result.returncode = 0
    result.stdout = "NOT JSON {"

    with patch("mammamiradio.downloader.subprocess.run", return_value=result):
        ok, reason = validate_download(p)
    assert ok is False
    assert "invalid JSON" in reason


def test_validate_download_missing_duration(tmp_path):
    from mammamiradio.downloader import validate_download

    p = tmp_path / "track.mp3"
    p.write_bytes(b"x" * (600 * 1024))
    result = MagicMock()
    result.returncode = 0
    result.stdout = '{"format": {}}'

    with patch("mammamiradio.downloader.subprocess.run", return_value=result):
        ok, reason = validate_download(p)
    assert ok is False
    assert "missing duration" in reason


def test_validate_download_invalid_duration_float(tmp_path):
    from mammamiradio.downloader import validate_download

    p = tmp_path / "track.mp3"
    p.write_bytes(b"x" * (600 * 1024))
    result = MagicMock()
    result.returncode = 0
    result.stdout = '{"format": {"duration": "not_a_number"}}'

    with patch("mammamiradio.downloader.subprocess.run", return_value=result):
        ok, reason = validate_download(p)
    assert ok is False
    assert "invalid duration" in reason


# ---------------------------------------------------------------------------
# evict_cache_lru: protected file in cache dir is skipped
# ---------------------------------------------------------------------------


def test_evict_cache_lru_skips_protected_files(tmp_path):
    """Files whose names are in _CACHE_PROTECTED must never be evicted."""
    from mammamiradio.downloader import _CACHE_PROTECTED, evict_cache_lru

    d = tmp_path / "cache"
    d.mkdir()
    protected_name = next(iter(_CACHE_PROTECTED))
    protected_file = d / protected_name
    protected_file.write_bytes(b"x" * (600 * 1024))

    # Force glob to return the protected file so the skip branch is exercised
    with patch.object(type(d), "glob", return_value=[protected_file]):
        evict_cache_lru(d, max_size_mb=0)  # zero budget → would evict non-protected files

    assert protected_file.exists()


# ---------------------------------------------------------------------------
# _find_demo_asset: cache hit on second call
# ---------------------------------------------------------------------------


def test_find_demo_asset_cache_hit_on_second_call(tmp_path):
    """_find_demo_asset must return a cached result without re-globbing on repeat calls."""
    import mammamiradio.downloader as _dl
    from mammamiradio.downloader import _find_demo_asset

    track = Track(title="Test Song", artist="Test Artist", duration_ms=180000)
    demo_dir = tmp_path / "demo_music"
    demo_dir.mkdir()
    mp3 = demo_dir / f"{track.title.lower()}.mp3"
    mp3.touch()

    # Reset module-level cache so we start clean
    _dl._demo_files_cache = None

    with patch("mammamiradio.downloader._DEMO_ASSETS_DIR", demo_dir):
        result1 = _find_demo_asset(track)
        # Second call: cache should be set, glob should NOT be called again
        result2 = _find_demo_asset(track)

    assert result1 == mp3
    assert result2 == mp3


# ---------------------------------------------------------------------------
# _find_local: TTL cache hit path
# ---------------------------------------------------------------------------


def test_find_local_uses_ttl_cache(tmp_path):
    """_find_local must return a cached file list within the TTL window."""
    import mammamiradio.downloader as _dl
    from mammamiradio.downloader import _find_local

    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(title="Cached Song", artist="Artist", duration_ms=180000)
    mp3 = music_dir / f"{track.title.lower()}.mp3"
    mp3.touch()

    # Prime the cache
    key = str(music_dir)
    import time as _time
    _dl._local_files_cache[key] = (_time.time(), [mp3])

    result = _find_local(track, music_dir)
    assert result == mp3


# ---------------------------------------------------------------------------
# _download_external_sync: cache hit, local file, ytdlp disabled
# ---------------------------------------------------------------------------


def test_download_external_sync_cache_hit(tmp_path):
    """_download_external_sync must return the cached file immediately."""
    from mammamiradio.downloader import _download_external_sync

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(title="Cached", artist="Artist", duration_ms=180000)
    cached = cache_dir / f"{track.cache_key}.mp3"
    cached.touch()

    result = _download_external_sync(track, cache_dir, music_dir)
    assert result == cached


def test_download_external_sync_local_file(tmp_path):
    """_download_external_sync must find a local file when cache misses."""
    from mammamiradio.downloader import _download_external_sync

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(title="Local Song", artist="Artist", duration_ms=180000)
    local = music_dir / f"{track.title.lower()}.mp3"
    local.touch()

    with patch.dict("os.environ", {"MAMMAMIRADIO_ALLOW_YTDLP": "false"}):
        result = _download_external_sync(track, cache_dir, music_dir)
    assert result == local


def test_download_external_sync_raises_when_ytdlp_disabled(tmp_path):
    """_download_external_sync must raise RuntimeError when yt-dlp is disabled and no cache/local."""
    from mammamiradio.downloader import _download_external_sync

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(title="Unavailable", artist="Nobody", duration_ms=180000)

    with patch.dict("os.environ", {"MAMMAMIRADIO_ALLOW_YTDLP": "false"}):
        with pytest.raises(RuntimeError, match="yt-dlp is disabled"):
            _download_external_sync(track, cache_dir, music_dir)


# ---------------------------------------------------------------------------
# download_external_track: async wrapper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_external_track_returns_path(tmp_path):
    """download_external_track must return the path from the sync helper via executor."""
    from mammamiradio.downloader import download_external_track

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    track = Track(title="Async Song", artist="Artist", duration_ms=180000)
    expected = cache_dir / f"{track.cache_key}.mp3"
    expected.touch()

    result = await download_external_track(track, cache_dir, music_dir=tmp_path / "music")
    assert result == expected
