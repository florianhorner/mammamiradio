"""Tests for LiveStreamHub, HTTP routes, and admin-auth enforcement on routes.

The admin-access tests here (``test_admin_*``) are the request-layer half of the
admin-access contract; the boot-layer half lives in ``tests/core/test_config.py``.
The Supervisor-network POST trust and basic-auth CSRF rows are additionally pinned
in ``tests/web/test_streamer_routes_extended.py``; helper-level unit tests live in
``tests/web/test_auth.py``. The single source of truth for the contract is the
"Admin access model" matrix in ``docs/operations.md`` — change a row there and in
``require_admin_access`` (``mammamiradio/web/auth.py``) together, and update these
tests to match.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.audio.norm_cache import select_norm_cache_rescue
from mammamiradio.core.config import load_config
from mammamiradio.core.listener_session import ListenerSession, ListenerSessionCueState
from mammamiradio.core.models import GenerationWasteReason, Segment, SegmentType, StationState, Track
from mammamiradio.home.authorization import HomeAuthorization, HomeAuthorizationMode
from mammamiradio.web.listener_requests import router as listener_requests_router
from mammamiradio.web.streamer import (
    _ASSET_VERSION,
    _DEMO_ASSETS_DIR,
    FIRST_BYTE_GRACE_SECONDS,
    QUEUE_FALLBACK_WAIT_SECONDS,
    SILENCE_FAILURE_SECONDS,
    STREAM_MAX_PACKET_SECONDS,
    STREAM_TARGET_LEAD_SECONDS,
    LiveStreamHub,
    StreamPacer,
    _ad_cast_status_payload,
    _consume_queue_shadow,
    _continuity_reservation_segments,
    _copy_home_context_to_state,
    _packaged_recovery_segment,
    _persist_completed_music,
    _record_provider_verdict,
    _run_provider_verdict,
    _stream_chunk_size,
    router,
    run_playback_loop,
)

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def _scripted_clock(values):
    """Monotonic-clock stand-in: play the scripted values, then hold the last.

    run_playback_loop reads the clock a variable number of times per iteration
    (gap bookkeeping, elapsed, air stamp); a bare finite side_effect list dies
    with StopIteration mid-loop when the count drifts, turning assertion
    failures into opaque poll timeouts. Holding the final value keeps the
    scripted timeline and stays exhaustion-proof.
    """
    it = iter(values)
    last = values[-1]

    def clock():
        nonlocal last
        try:
            last = next(it)
        except StopIteration:
            pass
        return last

    return clock


class _FakeMonotonic:
    """Deterministic monotonic clock for source-packet pacing tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


def _paced_send(pacer: StreamPacer, clock: _FakeMonotonic, chunk_bytes: int = 4096):
    decision = pacer.after_send(chunk_bytes)
    clock.advance(decision.sleep_seconds)
    return decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_app(
    *,
    admin_password: str = "",
    admin_token: str = "",
    is_addon: bool = False,
    preserve_bind_env: bool = False,
) -> FastAPI:
    """Build a minimal FastAPI app with the streamer router and populated state."""
    app = FastAPI()
    app.include_router(router)
    app.include_router(listener_requests_router)

    with patch.dict(os.environ, {"ADMIN_PASSWORD": "", "ADMIN_TOKEN": ""}):
        if not preserve_bind_env:
            os.environ.pop("MAMMAMIRADIO_BIND_HOST", None)
        if is_addon:
            os.environ["SUPERVISOR_TOKEN"] = "test-supervisor-token"
        else:
            os.environ.pop("SUPERVISOR_TOKEN", None)
        os.environ.pop("HASSIO_TOKEN", None)
        config = load_config(TOML_PATH)
    # Override auth settings for test isolation
    config.admin_password = admin_password
    config.admin_token = admin_token
    config.is_addon = is_addon

    state = StationState(
        playlist=[Track(title="Test Song", artist="Test Artist", duration_ms=180_000, spotify_id="t1")],
    )

    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    hub = LiveStreamHub()
    hub.bind_state(state)
    app.state.stream_hub = hub
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    # Drive run_playback_loop integration tests with a real-time pacer (no
    # send-ahead lead) so their queue/rescue timing assertions stay
    # deterministic. The 500 ms delivery cushion itself is covered directly by
    # the StreamPacer unit tests, not through these wall-clock loop tests.
    app.state.stream_pacer_factory = lambda bytes_per_second: StreamPacer(bytes_per_second, target_lead_seconds=0.0)
    return app


def _install_late_blocklisted_continuity_slot(
    state: StationState,
    tmp_path: Path,
    *,
    reservation_id: str,
) -> bytes:
    """Install ready slot bytes that became banned after reservation."""
    blocked_audio = b"blocked-slot-audio" * 1024
    blocked_path = tmp_path / f"{reservation_id}.mp3"
    blocked_path.write_bytes(blocked_audio)
    state.continuity_slot = Segment(
        type=SegmentType.MUSIC,
        path=blocked_path,
        duration_sec=180.0,
        metadata={
            "artist": "Late Artist",
            "title_only": "Late Song",
            "continuity_reservation": True,
            "continuity_reservation_id": reservation_id,
        },
        ephemeral=False,
    )
    state.blocklist = {("late artist", "late song"): {"display": "Late Artist - Late Song"}}
    return blocked_audio


# ---------------------------------------------------------------------------
# LiveStreamHub -- pure async unit tests
# ---------------------------------------------------------------------------


def test_ha_green_queue_fallback_budget_is_shorter_than_health_failure():
    assert QUEUE_FALLBACK_WAIT_SECONDS <= 5.0
    assert SILENCE_FAILURE_SECONDS >= 30.0
    assert QUEUE_FALLBACK_WAIT_SECONDS < SILENCE_FAILURE_SECONDS


def test_stream_pacer_builds_one_500ms_lead_and_keeps_natural_segments_on_the_same_timeline():
    clock = _FakeMonotonic()
    pacer = StreamPacer(24_000, monotonic=clock)

    initial = [_paced_send(pacer, clock) for _ in range(4)]
    assert all(decision.sleep_seconds >= 0 for decision in initial)
    assert pacer.media_seconds - clock.now == pytest.approx(0.5, abs=0.001)

    media_at_boundary = pacer.media_seconds
    first_packet_of_next_natural_segment = _paced_send(pacer, clock)
    assert pacer.reset_count == 0
    assert pacer.media_seconds == pytest.approx(media_at_boundary + 4096 / 24_000)
    assert first_packet_of_next_natural_segment.sleep_seconds == pytest.approx(4096 / 24_000)


def test_source_packet_cap_bounds_low_bitrate_delivery_lead():
    bytes_per_second = 4_000  # 32 kbps
    chunk_size = _stream_chunk_size(bytes_per_second)
    assert chunk_size == 500

    clock = _FakeMonotonic()
    pacer = StreamPacer(bytes_per_second, monotonic=clock)
    maximum_lead = 0.0
    for _ in range(8):
        decision = pacer.after_send(chunk_size)
        maximum_lead = max(maximum_lead, pacer.media_seconds - clock.now)
        clock.advance(decision.sleep_seconds)

    assert maximum_lead <= STREAM_TARGET_LEAD_SECONDS + STREAM_MAX_PACKET_SECONDS + 0.0001


def test_stream_pacer_records_100ms_lateness_without_moving_the_media_timeline():
    clock = _FakeMonotonic()
    pacer = StreamPacer(24_000, monotonic=clock)
    for _ in range(4):
        _paced_send(pacer, clock)

    before = pacer.media_seconds
    clock.advance(0.1)
    delayed = _paced_send(pacer, clock)
    assert delayed.kind == "late"
    assert delayed.lateness_seconds == pytest.approx(0.1)
    assert pacer.media_seconds == pytest.approx(before + 4096 / 24_000)

    next_packet = _paced_send(pacer, clock)
    assert next_packet.kind is None
    assert next_packet.sleep_seconds == pytest.approx(4096 / 24_000)


@pytest.mark.parametrize(
    "reason",
    ["no_listeners", "playback_stop_resume", "explicit_skip", "queue_gap_fallback"],
)
def test_stream_pacer_resets_only_for_named_transport_discontinuities(reason: str):
    clock = _FakeMonotonic()
    pacer = StreamPacer(24_000, monotonic=clock)
    for _ in range(4):
        _paced_send(pacer, clock)

    pacer.reset_timeline(reason)
    decision = _paced_send(pacer, clock)
    assert pacer.reset_count == 1
    assert decision.sleep_seconds == 0
    assert pacer.media_seconds == pytest.approx(4096 / 24_000)


def test_stream_pacer_absorbs_sub_lead_pause_without_rebase_or_negative_sleep():
    clock = _FakeMonotonic()
    pacer = StreamPacer(24_000, monotonic=clock)
    for _ in range(4):
        _paced_send(pacer, clock)

    clock.advance(0.4)
    recovery = [_paced_send(pacer, clock) for _ in range(3)]
    assert all(decision.sleep_seconds >= 0 for decision in recovery)
    assert all(decision.kind != "underrun" for decision in recovery)
    assert all(decision.kind != "overrun_rebased" for decision in recovery)
    assert recovery[-1].sleep_seconds > 0


def test_stream_pacer_caps_overlong_pause_recovery_at_three_chunks_then_rebases_once():
    clock = _FakeMonotonic()
    pacer = StreamPacer(24_000, monotonic=clock)
    for _ in range(4):
        _paced_send(pacer, clock)

    clock.advance(1.2)
    recovery = [_paced_send(pacer, clock) for _ in range(3)]
    assert [decision.kind for decision in recovery] == ["underrun", None, "overrun_rebased"]
    assert all(decision.sleep_seconds >= 0 for decision in recovery)
    assert recovery[0].deficit_seconds > 0
    assert recovery[2].deficit_seconds == recovery[0].deficit_seconds
    assert pacer.media_seconds == pytest.approx(3 * 4096 / 24_000)

    resumed = _paced_send(pacer, clock)
    assert resumed.kind is None
    assert resumed.sleep_seconds >= 0


def test_first_byte_grace_serves_rescue_before_producer_stall_threshold():
    # The connect/first-byte reaction must be well under the 1-2s INSTANT AUDIO
    # promise and never later than the producer-stall (norm-cache) threshold,
    # so a cold listener hears audio fast while a brief stall still prefers a
    # fresh produced segment over an early cached repeat.
    assert FIRST_BYTE_GRACE_SECONDS <= 2.0
    assert FIRST_BYTE_GRACE_SECONDS <= QUEUE_FALLBACK_WAIT_SECONDS


def test_select_norm_cache_rescue_avoids_current_song_when_alternatives_exist(tmp_path):
    state = StationState()
    state.now_streaming = {
        "type": "music",
        "label": "50 Cent – In Da Club",
        "metadata": {"title": "50 Cent – In Da Club", "artist": "50 Cent"},
    }

    current = tmp_path / "norm_youtube_dQw4w9WgXcQ_192k.mp3"
    current.write_bytes(b"x")
    (tmp_path / "norm_youtube_dQw4w9WgXcQ_192k.mp3.json").write_text('{"title": "In Da Club", "artist": "50 Cent"}')
    alternative = tmp_path / "norm_raffaella_carra_a_far_l_amore.mp3"
    alternative.write_bytes(b"x")
    (tmp_path / "norm_raffaella_carra_a_far_l_amore.mp3.json").write_text(
        '{"title": "A far l amore comincia tu", "artist": "Raffaella Carra"}'
    )

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=lambda items: items[0]) as choice:
        rescue = select_norm_cache_rescue(tmp_path, state)

    assert rescue == alternative
    choice.assert_called_once_with([alternative])


def _write_indexed_cache_track(tmp_path, name: str, *, title: str, artist: str, duration: float, state) -> Path:
    path = tmp_path / name
    path.write_bytes(b"audio")
    (tmp_path / f"{name}.json").write_text(f'{{"title": "{title}", "artist": "{artist}"}}')
    state.immediate_audio_index[path] = duration
    return path


def test_continuity_reservation_prefers_non_cooling_cache_track(tmp_path):
    """A live control reserves a fresher cached song over one that just aired as a
    rescue, so repeated controls don't keep reserving the same track."""
    state = StationState()
    cooling = _write_indexed_cache_track(
        tmp_path, "norm_aaa_cooling_192k.mp3", title="Cooling", artist="A", duration=180.0, state=state
    )
    fresh = _write_indexed_cache_track(
        tmp_path, "norm_zzz_fresh_192k.mp3", title="Fresh", artist="B", duration=180.0, state=state
    )
    recovery = _DEMO_ASSETS_DIR / "recovery" / "continuity_1.mp3"

    with patch("mammamiradio.audio.norm_cache.time.monotonic", return_value=10_000.0):
        state.rescue_airplay[cooling] = 10_000.0 - 60.0
        segments = _continuity_reservation_segments(
            state, SimpleNamespace(), target_seconds=1.0, max_segments=1, excluded_paths={recovery}
        )

    assert [seg.path for seg in segments] == [fresh]


def test_continuity_reservation_finds_fresh_track_beyond_cooling_scan_prefix(tmp_path):
    """Cooling entries cannot consume the bounded scan before an eligible track."""
    state = StationState()
    cooling_paths = []
    for index in range(24):
        path = _write_indexed_cache_track(
            tmp_path,
            f"norm_cooling_{index:02d}_192k.mp3",
            title=f"Cooling {index}",
            artist="A",
            duration=180.0,
            state=state,
        )
        cooling_paths.append(path)
    fresh = _write_indexed_cache_track(
        tmp_path,
        "norm_fresh_after_prefix_192k.mp3",
        title="Fresh after prefix",
        artist="B",
        duration=180.0,
        state=state,
    )
    recovery = _DEMO_ASSETS_DIR / "recovery" / "continuity_1.mp3"

    with patch("mammamiradio.audio.norm_cache.time.monotonic", return_value=10_000.0):
        state.rescue_airplay.update({path: 10_000.0 - 60.0 for path in cooling_paths})
        segments = _continuity_reservation_segments(
            state, SimpleNamespace(), target_seconds=1.0, max_segments=1, excluded_paths={recovery}
        )

    assert [seg.path for seg in segments] == [fresh]


def test_continuity_reservation_falls_back_to_least_recent_when_all_cooling(tmp_path):
    """When every cached track is cooling, the reservation still books real music —
    the least-recently-heard one — rather than dropping to the emergency tone."""
    state = StationState()
    older = _write_indexed_cache_track(
        tmp_path, "norm_aaa_older_192k.mp3", title="Older", artist="A", duration=180.0, state=state
    )
    newer = _write_indexed_cache_track(
        tmp_path, "norm_zzz_newer_192k.mp3", title="Newer", artist="B", duration=180.0, state=state
    )
    recovery = _DEMO_ASSETS_DIR / "recovery" / "continuity_1.mp3"

    with patch("mammamiradio.audio.norm_cache.time.monotonic", return_value=10_000.0):
        state.rescue_airplay[older] = 10_000.0 - 100.0
        state.rescue_airplay[newer] = 10_000.0 - 10.0
        segments = _continuity_reservation_segments(
            state, SimpleNamespace(), target_seconds=1.0, max_segments=1, excluded_paths={recovery}
        )

    assert [seg.path for seg in segments] == [older]


@pytest.mark.asyncio
async def test_subscribe_returns_id_and_queue():
    hub = LiveStreamHub()
    lid, q = hub.subscribe()
    assert isinstance(lid, int)
    assert isinstance(q, asyncio.Queue)
    assert hub.has_listener(lid)


@pytest.mark.asyncio
async def test_broadcast_reports_only_listener_queues_that_accept_the_chunk():
    hub = LiveStreamHub(listener_queue_size=1)
    _, accepting = hub.subscribe()
    accepting.put_nowait(b"already full")
    _, open_queue = hub.subscribe()

    accepted = await hub.broadcast(b"next")

    assert accepted == 1
    assert await open_queue.get() == b"next"
    assert len(hub._listeners) == 1


def test_delivery_generation_advances_only_when_an_empty_room_refills():
    hub = LiveStreamHub()
    first, _ = hub.subscribe()
    assert hub.delivery_generation == 1

    second, _ = hub.subscribe()
    assert hub.delivery_generation == 1

    hub.unsubscribe(first)
    hub.unsubscribe(second)
    hub.subscribe()
    assert hub.delivery_generation == 2


def _queue_companionship_cue(app: FastAPI, tmp_path: Path, *, audio: bytes = b"cue audio"):
    now = [0.0]
    session = ListenerSession(monotonic=lambda: now[0])
    app.state.station_state.listener_session = session
    listener_id, listener_queue = app.state.stream_hub.subscribe()
    now[0] = 1800.0
    claim = session.claim_companionship()
    assert claim is not None

    path = tmp_path / "companionship.mp3"
    path.write_bytes(audio)
    queue_id = "companionship-cue"
    segment = Segment(
        type=SegmentType.BANTER,
        path=path,
        duration_sec=1.0,
        metadata={
            "title": "Companionship",
            "queue_id": queue_id,
            "listener_session_epoch": claim.epoch,
            "listener_session_cue": "companionship",
        },
        ephemeral=False,
    )
    assert session.mark_companionship_queued(claim.epoch)
    app.state.queue.put_nowait(segment)
    app.state.station_state.queued_segments = [
        {
            "id": queue_id,
            "type": "banter",
            "label": "Companionship",
            "duration_sec": 1.0,
        }
    ]
    return now, listener_id, listener_queue, segment, claim


@pytest.mark.asyncio
async def test_companionship_cue_is_consumed_only_after_a_listener_accepts_audio(tmp_path):
    app = _make_test_app()
    _, _, listener_queue, _, claim = _queue_companionship_cue(app, tmp_path)

    task = asyncio.create_task(run_playback_loop(app))
    try:
        assert await asyncio.wait_for(listener_queue.get(), timeout=1.0) == b"cue audio"
        await asyncio.wait_for(app.state.queue.join(), timeout=1.0)
        assert app.state.station_state.listener_session.companionship_cue_state is ListenerSessionCueState.CONSUMED
        assert app.state.station_state.now_streaming["label"] == "Companionship"
        assert claim.epoch == app.state.station_state.listener_session.epoch
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_companionship_cue_without_an_accepting_listener_is_abandoned_before_start(tmp_path):
    app = _make_test_app()
    _, listener_id, listener_queue, _, _ = _queue_companionship_cue(app, tmp_path)

    async def _reject_first_chunk(_chunk: bytes) -> int:
        app.state.stream_hub.unsubscribe(listener_id)
        return 0

    app.state.stream_hub.broadcast = _reject_first_chunk
    task = asyncio.create_task(run_playback_loop(app))
    try:
        await asyncio.wait_for(app.state.queue.join(), timeout=1.0)
        state = app.state.station_state
        assert listener_queue.empty()
        assert state.listener_session.companionship_cue_state is ListenerSessionCueState.ABANDONED
        assert state.discard_by_reason[GenerationWasteReason.LISTENER_SESSION_STALE] == 1
        assert state.now_streaming.get("label") != "Companionship"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stale_queued_companionship_epoch_is_discarded_before_audio(tmp_path):
    app = _make_test_app()
    now, listener_id, _, _, claim = _queue_companionship_cue(app, tmp_path)
    app.state.stream_hub.unsubscribe(listener_id)
    now[0] = 2400.0  # exactly ten empty minutes starts a new station epoch
    _, new_listener_queue = app.state.stream_hub.subscribe()
    assert app.state.station_state.listener_session.epoch == claim.epoch + 1

    task = asyncio.create_task(run_playback_loop(app))
    try:
        await asyncio.wait_for(app.state.queue.join(), timeout=1.0)
        state = app.state.station_state
        assert new_listener_queue.empty()
        assert state.discard_by_reason[GenerationWasteReason.LISTENER_SESSION_STALE] == 1
        assert state.queued_segments == []
        assert state.now_streaming.get("label") != "Companionship"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_companionship_epoch_fence_stops_remaining_chunks_after_epoch_changes(tmp_path):
    app = _make_test_app()
    app.state.config.audio.bitrate = 32
    now, listener_id, first_queue, _, claim = _queue_companionship_cue(app, tmp_path, audio=b"x" * 4096)

    task = asyncio.create_task(run_playback_loop(app))
    try:
        assert await asyncio.wait_for(first_queue.get(), timeout=1.0)
        assert app.state.station_state.listener_session.companionship_cue_state is ListenerSessionCueState.CONSUMED
        app.state.stream_hub.unsubscribe(listener_id)
        now[0] = 2400.0
        _, new_listener_queue = app.state.stream_hub.subscribe()
        assert app.state.station_state.listener_session.epoch == claim.epoch + 1

        await asyncio.wait_for(app.state.queue.join(), timeout=1.0)
        assert new_listener_queue.empty()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_queue_shadow_consumption_repairs_identity_mismatch_without_blind_pop(tmp_path):
    state = StationState()
    queue: asyncio.Queue[Segment] = asyncio.Queue()
    pulled = Segment(
        type=SegmentType.BANTER,
        path=tmp_path / "pulled.mp3",
        metadata={"queue_id": "pulled", "title": "Pulled"},
    )
    remaining = Segment(
        type=SegmentType.MUSIC,
        path=tmp_path / "remaining.mp3",
        metadata={"queue_id": "remaining", "title": "Remaining"},
    )
    queue.put_nowait(pulled)
    queue.put_nowait(remaining)
    assert queue.get_nowait() is pulled
    state.queued_segments = [
        {"id": "remaining", "label": "Remaining", "reason": "preserve me"},
        {"id": "pulled", "label": "Pulled"},
    ]

    _consume_queue_shadow(queue, state, pulled)

    assert state.queued_segments == [{"id": "remaining", "label": "Remaining", "reason": "preserve me"}]


