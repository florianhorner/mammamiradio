"""Tests for downloader module: cache/local acquisition and unavailable-source markers."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from mammamiradio.core.models import Track


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
    from mammamiradio.playlist.downloader import validate_download

    file_path = tmp_path / "tiny.mp3"
    file_path.write_bytes(b"x" * 100)

    with patch("mammamiradio.playlist.downloader.subprocess.run") as mock_run:
        ok, reason = validate_download(file_path)

    assert ok is False
    assert "too small" in reason
    mock_run.assert_not_called()


def test_validate_download_accepts_valid_duration(tmp_path):
    from mammamiradio.playlist.downloader import validate_download

    file_path = tmp_path / "good.mp3"
    file_path.write_bytes(b"x" * (600 * 1024))
    result = MagicMock()
    result.returncode = 0
    result.stdout = '{"format":{"duration":"180.2"}}'

    with patch("mammamiradio.playlist.downloader.subprocess.run", return_value=result) as mock_run:
        ok, reason = validate_download(file_path)

    assert ok is True
    assert reason == "ok"
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[:5] == ["ffprobe", "-v", "quiet", "-print_format", "json"]
    assert str(file_path) in cmd


def test_find_local_returns_none_when_dir_missing(track, tmp_path):
    from mammamiradio.playlist.downloader import _find_local

    result = _find_local(track, tmp_path / "nonexistent")
    assert result is None


def test_find_local_returns_none_when_no_match(track, music_dir):
    from mammamiradio.playlist.downloader import _find_local

    (music_dir / "unrelated_song.mp3").touch()
    result = _find_local(track, music_dir)
    assert result is None


def test_find_local_matches_by_cache_key(track, music_dir):
    from mammamiradio.playlist.downloader import _find_local

    # Create a file whose name contains the cache_key
    mp3 = music_dir / f"{track.cache_key}.mp3"
    mp3.touch()
    result = _find_local(track, music_dir)
    assert result == mp3


def test_find_local_matches_by_title(track, music_dir):
    from mammamiradio.playlist.downloader import _find_local

    mp3 = music_dir / f"{track.title.lower()}.mp3"
    mp3.touch()
    result = _find_local(track, music_dir)
    assert result == mp3


def test_find_local_bounds_raw_directory_entries_before_mp3_filter(track, music_dir):
    import mammamiradio.playlist.downloader as downloader

    yielded = 0

    class CountingScandir:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            nonlocal yielded
            for index in range(100):
                yielded += 1
                yield SimpleNamespace(
                    name=f"not-music-{index}.txt",
                    path=str(music_dir / f"not-music-{index}.txt"),
                    is_file=lambda: True,
                )

    downloader._local_files_cache.pop(str(music_dir), None)
    with (
        patch("mammamiradio.playlist.downloader.os.scandir", return_value=CountingScandir()),
        patch("mammamiradio.playlist.downloader._LOCAL_DIRECTORY_ENTRY_LIMIT", 8),
    ):
        result = downloader._find_local(track, music_dir)

    assert result is None
    assert yielded == 8


def test_find_local_treats_unreadable_directory_as_no_match(track, music_dir):
    import mammamiradio.playlist.downloader as downloader

    downloader._local_files_cache.pop(str(music_dir), None)
    with patch("mammamiradio.playlist.downloader.os.scandir", side_effect=PermissionError("denied")):
        result = downloader._find_local(track, music_dir)

    assert result is None


def test_resolver_never_consults_unmanifested_demo_music(track, cache_dir, music_dir, tmp_path):
    import mammamiradio.playlist.downloader as downloader

    demo_file = tmp_path / "assets" / "demo" / "music" / f"{track.cache_key}.mp3"
    demo_file.parent.mkdir(parents=True)
    demo_file.touch()

    # Even if a future edit accidentally reintroduces a matching helper, the
    # concrete-source resolver must not call it or admit those bytes.
    with patch.object(downloader, "_find_demo_asset", return_value=demo_file, create=True) as demo_lookup:
        result = downloader._resolve_cached_or_local(track, cache_dir, music_dir)

    assert result is None
    demo_lookup.assert_not_called()


# --- _download_sync: cache hit ---


def test_cache_hit_returns_immediately(track, cache_dir, music_dir):
    from mammamiradio.playlist.downloader import _download_sync

    cached = cache_dir / f"{track.cache_key}.mp3"
    cached.write_text("fake audio")
    (cache_dir / f"_failed_{track.cache_key}.mp3").write_text("prior failure")

    with patch("mammamiradio.playlist.downloader._ytdlp_enabled", return_value=True):
        result = _download_sync(track, cache_dir, music_dir)
    assert result == cached


def test_external_cache_is_ineligible_when_effective_gate_is_off(track, cache_dir, music_dir):
    from mammamiradio.playlist.downloader import _download_sync

    cached = cache_dir / f"{track.cache_key}.mp3"
    cached.write_text("previously downloaded audio")

    with patch("mammamiradio.playlist.downloader._ytdlp_enabled", return_value=False):
        result = _download_sync(track, cache_dir, music_dir)

    assert result != cached
    assert result.name == f"_failed_{track.cache_key}.mp3"
    assert result.read_text() == "yt-dlp disabled"


# --- _download_sync: local file found ---


def test_local_file_found(track, cache_dir, music_dir):
    from mammamiradio.playlist.downloader import _download_sync

    local_mp3 = music_dir / f"{track.cache_key}.mp3"
    local_mp3.write_text("local audio")
    (cache_dir / f"_failed_{track.cache_key}.mp3").write_text("prior failure")

    result = _download_sync(track, cache_dir, music_dir)
    assert result == local_mp3


def test_download_sync_ignores_unmanifested_demo_asset(track, cache_dir, music_dir, tmp_path):
    from mammamiradio.playlist.downloader import _download_sync

    demo_dir = tmp_path / "demo_assets" / "music"
    demo_dir.mkdir(parents=True)
    demo_file = demo_dir / f"{track.cache_key}.mp3"
    demo_file.write_text("demo audio")
    local_file = music_dir / f"{track.cache_key}.mp3"
    local_file.write_text("local audio")
    (cache_dir / f"_failed_{track.cache_key}.mp3").write_text("prior failure")

    result = _download_sync(track, cache_dir, music_dir)

    assert result == local_file
    assert demo_file.exists()


# --- _download_sync: yt-dlp success ---


def test_ytdlp_disabled_by_default(track, cache_dir, music_dir):
    """yt-dlp-disabled tracks are marked unavailable, never rendered as silence."""
    import os

    from mammamiradio.playlist.downloader import _download_sync

    env = os.environ.copy()
    env.pop("MAMMAMIRADIO_ALLOW_YTDLP", None)

    with patch.dict(os.environ, env, clear=True):
        result = _download_sync(track, cache_dir, music_dir)

    assert result == cache_dir / f"_failed_{track.cache_key}.mp3"
    assert result.read_text() == "yt-dlp disabled"


def test_ytdlp_success_when_enabled(track, cache_dir, music_dir):
    """yt-dlp runs when MAMMAMIRADIO_ALLOW_YTDLP=true."""
    import os

    from mammamiradio.playlist.downloader import _download_sync

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
    from mammamiradio.playlist.downloader import _download_ytdlp

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


def test_ytdlp_sets_socket_timeout(track, cache_dir):
    """G1: _download_ytdlp wires socket_timeout so a stalled socket cannot hang forever."""
    from mammamiradio.playlist.downloader import _YTDLP_SOCKET_TIMEOUT_SEC, _download_ytdlp

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
        _download_ytdlp(track, cache_dir)

    assert captured_opts["socket_timeout"] == _YTDLP_SOCKET_TIMEOUT_SEC


def test_ytdlp_cleans_up_temp_dir_on_success(track, cache_dir):
    """Temp fragment dir is removed after a successful download."""
    import sys

    from mammamiradio.playlist.downloader import _download_ytdlp

    class _FakeYoutubeDL:
        def __init__(self, _opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def download(self, _queries):
            (cache_dir / f"{track.cache_key}.mp3").write_text("audio")

    mock_yt_dlp = MagicMock()
    mock_yt_dlp.YoutubeDL = _FakeYoutubeDL

    with patch.dict(sys.modules, {"yt_dlp": mock_yt_dlp}):
        _download_ytdlp(track, cache_dir)

    assert not (cache_dir / ".ytdlp_tmp" / track.cache_key).exists()


def test_ytdlp_cleans_up_temp_dir_on_failure(track, cache_dir):
    """Temp fragment dir is removed even when the download raises."""
    import sys

    import pytest

    from mammamiradio.playlist.downloader import _download_ytdlp

    class _FakeYoutubeDL:
        def __init__(self, _opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def download(self, _queries):
            raise RuntimeError("network error")

    mock_yt_dlp = MagicMock()
    mock_yt_dlp.YoutubeDL = _FakeYoutubeDL

    with patch.dict(sys.modules, {"yt_dlp": mock_yt_dlp}), pytest.raises(RuntimeError):
        _download_ytdlp(track, cache_dir)

    assert not (cache_dir / ".ytdlp_tmp" / track.cache_key).exists()


def test_download_ytdlp_uses_exact_watch_url_when_youtube_id(cache_dir):
    from mammamiradio.playlist.downloader import _download_ytdlp

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


# --- _download_sync: yt-dlp failure marks source unavailable ---


def test_ytdlp_403_marks_track_unavailable_without_rendering_silence(track, cache_dir, music_dir):
    import os
    from urllib.error import HTTPError

    from mammamiradio.playlist.downloader import _download_sync

    mock_yt_dlp = MagicMock()
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.download.side_effect = HTTPError(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        403,
        "Forbidden",
        hdrs=None,
        fp=None,
    )
    mock_yt_dlp.YoutubeDL.return_value = mock_ydl_instance

    with (
        patch.dict(os.environ, {"MAMMAMIRADIO_ALLOW_YTDLP": "true"}),
        patch.dict(sys.modules, {"yt_dlp": mock_yt_dlp}),
    ):
        result = _download_sync(track, cache_dir, music_dir)

    expected_path = cache_dir / f"_failed_{track.cache_key}.mp3"
    assert result == expected_path
    assert result.read_text() == "yt-dlp failed: HTTP Error 403: Forbidden"
    assert not (cache_dir / f"_silence_{track.cache_key}.mp3").exists()


def test_ytdlp_socket_timeout_marks_track_unavailable(track, cache_dir, music_dir):
    """A stalled download is skipped so the recovery ladder supplies real audio."""
    import os

    from mammamiradio.playlist.downloader import _download_sync

    mock_yt_dlp = MagicMock()
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.download.side_effect = TimeoutError("timed out")
    mock_yt_dlp.YoutubeDL.return_value = mock_ydl_instance

    with (
        patch.dict(os.environ, {"MAMMAMIRADIO_ALLOW_YTDLP": "true"}),
        patch.dict(sys.modules, {"yt_dlp": mock_yt_dlp}),
    ):
        result = _download_sync(track, cache_dir, music_dir)

    assert result == cache_dir / f"_failed_{track.cache_key}.mp3"
    assert result.read_text() == "yt-dlp failed: timed out"


# --- _download_sync: yt-dlp not installed marks source unavailable ---


def test_ytdlp_import_error_marks_track_unavailable(track, cache_dir, music_dir):
    import os

    from mammamiradio.playlist.downloader import _download_sync

    # Remove yt_dlp from sys.modules if present so the lazy import triggers ImportError
    with (
        patch.dict(os.environ, {"MAMMAMIRADIO_ALLOW_YTDLP": "true"}),
        patch.dict(sys.modules, {"yt_dlp": None}),
    ):
        result = _download_sync(track, cache_dir, music_dir)

    expected_path = cache_dir / f"_failed_{track.cache_key}.mp3"
    assert result == expected_path
    assert result.read_text() == "yt-dlp disabled"


def test_ytdlp_disabled_uses_failed_marker_not_real_cache_path(track, cache_dir, music_dir):
    """Unavailable tracks never occupy the real cache slot or synthesize audio."""
    import os

    from mammamiradio.playlist.downloader import _download_sync

    with patch.dict(os.environ, {"MAMMAMIRADIO_ALLOW_YTDLP": "false"}):
        result = _download_sync(track, cache_dir, music_dir)

    assert result.name != f"{track.cache_key}.mp3"
    assert result.name == f"_failed_{track.cache_key}.mp3"
    assert str(cache_dir) in str(result)


# --- download_track async wrapper ---


@pytest.mark.asyncio
async def test_download_track_async(track, cache_dir, music_dir):
    from mammamiradio.playlist.downloader import download_track

    # Put a file in cache so the sync function returns immediately
    cached = cache_dir / f"{track.cache_key}.mp3"
    cached.write_text("cached audio")

    with patch("mammamiradio.playlist.downloader._ytdlp_enabled", return_value=True):
        result = await download_track(track, cache_dir, music_dir)
    assert result == cached


@pytest.mark.asyncio
async def test_async_download_defaults_honor_music_dir_environment(track, cache_dir, tmp_path, monkeypatch):
    from mammamiradio.playlist.downloader import download_external_track, download_track

    configured_music = tmp_path / "mounted-music"
    monkeypatch.setenv("MAMMAMIRADIO_MUSIC_DIR", str(configured_music))
    expected = cache_dir / "result.mp3"
    with (
        patch("mammamiradio.playlist.downloader._download_sync", return_value=expected) as regular,
        patch("mammamiradio.playlist.downloader._download_external_sync", return_value=expected) as external,
    ):
        assert await download_track(track, cache_dir) == expected
        assert await download_external_track(track, cache_dir) == expected

    assert regular.call_args.args[2] == configured_music
    assert external.call_args.args[2] == configured_music


def test_download_external_sync_raises_when_ytdlp_disabled(track, cache_dir, music_dir):
    import os

    from mammamiradio.playlist.downloader import _download_external_sync

    env = os.environ.copy()
    env.pop("MAMMAMIRADIO_ALLOW_YTDLP", None)

    with patch.dict(os.environ, env, clear=True), pytest.raises(RuntimeError, match="yt-dlp is disabled"):
        _download_external_sync(track, cache_dir, music_dir)


# --- evict_cache_lru ---


def test_evict_cache_lru_zero_limit_is_noop(cache_dir):
    from mammamiradio.playlist.downloader import evict_cache_lru

    (cache_dir / "a.mp3").write_bytes(b"x" * 1024 * 1024)
    evict_cache_lru(cache_dir, 0)
    assert (cache_dir / "a.mp3").exists()


def test_evict_cache_lru_under_limit_noop(cache_dir):
    from mammamiradio.playlist.downloader import evict_cache_lru

    (cache_dir / "a.mp3").write_bytes(b"x" * 1024 * 1024)
    evict_cache_lru(cache_dir, 100)
    assert (cache_dir / "a.mp3").exists()


def test_evict_cache_lru_over_limit_removes_oldest(cache_dir):
    import time

    from mammamiradio.playlist.downloader import evict_cache_lru

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
    from mammamiradio.playlist.downloader import evict_cache_lru

    protected = ["mammamiradio.db", "playlist_source.json", "session_stopped.flag"]
    for name in protected:
        (cache_dir / name).write_bytes(b"x" * 1024 * 1024)
    (cache_dir / "track.mp3").write_bytes(b"x" * 1024 * 1024)
    evict_cache_lru(cache_dir, 0)  # even with 0 limit, protected files survive
    for name in protected:
        assert (cache_dir / name).exists()


def test_evict_cache_lru_handles_oserror(cache_dir):
    from unittest.mock import patch

    from mammamiradio.playlist.downloader import evict_cache_lru

    f = cache_dir / "broken.mp3"
    f.write_bytes(b"x" * 1024 * 1024)
    with patch("pathlib.Path.unlink", side_effect=OSError("permission denied")):
        # Should not raise — logs warning and continues
        evict_cache_lru(cache_dir, 0.0001)


def test_evict_cache_lru_keeps_processed_audio_over_regular(cache_dir):
    """Transient/regular cache files evict before processed audio — an fm_ broadcast-chain
    bake (which may be queued/airing) is kept over a newer regular file, matching the
    norm_ safety baseline so eviction can't pull a baked render out from under playback."""
    import time

    from mammamiradio.playlist.downloader import evict_cache_lru

    fm = cache_dir / "fm_norm_song_v1.mp3"
    reg = cache_dir / "download_tmp.mp3"
    fm.write_bytes(b"x" * 1024 * 1024)  # older
    time.sleep(0.02)
    reg.write_bytes(b"x" * 1024 * 1024)  # newer, but a regular file
    evict_cache_lru(cache_dir, 1)  # 2 MB -> 1 MB: evict one
    assert not reg.exists()  # regular evicted first despite being newer
    assert fm.exists()  # fm_ bake (processed bucket) survives


