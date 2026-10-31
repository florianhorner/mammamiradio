"""Processed music remains usable independently of its evictable download."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from mammamiradio.audio.audio_quality import AudioQualityError, AudioToolError
from mammamiradio.audio.normalizer import save_track_metadata
from mammamiradio.core.config import load_config
from mammamiradio.core.models import SegmentType, StationState, Track
from mammamiradio.playlist import downloader
from mammamiradio.scheduling import producer


@pytest.fixture
def music_cache(tmp_path, monkeypatch):
    config = load_config(Path(__file__).resolve().parents[2] / "radio.toml")
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path / "tmp"
    config.music_dir = tmp_path / "music"
    config.tmp_dir.mkdir()
    config.music_dir.mkdir()
    config.homeassistant.enabled = False
    track = Track(title="Cached Song", artist="Test Artist", duration_ms=180_000, source="local")
    normalized = producer._normalized_cache_path(track, config)
    normalized.write_bytes(b"x" * 700_000)
    save_track_metadata(normalized, track.title, track.artist, duration_ms=track.duration_ms, source_kind=track.source)
    downloader.clear_rejected_cache_keys()
    monkeypatch.setattr(producer, "_last_music_file", None)
    monkeypatch.setattr(producer, "reconcile_cached_music", Mock())
    monkeypatch.setattr(producer, "_probe_segment_duration", Mock(return_value=180.0))
    monkeypatch.setattr(producer, "validate_segment_audio", Mock())
    # Model the acquisition validator without external processes. Tiny failed
    # markers and corrupt normalized artifacts fail just as the real validator does.
    monkeypatch.setattr(
        producer,
        "validate_download",
        Mock(side_effect=lambda path, **kwargs: (path.stat().st_size >= kwargs.get("min_size_bytes", 512_000), "size")),
    )
    yield config, track, normalized
    downloader.clear_rejected_cache_keys()


def _set_source(config, track, normalized, source):
    track.source = source
    replacement = producer._normalized_cache_path(track, config)
    normalized.rename(replacement)
    save_track_metadata(replacement, track.title, track.artist, duration_ms=track.duration_ms, source_kind=source)
    return replacement


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["local", "youtube", "classic"])
@pytest.mark.parametrize("failure", ["marker", "exception"])
async def test_render_reuses_normalized_copy_after_original_eviction(
    music_cache, monkeypatch, external_media_installed, source, failure
):
    config, track, normalized = music_cache
    config.allow_ytdlp = True
    normalized = _set_source(config, track, normalized, source)
    raw = config.cache_dir / f"{track.cache_key}.mp3"
    raw.write_bytes(b"x" * 700_000)
    downloader.evict_cache_lru(config.cache_dir, 1)
    assert not raw.exists() and normalized.exists()
    marker = config.cache_dir / f"_failed_{track.cache_key}.mp3"
    marker.write_text("source unavailable")
    download = AsyncMock(return_value=marker, side_effect=RuntimeError("offline") if failure == "exception" else None)
    monkeypatch.setattr(producer, "download_track", download)
    normalize = Mock()
    monkeypatch.setattr(producer, "normalize", normalize)

    rendered = await producer._render_music_track(track, config, temp_prefix="music", context="music")

    assert rendered is not None and rendered.cache_hit
    assert rendered.path == normalized and normalized.exists()
    assert not downloader.is_rejected_cache_key(track.cache_key)
    download.assert_not_awaited()
    normalize.assert_not_called()
    producer.validate_download.assert_called_once_with(normalized, background=False, min_size_bytes=1)
    producer.reconcile_cached_music.assert_called_once_with(normalized, background=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [None, "youtube", "jamendo"])
async def test_unproven_or_mismatched_cache_requires_original_admission(music_cache, monkeypatch, origin):
    config, track, normalized = music_cache
    sidecar = normalized.with_suffix(".mp3.json")
    if origin is None:
        sidecar.unlink()
    else:
        save_track_metadata(normalized, track.title, track.artist, source_kind=origin)
    marker = config.cache_dir / f"_failed_{track.cache_key}.mp3"
    marker.write_text("source unavailable")
    download = AsyncMock(return_value=marker)
    monkeypatch.setattr(producer, "download_track", download)

    rendered = await producer._render_music_track(track, config, temp_prefix="music", context="music")

    assert rendered is None
    download.assert_awaited_once()
    producer.reconcile_cached_music.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["disabled", "missing"])
async def test_external_cache_cannot_bypass_effective_source_gate(
    music_cache, monkeypatch, external_media_installed, gate
):
    config, track, normalized = music_cache
    normalized = _set_source(config, track, normalized, "youtube")
    config.allow_ytdlp = gate != "disabled"
    if gate == "missing":
        monkeypatch.setitem(sys.modules, "yt_dlp", None)
    marker = config.cache_dir / f"_failed_{track.cache_key}.mp3"
    marker.write_text("external media unavailable")
    download = AsyncMock(return_value=marker)
    monkeypatch.setattr(producer, "download_track", download)

    rendered = await producer._render_music_track(track, config, temp_prefix="music", context="music")

    assert rendered is None
    download.assert_awaited_once()
    producer.reconcile_cached_music.assert_not_called()


@pytest.mark.asyncio
async def test_corrupt_normalized_copy_is_rebuilt_from_valid_original(music_cache, monkeypatch):
    config, track, normalized = music_cache
    normalized.write_bytes(b"broken")
    monkeypatch.setattr(
        producer, "validate_download", Mock(side_effect=lambda path, **_: (path != normalized, "corrupt audio"))
    )
    raw = config.music_dir / "source.mp3"
    raw.write_bytes(b"x" * 700_000)
    monkeypatch.setattr(producer, "download_track", AsyncMock(return_value=raw))

    def normalize(_source, destination, *_args, **_kwargs):
        destination.write_bytes(b"rebuilt normalized audio")

    normalize_mock = Mock(side_effect=normalize)
    monkeypatch.setattr(producer, "normalize", normalize_mock)
    rendered = await producer._render_music_track(track, config, temp_prefix="music", context="music")

    assert rendered is not None and not rendered.cache_hit
    assert normalized.read_bytes() == b"rebuilt normalized audio"
    producer.validate_download.assert_any_call(raw, background=False)
    normalize_mock.assert_called_once()
    assert not downloader.is_rejected_cache_key(track.cache_key)


@pytest.mark.asyncio
async def test_cached_external_audio_still_passes_current_duration_admission(
    music_cache, monkeypatch, external_media_installed
):
    config, track, normalized = music_cache
    normalized = _set_source(config, track, normalized, "youtube")
    config.allow_ytdlp = True
    download = AsyncMock(side_effect=AssertionError("cache must precede acquisition"))
    monkeypatch.setattr(producer, "download_track", download)
    monkeypatch.setattr(producer, "_probe_segment_duration", Mock(return_value=3_600.0))

    rendered = await producer._render_music_track(track, config, temp_prefix="music", context="music")

    assert rendered is None
    assert not normalized.exists()
    assert downloader.is_rejected_cache_key(track.cache_key)
    download.assert_not_awaited()


@pytest.mark.asyncio
async def test_prewarm_queues_cached_music_without_reacquisition(music_cache, monkeypatch):
    config, track, normalized = music_cache
    state = StationState(playlist=[track])
    queue = asyncio.Queue()
    download = AsyncMock(side_effect=RuntimeError("original unavailable"))
    monkeypatch.setattr(producer, "download_track", download)
    monkeypatch.setattr(producer, "broadcast_chain_version", lambda: None)

    assert await producer.prewarm_first_segment(queue, state, config)
    segment = queue.get_nowait()
    assert segment.type == SegmentType.MUSIC
    assert segment.path == normalized and not segment.ephemeral
    download.assert_not_awaited()


@pytest.mark.asyncio
async def test_prewarm_quality_rejection_still_removes_cached_audio(music_cache, monkeypatch):
    config, track, normalized = music_cache
    state = StationState(playlist=[track])
    queue = asyncio.Queue()
    monkeypatch.setattr(producer, "download_track", AsyncMock(side_effect=RuntimeError("offline")))
    monkeypatch.setattr(producer, "validate_segment_audio", Mock(side_effect=AudioQualityError("silent")))

    assert not await producer.prewarm_first_segment(queue, state, config)
    assert queue.empty() and not normalized.exists()


@pytest.mark.asyncio
async def test_prefetch_preserves_ready_cache_and_foreground_can_reuse_it(music_cache, monkeypatch):
    config, track, normalized = music_cache
    state = StationState(playlist=[track])
    download = AsyncMock(side_effect=RuntimeError("original unavailable"))
    monkeypatch.setattr(producer, "download_track", download)

    await producer._prefetch_next(state, config)
    rendered = await producer._render_music_track(track, config, temp_prefix="music", context="music")

    assert rendered is not None and rendered.path == normalized
    assert normalized.exists()
    download.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_rejected_cache_cannot_reopen_itself(music_cache, monkeypatch):
    config, track, normalized = music_cache
    downloader._REJECTED_CACHE_KEYS.add(track.cache_key)
    download = AsyncMock(side_effect=RuntimeError("original unavailable"))
    monkeypatch.setattr(producer, "download_track", download)

    with pytest.raises(RuntimeError, match="original unavailable"):
        await producer._render_music_track(track, config, temp_prefix="music", context="music")

    download.assert_awaited_once()
    producer.reconcile_cached_music.assert_not_called()
    assert normalized.exists()


@pytest.mark.asyncio
async def test_retired_jamendo_copy_cannot_bypass_its_provider(music_cache, monkeypatch):
    config, track, normalized = music_cache
    _set_source(config, track, normalized, "jamendo")
    # Exercise the real acquisition gate: a persisted Jamendo artifact must
    # never become an ordinary music/cache candidate again.
    with pytest.raises(RuntimeError, match="persistent Jamendo track acquisition is retired"):
        await producer._render_music_track(track, config, temp_prefix="music", context="music")

    producer.reconcile_cached_music.assert_not_called()


@pytest.mark.asyncio
async def test_ordinary_producer_admits_cached_music_without_download(music_cache, monkeypatch):
    config, track, normalized = music_cache
    config.pacing.lookahead_segments = 1
    state = StationState(playlist=[track], listeners_active=1)
    state.set_force_next(SegmentType.MUSIC)
    queue = asyncio.Queue(maxsize=1)
    download = AsyncMock(side_effect=RuntimeError("original unavailable"))
    monkeypatch.setattr(producer, "download_track", download)
    monkeypatch.setattr(producer, "broadcast_chain_version", lambda: None)
    monkeypatch.setattr(producer, "RUNWAY_FLOOR_SECONDS", 0)
    task = asyncio.create_task(producer.run_producer(queue, state, config))
    try:
        segment = await asyncio.wait_for(queue.get(), timeout=2)
        assert segment.type == SegmentType.MUSIC
        assert segment.path == normalized and not segment.ephemeral
        assert not segment.metadata.get("rescue")
        download.assert_not_awaited()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_recovery_keeps_cached_audio_without_original_and_honors_bans(music_cache):
    from mammamiradio.audio.norm_cache import select_norm_cache_rescue

    config, track, normalized = music_cache
    state = StationState(playlist=[track])
    assert (
        select_norm_cache_rescue(config.cache_dir, state, require_known_origin=True, allow_recent_repeat=False)
        == normalized
    )
    state.blocklist[track.normalized_key] = {"display": track.display}
    assert (
        select_norm_cache_rescue(config.cache_dir, state, require_known_origin=True, allow_recent_repeat=False) is None
    )


@pytest.mark.requires_ffmpeg
@pytest.mark.asyncio
@pytest.mark.parametrize(("bitrate", "duration"), [(192, 32), (96, 40)])
async def test_cache_reuse_with_real_mp3_validation(music_cache, monkeypatch, bitrate, duration):
    config, track, normalized = music_cache
    config.audio.bitrate = bitrate
    normalized = _set_source(config, track, normalized, track.source)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg tools are unavailable")
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration}",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            f"{bitrate}k",
            str(normalized),
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(producer, "validate_download", downloader.validate_download)
    download = AsyncMock(side_effect=RuntimeError("original unavailable"))
    monkeypatch.setattr(producer, "download_track", download)

    rendered = await producer._render_music_track(track, config, temp_prefix="music", context="music")

    assert rendered is not None and rendered.cache_hit and rendered.path == normalized
    download.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("eviction_point", ["validation", "reconciliation"])
@pytest.mark.parametrize("original_available", [True, False])
async def test_render_falls_back_once_if_cache_is_evicted_after_validation(
    music_cache, monkeypatch, eviction_point, original_available
):
    config, track, normalized = music_cache
    original = config.music_dir / "original.mp3"
    original.write_bytes(b"x" * 700_000)
    download = AsyncMock(
        return_value=original,
        side_effect=None if original_available else RuntimeError("original unavailable"),
    )
    monkeypatch.setattr(producer, "download_track", download)

    def validate(path, **_kwargs):
        if path == normalized and eviction_point == "validation":
            normalized.unlink()
        return True, "ok"

    def reconcile(path, **_kwargs):
        if eviction_point == "reconciliation":
            path.unlink()

    def normalize(source, destination, *_args, **_kwargs):
        assert source == original and source.exists()
        destination.write_bytes(b"x" * 700_000)

    monkeypatch.setattr(producer, "validate_download", Mock(side_effect=validate))
    monkeypatch.setattr(producer, "reconcile_cached_music", Mock(side_effect=reconcile))
    monkeypatch.setattr(producer, "normalize", Mock(side_effect=normalize))
    if original_available:
        rendered = await producer._render_music_track(track, config, temp_prefix="music", context="music")
        assert rendered is not None and not rendered.cache_hit and rendered.path.exists()
    else:
        with pytest.raises(RuntimeError, match="original unavailable"):
            await producer._render_music_track(track, config, temp_prefix="music", context="music")
    download.assert_awaited_once()


@pytest.mark.asyncio
async def test_local_identity_with_youtube_id_cannot_bypass_external_gate(music_cache, monkeypatch):
    config, track, normalized = music_cache
    track.youtube_id = "dQw4w9WgXcQ"
    normalized = _set_source(config, track, normalized, "local")
    config.allow_ytdlp = False
    download = AsyncMock(side_effect=RuntimeError("external media unavailable"))
    monkeypatch.setattr(producer, "download_track", download)
    with pytest.raises(RuntimeError, match="external media unavailable"):
        await producer._render_music_track(track, config, temp_prefix="music", context="music")
    download.assert_awaited_once()
    assert normalized.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["ffprobe timed out", "ffprobe failed to start: unavailable"])
async def test_probe_infrastructure_failure_preserves_prepared_copy(music_cache, monkeypatch, reason):
    config, track, normalized = music_cache
    download = AsyncMock(side_effect=RuntimeError("original unavailable"))
    monkeypatch.setattr(producer, "download_track", download)
    monkeypatch.setattr(producer, "validate_download", Mock(return_value=(False, reason)))
    with pytest.raises(AudioToolError):
        await producer._render_music_track(track, config, temp_prefix="music", context="music")
    assert normalized.exists()
    assert not downloader.is_rejected_cache_key(track.cache_key)
    download.assert_not_awaited()


@pytest.mark.asyncio
async def test_eviction_retry_keeps_source_outside_normalization_timing(music_cache, monkeypatch):
    config, track, normalized = music_cache
    state = StationState(playlist=[track])
    state.set_gen("finding", "music", track.display)
    original = config.music_dir / "original.mp3"
    original.write_bytes(b"x" * 700_000)
    active_stages = []
    real_stage = producer._timed_render_stage

    @contextlib.contextmanager
    def tracked_stage(timing_state, stage):
        if timing_state is None:
            yield
            return
        active_stages.append(stage)
        try:
            with real_stage(timing_state, stage):
                yield
        finally:
            active_stages.pop()

    def validate(path, **_kwargs):
        if path == normalized:
            normalized.unlink()
        return True, "ok"

    async def download(*_args, **_kwargs):
        assert active_stages == ["source"]
        return original

    def normalize(source, destination, *_args, **_kwargs):
        assert source == original
        assert active_stages == ["normalize"]
        destination.write_bytes(b"x" * 700_000)

    monkeypatch.setattr(producer, "_timed_render_stage", tracked_stage)
    monkeypatch.setattr(producer, "validate_download", Mock(side_effect=validate))
    monkeypatch.setattr(producer, "download_track", AsyncMock(side_effect=download))
    monkeypatch.setattr(producer, "normalize", Mock(side_effect=normalize))
    rendered = await producer._render_music_track(
        track, config, temp_prefix="music", context="music", timing_state=state
    )
    assert rendered is not None and rendered.path.exists()
    assert len(state.gen_recent) == 1