@pytest.mark.asyncio
async def test_run_playback_loop_restarts_default_cushion_for_midsegment_reconnect(tmp_path):
    """A reconnect within a file must not inherit the previous media clock."""
    app = _make_test_app()
    # The 32 kbps packet cap keeps the physical lead bounded; yield after the
    # first packet so the reconnect happens before the next one is broadcast.
    app.state.config.audio.bitrate = 32
    created_pacers: list[StreamPacer] = []

    def _default_pacer(bytes_per_second: float) -> StreamPacer:
        pacer = StreamPacer(bytes_per_second)
        created_pacers.append(pacer)
        return pacer

    app.state.stream_pacer_factory = _default_pacer
    first_listener, _ = app.state.stream_hub.subscribe()
    audio_path = tmp_path / "midsegment-reconnect.mp3"
    audio_path.write_bytes(b"x" * 8192)
    app.state.queue.put_nowait(
        Segment(
            type=SegmentType.MUSIC,
            path=audio_path,
            metadata={"title": "Reconnect", "title_only": "Reconnect", "artist": "Test"},
        )
    )

    first_packet_sent = asyncio.Event()
    release_first_packet = asyncio.Event()
    second_packet_sent = asyncio.Event()
    calls = 0
    broadcast = app.state.stream_hub.broadcast

    async def _broadcast(chunk: bytes) -> None:
        nonlocal calls
        await broadcast(chunk)
        calls += 1
        if calls == 1:
            first_packet_sent.set()
            await release_first_packet.wait()
        elif calls == 2:
            second_packet_sent.set()

    app.state.stream_hub.broadcast = _broadcast
    task = asyncio.create_task(run_playback_loop(app))
    try:
        await asyncio.wait_for(first_packet_sent.wait(), timeout=1.0)
        app.state.stream_hub.unsubscribe(first_listener)
        _, reconnected_queue = app.state.stream_hub.subscribe()
        release_first_packet.set()

        await asyncio.wait_for(second_packet_sent.wait(), timeout=1.0)
        pacer = created_pacers[0]
        assert pacer.target_lead_seconds == pytest.approx(0.5)
        assert pacer.reset_count == 1
        assert await asyncio.wait_for(reconnected_queue.get(), timeout=0.1) == b"x" * _stream_chunk_size(4_000)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_run_playback_loop_records_bounded_recovery_after_scheduler_stall(tmp_path):
    """The loop must carry real pacer recovery signals into private diagnostics."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 32
    clock = _FakeMonotonic()
    created_pacers: list[StreamPacer] = []

    def _pacer(bytes_per_second: float) -> StreamPacer:
        pacer = StreamPacer(bytes_per_second, monotonic=clock)
        created_pacers.append(pacer)
        return pacer

    app.state.stream_pacer_factory = _pacer
    app.state.stream_hub.subscribe()
    audio_path = tmp_path / "scheduler-stall.mp3"
    audio_path.write_bytes(b"x" * 4_000)
    app.state.queue.put_nowait(
        Segment(
            type=SegmentType.MUSIC,
            path=audio_path,
            metadata={"title": "Scheduler stall", "title_only": "Scheduler stall", "artist": "Test"},
        )
    )

    broadcasts = 0
    real_broadcast = app.state.stream_hub.broadcast

    async def _broadcast(chunk: bytes) -> None:
        nonlocal broadcasts
        broadcasts += 1
        # Four packets establish the 500 ms cushion; the fifth normally waits
        # one packet. Stall before the sixth send to exhaust that cushion.
        if broadcasts == 6:
            clock.advance(1.2)
        await real_broadcast(chunk)

    real_sleep = asyncio.sleep

    async def _paced_sleep(seconds: float) -> None:
        clock.advance(seconds)
        await real_sleep(0)

    app.state.stream_hub.broadcast = _broadcast
    with patch("mammamiradio.web.streamer.asyncio.sleep", side_effect=_paced_sleep):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            await asyncio.wait_for(app.state.queue.join(), timeout=0.5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert broadcasts == 8
    assert len(created_pacers) == 1
    assert created_pacers[0].media_seconds == pytest.approx(3 * 500 / 4_000)
    delivery = app.state.station_state.stream_delivery_snapshot()
    assert delivery["session"] == {"late": 0, "underrun": 1, "overrun_rebased": 1, "total": 2}
    assert [event["kind"] for event in delivery["recent"]] == ["underrun", "overrun_rebased"]


@pytest.mark.asyncio
async def test_subscribe_increments_id():
    hub = LiveStreamHub()
    id1, _ = hub.subscribe()
    id2, _ = hub.subscribe()
    assert id2 == id1 + 1


@pytest.mark.asyncio
async def test_unsubscribe_removes_listener():
    hub = LiveStreamHub()
    lid, _ = hub.subscribe()
    hub.unsubscribe(lid)
    assert not hub.has_listener(lid)


@pytest.mark.asyncio
async def test_has_listener_false_for_unknown():
    hub = LiveStreamHub()
    assert not hub.has_listener(999)


@pytest.mark.asyncio
async def test_subscribe_sets_listener_arrived_event():
    # The playback loop parks on this event when the room is empty; subscribe()
    # must set it so the loop resumes the instant a listener connects.
    hub = LiveStreamHub()
    hub._listener_arrived.clear()
    assert not hub._listener_arrived.is_set()
    hub.subscribe()
    assert hub._listener_arrived.is_set()


@pytest.mark.asyncio
async def test_listener_arrived_wakes_empty_room_waiter_before_poll_timeout():
    # Mirrors the loop's empty-room wait: a connect resumes playback well under
    # the 1s backstop poll instead of sleeping it out (the first-byte win).
    hub = LiveStreamHub()
    hub._listener_arrived.clear()

    async def _connect_soon():
        await asyncio.sleep(0.02)
        hub.subscribe()

    connector = asyncio.create_task(_connect_soon())
    start = asyncio.get_running_loop().time()
    await asyncio.wait_for(hub._listener_arrived.wait(), timeout=1.0)
    elapsed = asyncio.get_running_loop().time() - start
    await connector
    assert hub.has_listener(0)
    assert elapsed < 0.5  # woke on the event, not the 1s poll backstop


@pytest.mark.asyncio
async def test_broadcast_pushes_to_all():
    hub = LiveStreamHub()
    _, q1 = hub.subscribe()
    _, q2 = hub.subscribe()
    chunk = b"audio-data"
    await hub.broadcast(chunk)
    assert q1.get_nowait() == chunk
    assert q2.get_nowait() == chunk


@pytest.mark.asyncio
async def test_broadcast_drops_slow_listeners():
    hub = LiveStreamHub(listener_queue_size=1)
    state = StationState()
    hub.bind_state(state)
    lid, q = hub.subscribe()
    # Fill the queue so the listener is slow
    q.put_nowait(b"old")
    await hub.broadcast(b"new")
    # Slow listener should have been dropped
    assert not hub.has_listener(lid)
    assert state.slow_listener_drops_total == 1
    assert state.slow_listener_last_drop_at > 0


@pytest.mark.asyncio
async def test_close_sends_none():
    hub = LiveStreamHub()
    _, q1 = hub.subscribe()
    _, q2 = hub.subscribe()
    hub.close()
    assert q1.get_nowait() is None
    assert q2.get_nowait() is None


@pytest.mark.asyncio
async def test_close_clears_listeners():
    hub = LiveStreamHub()
    lid, _ = hub.subscribe()
    hub.close()
    assert not hub.has_listener(lid)


# ---------------------------------------------------------------------------
# Route tests -- using httpx.AsyncClient with ASGITransport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_listen_returns_html():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/listen")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_persist_completed_music_records_finished_track():
    app = _make_test_app()
    state = app.state.station_state
    persona_store = MagicMock()
    persona_store._session_id = "session-1"
    persona_store.record_motif = AsyncMock()
    persona_store.record_play = AsyncMock()
    state.persona_store = persona_store

    metadata = {
        "title": "Artist 9 – Song 9",
        "title_only": "Song 9",
        "artist": "Artist 9",
        "youtube_id": "yt_9",
        "spotify_id": "sp_9",
    }

    with patch("mammamiradio.playlist.song_cues.detect_anthem", new=AsyncMock()) as detect_anthem:
        await _persist_completed_music(state, app.state.config, metadata, listen_sec=123.4)

    persona_store.record_motif.assert_awaited_once_with("Artist 9", "Song 9")
    persona_store.record_play.assert_awaited_once_with(
        "yt_9",
        "session-1",
        skipped=False,
        listen_duration_s=123.4,
    )
    detect_anthem.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_playback_loop_persists_music_only_after_segment_finishes(tmp_path):
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()

    audio_path = tmp_path / "segment.mp3"
    audio_path.write_bytes(b"x" * 4096)
    app.state.queue.put_nowait(
        Segment(
            type=SegmentType.MUSIC,
            path=audio_path,
            metadata={"title": "Done", "title_only": "Done", "artist": "Artist", "youtube_id": "yt_done"},
        )
    )

    with patch("mammamiradio.web.streamer._persist_completed_music", new=AsyncMock()) as persist_completed:
        task = asyncio.create_task(run_playback_loop(app))
        try:
            for _ in range(20):
                if persist_completed.await_count:
                    break
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    persist_completed.assert_awaited_once()
    assert not audio_path.exists()


@pytest.mark.asyncio
async def test_run_playback_loop_snapshots_banter_segment_for_lookback(tmp_path):
    """After an ad/banter segment streams, the loop saves a lookback snapshot."""
    from collections import deque

    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()
    app.state.clip_ring_buffer = deque(maxlen=2000)
    app.state.last_shareworthy_clip = None

    audio_path = tmp_path / "banter.mp3"
    audio_path.write_bytes(b"\xff" * 4096)
    app.state.queue.put_nowait(
        Segment(
            type=SegmentType.BANTER,
            path=audio_path,
            metadata={"title": "Coffee machine bit"},
        )
    )

    task = asyncio.create_task(run_playback_loop(app))
    try:
        for _ in range(50):
            if app.state.last_shareworthy_clip is not None:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    snap = app.state.last_shareworthy_clip
    assert snap is not None
    assert snap["type"] == "banter"
    assert snap["bytes"]
    assert snap["title"] == "Coffee machine bit"
    assert "ended_monotonic" in snap


@pytest.mark.asyncio
async def test_run_playback_loop_partial_banter_send_does_not_schedule_memory(tmp_path):
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()

    audio_path = tmp_path / "banter.mp3"
    audio_path.write_bytes(b"x" * 8192)
    app.state.queue.put_nowait(
        Segment(
            type=SegmentType.BANTER,
            path=audio_path,
            metadata={
                "title": "Partial bit",
                "memory_extraction": {"script_lines": [{"host": "Marco", "text": "heard"}]},
            },
        )
    )
    app.state.stream_hub.broadcast = AsyncMock(side_effect=[None, RuntimeError("wire broke")])

    with patch("mammamiradio.hosts.memory_extractor.schedule_banter_memory_extraction") as schedule:
        task = asyncio.create_task(run_playback_loop(app))
        result = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(result[0], RuntimeError)
    assert app.state.station_state.stream_outcome_history[-1]["terminal_reason"] == "aborted"
    schedule.assert_not_called()


@pytest.mark.asyncio
async def test_run_playback_loop_records_cancellation_without_a_file_error(tmp_path):
    app = _make_test_app()
    app.state.stream_hub.subscribe()
    audio_path = tmp_path / "cancelled.mp3"
    audio_path.write_bytes(b"x" * 4096)
    app.state.queue.put_nowait(
        Segment(
            type=SegmentType.MUSIC,
            path=audio_path,
            metadata={"title": "Cancelled", "title_only": "Cancelled", "artist": "Test"},
        )
    )

    sent = asyncio.Event()
    broadcast = app.state.stream_hub.broadcast

    async def _block_after_first_packet(chunk: bytes) -> None:
        await broadcast(chunk)
        sent.set()
        await asyncio.Event().wait()

    app.state.stream_hub.broadcast = _block_after_first_packet
    task = asyncio.create_task(run_playback_loop(app))
    await asyncio.wait_for(sent.wait(), timeout=1.0)
    task.cancel()
    result = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(result[0], asyncio.CancelledError)
    assert app.state.station_state.stream_outcome_history[-1]["terminal_reason"] == "cancelled"


@pytest.mark.asyncio
async def test_run_playback_loop_memory_extraction_skips_if_listener_disconnects_before_start_sample(tmp_path):
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    listener_id, _ = app.state.stream_hub.subscribe()

    audio_path = tmp_path / "banter.mp3"
    audio_path.write_bytes(b"x" * 4096)
    app.state.queue.put_nowait(
        Segment(
            type=SegmentType.BANTER,
            path=audio_path,
            metadata={
                "title": "No-listener bit",
                "memory_extraction": {"script_lines": [{"host": "Marco", "text": "heard"}]},
            },
        )
    )

    original_on_stream_segment = app.state.station_state.on_stream_segment

    def _on_stream_segment_then_disconnect(segment):
        original_on_stream_segment(segment)
        app.state.stream_hub.unsubscribe(listener_id)

    app.state.station_state.on_stream_segment = _on_stream_segment_then_disconnect

    with patch("mammamiradio.hosts.memory_extractor.schedule_banter_memory_extraction") as schedule:
        task = asyncio.create_task(run_playback_loop(app))
        try:
            await asyncio.wait_for(app.state.queue.join(), timeout=1.0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    schedule.assert_not_called()


@pytest.mark.asyncio
async def test_run_playback_loop_skips_missing_file_and_survives(tmp_path):
    """F3 (Scenario-3): a queued segment whose file has vanished — evicted by the
    cache LRU or pruned by the restart-handoff spool while still queued — must be
    skipped, not crash the playback loop. The next queued segment still airs."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()

    missing = Segment(
        type=SegmentType.MUSIC,
        path=tmp_path / "gone.mp3",  # never written -> FileNotFoundError on open
        metadata={"title": "Vanished", "title_only": "Vanished", "artist": "Artist"},
    )
    good_path = tmp_path / "good.mp3"
    good_path.write_bytes(b"x" * 4096)
    good = Segment(
        type=SegmentType.MUSIC,
        path=good_path,
        metadata={"title": "Real", "title_only": "Real", "artist": "Artist", "youtube_id": "yt_real"},
    )
    app.state.queue.put_nowait(missing)
    app.state.queue.put_nowait(good)

    with patch("mammamiradio.web.streamer._persist_completed_music", new=AsyncMock()) as persist:
        task = asyncio.create_task(run_playback_loop(app))
        try:
            for _ in range(60):
                if persist.await_count:
                    break
                await asyncio.sleep(0.01)
            assert not task.done()  # loop survived the missing file (no crash)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    persist.assert_awaited_once()  # the valid segment aired after the skip


@pytest.mark.asyncio
async def test_run_playback_loop_skips_mid_read_oserror_and_survives(tmp_path):
    """F3 covers the open()-time failure; this covers the read()-time failure —
    the file opens fine (bytes_sent > 0 from earlier chunks) but a later
    f.read(chunk_size) call raises. Must still skip and continue, not crash."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()

    flaky_path = tmp_path / "flaky.mp3"
    flaky_path.write_bytes(b"x" * (4096 * 3))
    good_path = tmp_path / "good.mp3"
    good_path.write_bytes(b"x" * 4096)
    flaky = Segment(
        type=SegmentType.MUSIC,
        path=flaky_path,
        metadata={"title": "Flaky", "title_only": "Flaky", "artist": "Artist"},
    )
    good = Segment(
        type=SegmentType.MUSIC,
        path=good_path,
        metadata={"title": "Real", "title_only": "Real", "artist": "Artist", "youtube_id": "yt_real"},
    )
    app.state.queue.put_nowait(flaky)
    app.state.queue.put_nowait(good)

    real_open = open

    class _FlakyReaderFile:
        """Delegates to a real open()'d file but fails mid-stream.

        The two header-peek reads inside _skip_id3_and_xing_header (read(10)
        then read(4) on this non-MP3 fixture) must succeed normally; only the
        SECOND main-loop chunk read raises, after the first chunk already
        went through hub.broadcast() (so bytes_sent > 0 at failure time).
        """

        def __init__(self, path, mode):
            self._f = real_open(path, mode)
            self._reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self._f.close()
            return False

        def read(self, *args, **kwargs):
            self._reads += 1
            if self._reads == 4:  # 2 header-peek reads + 1 real chunk read, then fail
                raise OSError("disk read failed mid-segment")
            return self._f.read(*args, **kwargs)

        def seek(self, *args, **kwargs):
            return self._f.seek(*args, **kwargs)

        def tell(self, *args, **kwargs):
            return self._f.tell(*args, **kwargs)

    def _open_side_effect(path, mode="rb", *args, **kwargs):
        if str(path) == str(flaky_path):
            return _FlakyReaderFile(path, mode)
        return real_open(path, mode, *args, **kwargs)

    with (
        patch("mammamiradio.web.streamer._persist_completed_music", new=AsyncMock()) as persist,
        patch("builtins.open", side_effect=_open_side_effect),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            for _ in range(60):
                if persist.await_count:
                    break
                await asyncio.sleep(0.01)
            assert not task.done()  # loop survived the mid-read OSError (no crash)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    persist.assert_awaited_once()  # the valid segment aired after the skip
    assert app.state.queue.qsize() == 0  # missing segment consumed, not left blocking


@pytest.mark.asyncio
async def test_run_playback_loop_timeout_fallback_keeps_queue_bookkeeping_balanced(tmp_path):
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()
    app.state.station_state.queued_segments = [{"type": "music", "label": "Queued Song"}]

    fallback_path = tmp_path / "fallback.mp3"
    fallback_path.write_bytes(b"x" * 4096)

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=fallback_path),
        patch.object(app.state.queue, "task_done") as mock_task_done,
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while not app.state.station_state.now_streaming:
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not stream fallback segment")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert app.state.station_state.now_streaming["metadata"].get("fallback") is True
    assert app.state.station_state.queued_segments == [{"type": "music", "label": "Queued Song"}]
    mock_task_done.assert_not_called()


@pytest.mark.asyncio
async def test_run_playback_loop_resets_queue_empty_since_after_real_segment(tmp_path):
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()
    app.state.start_time = time.time() - 31
    app.state.station_state.queue_empty_since = time.monotonic() - 40

    audio_path = tmp_path / "real-segment.mp3"
    audio_path.write_bytes(b"x" * 4096)
    app.state.queue.put_nowait(
        Segment(
            type=SegmentType.MUSIC,
            path=audio_path,
            metadata={"title": "Real Song", "title_only": "Real Song", "artist": "Artist"},
        )
    )

    task = asyncio.create_task(run_playback_loop(app))
    try:
        deadline = time.monotonic() + 3.0
        while not app.state.station_state.now_streaming:
            if time.monotonic() > deadline:
                raise AssertionError("playback loop did not stream queued segment")
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert app.state.station_state.queue_empty_since is None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["ready"] is True


@pytest.mark.asyncio
async def test_run_playback_loop_timeout_fallback_keeps_queue_empty_clock_and_duration(tmp_path, caplog):
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()
    queue_empty_started = time.monotonic() - 35
    app.state.station_state.queue_empty_since = queue_empty_started
    caplog.set_level(logging.INFO)

    fallback_path = tmp_path / "fallback-canned.mp3"
    fallback_path.write_bytes(b"x" * 4096)

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=fallback_path),
        patch("mammamiradio.web.streamer.probe_duration_sec", return_value=1.7),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while app.state.station_state.now_streaming.get("metadata", {}).get("fallback") is not True:
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not stream canned fallback")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    now_streaming = app.state.station_state.now_streaming
    assert app.state.station_state.queue_empty_since == queue_empty_started
    assert now_streaming["duration_sec"] == 1.7
    assert now_streaming["metadata"]["duration_ms"] == 1700
    assert not any(record.levelname == "ERROR" for record in caplog.records)


@pytest.mark.asyncio
async def test_run_playback_loop_timeout_serves_one_packaged_clip_then_norm_cache(tmp_path):
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()

    recovery_path = tmp_path / "continuity_1.mp3"
    recovery_path.write_bytes(b"recovery-audio" * 512)
    norm_path = tmp_path / "norm_cached_song_192k.mp3"
    norm_path.write_bytes(b"cached-song" * 4096)
    (tmp_path / "norm_cached_song_192k.mp3.json").write_text('{"title": "Cached Song", "artist": "Cache Artist"}')

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    def _pick_canned_clip(subdir, *, state=None):
        assert state is app.state.station_state
        return recovery_path if subdir == "recovery" else None

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", side_effect=_pick_canned_clip) as pick_canned,
        patch("mammamiradio.web.streamer.probe_duration_sec", return_value=1.7),
        patch(
            "mammamiradio.web.streamer._runtime_monotonic", side_effect=_scripted_clock([100.0, 101.1, 103.0, 104.0])
        ),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while (
                app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "fallback_norm_cache"
            ):
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not escalate to norm-cache music")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    stream_log = list(app.state.station_state.stream_log)
    assert len(stream_log) >= 2
    assert stream_log[0].metadata.get("canned") is True
    assert stream_log[0].metadata.get("rescue") is True
    assert stream_log[0].metadata.get("duration_ms") == 1700
    assert stream_log[1].type == "music"
    assert stream_log[1].metadata.get("audio_source") == "fallback_norm_cache"
    assert stream_log[1].metadata.get("title") == "Cache Artist – Cached Song"
    assert not (stream_log[0].metadata.get("canned") and stream_log[1].metadata.get("canned"))
    assert pick_canned.call_args_list[0].args == ("recovery",)
    assert app.state.station_state.queue_empty_since is None


@pytest.mark.asyncio
async def test_run_playback_loop_repeats_clip_only_when_no_music_rescue_exists(tmp_path):
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()

    recovery_path = tmp_path / "continuity_1.mp3"
    recovery_path.write_bytes(b"recovery-audio" * 512)
    empty_assets = tmp_path / "empty_assets"
    empty_assets.mkdir()

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    def _pick_canned_clip(subdir, *, state=None):
        assert state is app.state.station_state
        return recovery_path if subdir == "recovery" else None

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", side_effect=_pick_canned_clip),
        patch("mammamiradio.web.streamer.probe_duration_sec", return_value=1.7),
        patch(
            "mammamiradio.web.streamer._runtime_monotonic", side_effect=_scripted_clock([100.0, 101.1, 103.0, 104.0])
        ),
        patch("mammamiradio.web.streamer._ASSETS_DIR", empty_assets),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while len(app.state.station_state.stream_log) < 2:
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not re-serve clip as last resort")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    stream_log = list(app.state.station_state.stream_log)
    assert [entry.metadata.get("canned") for entry in stream_log[:2]] == [True, True]
    assert [entry.metadata.get("duration_ms") for entry in stream_log[:2]] == [1700, 1700]
    assert app.state.station_state.force_next is None
    assert app.state.station_state.queue_empty_since is not None


@pytest.mark.asyncio
async def test_run_playback_loop_rung4_reclip_past_60s_does_not_also_force_banter(tmp_path):
    """A last-resort clip re-serve past the 60s threshold must not also request
    forced banter in the same iteration — the segment_ready guard makes them
    mutually exclusive, and the elapsed clock keeps running for /readyz."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()

    recovery_path = tmp_path / "continuity_1.mp3"
    recovery_path.write_bytes(b"recovery-audio" * 512)
    empty_assets = tmp_path / "empty_assets"
    empty_assets.mkdir()

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    def _pick_canned_clip(subdir, *, state=None):
        return recovery_path if subdir == "recovery" else None

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", side_effect=_pick_canned_clip),
        patch("mammamiradio.web.streamer.probe_duration_sec", return_value=1.7),
        # First miss at elapsed 1.1s serves the clip; every later miss lands
        # past the 60s forced-banter threshold while rung 4 re-serves.
        patch(
            "mammamiradio.web.streamer._runtime_monotonic",
            side_effect=_scripted_clock([100.0, 101.1, 165.0, 166.0, 167.0, 168.0]),
        ),
        patch("mammamiradio.web.streamer._ASSETS_DIR", empty_assets),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while len(app.state.station_state.stream_log) < 2:
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not re-serve clip past 60s")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    stream_log = list(app.state.station_state.stream_log)
    assert [entry.metadata.get("canned") for entry in stream_log[:2]] == [True, True]
    assert app.state.station_state.force_next is None
    assert app.state.station_state.queue_empty_since == 100.0


