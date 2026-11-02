"""Processed music remains usable independently of its evictable download."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
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
@pytest.mark.parametrize(
    "probe_error",
    [subprocess.TimeoutExpired("ffprobe", 30), FileNotFoundError("missing probe")],
    ids=["timeout", "unavailable"],
)
async def test_probe_infrastructure_failure_preserves_prepared_copy(music_cache, monkeypatch, probe_error):
    config, track, normalized = music_cache
    download = AsyncMock(side_effect=RuntimeError("original unavailable"))
    monkeypatch.setattr(producer, "download_track", download)
    monkeypatch.setattr(producer, "validate_download", downloader.validate_download)
    monkeypatch.setattr(downloader.subprocess, "run", Mock(side_effect=probe_error))
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


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["unknown", "mismatched", "retired", "disabled", "missing", "retry"])
@pytest.mark.parametrize("encode_failure", [False, True])
async def test_admitted_original_replaces_ineligible_derivative(
    music_cache, monkeypatch, external_media_installed, gate, encode_failure
):
    config, track, normalized = music_cache
    if gate in {"disabled", "missing"}:
        normalized = _set_source(config, track, normalized, "youtube")
        config.allow_ytdlp = gate == "missing"
        if gate == "missing":
            monkeypatch.setitem(sys.modules, "yt_dlp", None)
    elif gate == "unknown":
        normalized.with_suffix(".mp3.json").unlink()
    elif gate in {"mismatched", "retired"}:
        save_track_metadata(
            normalized, track.title, track.artist, source_kind="jamendo" if gate == "retired" else "youtube"
        )
    original = config.music_dir / "replacement.mp3"
    original.write_bytes(b"o" * 700_000)
    monkeypatch.setattr(producer, "download_track", AsyncMock(return_value=original))
    normalize = Mock(side_effect=lambda source, destination, *_a, **_k: destination.write_bytes(b"replacement audio"))
    monkeypatch.setattr(producer, "normalize", normalize)
    if encode_failure:
        normalize.side_effect = RuntimeError("encode failed")
    with pytest.raises(RuntimeError, match="encode failed") if encode_failure else contextlib.nullcontext():
        rendered = await producer._render_music_track(
            track, config, temp_prefix="music", context="music", _reuse_normalized=gate != "retry"
        )
    if encode_failure:
        assert not normalized.exists()
        return
    assert rendered is not None and not rendered.cache_hit
    assert normalized.read_bytes() == b"replacement audio"
    assert producer.load_track_cache_source(normalized) == track.source
    assert normalize.call_args.args[0] == original
    producer.reconcile_cached_music.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_failure", [None, "ffprobe timed out", "ffprobe failed to start: unavailable"])
@pytest.mark.parametrize("sweeper_ready", [True, False])
async def test_producer_recovers_without_reusing_unverified_audio(
    music_cache, monkeypatch, probe_failure, sweeper_ready
):
    config, track, normalized = music_cache
    config.pacing.lookahead_segments = 1
    state = StationState(playlist=[track], listeners_active=1, last_music_file=normalized)
    state.set_force_next(SegmentType.MUSIC)
    if probe_failure is None:
        normalized.unlink()
    else:
        monkeypatch.setattr(producer, "validate_download", Mock(return_value=(False, probe_failure)))
    download = AsyncMock(side_effect=RuntimeError("original unavailable"))
    monkeypatch.setattr(producer, "download_track", download)
    monkeypatch.setattr(producer, "broadcast_chain_version", lambda: None)
    monkeypatch.setattr(producer, "RUNWAY_FLOOR_SECONDS", 0)
    monkeypatch.setattr(producer, "_prefetch_next", AsyncMock())
    monkeypatch.setattr(producer, "RECOVERY_SWEEPER_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(producer, "RECOVERY_SWEEPER_LINES", producer.RECOVERY_SWEEPER_LINES[:1])
    speech = config.tmp_dir / "recovery.mp3"
    speech.write_bytes(b"speech" * 1024)

    async def render_sweeper(*_args, **_kwargs):
        if not sweeper_ready:
            await asyncio.Event().wait()
        return speech

    render = AsyncMock(side_effect=render_sweeper)
    monkeypatch.setattr(producer, "_render_sweeper_audio", render)
    if sweeper_ready:
        empty_assets = config.tmp_dir / "empty-assets"
        empty_assets.mkdir()
        monkeypatch.setattr(producer, "_DEMO_ASSETS_DIR", empty_assets)
        monkeypatch.setattr(producer, "_canned_clip_cache", {})
    else:
        monkeypatch.setattr(producer, "_pick_recovery_clip", lambda _state: None)
    queue = asyncio.Queue(maxsize=1)
    task = asyncio.create_task(producer.run_producer(queue, state, config))
    try:
        segment = await asyncio.wait_for(queue.get(), timeout=2)
        assert segment.path != normalized and state.failed_segments == 1
        render.assert_awaited_once()
        assert config.display_station_name in render.await_args.args[0]
        if sweeper_ready:
            assert segment.type is SegmentType.SWEEPER and segment.metadata["rescue"]
        else:
            assert segment.metadata["audio_source"] == "emergency_tone"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if probe_failure is not None:
        assert normalized.exists() and not downloader.is_rejected_cache_key(track.cache_key)
        monkeypatch.setattr(producer, "validate_download", Mock(return_value=(True, "ok")))
        retried = await producer._render_music_track(track, config, temp_prefix="music", context="retry")
        assert retried is not None and retried.cache_hit
        download.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_recovery_can_select_another_verified_cache_entry(music_cache, monkeypatch):
    config, track, normalized = music_cache
    state = StationState(playlist=[track], last_music_file=normalized)
    other = config.cache_dir / "norm_other_local_192k.mp3"
    other.write_bytes(b"o" * 700_000)
    save_track_metadata(other, "Other song", "Test artist", source_kind="local", duration_ms=180_000)
    monkeypatch.setattr(producer, "_pick_recovery_clip", lambda _state: None)
    segment = await producer._producer_error_recovery_segment(state, config, excluded_paths=(normalized,))
    assert segment is not None and segment.path == other
    assert normalized.exists() and not downloader.is_rejected_cache_key(track.cache_key)


@pytest.mark.asyncio
async def test_prepared_music_reaches_listener_after_stopped_restart(music_cache, monkeypatch):
    from mammamiradio.web import streamer

    config, track, normalized = music_cache
    config.pacing.lookahead_segments = 1
    streamer._persist_session_stopped(config, True)
    state = StationState(playlist=[track], session_stopped=streamer._session_stopped_flag(config).exists())
    state.set_force_next(SegmentType.MUSIC)
    parked = asyncio.Event()

    class ObservedResume(asyncio.Event):
        async def wait(self):
            parked.set()
            return await super().wait()

    state.resume_event = ObservedResume()
    download = AsyncMock(side_effect=RuntimeError("original unavailable"))
    monkeypatch.setattr(producer, "download_track", download)
    monkeypatch.setattr(producer, "broadcast_chain_version", lambda: None)
    monkeypatch.setattr(producer, "_prefetch_next", AsyncMock())
    queue = asyncio.Queue(maxsize=1)
    hub = streamer.LiveStreamHub()
    hub.bind_state(state)
    app = SimpleNamespace(state=SimpleNamespace(station_state=state, config=config, queue=queue, stream_hub=hub))
    app.state.skip_event = asyncio.Event()
    request = Mock(app=app, is_disconnected=AsyncMock(return_value=False))
    stream = streamer._audio_generator(request, prelude=False)
    first_chunk = asyncio.create_task(anext(stream))
    producing = asyncio.create_task(producer.run_producer(queue, state, config))
    tasks = [first_chunk, producing]
    try:
        await asyncio.wait_for(parked.wait(), timeout=2)
        assert queue.empty() and not first_chunk.done()
        assert await streamer.resume_session(request) == {"ok": True, "recovering": False}
        assert not state.session_stopped and not streamer._session_stopped_flag(config).exists()
        bridge = queue.get_nowait()
        queue.task_done()
        bridge.release()
        music = await asyncio.wait_for(queue.get(), timeout=2)
        queue.task_done()
        assert music.path == normalized and not music.metadata.get("rescue")
        producing.cancel()
        await asyncio.gather(producing, return_exceptions=True)
        queue.put_nowait(music)
        tasks.append(asyncio.create_task(streamer.run_playback_loop(app)))
        chunk = await asyncio.wait_for(first_chunk, timeout=2)
        assert chunk and normalized.read_bytes().startswith(chunk)
        download.assert_not_awaited()
        producer.validate_download.assert_any_call(normalized, background=False, min_size_bytes=1)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
async def test_first_download_source_timing_is_recorded_once(music_cache, monkeypatch):
    from mammamiradio.core import models

    config, track, normalized = music_cache
    normalized.unlink()
    original = config.music_dir / "original.mp3"
    original.write_bytes(b"x" * 700_000)
    state = StationState(playlist=[track])
    now = [100.0]
    clock = SimpleNamespace(monotonic=lambda: now[0], time=lambda: now[0])
    monkeypatch.setattr(models, "time", clock)
    monkeypatch.setattr(producer, "time", clock)

    async def download(*_args, **_kwargs):
        now[0] += 3
        return original

    def normalize(_source, destination, *_args, **_kwargs):
        now[0] += 2
        destination.write_bytes(b"x" * 700_000)

    monkeypatch.setattr(producer, "download_track", AsyncMock(side_effect=download))
    monkeypatch.setattr(producer, "normalize", Mock(side_effect=normalize))
    state.set_gen("finding", "music", track.display)
    rendered = await producer._render_music_track(
        track, config, temp_prefix="music", context="music", timing_state=state
    )
    assert rendered is not None
    assert state._render_timing_stages == {"source": 3000.0, "normalize": 2000.0}
    assert len(state.gen_recent) == 1