def test_evict_cache_lru_treats_synth_cache_as_regular(cache_dir):
    from mammamiradio.playlist.downloader import evict_cache_lru

    norm = cache_dir / "norm_track_192k.mp3"
    synth = cache_dir / "synth_music_bed_abc123.mp3"
    norm.write_bytes(b"x" * 700 * 1024)
    synth.write_bytes(b"x" * 700 * 1024)

    evict_cache_lru(cache_dir, 1)

    assert norm.exists()
    assert not synth.exists()


def test_evict_cache_lru_protects_queued_fm_bake(cache_dir):
    """A baked render currently queued/airing (passed in protected_paths) is never evicted,
    even under a zero budget — the queued-path protection covers fm_ bakes too."""
    from mammamiradio.playlist.downloader import evict_cache_lru

    fm = cache_dir / "fm_norm_song_v1.mp3"
    fm.write_bytes(b"x" * 1024 * 1024)
    evict_cache_lru(cache_dir, 0.0001, protected_paths={fm})
    assert fm.exists()


def test_evict_cache_lru_evicts_regular_before_norm(cache_dir):
    """Regular files are evicted before norm cache; norm evicted if still over budget."""
    from mammamiradio.playlist.downloader import evict_cache_lru

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
    from mammamiradio.playlist.downloader import evict_cache_lru

    norm = cache_dir / "norm_track_192k.mp3"
    norm.write_bytes(b"x" * 700 * 1024)

    # Budget is tiny — norm must be evicted
    evict_cache_lru(cache_dir, 0.1)
    assert not norm.exists()