@pytest.mark.asyncio
async def test_packaged_recovery_segment_probe_not_blocked_by_norm_slots(tmp_path):
    """The rescue probe takes the bounded rescue ffmpeg slot: a dead-air fill
    must never queue indefinitely behind ordinary normalization jobs holding
    both _NORM_SEM slots (the exact load pattern that starves the queue)."""
    from mammamiradio.audio import admission

    clip = tmp_path / "continuity_slots.mp3"
    clip.write_bytes(b"recovery-audio" * 512)

    fake_probe = subprocess.CompletedProcess(args=[], returncode=0, stdout="1.7\n", stderr="")
    held = [admission._NORM_SEM.acquire(timeout=1), admission._NORM_SEM.acquire(timeout=1)]
    assert all(held)
    try:
        with patch("mammamiradio.audio.normalizer.subprocess.run", return_value=fake_probe):
            segment = await asyncio.wait_for(_packaged_recovery_segment(clip), timeout=5.0)
    finally:
        for ok in held:
            if ok:
                admission._NORM_SEM.release()

    assert segment.duration_sec == 1.7
    assert segment.metadata["duration_ms"] == 1700
    assert segment.metadata["rescue"] is True


@pytest.mark.asyncio
async def test_playback_consumes_continuity_slot_and_clears_admin_projection(tmp_path):
    """The out-of-band row disappears at the same moment playback claims its audio."""
    from mammamiradio.web.streamer import _continuity_slot_status

    app = _make_test_app()
    app.state.stream_hub.subscribe()
    slot_path = tmp_path / "protected_slot.mp3"
    slot_path.write_bytes(b"protected-audio" * 1024)
    slot = Segment(
        type=SegmentType.BANTER,
        path=slot_path,
        duration_sec=4.44,
        metadata={
            "title": "Protected continuity",
            "continuity_reservation": True,
            "continuity_reservation_id": "playback-slot",
        },
        ephemeral=False,
    )
    state = app.state.station_state
    state.continuity_slot = slot
    started = asyncio.Event()
    original_on_stream_segment = state.on_stream_segment

    def _on_stream_segment(segment):
        original_on_stream_segment(segment)
        started.set()

    state.on_stream_segment = _on_stream_segment
    task = asyncio.create_task(run_playback_loop(app))
    try:
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert state.now_streaming["metadata"]["continuity_reservation_id"] == "playback-slot"
        assert state.continuity_slot is None
        assert _continuity_slot_status(state) is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_playback_rejects_late_blocklisted_music_slot_and_serves_recovery(tmp_path):
    """A song banned after reservation never reaches air; recovery takes over."""
    app = _make_test_app()
    _, listener_queue = app.state.stream_hub.subscribe()
    state = app.state.station_state
    blocked_audio = _install_late_blocklisted_continuity_slot(
        state,
        tmp_path,
        reservation_id="late-blocked-slot",
    )

    recovery_path = tmp_path / "continuity_1.mp3"
    recovery_audio = b"recovery-audio" * 512
    recovery_path.write_bytes(recovery_audio)

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    def _pick_canned_clip(subdir, *, state=None):
        assert state is app.state.station_state
        return recovery_path if subdir == "recovery" else None

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", side_effect=_pick_canned_clip),
        patch("mammamiradio.web.streamer.probe_duration_sec", return_value=1.7),
        patch("mammamiradio.web.streamer._runtime_monotonic", side_effect=_scripted_clock([100.0, 101.1, 101.2])),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while not state.stream_log:
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not fall through to recovery")
                await asyncio.sleep(0.01)
            while listener_queue.empty():
                if time.monotonic() > deadline:
                    raise AssertionError("recovery started but no bytes reached the listener")
                await asyncio.sleep(0.01)
            heard = listener_queue.get_nowait()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert state.continuity_slot is None
    assert recovery_audio.startswith(heard)
    assert not heard.startswith(blocked_audio[:32])
    assert state.stream_log[0].metadata.get("canned") is True
    assert state.stream_log[0].metadata.get("rescue") is True
    assert all(entry.metadata.get("continuity_reservation_id") != "late-blocked-slot" for entry in state.stream_log)
    assert all(entry.metadata.get("title_only") != "Late Song" for entry in state.stream_log)


@pytest.mark.asyncio
async def test_packaged_recovery_segment_caches_duration_per_clip(tmp_path):
    """A packaged clip's duration is probed once (as rescue) then reused, so
    rung-4 repeats stay ffprobe-free; a failed probe is retried, not cached."""
    clip = tmp_path / "continuity_cache.mp3"
    clip.write_bytes(b"recovery-audio" * 512)

    with patch("mammamiradio.web.streamer.probe_duration_sec", return_value=1.7) as probe:
        first = await _packaged_recovery_segment(clip)
        second = await _packaged_recovery_segment(clip)
    probe.assert_called_once_with(clip, rescue=True)
    assert first.metadata["duration_ms"] == second.metadata["duration_ms"] == 1700

    unprobeable = tmp_path / "continuity_unprobeable.mp3"
    unprobeable.write_bytes(b"x")
    with patch("mammamiradio.web.streamer.probe_duration_sec", return_value=None) as probe:
        await _packaged_recovery_segment(unprobeable)
        await _packaged_recovery_segment(unprobeable)
    assert probe.call_count == 2


@pytest.mark.asyncio
async def test_run_playback_loop_clip_rearms_for_next_gap_after_real_segment(tmp_path, caplog):
    """The instant clip must serve again in a LATER gap once real audio aired.

    A dropped gap_clips_served reset on the queue-pull path would serve the
    instant continuity clip exactly once per process lifetime — every later
    gap would open on silence until the 60s forced-banter rung, the inverse
    of the deathloop — while the rest of the suite stays green.
    """
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    caplog.set_level(logging.INFO)

    recovery_path = tmp_path / "continuity_1.mp3"
    recovery_path.write_bytes(b"recovery-audio" * 512)
    real_song = tmp_path / "real_song.mp3"
    real_song.write_bytes(b"music-bytes" * 512)
    empty_assets = tmp_path / "empty_assets"
    empty_assets.mkdir()

    app.state.queue.put_nowait(
        Segment(
            type=SegmentType.MUSIC,
            path=real_song,
            metadata={"type": "music", "title": "Real Song"},
            ephemeral=False,
        )
    )

    # Call 1 forces the first gap (clip serves); call 2 lets the real queued
    # segment through (resetting the gap counter); later calls force a second
    # gap that must open with the instant clip again.
    calls = {"n": 0}

    async def _scripted_wait(awaitable, *_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            return await awaitable
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    def _pick_canned_clip(subdir, *, state=None):
        return recovery_path if subdir == "recovery" else None

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_scripted_wait)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", side_effect=_pick_canned_clip),
        patch("mammamiradio.web.streamer.probe_duration_sec", return_value=1.7),
        patch("mammamiradio.web.streamer._ASSETS_DIR", empty_assets),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while len(app.state.station_state.stream_log) < 3:
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not reach the second gap")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    stream_log = list(app.state.station_state.stream_log)
    assert [entry.metadata.get("canned") for entry in stream_log[:3]] == [True, None, True]
    assert stream_log[1].metadata.get("title") == "Real Song"
    # Both clip airings must be the instant rung-1 serve, never the rung-4
    # last-resort re-serve — that would mean the counter never re-armed.
    messages = [r.getMessage() for r in caplog.records]
    first_serves = [m for m in messages if "Queue empty — serving packaged recovery clip" in m]
    reserves = [m for m in messages if "re-serving packaged recovery clip" in m]
    assert len(first_serves) == 2
    assert not reserves


def test_silence_gate_requires_no_air_not_just_an_empty_queue():
    """/healthz must not report a station audibly bridging on clips as silent.

    queue_empty_since keeps running across continuity-clip serves so the
    rescue ladder can escalate — but a fresh install looping its bridge clip
    during the first track render is airing audio, and flagging it silent
    would hand the add-on watchdog a reason to restart mid-render.
    """
    from mammamiradio.web.streamer import _silence_with_listeners

    state = StationState(playlist=[])
    state.listeners_active = 1

    with patch("mammamiradio.web.streamer._runtime_monotonic", return_value=200.0):
        # Queue empty past the threshold, but a clip started airing 2s ago.
        state.last_air_monotonic = 198.0
        assert _silence_with_listeners(state, 35.0) is False
        # Nothing started airing for 35s — genuine dead air.
        state.last_air_monotonic = 165.0
        assert _silence_with_listeners(state, 35.0) is True

    # Never aired anything at all — silence.
    state.last_air_monotonic = None
    assert _silence_with_listeners(state, 35.0) is True
    # Below the queue-empty threshold — never silence.
    assert _silence_with_listeners(state, 5.0) is False
    # Empty room — never silence.
    state.listeners_active = 0
    assert _silence_with_listeners(state, 35.0) is False


@pytest.mark.asyncio
async def test_run_playback_loop_never_discovers_legacy_welcome_or_banter_clips(tmp_path):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    checked_recovery = asyncio.Event()

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    def _pick_canned_clip(subdir, *, state=None):
        assert state is app.state.station_state
        checked_recovery.set()
        return None

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", side_effect=_pick_canned_clip) as pick_canned,
        patch("mammamiradio.web.streamer._select_norm_cache_rescue", return_value=None),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 1.0
            while not checked_recovery.is_set():
                if time.monotonic() >= deadline:
                    raise AssertionError("playback did not check the approved recovery inventory")
                await asyncio.sleep(0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert pick_canned.call_count >= 1
    assert {call.args[0] for call in pick_canned.call_args_list} == {"recovery"}
    assert app.state.station_state.now_streaming == {}


@pytest.mark.asyncio
async def test_run_playback_loop_stopped_session_never_selects_empty_queue_fallback(tmp_path):
    app = _make_test_app()
    state = app.state.station_state
    state.session_stopped = True
    state.resume_event.clear()
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    app.state.stream_hub.broadcast = AsyncMock()

    async def _fast_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_fast_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip") as pick_canned,
        patch("mammamiradio.web.streamer._select_norm_cache_rescue") as select_rescue,
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            await asyncio.sleep(0.03)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    pick_canned.assert_not_called()
    select_rescue.assert_not_called()
    assert state.force_next is None
    app.state.stream_hub.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_playback_loop_stop_during_queue_wait_skips_fallback(tmp_path):
    app = _make_test_app()
    state = app.state.station_state
    state.session_stopped = False
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    app.state.stream_hub.broadcast = AsyncMock()

    calls = 0

    async def _stop_during_wait(awaitable, *_args, **_kwargs):
        nonlocal calls
        awaitable.close()
        calls += 1
        await asyncio.sleep(0)
        if calls == 1:
            state.session_stopped = True
            raise TimeoutError
        await asyncio.sleep(3600)

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_stop_during_wait)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip") as pick_canned,
        patch("mammamiradio.web.streamer._select_norm_cache_rescue") as select_rescue,
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            # Exits the instant the loop reaches its queue wait, so a generous
            # ceiling costs nothing on a healthy run and avoids a wall-clock
            # flake under coverage instrumentation in CI.
            deadline = time.monotonic() + 5.0
            while calls == 0:
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not enter queue wait")
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.03)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    pick_canned.assert_not_called()
    select_rescue.assert_not_called()
    assert state.force_next is None
    app.state.stream_hub.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_playback_loop_timeout_uses_norm_cache_at_first_byte_grace(tmp_path, caplog):
    # Gate guard: norm-cache rescue must open at the short FIRST_BYTE_GRACE_SECONDS,
    # NOT at the 5s QUEUE_FALLBACK_WAIT_SECONDS ceiling. elapsed here is ~1.1s
    # (just over the grace, well under 5s) and a warm cache is the only rescue
    # rung — the realistic add-on-restart path. If someone re-gates norm cache
    # behind the 5s ceiling, norm cache won't fire at 1.1s and this test fails.
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    caplog.set_level(logging.WARNING)

    rescue_path = tmp_path / "norm_rescue.mp3"
    rescue_path.write_bytes(b"x" * 4096)

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    wait_for = AsyncMock(side_effect=_forced_timeout)
    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=wait_for),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=None),
        patch(
            "mammamiradio.web.streamer._runtime_monotonic",
            side_effect=_scripted_clock(
                [100.0, 100.0 + FIRST_BYTE_GRACE_SECONDS + 0.1, 101.2, 101.3, 101.4, 101.5, 101.6, 101.7]
            ),
        ),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while (
                app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "fallback_norm_cache"
            ):
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not rescue from norm cache")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert app.state.station_state.queue_empty_since is None
    wait_for.assert_called()
    assert wait_for.call_args.kwargs["timeout"] == FIRST_BYTE_GRACE_SECONDS
    assert any("rescuing with norm cache" in record.message for record in caplog.records)
    # Item 20: title must NEVER be the raw filename ("Recovered: norm_rescue.mp3").
    # Without a sidecar, humanize_norm_filename turns "norm_rescue.mp3" → "Rescue".
    now_meta = app.state.station_state.now_streaming.get("metadata", {})
    assert now_meta.get("title") == "Rescue", (
        f"rescue path should humanize filename when no sidecar present; got {now_meta.get('title')!r}"
    )
    assert "Recovered:" not in (now_meta.get("title") or ""), (
        "'Recovered:' prefix must not leak to listener-facing title"
    )


@pytest.mark.asyncio
async def test_run_playback_loop_norm_cache_rescue_status_exposes_progress_duration(tmp_path):
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()

    rescue_path = tmp_path / "norm_jamendo_jamendo_1131121_192k.mp3"
    rescue_path.write_bytes(b"x" * 1_048_576)
    (tmp_path / "norm_jamendo_jamendo_1131121_192k.mp3.json").write_text(
        '{"title": "Miss Understanding", "artist": "Sam Brown"}'
    )

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=None),
        patch(
            "mammamiradio.web.streamer._runtime_monotonic",
            side_effect=_scripted_clock([100.0, 100.0 + FIRST_BYTE_GRACE_SECONDS + 0.1, 101.2, 101.3, 101.4, 101.5]),
        ),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while (
                app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "fallback_norm_cache"
            ):
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not rescue from norm cache")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    now_streaming = app.state.station_state.now_streaming
    assert now_streaming["duration_sec"] > 0
    assert now_streaming["metadata"]["duration_ms"] == round(now_streaming["duration_sec"] * 1000)

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        public_status = (await client.get("/public-status")).json()
        admin_status = (await client.get("/status")).json()

    for body in (public_status, admin_status):
        assert body["now_streaming"]["metadata"]["audio_source"] == "fallback_norm_cache"
        assert body["now_streaming"]["duration_sec"] > 0
        assert body["current_duration_sec"] > 0
        assert isinstance(body["current_progress_sec"], int | float)

    assert public_status["current_duration_sec"] == admin_status["current_duration_sec"]


@pytest.mark.asyncio
async def test_run_playback_loop_rescue_reads_sidecar_metadata(tmp_path, caplog):
    """When a norm-cache file has a `.json` sidecar, the rescue path should use
    its title+artist instead of the humanized filename fallback (Item 20)."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    caplog.set_level(logging.WARNING)

    rescue_path = tmp_path / "norm_rescue.mp3"
    rescue_path.write_bytes(b"x" * 4096)
    # Write the sidecar the way producer.save_track_metadata would.
    import json

    (tmp_path / "norm_rescue.mp3.json").write_text(json.dumps({"title": "Esibizionista", "artist": "Annalisa"}))

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=None),
        patch(
            "mammamiradio.web.streamer._runtime_monotonic",
            side_effect=_scripted_clock([100.0, 130.5, 130.6, 130.7, 130.8, 130.9]),
        ),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while (
                app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "fallback_norm_cache"
            ):
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not rescue from norm cache")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    now_meta = app.state.station_state.now_streaming.get("metadata", {})
    assert now_meta.get("title") == "Annalisa – Esibizionista", (
        f"sidecar metadata should yield 'Annalisa – Esibizionista'; got {now_meta.get('title')!r}"
    )
    assert now_meta.get("artist") == "Annalisa"


@pytest.mark.asyncio
async def test_run_playback_loop_rescue_strips_foreign_station_name_from_sidecar(tmp_path, caplog):
    """Illusion guard: a norm-cache sidecar whose `artist` is a foreign "Radio X"
    station name (the production incident — a name the LLM invented from home
    context that poisoned a cached track) must NOT surface as the now-playing
    artist/label. The rescue path strips it and drops to title-only."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    caplog.set_level(logging.WARNING)

    rescue_path = tmp_path / "norm_rescue.mp3"
    rescue_path.write_bytes(b"x" * 4096)
    import json

    (tmp_path / "norm_rescue.mp3.json").write_text(
        json.dumps({"title": "Be Without U", "artist": "Radio Sabrina Sensatione"})
    )

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=None),
        patch(
            "mammamiradio.web.streamer._runtime_monotonic",
            side_effect=_scripted_clock([100.0, 130.5, 130.6, 130.7, 130.8, 130.9]),
        ),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while (
                app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "fallback_norm_cache"
            ):
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not rescue from norm cache")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    now_meta = app.state.station_state.now_streaming.get("metadata", {})
    # The foreign station name must not appear in any listener-facing field.
    assert "Radio Sabrina Sensatione" not in (now_meta.get("title") or "")
    assert now_meta.get("artist") in (None, "")  # stripped → no artist key
    # The real song title survives.
    assert now_meta.get("title") == "Be Without U", f"got {now_meta.get('title')!r}"


@pytest.mark.asyncio
async def test_run_playback_loop_rescue_strips_foreign_station_prefix_from_title(tmp_path, caplog):
    """Sibling of the artist-strip test on the TITLE field: a sidecar title that
    carries a foreign "Radio X - Song" rescue prefix must be trimmed to the song,
    so the listener-facing now-playing title never airs a competitor's name. This
    streamer rescue path is a separate function from the producer bridge, so it
    needs its own guard."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    caplog.set_level(logging.WARNING)

    rescue_path = tmp_path / "norm_rescue_title.mp3"
    rescue_path.write_bytes(b"x" * 4096)
    import json

    # artist is clean; the foreign name is baked into the title prefix.
    (tmp_path / "norm_rescue_title.mp3.json").write_text(
        json.dumps({"title": "Radio Sabrina Sensatione – Be Without U", "artist": "Mario Biondi"})
    )

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=None),
        patch(
            "mammamiradio.web.streamer._runtime_monotonic",
            side_effect=_scripted_clock([100.0, 130.5, 130.6, 130.7, 130.8, 130.9]),
        ),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while (
                app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "fallback_norm_cache"
            ):
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not rescue from norm cache")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    now_meta = app.state.station_state.now_streaming.get("metadata", {})
    # The foreign station prefix must not appear in the now-playing title.
    assert "Radio Sabrina Sensatione" not in (now_meta.get("title") or "")
    # Title keeps the clean artist + the real song, prefix trimmed.
    assert "Be Without U" in (now_meta.get("title") or "")


@pytest.mark.asyncio
async def test_run_playback_loop_rescue_handles_malformed_sidecar(tmp_path, caplog):
    """Malformed sidecar JSON must not crash; rescue falls back to humanize (Item 20)."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    caplog.set_level(logging.WARNING)

    rescue_path = tmp_path / "norm_busted.mp3"
    rescue_path.write_bytes(b"x" * 4096)
    (tmp_path / "norm_busted.mp3.json").write_text("{not valid json")

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=None),
        patch(
            "mammamiradio.web.streamer._runtime_monotonic",
            side_effect=_scripted_clock([100.0, 130.5, 130.6, 130.7, 130.8, 130.9]),
        ),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while (
                app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "fallback_norm_cache"
            ):
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not rescue from norm cache")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    now_meta = app.state.station_state.now_streaming.get("metadata", {})
    assert now_meta.get("title") == "Busted", (
        f"malformed sidecar should fall back to humanize; got {now_meta.get('title')!r}"
    )


