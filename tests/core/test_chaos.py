"""Chaos Mode producer invariants."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import ChaosSubtype, HostPersonality, Segment, SegmentType, StationState, Track
from mammamiradio.scheduling import producer
from mammamiradio.scheduling.producer import CHAOS_AUDIO_FAILURE_LIMIT, RenderedMusicTrack, run_producer

PRODUCER_MODULE = "mammamiradio.scheduling.producer"
SCRIPTWRITER_MODULE = "mammamiradio.hosts.scriptwriter"
TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


@pytest.fixture(autouse=True)
def _clean_last_music_cache():
    """Keep each station scenario isolated from the legacy module cache."""
    producer._last_music_file = None
    yield
    producer._last_music_file = None


def _config(tmp_path: Path):
    config = load_config(TOML_PATH)
    config.pacing.lookahead_segments = 3
    config.homeassistant.enabled = False
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    config.anthropic_api_key = "test-key"
    config.openai_api_key = ""
    return config


def _state() -> StationState:
    return StationState(
        playlist=[
            Track(title="Prima", artist="Artista", duration_ms=180_000, spotify_id="one", youtube_id="yt1"),
            Track(title="Seconda", artist="Artista", duration_ms=180_000, spotify_id="two", youtube_id="yt2"),
        ],
        listeners_active=1,
    )


async def _wait_for_queue(queue: asyncio.Queue[Segment], timeout: float = 3.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while queue.empty():
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError("producer did not queue a segment")
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_chaos_pending_first_strike_queues_banter_without_transition(tmp_path):
    state = _state()
    state.chaos_mode_active = True
    state.chaos_pending = ChaosSubtype.FOURTH_WALL
    config = _config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host: HostPersonality = config.hosts[0]

    with (
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=2.0),
        patch(f"{PRODUCER_MODULE}._crosses_music_speech_boundary", return_value=False),
        patch(f"{PRODUCER_MODULE}._apply_talk_bed", new_callable=AsyncMock, side_effect=lambda p, *_a, **_k: p),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock) as transition,
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            return_value=([(host, "Siamo nel prompt.")], None),
        ) as banter,
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=tmp_path / "chaos.mp3"),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await _wait_for_queue(queue)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    seg = queue.get_nowait()
    state.on_stream_segment(seg)
    assert seg.type == SegmentType.BANTER
    assert seg.metadata["chaos_subtype"] == ChaosSubtype.FOURTH_WALL.value
    assert state.stream_log[-1].metadata["chaos_subtype"] == ChaosSubtype.FOURTH_WALL.value
    assert state.chaos_pending is None
    transition.assert_not_called()
    assert banter.await_args.kwargs["chaos_subtype"] == ChaosSubtype.FOURTH_WALL


@pytest.mark.asyncio
async def test_chaos_cutover_discards_in_flight_music_then_queues_strike(tmp_path):
    state = _state()
    config = _config(tmp_path)
    config.tmp_dir = tmp_path / "tmp"
    config.cache_dir = tmp_path / "cache"
    config.tmp_dir.mkdir()
    config.cache_dir.mkdir()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host: HostPersonality = config.hosts[0]
    music_started = asyncio.Event()
    music_can_finish = asyncio.Event()
    music_path = config.cache_dir / "inflight.mp3"
    admitted_path = config.cache_dir / "post_cutover.mp3"
    music_path.write_bytes(b"fake")
    admitted_path.write_bytes(b"replacement")
    render_calls = 0

    async def _render_music(track, *_args, **_kwargs):
        nonlocal render_calls
        render_calls += 1
        if render_calls == 1:
            music_started.set()
            await music_can_finish.wait()
            path = music_path
        else:
            path = admitted_path
        return RenderedMusicTrack(track=track, path=path, cache_path=path, cache_hit=True)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}._render_music_track", new_callable=AsyncMock, side_effect=_render_music),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=2.0),
        patch(f"{PRODUCER_MODULE}._crosses_music_speech_boundary", return_value=False),
        patch(f"{PRODUCER_MODULE}.generate_track_rationale", return_value="test"),
        patch(f"{PRODUCER_MODULE}.classify_track_crate", return_value="test"),
        patch(f"{PRODUCER_MODULE}._apply_talk_bed", new_callable=AsyncMock, side_effect=lambda p, *_a, **_k: p),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            return_value=([(host, "Taglio caos.")], None),
        ),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=tmp_path / "chaos.mp3"),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.wait_for(music_started.wait(), timeout=1.0)
            state.chaos_mode_active = True
            state.chaos_pending = ChaosSubtype.ABANDONED_STORM
            state.chaos_cutover_epoch += 1
            music_can_finish.set()
            await _wait_for_queue(queue)
            await asyncio.wait_for(_wait_for_last_music(state, admitted_path), timeout=1.0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    seg = queue.get_nowait()
    state.on_stream_segment(seg)
    assert seg.type == SegmentType.BANTER
    assert seg.metadata["chaos_subtype"] == ChaosSubtype.ABANDONED_STORM.value
    assert state.stream_log[-1].metadata["chaos_subtype"] == ChaosSubtype.ABANDONED_STORM.value
    queued_after_cutover = list(queue._queue)
    assert all(item.path != music_path for item in queued_after_cutover)
    assert any(item.path == admitted_path for item in queued_after_cutover)
    # The discarded in-flight music never reached the listener, so the play-time
    # log stays empty. played_tracks is queue-time history and is intentionally
    # left untouched by chaos mode (plan decision 4A).
    assert list(state.played_track_log) == []
    assert music_path.exists()
    assert music_path not in state.immediate_audio_index
    assert state.last_music_file == admitted_path
    assert producer._last_music_file == admitted_path
    assert admitted_path in state.immediate_audio_index

    # Recovery selection happens after the cutover station has admitted valid
    # replacement music. A newly constructed station must still start without a
    # last-known-good candidate instead of inheriting the process cache.
    recovery = Segment(
        type=SegmentType.SWEEPER,
        path=tmp_path / "recovery.mp3",
        metadata={"type": "sweeper", "rescue": True, "error_recovery": True},
    )
    fresh_state = _state()
    with (
        patch(f"{PRODUCER_MODULE}._pick_recovery_clip", return_value=None),
        patch(f"{PRODUCER_MODULE}.select_norm_cache_rescue", return_value=None),
        patch(
            f"{PRODUCER_MODULE}._build_recovery_sweeper_segment",
            new_callable=AsyncMock,
            return_value=recovery,
        ),
    ):
        selected = await producer._producer_error_recovery_segment(fresh_state, config)

    assert selected is recovery


async def _wait_for_last_music(state: StationState, expected: Path) -> None:
    while state.last_music_file != expected:
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_playlist_revision_discard_preserves_cached_music(tmp_path):
    state = _state()
    config = _config(tmp_path)
    config.tmp_dir = tmp_path / "tmp"
    config.cache_dir = tmp_path / "cache"
    config.tmp_dir.mkdir()
    config.cache_dir.mkdir()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    music_started = asyncio.Event()
    music_can_finish = asyncio.Event()
    stale_path = config.cache_dir / "stale.mp3"
    replacement_path = config.cache_dir / "replacement.mp3"
    stale_path.write_bytes(b"stale")
    replacement_path.write_bytes(b"replacement")
    render_calls = 0

    async def _render_music(track, *_args, **_kwargs):
        nonlocal render_calls
        render_calls += 1
        if render_calls == 1:
            music_started.set()
            await music_can_finish.wait()
            return RenderedMusicTrack(track=track, path=stale_path, cache_path=stale_path, cache_hit=True)
        return RenderedMusicTrack(track=track, path=replacement_path, cache_path=replacement_path, cache_hit=True)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}._render_music_track", new_callable=AsyncMock, side_effect=_render_music),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=2.0),
        patch(f"{PRODUCER_MODULE}._crosses_music_speech_boundary", return_value=False),
        patch(f"{PRODUCER_MODULE}.generate_track_rationale", return_value="test"),
        patch(f"{PRODUCER_MODULE}.classify_track_crate", return_value="test"),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.wait_for(music_started.wait(), timeout=1.0)
            state.playlist_revision += 1
            music_can_finish.set()
            await _wait_for_queue(queue)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert queue.get_nowait().type == SegmentType.MUSIC
    assert render_calls >= 2
    assert stale_path.exists()
    assert replacement_path.exists()


@pytest.mark.asyncio
async def test_disable_teardown_clears_pending_before_producer_consumes(tmp_path):
    state = _state()
    state.chaos_mode_active = False
    state.chaos_pending = None
    state.chaos_cutover_epoch = 2
    config = _config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    music_path = tmp_path / "music.mp3"
    music_path.write_bytes(b"fake")

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(
            f"{PRODUCER_MODULE}._render_music_track",
            new_callable=AsyncMock,
            return_value=RenderedMusicTrack(state.playlist[0], music_path, music_path, True),
        ),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=2.0),
        patch(f"{PRODUCER_MODULE}._crosses_music_speech_boundary", return_value=False),
        patch(f"{PRODUCER_MODULE}.generate_track_rationale", return_value="test"),
        patch(f"{PRODUCER_MODULE}.classify_track_crate", return_value="test"),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await _wait_for_queue(queue)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert queue.get_nowait().type == SegmentType.MUSIC


@pytest.mark.asyncio
async def test_chaos_audio_failure_uses_canned_fallback_and_marks_degraded(tmp_path):
    state = _state()
    state.chaos_mode_active = True
    state.chaos_pending = ChaosSubtype.ICON_MOMENT
    config = _config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host: HostPersonality = config.hosts[0]
    canned = tmp_path / "canned.mp3"
    canned.write_bytes(b"fake")

    with (
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=2.0),
        patch(f"{PRODUCER_MODULE}._crosses_music_speech_boundary", return_value=False),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            return_value=([(host, "Linea caos.")], None),
        ),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, side_effect=RuntimeError("tts")),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=canned),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await _wait_for_queue(queue)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    seg = queue.get_nowait()
    assert seg.path == canned
    assert seg.metadata["canned"] is True
    assert seg.metadata["chaos_degraded"] == "audio_failure"
    assert state.chaos_audio_failures == 1


@pytest.mark.asyncio
async def test_chaos_pending_survives_generation_failure_without_fallback(tmp_path):
    state = _state()
    state.chaos_mode_active = True
    state.chaos_pending = ChaosSubtype.ICON_MOMENT
    config = _config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host: HostPersonality = config.hosts[0]

    with (
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            return_value=([(host, "Linea caos.")], None),
        ),
        patch(
            f"{PRODUCER_MODULE}.synthesize_dialogue",
            new_callable=AsyncMock,
            side_effect=RuntimeError("tts"),
        ) as synthesize,
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            deadline = asyncio.get_event_loop().time() + 1.0
            while synthesize.await_count == 0:
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("producer did not attempt chaos audio generation")
                await asyncio.sleep(0.02)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert queue.empty()
    assert state.chaos_pending == ChaosSubtype.ICON_MOMENT
    assert state.chaos_audio_failures >= 1


@pytest.mark.asyncio
async def test_chaos_pending_abandoned_after_failure_limit(tmp_path):
    """chaos_pending is auto-cleared after CHAOS_AUDIO_FAILURE_LIMIT consecutive failures
    to prevent the queue from starving indefinitely."""
    state = _state()
    state.chaos_mode_active = True
    state.chaos_pending = ChaosSubtype.ICON_MOMENT
    state.chaos_audio_failures = CHAOS_AUDIO_FAILURE_LIMIT - 1  # one more failure triggers abandon
    config = _config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host: HostPersonality = config.hosts[0]

    with (
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            return_value=([(host, "Linea caos.")], None),
        ),
        patch(
            f"{PRODUCER_MODULE}.synthesize_dialogue",
            new_callable=AsyncMock,
            side_effect=RuntimeError("tts"),
        ),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            deadline = asyncio.get_event_loop().time() + 3.0
            while state.chaos_pending is not None:
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("chaos_pending was not cleared after failure limit")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert state.chaos_pending is None
    assert state.chaos_last_degraded_reason == "strike_abandoned"
    assert state.chaos_audio_failures >= CHAOS_AUDIO_FAILURE_LIMIT