# --- search_ytdlp_metadata ---


def test_search_ytdlp_metadata_disabled_returns_empty():
    from mammamiradio.playlist.downloader import search_ytdlp_metadata

    with patch.dict("os.environ", {"MAMMAMIRADIO_ALLOW_YTDLP": "false"}):
        assert search_ytdlp_metadata("vasco", max_results=3) == []


def test_external_media_gate_requires_both_opt_in_and_optional_module():
    from mammamiradio.playlist.downloader import external_media_enabled

    with patch("mammamiradio.playlist.downloader._load_external_media_module") as load:
        assert external_media_enabled(configured=False) is False
        load.assert_not_called()

    with patch(
        "mammamiradio.playlist.downloader._load_external_media_module",
        side_effect=RuntimeError("optional module missing"),
    ):
        assert external_media_enabled(configured=True) is False

    with patch("mammamiradio.playlist.downloader._load_external_media_module", return_value=MagicMock()):
        assert external_media_enabled(configured=True) is True


def test_search_ytdlp_metadata_import_error_returns_empty():
    from mammamiradio.playlist.downloader import search_ytdlp_metadata

    with (
        patch.dict("os.environ", {"MAMMAMIRADIO_ALLOW_YTDLP": "true"}),
        patch.dict(sys.modules, {"yt_dlp": None}),
    ):
        assert search_ytdlp_metadata("vasco", max_results=3) == []


def test_search_ytdlp_metadata_success_parses_entries():
    from mammamiradio.playlist.downloader import search_ytdlp_metadata

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
                    {
                        "id": "albachiar01",
                        "title": "Albachiara",
                        "uploader": "Vasco Rossi",
                        "duration": 123,
                        "thumbnail": "https://img.example/albachiara.jpg",
                    },
                    {
                        "id": "volare00001",
                        "title": "Volare",
                        "channel": "Modugno Channel",
                        "duration": 0,
                        "thumbnails": [
                            {"url": "https://img.example/small.jpg"},
                            {"url": "https://img.example/large.jpg"},
                        ],
                    },
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
    assert results[0]["youtube_id"] == "albachiar01"
    assert results[0]["artist"] == "Vasco Rossi"
    assert results[0]["duration_ms"] == 123000
    assert results[0]["album_art"] == "https://img.example/albachiara.jpg"
    assert results[1]["youtube_id"] == "volare00001"
    assert results[1]["artist"] == "Modugno Channel"
    assert results[1]["album_art"] == "https://img.example/large.jpg"


def test_search_ytdlp_metadata_sets_socket_timeout():
    """G2: the metadata search wires socket_timeout so a stalled socket cannot hang forever."""
    from mammamiradio.playlist.downloader import _YTDLP_SOCKET_TIMEOUT_SEC, search_ytdlp_metadata

    captured_opts = {}

    class _FakeYoutubeDL:
        def __init__(self, opts):
            captured_opts.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, query, download=False):
            return {"entries": []}

    mock_yt_dlp = MagicMock()
    mock_yt_dlp.YoutubeDL = _FakeYoutubeDL

    with (
        patch.dict("os.environ", {"MAMMAMIRADIO_ALLOW_YTDLP": "true"}),
        patch.dict(sys.modules, {"yt_dlp": mock_yt_dlp}),
    ):
        search_ytdlp_metadata("vasco", max_results=2)

    assert captured_opts["socket_timeout"] == _YTDLP_SOCKET_TIMEOUT_SEC


# --- purge_suspect_cache_files ---


def test_purge_suspect_cache_files_empty_dir(tmp_path):
    from mammamiradio.playlist.downloader import purge_suspect_cache_files

    d = tmp_path / "cache"
    d.mkdir()
    assert purge_suspect_cache_files(d) == 0


def test_purge_suspect_cache_files_nonexistent_dir(tmp_path):
    from mammamiradio.playlist.downloader import purge_suspect_cache_files

    assert purge_suspect_cache_files(tmp_path / "nope") == 0


def test_purge_suspect_cache_files_removes_small_files(tmp_path):
    from mammamiradio.playlist.downloader import purge_suspect_cache_files

    d = tmp_path / "cache"
    d.mkdir()
    small = d / "bad_download.mp3"
    small.write_bytes(b"x" * 100)  # well below 10240
    assert purge_suspect_cache_files(d) == 1
    assert not small.exists()