@pytest.mark.asyncio
async def test_run_playback_loop_timeout_uses_demo_assets_after_30s(tmp_path, caplog):
    """Scenario 2 (empty fallback): no canned clips, no norm cache — demo assets must rescue."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    caplog.set_level(logging.WARNING)

    demo_dir = tmp_path / "demo" / "music"
    demo_dir.mkdir(parents=True)
    rescue_mp3 = demo_dir / "Pino Daniele - Napule E.mp3"
    rescue_mp3.write_bytes(b"x" * 4096)

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=None),
        patch(
            "mammamiradio.web.streamer._runtime_monotonic",
            side_effect=_scripted_clock([100.0, 130.5, 130.6, 130.7, 130.8, 130.9]),
        ),
        patch("mammamiradio.web.streamer._ASSETS_DIR", tmp_path),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while (
                app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "fallback_demo_asset"
            ):
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not rescue from demo assets")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert app.state.station_state.queue_empty_since is None
    assert any("rescuing with demo asset" in record.message for record in caplog.records)

    now_meta = app.state.station_state.now_streaming.get("metadata", {})
    assert now_meta.get("title") == "Napule E", (
        f"demo-asset rescue must parse 'Artist - Title.mp3' stems; got title={now_meta.get('title')!r}"
    )
    assert now_meta.get("artist") == "Pino Daniele", (
        f"demo-asset rescue must parse 'Artist - Title.mp3' stems; got artist={now_meta.get('artist')!r}"
    )
    assert app.state.station_state.now_streaming["duration_sec"] > 0
    assert now_meta["duration_ms"] == round(app.state.station_state.now_streaming["duration_sec"] * 1000)

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        public_status = (await client.get("/public-status")).json()
        admin_status = (await client.get("/status")).json()

    for body in (public_status, admin_status):
        assert body["now_streaming"]["metadata"]["audio_source"] == "fallback_demo_asset"
        assert body["now_streaming"]["duration_sec"] > 0
        assert body["current_duration_sec"] > 0


@pytest.mark.asyncio
async def test_run_playback_loop_serves_rescue_at_first_byte_grace_not_after_5s(tmp_path, caplog):
    """First-byte immediacy: a cold/empty queue must serve rescue audio at the
    short FIRST_BYTE_GRACE_SECONDS, not after the full QUEUE_FALLBACK_WAIT_SECONDS.

    Regression guard for the 1-2s INSTANT AUDIO promise: the loop used to block
    the full 5s queue-fallback wait before reaching for any rescue audio (first
    byte at ~5.9s). Here elapsed is ~1s (< the 5s producer-stall threshold), yet
    the demo-asset rescue must already fire — proving rescue is not re-gated
    behind the 5s wait. This is the cold-start path the launch smoke exercises.
    """
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    caplog.set_level(logging.WARNING)

    demo_dir = tmp_path / "demo" / "music"
    demo_dir.mkdir(parents=True)
    (demo_dir / "Pino Daniele - Napule E.mp3").write_bytes(b"x" * 4096)

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    wait_for = AsyncMock(side_effect=_forced_timeout)
    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=wait_for),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=None),
        # elapsed = 101.0 - 100.0 = 1.0s, well under QUEUE_FALLBACK_WAIT_SECONDS.
        patch(
            "mammamiradio.web.streamer._runtime_monotonic",
            side_effect=_scripted_clock([100.0, 101.0, 101.1, 101.2, 101.3, 101.4]),
        ),
        patch("mammamiradio.web.streamer._ASSETS_DIR", tmp_path),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while (
                app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "fallback_demo_asset"
            ):
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not rescue at the first-byte grace")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # The producer was given only the short grace, not the 5s stall threshold.
    # Literal <= 2.0 bound (not just == the symbolic constant) so a code revert
    # to wait_for(timeout=QUEUE_FALLBACK_WAIT_SECONDS) is caught even if the
    # FIRST_BYTE_GRACE_SECONDS constant is left at 1.0.
    assert wait_for.call_args.kwargs["timeout"] <= 2.0
    assert wait_for.call_args.kwargs["timeout"] == FIRST_BYTE_GRACE_SECONDS
    assert FIRST_BYTE_GRACE_SECONDS < QUEUE_FALLBACK_WAIT_SECONDS
    assert any("rescuing with demo asset" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_run_playback_loop_queued_segment_arriving_within_first_byte_grace_does_not_rescue(tmp_path):
    """Scenario 1 (normal): a fresh segment landing inside the first-byte grace
    must air from the queue, not get pre-empted by the rescue ladder."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 64
    app.state.stream_hub.subscribe()
    state = app.state.station_state
    state.queued_segments = [{"type": "music", "label": "Normal Grace"}]

    audio_path = tmp_path / "normal-grace.mp3"
    audio_path.write_bytes(b"x" * 8192)
    segment = Segment(
        type=SegmentType.MUSIC,
        path=audio_path,
        metadata={"title": "Normal Grace", "title_only": "Normal Grace", "artist": "Test Artist"},
    )

    with (
        patch("mammamiradio.web.streamer.FIRST_BYTE_GRACE_SECONDS", 0.2),
        patch("mammamiradio.scheduling.producer._pick_canned_clip") as pick_canned_clip,
        patch("mammamiradio.web.streamer._select_norm_cache_rescue") as select_norm_cache_rescue,
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while state.queue_empty_since is None:
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not enter the first-byte grace window")
                await asyncio.sleep(0.01)

            app.state.queue.put_nowait(segment)

            while state.now_streaming.get("metadata", {}).get("title") != "Normal Grace":
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not stream queued segment inside the grace window")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    now_meta = state.now_streaming.get("metadata", {})
    assert now_meta.get("title") == "Normal Grace"
    assert now_meta.get("fallback") is not True
    assert now_meta.get("audio_source") not in {"fallback_norm_cache", "fallback_demo_asset"}
    assert state.queue_empty_since is None
    assert state.queued_segments == []
    pick_canned_clip.assert_not_called()
    select_norm_cache_rescue.assert_not_called()


@pytest.mark.asyncio
async def test_run_playback_loop_post_restart_rejects_blocked_slot_and_serves_rescue_at_grace(tmp_path, caplog):
    """Scenario 3 (post-restart): session_stopped was set (HA watchdog restart),
    then resume fires with a reserved song banned in the meantime. The banned
    bytes must be rejected and a listener must get warm-cache rescue audio at
    the first-byte grace — not silence, not a 5s wait."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    _, listener_queue = app.state.stream_hub.subscribe()
    caplog.set_level(logging.WARNING)
    state = app.state.station_state
    blocked_audio = _install_late_blocklisted_continuity_slot(
        state,
        tmp_path,
        reservation_id="post-restart-blocked-slot",
    )
    state.session_stopped = True
    entered_stopped_wait = asyncio.Event()

    class ObservedResumeEvent(asyncio.Event):
        async def wait(self) -> Literal[True]:
            entered_stopped_wait.set()
            return await super().wait()

    state.resume_event = ObservedResumeEvent()

    rescue_path = tmp_path / "norm_rescue.mp3"
    rescue_audio = b"restart-rescue-audio" * 256
    rescue_path.write_bytes(rescue_audio)

    # Tiny real grace keeps the test fast while exercising the real wait_for /
    # resume_event timing (no wait_for mock, so the resume path is genuine).
    with (
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=None),
        patch("mammamiradio.web.streamer.FIRST_BYTE_GRACE_SECONDS", 0.05),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            await asyncio.wait_for(entered_stopped_wait.wait(), timeout=1.0)
            state.session_stopped = False  # the "restart" clears
            state.resume_event.set()  # and resume wakes the loop
            deadline = time.monotonic() + 3.0
            while state.now_streaming.get("metadata", {}).get("audio_source") != "fallback_norm_cache":
                if time.monotonic() > deadline:
                    raise AssertionError("post-restart resume did not serve rescue audio at the grace")
                await asyncio.sleep(0.01)
            heard = await asyncio.wait_for(listener_queue.get(), timeout=1.0)
            assert not task.done()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert state.session_stopped is False
    assert state.continuity_slot is None
    assert rescue_audio.startswith(heard)
    assert not heard.startswith(blocked_audio[:32])
    assert all(
        entry.metadata.get("continuity_reservation_id") != "post-restart-blocked-slot" for entry in state.stream_log
    )
    assert any("rescuing with norm cache" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_run_playback_loop_demo_asset_strips_foreign_station_name_from_stem(tmp_path, caplog):
    """Illusion guard on the demo-asset rescue path: a demo file whose stem parses
    to a foreign "Radio X" artist must not surface that artist on the now-playing
    label. The artist falls back to "Unknown" instead of airing a competitor."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    caplog.set_level(logging.WARNING)

    demo_dir = tmp_path / "demo" / "music"
    demo_dir.mkdir(parents=True)
    rescue_mp3 = demo_dir / "Radio Sabrina Sensatione - Be Without U.mp3"
    rescue_mp3.write_bytes(b"x" * 4096)

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=None),
        patch(
            "mammamiradio.web.streamer._runtime_monotonic",
            side_effect=_scripted_clock([100.0, 130.5, 130.6, 130.7, 130.8, 130.9]),
        ),
        patch("mammamiradio.web.streamer._ASSETS_DIR", tmp_path),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while (
                app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "fallback_demo_asset"
            ):
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not rescue from demo assets")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    now_meta = app.state.station_state.now_streaming.get("metadata", {})
    got_artist = now_meta.get("artist")
    assert got_artist == "Unknown", f"foreign station artist should fall back; got {got_artist!r}"
    assert "Radio Sabrina Sensatione" not in (now_meta.get("title") or "")


@pytest.mark.asyncio
async def test_run_playback_loop_rejects_blocked_slot_in_fully_empty_container_and_forces_banter(tmp_path, caplog):
    """Scenario 2 (fully empty): a banned slot and no usable rescue assets.

    The banned bytes never reach the listener, and the playback task remains
    alive long enough to request forced banter as the only remaining escape.
    """
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    _, listener_queue = app.state.stream_hub.subscribe()
    caplog.set_level(logging.ERROR)
    state = app.state.station_state
    _install_late_blocklisted_continuity_slot(
        state,
        tmp_path,
        reservation_id="fully-empty-blocked-slot",
    )

    empty_pkg = tmp_path / "empty_pkg"
    empty_pkg.mkdir()

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=None),
        patch("mammamiradio.web.streamer._select_norm_cache_rescue", return_value=None),
        patch(
            "mammamiradio.web.streamer._runtime_monotonic",
            side_effect=_scripted_clock([200.0, 260.5, 260.6, 260.7, 260.8, 260.9]),
        ),
        patch("mammamiradio.web.streamer._ASSETS_DIR", empty_pkg),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while app.state.station_state.force_next is None:
                if time.monotonic() > deadline:
                    raise AssertionError("empty-container run did not reach forced banter fallback")
                await asyncio.sleep(0.01)
            assert not task.done()
            assert state.continuity_slot is None
            assert listener_queue.empty()
            assert not state.stream_log
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert state.force_next == SegmentType.BANTER
    assert state.queue_empty_since is not None, (
        "queue_empty_since must stay set so /readyz keeps reporting 503 starting until real audio resumes"
    )
    assert not any("rescuing with demo asset" in record.message for record in caplog.records), (
        "demo-asset rescue fired despite empty _ASSETS_DIR"
    )


@pytest.mark.asyncio
async def test_run_playback_loop_timeout_force_resumes_after_60s(tmp_path, caplog):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    caplog.set_level(logging.ERROR)

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=None),
        patch("mammamiradio.web.streamer._runtime_monotonic", side_effect=[200.0, 260.5, 260.6, 260.7]),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while app.state.station_state.force_next is None:
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not force-resume after prolonged silence")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert app.state.station_state.queue_empty_since is not None
    assert app.state.station_state.force_next == SegmentType.BANTER
    assert app.state.skip_event.is_set() is False
    assert any("requesting forced banter from producer" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_readyz_returns_503_when_silent_with_active_listeners():
    app = _make_test_app()
    app.state.start_time = time.time() - 31
    app.state.station_state.listeners_active = 1
    app.state.station_state.queue_empty_since = time.monotonic() - 35

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/readyz")

    assert resp.status_code == 503
    body = resp.json()
    assert body["silence_with_listeners"] is True
    assert body["queue_empty_elapsed_s"] >= 30


@pytest.mark.asyncio
async def test_readyz_does_not_fail_silence_gate_without_listeners():
    app = _make_test_app()
    app.state.start_time = time.time() - 31
    app.state.station_state.listeners_active = 0
    app.state.station_state.queue_empty_since = time.monotonic() - 35

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/readyz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["silence_with_listeners"] is False
    assert body["ready"] is True


@pytest.mark.asyncio
async def test_readyz_returns_503_when_session_stopped():
    """readyz must return 503 when session_stopped=True — station is not ready for listeners."""
    app = _make_test_app()
    app.state.start_time = time.time() - 31  # startup_complete=True
    app.state.queue.put_nowait(object())  # queue_depth > 0
    app.state.station_state.session_stopped = True

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/readyz")

    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False


@pytest.mark.asyncio
async def test_readyz_returns_200_when_session_resumed():
    """readyz must return 200 once session_stopped is cleared and queue has audio."""
    app = _make_test_app()
    app.state.start_time = time.time() - 31
    app.state.queue.put_nowait(object())
    app.state.station_state.session_stopped = False

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/readyz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True


@pytest.mark.asyncio
async def test_healthz_returns_503_when_silent_with_active_listeners():
    """HA Supervisor polls /healthz — it must 503 when silently failing so auto-restart fires."""
    app = _make_test_app()
    app.state.start_time = time.time() - 31
    app.state.station_state.listeners_active = 1
    app.state.station_state.queue_empty_since = time.monotonic() - 35

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/healthz")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "failing"
    assert body["silence_with_listeners"] is True
    assert body["queue_empty_elapsed_s"] >= 30


@pytest.mark.asyncio
async def test_healthz_returns_200_when_quiet_but_no_listeners():
    """No listeners + queue empty is not a failure — nobody is being stranded."""
    app = _make_test_app()
    app.state.start_time = time.time() - 31
    app.state.station_state.listeners_active = 0
    app.state.station_state.queue_empty_since = time.monotonic() - 35

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/healthz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["silence_with_listeners"] is False


@pytest.mark.asyncio
async def test_audio_generator_preserves_persisted_session_stopped_on_connect(tmp_path):
    """A listener connecting must not resume a deliberately stopped session."""
    from mammamiradio.web.streamer import _audio_generator

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    flag = tmp_path / "session_stopped.flag"
    flag.touch()
    app.state.station_state.session_stopped = True

    mock_request = MagicMock()
    mock_request.app = app
    mock_request.is_disconnected = AsyncMock(return_value=True)

    gen = _audio_generator(mock_request)
    async for _ in gen:  # pragma: no cover - generator exits before yielding
        break

    assert app.state.station_state.session_stopped is True
    assert flag.exists()


@pytest.mark.asyncio
async def test_skip_route_persists_music_skips_with_youtube_id():
    app = _make_test_app()
    persona_store = MagicMock()
    persona_store._session_id = "session-2"
    persona_store.record_play = AsyncMock()
    app.state.station_state.persona_store = persona_store
    app.state.station_state.now_streaming = {
        "type": "music",
        "label": "Skipped Song",
        "started": time.time() - 8,
        "metadata": {"youtube_id": "yt_skip"},
    }

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.playlist.song_cues.detect_skip_bit", new=AsyncMock()) as detect_skip_bit:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/skip")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    persona_store.record_play.assert_awaited_once()
    detect_skip_bit.assert_awaited_once()


@pytest.mark.asyncio
async def test_skip_route_succeeds_when_skip_history_persistence_fails():
    """Once the cut is committed, history persistence is best-effort."""
    app = _make_test_app()
    persona_store = MagicMock()
    persona_store._session_id = "session-persistence-failure"
    persona_store.record_play = AsyncMock(side_effect=OSError("skip history unavailable"))
    app.state.station_state.persona_store = persona_store
    app.state.station_state.now_streaming = {
        "type": "music",
        "label": "Skipped Song",
        "started": time.time() - 8,
        "metadata": {"youtube_id": "yt_skip_failure"},
    }

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/skip")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert app.state.skip_event.is_set()
    assert app.state.station_state.now_streaming["type"] == "skipping"
    persona_store.record_play.assert_awaited_once()


@pytest.mark.asyncio
async def test_skip_bit_sets_pending_directive():
    """When detect_skip_bit returns True, ha_pending_directive is set for reactive banter."""
    app = _make_test_app()
    persona_store = MagicMock()
    persona_store._session_id = "session-3"
    persona_store.record_play = AsyncMock()
    app.state.station_state.persona_store = persona_store
    app.state.station_state.now_streaming = {
        "type": "music",
        "label": "Hated Song",
        "started": time.time() - 5,
        "metadata": {"youtube_id": "yt_hated", "title_only": "Brutta Canzone"},
    }

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.playlist.song_cues.detect_skip_bit", new=AsyncMock(return_value=True)):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/skip")

    assert resp.status_code == 200
    directive = app.state.station_state.ha_pending_directive
    assert "Brutta Canzone" in directive
    assert "saltato" in directive or "skippa" in directive


@pytest.mark.asyncio
async def test_get_root_serves_listener_page():
    """Root serves the public listener page (no auth required)."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    # Brand-engine PR-C: listener is now Jinja-templated. Assert on stable
    # structural elements (CTA, brand identity) — the tagline is now per-brand
    # via brand.tagline, so no longer a fixed string.
    assert "Mamma Mi Radio" in resp.text  # default brand from radio.toml
    # CTA copy is Super-Italian-Mode-aware. Default OFF renders English utility copy.
    assert "Listen Now" in resp.text
    assert "Manda al DJ" in resp.text  # dediche eyebrow stays Italian (decorative)
    assert 'data-cap="ha"' in resp.text  # capability-conditional rendering hooks present
    # Tail-anchored: tolerate non-strict-semver pyproject versions (rc/post/dev).
    assert re.search(r"-[a-f0-9]{8}$", _ASSET_VERSION)
    assert f"/static/listener.css?v={_ASSET_VERSION}" in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("bitrate_kbps", [192, 128])
async def test_listener_page_renders_configured_stream_bitrate(bitrate_kbps: int):
    """Every visible listener bitrate must match the canonical audio config."""
    app = _make_test_app()
    app.state.config.audio.bitrate = bitrate_kbps
    # Pin the frequency so the three frequency-gated ticker repetitions render
    # even if the default radio.toml brand is later changed.
    app.state.config.brand.frequency = "98.7 FM"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    # Assert each site class independently so a change to the ticker repetition
    # count can't silently mask the about-card losing its bitrate (a bare
    # total-count check passes if one site is dropped and another duplicated).
    # About-card: always visible, not frequency-gated.
    assert resp.text.count(f"Stream MP3</span> · {bitrate_kbps} kbps") == 1
    # Ticker: three frequency-prefixed repetitions.
    assert resp.text.count(f"98.7 FM · {bitrate_kbps} kbps") == 3
    # Total visible bitrate labels, and never the stale hardcoded value.
    assert resp.text.count(f"· {bitrate_kbps} kbps") == 4
    assert "320 kbps" not in resp.text


@pytest.mark.asyncio
async def test_listener_page_about_card_bitrate_survives_blank_frequency():
    """No frequency configured hides the ticker, but the always-visible about-card
    must still show the honest configured bitrate (the one ungated site)."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 128
    app.state.config.brand.frequency = ""
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    # Ticker sites are frequency-gated and gone; only the about-card remains.
    assert resp.text.count("· 128 kbps") == 1
    assert resp.text.count("Stream MP3</span> · 128 kbps") == 1
    assert "320 kbps" not in resp.text


@pytest.mark.asyncio
async def test_get_root_renders_italian_when_super_italian_on():
    """Super Italian Mode ON: CTA + form button render in Italian."""
    app = _make_test_app()
    app.state.config.super_italian_mode = True
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert "Ascolta Ora" in resp.text  # CTA in Italian
    assert "Spedisci con un bacio" in resp.text  # form submit in Italian
    assert "Listen Now" not in resp.text  # English CTA must be absent


@pytest.mark.asyncio
async def test_get_root_bakes_stopped_state_into_first_paint():
    """A stopped station bakes data-stopped + is-stopped into the first paint so it
    never flashes the live label before the JS poll hydrates (illusion/honesty)."""
    app = _make_test_app()
    app.state.config.super_italian_mode = False
    app.state.station_state.session_stopped = True
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert 'data-stopped="true"' in resp.text
    assert "is-stopped" in resp.text
    for control_id in ("nav-cta", "np-play", "hero-play"):
        assert re.search(
            rf'<button\b(?=[^>]*\bid="{control_id}")(?=[^>]*\baria-label="Station paused")(?=[^>]*\bdisabled\b)',
            resp.text,
        ), f"{control_id} must paint as a disabled paused-status control."
    assert not re.search(r'<button\b(?=[^>]*\bid="nav-cta")(?=[^>]*\baria-label="Listen now")', resp.text)
    assert "In Onda" not in resp.text  # live label must not flash on a stopped station