def test_purge_suspect_cache_files_keeps_small_synth_cache_files(tmp_path):
    from mammamiradio.playlist.downloader import purge_suspect_cache_files

    d = tmp_path / "cache"
    d.mkdir()
    synth = d / "synth_foley_abc123.mp3"
    failed = d / "_failed_track.mp3"
    silence = d / "_silence_track.mp3"
    tiny = d / "tiny.mp3"
    for path in (synth, failed, silence, tiny):
        path.write_bytes(b"x" * 100)

    assert purge_suspect_cache_files(d) == 3
    assert synth.exists()
    assert not failed.exists()
    assert not silence.exists()
    assert not tiny.exists()


def test_purge_suspect_cache_files_keeps_large_files(tmp_path):
    from mammamiradio.playlist.downloader import purge_suspect_cache_files

    d = tmp_path / "cache"
    d.mkdir()
    big = d / "good_track.mp3"
    big.write_bytes(b"x" * 10240)  # exactly at threshold
    assert purge_suspect_cache_files(d) == 0
    assert big.exists()


def test_purge_suspect_cache_files_skips_protected(tmp_path):
    from mammamiradio.playlist.downloader import purge_suspect_cache_files

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

    from mammamiradio.playlist.downloader import purge_suspect_cache_files

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
    from mammamiradio.playlist.downloader import _CACHE_PROTECTED, purge_suspect_cache_files

    d = tmp_path / "cache"
    d.mkdir()
    protected_name = next(iter(_CACHE_PROTECTED))
    fake_file = d / protected_name
    fake_file.write_bytes(b"x" * 10)

    with patch.object(type(d), "glob", return_value=[fake_file]):
        assert purge_suspect_cache_files(d) == 0
        assert fake_file.exists()


def test_purge_suspect_cache_files_purges_tiny_norm_cache_files(tmp_path):
    from mammamiradio.playlist.downloader import purge_suspect_cache_files

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
    from mammamiradio.playlist.downloader import purge_suspect_cache_files

    d = tmp_path / "cache"
    d.mkdir()
    norm = d / "norm_song_192k.mp3"
    norm.write_bytes(b"x" * 20000)

    purged = purge_suspect_cache_files(d)
    assert purged == 0
    assert norm.exists()


def test_purge_suspect_cache_files_custom_threshold(tmp_path):
    from mammamiradio.playlist.downloader import purge_suspect_cache_files

    d = tmp_path / "cache"
    d.mkdir()
    f = d / "medium.mp3"
    f.write_bytes(b"x" * 500)
    # With higher threshold, this should be purged
    assert purge_suspect_cache_files(d, min_size_bytes=1000) == 1
    assert not f.exists()


def test_reject_cached_download_purges_and_denylists(tmp_path):
    """WS5: rejected downloads must be removed from cache AND flagged for the session.

    Without purging, the next selection of the same track returns the broken
    file from ``cache_dir/{cache_key}.mp3`` and the quality gate rejects it
    again forever — the endless rejection loop the plan explicitly calls out.
    """
    from mammamiradio.playlist.downloader import (
        clear_rejected_cache_keys,
        is_rejected_cache_key,
        reject_cached_download,
    )

    clear_rejected_cache_keys()
    try:
        cache = tmp_path / "cache"
        cache.mkdir()
        cache_key = "poisoned_key_abc"
        cached = cache / f"{cache_key}.mp3"
        cached.write_bytes(b"x" * 2048)
        norm_cached = cache / f"norm_{cache_key}_192k.mp3"
        norm_cached.write_bytes(b"normalized poison")
        norm_sidecar = cache / f"{norm_cached.name}.json"
        norm_sidecar.write_text('{"title":"Poison","artist":"Cache"}')
        fm_bake = cache / f"fm_norm_{cache_key}_192k_v1_123_456.mp3"
        fm_bake.write_bytes(b"baked poison")

        assert not is_rejected_cache_key(cache_key)

        removed = reject_cached_download(cache, cache_key, "duration too short (8.2s)")

        assert removed is True
        assert not cached.exists(), "rejected cache file must be purged"
        assert not norm_cached.exists(), "rejected norm-cache rescue file must be purged"
        assert not norm_sidecar.exists(), "rejected norm-cache sidecar must be purged"
        assert not fm_bake.exists(), "rejected broadcast-chain bake must be purged"
        assert is_rejected_cache_key(cache_key), "rejected key must be denylisted"
    finally:
        clear_rejected_cache_keys()


def test_reject_cached_download_is_no_op_without_cache_key(tmp_path):
    """Empty cache_key short-circuits (no IO, no denylist entry)."""
    from mammamiradio.playlist.downloader import (
        clear_rejected_cache_keys,
        is_rejected_cache_key,
        reject_cached_download,
    )

    clear_rejected_cache_keys()
    cache = tmp_path / "cache"
    cache.mkdir()

    assert reject_cached_download(cache, "", "no key") is False
    assert not is_rejected_cache_key("")


def test_reject_cached_download_denylists_even_if_file_missing(tmp_path):
    """Denylisting must survive even when the cache file was already gone.

    If another task removed the file first, we still need the key blacklisted
    so the producer skips re-downloading it.
    """
    from mammamiradio.playlist.downloader import (
        clear_rejected_cache_keys,
        is_rejected_cache_key,
        reject_cached_download,
    )

    clear_rejected_cache_keys()
    try:
        cache = tmp_path / "cache"
        cache.mkdir()
        removed = reject_cached_download(cache, "ghost_key", "never existed")
        assert removed is False
        assert is_rejected_cache_key("ghost_key"), "key must still be denylisted even when the file was already gone"
    finally:
        clear_rejected_cache_keys()


def test_reject_cached_download_marks_local_recovery_boundary(tmp_path):
    """A rejected local file can be retried once only after replacement."""
    import os

    from mammamiradio.playlist.downloader import (
        clear_rejected_cache_keys,
        has_fresh_concrete_track_source,
        reject_cached_download,
    )

    cache = tmp_path / "cache"
    cache.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(title="Local Boundary", artist="Local Artist", duration_ms=180000, source="local")
    local_file = music_dir / f"{track.cache_key}.mp3"
    local_file.write_bytes(b"corrupt local audio")

    clear_rejected_cache_keys()
    try:
        reject_cached_download(cache, track.cache_key, "invalid local audio")

        marker = cache / f"_failed_{track.cache_key}.mp3"
        assert marker.read_text() == "invalid local audio"
        marker_mtime = marker.stat().st_mtime_ns
        os.utime(local_file, ns=(marker_mtime - 1, marker_mtime - 1))
        assert not has_fresh_concrete_track_source(track, cache, music_dir)

        os.utime(local_file, ns=(marker_mtime + 1, marker_mtime + 1))
        assert has_fresh_concrete_track_source(track, cache, music_dir)
    finally:
        clear_rejected_cache_keys()


def test_reject_cached_download_tolerates_unlink_errors(tmp_path):
    """OSError on unlink must not crash — denylist must still be populated."""
    from mammamiradio.playlist.downloader import (
        clear_rejected_cache_keys,
        is_rejected_cache_key,
        reject_cached_download,
    )

    clear_rejected_cache_keys()
    try:
        cache = tmp_path / "cache"
        cache.mkdir()
        cache_key = "err_key"
        (cache / f"{cache_key}.mp3").write_bytes(b"x" * 2048)

        with patch("mammamiradio.playlist.downloader.Path.unlink", side_effect=OSError("simulated permission denied")):
            removed = reject_cached_download(cache, cache_key, "simulated")

        assert removed is False
        assert is_rejected_cache_key(cache_key), "denylist must be populated even when unlink raises OSError"
    finally:
        clear_rejected_cache_keys()