@pytest.mark.asyncio
async def test_get_root_paints_live_when_not_stopped():
    """Default (running) state paints the live indicators and emits no data-stopped."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert 'data-stopped="true"' not in resp.text
    assert "In Onda" in resp.text


@pytest.mark.asyncio
async def test_get_root_lang_attr_follows_copy_register():
    """<html lang> reflects the active copy register (WCAG 3.1.1) so a screen reader
    uses the right phoneme table for the copy actually on screen."""
    app_it = _make_test_app()
    app_it.state.config.super_italian_mode = True
    transport_it = httpx.ASGITransport(app=app_it, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport_it, base_url="http://testserver") as client:
        resp_it = await client.get("/")
    assert 'lang="it"' in resp_it.text

    app_en = _make_test_app()
    app_en.state.config.super_italian_mode = False
    transport_en = httpx.ASGITransport(app=app_en, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport_en, base_url="http://testserver") as client:
        resp_en = await client.get("/")
    assert 'lang="en"' in resp_en.text


@pytest.mark.asyncio
async def test_public_status_returns_json():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    body = resp.json()
    assert "station" in body
    assert "now_streaming" in body
    assert "upcoming" in body
    assert "upcoming_mode" in body
    assert "stream_log" in body
    # Item 19: listener.html relies on session_stopped being in the public
    # payload so it can freeze the launch-waveform when the operator pauses.
    assert "session_stopped" in body
    assert body["session_stopped"] is False  # default for fresh test app


@pytest.mark.asyncio
async def test_stream_delivery_diagnostics_are_bounded_anonymous_and_admin_only():
    app = _make_test_app()
    state = app.state.station_state
    state.listeners_active = 2
    state.playback_epoch = 7
    state.set_ha_context_refresh_stage("projection", started=10.0)
    state.record_stream_pacing_event(
        "late",
        lateness_ms=100,
        remaining_lead_ms=400,
        segment_type="music",
        timestamp=1_000.0,
        monotonic_now=10.1,
    )
    for index in range(22):
        state.record_stream_outcome(
            segment_type="music" if index % 2 == 0 else "banter",
            result="aired" if index % 3 else "fallback_aired",
            bytes_sent=4096 + index,
            starting_listener_count=2,
            terminal_reason="eof",
            timestamp=1_000.0 + index,
        )
    state.record_slow_listener_drops(2, timestamp=1_020.0)

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        admin = (await client.get("/status")).json()
        public = (await client.get("/public-status")).json()

    delivery = admin["runtime_status"]["stream_delivery"]
    assert delivery["target_lead_ms"] == 500
    assert delivery["late_threshold_ms"] == 50
    assert delivery["session"]["late"] == 1
    assert len(delivery["recent"]) == 1
    assert len(delivery["recent_stream_outcomes"]) == 20
    assert set(delivery["recent_stream_outcomes"][-1]) == {
        "timestamp",
        "segment_type",
        "result",
        "bytes_sent",
        "starting_listener_count",
        "terminal_reason",
    }
    assert delivery["slow_listener_drops"]["session"] == 2
    assert delivery["slow_listener_drops"]["last_drop_at"] == 1_020.0
    assert delivery["ha_refresh"]["stage"] == "projection"

    assert "runtime_status" not in public
    assert "stream_delivery" not in public
    assert "ha_refresh" not in public
    assert "ha_context_refresh_stage" not in public


@pytest.mark.asyncio
async def test_public_status_reflects_session_stopped_flag():
    app = _make_test_app()
    app.state.station_state.session_stopped = True
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    assert resp.json()["session_stopped"] is True


@pytest.mark.asyncio
async def test_public_status_upcoming_mode_building_when_queue_empty():
    app = _make_test_app()
    # Queue is empty -- only render-ready segments belong in the public schedule.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["upcoming"] == []
    assert body["upcoming_mode"] == "building"


@pytest.mark.asyncio
async def test_public_status_needs_music_source_and_building_queue_together(monkeypatch):
    """No configured music source AND an empty render queue must both show up in
    the same response -- the two "getting started" surfaces (listener no-source
    banner, admin no-source empty state) both key off this combination, so a
    change that decouples them again must fail here, not silently in the UI."""
    from mammamiradio.web import status_payload as status_payload_mod

    monkeypatch.setattr(status_payload_mod, "_golden_path_cache", None)
    monkeypatch.setattr(status_payload_mod, "_golden_path_cache_ts", 0.0)
    monkeypatch.delenv("MAMMAMIRADIO_ALLOW_YTDLP", raising=False)

    app = _make_test_app()
    app.state.station_state.playlist = []
    transport = httpx.ASGITransport(app=app)
    with patch("mammamiradio.web.status_payload._has_any_mp3", return_value=False):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/public-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["golden_path"]["stage"] == "needs_music_source"
    assert body["upcoming"] == []
    assert body["upcoming_mode"] == "building"


@pytest.mark.asyncio
async def test_public_status_session_stopped_alongside_needs_music_source(monkeypatch):
    """A stopped station with no music source still reports both flags plainly --
    the listener/admin UIs are responsible for prioritizing "stopped" copy over
    "no source" copy; the backend never collapses one signal into the other."""
    from mammamiradio.web import status_payload as status_payload_mod

    monkeypatch.setattr(status_payload_mod, "_golden_path_cache", None)
    monkeypatch.setattr(status_payload_mod, "_golden_path_cache_ts", 0.0)
    monkeypatch.delenv("MAMMAMIRADIO_ALLOW_YTDLP", raising=False)

    app = _make_test_app()
    app.state.station_state.session_stopped = True
    app.state.station_state.playlist = []
    transport = httpx.ASGITransport(app=app)
    with patch("mammamiradio.web.status_payload._has_any_mp3", return_value=False):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/public-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_stopped"] is True
    assert body["golden_path"]["stage"] == "needs_music_source"


@pytest.mark.asyncio
async def test_public_status_upcoming_mode_queued_with_shadow_queue():
    app = _make_test_app()
    app.state.queue.put_nowait(Segment(type=SegmentType.MUSIC, path=Path("/tmp/fake.mp3"), metadata={}))
    app.state.station_state.queued_segments = [{"type": "music", "label": "Queued Song"}]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["upcoming"] == [{"type": "music", "label": "Queued Song", "source": "rendered_queue"}]
    assert body["upcoming_mode"] == "queued"


@pytest.mark.asyncio
async def test_setup_status_returns_onboarding_payload():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/setup/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "detected_mode" in body
    assert "essentials" in body
    assert "preflight_checks" in body
    assert "launch" in body
    assert "signature" in body


@pytest.mark.asyncio
async def test_setup_status_and_recheck_share_projection():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        status = (await client.get("/api/setup/status")).json()
        recheck = (await client.post("/api/setup/recheck")).json()

    assert recheck["signature"] == status["signature"]
    assert recheck["guided_setup"] == status["guided_setup"]
    assert recheck["recommended_next_action"] == status["recommended_next_action"]


@pytest.mark.asyncio
async def test_setup_recovery_endpoints_remain_available_while_session_stopped():
    """Paused transport must not lock operators out of setup and diagnostics."""
    app = _make_test_app()
    app.state.station_state.session_stopped = True
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    with patch("mammamiradio.web.streamer._persist_and_apply_credentials", new=AsyncMock()) as persist:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            recheck = await client.post("/api/setup/recheck")
            preview = await client.get("/api/homeassistant/context-candidates")
            save_keys = await client.post("/api/setup/save-keys", json={"ANTHROPIC_API_KEY": "sk-test"})

    assert recheck.status_code == 200
    assert preview.status_code == 200
    assert save_keys.status_code == 200
    assert save_keys.json()["ok"] is True
    persist.assert_awaited_once()
    assert app.state.station_state.session_stopped is True


@pytest.mark.asyncio
async def test_setup_recheck_bypasses_golden_path_ttl_cache(monkeypatch):
    from mammamiradio.web import status_payload as status_payload_mod

    monkeypatch.setattr(status_payload_mod, "_golden_path_cache", None)
    monkeypatch.setattr(status_payload_mod, "_golden_path_cache_ts", 0.0)
    monkeypatch.setattr(status_payload_mod, "_golden_path_cache_key", None)
    monkeypatch.delenv("MAMMAMIRADIO_ALLOW_YTDLP", raising=False)

    app = _make_test_app()
    app.state.config.allow_ytdlp = False
    app.state.station_state.playlist = []
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.status_payload._has_any_mp3", side_effect=[False, False, False, True]):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            status = (await client.get("/api/setup/status")).json()
            recheck = (await client.post("/api/setup/recheck")).json()

    assert recheck["guided_setup"]["stream"]["status"] == "ready"
    assert status["guided_setup"]["stream"]["status"] == "blocked"
    assert status["onboarding_required"] is True
    assert recheck["onboarding_required"] is False


@pytest.mark.asyncio
async def test_status_includes_station_mode():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "station_mode" in body
    assert "id" in body["station_mode"]
    assert "provider_health" in body


@pytest.mark.asyncio
async def test_status_includes_direct_cast_diagnostics_and_public_status_omits_them():
    app = _make_test_app()
    app.state.config.ads.cast_report = SimpleNamespace(
        excluded_brands=frozenset({"Broken Campaign"}),
        warnings=("Broken Campaign has no approved direct character",),
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        admin = (await client.get("/status")).json()
        public = (await client.get("/public-status")).json()

    assert admin["ad_cast"] == {
        "excluded_campaigns": ["Broken Campaign"],
        "warnings": ["Broken Campaign has no approved direct character"],
    }
    assert "ad_cast" not in public


def test_ad_cast_status_payload_rejects_unexpected_report_shapes():
    config = SimpleNamespace(ads=SimpleNamespace(cast_report=SimpleNamespace(excluded_brands="bad", warnings="bad")))

    assert _ad_cast_status_payload(config) == {"excluded_campaigns": [], "warnings": []}


@pytest.mark.asyncio
async def test_status_buffered_audio_sec_sums_real_queue_durations():
    """buffered_audio_sec surfaces airtime ahead (seconds), not item count."""
    app = _make_test_app()
    app.state.queue.put_nowait(Segment(type=SegmentType.MUSIC, path=Path("/tmp/a.mp3"), duration_sec=180.0))
    app.state.queue.put_nowait(Segment(type=SegmentType.BANTER, path=Path("/tmp/b.mp3"), duration_sec=12.5))
    app.state.queue.put_nowait(Segment(type=SegmentType.MUSIC, path=Path("/tmp/c.mp3")))
    app.state.station_state.queued_segments = [
        {"type": "music", "label": "A"},
        {"type": "banter", "label": "B"},
        {"type": "music", "label": "C", "duration_sec": 999.0},
    ]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    assert resp.json()["buffered_audio_sec"] == 192.5


@pytest.mark.asyncio
async def test_status_buffered_audio_sec_zero_when_queue_empty():
    """Empty real queue -> 0.0 (UI hides the readout; never a dead '0s' box)."""
    app = _make_test_app()
    app.state.station_state.queued_segments = []
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    assert resp.json()["buffered_audio_sec"] == 0.0


@pytest.mark.asyncio
async def test_status_buffered_audio_sec_respects_drift_guard():
    """The drift guard still trims stale shadow entries, but seconds come from the real queue."""
    app = _make_test_app()
    app.state.queue.put_nowait(Segment(type=SegmentType.MUSIC, path=Path("/tmp/fake.mp3"), duration_sec=180.0))
    app.state.station_state.queued_segments = [
        {"type": "music", "label": "A", "duration_sec": 1.0},
        {"type": "music", "label": "B", "duration_sec": 120.0},
        {"type": "music", "label": "C", "duration_sec": 60.0},
    ]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    assert resp.json()["buffered_audio_sec"] == 180.0
    assert app.state.station_state.shadow_queue_corrections == 1


@pytest.mark.asyncio
async def test_status_operator_force_pending_set_only_by_trigger():
    """The panel's "Triggered" row must reflect OPERATOR action only: /api/trigger
    sets operator_force_pending, but an internal force (the silence-rescue setting
    force_next directly) must NOT — otherwise the panel lies during an incident.
    """
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Internal force (simulates the 60s-silence dead-air rescue / stop-skip music force).
        app.state.station_state.force_next = SegmentType.BANTER
        body = (await client.get("/status")).json()
        assert body["force_pending"] == "banter"
        assert body["operator_force_pending"] is None  # not operator-attributed -> no Triggered row

        # Operator trigger.
        trig = await client.post("/api/trigger", json={"type": "ad"})
        assert trig.status_code == 200
        body = (await client.get("/status")).json()
        assert body["operator_force_pending"] == "ad"


@pytest.mark.asyncio
async def test_trigger_rejects_second_while_one_pending():
    """Air-next builds one trigger at a time: a second tap while one is still
    pending is rejected with a human way-out message (leadership #5), never a
    silent overwrite of the operator's first pick."""
    from mammamiradio.core.models import SegmentType

    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post("/api/trigger", json={"type": "banter"})
        assert first.json()["ok"] is True
        second = await client.post("/api/trigger", json={"type": "ad"})
        body = second.json()
    assert body["ok"] is False
    assert "tap again" in body["error"].lower()  # a way out, not a dead end
    # The operator's first pick is preserved, not overwritten by the rejected tap.
    assert app.state.station_state.operator_force_pending == SegmentType.BANTER


@pytest.mark.asyncio
async def test_trigger_rejects_operator_pick_while_session_stopped():
    """A visually disabled Air Next control must also be rejected server-side."""
    app = _make_test_app()
    app.state.station_state.session_stopped = True
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/trigger", json={"type": "banter"})

    body = response.json()
    assert body["ok"] is False
    assert "paused" in body["error"].lower()
    assert "press start" in body["error"].lower()
    assert app.state.station_state.force_next is None
    assert app.state.station_state.operator_force_pending is None


@pytest.mark.asyncio
async def test_skip_rejects_while_session_stopped_without_mutating_playback():
    """The routine Next-track control cannot change a stopped session's transport."""
    app = _make_test_app()
    state = app.state.station_state
    state.session_stopped = True
    state.now_streaming = {
        "type": "stopped",
        "label": "Session stopped",
        "started": 123.0,
        "metadata": {},
    }
    before = dict(state.now_streaming)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/skip")

    body = response.json()
    assert body["ok"] is False
    assert "paused" in body["error"].lower()
    assert "press start" in body["error"].lower()
    assert state.now_streaming == before
    assert state.force_next is None
    assert not app.state.skip_event.is_set()


@pytest.mark.asyncio
async def test_setup_recheck_returns_onboarding_payload():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/setup/recheck")
    assert resp.status_code == 200
    body = resp.json()
    assert "detected_mode" in body
    assert "station_mode" in body
    assert "signature" in body


@pytest.mark.asyncio
async def test_setup_provider_check_returns_secret_safe_probe_payload():
    app = _make_test_app()
    app.state.config.anthropic_api_key = "anthropic-secret"
    app.state.config.openai_api_key = "openai-secret"
    probe_payload = {
        "ok": True,
        "providers": {
            "anthropic": {
                "provider": "anthropic",
                "configured": True,
                "ok": False,
                "status_code": 401,
                "error_type": "authentication_error",
                "detail": "authentication_error invalid x-api-key",
            },
            "openai_chat": {
                "provider": "openai_chat",
                "configured": True,
                "ok": True,
                "status_code": 200,
                "error_type": "",
                "detail": "",
            },
            "openai_tts": {
                "provider": "openai_tts",
                "configured": True,
                "ok": True,
                "status_code": 200,
                "error_type": "",
                "detail": "",
            },
        },
    }
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer.check_provider_keys", new=AsyncMock(return_value=probe_payload)) as probe:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/setup/provider-check")

    assert resp.status_code == 200
    body = resp.json()
    assert body == probe_payload
    assert "anthropic-secret" not in resp.text
    assert "openai-secret" not in resp.text
    probe.assert_awaited_once_with(app.state.config)


@pytest.mark.asyncio
async def test_setup_provider_check_shares_in_flight_probe():
    app = _make_test_app()
    probe_payload = {"ok": True, "providers": {}}
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_probe(config):
        assert config is app.state.config
        started.set()
        await release.wait()
        return probe_payload

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer.check_provider_keys", new=AsyncMock(side_effect=slow_probe)) as probe:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = asyncio.create_task(client.post("/api/setup/provider-check"))
            await started.wait()
            second = asyncio.create_task(client.post("/api/setup/provider-check"))
            await asyncio.sleep(0)
            release.set()
            first_resp, second_resp = await asyncio.gather(first, second)

    assert first_resp.status_code == 200
    assert second_resp.status_code == 200
    assert first_resp.json() == probe_payload
    assert second_resp.json() == probe_payload
    assert probe.await_count == 1


@pytest.mark.asyncio
async def test_setup_provider_check_returns_cached_result_within_debounce_window():
    """Second call within the 2 s debounce window returns cached result without re-probing."""
    app = _make_test_app()
    probe_payload = {"ok": True, "providers": {}}
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer.check_provider_keys", new=AsyncMock(return_value=probe_payload)) as probe:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = await client.post("/api/setup/provider-check")
            second = await client.post("/api/setup/provider-check")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == probe_payload
    assert second.json() == probe_payload
    assert probe.await_count == 1


@pytest.mark.asyncio
async def test_setup_provider_check_clears_task_on_exception():
    """If check_provider_keys raises, the in-flight task reference is cleared so next call retries."""
    app = _make_test_app()
    # raise_app_exceptions=False: converts server errors to 500 responses rather
    # than propagating them, so we can inspect app state after the failure.
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345), raise_app_exceptions=False)
    with patch(
        "mammamiradio.web.streamer.check_provider_keys",
        new=AsyncMock(side_effect=RuntimeError("probe failed")),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/setup/provider-check")

    assert resp.status_code == 500
    assert app.state._provider_check_task is None


@pytest.mark.asyncio
async def test_setup_provider_check_clears_task_on_cancel():
    """Cancelling the in-flight provider-check task clears the task reference."""
    app = _make_test_app()
    barrier = asyncio.Event()

    async def slow_probe(_config):
        barrier.set()
        await asyncio.sleep(10)
        return {"anthropic": True}

    with patch("mammamiradio.web.streamer.check_provider_keys", new=slow_probe):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            check_coro = client.post("/api/setup/provider-check")
            check_task = asyncio.create_task(check_coro)
            await barrier.wait()
            # Cancel the in-flight probe at the app-state level, then let the
            # HTTP task observe the cancellation.
            probe_task = app.state._provider_check_task
            assert probe_task is not None
            probe_task.cancel()
            try:
                await check_task
            except (asyncio.CancelledError, httpx.RemoteProtocolError):
                pass

    assert getattr(app.state, "_provider_check_task", None) is None


@pytest.mark.asyncio
async def test_addon_snippet_returns_snippet():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/setup/addon-snippet")
    assert resp.status_code == 200
    body = resp.json()
    assert "snippet" in body


@pytest.mark.asyncio
async def test_setup_save_keys_updates_live_config_without_disk_write():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    import os as _os

    _prev_anthropic = _os.environ.get("ANTHROPIC_API_KEY")
    _prev_openai = _os.environ.get("OPENAI_API_KEY")
    try:
        with patch("mammamiradio.web.streamer._save_dotenv") as save_dotenv:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post(
                    "/api/setup/save-keys",
                    json={"ANTHROPIC_API_KEY": "ant-test\nEVIL=1", "OPENAI_API_KEY": "openai-test\rEVIL=1"},
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "ANTHROPIC_API_KEY" in body["saved"]
        assert "OPENAI_API_KEY" in body["saved"]
        assert app.state.config.anthropic_api_key == "ant-testEVIL=1"
        assert app.state.config.openai_api_key == "openai-testEVIL=1"
        save_dotenv.assert_called_once()
        assert save_dotenv.call_args.args[0] == {
            "ANTHROPIC_API_KEY": "ant-testEVIL=1",
            "OPENAI_API_KEY": "openai-testEVIL=1",
        }
    finally:
        # Restore env to avoid polluting subsequent tests
        if _prev_anthropic is None:
            _os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            _os.environ["ANTHROPIC_API_KEY"] = _prev_anthropic
        if _prev_openai is None:
            _os.environ.pop("OPENAI_API_KEY", None)
        else:
            _os.environ["OPENAI_API_KEY"] = _prev_openai


@pytest.mark.asyncio
async def test_setup_save_keys_in_addon_mode_uses_addon_secret_file():
    app = _make_test_app(is_addon=True)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    previous = os.environ.get("ANTHROPIC_API_KEY")

    try:
        with (
            patch("mammamiradio.web.streamer._save_addon_options") as save_addon_options,
            patch("mammamiradio.web.streamer._save_dotenv") as save_dotenv,
            patch(
                "mammamiradio.web.provider_verdict.check_provider_keys",
                new=AsyncMock(return_value=_probe_payload(anthropic="ok")),
            ),
        ):
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post("/api/setup/save-keys", json={"ANTHROPIC_API_KEY": "sk-addon"})

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        save_addon_options.assert_called_once_with({"ANTHROPIC_API_KEY": "sk-addon"})
        save_dotenv.assert_not_called()
    finally:
        if previous is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = previous


@pytest.mark.asyncio
async def test_setup_save_keys_rejects_empty_payload():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/setup/save-keys", json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "No keys provided" in body["error"]


@pytest.mark.asyncio
async def test_admin_status_without_auth_public_ip_rejected():
    """Public IP client without credentials should be rejected."""
    app = _make_test_app(admin_password="secret123")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_status_private_network_rejected_when_password_set():
    """When admin_password is configured, a LAN client must still authenticate —
    private-network trust no longer bypasses configured credentials."""
    app = _make_test_app(admin_password="secret123")
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_status_private_network_trusted_without_creds():
    """With no admin creds configured, a LAN client is still trusted (CSRF-guarded)."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_admin_status_with_basic_auth():
    app = _make_test_app(admin_password="secret123")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status", auth=("admin", "secret123"))
    assert resp.status_code == 200
    body = resp.json()
    assert "queue_depth" in body
    assert "segments_produced" in body
    assert "runtime_health" in body


@pytest.mark.asyncio
async def test_admin_status_exposes_ha_label_stats_and_registry_source():
    app = _make_test_app()
    state = app.state.station_state
    state.ha_context = "- Luce bancone: accesa"
    state.ha_catalog_hit_rate = 0.5
    state.ha_label_stats = {"eligible": 3, "curated": 1, "catalog_hits": 1, "fallback": 1, "catalog_hit_rate": 0.5}
    state.ha_registry_source = "disk_stale"

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")

    assert resp.status_code == 200
    details = resp.json()["ha_details"]
    assert details["catalog_hit_rate"] == 0.5
    assert details["label_stats"]["catalog_hits"] == 1
    assert details["registry_source"] == "disk_stale"


@pytest.mark.asyncio
async def test_admin_status_with_token():
    app = _make_test_app(admin_token="tok-abc-123")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status", headers={"X-Radio-Admin-Token": "tok-abc-123"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_admin_status_bad_credentials():
    app = _make_test_app(admin_password="secret123")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status", auth=("admin", "wrong"))
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_skip_with_admin_auth():
    app = _make_test_app(admin_password="secret123")
    # Put something in now_streaming so skip has something to act on
    app.state.station_state.now_streaming = {"type": "music", "label": "Test", "started": time.time()}
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/skip", auth=("admin", "secret123"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True


@pytest.mark.asyncio
async def test_stop_and_resume_toggle_session_state():
    app = _make_test_app()
    app.state.station_state.now_streaming = {"type": "music", "label": "Test", "started": time.time()}
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        stop = await client.post("/api/stop")
        assert stop.status_code == 200
        assert app.state.station_state.session_stopped is True
        assert app.state.station_state.now_streaming["type"] == "stopped"

        resume = await client.post("/api/resume")
        assert resume.status_code == 200
        assert app.state.station_state.session_stopped is False
        assert app.state.station_state.now_streaming == {}


@pytest.mark.asyncio
async def test_stop_clears_pending_interrupt_and_force_next(tmp_path):
    """A deliberate stop must drop any pending interrupt/forced segment so it
    cannot fire as stale audio on the next resume, and must unlink an ephemeral
    interrupt bridge temp so the stop does not leak it."""
    from mammamiradio.core.models import SegmentType

    app = _make_test_app()
    state = app.state.station_state
    bridge = tmp_path / "interrupt_bridge.mp3"
    bridge.write_bytes(b"id3")
    state.interrupt_slot = bridge
    state.interrupt_slot_ephemeral = True
    state.force_next = SegmentType.BANTER

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/stop")

    assert resp.status_code == 200
    assert state.interrupt_slot is None
    assert state.interrupt_slot_ephemeral is False
    assert state.force_next is None
    assert not bridge.exists()


@pytest.mark.asyncio
async def test_panic_cut_while_streaming():
    """Panic with fresh runway skips safely and forces the next segment to music."""
    from mammamiradio.core.models import SegmentType

    app = _make_test_app()
    state = app.state.station_state
    state.now_streaming = {"type": "music", "label": "Test", "started": time.time()}
    # Pre-populate shadow queue so we can verify it is cleared
    state.queued_segments.append({"type": "banter"})  # type: ignore[attr-defined]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/panic")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "purged" in data
    assert data["skipped"] is True
    # skip_event must have been set (skip fires for the current segment)
    assert app.state.skip_event.is_set()
    # force_next must be MUSIC
    assert state.force_next == SegmentType.MUSIC
    # session_stopped must NOT be set — stream stays live
    assert state.session_stopped is False
    # Stale rows are replaced by an audible protected reservation.
    assert len(state.queued_segments) == app.state.queue.qsize() == 1
    assert state.queued_segments[0]["reason"] == "Protected continuity audio."


@pytest.mark.asyncio
async def test_panic_cut_does_not_skip_when_no_ready_runway(tmp_path):
    """Panic still steers recovery, but never cuts current audio into an empty queue."""
    app = _make_test_app()
    state = app.state.station_state
    state.now_streaming = {"type": "music", "label": "Test", "started": time.time()}
    state.continuity_epoch = 5
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/panic")

    assert response.json() == {"ok": True, "purged": 0, "skipped": False}
    assert app.state.queue.empty()
    assert not app.state.skip_event.is_set()
    assert state.force_next is SegmentType.MUSIC
    assert state.continuity_epoch == 6

    # A render that captured the old epoch before Panic must now fail the same
    # admission gate used by the producer, even though the queue was untouched.
    captured_epoch = 5
    assert captured_epoch != state.continuity_epoch


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/api/skip", "/api/track/ban-now-playing"])
async def test_skip_controls_bridge_after_discarding_stale_companionship_only_runway(tmp_path, endpoint):
    """A rejected cue cannot hide that an explicit cut needs forced music."""
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    now, listener_id, _, stale_cue, claim = _queue_companionship_cue(app, tmp_path)
    app.state.stream_hub.unsubscribe(listener_id)
    now[0] = 2_400.0
    app.state.stream_hub.subscribe()
    state = app.state.station_state
    state.now_streaming = {
        "type": "music",
        "label": "Current Artist — Current Song",
        "started": time.time(),
        "metadata": {"artist": "Current Artist", "title_only": "Current Song"},
    }
    stale_cue.ephemeral = True
    stale_cue.metadata["ritual_moment_id"] = "stale-skip-moment"
    stale_path = stale_cue.path
    state.moment_store = MagicMock()
    assert state.listener_session.epoch == claim.epoch + 1
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(endpoint)

    assert response.json()["bridged"] is True
    assert app.state.queue.empty()
    assert state.queued_segments == []
    assert state.force_next is SegmentType.MUSIC
    assert app.state.skip_event.is_set()
    assert state.discard_by_reason[GenerationWasteReason.LISTENER_SESSION_STALE] == 1
    assert not stale_path.exists()
    state.moment_store.mark_dropped.assert_called_once_with(
        "stale-skip-moment",
        GenerationWasteReason.LISTENER_SESSION_STALE,
    )
    assert app.state.queue._unfinished_tasks == 0
    await asyncio.wait_for(app.state.queue.join(), timeout=1.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/api/skip", "/api/track/ban-now-playing"])
async def test_skip_controls_promote_safe_audio_past_stale_companionship_cue(tmp_path, endpoint):
    """Skip and Ban-now cut to the first segment playback will accept."""
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    now, listener_id, _, stale_cue, claim = _queue_companionship_cue(app, tmp_path)
    app.state.stream_hub.unsubscribe(listener_id)
    now[0] = 2_400.0
    app.state.stream_hub.subscribe()
    state = app.state.station_state
    state.now_streaming = {
        "type": "music",
        "label": "Current Artist — Current Song",
        "started": time.time(),
        "metadata": {"artist": "Current Artist", "title_only": "Current Song"},
    }
    assert state.listener_session.epoch == claim.epoch + 1

    safe_path = tmp_path / "safe_after_stale_skip.mp3"
    safe_path.write_bytes(b"safe-audio")
    safe = Segment(
        type=SegmentType.MUSIC,
        path=safe_path,
        duration_sec=180.0,
        metadata={
            "queue_id": "safe-after-stale-skip",
            "title": "Safe after stale skip",
            "title_only": "Safe after stale skip",
            "artist": "Safe Artist",
        },
        ephemeral=False,
    )
    app.state.queue.put_nowait(safe)
    safe_shadow = {
        "id": "safe-after-stale-skip",
        "type": "music",
        "label": "Safe after stale skip",
        "duration_sec": 180.0,
    }
    state.queued_segments.append(safe_shadow)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(endpoint)

    assert response.json()["bridged"] is False
    assert list(app.state.queue._queue) == [safe]
    assert stale_cue not in app.state.queue._queue
    assert state.queued_segments == [safe_shadow]
    assert state.force_next is None
    assert app.state.skip_event.is_set()
    assert state.discard_by_reason[GenerationWasteReason.LISTENER_SESSION_STALE] == 1
    assert app.state.queue._unfinished_tasks == 1
    assert app.state.queue.get_nowait() is safe
    app.state.queue.task_done()
    await asyncio.wait_for(app.state.queue.join(), timeout=1.0)


@pytest.mark.asyncio
async def test_panic_cut_does_not_skip_for_stale_companionship_only_runway(tmp_path):
    """A cue rejected by playback cannot justify cutting the current segment."""
    app = _make_test_app()
    now, listener_id, _, stale_cue, claim = _queue_companionship_cue(app, tmp_path)
    app.state.stream_hub.unsubscribe(listener_id)
    now[0] = 2_400.0
    app.state.stream_hub.subscribe()
    state = app.state.station_state
    state.now_streaming = {"type": "music", "label": "Current", "started": time.time()}
    assert state.listener_session.epoch == claim.epoch + 1
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/panic")

    assert response.json() == {"ok": True, "purged": 0, "skipped": False}
    assert list(app.state.queue._queue) == [stale_cue]
    assert not app.state.skip_event.is_set()
    assert state.force_next is SegmentType.MUSIC


@pytest.mark.asyncio
async def test_panic_cut_promotes_safe_audio_past_stale_companionship_cue(tmp_path):
    """Panic may cut only into a head the playback cue fence will accept."""
    app = _make_test_app()
    now, listener_id, _, stale_cue, claim = _queue_companionship_cue(app, tmp_path)
    app.state.stream_hub.unsubscribe(listener_id)
    now[0] = 2_400.0
    app.state.stream_hub.subscribe()
    state = app.state.station_state
    assert state.listener_session.epoch == claim.epoch + 1

    safe_path = tmp_path / "safe_after_stale_cue.mp3"
    safe_path.write_bytes(b"safe-audio")
    safe = Segment(
        type=SegmentType.MUSIC,
        path=safe_path,
        duration_sec=180.0,
        metadata={
            "queue_id": "safe-after-stale-cue",
            "title": "Safe after stale cue",
            "title_only": "Safe after stale cue",
            "artist": "Safe Artist",
        },
        ephemeral=False,
    )
    app.state.queue.put_nowait(safe)
    state.queued_segments.append(
        {
            "id": "safe-after-stale-cue",
            "type": "music",
            "label": "Safe after stale cue",
            "duration_sec": 180.0,
        }
    )
    state.now_streaming = {"type": "music", "label": "Current", "started": time.time()}
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/panic")

    assert response.json() == {"ok": True, "purged": 1, "skipped": True}
    assert list(app.state.queue._queue) == [safe]
    assert stale_cue not in app.state.queue._queue
    assert state.queued_segments == [
        {
            "id": "safe-after-stale-cue",
            "type": "music",
            "label": "Safe after stale cue",
            "duration_sec": 180.0,
        }
    ]
    assert state.discard_by_reason[GenerationWasteReason.OPERATOR_PANIC] == 1
    assert app.state.skip_event.is_set()


@pytest.mark.asyncio
async def test_panic_cut_invalidates_in_flight_admission_when_queue_is_unchanged(tmp_path):
    """Panic fences a render waiting on queue admission even without a purge."""
    from mammamiradio.scheduling.producer import _enqueue_with_egress

    class BlockingQueue(asyncio.Queue[Segment]):
        def __init__(self) -> None:
            super().__init__()
            self.put_started = asyncio.Event()
            self.allow_put = asyncio.Event()

        async def put(self, item: Segment) -> None:
            self.put_started.set()
            await self.allow_put.wait()
            await super().put(item)

    app = _make_test_app()
    state = app.state.station_state
    state.now_streaming = {"type": "music", "label": "Test", "started": time.time()}
    state.continuity_epoch = 5
    queue = BlockingQueue()
    app.state.queue = queue
    candidate_path = tmp_path / "stale_panic_candidate.mp3"
    candidate_path.write_bytes(b"candidate")
    candidate = Segment(
        type=SegmentType.MUSIC,
        path=candidate_path,
        duration_sec=180.0,
        metadata={"title": "Stale candidate", "title_only": "Stale candidate", "artist": "Artist"},
        ephemeral=True,
    )
    captured_epoch = state.continuity_epoch

    def stale_reason() -> str | None:
        if state.continuity_epoch != captured_epoch:
            return GenerationWasteReason.STALE_CONTINUITY
        return None

    with (
        patch("mammamiradio.scheduling.producer._apply_egress", new_callable=AsyncMock, return_value=candidate),
        patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"),
    ):
        enqueue_task = asyncio.create_task(
            _enqueue_with_egress(
                queue,
                state,
                app.state.config,
                candidate,
                shadow_entry={"id": "candidate", "type": "music", "label": "Stale candidate"},
                stale_check=stale_reason,
            )
        )
        try:
            await asyncio.wait_for(queue.put_started.wait(), timeout=1.0)
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post("/api/panic")

            assert response.json() == {"ok": True, "purged": 0, "skipped": False}
            assert state.continuity_epoch == captured_epoch + 1

            queue.allow_put.set()
            assert await asyncio.wait_for(enqueue_task, timeout=1.0) is False
        finally:
            queue.allow_put.set()
            if not enqueue_task.done():
                enqueue_task.cancel()
            await asyncio.gather(enqueue_task, return_exceptions=True)

    assert queue.empty()
    assert state.queued_segments == []
    assert state.discard_by_reason[GenerationWasteReason.STALE_CONTINUITY] == 1
    assert not candidate_path.exists()


@pytest.mark.asyncio
async def test_panic_cut_uses_capacity_exempt_slot_as_playable_runway(tmp_path):
    """Panic may cut when the protected slot, rather than the queue head, is ready."""
    app = _make_test_app()
    state = app.state.station_state
    state.now_streaming = {"type": "music", "label": "Test", "started": time.time()}
    state.continuity_epoch = 5
    slot_path = tmp_path / "capacity_exempt_slot.mp3"
    slot_path.write_bytes(b"slot")
    slot = Segment(
        type=SegmentType.BANTER,
        path=slot_path,
        duration_sec=4.44,
        metadata={"title": "Protected continuity", "continuity_reservation": True},
        ephemeral=False,
    )
    state.continuity_slot = slot
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/panic")

    assert response.json() == {"ok": True, "purged": 0, "skipped": True}
    assert app.state.queue.empty()
    assert app.state.skip_event.is_set()
    assert state.continuity_slot is slot
    assert state.force_next is SegmentType.MUSIC
    assert state.continuity_epoch == 6


@pytest.mark.asyncio
async def test_panic_cut_when_idle():
    """Panic while nothing is playing: skip_event stays unset, force_next still set to music."""
    from mammamiradio.core.models import SegmentType

    app = _make_test_app()
    state = app.state.station_state
    state.now_streaming = None  # nothing streaming
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/panic")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["skipped"] is False
    # No segment to skip — skip_event should not be fired
    assert not app.state.skip_event.is_set()
    assert state.force_next == SegmentType.MUSIC
    assert state.session_stopped is False


@pytest.mark.asyncio
async def test_panic_does_not_set_session_stopped():
    """Panic must never set session_stopped — that would drop all active listeners."""
    app = _make_test_app()
    state = app.state.station_state
    state.now_streaming = {"type": "banter", "label": "AI banter", "started": time.time()}
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await client.post("/api/panic")
    assert state.session_stopped is False


@pytest.mark.asyncio
async def test_panic_rejects_while_stopped_without_mutating_transport_or_queue(tmp_path):
    """Panic Cut is a live transport action, so a stopped station rejects it unchanged."""
    app = _make_test_app()
    state = app.state.station_state
    queued = Segment(
        type=SegmentType.MUSIC,
        path=tmp_path / "queued.mp3",
        metadata={"title": "Queued"},
    )
    app.state.queue.put_nowait(queued)
    state.queued_segments = [{"type": "music", "label": "Queued", "metadata": {}}]
    state.session_stopped = True
    state.now_streaming = {
        "type": "stopped",
        "label": "Session stopped",
        "started": 123.0,
        "metadata": {},
    }
    state.force_next = SegmentType.BANTER
    before_now = dict(state.now_streaming)
    before_shadow = list(state.queued_segments)

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/panic")

    body = response.json()
    assert body["ok"] is False
    assert "paused" in body["error"].lower()
    assert "press start" in body["error"].lower()
    assert list(app.state.queue._queue) == [queued]
    assert state.queued_segments == before_shadow
    assert state.now_streaming == before_now
    assert state.force_next is SegmentType.BANTER
    assert not app.state.skip_event.is_set()


@pytest.mark.asyncio
async def test_interrupt_remains_available_after_session_stop():
    """The emergency interrupt is intentional recovery, not a routine stopped transport control."""
    app = _make_test_app()
    app.state.station_state.session_stopped = True
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    with patch(
        "mammamiradio.scheduling.producer._fire_interrupt",
        new=AsyncMock(return_value=True),
    ) as fire_interrupt:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/api/interrupt",
                json={"directive": "Return to air with a short recovery message.", "urgency": "urgent"},
            )

    assert response.json()["ok"] is True
    fire_interrupt.assert_awaited_once()


@pytest.mark.asyncio
async def test_loopback_bypasses_auth_when_no_password():
    """Loopback client with no admin_password/token configured gets through."""
    app = _make_test_app()  # no password, no token
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_public_ip_no_auth_configured_rejected():
    """Public IP with no auth configured gets 403."""
    app = _make_test_app()  # no password, no token
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# PWA static asset routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sw_js_returns_javascript():
    """GET /sw.js should return the service worker with correct content-type."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/sw.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "CACHE_NAME" in resp.text


@pytest.mark.asyncio
async def test_sw_js_keeps_css_and_js_network_first():
    """Visual assets must not stay cache-first after a UI bug ships."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/sw.js")

    assert resp.status_code == 200
    text = resp.text
    assert "radio-itali-v6" in text
    assert "const isFreshAsset" in text
    assert "path.endsWith('.css')" in text
    assert "path.endsWith('.js')" in text
    assert "const isStableInstallAsset" in text

    stable_cache_block = text.split("const isStableInstallAsset", maxsplit=1)[1]
    assert "path.endsWith('.css')" not in stable_cache_block
    assert "path.endsWith('.js')" not in stable_cache_block


@pytest.mark.asyncio
async def test_static_manifest_returns_json():
    """GET /static/manifest.json should serve the PWA manifest."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/static/manifest.json")
    assert resp.status_code == 200
    assert "Radio" in resp.text


@pytest.mark.asyncio
async def test_static_nonexistent_returns_404():
    """GET /static/nonexistent.txt should return 404."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/static/nonexistent.txt")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_static_path_traversal_blocked():
    """Path traversal attempts in /static/ should return 404."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/static/../streamer.py")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_static_symlink_escape_blocked(tmp_path, monkeypatch):
    """GET /static/escape-link should return 404 when the symlink points outside static dir."""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "escape-link").symlink_to("../outside.txt")

    monkeypatch.setattr("mammamiradio.web.streamer._STATIC_DIR", static_dir)

    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/static/escape-link")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_regia_route_removed():
    """GET /regia must return 404 — the obsolete prototype was removed; admin lives at /admin."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/regia")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /admin route tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_panel_loopback_no_password_returns_html():
    """GET /admin on loopback with no credentials configured should return 200 HTML."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/admin")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def _admin_first_paint_responses(*, stopped: bool) -> tuple[httpx.Response, httpx.Response]:
    app = _make_test_app(is_addon=True)
    app.state.station_state.session_stopped = stopped
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        direct = await client.get("/admin")
        ingress = await client.get(
            "/",
            headers={"X-Ingress-Path": "/api/hassio_ingress/test-token"},
        )
    return direct, ingress


@pytest.mark.asyncio
async def test_admin_first_paint_seeds_stopped_state_for_direct_and_ingress_routes():
    """A stopped admin page must not flash enabled producer controls before polling."""
    direct, ingress = await _admin_first_paint_responses(stopped=True)

    for response in (direct, ingress):
        assert response.status_code == 200
        assert re.search(r'</head>\s*<body data-stopped="true">', response.text)


@pytest.mark.asyncio
async def test_admin_first_paint_seeds_running_state_for_direct_and_ingress_routes():
    """A running admin page explicitly paints enabled producer controls."""
    direct, ingress = await _admin_first_paint_responses(stopped=False)

    for response in (direct, ingress):
        assert response.status_code == 200
        assert re.search(r'</head>\s*<body data-stopped="false">', response.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [True, False])
async def test_admin_first_paint_injects_state_when_body_has_attributes(monkeypatch, stopped):
    """The stopped marker must survive harmless body-tag layout changes."""
    import mammamiradio.web.streamer as streamer
    from mammamiradio.web import pages

    altered_html = streamer._ADMIN_HTML.replace(
        "</head>\n<body>",
        '</head>\n<body class="admin-shell">',
        1,
    )
    assert altered_html != streamer._ADMIN_HTML
    monkeypatch.setattr(streamer, "_ADMIN_HTML", altered_html)
    monkeypatch.setattr(pages, "_injected_html_cache", {})

    app = _make_test_app(is_addon=True)
    app.state.station_state.session_stopped = stopped
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        direct = await client.get("/admin")
        ingress = await client.get(
            "/",
            headers={"X-Ingress-Path": "/api/hassio_ingress/test-token"},
        )

    expected = "true" if stopped else "false"
    for response in (direct, ingress):
        assert response.status_code == 200
        body_tag = re.search(r"</head>\s*(<body\b[^>]*>)", response.text)
        assert body_tag is not None
        assert 'class="admin-shell"' in body_tag.group(1)
        assert f'data-stopped="{expected}"' in body_tag.group(1)


@pytest.mark.asyncio
async def test_admin_panel_public_ip_without_auth_rejected():
    """GET /admin from public IP without credentials should return 401."""
    app = _make_test_app(admin_password="secret")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/admin")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_panel_with_basic_auth_returns_html():
    """GET /admin with valid basic auth should return 200 HTML."""
    app = _make_test_app(admin_password="secret")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/admin", auth=("admin", "secret"))
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


# ---------------------------------------------------------------------------
# HA add-on mode: LAN trust without admin_token configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_lan_access_in_addon_mode_no_creds(monkeypatch):
    """In HA add-on mode with no credentials, a LAN client can reach /admin."""
    monkeypatch.setenv("MAMMAMIRADIO_BIND_HOST", "0.0.0.0")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    app = _make_test_app(is_addon=True, preserve_bind_env=True)
    assert app.state.config.bind_host == "0.0.0.0"
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/admin")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
@pytest.mark.parametrize("client_ip", ["fd00::50", "fe80::50"])
async def test_admin_ipv6_lan_access_in_addon_mode_no_creds(client_ip):
    """In HA add-on mode with no credentials, IPv6 LAN clients can reach /admin."""
    app = _make_test_app(is_addon=True)
    transport = httpx.ASGITransport(app=app, client=(client_ip, 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/admin")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_admin_lan_post_without_csrf_blocked_in_addon_mode():
    """In HA add-on mode, a LAN POST without CSRF token is still blocked."""
    app = _make_test_app(is_addon=True)
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/skip")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_lan_with_user_set_token_requires_token():
    """In HA add-on mode with explicit admin_token, LAN clients must provide the token."""
    app = _make_test_app(is_addon=True, admin_token="tok-abc-123")
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/admin")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_public_ip_rejected_in_addon_mode_no_creds():
    """In HA add-on mode with no credentials, a public IP is still blocked."""
    app = _make_test_app(is_addon=True)
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/admin")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_live_route_removed():
    """GET /live must return 404 — the orphaned mobile operator surface was removed."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/live")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_admin_panel_csp_allows_inline_handlers():
    """GET /admin must return CSP with 'unsafe-inline' so onclick/oninput handlers work.

    admin.html has ~40 inline event handlers. A nonce-only CSP blocks them even when
    the <script> block loads — nonces cover <script> elements, not attribute handlers.
    """
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/admin")

    assert resp.status_code == 200
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "script-src" in csp, f"Admin response must set script-src CSP: {csp!r}"
    assert "'unsafe-inline'" in csp, f"Admin CSP must include 'unsafe-inline' to allow inline event handlers: {csp!r}"


@pytest.mark.asyncio
async def test_admin_panel_data_fetches_use_ingress_base():
    """admin.html must derive `_base` from window.location.pathname and prefix every
    data fetch with it, so HA Ingress-served pages reach the addon's API.

    Regression guard: prior to this fix, admin.html issued bare `fetch('/status')` and
    `fetch('/api/...')` calls. Under HA Ingress those resolved against the HA host root
    (not the addon's ingress prefix), returned non-JSON, were swallowed by the catch
    handler, and the panel hung at "Waiting for signal…". The server-side rewriter
    intentionally does NOT rewrite JS string literals (see _inject_ingress_prefix
    docstring) — adopting the `_base` contract is the admin page's responsibility,
    matching listener.js.
    """
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/admin")

    assert resp.status_code == 200
    body = resp.text

    assert "const _base = (() =>" in body, (
        "admin.html must declare a `_base` constant derived from window.location.pathname "
        "so HA Ingress data fetches resolve to the addon, not the HA host root."
    )

    bare_offenders = re.findall(r"fetch\((['\"`])/(?:api/|status|public-)", body)
    assert not bare_offenders, (
        "admin.html must not issue bare path-absolute fetches like `fetch('/status')` or "
        f"`fetch('/api/...')`; every call must compose against `_base`. Found: {bare_offenders!r}"
    )

    assert "fetch(_base+p," in body or "fetch(_base + p," in body, (
        "The `api(m, p, b)` helper in admin.html must call `fetch(_base+p, ...)` so every "
        "method/state/save call routed through it honors the HA Ingress prefix."
    )
    assert "__MAMMAMIRADIO_SCRIPT_NONCE__" not in resp.text, "Stale nonce placeholder found in rendered HTML."


# ---------------------------------------------------------------------------
# /api/capabilities route tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capabilities_loopback_returns_flags():
    """GET /api/capabilities on loopback returns capability flags and tier."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/capabilities")
    assert resp.status_code == 200
    body = resp.json()
    # Flags are nested under "capabilities"; top-level has tier, trial, etc.
    caps = body.get("capabilities", body)
    assert "llm" in caps
    assert "jamendo" in caps
    assert "charts_reload" in caps
    assert "tier" in body
    assert "trial" in body
    assert "canned_clips_streamed" in body["trial"]


@pytest.mark.asyncio
async def test_capabilities_exposes_jamendo_and_charts_reload_flags():
    app = _make_test_app()
    app.state.config.playlist.jamendo_client_id = "jamendo-client"
    app.state.config.allow_ytdlp = True
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/capabilities")

    assert resp.status_code == 200
    caps = resp.json()["capabilities"]
    assert caps["jamendo"] is True
    assert caps["charts_reload"] is True


@pytest.mark.asyncio
async def test_capabilities_public_ip_without_auth_rejected():
    """GET /api/capabilities from public IP without credentials returns 401."""
    app = _make_test_app(admin_password="secret")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/capabilities")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_capabilities_openai_only_marks_ai_as_available():
    app = _make_test_app()
    app.state.config.openai_api_key = "openai-key"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/capabilities")
    assert resp.status_code == 200
    assert resp.json()["capabilities"]["llm"] is True
    assert resp.json()["next_step"]["key"] != "add_ai_key"


@pytest.mark.asyncio
async def test_setup_status_and_capabilities_share_guided_setup_projection():
    app = _make_test_app()
    app.state.config.openai_api_key = "openai-key"
    _record_provider_verdict(app.state.station_state, _probe_payload(openai_chat="ok"))
    app.state.config.homeassistant.enabled = True
    app.state.config.ha_token = "ha-token"
    app.state.station_state.ha_context = "- Coffee machine: on"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        capabilities_resp = await client.get("/api/capabilities")
        setup_resp = await client.get("/api/setup/status")

    assert capabilities_resp.status_code == 200
    assert setup_resp.status_code == 200
    assert capabilities_resp.json()["guided_setup"] == setup_resp.json()["guided_setup"]
    assert capabilities_resp.json()["guided_setup"]["ai_hosts"]["status"] == "ready"
    assert capabilities_resp.json()["guided_setup"]["home_context"]["status"] == "ready"
    assert capabilities_resp.json()["guided_setup"]["strip"]["items"][2]["id"] == "home_context"


@pytest.mark.asyncio
async def test_capabilities_trial_exhausted_flag():
    """trial.exhausted is True when canned_clips_streamed >= limit."""
    from mammamiradio.scheduling.producer import SHAREWARE_CANNED_LIMIT

    app = _make_test_app()
    app.state.station_state.canned_clips_streamed = SHAREWARE_CANNED_LIMIT
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/capabilities")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trial"]["exhausted"] is True
    assert body["trial"]["canned_clips_streamed"] == SHAREWARE_CANNED_LIMIT


@pytest.mark.asyncio
async def test_capabilities_exposes_anthropic_degraded_health():
    app = _make_test_app()
    app.state.config.anthropic_api_key = "bad-key"
    app.state.config.openai_api_key = "openai-key"
    app.state.station_state.anthropic_disabled_until = time.time() + 90
    app.state.station_state.anthropic_last_error = "AuthenticationError: invalid x-api-key"
    app.state.station_state.anthropic_auth_failures = 2

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/capabilities")

    assert resp.status_code == 200
    body = resp.json()
    assert body["capabilities"]["anthropic_degraded"] is True
    assert body["provider_health"]["anthropic"]["degraded"] is True
    assert body["provider_health"]["anthropic"]["retry_after_s"] > 0
    assert body["provider_health"]["anthropic"]["auth_failures"] == 2


@pytest.mark.asyncio
async def test_homeassistant_labels_regenerate_schedules_once(tmp_path):
    app = _make_test_app()
    app.state.station_state.home_authorization = HomeAuthorization.legacy()
    app.state.config.cache_dir = tmp_path
    app.state.config.anthropic_api_key = "sk-ant-test"
    cached_context = SimpleNamespace(
        raw_states={"light.counter": {"state": "on", "attributes": {"friendly_name": "Counter light"}}},
        scored=[SimpleNamespace(entity_id="light.counter", score=0.6)],
    )

    with (
        patch("mammamiradio.web.streamer.get_cached_home_context", return_value=cached_context),
        patch("mammamiradio.web.streamer.generation_in_progress", return_value=False),
        patch("mammamiradio.web.streamer.schedule_label_generation", return_value=True) as schedule,
    ):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 9999))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/homeassistant/labels/regenerate")

    assert resp.status_code == 200
    assert resp.json() == {"scheduled": True}
    schedule.assert_called_once()
    assert schedule.call_args.kwargs["force"] is True
    assert schedule.call_args.kwargs["cache_dir"] == tmp_path