def test_reject_cached_download_tolerates_failed_marker_write(tmp_path):
    """A failure-marker refresh is best effort and must preserve the denylist."""
    from mammamiradio.playlist.downloader import (
        clear_rejected_cache_keys,
        is_rejected_cache_key,
        reject_cached_download,
    )

    cache = tmp_path / "cache"
    cache.mkdir()
    cache_key = "marker_write_error"
    (cache / f"_failed_{cache_key}.mp3").write_text("prior failure")

    clear_rejected_cache_keys()
    try:
        with patch("mammamiradio.playlist.downloader.Path.write_text", side_effect=OSError("read-only")):
            removed = reject_cached_download(cache, cache_key, "new failure")

        assert removed is False
        assert is_rejected_cache_key(cache_key)
    finally:
        clear_rejected_cache_keys()


def test_accept_recovered_download_is_no_op_without_cache_key(tmp_path):
    from mammamiradio.playlist.downloader import (
        accept_recovered_download,
        clear_rejected_cache_keys,
        is_rejected_cache_key,
    )

    clear_rejected_cache_keys()
    accept_recovered_download(tmp_path, "")
    assert not is_rejected_cache_key("")


def test_accept_recovered_download_tolerates_marker_cleanup_error(tmp_path):
    from mammamiradio.playlist.downloader import (
        accept_recovered_download,
        clear_rejected_cache_keys,
        is_rejected_cache_key,
        reject_cached_download,
    )

    cache = tmp_path / "cache"
    cache.mkdir()
    cache_key = "marker_cleanup_error"
    marker = cache / f"_failed_{cache_key}.mp3"
    marker.write_text("prior failure")

    clear_rejected_cache_keys()
    try:
        reject_cached_download(cache, cache_key, "prior failure")
        with patch("mammamiradio.playlist.downloader.Path.unlink", side_effect=OSError("read-only")):
            accept_recovered_download(cache, cache_key)

        assert not is_rejected_cache_key(cache_key)
        assert marker.exists()
    finally:
        clear_rejected_cache_keys()


def test_clear_rejected_cache_keys_empties_session_denylist():
    """Explicit reset must empty the set."""
    from pathlib import Path as _Path

    from mammamiradio.playlist.downloader import (
        clear_rejected_cache_keys,
        is_rejected_cache_key,
        reject_cached_download,
    )

    reject_cached_download(_Path("/tmp"), "some_key", "test")
    assert is_rejected_cache_key("some_key")
    clear_rejected_cache_keys()
    assert not is_rejected_cache_key("some_key")


def test_purge_suspect_cache_files_removes_silence_placeholders(tmp_path):
    """_silence_*.mp3 files must always be purged regardless of size."""
    from mammamiradio.playlist.downloader import purge_suspect_cache_files

    d = tmp_path / "cache"
    d.mkdir()
    # A large silence placeholder — size alone would not trigger the small-file heuristic
    silence = d / "_silence_some_track_key.mp3"
    silence.write_bytes(b"x" * 900_000)  # ~900KB, well above default 10KB threshold
    # A regular large file that should NOT be purged
    real = d / "real_track.mp3"
    real.write_bytes(b"x" * 900_000)

    purged = purge_suspect_cache_files(d)

    assert purged == 1
    assert not silence.exists(), "_silence_ placeholder must be deleted"
    assert real.exists(), "real track cache must be preserved"


def test_download_ytdlp_raises_when_no_output_file(cache_dir):
    """_download_ytdlp raises FileNotFoundError when yt-dlp doesn't create the output."""
    from mammamiradio.playlist.downloader import _download_ytdlp

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
    from mammamiradio.playlist.downloader import search_ytdlp_metadata

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

    from mammamiradio.playlist.downloader import validate_download

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
    from mammamiradio.playlist.downloader import validate_download

    p = tmp_path / "track.mp3"
    p.write_bytes(b"x" * (600 * 1024))

    import subprocess as _sp

    with patch("mammamiradio.playlist.downloader.subprocess.run", side_effect=_sp.TimeoutExpired("ffprobe", 30)):
        ok, reason = validate_download(p)
    assert ok is False
    assert "timed out" in reason


def test_validate_download_ffprobe_oserror(tmp_path):
    from mammamiradio.playlist.downloader import validate_download

    p = tmp_path / "track.mp3"
    p.write_bytes(b"x" * (600 * 1024))

    with patch("mammamiradio.playlist.downloader.subprocess.run", side_effect=OSError("not found")):
        ok, reason = validate_download(p)
    assert ok is False
    assert "failed to start" in reason


def test_validate_download_ffprobe_nonzero_returncode(tmp_path):
    from mammamiradio.playlist.downloader import validate_download

    p = tmp_path / "track.mp3"
    p.write_bytes(b"x" * (600 * 1024))
    result = MagicMock()
    result.returncode = 1
    result.stdout = ""

    with patch("mammamiradio.playlist.downloader.subprocess.run", return_value=result):
        ok, reason = validate_download(p)
    assert ok is False
    assert "ffprobe failed" in reason


def test_validate_download_json_decode_error(tmp_path):
    from mammamiradio.playlist.downloader import validate_download

    p = tmp_path / "track.mp3"
    p.write_bytes(b"x" * (600 * 1024))
    result = MagicMock()
    result.returncode = 0
    result.stdout = "NOT JSON {"

    with patch("mammamiradio.playlist.downloader.subprocess.run", return_value=result):
        ok, reason = validate_download(p)
    assert ok is False
    assert "invalid JSON" in reason


def test_validate_download_missing_duration(tmp_path):
    from mammamiradio.playlist.downloader import validate_download

    p = tmp_path / "track.mp3"
    p.write_bytes(b"x" * (600 * 1024))
    result = MagicMock()
    result.returncode = 0
    result.stdout = '{"format": {}}'

    with patch("mammamiradio.playlist.downloader.subprocess.run", return_value=result):
        ok, reason = validate_download(p)
    assert ok is False
    assert "missing duration" in reason


def test_validate_download_invalid_duration_float(tmp_path):
    from mammamiradio.playlist.downloader import validate_download

    p = tmp_path / "track.mp3"
    p.write_bytes(b"x" * (600 * 1024))
    result = MagicMock()
    result.returncode = 0
    result.stdout = '{"format": {"duration": "not_a_number"}}'

    with patch("mammamiradio.playlist.downloader.subprocess.run", return_value=result):
        ok, reason = validate_download(p)
    assert ok is False
    assert "invalid duration" in reason


# ---------------------------------------------------------------------------
# evict_cache_lru: protected file in cache dir is skipped
# ---------------------------------------------------------------------------


def test_evict_cache_lru_skips_protected_files(tmp_path):
    """Files whose names are in _CACHE_PROTECTED must never be evicted."""
    from mammamiradio.playlist.downloader import _CACHE_PROTECTED, evict_cache_lru

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
# _find_local: TTL cache hit path
# ---------------------------------------------------------------------------


def test_find_local_uses_ttl_cache(tmp_path):
    """_find_local must return a cached file list within the TTL window."""
    import mammamiradio.playlist.downloader as _dl
    from mammamiradio.playlist.downloader import _find_local

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
    """Enabled external media may reuse its own cached file."""
    from mammamiradio.playlist.downloader import _download_external_sync

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(title="Cached", artist="Artist", duration_ms=180000)
    cached = cache_dir / f"{track.cache_key}.mp3"
    cached.touch()

    with patch("mammamiradio.playlist.downloader._ytdlp_enabled", return_value=True):
        result = _download_external_sync(track, cache_dir, music_dir)
    assert result == cached