@pytest.mark.asyncio
async def test_homeassistant_labels_regenerate_excludes_entity_muted_since_last_poll(tmp_path):
    """The module-level HA cache is only refreshed on fetch_home_context()'s own
    poll cycle — this route reads it directly, so a mute applied after the last
    poll but before this manual trigger must still be honored (adversarial
    review: get_cached_home_context() previously returned the raw stale cache)."""
    from mammamiradio.home.entity_policy import set_entity_muted
    from mammamiradio.home.ha_context import HomeContext, ScoredEntity

    muted_id = "switch.bar_kaffeemaschine_steckdose"
    set_entity_muted(tmp_path, muted_id, True, label="Coffee machine")

    stale_cache = HomeContext(
        raw_states={
            muted_id: {"state": "on", "attributes": {"friendly_name": "Coffee"}},
            "light.counter": {"state": "on", "attributes": {"friendly_name": "Counter light"}},
        },
        scored=[
            ScoredEntity(
                entity_id="light.counter",
                area="Kitchen",
                domain="light",
                score=0.6,
                raw_state={"state": "on", "attributes": {}},
                label_it="Luce",
                label_en="Counter light",
                summary_line="Counter light: on",
            )
        ],
        authorization_mode=HomeAuthorizationMode.LEGACY.value,
    )

    app = _make_test_app()
    app.state.station_state.home_authorization = HomeAuthorization.legacy()
    app.state.config.cache_dir = tmp_path
    app.state.config.anthropic_api_key = "sk-ant-test"

    with (
        patch("mammamiradio.home.ha_context._ha_cache", stale_cache),
        patch("mammamiradio.web.streamer.generation_in_progress", return_value=False),
        patch("mammamiradio.web.streamer.schedule_label_generation", return_value=True) as schedule,
    ):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 9999))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/homeassistant/labels/regenerate")

    assert resp.status_code == 200
    schedule.assert_called_once()
    assert muted_id not in schedule.call_args.args[0]


@pytest.mark.asyncio
async def test_homeassistant_labels_regenerate_returns_409_when_running():
    app = _make_test_app()
    app.state.config.anthropic_api_key = "sk-ant-test"

    with (
        patch("mammamiradio.web.streamer.generation_in_progress", return_value=True),
        patch("mammamiradio.web.streamer.schedule_label_generation") as schedule,
    ):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 9999))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/homeassistant/labels/regenerate")

    assert resp.status_code == 409
    schedule.assert_not_called()


@pytest.mark.asyncio
async def test_homeassistant_labels_regenerate_no_key_returns_unscheduled():
    app = _make_test_app()
    app.state.config.anthropic_api_key = ""

    with (
        patch("mammamiradio.web.streamer.generation_in_progress", return_value=False),
        patch("mammamiradio.web.streamer.schedule_label_generation") as schedule,
    ):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 9999))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/homeassistant/labels/regenerate")

    assert resp.status_code == 200
    assert resp.json() == {"scheduled": False, "reason": "anthropic_key_missing"}
    schedule.assert_not_called()


@pytest.mark.asyncio
async def test_homeassistant_labels_regenerate_has_no_candidates_in_narrow_mode():
    app = _make_test_app()
    app.state.config.anthropic_api_key = "sk-ant-test"
    app.state.station_state.home_authorization = HomeAuthorization.narrow()

    with (
        patch("mammamiradio.web.streamer.generation_in_progress", return_value=False),
        patch("mammamiradio.web.streamer.get_cached_home_context") as cached_context,
        patch("mammamiradio.web.streamer.schedule_label_generation") as schedule,
    ):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 9999))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/homeassistant/labels/regenerate")

    assert resp.status_code == 200
    assert resp.json() == {"scheduled": False, "reason": "no_candidates"}
    cached_context.assert_not_called()
    schedule.assert_not_called()


@pytest.mark.asyncio
async def test_homeassistant_labels_regenerate_no_home_context_returns_unscheduled():
    app = _make_test_app()
    app.state.station_state.home_authorization = HomeAuthorization.legacy()
    app.state.config.anthropic_api_key = "sk-ant-test"

    with (
        patch("mammamiradio.web.streamer.generation_in_progress", return_value=False),
        patch("mammamiradio.web.streamer.get_cached_home_context", return_value=None),
        patch("mammamiradio.web.streamer.schedule_label_generation") as schedule,
    ):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 9999))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/homeassistant/labels/regenerate")

    assert resp.status_code == 200
    assert resp.json() == {"scheduled": False, "reason": "home_context_unavailable"}
    schedule.assert_not_called()


@pytest.mark.asyncio
async def test_homeassistant_labels_regenerate_no_candidates_is_not_a_conflict():
    # schedule_label_generation returns False with nothing to label; the route
    # must report a successful no-op, not a bogus 409 "already in progress".
    app = _make_test_app()
    app.state.station_state.home_authorization = HomeAuthorization.legacy()
    app.state.config.anthropic_api_key = "sk-ant-test"
    cached_context = SimpleNamespace(
        raw_states={"light.counter": {"state": "on", "attributes": {"friendly_name": "Counter light"}}},
        scored=[SimpleNamespace(entity_id="light.counter", score=0.6)],
    )

    with (
        patch("mammamiradio.web.streamer.get_cached_home_context", return_value=cached_context),
        patch("mammamiradio.web.streamer.generation_in_progress", return_value=False),
        patch("mammamiradio.web.streamer.schedule_label_generation", return_value=False),
    ):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 9999))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/homeassistant/labels/regenerate")

    assert resp.status_code == 200
    assert resp.json() == {"scheduled": False, "reason": "no_candidates"}


@pytest.mark.asyncio
async def test_homeassistant_context_candidates_returns_sanitized_admin_preview(tmp_path):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.station_state.ha_context_last_updated = time.time()
    app.state.station_state.ha_scored_entities = [
        {
            "entity_id": "switch.coffee_machine",
            "label": "Coffee machine",
            "area": "Kitchen",
            "domain": "switch",
            "state": "on",
            "summary": "Coffee machine: on",
            "score": 99,
        }
    ]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/homeassistant/context-candidates")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["entities"]
    assert body["entities"][0]["row_state"] == "used_by_hosts"
    assert body["entities"][0]["entity_id"] == "switch.coffee_machine"
    assert "sent_now" in body
    assert "candidates" in body
    assert "muted" in body
    row = body["sent_now"][0]
    assert row["entity_id"] == "switch.coffee_machine"
    assert row["label"] == "Coffee machine"
    assert row["state_summary"] == "Coffee machine: on"
    assert "score" not in row
    assert "attributes" not in row


def test_copy_home_context_to_state_projects_cached_context():
    from collections import deque

    from mammamiradio.home.ha_context import HomeContext, HomeEvent, ScoredEntity

    state = StationState()
    scored = ScoredEntity(
        entity_id="light.hallway",
        area="Hallway",
        domain="light",
        score=0.8,
        raw_state={"state": "on", "attributes": {"friendly_name": "Hallway light"}},
        label_it="Hallway light",
        label_en="Hallway light",
        summary_line="Hallway light: on",
    )
    context = HomeContext(
        raw_states={"light.hallway": scored.raw_state},
        summary="- Hallway light: on",
        events=deque(
            [
                HomeEvent(
                    entity_id="light.hallway",
                    label="Hallway light",
                    old_state="off",
                    new_state="on",
                    timestamp=321.0,
                )
            ],
            maxlen=20,
        ),
        events_summary="- Hallway light: off -> on",
        timestamp=123.0,
        mood="awake",
        weather_arc="clear",
        mood_en="Awake",
        weather_arc_en="Clear",
        events_summary_en="- Hallway light turned on",
        last_event_label_en="Hallway light",
        scored=[scored],
        catalog_hit_rate=1.0,
        label_stats={"catalog_hit_rate": 1.0, "total": 1},
        registry_source="cache",
        denylist_hits={"user_muted": 1},
    )

    _copy_home_context_to_state(state, context)

    assert state.ha_context == "- Hallway light: on"
    assert state.ha_events_summary == "- Hallway light: off -> on"
    assert state.ha_recent_event_count == 1
    assert state.ha_last_event_label == "Hallway light"
    assert state.ha_last_event_ts == 321.0
    assert state.ha_scored_entities[0]["entity_id"] == "light.hallway"
    assert state.ha_denylist_hits == {"user_muted": 1}
    assert state.ha_catalog_hit_rate == 1.0
    assert state.ha_label_stats == {"catalog_hit_rate": 1.0, "total": 1}
    assert state.ha_registry_source == "cache"
    assert state.ha_context_last_updated == 123.0
    assert state.ha_context_entity_count == 1
    assert state.ha_context_char_count == len("- Hallway light: on")


@pytest.mark.asyncio
async def test_homeassistant_entity_policy_partial_mute_preserves_remaining_home_context(tmp_path):
    from collections import deque

    from mammamiradio.home.ha_context import HomeContext, HomeEvent, ScoredEntity

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.anthropic_api_key = "sk-ant"
    app.state.config.homeassistant.enabled = True
    app.state.config.ha_token = "ha-token"
    state = app.state.station_state
    state.ha_context = "- Coffee machine: on"
    state.ha_events_summary = "- Coffee machine: off -> on"
    state.ha_pending_directive = "Mention coffee"
    state.ha_running_gag = "Coffee again"
    state.ha_last_event_label = "Coffee machine"
    state.ha_last_event_ts = time.time()
    state.ha_context_last_updated = time.time()
    state.ha_context_entity_count = 2
    state.ha_context_char_count = 42
    state.ha_scored_entities = [
        {
            "entity_id": "switch.coffee_machine",
            "label": "Coffee machine",
            "area": "Kitchen",
            "domain": "switch",
            "state": "on",
            "summary": "Coffee machine: on",
        }
    ]
    coffee = ScoredEntity(
        entity_id="switch.coffee_machine",
        area="Kitchen",
        domain="switch",
        score=0.9,
        raw_state={"state": "on", "attributes": {"friendly_name": "Coffee machine"}},
        label_it="Coffee machine",
        label_en="Coffee machine",
        summary_line="Coffee machine: on",
    )
    hallway = ScoredEntity(
        entity_id="light.hallway",
        area="Hallway",
        domain="light",
        score=0.8,
        raw_state={"state": "on", "attributes": {"friendly_name": "Hallway light"}},
        label_it="Hallway light",
        label_en="Hallway light",
        summary_line="Hallway light: on",
    )
    cached_context = HomeContext(
        raw_states={
            "switch.coffee_machine": coffee.raw_state,
            "light.hallway": hallway.raw_state,
        },
        summary="- Coffee machine: on\n- Hallway light: on",
        events=deque(
            [
                HomeEvent(
                    entity_id="switch.coffee_machine",
                    label="Coffee machine",
                    old_state="off",
                    new_state="on",
                    timestamp=time.time(),
                ),
                HomeEvent(
                    entity_id="light.hallway",
                    label="Hallway light",
                    old_state="off",
                    new_state="on",
                    timestamp=time.time(),
                ),
            ],
            maxlen=20,
        ),
        timestamp=time.time(),
        scored=[coffee, hallway],
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.home.ha_context._ha_cache", cached_context):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.patch(
                "/api/homeassistant/entity-policy",
                json={"entity_id": "switch.coffee_machine", "muted": True},
            )
            preview = await client.get("/api/homeassistant/context-candidates")
            setup_status = await client.get("/api/setup/status")
            capabilities = await client.get("/api/capabilities")

    assert resp.status_code == 200
    assert resp.json()["muted"] is True
    policy = tmp_path / "state" / "ha_entity_policy.json"
    assert "switch.coffee_machine" in policy.read_text()
    assert "Hallway light" in state.ha_context
    assert "Coffee machine" not in state.ha_context
    assert state.ha_pending_directive == ""
    assert state.ha_running_gag == ""
    assert [row["entity_id"] for row in state.ha_scored_entities] == ["light.hallway"]
    assert state.ha_context_last_updated > 0
    assert state.ha_context_entity_count == 1
    assert preview.json()["status"] == "ready"
    muted_rows = preview.json()["muted"]
    assert muted_rows[0]["entity_id"] == "switch.coffee_machine"
    assert muted_rows[0]["sent_to_prompt"] is False
    entity_rows = {row["entity_id"]: row for row in preview.json()["entities"]}
    assert entity_rows["switch.coffee_machine"]["row_state"] == "muted"
    assert entity_rows["switch.coffee_machine"]["muted"] is True
    assert entity_rows["light.hallway"]["row_state"] == "used_by_hosts"
    setup_home_context = setup_status.json()["guided_setup"]["home_context"]
    assert setup_home_context["status"] == "ready"
    assert setup_home_context["readiness"] == "prompt_ready"
    assert setup_home_context["action"] == "review_home_context"
    assert capabilities.json()["tier"] == "connected_home"


@pytest.mark.asyncio
async def test_homeassistant_entity_policy_mute_discards_baselines_before_later_unmute(tmp_path):
    """A transition while muted must not become a radio event after unmuting."""
    import mammamiradio.home.ha_context as ha_context
    from mammamiradio.core.config import RadioEventRule
    from mammamiradio.home.ha_context import HomeContext
    from mammamiradio.home.radio_events import match_radio_events

    entity_id = "switch.coffee_machine"
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.station_state.home_authorization = HomeAuthorization.legacy()
    app.state.station_state.ha_context_refresh_mailbox = MagicMock()
    prior = HomeContext(
        raw_states={entity_id: {"state": "off", "attributes": {}}},
        timestamp=time.time(),
        authorization_mode=HomeAuthorizationMode.LEGACY.value,
    )
    rule = RadioEventRule(id="coffee_started", entity_id=entity_id, to_state="on")

    with (
        patch.object(ha_context, "_ha_cache", prior),
        patch.object(ha_context, "_radio_event_state_cache", {entity_id: {"state": "off", "attributes": {}}}),
        patch.object(ha_context, "_ritual_recipe_state_cache", {entity_id: {"state": "off", "attributes": {}}}),
    ):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            muted = await client.patch(
                "/api/homeassistant/entity-policy",
                json={"entity_id": entity_id, "muted": True},
            )
            # The physical state flips while the hard mute is active.
            unmuted = await client.patch(
                "/api/homeassistant/entity-policy",
                json={"entity_id": entity_id, "muted": False},
            )

        assert muted.status_code == 200
        assert unmuted.status_code == 200
        assert entity_id not in ha_context._radio_event_state_cache
        assert entity_id not in ha_context._ritual_recipe_state_cache
        assert entity_id not in ha_context._ha_cache.raw_states
        app.state.station_state.ha_context_refresh_mailbox.invalidate_muted_entities.assert_called_once_with(
            {entity_id}
        )
        historical_matches = match_radio_events(
            [rule],
            ha_context._radio_event_state_cache,
            {entity_id: {"state": "on", "attributes": {}}},
            cooldowns={},
        )

    assert historical_matches == []


@pytest.mark.asyncio
async def test_homeassistant_entity_policy_mute_does_not_purge_already_rendered_queue(tmp_path):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    queued_segment = Segment(type=SegmentType.BANTER, path=Path("/tmp/already-rendered.mp3"), metadata={})
    app.state.queue.put_nowait(queued_segment)
    app.state.station_state.queued_segments = [{"type": "banter", "label": "Already rendered"}]

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.patch(
            "/api/homeassistant/entity-policy",
            json={"entity_id": "switch.coffee_machine", "muted": True},
        )

    assert resp.status_code == 200
    assert app.state.queue.qsize() == 1
    assert app.state.station_state.queued_segments == [{"type": "banter", "label": "Already rendered"}]


@pytest.mark.asyncio
async def test_homeassistant_entity_policy_personal_moment_opt_out_purges_queued_presence_banter(tmp_path):
    """Revoking a presence opt-in must pull an unstarted queued break for that
    entity, the same privacy contract as a mute — the airing segment is untouched."""
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    state = app.state.station_state
    state.ha_scored_entities = [
        {
            "entity_id": "binary_sensor.living_presence",
            "label": "Living presence",
            "area": "Living room",
            "domain": "binary_sensor",
            "device_class": "occupancy",
            "state": "on",
            "summary": "presence",
        }
    ]
    # A queued (not yet airing) presence break tied to that entity.
    queued = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/presence-break.mp3"),
        metadata={"queue_id": "q-presence-1", "home_fact_entity_id": "binary_sensor.living_presence"},
    )
    app.state.queue.put_nowait(queued)
    state.queued_segments = [{"type": "banter", "label": "Presence break", "id": "q-presence-1"}]

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        opt_in = await client.patch(
            "/api/homeassistant/entity-policy",
            json={"entity_id": "binary_sensor.living_presence", "personal_moment_enabled": True},
        )
        opt_out = await client.patch(
            "/api/homeassistant/entity-policy",
            json={"entity_id": "binary_sensor.living_presence", "personal_moment_enabled": False},
        )

    assert opt_in.status_code == 200
    assert opt_in.json()["personal_moment_enabled"] is True
    assert opt_out.status_code == 200
    assert opt_out.json()["personal_moment_enabled"] is False
    assert opt_out.json()["purged_pending_banter_count"] == 1
    # The queued presence break was pulled, and its shadow row with it.
    assert app.state.queue.qsize() == 0
    assert state.queued_segments == []


@pytest.mark.asyncio
async def test_homeassistant_entity_policy_mute_purges_running_gag_ledger(tmp_path):
    """A gag observed before a mute must not survive it — entity_denylist only
    stops NEW events from becoming buckets; it does nothing about a bucket
    already tallied before the operator muted the entity."""
    from mammamiradio.home.evening_memory import EveningLedger, GagBucket

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    state = app.state.station_state
    state.ha_scored_entities = [
        {
            "entity_id": "switch.coffee_machine",
            "label": "Coffee machine",
            "area": "Kitchen",
            "domain": "switch",
            "state": "on",
            "summary": "Coffee machine: on",
        }
    ]
    ledger = EveningLedger()
    ledger.buckets["k"] = GagBucket(
        "switch.coffee_machine", "Coffee machine", "off", "on", count=3, last_ts=time.time()
    )
    ledger.session_id = 1
    state.evening_ledger = ledger

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.patch(
            "/api/homeassistant/entity-policy",
            json={"entity_id": "switch.coffee_machine", "muted": True},
        )

    assert resp.status_code == 200
    assert ledger.buckets == {}
    ledger_file = tmp_path / "evening_ledger.json"
    assert ledger_file.exists()
    assert "switch.coffee_machine" not in ledger_file.read_text()