def test_download_external_sync_rejects_cached_external_bytes_when_disabled(tmp_path):
    from mammamiradio.playlist.downloader import _download_external_sync

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(title="Cached", artist="Artist", duration_ms=180000)
    (cache_dir / f"{track.cache_key}.mp3").touch()

    with (
        patch("mammamiradio.playlist.downloader._ytdlp_enabled", return_value=False),
        pytest.raises(RuntimeError, match="yt-dlp is disabled"),
    ):
        _download_external_sync(track, cache_dir, music_dir)


def test_download_external_sync_local_file(tmp_path):
    """_download_external_sync must find a local file when cache misses."""
    from mammamiradio.playlist.downloader import _download_external_sync

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


def test_download_external_sync_raises_when_ytdlp_disabled_standalone(tmp_path):
    """_download_external_sync must raise RuntimeError when yt-dlp is disabled and no cache/local."""
    from mammamiradio.playlist.downloader import _download_external_sync

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(title="Unavailable", artist="Nobody", duration_ms=180000)

    with (
        patch.dict("os.environ", {"MAMMAMIRADIO_ALLOW_YTDLP": "false"}),
        pytest.raises(RuntimeError, match="yt-dlp is disabled"),
    ):
        _download_external_sync(track, cache_dir, music_dir)


# ---------------------------------------------------------------------------
# download_external_track: async wrapper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_external_track_returns_path(tmp_path):
    """download_external_track must return the path from the sync helper via executor."""
    from mammamiradio.playlist.downloader import download_external_track

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    track = Track(title="Async Song", artist="Artist", duration_ms=180000)
    expected = cache_dir / f"{track.cache_key}.mp3"
    expected.touch()

    with patch("mammamiradio.playlist.downloader._ytdlp_enabled", return_value=True):
        result = await download_external_track(track, cache_dir, music_dir=tmp_path / "music")
    assert result == expected


def test_persistent_jamendo_direct_url_never_reaches_any_downloader(tmp_path):
    from mammamiradio.playlist.downloader import _download_sync

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(
        title="Solo CC",
        artist="Jamendo Artist",
        duration_ms=180000,
        spotify_id="jamendo_123",
        direct_url="https://storage.jamendo.com/tracks/123.mp3",
        source="jamendo",
    )

    with (
        patch("mammamiradio.playlist.downloader._download_ytdlp") as mock_ytdlp,
        pytest.raises(RuntimeError, match="persistent Jamendo track acquisition is retired"),
    ):
        _download_sync(track, cache_dir, music_dir)

    assert list(cache_dir.iterdir()) == []
    mock_ytdlp.assert_not_called()


def test_cache_key_separates_same_song_across_sources():
    jamendo_track = Track(
        title="Same Song",
        artist="Same Artist",
        duration_ms=180000,
        spotify_id="jamendo_42",
        source="jamendo",
    )
    youtube_track = Track(
        title="Same Song",
        artist="Same Artist",
        duration_ms=180000,
        source="youtube",
    )

    assert jamendo_track.cache_key != youtube_track.cache_key


def test_jamendo_cache_key_normalizes_direct_url():
    base = Track(
        title="Normalized",
        artist="URL Artist",
        duration_ms=180000,
        direct_url="https://STORAGE.JAMENDO.com/tracks/song.mp3?token=abc",
        source="jamendo",
    )
    variant = Track(
        title="Normalized",
        artist="URL Artist",
        duration_ms=180000,
        direct_url="https://storage.jamendo.com/tracks/song.mp3/",
        source="jamendo",
    )

    assert base.cache_key == variant.cache_key


def test_jamendo_lookup_does_not_reuse_legacy_unsourced_cache(tmp_path):
    from mammamiradio.playlist.downloader import _resolve_cached_or_local

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(
        title="Legacy Clash",
        artist="Artist",
        duration_ms=180000,
        spotify_id="jamendo_legacy",
        source="jamendo",
    )
    legacy_path = cache_dir / f"{track.legacy_cache_key}.mp3"
    legacy_path.write_text("legacy youtube audio")

    assert _resolve_cached_or_local(track, cache_dir, music_dir) is None


def test_legacy_youtube_cache_hit_returns_old_path(tmp_path, monkeypatch, external_media_installed):
    from mammamiradio.playlist.downloader import _resolve_cached_or_local

    monkeypatch.setenv("MAMMAMIRADIO_ALLOW_YTDLP", "true")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(
        title="Legacy Hit",
        artist="Old Artist",
        duration_ms=180000,
        youtube_id="abc123",
        source="youtube",
    )
    legacy_path = cache_dir / f"{track.legacy_cache_key}.mp3"
    legacy_path.write_bytes(b"x" * 600_000)
    (cache_dir / f"_failed_{track.cache_key}.mp3").write_text("prior failure")

    result = _resolve_cached_or_local(track, cache_dir, music_dir)
    assert result == legacy_path


def test_legacy_youtube_cache_stays_invisible_when_external_media_missing(
    tmp_path, monkeypatch, external_media_missing
):
    """Extractor-owned cache bytes are not served by installs without the extra, even with opt-in."""
    from mammamiradio.playlist.downloader import _resolve_cached_or_local

    monkeypatch.setenv("MAMMAMIRADIO_ALLOW_YTDLP", "true")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(
        title="Legacy Hit",
        artist="Old Artist",
        duration_ms=180000,
        youtube_id="abc123",
        source="youtube",
    )
    legacy_path = cache_dir / f"{track.legacy_cache_key}.mp3"
    legacy_path.write_bytes(b"x" * 600_000)

    assert _resolve_cached_or_local(track, cache_dir, music_dir) is None


def test_jamendo_without_direct_url_blocks_ytdlp(tmp_path):
    from mammamiradio.playlist.downloader import _download_sync

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(
        title="No URL",
        artist="Jamendo Artist",
        duration_ms=180000,
        spotify_id="jamendo_no_url",
        source="jamendo",
    )

    with (
        patch("mammamiradio.playlist.downloader._download_ytdlp") as mock_ytdlp,
        pytest.raises(RuntimeError, match="persistent Jamendo track acquisition is retired"),
    ):
        _download_sync(track, cache_dir, music_dir)

    assert list(cache_dir.iterdir()) == []
    mock_ytdlp.assert_not_called()


def test_failed_download_marker_short_circuits_repeat_acquisition(tmp_path):
    """A marker avoids retrying yt-dlp again before the next process start."""
    from mammamiradio.playlist.downloader import _download_sync

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(
        title="Failed Marker",
        artist="Skip Artist",
        duration_ms=180000,
        youtube_id="dQw4w9WgXcQ",
        source="youtube",
    )
    marker = cache_dir / f"_failed_{track.cache_key}.mp3"
    marker.write_text("prior failure")

    with patch("mammamiradio.playlist.downloader._download_ytdlp") as mock_ytdlp:
        result = _download_sync(track, cache_dir, music_dir)

    assert result == marker
    mock_ytdlp.assert_not_called()


def test_fresh_concrete_track_source_allows_replaced_local_recovery_once(tmp_path):
    import os

    from mammamiradio.playlist.downloader import (
        clear_rejected_cache_keys,
        has_fresh_concrete_track_source,
        reject_cached_download,
    )

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(
        title="Local Recovery",
        artist="Local Artist",
        duration_ms=180000,
        source="local",
    )
    local_file = music_dir / f"{track.cache_key}.mp3"
    (cache_dir / f"_failed_{track.cache_key}.mp3").write_text("prior failure")

    assert not has_fresh_concrete_track_source(track, cache_dir, music_dir)

    local_file.write_bytes(b"x" * 600_000)
    assert has_fresh_concrete_track_source(track, cache_dir, music_dir)

    clear_rejected_cache_keys()
    try:
        reject_cached_download(cache_dir, track.cache_key, "local recovery was corrupt")
        assert not has_fresh_concrete_track_source(track, cache_dir, music_dir)

        marker_mtime = (cache_dir / f"_failed_{track.cache_key}.mp3").stat().st_mtime_ns
        os.utime(local_file, ns=(marker_mtime + 1, marker_mtime + 1))
        assert has_fresh_concrete_track_source(track, cache_dir, music_dir)
    finally:
        clear_rejected_cache_keys()


def test_fresh_concrete_track_source_tolerates_stat_error(tmp_path, monkeypatch):
    from mammamiradio.playlist.downloader import has_fresh_concrete_track_source

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = Track(title="Stat Error", artist="Local Artist", duration_ms=180000, source="local")
    marker = cache_dir / f"_failed_{track.cache_key}.mp3"
    marker.write_text("prior failure")
    local_file = music_dir / f"{track.cache_key}.mp3"
    local_file.write_bytes(b"x" * 600_000)
    real_stat = Path.stat

    def _stat(path, *args, **kwargs):
        if path == local_file:
            raise OSError("stat denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _stat)
    with patch("mammamiradio.playlist.downloader._resolve_cached_or_local", return_value=local_file):
        assert not has_fresh_concrete_track_source(track, cache_dir, music_dir)


def test_failed_download_marker_yields_to_track_local_path(tmp_path):
    from mammamiradio.playlist.downloader import _resolve_cached_or_local

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    local_file = tmp_path / "local.mp3"
    local_file.write_bytes(b"x" * 600_000)
    track = Track(
        title="Local Track",
        artist="Local Artist",
        duration_ms=180000,
        local_path=local_file,
        source="local",
    )
    (cache_dir / f"_failed_{track.cache_key}.mp3").write_text("prior failure")

    result = _resolve_cached_or_local(track, cache_dir, music_dir)
    assert result == local_file


def test_search_ytdlp_metadata_filters_non_video_ids():
    """ytsearch mixes channel/playlist hits in with videos; only 11-char
    video ids survive so every search result is queueable (no 400 on add)."""
    import os

    from mammamiradio.playlist.downloader import search_ytdlp_metadata

    class _FakeYoutubeDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, query, download=False):
            return {
                "entries": [
                    # Channel hit (24-char "UC..." id) — must be dropped.
                    {"id": "UC2y0t3AAHuZxb8IgNm-A-yA", "title": "Nina Chuba", "uploader": "Nina Chuba"},
                    # Playlist hit (34-char "PL..." id) — must be dropped.
                    {"id": "PLFgquLnL59alW3xmYiWRaoz0oM3H17Lth", "title": "Nina Chuba Mix", "uploader": "YouTube"},
                    # Non-string id (provider quirk) — str() guard must drop it,
                    # NOT raise TypeError and wipe the whole result set.
                    {"id": 1234567890, "title": "numeric id", "uploader": "x"},
                    # Real video.
                    {"id": "qVSALcVpwkc", "title": "Wildberry Lillet", "uploader": "Nina Chuba", "duration": 180},
                    # Empty id — dropped by the pre-existing guard.
                    {"id": "", "title": "junk"},
                ]
            }

    mock_yt_dlp = MagicMock()
    mock_yt_dlp.YoutubeDL = _FakeYoutubeDL

    with (
        patch.dict(os.environ, {"MAMMAMIRADIO_ALLOW_YTDLP": "true"}),
        patch.dict(sys.modules, {"yt_dlp": mock_yt_dlp}),
    ):
        out = search_ytdlp_metadata("nina chuba", 5)

    ids = [r["youtube_id"] for r in out]
    # The non-string id is dropped without crashing; only the real video survives.
    assert ids == ["qVSALcVpwkc"]
    assert all(isinstance(i, str) and len(i) == 11 for i in ids)


def test_search_ytdlp_metadata_skips_malformed_duration_without_dropping_siblings():
    import os

    from mammamiradio.playlist.downloader import search_ytdlp_metadata

    class _FakeYoutubeDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, query, download=False):
            return {
                "entries": [
                    {
                        "id": "badtime0001",
                        "title": "Bad Duration",
                        "uploader": "Uploader",
                        "duration": "not-a-number",
                    },
                    {
                        "id": "infinite001",
                        "title": "Infinite Duration",
                        "uploader": "Uploader",
                        "duration": "inf",
                    },
                    {
                        "id": "unknown0001",
                        "title": "Unknown Duration",
                        "uploader": "Uploader",
                        "duration": None,
                    },
                    {
                        "id": "missing0001",
                        "title": "Missing Duration",
                        "uploader": "Uploader",
                    },
                    {
                        "id": "emptytime01",
                        "title": "Empty Duration",
                        "uploader": "Uploader",
                        "duration": "",
                    },
                    {
                        "id": "validtime01",
                        "title": "Good Duration",
                        "uploader": "Uploader",
                        "duration": "185.5",
                    },
                ]
            }

    mock_yt_dlp = MagicMock()
    mock_yt_dlp.YoutubeDL = _FakeYoutubeDL

    with (
        patch.dict(os.environ, {"MAMMAMIRADIO_ALLOW_YTDLP": "true"}),
        patch.dict(sys.modules, {"yt_dlp": mock_yt_dlp}),
    ):
        out = search_ytdlp_metadata("duration edge", 5)

    assert [result["youtube_id"] for result in out] == [
        "unknown0001",
        "missing0001",
        "emptytime01",
        "validtime01",
    ]
    assert [result["duration_ms"] for result in out] == [0, 0, 0, 185_500]


# --- prune_stale_tmp_files ---


def _age_file(path, hours: float) -> None:
    """Back-date a file's mtime/atime by *hours*."""
    import os
    import time

    past = time.time() - hours * 3600
    os.utime(path, (past, past))


def test_prune_stale_tmp_files_removes_old_mp3(tmp_path):
    from mammamiradio.playlist.downloader import prune_stale_tmp_files

    old = tmp_path / "banter_full_abcd1234.mp3"
    old.write_bytes(b"x" * 2048)
    _age_file(old, hours=12)  # older than the 6h default

    pruned = prune_stale_tmp_files(tmp_path)

    assert pruned == 1
    assert not old.exists()


def test_prune_stale_tmp_files_keeps_recent_mp3(tmp_path):
    from mammamiradio.playlist.downloader import prune_stale_tmp_files

    fresh = tmp_path / "egress_deadbeef.mp3"
    fresh.write_bytes(b"x" * 2048)  # just written, well within the window

    pruned = prune_stale_tmp_files(tmp_path)

    assert pruned == 0
    assert fresh.exists()


def test_prune_stale_tmp_files_removes_old_mmr_atomic_part(tmp_path):
    from mammamiradio.playlist.downloader import prune_stale_tmp_files

    orphan = tmp_path / ".mmr-atomic-handoff_tail.mp3.deadbeef.part"
    orphan.write_bytes(b"partial audio")
    _age_file(orphan, hours=12)

    pruned = prune_stale_tmp_files(tmp_path)

    assert pruned == 1
    assert not orphan.exists()


def test_prune_stale_tmp_files_keeps_recent_mmr_atomic_part(tmp_path):
    from mammamiradio.playlist.downloader import prune_stale_tmp_files

    in_flight = tmp_path / ".mmr-atomic-handoff_head.mp3.cafebabe.part"
    in_flight.write_bytes(b"partial audio")

    pruned = prune_stale_tmp_files(tmp_path)

    assert pruned == 0
    assert in_flight.exists()