@pytest.mark.asyncio
async def test_homeassistant_entity_policy_unmute_is_idempotent_for_existing_muted_entity(tmp_path):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    from mammamiradio.home.entity_policy import set_entity_muted

    set_entity_muted(tmp_path, "switch.coffee_machine", True, label="Coffee machine", domain="switch", area="Kitchen")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.patch(
            "/api/homeassistant/entity-policy",
            json={"entity_id": "switch.coffee_machine", "muted": False},
        )
        second = await client.patch(
            "/api/homeassistant/entity-policy",
            json={"entity_id": "switch.coffee_machine", "muted": False},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["muted"] is False


@pytest.mark.asyncio
async def test_homeassistant_entity_policy_unmute_removes_live_muted_ledger_deny(tmp_path):
    from mammamiradio.home.entity_policy import set_entity_muted
    from mammamiradio.home.evening_memory import EveningLedger
    from mammamiradio.home.ha_context import HomeEvent

    entity_id = "switch.coffee_machine"
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    set_entity_muted(tmp_path, entity_id, True, label="Coffee machine", domain="switch", area="Kitchen")
    ledger = EveningLedger(entity_denylist=frozenset({entity_id}))
    app.state.station_state.evening_ledger = ledger

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.patch(
            "/api/homeassistant/entity-policy",
            json={"entity_id": entity_id, "muted": False},
        )

    assert resp.status_code == 200
    assert entity_id not in ledger.entity_denylist
    changed = ledger.observe(
        [
            HomeEvent(
                entity_id=entity_id,
                label="Coffee machine",
                old_state="off",
                new_state="on",
                timestamp=time.time(),
            )
        ],
        now=time.time(),
    )
    assert changed is True
    assert any(bucket.entity_id == entity_id for bucket in ledger.buckets.values())


@pytest.mark.asyncio
async def test_homeassistant_entity_policy_unmute_preserves_config_ledger_deny(tmp_path):
    from mammamiradio.home.entity_policy import set_entity_muted
    from mammamiradio.home.evening_memory import EveningLedger

    entity_id = "switch.noisy"
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.running_gags.entity_denylist = [entity_id]
    set_entity_muted(tmp_path, entity_id, True, label="Noisy switch", domain="switch", area="Kitchen")
    ledger = EveningLedger(entity_denylist=frozenset({entity_id}))
    app.state.station_state.evening_ledger = ledger

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.patch(
            "/api/homeassistant/entity-policy",
            json={"entity_id": entity_id, "muted": False},
        )

    assert resp.status_code == 200
    assert entity_id in ledger.entity_denylist


@pytest.mark.asyncio
async def test_homeassistant_context_candidates_public_ip_without_auth_rejected():
    app = _make_test_app(admin_password="secret")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/homeassistant/context-candidates")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_homeassistant_entity_policy_token_auth_public_ip_allows_write(tmp_path):
    app = _make_test_app(admin_token="tok")
    app.state.config.cache_dir = tmp_path
    app.state.station_state.ha_scored_entities = [
        {
            "entity_id": "switch.coffee_machine",
            "label": "Coffee machine",
            "area": "Kitchen",
            "domain": "switch",
            "state": "on",
            "summary": "Coffee machine: on",
        }
    ]
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.patch(
            "/api/homeassistant/entity-policy",
            headers={"X-Radio-Admin-Token": "tok"},
            json={"entity_id": "switch.coffee_machine", "muted": True},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_homeassistant_entity_policy_rejects_malformed_entity_id():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.patch(
            "/api/homeassistant/entity-policy",
            json={"entity_id": "not-a-valid-entity-id", "muted": True},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_homeassistant_entity_policy_rejects_non_boolean_muted():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.patch(
            "/api/homeassistant/entity-policy",
            json={"entity_id": "switch.coffee_machine", "muted": "yes"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_homeassistant_entity_policy_can_mute_entity_absent_from_preview(tmp_path):
    """Radio_event-only entities are deliberately kept out of the ambient
    preview, but an operator must still be able to mute them by id — muting
    something that was never going to be sent is inert, not unsafe."""
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    # No ha_scored_entities and no cached context — the entity is not in the
    # safe preview, but the mute must still persist.
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.patch(
            "/api/homeassistant/entity-policy",
            json={"entity_id": "switch.never_seen", "muted": True},
        )
    assert resp.status_code == 200
    assert resp.json()["muted"] is True
    policy = tmp_path / "state" / "ha_entity_policy.json"
    assert "switch.never_seen" in policy.read_text()


@pytest.mark.asyncio
async def test_homeassistant_entity_policy_write_failure_returns_500(tmp_path):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.station_state.ha_scored_entities = [
        {
            "entity_id": "switch.coffee_machine",
            "label": "Coffee machine",
            "area": "Kitchen",
            "domain": "switch",
            "state": "on",
            "summary": "Coffee machine: on",
        }
    ]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer.set_entity_muted", side_effect=OSError("disk full")):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.patch(
                "/api/homeassistant/entity-policy",
                json={"entity_id": "switch.coffee_machine", "muted": True},
            )
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Stopped sessions stay stopped until explicit resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audio_generator_does_not_auto_resume_stopped_session(tmp_path):
    """_audio_generator must preserve session_stopped when a listener connects."""
    from mammamiradio.web.streamer import _audio_generator

    app = _make_test_app()
    state = app.state.station_state
    state.session_stopped = True
    flag = tmp_path / "session_stopped.flag"
    flag.touch()
    app.state.config.cache_dir = tmp_path

    mock_request = MagicMock()
    mock_request.app = app
    mock_request.is_disconnected = AsyncMock(return_value=True)

    async for _ in _audio_generator(mock_request):
        pass

    assert state.session_stopped is True
    assert flag.exists()


@pytest.mark.asyncio
async def test_audio_generator_leaves_flag_until_explicit_resume(tmp_path):
    """A stream connection must not remove session_stopped.flag."""
    from mammamiradio.web.streamer import _audio_generator

    app = _make_test_app()
    app.state.station_state.session_stopped = True
    flag = tmp_path / "session_stopped.flag"
    flag.touch()
    app.state.config.cache_dir = tmp_path

    mock_request = MagicMock()
    mock_request.app = app
    mock_request.is_disconnected = AsyncMock(return_value=True)

    async for _ in _audio_generator(mock_request):
        pass

    assert app.state.station_state.session_stopped is True
    assert flag.exists()


@pytest.mark.asyncio
async def test_audio_generator_active_session_is_unaffected(tmp_path):
    """When the session is not stopped, _audio_generator subscribes normally.

    Regression guard: the auto-resume removal must not break the normal
    (session_stopped=False) path — the generator should subscribe without error.
    """
    from mammamiradio.web.streamer import _audio_generator

    app = _make_test_app()
    state = app.state.station_state
    state.session_stopped = False

    mock_request = MagicMock()
    mock_request.app = app
    mock_request.is_disconnected = AsyncMock(return_value=True)

    # Generator should run and exit cleanly (listener immediately disconnects)
    async for _ in _audio_generator(mock_request):
        pass

    assert state.session_stopped is False


# ---------------------------------------------------------------------------
# POST /api/hot-reload tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hot_reload_authenticated_200():
    """POST /api/hot-reload with valid admin token returns 200 with expected fields."""
    app = _make_test_app(admin_token="testtoken")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/hot-reload",
            headers={"X-Radio-Admin-Token": "testtoken"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["reloaded_modules"] == [
        "mammamiradio.hosts.language_policy",
        "mammamiradio.hosts.prompt_world",
        "mammamiradio.hosts.transitions",
        "mammamiradio.hosts.fallbacks",
        "mammamiradio.hosts.station_name_guard",
        "mammamiradio.hosts.scriptwriter",
    ]
    assert body["stream_status"] == "unaffected"
    assert body["effective_on"] == "next_banter_generation"
    assert isinstance(body["duration_ms"], int)


@pytest.mark.asyncio
async def test_hot_reload_unauthenticated_rejected():
    """POST /api/hot-reload without auth credentials is rejected."""
    app = _make_test_app(admin_password="secret", admin_token="tok")
    transport = httpx.ASGITransport(app=app, client=("10.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/hot-reload")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_hot_reload_language_policy_stage_failure_returns_500():
    """First reload stage (language_policy) raises → 500 with stream_status=unaffected.

    Guards the failure contract for the leaves-first stage. With language_policy reloaded
    first, a single raising reload exercises this stage.
    """
    app = _make_test_app(admin_token="testtoken")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch(
        "mammamiradio.web.streamer.importlib.reload",
        side_effect=ImportError("syntax error in language_policy.py"),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/api/hot-reload",
                headers={"X-Radio-Admin-Token": "testtoken"},
            )
    assert resp.status_code == 500
    body = resp.json()
    assert body["ok"] is False
    assert body["stream_status"] == "unaffected"
    assert body["error_code"] == "reload_failed"
    assert body["retryable"] is True
    assert "syntax error in language_policy.py" in body["exception"]


@pytest.mark.asyncio
async def test_hot_reload_scriptwriter_stage_failure_returns_500():
    """Last reload stage (the scriptwriter facade) fails after the leaves succeed → 500.

    The data leaves reload cleanly, then the scriptwriter facade raises at the
    final stage.
    Without the sequenced side-effect this stage would go uncovered, since an earlier
    reload would short-circuit the failure.
    """
    app = _make_test_app(admin_token="testtoken")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch(
        "mammamiradio.web.streamer.importlib.reload",
        side_effect=[None, None, None, None, None, ImportError("syntax error in scriptwriter.py")],
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/api/hot-reload",
                headers={"X-Radio-Admin-Token": "testtoken"},
            )
    assert resp.status_code == 500
    body = resp.json()
    assert body["ok"] is False
    assert body["stream_status"] == "unaffected"
    assert body["error_code"] == "reload_failed"
    assert body["retryable"] is True
    assert "syntax error in scriptwriter.py" in body["exception"]


@pytest.mark.asyncio
async def test_hot_reload_debounce_returns_429_on_rapid_calls():
    """A second hot-reload call within 5s returns 429 with retry_after_s."""
    app = _make_test_app(admin_token="testtoken")
    # Prime the debounce timestamp to now
    app.state._last_hot_reload_ts = time.monotonic()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/hot-reload",
            headers={"X-Radio-Admin-Token": "testtoken"},
        )
    assert resp.status_code == 429
    body = resp.json()
    assert body["ok"] is False
    assert body["error_code"] == "debounced"
    assert body["stream_status"] == "unaffected"
    assert body["retryable"] is True
    assert body["retry_after_s"] > 0


@pytest.mark.asyncio
async def test_hot_reload_reloads_prompt_world_before_scriptwriter():
    """Data submodules reload before the scriptwriter facade (leaves-first).

    The facade re-imports values via ``from .prompt_world / .transitions / .fallbacks
    import ...``. Reloading the facade alone would rebind those names to the stale
    submodules, so an operator's edit to any data leaf would silently not take effect.
    The reload set must list (and reload) every data submodule ahead of scriptwriter.
    """
    app = _make_test_app(admin_token="testtoken")
    reloaded: list[str] = []
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    # Record the ACTUAL importlib.reload call sequence. Asserting only the response
    # `reloaded_modules` list is too weak — it's a fixed literal and would pass even if
    # the implementation issued the reloads in the wrong order.
    with patch(
        "mammamiradio.web.streamer.importlib.reload",
        side_effect=lambda mod: reloaded.append(mod.__name__),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/api/hot-reload",
                headers={"X-Radio-Admin-Token": "testtoken"},
            )
    assert resp.status_code == 200
    assert reloaded == [
        "mammamiradio.hosts.language_policy",
        "mammamiradio.hosts.prompt_world",
        "mammamiradio.hosts.transitions",
        "mammamiradio.hosts.fallbacks",
        "mammamiradio.hosts.station_name_guard",
        "mammamiradio.hosts.scriptwriter",
    ], "data submodules must reload before scriptwriter (leaves-first)"


# ---------------------------------------------------------------------------
# Provider key-validation verdict (rejected/valid/unverified)
#
# A bogus key persisted at boot must read as "key not working" BEFORE any banter
# segment 401s. These cover the mapping helper, the non-blocking runner, and the
# startup/save/on-demand wiring that persists the verdict onto StationState.
# ---------------------------------------------------------------------------


def _probe_entry(provider: str, outcome: str | None) -> dict:
    """Build one check_provider_keys provider entry. outcome: 'ok'|'auth'|'quota'|None."""
    if outcome is None:
        return {
            "provider": provider,
            "configured": False,
            "ok": False,
            "status_code": None,
            "error_type": "not_configured",
            "detail": "",
        }
    if outcome == "ok":
        return {
            "provider": provider,
            "configured": True,
            "ok": True,
            "status_code": 200,
            "error_type": "",
            "detail": "",
        }
    mapping = {"auth": (401, "authentication_error"), "quota": (403, "insufficient_quota"), "rate": (429, "rate_limit")}
    status_code, error_type = mapping[outcome]
    return {
        "provider": provider,
        "configured": True,
        "ok": False,
        "status_code": status_code,
        "error_type": error_type,
        "detail": "",
    }


def _probe_payload(*, anthropic: str | None = None, openai_chat: str | None = None) -> dict:
    providers = {
        "anthropic": _probe_entry("anthropic", anthropic),
        "openai_chat": _probe_entry("openai_chat", openai_chat),
        "openai_tts": _probe_entry("openai_tts", openai_chat),
    }
    return {"ok": any(p["ok"] for p in providers.values()), "providers": providers}


def test_record_provider_verdict_maps_auth_to_rejected():
    state = StationState()
    _record_provider_verdict(state, _probe_payload(anthropic="auth"))
    assert state.anthropic_key_status == "rejected"
    assert state.anthropic_key_checked_at > 0


def test_record_provider_verdict_maps_ok_to_valid():
    state = StationState()
    _record_provider_verdict(state, _probe_payload(anthropic="ok"))
    assert state.anthropic_key_status == "valid"


def test_record_provider_verdict_leaves_inconclusive_unchanged():
    """Quota / rate-limit / network are NOT auth rejections — status must not flip to rejected."""
    state = StationState()
    state.anthropic_key_status = "valid"
    _record_provider_verdict(state, _probe_payload(anthropic="quota"))
    assert state.anthropic_key_status == "valid", "quota error must not be mislabeled rejected"
    _record_provider_verdict(state, _probe_payload(anthropic="rate"))
    assert state.anthropic_key_status == "valid"


def test_record_provider_verdict_openai_parity():
    state = StationState()
    _record_provider_verdict(state, _probe_payload(openai_chat="auth"))
    assert state.openai_key_status == "rejected"
    _record_provider_verdict(state, _probe_payload(openai_chat="ok"))
    assert state.openai_key_status == "valid"


@pytest.mark.asyncio
async def test_run_provider_verdict_no_keys_skips_probe():
    app = _make_test_app()
    app.state.config.anthropic_api_key = ""
    app.state.config.openai_api_key = ""
    with patch("mammamiradio.web.provider_verdict.check_provider_keys", new=AsyncMock()) as probe:
        await _run_provider_verdict(app.state)
    probe.assert_not_awaited()
    assert app.state.station_state.anthropic_key_status == "unverified"


@pytest.mark.asyncio
async def test_run_provider_verdict_swallows_probe_exception():
    """A flaky network must never crash boot or a key-save — status stays unverified."""
    app = _make_test_app()
    app.state.config.anthropic_api_key = "sk-ant-x"
    with patch(
        "mammamiradio.web.provider_verdict.check_provider_keys", new=AsyncMock(side_effect=RuntimeError("boom"))
    ):
        await _run_provider_verdict(app.state)  # must not raise
    assert app.state.station_state.anthropic_key_status == "unverified"


@pytest.mark.asyncio
async def test_run_provider_verdict_success_writes_state():
    app = _make_test_app()
    app.state.config.anthropic_api_key = "sk-ant-bogus"
    with patch(
        "mammamiradio.web.provider_verdict.check_provider_keys",
        new=AsyncMock(return_value=_probe_payload(anthropic="auth")),
    ):
        await _run_provider_verdict(app.state)
    assert app.state.station_state.anthropic_key_status == "rejected"


@pytest.mark.asyncio
async def test_provider_check_route_persists_rejected_verdict():
    """POST /api/setup/provider-check records the verdict on state, not just the response."""
    app = _make_test_app()
    app.state.config.anthropic_api_key = "anthropic-secret"
    payload = _probe_payload(anthropic="auth")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer.check_provider_keys", new=AsyncMock(return_value=payload)):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/setup/provider-check")
    assert resp.status_code == 200
    assert resp.json() == payload  # response body unchanged (existing contract)
    assert app.state.station_state.anthropic_key_status == "rejected"


@pytest.mark.asyncio
async def test_save_keys_resets_status_and_revalidates():
    app = _make_test_app()
    app.state.station_state.anthropic_key_status = "rejected"  # stale prior verdict
    previous = os.environ.get("ANTHROPIC_API_KEY")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    # Gate the background probe so it can't finish before we observe the synchronous
    # reset — otherwise an immediate AsyncMock makes the "unverified" assertion racy.
    gate = asyncio.Event()

    async def _delayed_probe(_config):
        await gate.wait()
        return _probe_payload(anthropic="ok")

    try:
        with (
            patch("mammamiradio.web.streamer._save_dotenv"),
            patch("mammamiradio.web.provider_verdict.check_provider_keys", new=AsyncMock(side_effect=_delayed_probe)),
        ):
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post("/api/setup/save-keys", json={"ANTHROPIC_API_KEY": "sk-ant-new"})
            assert resp.status_code == 200
            # _apply_live_credentials wiped the stale verdict synchronously; the gated
            # probe is still parked, so this is deterministic.
            assert app.state.station_state.anthropic_key_status == "unverified"
            # Release the background re-probe; it then writes the fresh verdict.
            gate.set()
            await app.state.provider_verdict_task
        assert app.state.station_state.anthropic_key_status == "valid"
    finally:
        if previous is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = previous


@pytest.mark.asyncio
async def test_capabilities_exposes_key_status_and_steers_next_step():
    app = _make_test_app()
    app.state.config.anthropic_api_key = "sk-ant-bogus"
    app.state.config.openai_api_key = ""
    app.state.station_state.anthropic_key_status = "rejected"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/capabilities")).json()
    caps = body["capabilities"]
    assert caps["anthropic_key_status"] == "rejected"
    assert "openai_key_status" in caps
    # A confirmed-rejected sole key steers next_step toward replacing it.
    assert body["next_step"]["key"] == "fix_llm_key"
    # provider_health carries the verdict for both providers.
    assert body["provider_health"]["anthropic"]["key_status"] == "rejected"
    assert "key_status" in body["provider_health"]["openai"]


@pytest.mark.asyncio
async def test_capabilities_valid_key_does_not_steer_next_step():
    app = _make_test_app()
    app.state.config.anthropic_api_key = "sk-ant-good"
    app.state.station_state.anthropic_key_status = "valid"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/capabilities")).json()
    assert body["next_step"]["key"] != "fix_llm_key"


@pytest.mark.asyncio
async def test_capabilities_rejected_anthropic_but_valid_openai_does_not_steer():
    """OpenAI is a working fallback — a rejected Anthropic key must NOT nag to fix it."""
    app = _make_test_app()
    app.state.config.anthropic_api_key = "sk-ant-bad"
    app.state.config.openai_api_key = "sk-openai-good"
    app.state.station_state.anthropic_key_status = "rejected"
    app.state.station_state.openai_key_status = "valid"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/capabilities")).json()
    assert body["next_step"]["key"] != "fix_llm_key"


@pytest.mark.asyncio
async def test_capabilities_rejected_anthropic_with_unverified_openai_does_not_steer_yet():
    """While the second provider's probe is still in flight, hold the fix nudge."""
    app = _make_test_app()
    app.state.config.anthropic_api_key = "sk-ant-bad"
    app.state.config.openai_api_key = "sk-openai-pending"
    app.state.station_state.anthropic_key_status = "rejected"
    app.state.station_state.openai_key_status = "unverified"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/capabilities")).json()
    assert body["next_step"]["key"] != "fix_llm_key"


@pytest.mark.asyncio
async def test_capabilities_openai_rejected_alone_steers_fix_llm_key():
    """OpenAI-only deployment with a rejected key: surface it end-to-end via /api/capabilities."""
    app = _make_test_app()
    app.state.config.anthropic_api_key = ""
    app.state.config.openai_api_key = "sk-openai-bad"
    app.state.station_state.openai_key_status = "rejected"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/capabilities")).json()
    assert body["capabilities"]["openai_key_status"] == "rejected"
    assert body["provider_health"]["openai"]["key_status"] == "rejected"
    assert body["next_step"]["key"] == "fix_llm_key"


@pytest.mark.asyncio
async def test_provider_check_cached_result_does_not_clear_verdict():
    """A debounced (cached) second /provider-check must not wipe the persisted verdict."""
    app = _make_test_app()
    app.state.config.anthropic_api_key = "anthropic-secret"
    payload = _probe_payload(anthropic="auth")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer.check_provider_keys", new=AsyncMock(return_value=payload)):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.post("/api/setup/provider-check")
            assert app.state.station_state.anthropic_key_status == "rejected"
            # Second call inside the 2s debounce window returns the cached result.
            await client.post("/api/setup/provider-check")
    assert app.state.station_state.anthropic_key_status == "rejected"


@pytest.mark.asyncio
async def test_run_provider_verdict_discards_stale_result_when_key_changed():
    """A late-finishing probe must not clobber the verdict after the key was swapped."""
    app = _make_test_app()
    app.state.config.anthropic_api_key = "sk-ant-old-bad"
    app.state.station_state.anthropic_key_status = "valid"  # a fresh save already set this

    async def _slow_probe(config):
        # Simulate save_keys swapping the key while this stale probe is in flight.
        config.anthropic_api_key = "sk-ant-new-good"
        return _probe_payload(anthropic="auth")

    with patch("mammamiradio.web.provider_verdict.check_provider_keys", new=_slow_probe):
        await _run_provider_verdict(app.state)
    # Stale "rejected" for the old key must be discarded; the fresh verdict stands.
    assert app.state.station_state.anthropic_key_status == "valid"


@pytest.mark.asyncio
async def test_provider_check_stale_shared_task_not_recorded_after_key_swap():
    """A shared in-flight probe must not record its verdict against a key saved mid-check.

    Covers the case a per-request snapshot missed: a later waiter joins an OLD task after
    a save swapped the key, so the verdict must travel with the task, not the waiter.
    """
    app = _make_test_app()
    app.state.config.anthropic_api_key = "sk-ant-old"
    app.state.station_state.anthropic_key_status = "valid"  # fresh verdict from a save
    started = asyncio.Event()
    gate = asyncio.Event()

    async def _gated_old_probe(_config):
        started.set()
        await gate.wait()
        return _probe_payload(anthropic="auth")  # old key 401

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer.check_provider_keys", new=AsyncMock(side_effect=_gated_old_probe)):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            req = asyncio.create_task(client.post("/api/setup/provider-check"))
            await started.wait()  # task created with the "sk-ant-old" snapshot
            app.state.config.anthropic_api_key = "sk-ant-new"  # operator saves a new key
            gate.set()
            resp = await req
    assert resp.status_code == 200
    # The stale old-key 401 must NOT clobber the fresh "valid" verdict.
    assert app.state.station_state.anthropic_key_status == "valid"


@pytest.mark.asyncio
async def test_personal_moment_consent_is_presence_only_and_mute_purges_queued_fact(tmp_path):
    from mammamiradio.home.context_director import HomeContextDirector
    from mammamiradio.home.ha_context import HomeContext, ScoredEntity

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    presence = ScoredEntity(
        entity_id="binary_sensor.office_presence",
        area="Office",
        domain="binary_sensor",
        score=0.9,
        raw_state={"state": "on", "attributes": {"device_class": "presence"}},
        label_it="Office presence",
        label_en="Office presence",
        summary_line="Office: active",
    )
    context = HomeContext(scored=[presence], timestamp=time.time())
    state = app.state.station_state
    state.home_context_director = HomeContextDirector()
    state.home_context_director.observe([], policy_revision=0)
    queued = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/context-fact.mp3"),
        metadata={
            "queue_id": "fact-queue",
            "home_fact_entity_id": "binary_sensor.office_presence",
            "home_fact_id": "opaque",
        },
    )
    app.state.queue.put_nowait(queued)
    state.queued_segments = [{"id": "fact-queue", "type": "banter", "label": "Host break"}]

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.home.ha_context._ha_cache", context):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            enabled = await client.patch(
                "/api/homeassistant/entity-policy",
                json={"entity_id": "binary_sensor.office_presence", "personal_moment_enabled": True},
            )
            muted = await client.patch(
                "/api/homeassistant/entity-policy",
                json={"entity_id": "binary_sensor.office_presence", "muted": True},
            )

    assert enabled.status_code == 200
    assert enabled.json()["personal_moment_effective"] is True
    assert muted.status_code == 200
    assert muted.json()["personal_moment_enabled"] is False
    assert muted.json()["purged_pending_banter_count"] == 1
    assert app.state.queue.empty()
    assert state.queued_segments == []


@pytest.mark.asyncio
async def test_mute_releases_inflight_home_fact_reservation_not_in_queue(tmp_path):
    """A fact reserved at admission but not yet physically enqueued (mid-egress
    render) is released when its entity is muted. The physical-queue purge cannot
    see it, so the endpoint must honor invalidate_entity's returned pending ids."""
    from mammamiradio.home.context_director import DirectorObservation, HomeContextDirector

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    director = HomeContextDirector()
    director.observe(
        [
            DirectorObservation(
                entity_id="weather.forecast_home", domain="weather", state="sunny", score=9.0, temperature_c=24.0
            )
        ],
        policy_revision=0,
    )
    fact = director.select()
    assert fact is not None
    # Reserved, but deliberately NOT put in app.state.queue — it is still rendering.
    assert director.reserve("inflight-queue", fact)
    state = app.state.station_state
    state.home_context_director = director
    assert director.admin_status()["reserved_count"] == 1

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        muted = await client.patch(
            "/api/homeassistant/entity-policy",
            json={"entity_id": "weather.forecast_home", "muted": True},
        )

    assert muted.status_code == 200
    assert muted.json()["purged_pending_banter_count"] == 0
    # Released via invalidate_entity's return value, not the physical-queue purge.
    status = director.admin_status()
    assert status["reserved_count"] == 0
    assert status["cooling_count"] == 0
    assert status["session_counters"]["activated"] == 0
    assert status["session_counters"]["released"] == 1
    assert director._issued_facts[fact.fact_id].state == "released"
    settled = director._settled_queue_ids["inflight-queue"]
    assert settled.terminal_state == "released"
    assert settled.revision_current is False

    # If a stale callback arrives after the route released this unstarted work,
    # it must remain a no-op for listener cooldown accounting.
    before = director.admin_status()
    assert director.activate("inflight-queue", fact_id=fact.fact_id) is False
    after = director.admin_status()
    assert after["cooling_count"] == 0
    assert after["session_counters"] == before["session_counters"]


@pytest.mark.asyncio
async def test_personal_moment_enable_rejects_non_presence_entity(tmp_path):
    """Enabling a personal moment on an entity that is not a live room-presence
    sensor is refused with 422 and never persisted (fail-closed consent)."""
    from mammamiradio.home.entity_policy import personal_moment_opt_in_entity_ids

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.home.ha_context._ha_cache", None):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.patch(
                "/api/homeassistant/entity-policy",
                json={"entity_id": "switch.kitchen_light", "personal_moment_enabled": True},
            )

    assert resp.status_code == 422
    assert "personal moment" in resp.json()["detail"]
    assert personal_moment_opt_in_entity_ids(tmp_path) == set()


@pytest.mark.asyncio
async def test_entity_policy_requires_exactly_one_action(tmp_path):
    """The PATCH contract accepts exactly one of muted / personal_moment_enabled;
    both-present or neither-present is a 422."""
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        both = await client.patch(
            "/api/homeassistant/entity-policy",
            json={"entity_id": "switch.kitchen_light", "muted": True, "personal_moment_enabled": True},
        )
        neither = await client.patch(
            "/api/homeassistant/entity-policy",
            json={"entity_id": "switch.kitchen_light"},
        )

    assert both.status_code == 422
    assert neither.status_code == 422