def test_prune_stale_tmp_files_matches_atomic_writer_staging_contract(tmp_path):
    import tempfile
    from unittest.mock import patch

    from mammamiradio.playlist.downloader import prune_stale_tmp_files
    from mammamiradio.web.mp3_frames import _write_bytes_atomically

    real_mkstemp = tempfile.mkstemp
    captured_prefix = ""

    def capture_prefix(*args, **kwargs):
        nonlocal captured_prefix
        captured_prefix = kwargs["prefix"]
        return real_mkstemp(*args, **kwargs)

    with patch("mammamiradio.web.mp3_frames.tempfile.mkstemp", side_effect=capture_prefix):
        _write_bytes_atomically(tmp_path / "published.mp3", b"complete")

    crash_orphan = tmp_path / f"{captured_prefix}crash.part"
    crash_orphan.write_bytes(b"partial")
    _age_file(crash_orphan, hours=12)

    assert prune_stale_tmp_files(tmp_path) == 1
    assert not crash_orphan.exists()


def test_prune_stale_tmp_files_ignores_non_mp3(tmp_path):
    from mammamiradio.playlist.downloader import prune_stale_tmp_files

    other = tmp_path / "leftover.txt"
    other.write_text("not audio")
    _age_file(other, hours=48)
    unrelated_part = tmp_path / ".download.part"
    unrelated_part.write_bytes(b"owned by another workflow")
    _age_file(unrelated_part, hours=48)

    pruned = prune_stale_tmp_files(tmp_path)

    assert pruned == 0
    assert other.exists()
    assert unrelated_part.exists()


def test_prune_stale_tmp_files_skips_symlinked_mp3(tmp_path):
    from mammamiradio.playlist.downloader import prune_stale_tmp_files

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    victim = outside_dir / "victim.mp3"
    victim.write_bytes(b"x" * 2048)
    _age_file(victim, hours=12)
    scratch_link = tmp_path / "banter_link.mp3"
    scratch_link.symlink_to(victim)

    assert prune_stale_tmp_files(tmp_path) == 0
    assert scratch_link.is_symlink()
    assert victim.exists()


def test_prune_stale_tmp_files_skips_symlinked_mmr_atomic_part(tmp_path):
    from mammamiradio.playlist.downloader import prune_stale_tmp_files

    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"do not delete")
    _age_file(victim, hours=12)
    scratch_link = tmp_path / ".mmr-atomic-handoff_tail.mp3.bad.part"
    scratch_link.symlink_to(victim)

    assert prune_stale_tmp_files(tmp_path) == 0
    assert scratch_link.is_symlink()
    assert victim.exists()


def test_prune_stale_tmp_files_missing_dir_returns_zero(tmp_path):
    from mammamiradio.playlist.downloader import prune_stale_tmp_files

    assert prune_stale_tmp_files(tmp_path / "does_not_exist") == 0


def test_prune_stale_tmp_files_rejects_symlinked_tmp_dir_root(tmp_path):
    # Unlike a symlinked *leaf* (unlink() never dereferences a symlink), a
    # symlinked tmp_dir *root* means every glob/stat/unlink targets real
    # files in the redirected directory through normal path resolution —
    # reject_symlinks=True on the per-file check can't catch this because
    # both tmp_dir and the file resolve "contained" relative to each other.
    from mammamiradio.playlist.downloader import prune_stale_tmp_files

    sensitive_dir = tmp_path / "sensitive"
    sensitive_dir.mkdir()
    important = sensitive_dir / "important.mp3"
    important.write_bytes(b"do not delete me")
    _age_file(important, hours=12)

    tmp_dir = tmp_path / "tmp"
    tmp_dir.symlink_to(sensitive_dir, target_is_directory=True)

    assert prune_stale_tmp_files(tmp_dir) == 0
    assert important.exists()


def test_prune_stale_tmp_files_swallows_unlink_error(tmp_path):
    from unittest.mock import patch

    from mammamiradio.playlist.downloader import prune_stale_tmp_files

    old = tmp_path / "trans_cafe9999.mp3"
    old.write_bytes(b"x" * 2048)
    _age_file(old, hours=12)

    with patch("pathlib.Path.unlink", side_effect=OSError("permission denied")):
        # Best-effort: must not raise into startup.
        pruned = prune_stale_tmp_files(tmp_path)

    assert pruned == 0
    assert old.exists()


# ── Coverage for availability seams and filesystem edges ────────────────────


def test_rejected_cache_artifacts_fall_back_to_raw_name_when_dir_unreadable(tmp_path):
    """A missing/unreadable cache dir still yields the raw artifact so purges stay targeted."""
    from mammamiradio.playlist.downloader import _rejected_cache_artifacts

    missing = tmp_path / "never-created"
    assert _rejected_cache_artifacts(missing, "abc123") == [missing / "abc123.mp3"]


def test_evict_cache_lru_skips_caller_protected_paths(cache_dir):
    """A queued file passed via protected_paths survives eviction pressure."""
    from mammamiradio.playlist.downloader import evict_cache_lru

    protected = cache_dir / "norm_queued_song.mp3"
    protected.write_bytes(b"x" * 2 * 1024 * 1024)
    evictable = cache_dir / "cold_song.mp3"
    evictable.write_bytes(b"x" * 2 * 1024 * 1024)

    evict_cache_lru(cache_dir, 1, protected_paths={protected})

    assert protected.exists()
    assert not evictable.exists()


def test_find_local_returns_none_when_music_dir_is_not_scannable(tmp_path, track):
    """A music path that exists but cannot be scanned reads as no local match."""
    from mammamiradio.playlist.downloader import _find_local

    not_a_dir = tmp_path / "music-file"
    not_a_dir.write_text("not a directory")

    assert _find_local(track, not_a_dir) is None


def test_download_external_sync_serves_operator_local_file(tmp_path, track):
    """An explicit external request is satisfied by an operator-local file before any extractor work."""
    from mammamiradio.playlist.downloader import _download_external_sync

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    local = music_dir / "Domenico Modugno - Volare.mp3"
    local.write_bytes(b"x" * 600_000)

    assert _download_external_sync(track, cache_dir, music_dir) == local


def test_search_ytdlp_metadata_degrades_when_module_vanishes(monkeypatch, external_media_missing):
    """If the opt-in gate passes but the module cannot load, search degrades to no results."""
    from mammamiradio.playlist import downloader

    monkeypatch.setattr(downloader, "_ytdlp_enabled", lambda: True)

    assert downloader.search_ytdlp_metadata("volare") == []


def test_download_external_sync_prefers_attached_local_path(tmp_path, track):
    """A track carrying its own existing local file airs that file directly."""
    from mammamiradio.playlist.downloader import _download_external_sync

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    attached = tmp_path / "attached.mp3"
    attached.write_bytes(b"x" * 600_000)
    track.local_path = attached

    assert _download_external_sync(track, cache_dir, music_dir) == attached


def test_find_local_skips_unstatable_entries_and_honors_scan_limit(tmp_path, track, monkeypatch):
    """Directories, broken symlink loops, and the scan cap never break local lookup."""
    from mammamiradio.playlist import downloader

    music_dir = tmp_path / "music"
    music_dir.mkdir()
    (music_dir / "album.mp3").mkdir()  # a directory posing as an mp3
    loop = music_dir / "loop.mp3"
    loop.symlink_to(loop)  # ELOOP on stat
    real = music_dir / "Domenico Modugno - Volare.mp3"
    real.write_bytes(b"x" * 600_000)

    monkeypatch.setattr(downloader, "_LOCAL_FILES_LIMIT", 1)

    assert downloader._find_local(track, music_dir) == real
