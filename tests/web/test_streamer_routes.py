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
import contextlib
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.audio.norm_cache import recent_music_identity_keys, select_norm_cache_rescue
from mammamiradio.core.config import load_config
from mammamiradio.core.first_listen import (
    FirstListenInstallOriginStatus,
    FirstListenInstallOriginV1,
    FirstListenReceiptV1,
)
from mammamiradio.core.listener_session import ListenerSession, ListenerSessionCueState
from mammamiradio.core.models import (
    LISTENER_REQUEST_DEDICATION_QUEUE_ID_KEY,
    LISTENER_REQUEST_HANDOFF_ADMITTED_KEY,
    LISTENER_REQUEST_HANDOFF_EXCLUSIVE_KEY,
    LISTENER_REQUEST_HANDOFF_TOKEN_KEY,
    GenerationWasteReason,
    PlaylistSource,
    RuntimeProviderObservation,
    Segment,
    SegmentLogEntry,
    SegmentType,
    StationState,
    Track,
)
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
    _apply_loaded_source,
    _commit_audible_stream_segment,
    _consume_queue_shadow,
    _continuity_reservation_segments,
    _copy_home_context_to_state,
    _ha_playback_access_snapshot,
    _packaged_recovery_segment,
    _persist_completed_music,
    _record_provider_verdict,
    _run_provider_verdict,
    _stream_chunk_size,
    router,
    run_playback_loop,
)

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")
SAME_ORIGIN = {"Origin": "http://testserver"}
TEST_CSRF_TOKEN = "test-active-setup-csrf-token"
ACTIVE_SETUP_HEADERS = {
    **SAME_ORIGIN,
    "Host": "127.0.0.1",
    "X-Radio-CSRF-Token": TEST_CSRF_TOKEN,
}


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
    app.state.csrf_token = TEST_CSRF_TOKEN
    # Most route tests model a proven pre-feature install. Individual First
    # Listen tests opt into fresh/unknown explicitly to exercise the gate.
    app.state.first_listen_install_origin = FirstListenInstallOriginV1(FirstListenInstallOriginStatus.EXISTING)
    # This minimal app injects authoritative setup state directly.  It must
    # opt in explicitly rather than relying on a taskless bootstrap tuple.
    app.state.first_listen_bootstrap_snapshot_authoritative = True
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
        rescue = select_norm_cache_rescue(tmp_path, state, allow_recent_repeat=True)

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

    with patch("mammamiradio.audio.norm_cache.time.monotonic", return_value=10_000.0):
        state.rescue_airplay[cooling] = 10_000.0 - 60.0
        segments = _continuity_reservation_segments(state, SimpleNamespace(), target_seconds=1.0, max_segments=1)

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

    with patch("mammamiradio.audio.norm_cache.time.monotonic", return_value=10_000.0):
        state.rescue_airplay.update({path: 10_000.0 - 60.0 for path in cooling_paths})
        segments = _continuity_reservation_segments(state, SimpleNamespace(), target_seconds=1.0, max_segments=1)

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

    with patch("mammamiradio.audio.norm_cache.time.monotonic", return_value=10_000.0):
        state.rescue_airplay[older] = 10_000.0 - 100.0
        state.rescue_airplay[newer] = 10_000.0 - 10.0
        segments = _continuity_reservation_segments(state, SimpleNamespace(), target_seconds=1.0, max_segments=1)

    assert [seg.path for seg in segments] == [older]


# ---------------------------------------------------------------------------
# The 2026-07-24 incident: a live control re-reserved the song still on air,
# with the packaged sweeper in front of it, so the listener heard
#   Don't Lose Your Way -> "Siamo sempre in onda..." -> Don't Lose Your Way.
# All three scenarios required by the audio-delivery test coverage rule.
# ---------------------------------------------------------------------------


def _on_air(state, *, title: str, artist: str) -> None:
    state.now_streaming = {
        "type": "music",
        "label": f"{artist} – {title}",
        "metadata": {"title": f"{artist} – {title}", "title_only": title, "artist": artist},
    }


def test_continuity_reservation_never_reserves_the_song_on_air_and_skips_the_sweeper(tmp_path):
    """Scenario 1 (normal): the incident state, replayed.

    A control fires two minutes into a 3.5-minute play, so the on-air song has
    NO rescue_airplay entry yet — the cooldown cannot see it. The reservation
    must still refuse it, and must go straight into the other cached song with
    no packaged clip in front.
    """
    state = StationState()
    on_air = _write_indexed_cache_track(
        tmp_path, "norm_on_air_192k.mp3", title="Dont Lose Your Way", artist="Fleece", duration=211.0, state=state
    )
    other = _write_indexed_cache_track(
        tmp_path, "norm_other_192k.mp3", title="Something Else", artist="Nomadi", duration=190.0, state=state
    )
    _on_air(state, title="Dont Lose Your Way", artist="Fleece")
    assert state.rescue_airplay == {}  # mid-song: the cooldown has nothing on it

    segments = _continuity_reservation_segments(state, SimpleNamespace(), target_seconds=240.0, max_segments=6)

    assert [seg.path for seg in segments] == [other]
    assert all(seg.type is SegmentType.MUSIC for seg in segments)
    assert on_air not in {seg.path for seg in segments}
    recovery = _DEMO_ASSETS_DIR / "recovery" / "continuity_1.mp3"
    assert recovery not in {seg.path for seg in segments}


def test_continuity_reservation_falls_back_to_packaged_audio_rather_than_repeating_the_on_air_song(tmp_path):
    """Scenario 2 (empty fallback): the on-air song is the ONLY cached track.

    This is the dead-air proof for refusing to share select_norm_cache_rescue's
    ``candidates or norm_files`` collapse. The reservation degrades down its own
    ladder — packaged clip, then emergency tone — but never re-airs the song the
    listener is hearing right now, and is never empty.
    """
    state = StationState()
    on_air = _write_indexed_cache_track(
        tmp_path, "norm_on_air_192k.mp3", title="Dont Lose Your Way", artist="Fleece", duration=211.0, state=state
    )
    _on_air(state, title="Dont Lose Your Way", artist="Fleece")

    segments = _continuity_reservation_segments(state, SimpleNamespace(), target_seconds=240.0, max_segments=6)

    assert [seg.path for seg in segments] == [_DEMO_ASSETS_DIR / "recovery" / "continuity_1.mp3"]
    assert segments[0].type is SegmentType.BANTER
    assert on_air not in {seg.path for seg in segments}

    # ...and with the packaged speech unavailable too (the real container ships
    # README stubs), the tone still keeps the station audible.
    with patch("mammamiradio.web.streamer.is_approved_spoken_asset", return_value=False):
        segments = _continuity_reservation_segments(state, SimpleNamespace(), target_seconds=240.0, max_segments=6)

    assert [seg.path for seg in segments] == [_DEMO_ASSETS_DIR / "recovery" / "emergency_tone.mp3"]
    assert segments[0].metadata.get("audio_source") == "emergency_tone"
    assert on_air not in {seg.path for seg in segments}


def test_continuity_reservation_after_restart_does_not_replay_the_handoff_song(tmp_path):
    """Scenario 3 (post-restart): nothing survives the restart except last_music_file.

    ``stream_log``, ``now_streaming`` and ``rescue_airplay`` are all empty in a
    fresh process, so the ``cached == state.last_music_file`` guard is the ONLY
    live guard here — which is exactly why it must be kept alongside the new
    recent-identity filter.
    """
    state = StationState()
    state.session_stopped = True  # flag persisted from the prior run
    handoff = _write_indexed_cache_track(
        tmp_path, "norm_handoff_192k.mp3", title="Handoff Song", artist="A", duration=190.0, state=state
    )
    other = _write_indexed_cache_track(
        tmp_path, "norm_other_192k.mp3", title="Other Song", artist="B", duration=190.0, state=state
    )
    state.last_music_file = handoff  # as main.py::_admit_restart_handoff leaves it
    assert not state.stream_log and not state.now_streaming and not state.rescue_airplay

    segments = _continuity_reservation_segments(state, SimpleNamespace(), target_seconds=190.0, max_segments=6)

    assert [seg.path for seg in segments] == [other]

    # With the handoff song as the only cached track, it still degrades to
    # packaged audio rather than replaying what the listener just heard.
    state.immediate_audio_index.pop(other)
    segments = _continuity_reservation_segments(state, SimpleNamespace(), target_seconds=190.0, max_segments=6)

    assert segments
    assert handoff not in {seg.path for seg in segments}


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
async def test_companionship_cue_without_an_accepting_listener_clears_selected_state(tmp_path):
    from mammamiradio.integrations.now_playing import router as integrations_router

    app = _make_test_app()
    app.include_router(integrations_router)
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
        assert state.now_streaming == {}
        assert state.current_stream_audible is False
        assert state.last_air_monotonic is None
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            now_playing = await client.get("/api/integrations/v1/now-playing")
        assert now_playing.json()["session_state"] == "empty_queue"
        assert now_playing.json()["now_playing"] is None
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


def test_playback_gap_rescue_asks_permissively_because_nothing_real_is_below_it():
    """The gap rescue must not decline a song in favour of a looping canned clip.

    Audio-delivery Scenario 2 (empty fallback), pinned at the call site. The
    rungs this site's earlier comment named as "below" it do not exist: there is
    no ``assets/demo/music/`` in the package, and the packaged-clip branch sets
    ``segment_ready``, which makes the 60s forced-banter escape unreachable. So a
    strict ask here means the same 4.4s clip on repeat while a playable song sits
    in the cache — a worse illusion break than the repeat it was avoiding.

    Two assertions, because the fix is only correct if BOTH hold: the packaged
    demo-music rung really is absent, and the call site really is permissive.
    """
    import inspect

    from mammamiradio.core.packaged_assets import DEMO_ASSETS_DIR

    assert not (DEMO_ASSETS_DIR / "music").exists(), (
        "assets/demo/music/ now ships — re-evaluate whether this rung can ask strictly again"
    )

    source = inspect.getsource(run_playback_loop)
    gap_call = next(
        (line for line in source.splitlines() if "_select_norm_cache_rescue(" in line),
        None,
    )
    assert gap_call is not None, "playback-gap rescue call site disappeared"
    assert "allow_recent_repeat=True" in gap_call, (
        "the playback-gap rescue must ask permissively while nothing real sits below it"
    )


@pytest.mark.asyncio
async def test_playback_gap_rescue_airs_a_recent_song_rather_than_looping_the_clip(tmp_path):
    """Recent-only warm cache: a real song airs, not the packaged clip on repeat.

    The behavioural half of the test above. `origin/main` always returned a song
    when the cache was non-empty; a strict ask here returned None and dropped the
    listener onto a 4.4s canned line that then repeated, because the rungs below
    were empty. This drives the real playback loop to prove a song wins.
    """
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    state = app.state.station_state

    # The ONLY cached song is one that just aired, so every candidate is "recent"
    # and a strict ask declines all of them. Seed the recency through stream_log,
    # not now_streaming: the loop clears now_streaming before it reaches the
    # rescue, which would leave recent_keys empty and make this test vacuous.
    on_air = tmp_path / "norm_only_song_192k.mp3"
    on_air.write_bytes(b"x" * 65536)
    (tmp_path / "norm_only_song_192k.mp3.json").write_text('{"title": "Only Song", "artist": "Solo"}')
    state.stream_log.append(
        SegmentLogEntry(
            type="music",
            label="Solo – Only Song",
            metadata={"title_only": "Only Song", "artist": "Solo"},
        )
    )
    assert recent_music_identity_keys(state), "test setup failed to make the cached song look recent"

    aired_source = None
    task = asyncio.create_task(run_playback_loop(app))
    try:
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            meta = (state.now_streaming or {}).get("metadata", {})
            source = meta.get("audio_source")
            if source:
                aired_source = source
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert aired_source == "norm_cache", (
        f"a recent-only warm cache must still air its song, got {aired_source!r} "
        "(a strict ask here returns None and drops the listener onto the looping canned clip)"
    )


@pytest.mark.asyncio
async def test_rescue_airplay_is_stamped_mid_segment_not_only_at_the_end(tmp_path):
    """A cached rescue enters the rotation cooldown on its FIRST heard chunk.

    The 2026-07-24 incident happened because the only stamp was at segment end:
    a live control firing two minutes into a 3.5-minute play saw the on-air song
    as never heard and re-reserved it. The stamp must land while the song is
    still playing, so anything asking "has this aired recently?" gets a true
    answer for the whole duration.
    """
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()
    state = app.state.station_state

    audio_path = tmp_path / "norm_rescue_192k.mp3"
    audio_path.write_bytes(b"x" * 65536)
    app.state.queue.put_nowait(
        Segment(
            type=SegmentType.MUSIC,
            path=audio_path,
            metadata={
                "title": "Rescue Song",
                "title_only": "Rescue Song",
                "artist": "Cache Artist",
                "audio_source": "norm_cache",
                "rescue": True,
            },
        )
    )

    stamped_while_playing = asyncio.Event()
    real_broadcast = app.state.stream_hub.broadcast

    async def _watch(chunk):
        accepted = await real_broadcast(chunk)
        if audio_path in state.rescue_airplay:
            stamped_while_playing.set()
        return accepted

    app.state.stream_hub.broadcast = _watch

    task = asyncio.create_task(run_playback_loop(app))
    try:
        await asyncio.wait_for(stamped_while_playing.wait(), timeout=3.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert audio_path in state.rescue_airplay


@pytest.mark.asyncio
async def test_air_start_stamp_needs_a_listener_to_accept_the_chunk(tmp_path):
    """The air-start stamp fires only for audio a listener queue actually took.

    Scoped to the air-start stamp: the end-of-segment stamp keeps its own,
    pre-existing predicate (a listener was in the room and bytes flowed), so
    this asserts mid-flight state rather than the final map.
    """
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()
    state = app.state.station_state
    state.last_air_monotonic = 99.0

    audio_path = tmp_path / "norm_unheard_192k.mp3"
    audio_path.write_bytes(b"x" * 65536)
    app.state.queue.put_nowait(
        Segment(
            type=SegmentType.MUSIC,
            path=audio_path,
            metadata={"title": "Unheard", "title_only": "Unheard", "artist": "A", "audio_source": "norm_cache"},
            runtime_provider_observations={
                "script_provider": RuntimeProviderObservation(
                    current_provider="openai",
                    primary_provider="anthropic",
                    fallback_active=True,
                    current_reason="anthropic_exception",
                )
            },
        )
    )

    chunks_dropped = 0
    stamped_mid_flight = False

    async def _drop_every_chunk(_chunk):
        nonlocal chunks_dropped, stamped_mid_flight
        chunks_dropped += 1
        if audio_path in state.rescue_airplay:
            stamped_mid_flight = True
        return 0  # no listener queue accepted it

    app.state.stream_hub.broadcast = _drop_every_chunk

    task = asyncio.create_task(run_playback_loop(app))
    try:
        await asyncio.sleep(0.4)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert chunks_dropped > 1, "the loop must have sent several chunks for this to mean anything"
    assert not stamped_mid_flight
    assert state.last_air_monotonic == 99.0
    assert state.runtime_provider_state == {}
    assert list(state.played_track_log) == []
    outcome = state.stream_outcome_history[0]
    assert outcome["result"] == "not_streamed"
    # `bytes_sent` counts bytes the loop WROTE; `accepted_listener_count` carries
    # the audible truth. Keeping them separate is what lets an empty room report
    # `no_listeners` rather than `not_streamed`, which names a file error. Here a
    # listener was connected and rejected every chunk, so bytes were written and
    # none landed — `not_streamed` is right, and the two counters say why.
    assert outcome["bytes_sent"] > 0
    assert outcome["accepted_listener_count"] == 0


@pytest.mark.asyncio
async def test_rejected_banter_never_commits_audible_truth(tmp_path):
    """One rejected send stays unheard across every post-air consumer."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()
    state = app.state.station_state
    state.moment_store = MagicMock()
    state.release_campaign = MagicMock()

    audio_path = tmp_path / "rejected-banter.mp3"
    audio_path.write_bytes(b"x" * 4096)
    segment = Segment(
        type=SegmentType.BANTER,
        path=audio_path,
        metadata={
            "title": "Unheard banter",
            "ritual_moment_id": "moment-unheard",
            "release_beat_id": "beat-unheard",
            "memory_extraction": {"script_lines": [{"host": "Marco", "text": "unheard"}]},
        },
        runtime_provider_observations={
            "script_provider": RuntimeProviderObservation(
                current_provider="openai",
                primary_provider="anthropic",
                fallback_active=True,
                current_reason="anthropic_exception",
            )
        },
    )
    app.state.queue.put_nowait(segment)
    app.state.stream_hub.broadcast = AsyncMock(return_value=0)

    with patch("mammamiradio.hosts.memory_extractor.schedule_banter_memory_extraction") as schedule:
        task = asyncio.create_task(run_playback_loop(app))
        try:
            await asyncio.wait_for(app.state.queue.join(), timeout=1.0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    schedule.assert_not_called()
    state.moment_store.mark_airing.assert_not_called()
    state.moment_store.finalize.assert_called_once_with("moment-unheard", "not_streamed")
    state.release_campaign.record_stream_result.assert_called_once_with(
        segment.metadata,
        bytes_sent=4096,
        was_skipped=False,
        listeners=1,
        accepted_listeners=0,
    )
    assert state.runtime_provider_state == {}
    assert state.current_stream_audible is False
    assert list(state.recent_banter_paths) == []


@pytest.mark.asyncio
async def test_first_listener_accepted_chunk_commits_audible_state_once(tmp_path):
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()
    state = app.state.station_state
    audio_path = tmp_path / "accepted_once.mp3"
    audio_path.write_bytes(b"x" * (4096 * 4))
    app.state.queue.put_nowait(
        Segment(
            type=SegmentType.MUSIC,
            path=audio_path,
            duration_sec=180.0,
            metadata={
                "title": "Artist – Accepted",
                "title_only": "Accepted",
                "artist": "Artist",
                "duration_ms": 180_000,
            },
            runtime_provider_observations={
                "script_provider": RuntimeProviderObservation(
                    current_provider="openai",
                    primary_provider="anthropic",
                    fallback_active=True,
                    current_reason="anthropic_exception",
                ),
                "tts_provider": RuntimeProviderObservation(
                    current_provider="edge",
                    primary_provider="mixed_tts",
                    fallback_active=True,
                    current_reason="missing_credentials",
                ),
            },
        )
    )
    commits = 0
    original_commit = state.on_stream_segment_audible

    def _count_commit(segment):
        nonlocal commits
        commits += 1
        return original_commit(segment)

    state.on_stream_segment_audible = _count_commit
    task = asyncio.create_task(run_playback_loop(app))
    try:
        await asyncio.wait_for(app.state.queue.join(), timeout=1.0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert commits == 1
    assert state.audible_playback_epoch == state.playback_epoch == 1
    assert state.last_air_monotonic is not None
    assert len(state.played_track_log) == 1
    assert state.runtime_provider_state["script_provider"]["last_switch_reason"] == "anthropic_exception"
    assert state.runtime_provider_state["tts_provider"]["last_switch_reason"] == "missing_credentials"
    assert [event.provider_class for event in state.runtime_events] == [
        "script_provider",
        "tts_provider",
    ]
    assert state.stream_outcome_history[-1]["accepted_listener_count"] == 1


def test_audible_commit_logs_new_provider_events_when_object_ids_collide(caplog):
    state = StationState()
    state.update_runtime_provider(
        "audio_source",
        current_provider="norm_cache",
        primary_provider="charts",
        fallback_active=True,
        reason="old fallback",
    )
    assert state.runtime_events
    state.update_runtime_provider(
        "script_provider",
        current_provider="anthropic",
        primary_provider="anthropic",
        fallback_active=False,
        reason="primary_success",
    )
    state.update_runtime_provider(
        "tts_provider",
        current_provider="azure",
        primary_provider="azure",
        fallback_active=False,
        reason="primary_success",
    )
    segment = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/provider-id-collision.mp3"),
        metadata={"title": "Fallback render"},
        runtime_provider_observations={
            "script_provider": RuntimeProviderObservation(
                current_provider="openai",
                primary_provider="anthropic",
                fallback_active=True,
                current_reason="anthropic_exception",
            ),
            "tts_provider": RuntimeProviderObservation(
                current_provider="edge",
                primary_provider="azure",
                fallback_active=True,
                current_reason="missing_credentials",
            ),
        },
    )
    state.on_stream_segment_selected(segment)
    caplog.set_level(logging.INFO, logger="mammamiradio.web.streamer")

    # The old detector compared integer ids across a maxlen deque. Holding an
    # `id` collision here deterministically reproduced its lost-event path.
    with patch("mammamiradio.web.streamer.id", return_value=1, create=True):
        assert _commit_audible_stream_segment(state, segment, accepted_listeners=1) is True

    logged_classes = [
        record.provider_class for record in caplog.records if record.getMessage() == "provider_switch_event"
    ]
    assert logged_classes == ["script_provider", "tts_provider"]


@pytest.mark.asyncio
async def test_continuity_reservation_reports_a_bridge_fire_from_the_send_loop(tmp_path):
    """Reserved safety audio reports a bridge ONLY once a listener has it.

    The wiring, not the helper. `_record_continuity_air` had three unit tests
    that all called it directly, so deleting its call site in the send loop left
    the entire `tests/web` suite green — the one thing the function exists to do
    was unguarded. This drives the real playback loop instead.
    """
    from mammamiradio.web.streamer import _CONTINUITY_RESERVATION_FLAG

    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()
    state = app.state.station_state

    audio_path = tmp_path / "norm_reserved_192k.mp3"
    audio_path.write_bytes(b"x" * 65536)
    app.state.queue.put_nowait(
        Segment(
            type=SegmentType.MUSIC,
            path=audio_path,
            metadata={
                "title": "Reserved Song",
                "title_only": "Reserved Song",
                "artist": "Cache Artist",
                "audio_source": "norm_cache",
                _CONTINUITY_RESERVATION_FLAG: True,
                "continuity_reservation_id": "res-abc",
            },
            ephemeral=False,
        )
    )

    reported = asyncio.Event()
    real_broadcast = app.state.stream_hub.broadcast

    async def _watch(chunk):
        accepted = await real_broadcast(chunk)
        if any(e.get("bridge_type") == "continuity" for e in state.bridge_events):
            reported.set()
        return accepted

    app.state.stream_hub.broadcast = _watch

    task = asyncio.create_task(run_playback_loop(app))
    try:
        await asyncio.wait_for(reported.wait(), timeout=3.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    continuity = [e for e in state.bridge_events if e.get("bridge_type") == "continuity"]
    assert len(continuity) == 1
    assert continuity[0]["source"] == "norm_cache"
    assert state.last_continuity_air_reservation_id == "res-abc"


@pytest.mark.asyncio
async def test_continuity_reservation_reports_nothing_when_no_listener_accepts(tmp_path):
    """Reserved audio nobody heard is not a bridge fire.

    Most reservations are never heard — the real queue refills first. Counting
    those would trip the 2-per-30-min "running on rescue" alarm after two
    ordinary admin actions.
    """
    from mammamiradio.web.streamer import _CONTINUITY_RESERVATION_FLAG

    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()
    state = app.state.station_state

    audio_path = tmp_path / "norm_unheard_reservation_192k.mp3"
    audio_path.write_bytes(b"x" * 65536)
    app.state.queue.put_nowait(
        Segment(
            type=SegmentType.MUSIC,
            path=audio_path,
            metadata={
                "title": "Unheard Reservation",
                "title_only": "Unheard Reservation",
                "artist": "A",
                "audio_source": "norm_cache",
                _CONTINUITY_RESERVATION_FLAG: True,
                "continuity_reservation_id": "res-unheard",
            },
            ephemeral=False,
        )
    )

    chunks_dropped = 0

    async def _drop_every_chunk(_chunk):
        nonlocal chunks_dropped
        chunks_dropped += 1
        return 0  # no listener queue accepted it

    app.state.stream_hub.broadcast = _drop_every_chunk

    task = asyncio.create_task(run_playback_loop(app))
    try:
        await asyncio.sleep(0.4)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert chunks_dropped > 1, "the loop must have sent several chunks for this to mean anything"
    assert not [e for e in state.bridge_events if e.get("bridge_type") == "continuity"]
    assert state.last_continuity_air_reservation_id == ""


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

    original_on_stream_segment_selected = app.state.station_state.on_stream_segment_selected

    def _on_stream_segment_then_disconnect(segment):
        epoch = original_on_stream_segment_selected(segment)
        app.state.stream_hub.unsubscribe(listener_id)
        return epoch

    app.state.station_state.on_stream_segment_selected = _on_stream_segment_then_disconnect

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
    file_error = next(
        outcome
        for outcome in app.state.station_state.stream_outcome_history
        if outcome["terminal_reason"] == "file_error"
    )
    assert file_error["result"] == "not_streamed"
    assert file_error["bytes_sent"] == 0
    assert file_error["accepted_listener_count"] == 0
    assert all(entry.label != "Vanished" for entry in app.state.station_state.stream_log)


@pytest.mark.asyncio
async def test_missing_admitted_song_after_dedication_restores_promise_until_retry_emits(tmp_path):
    """A dedication cannot silently spend its song promise on a vanished file."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    _, listener_audio = app.state.stream_hub.subscribe()
    state = app.state.station_state
    requested = Track(
        title="Albachiara",
        artist="Vasco Rossi",
        duration_ms=180_000,
        youtube_id="listener-request-source",
    )
    dedication_queue_id = "aired-listener-dedication"
    assert state.arm_listener_request_handoff(
        {"request_id": "listener-request"},
        requested,
        dedication_queue_id=dedication_queue_id,
    )
    original_handoff = state.listener_request_handoff
    assert original_handoff is not None

    dedication_audio = b"listener dedication"
    dedication_path = tmp_path / "listener-dedication.mp3"
    dedication_path.write_bytes(dedication_audio)
    dedication = Segment(
        type=SegmentType.BANTER,
        path=dedication_path,
        metadata={"queue_id": dedication_queue_id, "title": "Listener dedication"},
        ephemeral=False,
    )
    missing = Segment(
        type=SegmentType.MUSIC,
        path=tmp_path / "deleted-before-playback.mp3",
        metadata={
            "queue_id": "first-promised-song",
            "title": requested.display,
            "title_only": requested.title,
            "artist": requested.artist,
            "youtube_id": requested.youtube_id,
            **state.listener_request_handoff_metadata(requested),
        },
        ephemeral=False,
    )
    missing.path.write_bytes(b"promised audio")
    state.admit_listener_request_handoff(missing)
    state.queued_segments = [
        {"id": dedication_queue_id, "type": "banter", "label": "Listener dedication"},
        {"id": missing.metadata["queue_id"], "type": "music", "label": requested.display},
    ]
    app.state.queue.put_nowait(dedication)
    app.state.queue.put_nowait(missing)
    # Model eviction after admission but before playback claims the song. The
    # dedication remains readable and therefore really airs first.
    missing.path.unlink()

    with patch("mammamiradio.web.streamer._persist_completed_music", new=AsyncMock()):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            await asyncio.wait_for(app.state.queue.join(), timeout=1.0)
            assert listener_audio.get_nowait() == dedication_audio

            # _start_stream_segment published the song before open() failed, but
            # the admitted tombstone survived long enough to restore the exact
            # request-owned handoff rather than silently losing the promise.
            restored = state.listener_request_handoff
            assert restored is not None
            assert restored.token == original_handoff.token
            assert restored.request_id == original_handoff.request_id
            assert restored.dedication_queue_id == dedication_queue_id
            assert restored.matches_track(requested)
            assert state.listener_request_admitted_reservations == {}

            retry_path = tmp_path / "retry.mp3"
            retry_path.write_bytes(b"retry audio")
            retry = Segment(
                type=SegmentType.MUSIC,
                path=retry_path,
                metadata={
                    "queue_id": "retried-promised-song",
                    "title": requested.display,
                    "title_only": requested.title,
                    "artist": requested.artist,
                    "youtube_id": requested.youtube_id,
                    **state.listener_request_handoff_metadata(requested),
                },
                ephemeral=False,
            )
            state.admit_listener_request_handoff(retry)
            state.queued_segments = [{"id": retry.metadata["queue_id"], "type": "music", "label": requested.display}]
            app.state.queue.put_nowait(retry)
            assert state.listener_request_admitted_reservations

            await asyncio.wait_for(app.state.queue.join(), timeout=1.0)
            assert listener_audio.get_nowait() == b"retry audio"
            assert state.listener_request_handoff is None
            assert state.listener_request_admitted_reservations == {}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_pre_byte_skip_releases_admitted_song_without_retry(tmp_path):
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()
    state = app.state.station_state
    requested = Track(
        title="Albachiara",
        artist="Vasco Rossi",
        duration_ms=180_000,
        youtube_id="skipped-request-source",
    )
    assert state.arm_listener_request_handoff({"request_id": "skipped-request"}, requested)
    song_path = tmp_path / "skipped-before-first-byte.mp3"
    song_path.write_bytes(b"promised audio")
    song = Segment(
        type=SegmentType.MUSIC,
        path=song_path,
        metadata={
            "queue_id": "skipped-promised-song",
            "title": requested.display,
            "title_only": requested.title,
            "artist": requested.artist,
            "youtube_id": requested.youtube_id,
            **state.listener_request_handoff_metadata(requested),
        },
        ephemeral=False,
    )
    state.admit_listener_request_handoff(song)
    state.queued_segments = [{"id": song.metadata["queue_id"], "type": "music", "label": requested.display}]
    app.state.queue.put_nowait(song)

    def _request_skip_before_read(_file) -> None:
        app.state.skip_event.set()

    with patch("mammamiradio.web.streamer._skip_id3_and_xing_header", side_effect=_request_skip_before_read):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            await asyncio.wait_for(app.state.queue.join(), timeout=1.0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert state.listener_request_handoff is None
    assert state.listener_request_admitted_reservations == {}
    assert not state.listener_request_retry_handoffs


@pytest.mark.asyncio
async def test_missing_dedication_revokes_pending_listener_handoff(tmp_path):
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()
    state = app.state.station_state
    requested = Track(title="Albachiara", artist="Vasco Rossi", duration_ms=180000, youtube_id="pending")
    dedication_queue_id = "missing-pending-dedication"
    assert state.arm_listener_request_handoff(
        {"request_id": "pending-request"},
        requested,
        dedication_queue_id=dedication_queue_id,
    )
    app.state.queue.put_nowait(
        Segment(
            type=SegmentType.BANTER,
            path=tmp_path / "missing-dedication.mp3",
            metadata={"queue_id": dedication_queue_id, "title": "Listener dedication"},
            ephemeral=False,
        )
    )

    task = asyncio.create_task(run_playback_loop(app))
    try:
        await asyncio.wait_for(app.state.queue.join(), timeout=1.0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert state.listener_request_handoff is None


@pytest.mark.asyncio
@pytest.mark.parametrize("request_exclusive", [True, False], ids=["exclusive", "borrowed-operator"])
@pytest.mark.parametrize("dedication_exists", [False, True], ids=["missing", "empty"])
async def test_unheard_dedication_settles_linked_music_before_first_byte(
    tmp_path,
    request_exclusive,
    dedication_exists,
):
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()
    state = app.state.station_state
    dedication_queue_id = f"missing-linked-dedication-{request_exclusive}"
    dedication = Segment(
        type=SegmentType.BANTER,
        path=tmp_path / f"missing-dedication-{request_exclusive}.mp3",
        metadata={"queue_id": dedication_queue_id, "title": "Listener dedication"},
        ephemeral=False,
    )
    if dedication_exists:
        dedication.path.write_bytes(b"")
    music_path = tmp_path / f"linked-music-{request_exclusive}.mp3"
    music_path.write_bytes(b"x" * 4096)
    music = Segment(
        type=SegmentType.MUSIC,
        path=music_path,
        metadata={
            "queue_id": f"linked-music-{request_exclusive}",
            "title": "Albachiara",
            "title_only": "Albachiara",
            "artist": "Vasco Rossi",
            LISTENER_REQUEST_HANDOFF_TOKEN_KEY: "admitted-token",
            LISTENER_REQUEST_HANDOFF_ADMITTED_KEY: True,
            LISTENER_REQUEST_DEDICATION_QUEUE_ID_KEY: dedication_queue_id,
            LISTENER_REQUEST_HANDOFF_EXCLUSIVE_KEY: request_exclusive,
        },
        ephemeral=False,
    )
    app.state.queue.put_nowait(dedication)
    app.state.queue.put_nowait(music)
    state.queued_segments = [
        {"id": dedication_queue_id, "type": "banter", "label": "Listener dedication"},
        {"id": music.metadata["queue_id"], "type": "music", "label": "Albachiara"},
    ]

    with patch("mammamiradio.web.streamer._persist_completed_music", new=AsyncMock()) as persist:
        task = asyncio.create_task(run_playback_loop(app))
        try:
            await asyncio.wait_for(app.state.queue.join(), timeout=1.0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert state.listener_request_handoff is None
    if request_exclusive:
        persist.assert_not_awaited()
        assert not any(row.get("id") == music.metadata["queue_id"] for row in state.queued_segments)
    else:
        persist.assert_awaited_once()
        for key in (
            LISTENER_REQUEST_HANDOFF_TOKEN_KEY,
            LISTENER_REQUEST_HANDOFF_ADMITTED_KEY,
            LISTENER_REQUEST_DEDICATION_QUEUE_ID_KEY,
            LISTENER_REQUEST_HANDOFF_EXCLUSIVE_KEY,
        ):
            assert key not in music.metadata


@pytest.mark.asyncio
async def test_run_playback_loop_skips_mid_read_oserror_and_survives(tmp_path):
    """F3 covers the open()-time failure; this covers the read()-time failure —
    the file opens fine (bytes_sent > 0 from earlier chunks) but a later
    f.read(chunk_size) call raises. Must still skip and continue, not crash."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()
    app.state.station_state.moment_store = MagicMock()
    app.state.station_state.release_campaign = MagicMock()

    flaky_path = tmp_path / "flaky.mp3"
    flaky_path.write_bytes(b"x" * (4096 * 3))
    good_path = tmp_path / "good.mp3"
    good_path.write_bytes(b"x" * 4096)
    flaky = Segment(
        type=SegmentType.MUSIC,
        path=flaky_path,
        metadata={
            "title": "Flaky",
            "title_only": "Flaky",
            "artist": "Artist",
            "ritual_moment_id": "partial-moment",
            "release_beat_id": "partial-beat",
        },
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
    partial_error = next(
        outcome
        for outcome in app.state.station_state.stream_outcome_history
        if outcome["terminal_reason"] == "file_error"
    )
    # A mid-read failure TRUNCATES the segment, so it did not air in full.
    # Classifying it `aired` told the Moment Receipt panel a home-triggered
    # moment "made it to air" and let a cut-off release beat count a delivery
    # against max_airings. A file that never opened is different: it writes zero
    # bytes and still classifies `not_streamed` (see the missing-file test).
    assert partial_error["result"] == "skipped"
    assert partial_error["bytes_sent"] > 0
    assert partial_error["accepted_listener_count"] == 1
    app.state.station_state.moment_store.finalize.assert_any_call("partial-moment", "skipped")
    app.state.station_state.release_campaign.record_stream_result.assert_any_call(
        flaky.metadata,
        bytes_sent=partial_error["bytes_sent"],
        was_skipped=True,
        listeners=1,
        accepted_listeners=1,
    )


@pytest.mark.asyncio
async def test_rejected_mid_read_oserror_stays_not_streamed_across_post_air_truth(tmp_path):
    """A connected room that rejects every partial chunk never becomes a skip.

    ``bytes_sent`` records bytes written by the loop, not listener acceptance.
    A later read error must therefore use accepted-listener truth: an accepted
    partial send is ``skipped`` (covered above), while this wholly rejected
    delivery remains ``not_streamed`` everywhere downstream.
    """
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()
    state = app.state.station_state
    state.moment_store = MagicMock()
    state.release_campaign = MagicMock()

    flaky_path = tmp_path / "rejected-flaky-banter.mp3"
    flaky_path.write_bytes(b"x" * (4096 * 3))
    segment = Segment(
        type=SegmentType.BANTER,
        path=flaky_path,
        metadata={
            "title": "Rejected partial banter",
            "ritual_moment_id": "rejected-partial-moment",
            "release_beat_id": "rejected-partial-beat",
            "memory_extraction": {"script_lines": [{"host": "Marco", "text": "unheard"}]},
        },
        runtime_provider_observations={
            "script_provider": RuntimeProviderObservation(
                current_provider="openai",
                primary_provider="anthropic",
                fallback_active=True,
                current_reason="anthropic_exception",
            )
        },
    )
    app.state.queue.put_nowait(segment)
    app.state.stream_hub.broadcast = AsyncMock(return_value=0)

    real_open = open

    class _RejectedFlakyReaderFile:
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
            if self._reads == 4:  # header probes, one rejected chunk, then fail
                raise OSError("disk read failed after rejected chunk")
            return self._f.read(*args, **kwargs)

        def seek(self, *args, **kwargs):
            return self._f.seek(*args, **kwargs)

        def tell(self, *args, **kwargs):
            return self._f.tell(*args, **kwargs)

    def _open_side_effect(path, mode="rb", *args, **kwargs):
        if str(path) == str(flaky_path):
            return _RejectedFlakyReaderFile(path, mode)
        return real_open(path, mode, *args, **kwargs)

    with (
        patch("builtins.open", side_effect=_open_side_effect),
        patch("mammamiradio.hosts.memory_extractor.schedule_banter_memory_extraction") as schedule,
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            await asyncio.wait_for(app.state.queue.join(), timeout=1.0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    rejected_error = next(
        outcome for outcome in state.stream_outcome_history if outcome["terminal_reason"] == "file_error"
    )
    assert rejected_error["result"] == "not_streamed"
    assert rejected_error["bytes_sent"] > 0
    assert rejected_error["starting_listener_count"] == 1
    assert rejected_error["accepted_listener_count"] == 0
    state.moment_store.mark_airing.assert_not_called()
    state.moment_store.finalize.assert_called_once_with(
        "rejected-partial-moment",
        "not_streamed",
    )
    state.release_campaign.record_stream_result.assert_called_once_with(
        segment.metadata,
        bytes_sent=rejected_error["bytes_sent"],
        was_skipped=False,
        listeners=1,
        accepted_listeners=0,
    )
    schedule.assert_not_called()
    assert state.current_stream_audible is False
    assert state.audible_playback_epoch == 0
    assert state.last_air_monotonic is None
    assert state.runtime_provider_state == {}
    assert list(state.runtime_events) == []
    assert list(state.recent_banter_paths) == []


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
async def test_playback_built_rescue_rearms_after_epoch_changes_during_probe(tmp_path):
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    _, listener_queue = app.state.stream_hub.subscribe()
    state = app.state.station_state

    fallback_path = tmp_path / "fallback-after-control.mp3"
    fallback_path.write_bytes(b"current-timeline-rescue" * 512)
    audible_committed = False

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    async def _build_after_control(_path):
        await asyncio.sleep(0)
        state.continuity_epoch += 1
        return Segment(
            type=SegmentType.BANTER,
            path=fallback_path,
            duration_sec=1.7,
            metadata={"title": "Recovery", "canned": True},
            ephemeral=False,
        )

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=fallback_path),
        patch(
            "mammamiradio.web.streamer._packaged_recovery_segment",
            new=AsyncMock(side_effect=_build_after_control),
        ),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while listener_queue.empty():
                if time.monotonic() > deadline:
                    raise AssertionError("current-timeline rescue was discarded after the epoch changed")
                await asyncio.sleep(0.01)
            heard = listener_queue.get_nowait()
            while not state.current_stream_audible:
                if time.monotonic() > deadline:
                    raise AssertionError("accepted rescue never committed listener-audible state")
                await asyncio.sleep(0)
            audible_committed = True
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    metadata = state.now_streaming["metadata"]
    assert heard
    assert metadata["playback_gap_fill"] is True
    assert metadata["continuity_reservation"] is True
    assert metadata["continuity_admission_epoch"] == state.continuity_epoch
    assert audible_committed is True


@pytest.mark.asyncio
async def test_rejected_playback_rescue_preserves_gap_clock(tmp_path):
    from mammamiradio.web.streamer import _stamp_playback_gap_fill

    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()
    state = app.state.station_state
    gap_started = time.monotonic() - 20
    state.queue_empty_since = gap_started

    fallback_path = tmp_path / "rejected-rescue.mp3"
    fallback_path.write_bytes(b"rejected-rescue" * 512)
    second_wait_started = asyncio.Event()
    waits = 0

    async def _scripted_wait(awaitable, *_args, **_kwargs):
        nonlocal waits
        waits += 1
        if waits == 1:
            awaitable.close()
            await asyncio.sleep(0)
            raise TimeoutError
        second_wait_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            awaitable.close()

    def _stamp_then_invalidate(segment, current_state):
        stamped = _stamp_playback_gap_fill(segment, current_state)
        current_state.continuity_epoch += 1
        return stamped

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_scripted_wait)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=fallback_path),
        patch("mammamiradio.web.streamer.probe_duration_sec", return_value=1.7),
        patch("mammamiradio.web.streamer._stamp_playback_gap_fill", side_effect=_stamp_then_invalidate),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while not second_wait_started.is_set():
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not reject the stale rescue")
                await asyncio.sleep(0.01)
            assert state.queue_empty_since == gap_started
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert not state.stream_log
    assert state.discard_by_reason[GenerationWasteReason.STALE_CONTINUITY] == 1


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
            while app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "norm_cache":
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
    assert stream_log[1].metadata.get("audio_source") == "norm_cache"
    assert stream_log[1].metadata.get("title") == "Cache Artist – Cached Song"
    # `title` is a display label with the artist packed in, so it is NOT a usable
    # song identity. Without a bare `title_only` alongside it, segment_track_key
    # yields ("cache artist", "cache artist - cached song"): a shape that can
    # never be in state.blocklist, which silently disarms the ban fence this
    # rescue path runs before it airs. The blocklist is keyed on the same
    # (artist, title) pair the sidecar carries, so that pair must round-trip.
    assert stream_log[1].metadata.get("title_only") == "Cached Song"
    assert stream_log[1].metadata.get("artist") == "Cache Artist"
    assert not (stream_log[0].metadata.get("canned") and stream_log[1].metadata.get("canned"))
    assert pick_canned.call_args_list[0].args == ("recovery",)
    assert app.state.station_state.queue_empty_since is None


def test_norm_cache_rescue_fill_keys_onto_the_ban_identity():
    """A rescue fill must be recognisable to the blocklist it is checked against.

    Pins the round trip the test above proves end to end: the sidecar's
    (artist, title) is what the operator banned, so a segment built from that
    sidecar must produce the same key. This failed silently before: `title`
    carried "Artist - Title" and no `title_only`, so the fence compared a label
    against an identity and never matched.
    """
    from mammamiradio.audio.norm_cache import sidecar_track_key
    from mammamiradio.core.models import segment_track_key

    sidecar = {"title": "Cached Song", "artist": "Cache Artist"}
    fill = Segment(
        type=SegmentType.MUSIC,
        path=Path("/cache/norm_cached_song_192k.mp3"),
        metadata={
            "type": "music",
            "title": "Cache Artist – Cached Song",  # display label, artist packed in
            "title_only": "Cached Song",
            "artist": "Cache Artist",
            "audio_source": "norm_cache",
            "fallback": True,
        },
    )
    assert segment_track_key(fill) == sidecar_track_key(sidecar) == ("cache artist", "cached song")


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
    original_on_stream_segment_selected = state.on_stream_segment_selected

    def _on_stream_segment(segment):
        epoch = original_on_stream_segment_selected(segment)
        started.set()
        return epoch

    state.on_stream_segment_selected = _on_stream_segment
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
async def test_playback_rejects_late_blocklisted_music_from_normal_queue(tmp_path):
    """The queue and capacity-exempt slot share the same final ban fence."""
    app = _make_test_app()
    _, listener_queue = app.state.stream_hub.subscribe()
    state = app.state.station_state

    blocked_audio = b"blocked-queued-audio" * 512
    blocked_path = tmp_path / "blocked-queued.mp3"
    blocked_path.write_bytes(blocked_audio)
    blocked = Segment(
        type=SegmentType.MUSIC,
        path=blocked_path,
        duration_sec=180.0,
        metadata={
            "queue_id": "blocked-queued",
            "artist": "Late Artist",
            "title_only": "Late Song",
            "continuity_reservation": True,
        },
        ephemeral=False,
    )
    safe_audio = b"safe-queued-audio" * 512
    safe_path = tmp_path / "safe-queued.mp3"
    safe_path.write_bytes(safe_audio)
    safe = Segment(
        type=SegmentType.MUSIC,
        path=safe_path,
        duration_sec=180.0,
        metadata={
            "queue_id": "safe-queued",
            "artist": "Safe Artist",
            "title_only": "Safe Song",
        },
        ephemeral=False,
    )
    for segment in (blocked, safe):
        app.state.queue.put_nowait(segment)
    state.queued_segments = [
        {"id": "blocked-queued", "type": "music", "label": "Late Song"},
        {"id": "safe-queued", "type": "music", "label": "Safe Song"},
    ]
    state.last_music_file = safe_path
    state.last_enqueued_type = SegmentType.MUSIC
    state.blocklist = {("late artist", "late song"): {"display": "Late Artist - Late Song"}}

    task = asyncio.create_task(run_playback_loop(app))
    try:
        heard = await asyncio.wait_for(listener_queue.get(), timeout=3.0)
        assert safe_audio.startswith(heard)
        assert not heard.startswith(blocked_audio[:32])
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert state.discard_by_reason[GenerationWasteReason.OPERATOR_BAN] == 1
    assert all(entry.metadata.get("title_only") != "Late Song" for entry in state.stream_log)
    assert state.last_music_file == safe_path
    assert state.last_enqueued_type is SegmentType.MUSIC
    assert app.state.queue._unfinished_tasks == 0
    await asyncio.wait_for(app.state.queue.join(), timeout=1.0)


@pytest.mark.asyncio
async def test_playback_rejects_queued_song_claimed_by_late_listener_match(tmp_path):
    """A request matched after queueing still owns the song at the last mile."""
    app = _make_test_app()
    _, listener_queue = app.state.stream_hub.subscribe()
    state = app.state.station_state
    requested_track = Track(
        title="LItaliano",
        artist="Toto Cutugno",
        duration_ms=240_000,
        youtube_id="listener-requested-song",
    )
    request = {
        "request_id": "late-matched-listener-request",
        "type": "song_request",
        "song_found": True,
        "song_pinned": True,
        "song_track_obj": requested_track,
    }
    state.pending_requests.append(request)

    requested_audio = b"anonymous-requested-audio" * 512
    requested_path = tmp_path / "already-queued-request.mp3"
    requested_path.write_bytes(requested_audio)
    requested = Segment(
        type=SegmentType.MUSIC,
        path=requested_path,
        duration_sec=240.0,
        metadata={
            "queue_id": "already-queued-request",
            "artist": requested_track.artist,
            "title_only": "L'Italiano",
        },
        ephemeral=False,
    )
    safe_audio = b"safe-queued-audio" * 512
    safe_path = tmp_path / "safe-after-request.mp3"
    safe_path.write_bytes(safe_audio)
    safe = Segment(
        type=SegmentType.MUSIC,
        path=safe_path,
        duration_sec=180.0,
        metadata={
            "queue_id": "safe-after-request",
            "artist": "Safe Artist",
            "title_only": "Safe Song",
        },
        ephemeral=False,
    )
    for segment in (requested, safe):
        app.state.queue.put_nowait(segment)
    state.queued_segments = [
        {"id": "already-queued-request", "type": "music", "label": requested_track.display},
        {"id": "safe-after-request", "type": "music", "label": "Safe Artist - Safe Song"},
    ]
    state.last_music_file = safe_path
    state.last_enqueued_type = SegmentType.MUSIC

    task = asyncio.create_task(run_playback_loop(app))
    try:
        heard = await asyncio.wait_for(listener_queue.get(), timeout=3.0)
        assert safe_audio.startswith(heard)
        assert not heard.startswith(requested_audio[:32])
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert request in state.pending_requests
    assert state.discard_by_reason[GenerationWasteReason.LISTENER_REQUEST_RESERVED] == 1
    assert all(entry.metadata.get("title_only") != requested_track.title for entry in state.stream_log)
    assert app.state.queue._unfinished_tasks == 0
    await asyncio.wait_for(app.state.queue.join(), timeout=1.0)


@pytest.mark.asyncio
async def test_playback_rejects_blocklisted_demo_fallback_without_queue_task(tmp_path):
    """A non-queue rescue obeys the ban fence without unbalancing task accounting."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 64
    _, listener_queue = app.state.stream_hub.subscribe()
    state = app.state.station_state
    state.blocklist = {("late artist", "late song"): {"display": "Late Artist - Late Song"}}

    demo_dir = tmp_path / "demo" / "music"
    demo_dir.mkdir(parents=True)
    blocked_path = demo_dir / "Late Artist - Late Song.mp3"
    blocked_audio = b"blocked-demo-audio" * 512
    blocked_path.write_bytes(blocked_audio)
    safe_path = demo_dir / "Safe Artist - Safe Song.mp3"
    safe_audio = b"safe-demo-audio" * 512
    safe_path.write_bytes(safe_audio)

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
            side_effect=_scripted_clock([100.0, 101.1, 102.0, 103.1, 103.2]),
        ),
        patch("mammamiradio.web.streamer._ASSETS_DIR", tmp_path),
        patch("mammamiradio.web.streamer._random.choice", side_effect=[blocked_path, safe_path]),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while listener_queue.empty():
                if time.monotonic() > deadline:
                    raise AssertionError("safe fallback did not follow the rejected blocklisted fallback")
                await asyncio.sleep(0.01)
            heard = listener_queue.get_nowait()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert safe_audio.startswith(heard)
    assert not heard.startswith(blocked_audio[:32])
    assert state.discard_by_reason[GenerationWasteReason.OPERATOR_BAN] == 1
    assert all(entry.metadata.get("title") != "Late Song" for entry in state.stream_log)
    assert app.state.queue._unfinished_tasks == 0
    await asyncio.wait_for(app.state.queue.join(), timeout=1.0)


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
async def test_blocked_playback_waiter_delivers_fresh_resume_runway(tmp_path):
    """A pre-Stop queue waiter must accept runway admitted by the fast Resume.

    The grace window is widened so the waiter cannot fall out of `queue.get()`
    before Resume's runway lands. Without that barrier a slow runner lets the
    wait time out, the loop re-enters and re-captures the epoch *after* the Stop,
    and every assertion below still passes — the test would silently stop
    exercising the ABA fence instead of failing.
    """
    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    app.state.stream_hub.broadcast = AsyncMock(return_value=1)
    queue_wait_started = asyncio.Event()
    original_queue_get = app.state.queue.get
    captured_epochs: list[int] = []

    async def _observed_queue_get():
        captured_epochs.append(state.continuity_epoch)
        queue_wait_started.set()
        return await original_queue_get()

    app.state.queue.get = _observed_queue_get

    with patch("mammamiradio.web.streamer.FIRST_BYTE_GRACE_SECONDS", 30.0):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            await asyncio.wait_for(queue_wait_started.wait(), timeout=1.0)
            epoch_at_wait = state.continuity_epoch

            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                stopped = await client.post("/api/stop")
                resumed = await client.post("/api/resume")

            assert stopped.status_code == 200
            assert resumed.status_code == 200
            deadline = time.monotonic() + 2.0
            while app.state.stream_hub.broadcast.await_count == 0:
                if time.monotonic() > deadline:
                    raise AssertionError("fresh Resume runway did not reach the listener")
                await asyncio.sleep(0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # The ABA cycle really happened: the loop never re-entered `queue.get()` after
    # the Stop, so the selection it played was captured against the older epoch.
    assert len(captured_epochs) == 1
    assert epoch_at_wait < state.continuity_epoch
    assert state.session_stopped is False
    assert state.now_streaming["type"] in {"banter", "music"}
    assert state.now_streaming["metadata"]["continuity_admission_epoch"] == state.continuity_epoch
    assert state.discard_by_reason.get(GenerationWasteReason.STALE_CONTINUITY, 0) == 0


@pytest.mark.asyncio
async def test_blocked_playback_waiter_reparks_runway_when_resume_marker_fails(tmp_path, caplog):
    """A failed Resume persistence commit cannot consume its parked runway."""
    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    app.state.stream_hub.broadcast = AsyncMock(return_value=1)
    caplog.set_level(logging.INFO)
    first_path = tmp_path / "resume-first.mp3"
    second_path = tmp_path / "resume-second.mp3"
    first_path.write_bytes(b"first-resume-runway")
    second_path.write_bytes(b"second-resume-runway")
    reservations = [
        Segment(
            type=SegmentType.BANTER,
            path=first_path,
            duration_sec=4.0,
            metadata={
                "queue_id": "resume-first",
                "title": "First runway",
                "continuity_reservation": True,
            },
            ephemeral=False,
        ),
        Segment(
            type=SegmentType.MUSIC,
            path=second_path,
            duration_sec=180.0,
            metadata={
                "queue_id": "resume-second",
                "title": "Second runway",
                "continuity_reservation": True,
            },
            ephemeral=False,
        ),
    ]
    queue_wait_started = asyncio.Event()
    original_queue_get = app.state.queue.get

    async def _observed_queue_get():
        queue_wait_started.set()
        return await original_queue_get()

    app.state.queue.get = _observed_queue_get

    task = asyncio.create_task(run_playback_loop(app))
    try:
        await asyncio.wait_for(queue_wait_started.wait(), timeout=1.0)

        with patch(
            "mammamiradio.web.streamer._continuity_reservation_segments",
            return_value=reservations,
        ):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                stopped = await client.post("/api/stop")
                with patch("mammamiradio.web.streamer._persist_session_stopped", side_effect=OSError("read only")):
                    resumed = await client.post("/api/resume")

        assert stopped.status_code == 200
        assert resumed.status_code == 503
        deadline = time.monotonic() + 1.0
        while not any("Playback re-parked Resume runway" in record.message for record in caplog.records):
            if time.monotonic() > deadline:
                raise AssertionError("failed Resume runway was not re-parked")
            await asyncio.sleep(0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert state.session_stopped is True
    assert state.now_streaming["type"] == "stopped"
    assert app.state.queue.qsize() == len(state.queued_segments) == 2
    real_ids = [segment.metadata["queue_id"] for segment in app.state.queue._queue]
    shadow_ids = [row["id"] for row in state.queued_segments]
    assert real_ids == shadow_ids == ["resume-second", "resume-first"]
    assert all(
        segment.metadata["continuity_admission_epoch"] == state.continuity_epoch for segment in app.state.queue._queue
    )
    app.state.stream_hub.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_playback_loop_rearms_source_neutral_recovery_across_stop_resume_aba(tmp_path):
    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    app.state.stream_hub.broadcast = AsyncMock(return_value=1)
    clip = tmp_path / "continuity.mp3"
    clip.write_bytes(b"recovery" * 512)
    probe_started = asyncio.Event()
    release_second_probe = asyncio.Event()
    calls = 0

    async def _probe_across_stop_resume(_fallback):
        nonlocal calls
        calls += 1
        if calls > 1:
            await release_second_probe.wait()
        state.session_stopped = True
        state.continuity_epoch += 1
        state.session_stopped = False
        probe_started.set()
        return Segment(
            type=SegmentType.BANTER,
            path=clip,
            duration_sec=1.0,
            metadata={"title": "Pre-stop recovery", "rescue": True},
            ephemeral=False,
        )

    with (
        patch("mammamiradio.web.streamer.FIRST_BYTE_GRACE_SECONDS", 0.001),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=clip),
        patch(
            "mammamiradio.web.streamer._packaged_recovery_segment",
            new=AsyncMock(side_effect=_probe_across_stop_resume),
        ),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            await asyncio.wait_for(probe_started.wait(), timeout=1.0)
            deadline = time.monotonic() + 1.0
            while app.state.stream_hub.broadcast.await_count == 0:
                if time.monotonic() > deadline:
                    raise AssertionError("source-neutral recovery was not rearmed on the resumed timeline")
                await asyncio.sleep(0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    app.state.stream_hub.broadcast.assert_awaited()
    assert state.now_streaming["metadata"]["continuity_admission_epoch"] == state.continuity_epoch
    assert state.discard_by_reason.get(GenerationWasteReason.STALE_CONTINUITY, 0) == 0


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
            while app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "norm_cache":
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
            while app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "norm_cache":
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
        assert body["now_streaming"]["metadata"]["audio_source"] == "norm_cache"
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
            while app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "norm_cache":
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
            while app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "norm_cache":
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
            while app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "norm_cache":
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
            while app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "norm_cache":
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
    assert now_meta.get("audio_source") not in {"norm_cache", "fallback_demo_asset"}
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
            while state.now_streaming.get("metadata", {}).get("audio_source") != "norm_cache":
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
async def test_readyz_stays_starting_without_listener_accepted_audio():
    app = _make_test_app()
    app.state.start_time = time.time() - 31
    app.state.station_state.listeners_active = 0
    app.state.station_state.queue_empty_since = time.monotonic() - 35

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/readyz")

    assert resp.status_code == 503
    body = resp.json()
    assert body["silence_with_listeners"] is False
    assert body["ready"] is False
    assert body["status"] == "starting"


@pytest.mark.asyncio
async def test_stop_clears_readiness_after_listener_accepted_audio(tmp_path):
    """An accepted stream is ready until Stop clears the audible-session latch."""
    app = _make_test_app()
    state = app.state.station_state
    state.on_stream_segment(
        Segment(
            type=SegmentType.BANTER,
            path=tmp_path / "accepted-before-stop.mp3",
            metadata={"title": "Accepted before Stop"},
        )
    )
    state.last_air_monotonic = time.monotonic()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        before = await client.get("/readyz")
        stop = await client.post("/api/stop")
        after = await client.get("/readyz")

    assert before.status_code == 200
    assert before.json()["ready"] is True
    assert stop.status_code == 200
    assert stop.json()["ok"] is True
    assert after.status_code == 503
    body = after.json()
    assert body["ready"] is False
    assert body["status"] == "stopped"
    assert state.current_stream_audible is False
    assert state.last_air_monotonic is None


@pytest.mark.asyncio
async def test_readyz_returns_200_after_resumed_audio_is_accepted(tmp_path):
    """Clearing Stop is insufficient; readiness begins at listener acceptance."""
    app = _make_test_app()
    state = app.state.station_state
    state.session_stopped = False
    state.on_stream_segment(
        Segment(
            type=SegmentType.MUSIC,
            path=tmp_path / "accepted-after-resume.mp3",
            metadata={"title": "Accepted after Resume"},
        )
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/readyz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True


@pytest.mark.asyncio
async def test_readyz_requires_listener_accepted_audio_not_queue_or_startup_grace(tmp_path):
    app = _make_test_app()
    app.state.start_time = time.time() - 31
    state = app.state.station_state
    app.state.queue.put_nowait(
        Segment(
            type=SegmentType.MUSIC,
            path=tmp_path / "queued-not-accepted.mp3",
            metadata={"title": "Queued but unheard"},
        )
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        before = await client.get("/readyz")
        state.on_stream_segment(
            Segment(
                type=SegmentType.BANTER,
                path=tmp_path / "accepted-startup.mp3",
                metadata={"title": "Accepted startup"},
            )
        )
        after = await client.get("/readyz")

    assert before.status_code == 503
    assert before.json()["status"] == "starting"
    assert after.status_code == 200
    assert after.json()["status"] == "ready"


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
async def test_fresh_unfinished_audio_generator_prepends_show_before_live_subscription(tmp_path):
    """The mini-show is client-local and hands off to the ordinary live hub."""
    import threading

    from mammamiradio.web.streamer import _audio_generator

    app = _make_test_app()
    app.state.first_listen_install_origin = FirstListenInstallOriginV1(FirstListenInstallOriginStatus.FRESH)
    app.state.first_listen_receipt = None
    show = tmp_path / "first-listen-show.mp3"
    show.write_bytes(b"authored-mini-show")

    mock_request = MagicMock()
    mock_request.app = app
    mock_request.is_disconnected = AsyncMock(return_value=False)
    event_loop_thread = threading.get_ident()
    approval_threads: list[int] = []
    read_threads: list[int] = []

    def approve_show():
        approval_threads.append(threading.get_ident())
        return show

    def chunks(_path):
        read_threads.append(threading.get_ident())
        yield b"authored-mini-show"

    with (
        patch("mammamiradio.web.streamer.first_listen_show_required", return_value=True),
        patch("mammamiradio.web.streamer.approved_first_listen_show_path", side_effect=approve_show),
        patch("mammamiradio.web.streamer.iter_first_listen_show_chunks", side_effect=chunks),
    ):
        generator = _audio_generator(mock_request)
        assert await anext(generator) == b"authored-mini-show"
        assert app.state.station_state.listeners_active == 0

        live_chunk = asyncio.create_task(anext(generator))
        deadline = time.monotonic() + 1.0
        while app.state.station_state.listeners_active == 0:
            if time.monotonic() > deadline:
                raise AssertionError("mini-show did not hand off to the live hub")
            await asyncio.sleep(0)

        await app.state.stream_hub.broadcast(b"live-station")
        assert await live_chunk == b"live-station"
        await generator.aclose()

    assert app.state.station_state.listeners_active == 0
    assert approval_threads and approval_threads[0] != event_loop_thread
    assert read_threads and read_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_first_listen_show_read_failure_falls_through_to_live_audio(tmp_path):
    """A truncated packaged mini-show must hand off to live audio, never silence."""
    from mammamiradio.web.streamer import _audio_generator

    app = _make_test_app()
    show = tmp_path / "first-listen-show.mp3"
    show.write_bytes(b"authored-mini-show")

    def broken_chunks(_path):
        yield b"partial-show"
        raise OSError("truncated packaged show")

    mock_request = MagicMock()
    mock_request.app = app
    mock_request.is_disconnected = AsyncMock(return_value=False)

    with (
        patch("mammamiradio.web.streamer.first_listen_show_required", return_value=True),
        patch("mammamiradio.web.streamer.approved_first_listen_show_path", return_value=show),
        patch("mammamiradio.web.streamer.iter_first_listen_show_chunks", side_effect=broken_chunks),
    ):
        generator = _audio_generator(mock_request)
        assert await anext(generator) == b"partial-show"

        live_chunk = asyncio.create_task(anext(generator))
        deadline = time.monotonic() + 1.0
        while app.state.station_state.listeners_active == 0:
            if time.monotonic() > deadline:
                raise AssertionError("failed mini-show did not hand off to the live hub")
            await asyncio.sleep(0)

        await app.state.stream_hub.broadcast(b"live-station")
        assert await live_chunk == b"live-station"
        await generator.aclose()

    assert app.state.station_state.listeners_active == 0


@pytest.mark.asyncio
async def test_first_listen_package_approval_timeout_falls_through_to_live_audio():
    """Slow package I/O never consumes the instant-audio startup guarantee."""
    import threading

    from mammamiradio.web.streamer import _audio_generator

    app = _make_test_app()
    release_approval = threading.Event()

    def stalled_approval():
        release_approval.wait(timeout=1)
        return None

    mock_request = MagicMock()
    mock_request.app = app
    mock_request.is_disconnected = AsyncMock(return_value=False)

    try:
        with (
            patch("mammamiradio.web.streamer.first_listen_show_required", return_value=True),
            patch("mammamiradio.web.streamer.approved_first_listen_show_path", side_effect=stalled_approval),
            patch("mammamiradio.web.streamer.FIRST_LISTEN_SHOW_APPROVAL_TIMEOUT_SECONDS", 0.01),
        ):
            generator = _audio_generator(mock_request)
            live_chunk = asyncio.create_task(anext(generator))
            deadline = time.monotonic() + 0.5
            while app.state.station_state.listeners_active == 0:
                if time.monotonic() > deadline:
                    raise AssertionError("slow package approval did not fall through to the live hub")
                await asyncio.sleep(0)

            await app.state.stream_hub.broadcast(b"live-after-approval-timeout")
            assert await asyncio.wait_for(live_chunk, timeout=0.5) == b"live-after-approval-timeout"
            await generator.aclose()
    finally:
        release_approval.set()

    assert app.state.station_state.listeners_active == 0


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
    app.state.station_state.current_stream_audible = True

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
    app.state.station_state.current_stream_audible = True

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
    app.state.station_state.current_stream_audible = True

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.playlist.song_cues.detect_skip_bit", new=AsyncMock(return_value=True)):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/skip")

    assert resp.status_code == 200
    directive = app.state.station_state.ha_pending_directive
    assert "Brutta Canzone" in directive
    assert "saltato" in directive or "skippa" in directive


@pytest.mark.asyncio
async def test_stale_now_streaming_is_not_skip_ready_on_public_or_admin_status():
    """Selected metadata left after EOF cannot advertise or execute Skip."""
    app = _make_test_app()
    state = app.state.station_state
    state.now_streaming = {
        "type": "music",
        "label": "Already ended",
        "started": time.time() - 180,
        "metadata": {"title": "Already ended"},
    }
    state.current_stream_audible = False

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        public_status = await client.get("/public-status")
        admin_status = await client.get("/status")
        skip = await client.post("/api/skip")

    expected_actions = {"skip_ready": False, "skip_would_bridge": False}
    assert public_status.json()["playback_actions"] == expected_actions
    assert admin_status.json()["playback_actions"] == expected_actions
    assert skip.json()["ok"] is False
    error = skip.json()["error"].lower()
    assert "nothing is on air" in error
    # The station is running, so it must not point at Start — the admin hides
    # that button while running, which made the refusal read as broken.
    assert "press start" not in error
    assert not app.state.skip_event.is_set()
    assert state.force_next is None


@pytest.mark.asyncio
async def test_panic_does_not_cut_stale_now_streaming_even_with_ready_runway(tmp_path):
    """Panic must not treat selected-but-unheard metadata as an audible cut target."""
    app = _make_test_app()
    state = app.state.station_state
    state.now_streaming = {
        "type": "music",
        "label": "Already ended",
        "started": time.time() - 180,
        "metadata": {"title": "Already ended"},
    }
    state.current_stream_audible = False
    runway_path = tmp_path / "panic-ready-runway.mp3"
    runway_path.write_bytes(b"ready-runway")
    runway = Segment(
        type=SegmentType.MUSIC,
        path=runway_path,
        duration_sec=180.0,
        metadata={"queue_id": "panic-ready", "title": "Ready runway"},
        ephemeral=False,
    )
    app.state.queue.put_nowait(runway)
    state.queued_segments = [{"id": "panic-ready", "type": "music", "label": "Ready runway"}]

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer._continuity_reservation_segments", return_value=[]):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/panic")

    assert response.json() == {"ok": True, "purged": 0, "skipped": False}
    assert list(app.state.queue._queue) == [runway]
    assert not app.state.skip_event.is_set()
    assert state.force_next is SegmentType.MUSIC


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
        "accepted_listener_count",
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
    app.state.station_state.switch_playlist([], None)
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
    app.state.station_state.switch_playlist([], None)
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
        resp = await client.get("/api/setup/status", headers=ACTIVE_SETUP_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert "detected_mode" in body
    assert "essentials" in body
    assert "preflight_checks" in body
    assert "launch" in body
    assert "signature" in body


@pytest.mark.asyncio
async def test_setup_status_joins_background_first_listen_state_before_projecting():
    app = _make_test_app()
    app.state.first_listen_install_origin = FirstListenInstallOriginV1(FirstListenInstallOriginStatus.UNKNOWN)
    app.state.first_listen_receipt = None
    release = asyncio.Event()

    async def resolve_origin() -> None:
        await release.wait()
        app.state.first_listen_install_origin = FirstListenInstallOriginV1(FirstListenInstallOriginStatus.FRESH)

    async def resolve_receipt() -> None:
        await release.wait()
        app.state.first_listen_receipt = None

    app.state.first_listen_origin_task = asyncio.create_task(resolve_origin())
    app.state.first_listen_receipt_task = asyncio.create_task(resolve_receipt())
    release.set()

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/setup/status", headers=ACTIVE_SETUP_HEADERS)

    assert response.status_code == 200
    first_listen = response.json()["guided_setup"]["first_listen"]
    assert first_listen["bootstrap_ready"] is True
    assert first_listen["fresh_install"] is True
    assert first_listen["audio_complete"] is False
    assert response.json()["onboarding_required"] is True


@pytest.mark.asyncio
async def test_setup_status_and_recheck_share_projection():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        status = (await client.get("/api/setup/status", headers=ACTIVE_SETUP_HEADERS)).json()
        recheck = (await client.post("/api/setup/recheck", headers=ACTIVE_SETUP_HEADERS, json={})).json()

    assert recheck["signature"] == status["signature"]
    assert recheck["guided_setup"] == status["guided_setup"]
    assert recheck["recommended_next_action"] == status["recommended_next_action"]


@pytest.mark.asyncio
async def test_active_setup_recheck_requires_csrf_and_exact_empty_json_on_loopback():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        without_csrf = await client.post("/api/setup/recheck", json={})
        same_origin_only = await client.post("/api/setup/recheck", headers=SAME_ORIGIN, json={})
        missing_json = await client.post("/api/setup/recheck", headers=ACTIVE_SETUP_HEADERS)
        valid = await client.post("/api/setup/recheck", headers=ACTIVE_SETUP_HEADERS, json={})

    assert without_csrf.status_code == 403
    assert same_origin_only.status_code == 403
    assert missing_json.status_code == 422
    assert missing_json.json()["error"]["code"] == "invalid_request"
    assert valid.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/api/setup/recheck", {}),
        ("POST", "/api/setup/provider-check", {}),
        ("POST", "/api/setup/save-keys", {"ANTHROPIC_API_KEY": "attacker-key"}),
        (
            "PATCH",
            "/api/homeassistant/entity-policy",
            {"entity_id": "switch.coffee_machine", "muted": False},
        ),
    ],
)
async def test_active_setup_rejects_dns_rebinding_host_even_with_page_csrf_token(method, path, payload):
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    rebound_headers = {
        "Host": "attacker.example",
        "Origin": "http://attacker.example",
        "X-Radio-CSRF-Token": TEST_CSRF_TOKEN,
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://attacker.example") as client:
        response = await client.request(method, path, headers=rebound_headers, json=payload)

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_entity_policy_requires_and_accepts_dashboard_csrf_token(tmp_path):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    payload = {"entity_id": "switch.coffee_machine", "muted": True}

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        blocked = await client.patch("/api/homeassistant/entity-policy", json=payload)
        accepted = await client.patch(
            "/api/homeassistant/entity-policy",
            headers=ACTIVE_SETUP_HEADERS,
            json=payload,
        )

    assert blocked.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["muted"] is True


@pytest.mark.asyncio
async def test_setup_status_rejects_dns_rebinding_host_even_with_page_csrf_token():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    rebound_headers = {
        "Host": "attacker.example",
        "Origin": "http://attacker.example",
        "X-Radio-CSRF-Token": TEST_CSRF_TOKEN,
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://attacker.example") as client:
        response = await client.get("/api/setup/status", headers=rebound_headers)

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_active_setup_admin_token_allows_intentional_custom_hostname_automation():
    app = _make_test_app(admin_token="operator-token")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="https://radio.example") as client:
        response = await client.post(
            "/api/setup/recheck",
            headers={"X-Radio-Admin-Token": "operator-token"},
            json={},
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_setup_recovery_endpoints_remain_available_while_session_stopped():
    """Paused transport must not lock operators out of setup and diagnostics."""
    app = _make_test_app()
    app.state.station_state.session_stopped = True
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    with patch("mammamiradio.web.streamer._persist_and_apply_credentials", new=AsyncMock()) as persist:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            recheck = await client.post("/api/setup/recheck", json={})
            preview = await client.get("/api/homeassistant/context-candidates")
            save_keys = await client.post(
                "/api/setup/save-keys",
                headers=ACTIVE_SETUP_HEADERS,
                json={"ANTHROPIC_API_KEY": "sk-test"},
            )

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
    app.state.station_state.switch_playlist([], None)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.status_payload._has_any_mp3", side_effect=AssertionError("status must not scan")):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            status = (await client.get("/api/setup/status", headers=ACTIVE_SETUP_HEADERS)).json()
            app.state.station_state.source_readiness.mark_playable("local")
            recheck = (await client.post("/api/setup/recheck", headers=ACTIVE_SETUP_HEADERS, json={})).json()

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
async def test_status_buffered_audio_sec_sums_real_queue_durations(tmp_path):
    """buffered_audio_sec surfaces airtime ahead (seconds), not item count.

    Counts only immediately-playable audio (matching the producer runway
    governor), so the queued segments carry real files here.
    """
    app = _make_test_app()
    a = tmp_path / "a.mp3"
    a.write_bytes(b"a" * 1024)
    b = tmp_path / "b.mp3"
    b.write_bytes(b"b" * 1024)
    c = tmp_path / "c.mp3"
    c.write_bytes(b"c" * 1024)
    app.state.queue.put_nowait(Segment(type=SegmentType.MUSIC, path=a, duration_sec=180.0))
    app.state.queue.put_nowait(Segment(type=SegmentType.BANTER, path=b, duration_sec=12.5))
    app.state.queue.put_nowait(Segment(type=SegmentType.MUSIC, path=c))
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
async def test_status_buffered_audio_sec_respects_drift_guard(tmp_path):
    """The drift guard still trims stale shadow entries, but seconds come from the real queue."""
    app = _make_test_app()
    real_path = tmp_path / "real.mp3"
    real_path.write_bytes(b"x" * 1024)
    app.state.queue.put_nowait(Segment(type=SegmentType.MUSIC, path=real_path, duration_sec=180.0))
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
async def test_interrupt_supersedes_earlier_operator_trigger_without_stranding_guard():
    """Trigger -> interrupt releases the superseded one-at-a-time guard."""
    from mammamiradio.core.models import ChaosSubtype

    app = _make_test_app()
    state = app.state.station_state
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        trigger = await client.post("/api/trigger", json={"type": "banter"})
        trigger_revision = state.force_next_revision
        interrupt = await client.post(
            "/api/interrupt",
            json={"directive": "Announce the urgent safety warning.", "urgency": "urgent"},
        )

        assert trigger.json() == {"ok": True, "triggered": "banter"}
        assert interrupt.json()["ok"] is True
        assert state.chaos_pending is ChaosSubtype.URGENT_INTERRUPT
        assert state.force_next is SegmentType.BANTER
        assert state.force_next_revision > trigger_revision
        assert state.operator_force_pending is None

        retry = await client.post("/api/trigger", json={"type": "banter"})

    assert retry.json() == {"ok": True, "triggered": "banter"}
    assert state.operator_force_pending is SegmentType.BANTER


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
        resp = await client.post("/api/setup/recheck", headers=ACTIVE_SETUP_HEADERS, json={})
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
            resp = await client.post("/api/setup/provider-check", headers=ACTIVE_SETUP_HEADERS, json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body == probe_payload
    assert "anthropic-secret" not in resp.text
    assert "openai-secret" not in resp.text
    probe.assert_awaited_once_with(app.state.config)


@pytest.mark.asyncio
async def test_setup_provider_check_requires_exact_empty_json():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer.check_provider_keys", new=AsyncMock()) as probe:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            missing = await client.post("/api/setup/provider-check", headers=ACTIVE_SETUP_HEADERS)
            extra = await client.post(
                "/api/setup/provider-check",
                headers=ACTIVE_SETUP_HEADERS,
                json={"unexpected": True},
            )

    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "invalid_request"
    assert extra.status_code == 422
    assert extra.json()["error"]["code"] == "invalid_request"
    probe.assert_not_awaited()


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
            first = asyncio.create_task(client.post("/api/setup/provider-check", headers=ACTIVE_SETUP_HEADERS, json={}))
            await started.wait()
            second = asyncio.create_task(
                client.post("/api/setup/provider-check", headers=ACTIVE_SETUP_HEADERS, json={})
            )
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
            first = await client.post("/api/setup/provider-check", headers=ACTIVE_SETUP_HEADERS, json={})
            second = await client.post("/api/setup/provider-check", headers=ACTIVE_SETUP_HEADERS, json={})

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
            resp = await client.post("/api/setup/provider-check", headers=ACTIVE_SETUP_HEADERS, json={})

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
            check_coro = client.post("/api/setup/provider-check", headers=ACTIVE_SETUP_HEADERS, json={})
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
                    headers=ACTIVE_SETUP_HEADERS,
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
            from mammamiradio.web import persistence

            save_addon_options.return_value = persistence._SECRET_WRITE_DURABLE
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post(
                    "/api/setup/save-keys",
                    headers=ACTIVE_SETUP_HEADERS,
                    json={"ANTHROPIC_API_KEY": "sk-addon"},
                )

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
async def test_setup_save_keys_reports_structured_500_on_addon_persistence_failure():
    """An unconfirmed/failed add-on credential save must not silently 200."""
    app = _make_test_app(is_addon=True)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    with patch("mammamiradio.web.streamer._save_addon_options") as save_addon_options:
        from mammamiradio.web import persistence

        save_addon_options.side_effect = persistence._AddonPersistenceError("Unable to persist add-on credentials")
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/api/setup/save-keys",
                headers=ACTIVE_SETUP_HEADERS,
                json={"ANTHROPIC_API_KEY": "sk-addon"},
            )

    assert resp.status_code == 500
    body = resp.json()
    assert body["ok"] is False
    assert "failed to save credentials" in body["error"]


@pytest.mark.asyncio
async def test_setup_save_keys_rejects_empty_payload():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/setup/save-keys", headers=ACTIVE_SETUP_HEADERS, json={})

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
async def test_audio_provider_current_reason_is_independent_from_switch_history():
    app = _make_test_app()
    state = app.state.station_state
    state.playlist_source = PlaylistSource(kind="charts", label="Charts")
    state.now_streaming = {
        "type": "music",
        "label": "Cache Artist – Current",
        "started": time.time(),
        "metadata": {
            "audio_source": "norm_cache",
            "fallback": True,
            "fallback_reason": "Serving the reserved cache runway",
        },
    }
    state.current_stream_audible = True
    state.update_runtime_provider(
        "audio_source",
        current_provider="norm_cache",
        primary_provider="charts",
        fallback_active=True,
        reason="Chart download failed",
        timestamp=10.0,
    )
    state.update_runtime_provider(
        "audio_source",
        current_provider="norm_cache",
        primary_provider="charts",
        fallback_active=True,
        reason="Cache remains healthy",
        timestamp=20.0,
    )

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/status")

    provider = response.json()["runtime_status"]["providers"]["audio_source"]
    assert response.json()["runtime_status"]["station_on_air"] is True
    assert provider["current_provider"] == "norm_cache"
    assert provider["current_reason"] == "Serving the reserved cache runway"
    assert provider["switch_reason"] == "Chart download failed"
    assert provider["last_switch_timestamp"] == 10.0


@pytest.mark.asyncio
async def test_script_provider_switch_reason_never_shows_a_raw_code():
    """A stored provider code must reach the operator as words, not snake_case."""

    app = _make_test_app()
    state = app.state.station_state
    app.state.config.anthropic_api_key = "anthropic-key"
    app.state.config.openai_api_key = "openai-key"
    state.update_runtime_provider(
        "script_provider",
        current_provider="openai",
        primary_provider="anthropic",
        fallback_active=True,
        reason="anthropic_auth_failed",
        timestamp=10.0,
    )

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        provider = (await client.get("/status")).json()["runtime_status"]["providers"]["script_provider"]

    assert provider["last_switch_timestamp"] == 10.0
    assert provider["switch_reason"] == "Anthropic API key rejected - check your key in Engine Room"
    assert "anthropic_auth_failed" not in provider["switch_reason"]
    assert "_" not in provider["switch_reason"]
    assert "_" not in provider["current_reason"]


@pytest.mark.asyncio
async def test_script_provider_event_keeps_raw_diagnostic_but_shows_plain_reason():
    app = _make_test_app()
    state = app.state.station_state
    state.update_runtime_provider(
        "script_provider",
        current_provider="openai",
        primary_provider="anthropic",
        fallback_active=True,
        reason="anthropic_auth_failed",
        timestamp=10.0,
    )

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        event = (await client.get("/status")).json()["runtime_status"]["recent_events"][0]

    assert event["provider_class"] == "script_provider"
    assert event["diagnostic_reason"] == "anthropic_auth_failed"
    assert event["reason"] == "Anthropic API key rejected - check your key in Engine Room"
    assert "_" not in event["reason"]


@pytest.mark.asyncio
async def test_legacy_fallback_norm_cache_reaches_the_admin_as_norm_cache():
    """The rescue cache has one identifier. "Serving as fallback" is separate metadata.

    Older state and older segment metadata can still carry the retired
    `fallback_norm_cache` value, on air and while paused alike.
    """

    app = _make_test_app()
    state = app.state.station_state
    state.playlist_source = PlaylistSource(kind="charts", label="Charts")
    state.now_streaming = {
        "type": "music",
        "label": "Cache Artist – Rescued",
        "started": time.time(),
        "metadata": {
            "audio_source": "fallback_norm_cache",
            "fallback": True,
            "fallback_reason": "Serving the reserved cache runway",
        },
    }
    state.current_stream_audible = True
    state.update_runtime_provider(
        "audio_source",
        current_provider="fallback_norm_cache",
        primary_provider="charts",
        fallback_active=True,
        reason="Chart download failed",
        timestamp=10.0,
    )

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        on_air = (await client.get("/status")).json()["runtime_status"]
        state.session_stopped = True
        state.current_stream_audible = False
        paused = (await client.get("/status")).json()["runtime_status"]

    for snapshot in (on_air, paused):
        provider = snapshot["providers"]["audio_source"]
        assert provider["current_provider"] == "norm_cache"
        assert provider["fallback_active"] is True
        assert "fallback_norm_cache" not in provider["current_label"]


@pytest.mark.asyncio
async def test_admin_provider_rows_ignore_newer_unheard_generation_observations():
    app = _make_test_app()
    state = app.state.station_state
    app.state.config.anthropic_api_key = "anthropic-key"
    app.state.config.openai_api_key = "openai-key"
    app.state.config.azure_speech_key = "azure-key"
    app.state.config.azure_speech_region = "westeurope"
    for host in app.state.config.hosts:
        host.engine = "azure"

    script_observation = state.observe_runtime_provider(
        "script_provider",
        current_provider="openai",
        primary_provider="anthropic",
        fallback_active=True,
        reason="anthropic_exception",
        timestamp=10.0,
    )
    tts_observation = state.observe_runtime_provider(
        "tts_provider",
        current_provider="edge",
        primary_provider="azure",
        fallback_active=True,
        reason="missing_credentials",
        timestamp=10.0,
    )
    segment = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/audible-provider-segment.mp3"),
        duration_sec=5.0,
        metadata={"title": "Audible provider segment", "audio_source": "charts"},
        runtime_provider_observations={
            "script_provider": script_observation,
            "tts_provider": tts_observation,
        },
    )
    state.on_stream_segment_selected(segment)
    assert state.on_stream_segment_audible(segment) is True

    # The producer is already rendering the next segment on recovered primary
    # routes. Those observations are not listener truth for the current segment.
    state.observe_runtime_provider(
        "script_provider",
        current_provider="anthropic",
        primary_provider="anthropic",
        fallback_active=False,
        reason="primary recovered",
        timestamp=20.0,
    )
    state.observe_runtime_provider(
        "tts_provider",
        current_provider="azure",
        primary_provider="azure",
        fallback_active=False,
        reason="primary_success",
        timestamp=20.0,
    )

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        on_air = (await client.get("/status")).json()["runtime_status"]
        state.session_stopped = True
        state.current_stream_audible = False
        paused = (await client.get("/status")).json()["runtime_status"]

    assert on_air["station_on_air"] is True
    assert on_air["providers"]["script_provider"]["current_provider"] == "openai"
    assert "brief api error" in on_air["providers"]["script_provider"]["current_reason"].lower()
    assert on_air["providers"]["tts_provider"]["current_provider"] == "edge"
    assert "cloud voice key is missing" in on_air["providers"]["tts_provider"]["current_reason"].lower()

    assert paused["station_on_air"] is False
    assert paused["providers"]["script_provider"]["current_provider"] == "openai"
    assert paused["providers"]["tts_provider"]["current_provider"] == "edge"
    assert "last listener-audible provider; station is paused" in (
        paused["providers"]["script_provider"]["current_reason"].lower()
    )
    assert "last listener-audible provider; station is paused" in (
        paused["providers"]["tts_provider"]["current_reason"].lower()
    )


@pytest.mark.asyncio
async def test_paused_provider_status_exposes_newer_action_required_observation():
    app = _make_test_app()
    state = app.state.station_state
    app.state.config.anthropic_api_key = "anthropic-key"
    app.state.config.openai_api_key = "openai-key"
    audible = state.observe_runtime_provider(
        "script_provider",
        current_provider="anthropic",
        primary_provider="anthropic",
        fallback_active=False,
        reason="primary_success",
    )
    segment = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/audible-script-provider.mp3"),
        metadata={"title": "Primary render"},
        runtime_provider_observations={"script_provider": audible},
    )
    state.on_stream_segment_selected(segment)
    assert state.on_stream_segment_audible(segment) is True
    state.observe_runtime_provider(
        "script_provider",
        current_provider="openai",
        primary_provider="anthropic",
        fallback_active=True,
        reason="anthropic_auth_failed",
    )
    state.anthropic_disabled_until = time.time() + 120
    state.session_stopped = True
    state.current_stream_audible = False

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        provider = (await client.get("/status")).json()["runtime_status"]["providers"]["script_provider"]

    assert provider["current_provider"] == "anthropic"
    assert provider["fallback_active"] is False
    observed = provider["latest_observation"]
    assert observed["current_provider"] == "openai"
    assert observed["fallback_active"] is True
    assert observed["recovery_mode"] == "circuit_breaker"
    assert observed["action_guidance"] == "Anthropic API key rejected - check your key in Engine Room"
    assert observed["current_reason"] == "Anthropic API key rejected - check your key in Engine Room"


@pytest.mark.asyncio
async def test_same_provider_unheard_reason_is_kept_separate_from_audible_truth():
    app = _make_test_app()
    state = app.state.station_state
    app.state.config.anthropic_api_key = "anthropic-key"
    app.state.config.openai_api_key = "openai-key"
    audible = state.observe_runtime_provider(
        "script_provider",
        current_provider="openai",
        primary_provider="anthropic",
        fallback_active=True,
        reason="anthropic_transient",
    )
    segment = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/audible-openai-fallback.mp3"),
        metadata={"title": "Audible OpenAI fallback"},
        runtime_provider_observations={"script_provider": audible},
    )
    state.on_stream_segment_selected(segment)
    assert state.on_stream_segment_audible(segment) is True

    state.observe_runtime_provider(
        "script_provider",
        current_provider="openai",
        primary_provider="anthropic",
        fallback_active=True,
        reason="anthropic_auth_failed",
    )
    state.anthropic_disabled_until = time.time() + 120

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        provider = (await client.get("/status")).json()["runtime_status"]["providers"]["script_provider"]

    assert provider["current_provider"] == "openai"
    assert "overloaded" in provider["current_reason"].lower()
    assert provider["recovery_mode"] is None
    observed = provider["latest_observation"]
    assert observed["current_provider"] == "openai"
    assert observed["recovery_mode"] == "circuit_breaker"
    assert observed["current_reason"] == "Anthropic API key rejected - check your key in Engine Room"
    assert observed["action_guidance"] == "Anthropic API key rejected - check your key in Engine Room"


@pytest.mark.asyncio
async def test_runtime_status_keeps_on_air_hysteresis_only_for_recent_listener_audio():
    app = _make_test_app()
    state = app.state.station_state
    state.listeners_active = 1
    state.current_stream_audible = False
    state.last_air_monotonic = 100.0
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        with patch("mammamiradio.web.streamer._runtime_monotonic", return_value=102.0):
            recent = (await client.get("/status")).json()["runtime_status"]
        with patch(
            "mammamiradio.web.streamer._runtime_monotonic",
            return_value=103.1,
        ):
            expired = (await client.get("/status")).json()["runtime_status"]
        state.session_stopped = True
        with patch("mammamiradio.web.streamer._runtime_monotonic", return_value=102.0):
            stopped = (await client.get("/status")).json()["runtime_status"]
        state.session_stopped = False
        state.queue_empty_since = 50.0
        state.last_air_monotonic = 50.0
        with patch("mammamiradio.web.streamer._runtime_monotonic", return_value=100.0):
            silent = (await client.get("/status")).json()["runtime_status"]

    assert recent["station_on_air"] is True
    assert expired["station_on_air"] is False
    assert stopped["station_on_air"] is False
    assert silent["station_on_air"] is False


@pytest.mark.asyncio
async def test_runtime_status_handoff_keeps_last_audible_provider_until_new_audio_is_accepted():
    """A selected next source cannot replace provider truth during handoff grace."""
    app = _make_test_app()
    state = app.state.station_state
    state.listeners_active = 1
    state.playlist_source = PlaylistSource(kind="charts", label="Charts")
    state.update_runtime_provider(
        "audio_source",
        current_provider="charts",
        primary_provider="charts",
        fallback_active=False,
        reason="Primary audio source is on air",
        timestamp=10.0,
    )

    # Selection has advanced to Local, but no listener accepted those bytes.
    state.playlist_source = PlaylistSource(kind="local", label="Local")
    state.now_streaming = {
        "type": "music",
        "label": "Selected but unheard Local song",
        "started": time.time(),
        "metadata": {"audio_source": "local", "title": "Selected but unheard"},
    }
    state.current_stream_audible = False
    state.last_air_monotonic = 100.0

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer._runtime_monotonic", return_value=102.0):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            runtime_status = (await client.get("/status")).json()["runtime_status"]

    provider = runtime_status["providers"]["audio_source"]
    assert runtime_status["station_on_air"] is True
    assert provider["current_provider"] == "charts"
    assert provider["primary_provider"] == "charts"
    assert "handoff is in progress" in provider["current_reason"].lower()


@pytest.mark.asyncio
async def test_skip_with_admin_auth():
    app = _make_test_app(admin_password="secret123")
    # Put something in now_streaming so skip has something to act on
    app.state.station_state.now_streaming = {"type": "music", "label": "Test", "started": time.time()}
    app.state.station_state.current_stream_audible = True
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/skip", auth=("admin", "secret123"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True


@pytest.mark.asyncio
async def test_stop_and_resume_toggle_session_state():
    app = _make_test_app()
    state = app.state.station_state
    state.now_streaming = {"type": "music", "label": "Test", "started": time.time()}
    state._last_audible_stream = dict(state.now_streaming)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        stop = await client.post("/api/stop")
        assert stop.status_code == 200
        assert state.session_stopped is True
        assert state.now_streaming["type"] == "stopped"
        assert state._last_audible_stream == {}

        resume = await client.post("/api/resume")
        assert resume.status_code == 200
        assert state.session_stopped is False
        assert state.now_streaming == {}


@pytest.mark.asyncio
async def test_stop_persistence_failure_is_total_live_state_noop(tmp_path):
    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    audio_path = tmp_path / "queued.mp3"
    audio_path.write_bytes(b"queued")
    queued = Segment(
        type=SegmentType.MUSIC,
        path=audio_path,
        duration_sec=10.0,
        metadata={"queue_id": "queued-1", "title": "Queued"},
    )
    app.state.queue.put_nowait(queued)
    state.queued_segments = [{"id": "queued-1", "label": "Queued"}]
    state.now_streaming = {"type": "music", "label": "Live", "started": time.time(), "metadata": {}}
    state._last_audible_stream = dict(state.now_streaming)
    state.continuity_slot = queued
    state.continuity_epoch = 9
    app.state.last_shareworthy_clip = {"bytes": b"clip"}

    with patch("mammamiradio.web.streamer._persist_session_stopped", side_effect=OSError("disk full")):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/stop")

    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "error": "Couldn't save the stopped state. Nothing changed; try again.",
    }
    assert state.session_stopped is False
    assert state.now_streaming["label"] == "Live"
    assert state._last_audible_stream["label"] == "Live"
    assert state.continuity_epoch == 9
    assert state.continuity_slot is queued
    assert list(app.state.queue._queue) == [queued]
    assert state.queued_segments == [{"id": "queued-1", "label": "Queued"}]
    assert not app.state.skip_event.is_set()
    assert app.state.last_shareworthy_clip == {"bytes": b"clip"}


@pytest.mark.asyncio
@pytest.mark.parametrize("sentinel_type", ["stopped", "skipping"])
async def test_stop_never_treats_transport_sentinel_as_real_media(tmp_path, sentinel_type):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.station_state.now_streaming = {
        "type": sentinel_type,
        "label": sentinel_type.title(),
        "started": time.time(),
        "metadata": {},
    }

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/stop")

    assert response.status_code == 200
    assert not app.state.skip_event.is_set()


@pytest.mark.asyncio
async def test_resume_warm_cache_reservation_is_synchronous_and_probe_free(tmp_path):
    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    state.session_stopped = True
    state.now_streaming = {"type": "stopped", "label": "Session stopped", "metadata": {}}
    (tmp_path / "session_stopped.flag").touch()
    cached = tmp_path / "norm_warm_song_192k.mp3"
    cached.write_bytes(b"warm-cache-audio" * 1024)
    (tmp_path / "norm_warm_song_192k.mp3.json").write_text(
        '{"title":"Warm Song","artist":"Cache Artist","duration_ms":180000}'
    )
    state.immediate_audio_index[cached] = 180.0

    with (
        patch("mammamiradio.web.streamer.probe_duration_sec") as probe,
        patch("mammamiradio.web.streamer._packaged_recovery_segment", new=AsyncMock()) as packaged_probe_path,
    ):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/resume")

    assert response.status_code == 200
    assert state.session_stopped is False
    assert not (tmp_path / "session_stopped.flag").exists()
    runway = list(app.state.queue._queue)
    assert len(runway) == 1
    assert runway[0].path == cached
    assert runway[0].metadata["audio_source"] == "norm_cache"
    probe.assert_not_called()
    packaged_probe_path.assert_not_called()


@pytest.mark.asyncio
async def test_resume_cold_cache_reserves_packaged_continuity_without_probe(tmp_path):
    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    state.session_stopped = True
    state.now_streaming = {"type": "stopped", "label": "Session stopped", "metadata": {}}
    (tmp_path / "session_stopped.flag").touch()

    with patch("mammamiradio.web.streamer.probe_duration_sec") as probe:
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/resume")

    assert response.status_code == 200
    runway = list(app.state.queue._queue)
    assert len(runway) == 1
    assert runway[0].path == _DEMO_ASSETS_DIR / "recovery" / "continuity_1.mp3"
    assert runway[0].metadata["continuity_reservation"] is True
    probe.assert_not_called()


@pytest.mark.asyncio
async def test_resume_without_any_immediate_audio_fails_closed(tmp_path):
    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    state.session_stopped = True
    state.now_streaming = {"type": "stopped", "label": "Session stopped", "metadata": {}}
    marker = tmp_path / "session_stopped.flag"
    marker.touch()

    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/resume")

    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "error": (
            "No recovery audio is installed. Restore the packaged recovery assets, "
            "or confirm Force Start to rebuild the station with host audio."
        ),
        "force_available": True,
    }
    assert state.session_stopped is True
    assert state.now_streaming["type"] == "stopped"
    assert marker.exists()
    assert app.state.queue.empty()
    assert state.continuity_slot is None
    assert not state.resume_event.is_set()


@pytest.mark.asyncio
async def test_first_listen_resume_refuses_without_playable_runway(tmp_path):
    """The cast resume mirrors the admin Start gate: no runway, no resume."""
    from mammamiradio.web.streamer import _resume_station

    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    state.session_stopped = True
    marker = tmp_path / "session_stopped.flag"
    marker.touch()

    with (
        patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"),
        pytest.raises(RuntimeError, match="no immediately playable runway"),
    ):
        await _resume_station(app.state)

    assert state.session_stopped is True
    assert marker.exists()
    assert not state.resume_event.is_set()


@pytest.mark.asyncio
async def test_first_listen_resume_with_warm_runway_clears_stop_and_signals(tmp_path):
    """A stopped station with cached audio resumes: marker gone, event set."""
    from mammamiradio.web.streamer import _resume_station

    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    state.session_stopped = True
    marker = tmp_path / "session_stopped.flag"
    marker.touch()
    cached = tmp_path / "norm_warm_song_192k.mp3"
    cached.write_bytes(b"warm-cache-audio" * 1024)
    (tmp_path / "norm_warm_song_192k.mp3.json").write_text(
        '{"title":"Warm Song","artist":"Cache Artist","duration_ms":180000}'
    )
    state.immediate_audio_index[cached] = 180.0

    with patch("mammamiradio.web.streamer.probe_duration_sec") as probe:
        await _resume_station(app.state)

    assert state.session_stopped is False
    assert not marker.exists()
    assert state.resume_event.is_set()
    assert state.force_recovery_active is False
    probe.assert_not_called()


@pytest.mark.asyncio
async def test_first_listen_resume_restores_stop_marker_when_control_changes_mid_write(tmp_path):
    """A Stop landing during the off-loop marker write owns the newer epoch."""
    from mammamiradio.web.streamer import _resume_station

    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    state.session_stopped = True
    cached = tmp_path / "norm_warm_song_192k.mp3"
    cached.write_bytes(b"warm-cache-audio" * 1024)
    (tmp_path / "norm_warm_song_192k.mp3.json").write_text(
        '{"title":"Warm Song","artist":"Cache Artist","duration_ms":180000}'
    )
    state.immediate_audio_index[cached] = 180.0
    persist_calls: list[bool] = []

    def persist(config, stopped):
        persist_calls.append(stopped)
        if not stopped:
            state.continuity_epoch += 1

    with (
        patch("mammamiradio.web.streamer.probe_duration_sec"),
        patch("mammamiradio.web.streamer._persist_session_stopped", side_effect=persist),
        pytest.raises(RuntimeError, match="station control changed"),
    ):
        await _resume_station(app.state)

    assert persist_calls == [False, True]
    assert state.session_stopped is True
    assert not state.resume_event.is_set()


@pytest.mark.asyncio
async def test_ha_playback_service_wiring_uses_real_resume_and_receipt_closures(tmp_path):
    """The service built for routes must drive the real station state and store."""
    from mammamiradio.web.streamer import _ha_playback_service

    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    service = _ha_playback_service(app.state)

    state.session_stopped = False
    await service._resume_station()
    assert state.session_stopped is False

    attempt_id = await service._persist_accepted_attempt("media_player.kitchen")

    assert attempt_id
    receipt = app.state.first_listen_receipt
    assert receipt is not None
    assert receipt.selected_entity_id == "media_player.kitchen"
    assert receipt.accepted_attempt_id == attempt_id


@pytest.mark.asyncio
async def test_force_resume_without_assets_arms_recovery_after_marker_commit(tmp_path):
    app = _make_test_app()
    app.state.start_time = time.time() - 31
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    state.session_stopped = True
    state.now_streaming = {"type": "stopped", "label": "Session stopped", "metadata": {}}
    marker = tmp_path / "session_stopped.flag"
    marker.touch()

    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/resume?force=true")
            readiness = await client.get("/readyz")
            runtime = (await client.get("/status")).json()["runtime_status"]
            assert not marker.exists()
            assert state.session_stopped is False
            assert state.now_streaming == {}
            assert state.force_next is SegmentType.BANTER
            assert state.force_recovery_active is True
            assert state.resume_event.is_set()
            state.on_stream_segment(
                Segment(
                    type=SegmentType.BANTER,
                    path=tmp_path / "accepted-recovery.mp3",
                    metadata={"title": "Accepted recovery"},
                )
            )
            recovered_readiness = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "recovering": True,
        "runway_source": "none",
    }
    assert readiness.status_code == 503
    assert readiness.json()["status"] == "starting"
    assert runtime["recovering"] is True
    assert runtime["health_state"] == "degraded"
    assert runtime["station_on_air"] is False
    assert state.force_recovery_active is False
    assert recovered_readiness.status_code == 200
    assert recovered_readiness.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_force_resume_marker_failure_is_total_live_state_noop(tmp_path):
    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    state.session_stopped = True
    state.now_streaming = {"type": "stopped", "label": "Session stopped", "metadata": {}}
    state.continuity_epoch = 9
    state.force_next = SegmentType.AD
    marker = tmp_path / "session_stopped.flag"
    marker.touch()

    with (
        patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"),
        patch("mammamiradio.web.streamer._persist_session_stopped", side_effect=OSError("read only")),
    ):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/resume?force=true")

    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "error": "Couldn't save the running state. The station is still paused; try again.",
    }
    assert marker.exists()
    assert state.session_stopped is True
    assert state.now_streaming["type"] == "stopped"
    assert state.continuity_epoch == 9
    assert state.force_next is SegmentType.AD
    assert state.force_recovery_active is False
    assert not state.resume_event.is_set()
    assert app.state.queue.empty()


@pytest.mark.asyncio
async def test_resume_clears_a_dead_queue_head_instead_of_refusing_forever(tmp_path):
    """A dead head in front of ready audio must not brick Start.

    The reservation counts ready seconds across every protected segment, while
    the fail-closed gate reads only the head — the loop's next pull. When a
    reserved head's cache file goes away (LRU prune, cache clear, SD read error)
    the reservation is satisfied and the gate is not, so every retry replays the
    same 503 and the operator's only remedy is an add-on restart.
    """

    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    state.session_stopped = True
    state.now_streaming = {"type": "stopped", "label": "Session stopped", "metadata": {}}
    marker = tmp_path / "session_stopped.flag"
    marker.touch()

    live = tmp_path / "reserved-live.mp3"
    live.write_bytes(b"ID3reserved-live-audio")
    evicted = tmp_path / "reserved-evicted.mp3"  # never created: the file is gone

    def _reserved(path: Path, title: str) -> Segment:
        return Segment(
            type=SegmentType.MUSIC,
            path=path,
            duration_sec=200.0,
            metadata={
                "title": title,
                "continuity_reservation": True,
                "continuity_admission_epoch": state.continuity_epoch,
            },
        )

    app.state.queue.put_nowait(_reserved(evicted, "Gone"))
    app.state.queue.put_nowait(_reserved(live, "Still here"))

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/resume")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "recovering": False}
    assert state.session_stopped is False
    assert not marker.exists()
    assert state.resume_event.is_set()
    # The dead head is gone and the playable tail is what the loop will pull.
    heads = [segment.path for segment in list(app.state.queue._queue)]
    assert evicted not in heads
    assert heads[:1] == [live]


@pytest.mark.asyncio
async def test_resume_marker_failure_parks_runway_for_retry(tmp_path):
    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    state.session_stopped = True
    state.now_streaming = {"type": "stopped", "label": "Session stopped", "metadata": {}}
    marker = tmp_path / "session_stopped.flag"
    marker.touch()

    with patch("mammamiradio.web.streamer._persist_session_stopped", side_effect=OSError("read only")):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            failed = await client.post("/api/resume")

    assert failed.status_code == 503
    assert failed.json()["ok"] is False
    assert state.session_stopped is True
    assert state.now_streaming["type"] == "stopped"
    assert marker.exists()
    assert app.state.queue.qsize() == len(state.queued_segments) == 1
    parked = next(iter(app.state.queue._queue))
    parked_epoch = state.continuity_epoch
    assert not state.resume_event.is_set()

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        retried = await client.post("/api/resume")

    assert retried.status_code == 200
    assert state.session_stopped is False
    assert list(app.state.queue._queue) == [parked]
    assert state.continuity_epoch == parked_epoch


@pytest.mark.asyncio
async def test_resume_while_running_marker_failure_leaves_playback_untouched(tmp_path):
    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    state.now_streaming = {"type": "music", "label": "Live", "started": time.time(), "metadata": {}}
    audio_path = tmp_path / "next.mp3"
    audio_path.write_bytes(b"next")
    queued = Segment(type=SegmentType.MUSIC, path=audio_path, metadata={"title": "Next"})
    app.state.queue.put_nowait(queued)
    state.continuity_epoch = 7
    before_change = state.last_state_change_at

    with patch("mammamiradio.web.streamer._persist_session_stopped", side_effect=OSError("read only")):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/resume")

    assert response.status_code == 503
    assert state.session_stopped is False
    assert state.now_streaming["label"] == "Live"
    assert list(app.state.queue._queue) == [queued]
    assert state.continuity_epoch == 7
    assert state.last_state_change_at == before_change
    assert not state.resume_event.is_set()


@pytest.mark.asyncio
async def test_every_accepted_stop_advances_the_continuity_epoch(tmp_path):
    """The epoch is the fence that defeats a Stop->Resume ABA race.

    It must advance on each accepted Stop regardless of what is on air, and it
    must not move when the Stop was refused — otherwise work captured under the
    old epoch would be discarded even though nothing was ever stopped.
    """

    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    state.continuity_epoch = 5
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Idle station: nothing on air, no queue, nothing to purge.
        assert (await client.post("/api/stop")).status_code == 200
        assert state.continuity_epoch == 6

        # Already stopped: still an accepted Stop, still a new fence.
        assert (await client.post("/api/stop")).status_code == 200
        assert state.continuity_epoch == 7

        # Real media on air.
        state.now_streaming = {"type": "music", "label": "Live", "started": time.time(), "metadata": {}}
        assert (await client.post("/api/stop")).status_code == 200
        assert state.continuity_epoch == 8

        # A refused Stop changed nothing, so the fence must not move either.
        with patch("mammamiradio.web.streamer._persist_session_stopped", side_effect=OSError("disk full")):
            assert (await client.post("/api/stop")).status_code == 503
        assert state.continuity_epoch == 8


@pytest.mark.asyncio
async def test_repeated_stop_and_resume_are_idempotent_without_duplicate_runway(tmp_path):
    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    state.now_streaming = {"type": "music", "label": "Live", "started": time.time(), "metadata": {}}
    state.continuity_epoch = 20
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.post("/api/stop")).status_code == 200
        first_stop_epoch = state.continuity_epoch
        assert (await client.post("/api/stop")).status_code == 200
        second_stop_epoch = state.continuity_epoch
        assert second_stop_epoch == first_stop_epoch + 1

        assert (await client.post("/api/resume")).status_code == 200
        runway = list(app.state.queue._queue)
        resumed_epoch = state.continuity_epoch
        assert len(runway) == 1

        assert (await client.post("/api/resume")).status_code == 200

    assert state.session_stopped is False
    assert list(app.state.queue._queue) == runway
    assert state.continuity_epoch == resumed_epoch


@pytest.mark.asyncio
async def test_successful_resume_delivers_listener_bytes_within_two_seconds(tmp_path):
    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    state.session_stopped = True
    state.now_streaming = {"type": "stopped", "label": "Session stopped", "metadata": {}}
    (tmp_path / "session_stopped.flag").touch()
    _listener_id, listener_queue = app.state.stream_hub.subscribe()
    playback = asyncio.create_task(run_playback_loop(app))

    try:
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/resume")
            assert response.status_code == 200
            chunk = await asyncio.wait_for(listener_queue.get(), timeout=2.0)
            # Sampled while the segment is still on air: the loop clears this on
            # the way out of a segment, including when it unwinds on cancel.
            audible_while_airing = state.current_stream_audible
    finally:
        playback.cancel()
        await asyncio.gather(playback, return_exceptions=True)

    assert isinstance(chunk, bytes)
    assert chunk
    assert state.last_air_monotonic is not None
    # A selected-but-never-heard segment also produces bytes on the wire, so the
    # assertions above cannot tell a real audible commit from a bare selection.
    assert audible_while_airing is True
    assert state.audible_playback_epoch == state.playback_epoch
    # Resume's persistence and live state both committed. `resume_event` is not
    # checked here: the playback loop clears it on wake, which is the proof the
    # chunk above already gives.
    assert not (tmp_path / "session_stopped.flag").exists()
    assert state.session_stopped is False


@pytest.mark.asyncio
async def test_stop_and_resume_move_the_marker_on_disk(tmp_path):
    """Persistence is the session boundary — assert the boundary, not just the flag.

    Every other Stop/Resume test reads in-memory state or the 503 paths. A
    `_persist_session_stopped` that silently no-op'd would pass all of them, and
    an HA watchdog restart would then resurrect a station the operator stopped.
    """

    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    marker = tmp_path / "session_stopped.flag"
    state.now_streaming = {"type": "music", "label": "Live", "started": time.time(), "metadata": {}}
    assert not marker.exists()

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.post("/api/stop")).status_code == 200
        assert marker.exists(), "Stop must persist before it mutates live state"

        # Scenario 3: the marker survives from a prior run. A second Stop is
        # idempotent on disk and still advances the fence.
        epoch_before = state.continuity_epoch
        assert (await client.post("/api/stop")).status_code == 200
        assert marker.exists()
        assert state.continuity_epoch == epoch_before + 1

        assert (await client.post("/api/resume")).status_code == 200
        assert not marker.exists(), "Resume must remove the marker before publishing running state"


@pytest.mark.asyncio
async def test_assetless_running_station_synthesizes_through_producer_queue_playback_listener(tmp_path):
    """A running station may recover asynchronously even though Resume cannot."""
    from mammamiradio.audio.tts import TTSUnavailableError
    from mammamiradio.scheduling.producer import run_producer

    app = _make_test_app()
    state = app.state.station_state
    config = app.state.config
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    state.playlist.clear()
    state.force_next = SegmentType.BANTER
    _listener_id, listener_queue = app.state.stream_hub.subscribe()
    missing_assets = tmp_path / "missing-demo-assets"
    synthesized = tmp_path / "recovery_sweeper.mp3"
    render_calls = 0
    audible_committed = False

    async def _render_recovery_sweeper(*_args, **_kwargs):
        nonlocal render_calls
        render_calls += 1
        if render_calls > 1:
            await asyncio.Event().wait()
        synthesized.write_bytes(b"assetless synthesized recovery" * 512)
        return synthesized

    with (
        patch("mammamiradio.scheduling.producer._DEMO_ASSETS_DIR", missing_assets),
        patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", missing_assets),
        patch("mammamiradio.web.streamer._ASSETS_DIR", missing_assets),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=None),
        patch("mammamiradio.scheduling.producer.select_norm_cache_rescue", return_value=None),
        patch("mammamiradio.scheduling.producer._blocklist_safe_last_music", return_value=None),
        patch(
            "mammamiradio.scheduling.producer._synthesize_impossible_moment",
            new=AsyncMock(side_effect=TTSUnavailableError("no immediate provider")),
        ),
        patch(
            "mammamiradio.scheduling.producer._render_sweeper_audio",
            new=AsyncMock(side_effect=_render_recovery_sweeper),
        ),
        patch("mammamiradio.scheduling.producer.validate_segment_audio"),
        patch("mammamiradio.scheduling.producer._probe_segment_duration", return_value=1.0),
    ):
        producer_task = asyncio.create_task(run_producer(app.state.queue, state, config, app.state.skip_event))
        playback_task = asyncio.create_task(run_playback_loop(app))
        try:
            chunk = await asyncio.wait_for(listener_queue.get(), timeout=2.0)
            deadline = time.monotonic() + 1.0
            while not state.current_stream_audible:
                if time.monotonic() > deadline:
                    raise AssertionError("assetless recovery never committed listener-audible state")
                await asyncio.sleep(0)
            audible_committed = True
        finally:
            producer_task.cancel()
            playback_task.cancel()
            await asyncio.gather(producer_task, playback_task, return_exceptions=True)

    assert chunk
    assert synthesized.exists() is False, "ephemeral synthesized recovery should be cleaned after playback"
    assert audible_committed is True
    assert state.last_air_monotonic is not None
    assert any(entry.metadata.get("error_recovery") for entry in state.stream_log)
    assert state.stream_outcome_history[-1]["accepted_listener_count"] >= 1


@pytest.mark.asyncio
async def test_slow_source_load_crossing_stop_commits_filtered_metadata_only(tmp_path):
    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    app.state.source_switch_lock = asyncio.Lock()
    state.now_streaming = {"type": "music", "label": "Live", "started": time.time(), "metadata": {}}
    state.blocklist[("blocked artist", "blocked song")] = {"display": "Blocked Artist - Blocked Song"}
    started = threading.Event()
    release = threading.Event()
    loaded_tracks = [
        Track(title="Blocked Song", artist="Blocked Artist", duration_ms=180_000),
        Track(title="Allowed Song", artist="Allowed Artist", duration_ms=180_000),
    ]
    source = PlaylistSource(kind="url", url="https://example.test/playlist", label="Slow source")

    def _slow_load(*_args, **_kwargs):
        started.set()
        assert release.wait(timeout=2.0)
        return loaded_tracks, source

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer.load_explicit_source", side_effect=_slow_load):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            load_task = asyncio.create_task(
                client.post("/api/playlist/load", json={"url": "https://example.test/playlist"})
            )
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1.0), timeout=2.0)
            stop_response = await client.post("/api/stop")
            release.set()
            load_response = await load_task

    assert stop_response.status_code == 200
    assert load_response.status_code == 200
    assert load_response.json()["tracks"] == 1
    assert load_response.json()["skipped"] is False
    # The route must actually say it went metadata-only. Every sibling ingest
    # route reports this flag and the operator docs promise it here too, but the
    # response dropped it while the test name still claimed it.
    assert load_response.json()["metadata_only"] is True
    assert load_response.json()["resume_required"] is True
    assert state.session_stopped is True
    assert state.now_streaming["type"] == "stopped"
    assert [track.display for track in state.playlist] == ["Allowed Artist – Allowed Song"]
    assert state.playlist_source == source
    assert app.state.queue.empty()
    assert state.queued_segments == []
    assert state.continuity_slot is None


def test_source_load_epoch_change_after_fast_resume_preserves_entire_queue(tmp_path):
    app = _make_test_app()
    state = app.state.station_state
    app.state.config.cache_dir = tmp_path
    protected_path = tmp_path / "resume-runway.mp3"
    protected_path.write_bytes(b"resume-runway")
    stale_path = tmp_path / "stale-source.mp3"
    stale_path.write_bytes(b"stale")
    protected = Segment(
        type=SegmentType.BANTER,
        path=protected_path,
        duration_sec=4.0,
        metadata={
            "queue_id": "protected",
            "title": "Protected continuity",
            "continuity_reservation": True,
        },
    )
    stale = Segment(
        type=SegmentType.MUSIC,
        path=stale_path,
        duration_sec=180.0,
        metadata={"queue_id": "stale", "title": "Old source"},
    )
    app.state.queue.put_nowait(protected)
    app.state.queue.put_nowait(stale)
    state.queued_segments = [{"id": "protected"}, {"id": "stale"}]
    state.session_stopped = False
    state.continuity_epoch = 6
    state.now_streaming = {"type": "music", "label": "Current", "started": time.time(), "metadata": {}}
    pinned = Track(title="Newer Pin", artist="Operator", duration_ms=180_000)
    state.pinned_track = pinned
    state.force_next = SegmentType.BANTER
    state.operator_force_pending = SegmentType.AD
    state.pending_actions.append({"type": "newer-control", "label": "keep me"})
    new_source = PlaylistSource(kind="url", url="https://example.test/new", label="New source")

    result = _apply_loaded_source(
        SimpleNamespace(app=app),
        [Track(title="New", artist="Artist", duration_ms=180_000)],
        new_source,
        captured_continuity_epoch=5,
    )

    assert result["metadata_only"] is True
    assert result["resume_required"] is False
    assert result["skipped"] is False
    assert state.session_stopped is False
    assert list(app.state.queue._queue) == [protected, stale]
    assert [row["id"] for row in state.queued_segments] == ["protected", "stale"]
    assert stale_path.exists()
    assert not app.state.skip_event.is_set()
    assert state.playlist_source == new_source
    assert state.pinned_track is pinned
    assert state.force_next is SegmentType.BANTER
    assert state.operator_force_pending is SegmentType.AD
    assert list(state.pending_actions) == [{"type": "newer-control", "label": "keep me"}]


@pytest.mark.asyncio
async def test_stop_clears_pending_interrupt_and_force_next(tmp_path):
    """A deliberate stop must drop any pending interrupt/forced segment so it
    cannot fire as stale audio on the next resume, and must unlink an ephemeral
    interrupt bridge temp so the stop does not leak it."""
    from mammamiradio.core.models import ChaosSubtype, SegmentType

    app = _make_test_app()
    state = app.state.station_state
    bridge = tmp_path / "interrupt_bridge.mp3"
    bridge.write_bytes(b"id3")
    state.interrupt_slot = bridge
    state.interrupt_slot_ephemeral = True
    state.chaos_pending = ChaosSubtype.URGENT_INTERRUPT
    state.ha_pending_directive = "Announce the urgent kitchen warning."
    state.ha_pending_directive_moment_id = "interrupt-moment"
    state.ha_pending_directive_source = "interrupt"
    state.urgent_interrupt_force_next_revision = state.set_force_next(SegmentType.BANTER)

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/stop")

    assert resp.status_code == 200
    assert state.interrupt_slot is None
    assert state.interrupt_slot_ephemeral is False
    assert state.force_next is None
    assert state.urgent_interrupt_force_next_revision is None
    assert state.chaos_pending is None
    assert state.ha_pending_directive == ""
    assert state.ha_pending_directive_moment_id == ""
    assert state.ha_pending_directive_source == ""
    assert not bridge.exists()


@pytest.mark.asyncio
async def test_stop_cancels_urgent_interrupt_after_chaos_slot_was_abandoned(tmp_path):
    """Stop follows durable urgent ownership after a failed chaos render."""
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    state = app.state.station_state
    bridge = tmp_path / "postfailure-interrupt.mp3"
    bridge.write_bytes(b"interrupt")
    state.interrupt_slot = bridge
    state.interrupt_slot_ephemeral = True
    state.chaos_pending = None
    state.ha_pending_directive = "A failed urgent render must not return after Start."
    state.ha_pending_directive_source = "operator"
    state.urgent_interrupt_force_next_revision = state.set_force_next(SegmentType.BANTER)
    chaos_epoch = state.chaos_cutover_epoch

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/stop")

    assert response.json()["ok"] is True
    assert state.session_stopped is True
    assert state.force_next is None
    assert state.urgent_interrupt_force_next_revision is None
    assert state.ha_pending_directive == ""
    assert state.ha_pending_directive_source == ""
    assert state.interrupt_slot is None
    assert state.interrupt_slot_ephemeral is False
    assert state.chaos_cutover_epoch == chaos_epoch + 1
    assert not bridge.exists()


@pytest.mark.asyncio
async def test_stop_demotes_pending_urgent_interrupt_moment_receipt(tmp_path):
    """Canceling an unqueued ritual interrupt leaves an honest terminal receipt."""
    from mammamiradio.core.models import ChaosSubtype
    from mammamiradio.home.moment_receipts import MomentStore

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    state = app.state.station_state
    store = MomentStore()
    moment_id = store.record(
        lane="interrupt",
        family="safety_saves",
        public_label="Safety moment",
    )
    state.moment_store = store
    state.ha_pending_directive = "React to the urgent home safety event."
    state.ha_pending_directive_moment_id = moment_id
    state.ha_pending_directive_source = "ha"
    state.chaos_pending = ChaosSubtype.URGENT_INTERRUPT
    state.urgent_interrupt_force_next_revision = state.set_force_next(SegmentType.BANTER)

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/stop")

    assert response.json()["ok"] is True
    (row,) = store.rows
    assert row.status == "dropped"
    assert row.drop_reason == GenerationWasteReason.OPERATOR_STOP
    assert state.ha_pending_directive_moment_id == ""


@pytest.mark.asyncio
async def test_panic_cut_while_streaming():
    """Panic with fresh runway skips safely and forces the next segment to music."""
    from mammamiradio.core.models import SegmentType

    app = _make_test_app()
    state = app.state.station_state
    state.now_streaming = {"type": "music", "label": "Test", "started": time.time()}
    state.current_stream_audible = True
    state._last_audible_stream = dict(state.now_streaming)
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
    assert state._last_audible_stream == {}
    # Stale rows are replaced by an audible protected reservation.
    assert len(state.queued_segments) == app.state.queue.qsize() == 1
    assert state.queued_segments[0]["reason"] == "Protected continuity audio."


@pytest.mark.asyncio
async def test_panic_supersedes_stale_operator_force_attribution():
    """Panic cancels an older Air Next card as well as replacing its directive."""
    app = _make_test_app()
    state = app.state.station_state
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        trigger = await client.post("/api/trigger", json={"type": "banter"})
        trigger_revision = state.force_next_revision
        panic = await client.post("/api/panic")

    assert trigger.status_code == 200
    assert trigger.json() == {"ok": True, "triggered": "banter"}
    assert panic.status_code == 200
    assert panic.json()["ok"] is True
    assert state.force_next is SegmentType.MUSIC
    assert state.force_next_revision > trigger_revision
    assert state.operator_force_pending is None


@pytest.mark.asyncio
async def test_panic_supersedes_pending_urgent_interrupt_before_forcing_music(tmp_path):
    """Interrupt -> Panic cancels the bridge/directive before MUSIC takes ownership."""
    from mammamiradio.core.models import ChaosSubtype

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.tmp_dir = tmp_path
    state = app.state.station_state
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        interrupt = await client.post(
            "/api/interrupt",
            json={"directive": "Announce the urgent safety warning.", "urgency": "urgent"},
        )
        assert interrupt.json()["ok"] is True
        assert state.chaos_pending is ChaosSubtype.URGENT_INTERRUPT
        interrupt_revision = state.force_next_revision
        assert state.interrupt_slot is not None

        panic = await client.post("/api/panic")

    assert panic.json()["ok"] is True
    assert state.chaos_pending is None
    assert state.ha_pending_directive == ""
    assert state.ha_pending_directive_moment_id == ""
    assert state.ha_pending_directive_source == ""
    assert state.interrupt_slot is None
    assert state.interrupt_slot_ephemeral is False
    assert state.urgent_interrupt_force_next_revision is None
    assert state.force_next is SegmentType.MUSIC
    assert state.force_next_revision > interrupt_revision


@pytest.mark.asyncio
async def test_panic_clears_urgent_bridge_after_warning_admission(tmp_path):
    """A queued warning cannot leave its immediate bridge ahead of Panic audio."""
    app = _make_test_app()
    state = app.state.station_state
    bridge = tmp_path / "admitted-urgent-bridge.mp3"
    bridge.write_bytes(b"bridge")
    state.interrupt_slot = bridge
    state.interrupt_slot_ephemeral = True
    warning_path = tmp_path / "admitted-urgent-warning.mp3"
    warning_path.write_bytes(b"warning")
    warning = Segment(
        type=SegmentType.BANTER,
        path=warning_path,
        metadata={"queue_id": "admitted-urgent-warning", "urgent_interrupt_priority": True},
    )
    app.state.queue.put_nowait(warning)
    state.queued_segments = [{"id": "admitted-urgent-warning", "type": "banter", "label": "Urgent warning"}]
    # Successful producer admission has already settled these two ownership
    # markers; playback has not yet claimed the bridge slot.
    state.urgent_interrupt_force_next_revision = None
    state.chaos_pending = None

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        panic = await client.post("/api/panic")

    assert panic.status_code == 200
    assert panic.json()["ok"] is True
    assert state.interrupt_slot is None
    assert state.interrupt_slot_ephemeral is False
    assert not bridge.exists()
    assert warning not in app.state.queue._queue
    assert state.force_next is SegmentType.MUSIC


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/api/purge", "/api/panic"], ids=["purge", "panic"])
@pytest.mark.parametrize("dedication_queued", [True, False], ids=["queued", "already-dequeued"])
async def test_queue_reset_revokes_only_discarded_listener_dedication(
    tmp_path,
    endpoint,
    dedication_queued,
):
    """A reset cancels the promise it drops, not a dedication already leaving the queue."""
    from mammamiradio.hosts.scriptwriter import _plan_listener_request_block

    app = _make_test_app()
    state = app.state.station_state
    requested = state.playlist[0]
    listener_request = {
        "request_id": "queued-dedication",
        "name": "Luca",
        "message": f"Play {requested.title} by {requested.artist}",
        "type": "song_request",
        "song_found": True,
        "song_error": False,
        "song_error_reason": "",
        "song_track": requested.display,
        "song_track_obj": requested,
        "song_pinned": False,
        "banter_cycles_missed": 0,
    }
    state.pending_requests.append(listener_request)
    prompt, commit = _plan_listener_request_block(state)
    assert "LISTENER REQUEST:" in prompt
    assert commit is not None

    dedication_path = tmp_path / "listener-dedication.mp3"
    dedication_path.write_bytes(b"dedication")
    queue_id = "listener-dedication-q"
    dedication = Segment(
        type=SegmentType.BANTER,
        path=dedication_path,
        duration_sec=20.0,
        metadata={"queue_id": queue_id, "title": "Listener dedication"},
        ephemeral=False,
    )
    app.state.queue.put_nowait(dedication)
    state.queued_segments = [{"id": queue_id, "type": "banter", "label": "Listener dedication"}]
    commit.apply(state, app.state.config, queue_id=queue_id)
    handoff = state.listener_request_handoff
    assert handoff is not None

    if not dedication_queued:
        assert app.state.queue.get_nowait() is dedication
        app.state.queue.task_done()
        state.queued_segments.clear()

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(endpoint)

    assert response.status_code == 200
    assert response.json()["ok"] is True
    if dedication_queued:
        assert state.listener_request_handoff is None
    else:
        assert state.listener_request_handoff is not None
        assert state.listener_request_handoff.token == handoff.token


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operator_owned",
    [False, True],
    ids=["request-exclusive", "newer-operator-pin"],
)
async def test_queue_remove_settles_music_linked_to_discarded_dedication(tmp_path, operator_owned):
    """An admitted song follows its dedication unless a newer operator owns it."""
    from mammamiradio.hosts.scriptwriter import _plan_listener_request_block
    from mammamiradio.scheduling.producer import _select_accepted_music_track

    app = _make_test_app()
    state = app.state.station_state
    requested = state.playlist[0]
    listener_request = {
        "request_id": f"linked-admitted-{operator_owned}",
        "name": "Luca",
        "message": f"Play {requested.title} by {requested.artist}",
        "type": "song_request",
        "song_found": True,
        "song_error": False,
        "song_error_reason": "",
        "song_track": requested.display,
        "song_track_obj": requested,
        "song_pinned": False,
        "banter_cycles_missed": 0,
    }
    state.pending_requests.append(listener_request)
    if operator_owned:
        state.set_pinned_track(requested)

    prompt, commit = _plan_listener_request_block(state)
    assert "LISTENER REQUEST:" in prompt
    assert commit is not None

    dedication_path = tmp_path / f"linked-dedication-{operator_owned}.mp3"
    dedication_path.write_bytes(b"dedication")
    dedication_queue_id = f"linked-dedication-{operator_owned}-q"
    dedication = Segment(
        type=SegmentType.BANTER,
        path=dedication_path,
        duration_sec=20.0,
        metadata={"queue_id": dedication_queue_id, "title": "Listener dedication"},
        ephemeral=False,
    )
    app.state.queue.put_nowait(dedication)
    state.queued_segments = [{"id": dedication_queue_id, "type": "banter", "label": "Listener dedication"}]
    commit.apply(state, app.state.config, queue_id=dedication_queue_id)
    assert state.listener_request_handoff is not None
    if state.force_next is None:
        state.force_listener_request_handoff_music()
    if state.force_next is not None:
        state.clear_force_next()

    selected = _select_accepted_music_track(state, app.state.config, app.state.queue)
    assert selected is requested
    handoff_metadata = state.listener_request_handoff_metadata(selected)
    assert handoff_metadata[LISTENER_REQUEST_DEDICATION_QUEUE_ID_KEY] == dedication_queue_id
    assert handoff_metadata[LISTENER_REQUEST_HANDOFF_EXCLUSIVE_KEY] is (not operator_owned)

    music_path = tmp_path / f"linked-music-{operator_owned}.mp3"
    music_path.write_bytes(b"music")
    music = Segment(
        type=SegmentType.MUSIC,
        path=music_path,
        duration_sec=180.0,
        metadata={
            "queue_id": f"linked-music-{operator_owned}-q",
            "title": selected.display,
            "title_only": selected.title,
            "artist": selected.artist,
            **handoff_metadata,
        },
        ephemeral=False,
    )
    state.admit_listener_request_handoff(music)
    assert state.listener_request_admitted_reservations
    app.state.queue.put_nowait(music)
    state.queued_segments.append({"id": music.metadata["queue_id"], "type": "music", "label": selected.display})
    discarded_before = state.discarded_segments_total

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/queue/remove", json={"id": dedication_queue_id})

    assert response.status_code == 200
    assert response.json()["removed"] == "Listener dedication"
    queued = list(app.state.queue._queue)
    if operator_owned:
        assert music in queued
        assert state.discarded_segments_total == discarded_before + 1
        assert any(row.get("id") == music.metadata["queue_id"] for row in state.queued_segments)
        for key in (
            LISTENER_REQUEST_HANDOFF_TOKEN_KEY,
            LISTENER_REQUEST_HANDOFF_ADMITTED_KEY,
            LISTENER_REQUEST_DEDICATION_QUEUE_ID_KEY,
            LISTENER_REQUEST_HANDOFF_EXCLUSIVE_KEY,
        ):
            assert key not in music.metadata
    else:
        assert music not in queued
        assert state.discarded_segments_total == discarded_before + 2
        assert not any(row.get("id") == music.metadata["queue_id"] for row in state.queued_segments)
    assert state.listener_request_admitted_reservations == {}


@pytest.mark.asyncio
async def test_panic_music_force_survives_stale_listener_plan_abandon():
    """A newer same-valued Panic force must outlive listener-plan cleanup."""
    from mammamiradio.hosts.scriptwriter import _plan_listener_request_block
    from mammamiradio.scheduling.producer import _abandon_banter_commit

    app = _make_test_app()
    state = app.state.station_state
    requested = state.playlist[0]
    listener_request = {
        "request_id": "panic-listener-plan",
        "name": "Luca",
        "message": f"Play {requested.title} by {requested.artist}",
        "type": "song_request",
        "song_found": True,
        "song_error": False,
        "song_error_reason": "",
        "song_track": requested.display,
        "song_track_obj": requested,
        "song_pinned": False,
        "banter_cycles_missed": 0,
    }
    state.pending_requests.append(listener_request)
    state.now_streaming = {"type": "music", "label": "Current song", "started": time.time()}

    prompt, commit = _plan_listener_request_block(state)

    assert "LISTENER REQUEST:" in prompt
    assert commit is not None
    assert state.pinned_track is requested
    assert state.force_next is SegmentType.MUSIC
    listener_force_revision = state.force_next_revision

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/panic")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert state.force_next is SegmentType.MUSIC
    panic_force_revision = state.force_next_revision
    assert panic_force_revision > listener_force_revision

    # Panic invalidates the in-flight dedication through continuity_epoch. Its
    # cleanup owns the listener pin, but not Panic's newer same-valued force.
    _abandon_banter_commit(state, commit)

    assert listener_request["song_pinned"] is False
    assert state.pinned_track is None
    assert state.force_next is SegmentType.MUSIC
    assert state.force_next_revision == panic_force_revision


@pytest.mark.asyncio
async def test_newer_same_track_operator_pin_survives_listener_plan_abandon():
    """A failed dedication render cannot retract a later move-to-next of its track."""
    from mammamiradio.hosts.scriptwriter import _plan_listener_request_block
    from mammamiradio.scheduling.producer import _abandon_banter_commit
    from mammamiradio.web.streamer import _admin_track_id

    app = _make_test_app()
    app.state.source_switch_lock = asyncio.Lock()
    state = app.state.station_state
    requested = state.playlist[0]
    listener_request = {
        "request_id": "same-track-listener-plan",
        "name": "Luca",
        "message": f"Play {requested.title} by {requested.artist}",
        "type": "song_request",
        "song_found": True,
        "song_error": False,
        "song_error_reason": "",
        "song_track": requested.display,
        "song_track_obj": requested,
        "song_pinned": False,
        "banter_cycles_missed": 0,
    }
    state.pending_requests.append(listener_request)

    prompt, commit = _plan_listener_request_block(state)

    assert "LISTENER REQUEST:" in prompt
    assert commit is not None
    assert state.pinned_track is requested
    listener_force_revision = state.force_next_revision

    target = {
        "revision": state.playlist_revision,
        "index": 0,
        "id": _admin_track_id(requested),
    }
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 0):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/playlist/move_to_next", json=target)

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert state.pinned_track is requested
    operator_force_revision = state.force_next_revision
    assert operator_force_revision > listener_force_revision

    _abandon_banter_commit(state, commit)

    assert listener_request["song_pinned"] is False
    assert state.pinned_track is requested
    assert state.force_next is SegmentType.MUSIC
    assert state.force_next_revision == operator_force_revision


@pytest.mark.asyncio
async def test_discarded_dedication_preserves_newer_same_track_operator_pin(tmp_path):
    """Removing an old dedication cannot retract a later move-to-next of its track."""
    from mammamiradio.hosts.scriptwriter import _plan_listener_request_block
    from mammamiradio.web.streamer import _admin_track_id

    app = _make_test_app()
    app.state.source_switch_lock = asyncio.Lock()
    state = app.state.station_state
    requested = state.playlist[0]
    listener_request = {
        "request_id": "same-track-queued-dedication",
        "name": "Luca",
        "message": f"Play {requested.title} by {requested.artist}",
        "type": "song_request",
        "song_found": True,
        "song_error": False,
        "song_error_reason": "",
        "song_track": requested.display,
        "song_track_obj": requested,
        "song_pinned": False,
        "banter_cycles_missed": 0,
    }
    state.pending_requests.append(listener_request)
    prompt, commit = _plan_listener_request_block(state)
    assert "LISTENER REQUEST:" in prompt
    assert commit is not None

    dedication_path = tmp_path / "same-track-dedication.mp3"
    dedication_path.write_bytes(b"dedication")
    queue_id = "same-track-dedication-q"
    dedication = Segment(
        type=SegmentType.BANTER,
        path=dedication_path,
        duration_sec=20.0,
        metadata={"queue_id": queue_id, "title": "Listener dedication"},
        ephemeral=False,
    )
    app.state.queue.put_nowait(dedication)
    state.queued_segments = [{"id": queue_id, "type": "banter", "label": "Listener dedication"}]
    commit.apply(state, app.state.config, queue_id=queue_id)
    assert state.listener_request_handoff is not None
    listener_force_revision = state.force_next_revision

    target = {
        "revision": state.playlist_revision,
        "index": 0,
        "id": _admin_track_id(requested),
    }
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 0):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            move_response = await client.post("/api/playlist/move_to_next", json=target)
            operator_force_revision = state.force_next_revision
            continuity_epoch_before_remove = state.continuity_epoch
            remove_response = await client.post("/api/queue/remove", json={"id": queue_id})

    assert move_response.status_code == 200
    assert move_response.json()["ok"] is True
    assert operator_force_revision > listener_force_revision
    assert remove_response.status_code == 200
    assert remove_response.json()["removed"] == "Listener dedication"
    assert state.listener_request_handoff is None
    assert state.pinned_track is requested
    assert state.force_next is SegmentType.MUSIC
    assert state.force_next_revision == operator_force_revision
    assert state.continuity_epoch > continuity_epoch_before_remove


@pytest.mark.asyncio
async def test_panic_cut_does_not_skip_when_no_ready_runway(tmp_path):
    """Panic still steers recovery, but never cuts current audio into an empty queue."""
    app = _make_test_app()
    state = app.state.station_state
    state.now_streaming = {"type": "music", "label": "Test", "started": time.time()}
    state.current_stream_audible = True
    state._last_audible_stream = dict(state.now_streaming)
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
    assert state._last_audible_stream["label"] == "Test"

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
    state.current_stream_audible = True
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
    state.current_stream_audible = True
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
async def test_skip_ignores_unplayable_duration_when_reserving_before_cut(tmp_path):
    """Missing queue files cannot shrink the fresh safety-audio target."""
    app = _make_test_app()
    state = app.state.station_state
    state.now_streaming = {
        "type": "music",
        "label": "Current Artist — Current Song",
        "started": time.time(),
        "metadata": {"artist": "Current Artist", "title_only": "Current Song"},
    }
    state.current_stream_audible = True
    missing_segments = [
        Segment(
            type=SegmentType.MUSIC,
            path=tmp_path / f"missing-{index}.mp3",
            duration_sec=120.0,
            metadata={"queue_id": f"missing-{index}", "title": f"Missing {index}"},
            ephemeral=False,
        )
        for index in range(2)
    ]
    for segment in missing_segments:
        app.state.queue.put_nowait(segment)
    state.queued_segments = [
        {"id": f"missing-{index}", "type": "music", "label": f"Missing {index}"} for index in range(2)
    ]
    recovery_path = tmp_path / "ready-continuity.mp3"
    recovery_path.write_bytes(b"ready-continuity")
    recovery = Segment(
        type=SegmentType.BANTER,
        path=recovery_path,
        duration_sec=240.0,
        metadata={"queue_id": "ready-continuity", "continuity_reservation": True},
        ephemeral=False,
    )
    reservation_builder = MagicMock(return_value=[recovery])
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    with (
        patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 240),
        patch("mammamiradio.web.streamer._continuity_reservation_segments", reservation_builder),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/skip")

    assert response.json()["bridged"] is False
    assert reservation_builder.call_args.args[2] == pytest.approx(240.0)
    assert list(app.state.queue._queue) == [recovery]
    assert state.queued_segments[0]["id"] == "ready-continuity"
    assert state.force_next is None
    assert app.state.skip_event.is_set()
    assert state.discard_by_reason[GenerationWasteReason.OPERATOR_PURGE] == 2
    assert app.state.queue._unfinished_tasks == 1
    assert app.state.queue.get_nowait() is recovery
    app.state.queue.task_done()
    await asyncio.wait_for(app.state.queue.join(), timeout=1.0)


@pytest.mark.asyncio
async def test_zero_byte_queue_head_is_not_skip_or_status_runway(tmp_path):
    """An existing-but-empty file cannot greenlight a live cut."""
    app = _make_test_app()
    state = app.state.station_state
    state.now_streaming = {"type": "banter", "label": "Current", "started": time.time()}
    state.current_stream_audible = True
    empty_path = tmp_path / "empty-runway.mp3"
    empty_path.touch()
    empty = Segment(
        type=SegmentType.MUSIC,
        path=empty_path,
        duration_sec=300.0,
        metadata={"queue_id": "empty-runway", "artist": "Artist", "title_only": "Empty"},
        ephemeral=False,
    )
    app.state.queue.put_nowait(empty)
    state.queued_segments = [{"id": "empty-runway", "type": "music", "label": "Empty"}]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            status_response = await client.get("/public-status")
            skip_response = await client.post("/api/skip")

    assert status_response.json()["playback_actions"] == {"skip_ready": True, "skip_would_bridge": True}
    assert skip_response.json() == {"ok": True, "bridged": True}
    assert app.state.queue.empty()
    assert state.queued_segments == []
    assert state.force_next is SegmentType.MUSIC
    assert state.discard_by_reason[GenerationWasteReason.OPERATOR_PURGE] == 1
    assert app.state.queue._unfinished_tasks == 0
    await asyncio.wait_for(app.state.queue.join(), timeout=1.0)


@pytest.mark.asyncio
async def test_public_status_reports_stale_companionship_head_as_skip_bridge(tmp_path):
    """Status and Skip share the playback-valid runway predicate."""
    app = _make_test_app()
    now, listener_id, _, stale_cue, claim = _queue_companionship_cue(app, tmp_path)
    app.state.stream_hub.unsubscribe(listener_id)
    now[0] = 2_400.0
    app.state.stream_hub.subscribe()
    state = app.state.station_state
    state.now_streaming = {"type": "music", "label": "Current", "started": time.time()}
    state.current_stream_audible = True
    assert state.listener_session.epoch == claim.epoch + 1
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/public-status")

    assert response.json()["playback_actions"] == {"skip_ready": True, "skip_would_bridge": True}
    assert list(app.state.queue._queue) == [stale_cue]


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
    state.current_stream_audible = True
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
    state.current_stream_audible = True
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
    state.current_stream_audible = True
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
    state.current_stream_audible = True
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
    assert "radio-itali-v7" in text
    assert "const isFreshAsset" in text
    assert "path.endsWith('.css')" in text
    assert "path.endsWith('.js')" in text
    assert "const isStableInstallAsset" in text

    stable_cache_block = text.split("const isStableInstallAsset", maxsplit=1)[1]
    assert "path.endsWith('.css')" not in stable_cache_block
    assert "path.endsWith('.js')" not in stable_cache_block


@pytest.mark.asyncio
async def test_sw_js_never_caches_listener_request_receipts():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/sw.js")

    assert resp.status_code == 200
    assert "path.includes('/public-listener-requests')" in resp.text


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
        assert re.search(r'</head>\s*<body\b[^>]*data-stopped="true"[^>]*>', response.text)
        assert 'data-first-listen-entry="complete"' in response.text


@pytest.mark.asyncio
async def test_admin_first_paint_seeds_running_state_for_direct_and_ingress_routes():
    """A running admin page explicitly paints enabled producer controls."""
    direct, ingress = await _admin_first_paint_responses(stopped=False)

    for response in (direct, ingress):
        assert response.status_code == 200
        assert re.search(r'</head>\s*<body\b[^>]*data-stopped="false"[^>]*>', response.text)
        assert 'data-first-listen-entry="complete"' in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin", "receipt", "expected"),
    [
        (FirstListenInstallOriginStatus.FRESH, None, "required"),
        (
            FirstListenInstallOriginStatus.FRESH,
            FirstListenReceiptV1(
                selected_entity_id="media_player.kitchen",
                accepted_attempt_id="abcdefghijklmnop",
                accepted_at=100.0,
                heard_at=101.0,
                privacy_reviewed_at=102.0,
            ),
            "complete",
        ),
        (FirstListenInstallOriginStatus.EXISTING, None, "complete"),
        (FirstListenInstallOriginStatus.UNKNOWN, None, "required"),
    ],
)
async def test_admin_first_paint_selects_first_listen_only_for_fresh_unfinished_install(origin, receipt, expected):
    app = _make_test_app()
    app.state.first_listen_install_origin = FirstListenInstallOriginV1(origin)
    app.state.first_listen_receipt = receipt
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/admin")

    assert response.status_code == 200
    assert f'data-first-listen-entry="{expected}"' in response.text


@pytest.mark.asyncio
async def test_admin_first_paint_stays_pending_before_bootstrap_tasks_are_wired():
    """An empty task tuple during partial construction is not authoritative."""
    app = _make_test_app()
    app.state.first_listen_bootstrap_snapshot_authoritative = False
    app.state.first_listen_bootstrap_wired = True
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/admin")

    assert response.status_code == 200
    assert 'data-first-listen-entry="pending"' in response.text


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
    app.state.config.homeassistant.context_enabled = True
    app.state.config.ha_token = "ha-token"
    app.state.station_state.ha_context = "- Coffee machine: on"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        capabilities_resp = await client.get("/api/capabilities")
        setup_resp = await client.get("/api/setup/status", headers=ACTIVE_SETUP_HEADERS)

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
async def test_homeassistant_labels_regenerate_disabled_context_returns_unscheduled():
    app = _make_test_app()
    app.state.station_state.home_authorization = HomeAuthorization.legacy()
    app.state.config.homeassistant.context_enabled = False
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
    assert resp.json() == {"scheduled": False, "reason": "home_context_disabled"}
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
                headers=ACTIVE_SETUP_HEADERS,
                json={"entity_id": "switch.coffee_machine", "muted": True},
            )
            preview = await client.get("/api/homeassistant/context-candidates")
            setup_status = await client.get("/api/setup/status", headers=ACTIVE_SETUP_HEADERS)
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
                headers=ACTIVE_SETUP_HEADERS,
                json={"entity_id": entity_id, "muted": True},
            )
            # The physical state flips while the hard mute is active.
            unmuted = await client.patch(
                "/api/homeassistant/entity-policy",
                headers=ACTIVE_SETUP_HEADERS,
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
            headers=ACTIVE_SETUP_HEADERS,
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
            headers=ACTIVE_SETUP_HEADERS,
            json={"entity_id": "binary_sensor.living_presence", "personal_moment_enabled": True},
        )
        opt_out = await client.patch(
            "/api/homeassistant/entity-policy",
            headers=ACTIVE_SETUP_HEADERS,
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
            headers=ACTIVE_SETUP_HEADERS,
            json={"entity_id": "switch.coffee_machine", "muted": True},
        )

    assert resp.status_code == 200
    assert ledger.buckets == {}
    ledger_file = tmp_path / "evening_ledger.json"
    assert ledger_file.exists()
    assert "switch.coffee_machine" not in ledger_file.read_text()


@pytest.mark.asyncio
async def test_homeassistant_entity_policy_hard_mute_fences_detached_llm_work_before_cleanup_await(tmp_path):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    order: list[str] = []
    ledger = SimpleNamespace(save_if_dirty=lambda _cache_dir: order.append("ledger_save"))
    app.state.station_state.evening_ledger = ledger

    def write_policy(*_args, **_kwargs):
        order.append("policy_write")
        return {
            "schema_version": 1,
            "policy_revision": 1,
            "muted": {"switch.coffee_machine": {}},
            "personal_moment_opt_ins": {},
        }

    def invalidate_labels():
        order.append("labels_invalidated")
        return ()

    def reset_scene():
        order.append("scene_reset")

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with (
        patch("mammamiradio.web.streamer.set_entity_muted", side_effect=write_policy),
        patch("mammamiradio.web.streamer.invalidate_label_generation", side_effect=invalidate_labels),
        patch("mammamiradio.web.streamer.reset_scene_namer_cache", side_effect=reset_scene),
        patch("mammamiradio.web.streamer._clear_home_context_usage", return_value=True),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.patch(
                "/api/homeassistant/entity-policy",
                headers=ACTIVE_SETUP_HEADERS,
                json={"entity_id": "switch.coffee_machine", "muted": True},
            )

    assert response.status_code == 200
    assert order == ["policy_write", "labels_invalidated", "scene_reset", "ledger_save"]


@pytest.mark.asyncio
async def test_homeassistant_entity_policy_hard_mute_blocks_late_label_catalog_publish(tmp_path):
    import mammamiradio.home.catalog as catalog

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.anthropic_api_key = "test-key"
    entity_id = "switch.coffee_machine"
    states = {
        entity_id: {
            "entity_id": entity_id,
            "state": "on",
            "attributes": {"friendly_name": "Coffee machine"},
        }
    }
    provider_entered = asyncio.Event()

    async def cancellation_resistant_provider(candidates, _config, *, role):
        assert role == "fast"
        provider_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return [
                {
                    "entity_id": candidates[0].entity_id,
                    "label_it": "Macchina privata",
                    "label_en": "Private machine",
                }
            ]
        raise AssertionError("provider gate unexpectedly completed")

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with (
        patch("mammamiradio.home.catalog._call_anthropic_labels", side_effect=cancellation_resistant_provider),
        patch("mammamiradio.home.catalog.save_catalog") as save,
    ):
        assert catalog.schedule_label_generation(states, cache_dir=tmp_path, config=app.state.config, force=True)
        await asyncio.wait_for(provider_entered.wait(), timeout=1)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.patch(
                "/api/homeassistant/entity-policy",
                headers=ACTIVE_SETUP_HEADERS,
                json={"entity_id": entity_id, "muted": True},
            )
        await asyncio.sleep(0)

    assert response.status_code == 200
    save.assert_not_called()
    assert catalog.load_catalog(tmp_path)["entries"] == {}


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
            headers=ACTIVE_SETUP_HEADERS,
            json={"entity_id": "switch.coffee_machine", "muted": False},
        )
        second = await client.patch(
            "/api/homeassistant/entity-policy",
            headers=ACTIVE_SETUP_HEADERS,
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
            headers=ACTIVE_SETUP_HEADERS,
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
            headers=ACTIVE_SETUP_HEADERS,
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
            headers=ACTIVE_SETUP_HEADERS,
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
            headers=ACTIVE_SETUP_HEADERS,
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
            headers=ACTIVE_SETUP_HEADERS,
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
                headers=ACTIVE_SETUP_HEADERS,
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
            resp = await client.post("/api/setup/provider-check", headers=ACTIVE_SETUP_HEADERS, json={})
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
                resp = await client.post(
                    "/api/setup/save-keys",
                    headers=ACTIVE_SETUP_HEADERS,
                    json={"ANTHROPIC_API_KEY": "sk-ant-new"},
                )
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
            await client.post("/api/setup/provider-check", headers=ACTIVE_SETUP_HEADERS, json={})
            assert app.state.station_state.anthropic_key_status == "rejected"
            # Second call inside the 2s debounce window returns the cached result.
            await client.post("/api/setup/provider-check", headers=ACTIVE_SETUP_HEADERS, json={})
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
            req = asyncio.create_task(client.post("/api/setup/provider-check", headers=ACTIVE_SETUP_HEADERS, json={}))
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
                headers=ACTIVE_SETUP_HEADERS,
                json={"entity_id": "binary_sensor.office_presence", "personal_moment_enabled": True},
            )
            muted = await client.patch(
                "/api/homeassistant/entity-policy",
                headers=ACTIVE_SETUP_HEADERS,
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
            headers=ACTIVE_SETUP_HEADERS,
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
                headers=ACTIVE_SETUP_HEADERS,
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
            headers=ACTIVE_SETUP_HEADERS,
            json={"entity_id": "switch.kitchen_light", "muted": True, "personal_moment_enabled": True},
        )
        neither = await client.patch(
            "/api/homeassistant/entity-policy",
            headers=ACTIVE_SETUP_HEADERS,
            json={"entity_id": "switch.kitchen_light"},
        )

    assert both.status_code == 422
    assert neither.status_code == 422


@pytest.mark.asyncio
async def test_first_listen_players_requires_exact_empty_json_and_returns_saved_selection(tmp_path):
    from mammamiradio.core.first_listen import FirstListenReceiptStore
    from mammamiradio.home.ha_playback import HADiscoveryResult, HAPlayerCandidate

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    store = FirstListenReceiptStore(tmp_path)
    receipt = await store.record_accepted("media_player.kitchen")
    app.state.first_listen_store = store
    app.state.first_listen_receipt = receipt
    discovery = HADiscoveryResult(
        candidates=(
            HAPlayerCandidate(
                entity_id="media_player.kitchen",
                friendly_name="Kitchen",
                state="idle",
                device_class="speaker",
                area="Kitchen",
                supports_play_media=True,
                available=True,
            ),
        )
    )
    service = SimpleNamespace(
        discover=AsyncMock(return_value=discovery),
        pending_receipt_entity_id=MagicMock(return_value="media_player.kitchen"),
    )
    app.state.ha_playback_fingerprint = _ha_playback_access_snapshot(app.state.config)[2]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer._ha_playback_service", return_value=service):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            invalid = await client.post("/api/setup/first-listen/players")
            valid = await client.post("/api/setup/first-listen/players", json={})

    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_request"
    assert valid.status_code == 200
    assert valid.json()["selected_entity_id"] == "media_player.kitchen"
    assert valid.json()["candidates"] == valid.json()["players"]
    assert valid.json()["media_source_uri"] == "media-source://mammamiradio/live"
    assert valid.json()["receipt_recovery"] == {
        "available": True,
        "entity_id": "media_player.kitchen",
    }


@pytest.mark.asyncio
async def test_setup_status_projects_server_owned_receipt_recovery_without_ha_io():
    app = _make_test_app()
    app.state.ha_playback_service = SimpleNamespace(
        pending_receipt_entity_id=MagicMock(return_value="media_player.kitchen")
    )
    app.state.ha_playback_fingerprint = _ha_playback_access_snapshot(app.state.config)[2]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers=ACTIVE_SETUP_HEADERS,
    ) as client:
        response = await client.get("/api/setup/status")

    recovery = response.json()["guided_setup"]["first_listen"]["receipt_recovery"]
    assert response.status_code == 200
    assert recovery == {"available": True, "entity_id": "media_player.kitchen"}
    app.state.ha_playback_service.pending_receipt_entity_id.assert_called_once_with()


@pytest.mark.asyncio
async def test_setup_status_hides_cached_receipt_recovery_after_ha_access_changes():
    app = _make_test_app()
    app.state.config.homeassistant.enabled = True
    app.state.config.homeassistant.url = "http://supervisor/core"
    app.state.config.ha_token = "first-supervisor-token"
    projection = MagicMock(return_value="media_player.kitchen")
    app.state.ha_playback_service = SimpleNamespace(pending_receipt_entity_id=projection)
    app.state.ha_playback_fingerprint = _ha_playback_access_snapshot(app.state.config)[2]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers=ACTIVE_SETUP_HEADERS,
    ) as client:
        before_rotation = await client.get("/api/setup/status")
        app.state.config.ha_token = "rotated-supervisor-token"
        after_rotation = await client.get("/api/setup/status")

    assert before_rotation.status_code == 200
    assert before_rotation.json()["guided_setup"]["first_listen"]["receipt_recovery"] == {
        "available": True,
        "entity_id": "media_player.kitchen",
    }
    assert after_rotation.status_code == 200
    assert after_rotation.json()["guided_setup"]["first_listen"]["receipt_recovery"] == {
        "available": False,
        "entity_id": "",
    }
    projection.assert_called_once_with()


@pytest.mark.asyncio
async def test_first_listen_play_and_matching_heard_confirmation_persist(tmp_path):
    from mammamiradio.core.first_listen import FirstListenReceiptStore
    from mammamiradio.home.ha_playback import HAPlayResult

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    store = FirstListenReceiptStore(tmp_path)
    accepted = await store.record_accepted("media_player.living_room")
    app.state.first_listen_store = store
    app.state.first_listen_receipt = accepted
    service = SimpleNamespace(
        play=AsyncMock(
            return_value=HAPlayResult(
                entity_id="media_player.living_room",
                accepted=True,
                station_resumed=True,
                receipt_persisted=True,
                attempt_id=accepted.accepted_attempt_id,
            )
        )
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer._ha_playback_service", return_value=service):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            play = await client.post(
                "/api/setup/first-listen/play",
                json={"entity_id": "media_player.living_room"},
            )
            heard = await client.post(
                "/api/setup/first-listen/verify",
                json={"attempt_id": accepted.accepted_attempt_id, "heard": True},
            )

    assert play.status_code == 200
    assert play.json()["accepted"] is True
    assert heard.status_code == 200
    assert heard.json()["first_listen_achieved"] is True
    assert (await store.load()).audio_complete is True


@pytest.mark.asyncio
async def test_first_listen_routes_return_safe_errors_for_ha_and_receipt_failures(tmp_path):
    from mammamiradio.core.first_listen import FirstListenReceiptUnavailableError
    from mammamiradio.home.ha_playback import (
        HADiscoveryResult,
        HAPlaybackError,
        HAPlaybackReason,
        HAPlayResult,
    )

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.first_listen_store = SimpleNamespace(load=AsyncMock(side_effect=OSError("receipt read failed")))
    service = SimpleNamespace(
        discover=AsyncMock(
            side_effect=[
                HAPlaybackError(HAPlaybackReason.HA_UNREACHABLE),
                HADiscoveryResult(candidates=()),
            ]
        ),
        play=AsyncMock(
            side_effect=[
                HAPlaybackError(HAPlaybackReason.SERVICE_REJECTED, station_resumed=True),
                HAPlayResult(
                    entity_id="media_player.kitchen",
                    accepted=True,
                    station_resumed=True,
                    receipt_persisted=False,
                ),
            ]
        ),
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer._ha_playback_service", return_value=service):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            discovery_failed = await client.post("/api/setup/first-listen/players", json={})
            receipt_read_failed = await client.post("/api/setup/first-listen/players", json={})
            playback_failed = await client.post(
                "/api/setup/first-listen/play",
                json={"entity_id": "media_player.kitchen"},
            )
            receipt_write_failed = await client.post(
                "/api/setup/first-listen/play",
                json={"entity_id": "media_player.kitchen"},
            )
            app.state.first_listen_store = SimpleNamespace(
                verify=AsyncMock(side_effect=FirstListenReceiptUnavailableError("receipt write failed"))
            )
            verify_failed = await client.post(
                "/api/setup/first-listen/verify",
                json={"attempt_id": "current-attempt", "heard": True},
            )

    assert discovery_failed.status_code == 503
    assert discovery_failed.json()["error"]["code"] == "ha_unreachable"
    assert receipt_read_failed.status_code == 200
    assert receipt_read_failed.json()["selected_entity_id"] == ""
    assert playback_failed.status_code == 502
    assert playback_failed.json()["error"]["code"] == "service_rejected"
    assert playback_failed.json()["station_resumed"] is True
    assert receipt_write_failed.status_code == 503
    assert receipt_write_failed.json()["error"]["code"] == "receipt_unavailable"
    assert receipt_write_failed.json()["accepted"] is True
    assert receipt_write_failed.json()["receipt_persisted"] is False
    assert receipt_write_failed.json()["entity_id"] == "media_player.kitchen"
    assert verify_failed.status_code == 503
    assert verify_failed.json()["error"]["code"] == "receipt_unavailable"
    assert verify_failed.json()["accepted"] is True
    assert verify_failed.json()["receipt_persisted"] is False


@pytest.mark.asyncio
async def test_first_listen_receipt_retry_persists_without_replaying(tmp_path):
    """A proven HA acceptance can be saved later without another cast."""
    from mammamiradio.home.ha_playback import (
        HAPlaybackError,
        HAPlaybackReason,
        HAPlayResult,
    )

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    service = SimpleNamespace(
        play=AsyncMock(
            return_value=HAPlayResult(
                entity_id="media_player.kitchen",
                accepted=True,
                station_resumed=True,
                receipt_persisted=False,
            )
        ),
        persist_pending_receipt=AsyncMock(
            side_effect=[
                HAPlaybackError(HAPlaybackReason.RECEIPT_UNAVAILABLE, station_resumed=True),
                HAPlayResult(
                    entity_id="media_player.kitchen",
                    accepted=True,
                    station_resumed=True,
                    receipt_persisted=True,
                    attempt_id="saved-listening-check",
                ),
            ]
        ),
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer._ha_playback_service", return_value=service):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            play = await client.post(
                "/api/setup/first-listen/play",
                json={"entity_id": "media_player.kitchen"},
            )
            invalid = await client.post(
                "/api/setup/first-listen/receipt/retry",
                json={"entity_id": "media_player.kitchen", "unexpected": True},
            )
            still_unavailable = await client.post(
                "/api/setup/first-listen/receipt/retry",
                json={"entity_id": "media_player.kitchen"},
            )
            recovered = await client.post(
                "/api/setup/first-listen/receipt/retry",
                json={"entity_id": "media_player.kitchen"},
            )

    assert play.status_code == 503
    assert play.json()["accepted"] is True
    assert play.json()["receipt_persisted"] is False
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_request"
    assert still_unavailable.status_code == 503
    assert still_unavailable.json()["accepted"] is True
    assert still_unavailable.json()["receipt_persisted"] is False
    assert recovered.status_code == 200
    assert recovered.json()["attempt_id"] == "saved-listening-check"
    assert "No playback request was sent again" in recovered.json()["message"]
    service.play.assert_awaited_once_with("media_player.kitchen")
    assert service.persist_pending_receipt.await_count == 2


@pytest.mark.asyncio
async def test_first_listen_receipt_retry_requires_server_owned_pending_acceptance(tmp_path):
    """A restart, config change, or invented entity cannot mint audible proof."""
    from mammamiradio.home.ha_playback import HAPlaybackError, HAPlaybackReason

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    service = SimpleNamespace(
        persist_pending_receipt=AsyncMock(side_effect=HAPlaybackError(HAPlaybackReason.RECEIPT_RECOVERY_MISSING))
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer._ha_playback_service", return_value=service):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            missing_body = await client.post("/api/setup/first-listen/receipt/retry")
            missing_proof = await client.post(
                "/api/setup/first-listen/receipt/retry",
                json={"entity_id": "media_player.kitchen"},
            )

    assert missing_body.status_code == 422
    assert missing_body.json()["error"]["code"] == "invalid_request"
    assert missing_proof.status_code == 409
    assert missing_proof.json()["error"]["code"] == "receipt_recovery_missing"
    assert "Nothing was replayed" in missing_proof.json()["error"]["message"]
    service.persist_pending_receipt.assert_awaited_once_with("media_player.kitchen")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b'{"entity_id":"media_player.kitchen"}', "text/plain"),
        (b"{bad", "application/json"),
        (b'["media_player.kitchen"]', "application/json"),
        (b'{"entity_id":42}', "application/json"),
    ],
)
async def test_first_listen_receipt_retry_rejects_non_exact_json_without_persisting(
    tmp_path,
    content,
    content_type,
):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    service = SimpleNamespace(persist_pending_receipt=AsyncMock())
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    headers = {**ACTIVE_SETUP_HEADERS, "Content-Type": content_type}
    with patch("mammamiradio.web.streamer._ha_playback_service", return_value=service):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=headers,
        ) as client:
            response = await client.post(
                "/api/setup/first-listen/receipt/retry",
                content=content,
            )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    service.persist_pending_receipt.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected_status"),
    [
        ("request_in_flight", 409),
        ("ha_access_missing", 409),
    ],
)
async def test_first_listen_receipt_retry_does_not_invent_acceptance_for_other_errors(
    tmp_path,
    reason,
    expected_status,
):
    from mammamiradio.home.ha_playback import HAPlaybackError, HAPlaybackReason

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    service = SimpleNamespace(persist_pending_receipt=AsyncMock(side_effect=HAPlaybackError(HAPlaybackReason(reason))))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer._ha_playback_service", return_value=service):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            response = await client.post(
                "/api/setup/first-listen/receipt/retry",
                json={"entity_id": "media_player.kitchen"},
            )

    payload = response.json()
    assert response.status_code == expected_status
    assert payload["error"]["code"] == reason
    assert "accepted" not in payload
    assert "receipt_persisted" not in payload
    service.persist_pending_receipt.assert_awaited_once_with("media_player.kitchen")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("receipt_persisted", "attempt_id"),
    [(False, None), (True, None)],
)
async def test_first_listen_play_requires_explicit_durable_attempt_truth(
    tmp_path,
    receipt_persisted,
    attempt_id,
):
    """Unsaved attempts and missing IDs never unlock verification."""
    from mammamiradio.home.ha_playback import HAPlayResult

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    service = SimpleNamespace(
        play=AsyncMock(
            return_value=HAPlayResult(
                entity_id="media_player.kitchen",
                accepted=True,
                station_resumed=True,
                receipt_persisted=receipt_persisted,
                attempt_id=attempt_id,
            )
        )
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer._ha_playback_service", return_value=service):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            response = await client.post(
                "/api/setup/first-listen/play",
                json={"entity_id": "media_player.kitchen"},
            )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "receipt_unavailable"
    assert response.json()["accepted"] is True
    assert response.json()["receipt_persisted"] is False
    service.play.assert_awaited_once_with("media_player.kitchen")


@pytest.mark.asyncio
async def test_first_listen_verify_rejects_stale_attempt_and_unknown_fields(tmp_path):
    from mammamiradio.core.first_listen import FirstListenReceiptStore

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.first_listen_store = FirstListenReceiptStore(tmp_path)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers=ACTIVE_SETUP_HEADERS,
    ) as client:
        stale = await client.post(
            "/api/setup/first-listen/verify",
            json={"attempt_id": "stale-attempt-id-1234", "heard": True},
        )
        extra = await client.post(
            "/api/setup/first-listen/verify",
            json={"attempt_id": "stale-attempt-id-1234", "heard": True, "extra": 1},
        )

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "attempt_mismatch"
    assert extra.status_code == 422
    assert extra.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_home_context_setup_routes_reject_invalid_json_and_missing_ha_access(tmp_path):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.homeassistant.enabled = False
    app.state.config.ha_token = ""
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers=ACTIVE_SETUP_HEADERS,
    ) as client:
        invalid_preview = await client.post("/api/setup/home-context-preview")
        missing_access = await client.post("/api/setup/home-context-preview", json={})
        invalid_choice = await client.patch("/api/setup/home-context-choice")

    assert invalid_preview.status_code == 422
    assert invalid_preview.json()["error"]["code"] == "invalid_request"
    assert missing_access.status_code == 409
    assert missing_access.json()["error"]["code"] == "ha_access_missing"
    assert invalid_choice.status_code == 422
    assert invalid_choice.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin_status",
    [FirstListenInstallOriginStatus.FRESH, FirstListenInstallOriginStatus.UNKNOWN],
)
async def test_home_context_widening_requires_audible_first_listen_for_unproven_installs(
    tmp_path,
    origin_status,
    monkeypatch,
):
    """Preview and Enable fail closed; Keep off remains an immediate narrowing action."""
    from mammamiradio.core.first_listen import FirstListenReceiptStore
    from mammamiradio.home.ha_context import HomeContext, HomeContextPreviewResult

    monkeypatch.delenv("MAMMAMIRADIO_HA_CONTEXT_ENABLED", raising=False)
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.homeassistant.enabled = True
    app.state.config.homeassistant.url = "http://supervisor/core"
    app.state.config.homeassistant.context_enabled = False
    app.state.config.ha_token = "supervisor-token"
    app.state.first_listen_store = FirstListenReceiptStore(tmp_path)
    app.state.first_listen_install_origin = FirstListenInstallOriginV1(origin_status)
    preview_result = HomeContextPreviewResult(
        kind="fresh",
        context=HomeContext(authorization_mode=HomeAuthorizationMode.NARROW.value, timestamp=time.time()),
        duration_seconds=0.01,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with (
        patch(
            "mammamiradio.web.streamer.fetch_home_context_preview",
            new=AsyncMock(return_value=preview_result),
        ) as fetch,
        patch("mammamiradio.web.streamer._persist_home_context_choice", new=AsyncMock()) as persist,
    ):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            preview = await client.post("/api/setup/home-context-preview", json={})
            enabled = await client.patch("/api/setup/home-context-choice", json={"enabled": True})
            kept_off = await client.patch("/api/setup/home-context-choice", json={"enabled": False})

    assert preview.status_code == 409
    assert preview.json()["error"]["code"] == "first_listen_required"
    assert enabled.status_code == 409
    assert enabled.json()["error"]["code"] == "first_listen_required"
    assert kept_off.status_code == 200
    assert kept_off.json()["enabled"] is False
    assert "MAMMAMIRADIO_HA_CONTEXT_ENABLED" not in os.environ
    fetch.assert_not_awaited()
    persist.assert_awaited_once_with(app.state.config, False)


@pytest.mark.asyncio
async def test_fresh_empty_home_preview_unlocks_enable_without_publishing_context(tmp_path, monkeypatch):
    from mammamiradio.core.first_listen import FirstListenReceiptStore
    from mammamiradio.home.ha_context import HomeContext, HomeContextPreviewResult

    monkeypatch.delenv("MAMMAMIRADIO_HA_CONTEXT_ENABLED", raising=False)
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.homeassistant.enabled = True
    app.state.config.homeassistant.url = "http://supervisor/core"
    app.state.config.homeassistant.context_enabled = False
    app.state.config.ha_token = "supervisor-token"
    app.state.station_state.home_authorization = HomeAuthorization.narrow()
    store = FirstListenReceiptStore(tmp_path)
    accepted = await store.record_accepted("media_player.kitchen", accepted_at=time.time() - 2)
    heard = await store.verify(accepted.accepted_attempt_id or "", heard=True, verified_at=time.time() - 1)
    app.state.first_listen_store = store
    app.state.first_listen_receipt = heard
    app.state.first_listen_install_origin = FirstListenInstallOriginV1(FirstListenInstallOriginStatus.FRESH)
    preview_result = HomeContextPreviewResult(
        kind="fresh",
        context=HomeContext(authorization_mode=HomeAuthorizationMode.NARROW.value, timestamp=time.time()),
        duration_seconds=0.01,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with (
        patch("mammamiradio.web.streamer.fetch_home_context_preview", new=AsyncMock(return_value=preview_result)),
        patch("mammamiradio.web.streamer._persist_home_context_choice", new=AsyncMock()) as persist,
    ):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            preview = await client.post("/api/setup/home-context-preview", json={})
            enabled = await client.patch("/api/setup/home-context-choice", json={"enabled": True})

    assert preview.status_code == 200
    assert preview.json()["fresh"] is True
    assert preview.json()["status"] == "empty"
    assert app.state.station_state.ha_context == ""
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert "MAMMAMIRADIO_HA_CONTEXT_ENABLED" not in os.environ
    assert app.state.config.homeassistant.context_enabled is True
    assert (await app.state.first_listen_store.load()).privacy_complete is True
    persist.assert_awaited_once_with(app.state.config, True)


@pytest.mark.asyncio
async def test_home_context_preview_shares_one_bounded_in_flight_fetch(tmp_path):
    from mammamiradio.home.ha_context import HomeContext, HomeContextPreviewResult

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.homeassistant.enabled = True
    app.state.config.homeassistant.url = "http://supervisor/core"
    app.state.config.ha_token = "supervisor-token"
    app.state.station_state.home_authorization = HomeAuthorization.narrow()
    started = asyncio.Event()
    both_requests_passed_audio_gate = asyncio.Event()
    audio_gate_calls = 0

    async def open_audio_gate(_app_state):
        nonlocal audio_gate_calls
        audio_gate_calls += 1
        if audio_gate_calls == 2:
            both_requests_passed_audio_gate.set()
        return True

    async def slow_preview(*_args, **_kwargs):
        started.set()
        await both_requests_passed_audio_gate.wait()
        return HomeContextPreviewResult(
            kind="fresh",
            context=HomeContext(authorization_mode=HomeAuthorizationMode.NARROW.value, timestamp=time.time()),
            duration_seconds=0.01,
        )

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with (
        patch(
            "mammamiradio.web.streamer.fetch_home_context_preview",
            new=AsyncMock(side_effect=slow_preview),
        ) as fetch,
        patch(
            "mammamiradio.web.streamer._first_listen_audio_gate_open",
            new=AsyncMock(side_effect=open_audio_gate),
        ),
    ):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            first = asyncio.create_task(client.post("/api/setup/home-context-preview", json={}))
            await started.wait()
            second = asyncio.create_task(client.post("/api/setup/home-context-preview", json={}))
            first_response, second_response = await asyncio.gather(first, second)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_home_context_preview_total_deadline_returns_fixed_failure_without_spawning_again(
    tmp_path,
    monkeypatch,
):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.homeassistant.enabled = True
    app.state.config.homeassistant.url = "http://supervisor/core"
    app.state.config.ha_token = "supervisor-token"
    app.state.station_state.home_authorization = HomeAuthorization.narrow()
    release = asyncio.Event()

    async def stuck_preview(*_args, **_kwargs):
        await release.wait()

    monkeypatch.setattr("mammamiradio.web.streamer._HOME_PREVIEW_TOTAL_TIMEOUT_SECONDS", 0.01)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch(
        "mammamiradio.web.streamer.fetch_home_context_preview",
        new=AsyncMock(side_effect=stuck_preview),
    ) as fetch:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            first = await client.post("/api/setup/home-context-preview", json={})
            second = await client.post("/api/setup/home-context-preview", json={})
        task = app.state._home_context_preview_task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert first.status_code == 503
    assert first.json()["error"]["code"] == "preview_unavailable"
    assert second.status_code == 503
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_enabled_home_context_can_retry_privacy_receipt_after_a_fresh_preview(tmp_path):
    from dataclasses import replace

    from mammamiradio.core.first_listen import (
        FirstListenReceiptStore,
        FirstListenReceiptUnavailableError,
    )
    from mammamiradio.home.ha_context import HomeContext, HomeContextPreviewResult

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.homeassistant.enabled = True
    app.state.config.homeassistant.url = "http://supervisor/core"
    app.state.config.homeassistant.context_enabled = False
    app.state.config.ha_token = "supervisor-token"
    app.state.station_state.home_authorization = HomeAuthorization.narrow()
    store = FirstListenReceiptStore(tmp_path)
    accepted = await store.record_accepted("media_player.kitchen", accepted_at=time.time() - 2)
    heard = await store.verify(accepted.accepted_attempt_id or "", heard=True, verified_at=time.time() - 1)
    reviewed = replace(heard, privacy_reviewed_at=time.time())
    app.state.first_listen_store = store
    app.state.first_listen_receipt = heard
    app.state.first_listen_install_origin = FirstListenInstallOriginV1(FirstListenInstallOriginStatus.FRESH)
    preview_result = HomeContextPreviewResult(
        kind="fresh",
        context=HomeContext(authorization_mode=HomeAuthorizationMode.NARROW.value, timestamp=time.time()),
        duration_seconds=0.01,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with (
        patch("mammamiradio.web.streamer.fetch_home_context_preview", new=AsyncMock(return_value=preview_result)),
        patch("mammamiradio.web.streamer._persist_home_context_choice", new=AsyncMock()) as persist,
        patch.object(
            store,
            "record_privacy_reviewed",
            new=AsyncMock(
                side_effect=[
                    FirstListenReceiptUnavailableError("disk unavailable"),
                    reviewed,
                ]
            ),
        ),
    ):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            assert (await client.post("/api/setup/home-context-preview", json={})).status_code == 200
            first = await client.patch("/api/setup/home-context-choice", json={"enabled": True})
            assert (await client.post("/api/setup/home-context-preview", json={})).status_code == 200
            second = await client.patch("/api/setup/home-context-choice", json={"enabled": True})

    assert first.status_code == 503
    assert first.json()["error"]["code"] == "privacy_receipt_unavailable"
    assert app.state.config.homeassistant.context_enabled is True
    assert second.status_code == 200
    assert second.json()["privacy_reviewed"] is True
    assert app.state.first_listen_receipt.privacy_complete is True
    assert persist.await_count == 2


@pytest.mark.asyncio
async def test_detached_home_preview_projects_existing_personal_moment_opt_in_as_effective(tmp_path):
    """Detached preview preserves an eligible consent control without publishing the row."""
    from mammamiradio.core.first_listen import FirstListenReceiptStore
    from mammamiradio.home.entity_policy import set_personal_moment_enabled
    from mammamiradio.home.ha_context import HomeContext, HomeContextPreviewResult, ScoredEntity

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.homeassistant.enabled = True
    app.state.config.homeassistant.url = "http://supervisor/core"
    app.state.config.homeassistant.context_enabled = False
    app.state.config.ha_token = "supervisor-token"
    app.state.station_state.home_authorization = HomeAuthorization.narrow()
    app.state.first_listen_store = FirstListenReceiptStore(tmp_path)
    entity_id = "binary_sensor.office_presence"
    set_personal_moment_enabled(
        tmp_path,
        entity_id,
        True,
        label="Office presence",
        domain="binary_sensor",
        area="Office",
        now=100.0,
    )
    presence = ScoredEntity(
        entity_id=entity_id,
        area="Office",
        domain="binary_sensor",
        score=1.0,
        raw_state={"state": "on", "attributes": {"device_class": "presence"}},
        label_it="Presenza ufficio",
        label_en="Office presence",
        summary_line="Office presence: active",
    )
    preview_result = HomeContextPreviewResult(
        kind="fresh",
        context=HomeContext(
            scored=[presence],
            authorization_mode=HomeAuthorizationMode.NARROW.value,
            timestamp=time.time(),
        ),
        duration_seconds=0.01,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch(
        "mammamiradio.web.streamer.fetch_home_context_preview",
        new=AsyncMock(return_value=preview_result),
    ):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            response = await client.post("/api/setup/home-context-preview", json={})

    assert response.status_code == 200
    body = response.json()
    row = next(item for item in body["entities"] if item["entity_id"] == entity_id)
    assert row["row_state"] == "preview_only"
    assert row["personal_moment_eligible"] is True
    assert row["personal_moment_enabled"] is True
    assert row["personal_moment_effective"] is True
    assert row["sent_to_prompt"] is False
    assert body["sent_now"] == []
    assert app.state.station_state.ha_context == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_ids", "expected_status", "expected_value", "expected_useful"),
    [
        (["sun.ambient"], "ambient_only", "ambient_only", False),
        (["sun.ambient", "weather.ambient"], "ready", "useful", True),
    ],
)
async def test_detached_home_preview_separates_privacy_safe_from_product_useful(
    tmp_path,
    entity_ids,
    expected_status,
    expected_value,
    expected_useful,
):
    """Generic daylight stays disclosed without being sold as personalization."""
    from mammamiradio.core.first_listen import FirstListenReceiptStore
    from mammamiradio.home.ha_context import HomeContext, HomeContextPreviewResult, ScoredEntity

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.homeassistant.enabled = True
    app.state.config.homeassistant.url = "http://supervisor/core"
    app.state.config.homeassistant.context_enabled = False
    app.state.config.ha_token = "supervisor-token"
    app.state.station_state.home_authorization = HomeAuthorization.narrow()
    app.state.first_listen_store = FirstListenReceiptStore(tmp_path)
    scored = [
        ScoredEntity(
            entity_id=entity_id,
            area="",
            domain=entity_id.split(".", 1)[0],
            score=1.0,
            raw_state={"state": "sunny" if entity_id.startswith("weather.") else "above_horizon"},
            label_it="Meteo" if entity_id.startswith("weather.") else "Luce del giorno",
            label_en="Weather" if entity_id.startswith("weather.") else "Daylight",
            summary_line="Sunny" if entity_id.startswith("weather.") else "Daylight: above horizon",
        )
        for entity_id in entity_ids
    ]
    preview_result = HomeContextPreviewResult(
        kind="fresh",
        context=HomeContext(
            scored=scored,
            authorization_mode=HomeAuthorizationMode.NARROW.value,
            timestamp=time.time(),
        ),
        duration_seconds=0.01,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch(
        "mammamiradio.web.streamer.fetch_home_context_preview",
        new=AsyncMock(return_value=preview_result),
    ):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            response = await client.post("/api/setup/home-context-preview", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == expected_status
    assert body["context_value"] == expected_value
    assert body["useful_context"] is expected_useful
    assert [row["entity_id"] for row in body["entities"]] == entity_ids
    assert body["sent_now"] == []
    assert app.state.station_state.ha_context == ""


@pytest.mark.asyncio
async def test_keep_home_context_off_needs_no_preview_and_stays_live_off_when_save_fails(tmp_path):
    from mammamiradio.core.first_listen import FirstListenReceiptStore

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.homeassistant.context_enabled = True
    app.state.first_listen_store = FirstListenReceiptStore(tmp_path)
    app.state.first_listen_install_origin = FirstListenInstallOriginV1(FirstListenInstallOriginStatus.FRESH)
    state = app.state.station_state
    state.ha_context = "Private retained context"
    state.ha_context_last_updated = 123.0
    state.ha_scored_entities = [{"entity_id": "switch.private"}]
    state.home_context_policy_generation = 4
    segment = Segment(
        type=SegmentType.BANTER,
        path=tmp_path / "private.mp3",
        metadata={"queue_id": "private-queued", "home_context_generation": 4},
    )
    app.state.queue.put_nowait(segment)
    state.queued_segments = [{"id": "private-queued", "type": "banter"}]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with (
        patch(
            "mammamiradio.web.streamer._persist_home_context_choice",
            new=AsyncMock(side_effect=OSError("read only")),
        ),
        patch(
            "mammamiradio.web.streamer.invalidate_all_home_context",
            side_effect=OSError("cache read only"),
        ),
    ):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            response = await client.patch("/api/setup/home-context-choice", json={"enabled": False})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "privacy_persist_failed"
    assert response.json()["live_off"] is True
    assert app.state.config.homeassistant.context_enabled is False
    assert state.home_context_policy_generation == 5
    assert state.ha_context == ""
    assert state.ha_context_last_updated == 0.0
    assert app.state.queue.empty()
    assert state.queued_segments == []


@pytest.mark.asyncio
async def test_home_context_disable_invalidates_memory_before_async_cleanup(tmp_path):
    from mammamiradio.web.streamer import _disable_home_context_runtime

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.homeassistant.context_enabled = True
    state = app.state.station_state
    state.home_context_policy_generation = 2
    events: list[str] = []
    revoked_snapshot: tuple[asyncio.Task, ...] = ()

    def _revoke_memory() -> tuple[asyncio.Task, ...]:
        events.append("memory_epoch")
        return revoked_snapshot

    async def _drain_memory(tasks: tuple[asyncio.Task, ...]) -> None:
        assert tasks is revoked_snapshot
        events.append("memory_drain")

    async def _revoke_context_fetch() -> None:
        events.append("context_fetch")

    def _suspend_context_fetch() -> None:
        events.append("context_suspend")

    state.ha_context_refresh_mailbox = SimpleNamespace(
        suspend=_suspend_context_fetch,
        revoke=_revoke_context_fetch,
    )
    with (
        patch("mammamiradio.web.streamer.revoke_home_memory_extractions", side_effect=_revoke_memory),
        patch(
            "mammamiradio.web.streamer.drain_revoked_home_memory_extractions",
            new=AsyncMock(side_effect=_drain_memory),
        ),
        patch("mammamiradio.web.streamer.invalidate_all_home_context"),
    ):
        await _disable_home_context_runtime(app.state)

    assert events[:4] == ["memory_epoch", "context_suspend", "context_fetch", "memory_drain"]
    assert state.home_context_policy_generation == 3


def test_home_context_disable_retires_only_pending_home_interrupt(tmp_path):
    from mammamiradio.core.models import ChaosSubtype
    from mammamiradio.web.streamer import _retire_pending_home_interrupt

    state = StationState()
    bridge = tmp_path / "pending-home-interrupt.mp3"
    bridge.write_bytes(b"ID3")
    state.interrupt_slot = bridge
    state.interrupt_slot_ephemeral = True
    state.interrupt_slot_source = "ha:binary_sensor.kitchen_presence"
    state.interrupt_slot_home_context_generation = 4
    state.chaos_pending = ChaosSubtype.URGENT_INTERRUPT
    urgent_force_revision = state.set_force_next(SegmentType.BANTER)
    state.urgent_interrupt_force_next_revision = urgent_force_revision

    assert _retire_pending_home_interrupt(state) is True
    assert not bridge.exists()
    assert state.interrupt_slot is None
    assert state.interrupt_slot_ephemeral is False
    assert state.interrupt_slot_source == ""
    assert state.interrupt_slot_home_context_generation is None
    assert state.chaos_pending is None
    assert state.force_next is None
    assert state.force_next_revision > urgent_force_revision
    assert state.urgent_interrupt_force_next_revision is None

    operator_bridge = tmp_path / "pending-operator-interrupt.mp3"
    operator_bridge.write_bytes(b"ID3")
    state.interrupt_slot = operator_bridge
    state.interrupt_slot_ephemeral = True
    state.interrupt_slot_source = "operator"
    state.force_next = SegmentType.BANTER
    state.operator_force_pending = SegmentType.BANTER

    assert _retire_pending_home_interrupt(state) is False
    assert operator_bridge.exists()
    assert state.interrupt_slot is operator_bridge
    assert state.force_next is SegmentType.BANTER


def test_home_context_disable_preserves_newer_operator_force(tmp_path):
    from mammamiradio.core.models import ChaosSubtype
    from mammamiradio.web.streamer import _retire_pending_home_interrupt

    state = StationState()
    bridge = tmp_path / "pending-home-interrupt.mp3"
    bridge.write_bytes(b"ID3")
    state.interrupt_slot = bridge
    state.interrupt_slot_ephemeral = True
    state.interrupt_slot_source = "ha:binary_sensor.kitchen_presence"
    state.chaos_pending = ChaosSubtype.URGENT_INTERRUPT
    state.urgent_interrupt_force_next_revision = state.set_force_next(SegmentType.BANTER)
    operator_force_revision = state.set_force_next(SegmentType.AD)
    state.operator_force_pending = SegmentType.AD

    assert _retire_pending_home_interrupt(state) is True
    assert not bridge.exists()
    assert state.interrupt_slot is None
    assert state.chaos_pending is None
    assert state.urgent_interrupt_force_next_revision is None
    assert state.force_next is SegmentType.AD
    assert state.force_next_revision == operator_force_revision
    assert state.operator_force_pending is SegmentType.AD


def test_home_context_disable_retires_unknown_source_interrupt_fail_closed(tmp_path):
    from mammamiradio.web.streamer import _retire_pending_home_interrupt

    state = StationState()
    bridge = tmp_path / "pending-unknown-interrupt.mp3"
    bridge.write_bytes(b"ID3")
    state.interrupt_slot = bridge
    state.interrupt_slot_ephemeral = True
    state.interrupt_slot_source = "legacy_unknown"
    state.interrupt_slot_home_context_generation = 4

    # Unknown provenance fails closed as Home-owned, same as the tagging rule.
    assert _retire_pending_home_interrupt(state) is True
    assert not bridge.exists()
    assert state.interrupt_slot is None
    assert state.interrupt_slot_source == ""
    assert state.interrupt_slot_home_context_generation is None


def test_home_context_disable_without_pending_interrupt_preserves_unrelated_state():
    from mammamiradio.core.models import ChaosSubtype
    from mammamiradio.web.streamer import _retire_pending_home_interrupt

    state = StationState()
    state.chaos_pending = ChaosSubtype.URGENT_INTERRUPT
    state.force_next = SegmentType.BANTER

    # No interrupt is pending, so the fail-closed rule must not disturb
    # unrelated chaos or forced-banter state.
    assert _retire_pending_home_interrupt(state) is False
    assert state.chaos_pending is ChaosSubtype.URGENT_INTERRUPT
    assert state.force_next is SegmentType.BANTER


def test_clear_global_home_context_runtime_state_clears_unknown_source_directive():
    from mammamiradio.web.streamer import _clear_global_home_context_runtime_state

    state = StationState()
    state.ha_pending_directive = "Mention the private kitchen light."
    state.ha_pending_directive_moment_id = "private-moment"
    state.ha_pending_directive_source = "legacy_unknown"

    _clear_global_home_context_runtime_state(state)

    assert state.ha_pending_directive == ""
    assert state.ha_pending_directive_moment_id == ""
    assert state.ha_pending_directive_source == ""


def test_clear_global_home_context_runtime_state_preserves_operator_directive():
    from mammamiradio.web.streamer import _clear_global_home_context_runtime_state

    state = StationState()
    state.ha_pending_directive = "Play the explicit studio bit next."
    state.ha_pending_directive_source = "operator"

    _clear_global_home_context_runtime_state(state)

    assert state.ha_pending_directive == "Play the explicit studio bit next."
    assert state.ha_pending_directive_source == "operator"


@pytest.mark.asyncio
async def test_home_context_disable_contains_every_best_effort_cleanup_failure(tmp_path, caplog):
    from mammamiradio.web.streamer import _disable_home_context_runtime

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.homeassistant.context_enabled = True
    app.state.home_context_preview_proof = object()
    state = app.state.station_state
    state.home_context_policy_generation = 8
    coordinator = SimpleNamespace(
        suspend=MagicMock(side_effect=RuntimeError("suspend failed")),
        revoke=AsyncMock(side_effect=RuntimeError("revoke failed")),
    )
    state.ha_context_refresh_mailbox = coordinator
    ledger = SimpleNamespace(
        buckets={"private": object()},
        _dirty=False,
        save_if_dirty=MagicMock(side_effect=RuntimeError("ledger save failed")),
    )
    state.evening_ledger = ledger

    with (
        caplog.at_level(logging.WARNING),
        patch("mammamiradio.web.streamer.revoke_home_memory_extractions", return_value=()),
        patch("mammamiradio.web.streamer.invalidate_label_generation", return_value=()),
        patch(
            "mammamiradio.web.streamer.drain_revoked_home_memory_extractions",
            new=AsyncMock(side_effect=RuntimeError("memory drain failed")),
        ),
        patch(
            "mammamiradio.web.streamer.drain_invalidated_label_generation",
            new=AsyncMock(side_effect=RuntimeError("label drain failed")),
        ),
        patch(
            "mammamiradio.web.streamer.invalidate_all_home_context",
            side_effect=RuntimeError("cache invalidation failed"),
        ),
        patch("mammamiradio.web.streamer.reset_scene_namer_cache"),
    ):
        purged = await _disable_home_context_runtime(app.state)

    assert purged == 0
    assert app.state.config.homeassistant.context_enabled is False
    assert state.home_context_policy_generation == 9
    assert app.state.home_context_preview_proof is None
    assert ledger.buckets == {}
    assert ledger._dirty is True
    coordinator.suspend.assert_called_once_with()
    coordinator.revoke.assert_awaited_once_with()
    ledger.save_if_dirty.assert_called_once_with(tmp_path)
    assert {
        "Home-context refresh suspension failed during revocation",
        "Home-context refresh cancellation failed during revocation",
        "Home memory-extraction cancellation failed during revocation",
        "Home label-generation cancellation failed during revocation",
        "Home-context cache cleanup failed during revocation",
        "Home-context running-gag cleanup could not be saved",
    }.issubset({record.message for record in caplog.records})


def test_home_context_runtime_enable_resumes_refresh_mailbox():
    from mammamiradio.web.streamer import _enable_home_context_runtime

    app = _make_test_app()
    coordinator = SimpleNamespace(enable=MagicMock())
    app.state.station_state.ha_context_refresh_mailbox = coordinator
    app.state.station_state.ha_context_refresh_stage = "disabled"
    app.state.config.homeassistant.context_enabled = False

    _enable_home_context_runtime(app.state)

    assert app.state.config.homeassistant.context_enabled is True
    assert app.state.station_state.ha_context_refresh_stage == "idle"
    coordinator.enable.assert_called_once_with()


def test_home_entity_metadata_prefers_cache_then_runtime_then_safe_default(tmp_path):
    from mammamiradio.web.streamer import _home_entity_metadata

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    state = app.state.station_state
    cached_context = SimpleNamespace(
        scored=[
            SimpleNamespace(to_status_dict=lambda: {"entity_id": "sensor.other"}),
            SimpleNamespace(
                to_status_dict=lambda: {
                    "entity_id": "binary_sensor.kitchen_presence",
                    "label": "Kitchen presence",
                    "domain": "binary_sensor",
                    "area": "Kitchen",
                }
            ),
        ]
    )

    with patch("mammamiradio.web.streamer.get_cached_home_context", return_value=cached_context):
        cached = _home_entity_metadata(state, app.state.config, "binary_sensor.kitchen_presence")

    assert cached == {"label": "Kitchen presence", "domain": "binary_sensor", "area": "Kitchen"}

    state.ha_scored_entities = [
        {"entity_id": "sensor.other"},
        {
            "entity_id": "switch.espresso_machine",
            "label": "",
            "domain": "",
            "area": None,
        },
    ]
    with patch("mammamiradio.web.streamer.get_cached_home_context", return_value=None):
        runtime = _home_entity_metadata(state, app.state.config, "switch.espresso_machine")
        missing = _home_entity_metadata(state, app.state.config, "light.unlisted")

    assert runtime == {"label": "switch.espresso_machine", "domain": "switch", "area": ""}
    assert missing == {"label": "light.unlisted", "domain": "light", "area": ""}


@pytest.mark.asyncio
async def test_home_context_enable_rejects_preview_after_policy_revision_changes(tmp_path):
    from mammamiradio.core.first_listen import FirstListenReceiptStore
    from mammamiradio.home.entity_policy import set_entity_muted
    from mammamiradio.home.ha_context import HomeContext, HomeContextPreviewResult

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.homeassistant.enabled = True
    app.state.config.homeassistant.url = "http://supervisor/core"
    app.state.config.homeassistant.context_enabled = False
    app.state.config.ha_token = "supervisor-token"
    app.state.station_state.home_authorization = HomeAuthorization.narrow()
    app.state.first_listen_store = FirstListenReceiptStore(tmp_path)
    preview_result = HomeContextPreviewResult(
        kind="fresh",
        context=HomeContext(authorization_mode=HomeAuthorizationMode.NARROW.value, timestamp=time.time()),
        duration_seconds=0.01,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch(
        "mammamiradio.web.streamer.fetch_home_context_preview",
        new=AsyncMock(return_value=preview_result),
    ):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            preview = await client.post("/api/setup/home-context-preview", json={})
            set_entity_muted(tmp_path, "weather.forecast_home", True)
            enabled = await client.patch("/api/setup/home-context-choice", json={"enabled": True})

    assert preview.status_code == 200
    assert enabled.status_code == 409
    assert enabled.json()["error"]["code"] == "preview_required"
    assert app.state.config.homeassistant.context_enabled is False


@pytest.mark.asyncio
async def test_home_context_enable_compensates_policy_change_while_choice_persists(tmp_path):
    """Out-of-band policy drift during durable enable requires a new preview."""
    from mammamiradio.core.first_listen import FirstListenReceiptStore
    from mammamiradio.home.entity_policy import set_entity_muted
    from mammamiradio.home.ha_context import HomeContext, HomeContextPreviewResult

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.homeassistant.enabled = True
    app.state.config.homeassistant.url = "http://supervisor/core"
    app.state.config.homeassistant.context_enabled = False
    app.state.config.ha_token = "supervisor-token"
    app.state.station_state.home_authorization = HomeAuthorization.narrow()
    app.state.first_listen_store = FirstListenReceiptStore(tmp_path)
    entity_id = "weather.forecast_home"
    set_entity_muted(tmp_path, entity_id, True)
    preview_result = HomeContextPreviewResult(
        kind="fresh",
        context=HomeContext(authorization_mode=HomeAuthorizationMode.NARROW.value, timestamp=time.time()),
        duration_seconds=0.01,
    )
    persist_started = asyncio.Event()
    allow_persist = asyncio.Event()
    persisted_choices: list[bool] = []

    async def persist_choice(_config, enabled: bool) -> None:
        persisted_choices.append(enabled)
        if enabled:
            persist_started.set()
            await allow_persist.wait()

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with (
        patch("mammamiradio.web.streamer.fetch_home_context_preview", new=AsyncMock(return_value=preview_result)),
        patch(
            "mammamiradio.web.streamer._persist_home_context_choice",
            new=AsyncMock(side_effect=persist_choice),
        ),
    ):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            preview = await client.post("/api/setup/home-context-preview", json={})
            enable_task = asyncio.create_task(client.patch("/api/setup/home-context-choice", json={"enabled": True}))
            await asyncio.wait_for(persist_started.wait(), timeout=1)
            try:
                widened_policy = await asyncio.to_thread(
                    set_entity_muted,
                    tmp_path,
                    entity_id,
                    False,
                )
            finally:
                allow_persist.set()
            enabled = await asyncio.wait_for(enable_task, timeout=1)

    assert preview.status_code == 200
    assert entity_id not in widened_policy["muted"]
    assert enabled.status_code == 409
    assert enabled.json()["error"]["code"] == "preview_required"
    assert enabled.json()["enabled"] is False
    assert enabled.json()["persisted"] is True
    assert app.state.config.homeassistant.context_enabled is False
    assert app.state.home_context_choice_explicit is True
    assert app.state.home_context_preview_proof is None
    assert persisted_choices == [True, False]


@pytest.mark.asyncio
async def test_home_context_enable_serializes_entity_policy_widening(tmp_path):
    """The supported policy API cannot commit between proof and enable."""
    from mammamiradio.core.first_listen import FirstListenReceiptStore
    from mammamiradio.home.entity_policy import set_entity_muted
    from mammamiradio.home.ha_context import HomeContext, HomeContextPreviewResult

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.homeassistant.enabled = True
    app.state.config.homeassistant.url = "http://supervisor/core"
    app.state.config.homeassistant.context_enabled = False
    app.state.config.ha_token = "supervisor-token"
    app.state.station_state.home_authorization = HomeAuthorization.narrow()
    app.state.first_listen_store = FirstListenReceiptStore(tmp_path)
    entity_id = "weather.forecast_home"
    set_entity_muted(tmp_path, entity_id, True)
    preview_result = HomeContextPreviewResult(
        kind="fresh",
        context=HomeContext(authorization_mode=HomeAuthorizationMode.NARROW.value, timestamp=time.time()),
        duration_seconds=0.01,
    )
    persist_started = asyncio.Event()
    allow_persist = asyncio.Event()

    async def persist_choice(_config, enabled: bool) -> None:
        if enabled:
            persist_started.set()
            await allow_persist.wait()

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with (
        patch("mammamiradio.web.streamer.fetch_home_context_preview", new=AsyncMock(return_value=preview_result)),
        patch(
            "mammamiradio.web.streamer._persist_home_context_choice",
            new=AsyncMock(side_effect=persist_choice),
        ),
    ):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            preview = await client.post("/api/setup/home-context-preview", json={})
            enable_task = asyncio.create_task(client.patch("/api/setup/home-context-choice", json={"enabled": True}))
            await asyncio.wait_for(persist_started.wait(), timeout=1)
            widening_task = asyncio.create_task(
                client.patch(
                    "/api/homeassistant/entity-policy",
                    json={"entity_id": entity_id, "muted": False},
                )
            )
            await asyncio.sleep(0)
            assert not widening_task.done()
            allow_persist.set()
            enabled = await asyncio.wait_for(enable_task, timeout=1)
            widened = await asyncio.wait_for(widening_task, timeout=1)

    assert preview.status_code == 200
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert widened.status_code == 200
    assert widened.json()["muted"] is False
    assert app.state.config.homeassistant.context_enabled is True


@pytest.mark.asyncio
async def test_home_context_preview_rejects_policy_generation_change_during_fetch(tmp_path):
    from mammamiradio.home.ha_context import HomeContext, HomeContextPreviewResult

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.homeassistant.enabled = True
    app.state.config.homeassistant.url = "http://supervisor/core"
    app.state.config.ha_token = "supervisor-token"
    app.state.station_state.home_authorization = HomeAuthorization.narrow()
    app.state.station_state.home_context_policy_generation = 3

    async def fetch_after_policy_change(*_args, **_kwargs):
        app.state.station_state.home_context_policy_generation += 1
        return HomeContextPreviewResult(
            kind="fresh",
            context=HomeContext(
                authorization_mode=HomeAuthorizationMode.NARROW.value,
                timestamp=time.time(),
            ),
            duration_seconds=0.01,
        )

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch(
        "mammamiradio.web.streamer.fetch_home_context_preview",
        new=AsyncMock(side_effect=fetch_after_policy_change),
    ) as fetch:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            response = await client.post("/api/setup/home-context-preview", json={})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "preview_unavailable"
    assert app.state.station_state.home_context_policy_generation == 4
    assert getattr(app.state, "home_context_preview_proof", None) is None
    fetch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidation", ["expired", "ha_config", "authorization_mode"])
async def test_home_context_enable_rejects_stale_preview_proof(tmp_path, invalidation):
    """Every server-bound preview proof dimension is revalidated on Enable."""
    from dataclasses import replace

    from mammamiradio.core.first_listen import FirstListenReceiptStore
    from mammamiradio.home.ha_context import HomeContext, HomeContextPreviewResult

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.homeassistant.enabled = True
    app.state.config.homeassistant.url = "http://supervisor/core"
    app.state.config.homeassistant.context_enabled = False
    app.state.config.ha_token = "supervisor-token"
    app.state.station_state.home_authorization = HomeAuthorization.narrow()
    app.state.first_listen_store = FirstListenReceiptStore(tmp_path)
    preview_result = HomeContextPreviewResult(
        kind="fresh",
        context=HomeContext(authorization_mode=HomeAuthorizationMode.NARROW.value, timestamp=time.time()),
        duration_seconds=0.01,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with (
        patch("mammamiradio.web.streamer.fetch_home_context_preview", new=AsyncMock(return_value=preview_result)),
        patch("mammamiradio.web.streamer._persist_home_context_choice", new=AsyncMock()) as persist,
    ):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            preview = await client.post("/api/setup/home-context-preview", json={})
            assert preview.status_code == 200

            if invalidation == "expired":
                proof = app.state.home_context_preview_proof
                app.state.home_context_preview_proof = replace(proof, expires_at=0.0)
            elif invalidation == "ha_config":
                app.state.config.ha_token = "rotated-supervisor-token"
            else:
                app.state.station_state.home_authorization = HomeAuthorization.legacy()

            enabled = await client.patch("/api/setup/home-context-choice", json={"enabled": True})

    assert enabled.status_code == 409
    assert enabled.json()["error"]["code"] == "preview_required"
    assert app.state.config.homeassistant.context_enabled is False
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_home_context_preview_never_unlocks_enable(tmp_path):
    """A failed fresh fetch cannot create the server proof required by Enable."""
    from mammamiradio.core.first_listen import FirstListenReceiptStore
    from mammamiradio.home.ha_context import HomeContext, HomeContextPreviewResult

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.homeassistant.enabled = True
    app.state.config.homeassistant.url = "http://supervisor/core"
    app.state.config.homeassistant.context_enabled = False
    app.state.config.ha_token = "supervisor-token"
    app.state.station_state.home_authorization = HomeAuthorization.narrow()
    app.state.first_listen_store = FirstListenReceiptStore(tmp_path)
    failed_result = HomeContextPreviewResult(
        kind="failed",
        context=HomeContext(authorization_mode=HomeAuthorizationMode.NARROW.value),
        duration_seconds=0.01,
        error_code="ha_unreachable",
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with (
        patch("mammamiradio.web.streamer.fetch_home_context_preview", new=AsyncMock(return_value=failed_result)),
        patch("mammamiradio.web.streamer._persist_home_context_choice", new=AsyncMock()) as persist,
    ):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            preview = await client.post("/api/setup/home-context-preview", json={})
            enabled = await client.patch("/api/setup/home-context-choice", json={"enabled": True})

    assert preview.status_code == 503
    assert preview.json()["error"]["code"] == "ha_unreachable"
    assert getattr(app.state, "home_context_preview_proof", None) is None
    assert enabled.status_code == 409
    assert enabled.json()["error"]["code"] == "preview_required"
    assert app.state.config.homeassistant.context_enabled is False
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_keep_home_context_off_clears_all_runtime_context_and_generated_breaks(tmp_path):
    """Normal global revocation clears every context owner and generated break kind."""
    import mammamiradio.home.ha_context as ha_context
    from mammamiradio.core.first_listen import FirstListenReceiptStore
    from mammamiradio.home.context_director import DirectorObservation, HomeContextDirector
    from mammamiradio.home.evening_memory import EveningLedger, GagBucket
    from mammamiradio.home.ha_context import HomeContext, HomeRegistrySnapshot

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.config.homeassistant.context_enabled = True
    app.state.first_listen_store = FirstListenReceiptStore(tmp_path)
    state = app.state.station_state
    state.home_authorization = HomeAuthorization.narrow()
    state.home_context_policy_generation = 7
    state.ha_context = "Private retained context"
    state.ha_events_summary = "Private event"
    state.ha_home_mood = "Private mood"
    state.ha_pending_directive = "Mention the kitchen"
    state.ha_pending_directive_source = "ha"
    state.ha_running_gag = "Private running gag"
    state.ha_running_gag_key = "private-gag"
    state.last_banter_home_fact = MagicMock()

    director = HomeContextDirector()
    director.observe(
        [
            DirectorObservation(
                entity_id="weather.forecast_home",
                domain="weather",
                state="sunny",
                score=9.0,
                temperature_c=24.0,
            )
        ],
        policy_revision=0,
    )
    fact = director.select()
    assert fact is not None
    assert director.reserve("home-banter", fact)
    state.home_context_director = director

    ledger = EveningLedger(session_id=1, started_at=1.0, last_active=1.0)
    ledger.buckets["private-gag"] = GagBucket(
        "switch.private",
        "Private switch",
        "off",
        "on",
        count=3,
        last_ts=1.0,
    )
    ledger._dirty = True
    state.evening_ledger = ledger

    generated = [
        Segment(
            type=segment_type,
            path=tmp_path / f"private-{index}.mp3",
            metadata={
                "queue_id": queue_id,
                "home_context_generation": 7,
                "home_fact_id": fact.fact_id,
            },
            ephemeral=False,
        )
        for index, (segment_type, queue_id) in enumerate(
            (
                (SegmentType.BANTER, "home-banter"),
                (SegmentType.AD, "home-ad"),
                (SegmentType.NEWS_FLASH, "home-news"),
            )
        )
    ]
    safe_music = Segment(
        type=SegmentType.MUSIC,
        path=tmp_path / "safe-music.mp3",
        metadata={"queue_id": "safe-music"},
        ephemeral=False,
    )
    for segment in [*generated, safe_music]:
        app.state.queue.put_nowait(segment)
    state.queued_segments = [
        {"id": "home-banter", "type": "banter"},
        {"id": "home-ad", "type": "ad"},
        {"id": "home-news", "type": "news_flash"},
        {"id": "safe-music", "type": "music"},
    ]

    retained = HomeContext(
        raw_states={"switch.private": {"state": "on", "attributes": {}}},
        authorization_mode=HomeAuthorizationMode.NARROW.value,
        timestamp=1.0,
    )
    registry_cache = tmp_path / "ha_registry.json"
    registry_cache.write_text("{}", encoding="utf-8")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with (
        patch.object(ha_context, "_ha_cache", retained),
        patch.object(ha_context, "_radio_event_state_cache", {"switch.private": {"state": "on"}}),
        patch.object(ha_context, "_ritual_recipe_state_cache", {"switch.private": {"state": "on"}}),
        patch.object(ha_context, "_ha_registry_snapshot_cache", HomeRegistrySnapshot(source="memory")),
        patch.object(ha_context, "_ha_registry_fetched_at", 1.0),
        patch.object(ha_context, "_weather_forecast_cache", "Private weather"),
        patch.object(ha_context, "_weather_forecast_cache_en", "Private weather"),
        patch.object(ha_context, "_weather_forecast_fetched_at", 1.0),
        patch.object(ha_context, "_home_context_invalidation_generation", 11),
        patch("mammamiradio.web.streamer._persist_home_context_choice", new=AsyncMock()) as persist,
    ):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=ACTIVE_SETUP_HEADERS,
        ) as client:
            response = await client.patch("/api/setup/home-context-choice", json={"enabled": False})

        assert ha_context._ha_cache is None
        assert ha_context._radio_event_state_cache == {}
        assert ha_context._ritual_recipe_state_cache == {}
        assert ha_context._ha_registry_snapshot_cache is None
        assert ha_context._weather_forecast_cache == ""
        assert ha_context._weather_forecast_cache_en == ""
        assert ha_context._home_context_invalidation_generation == 12

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["purged_pending_segments"] == 3
    assert app.state.config.homeassistant.context_enabled is False
    assert state.home_context_policy_generation == 8
    assert state.ha_context == ""
    assert state.ha_events_summary == ""
    assert state.ha_home_mood == ""
    assert state.ha_pending_directive == ""
    assert state.ha_running_gag == ""
    assert state.last_banter_home_fact is None
    assert state.home_context_director is not director
    assert state.home_context_director.admin_status()["reserved_count"] == 0
    assert ledger.buckets == {}
    assert EveningLedger.load(tmp_path).buckets == {}
    assert not registry_cache.exists()
    assert app.state.queue.qsize() == 1
    assert app.state.queue.get_nowait() is safe_music
    assert state.queued_segments == [{"id": "safe-music", "type": "music"}]
    assert (await app.state.first_listen_store.load()).privacy_complete is True
    persist.assert_awaited_once_with(app.state.config, False)


@pytest.mark.asyncio
async def test_public_status_skip_hint_does_not_clear_continuity_slot(tmp_path):
    """A listener /public-status poll must never clear the reserved dead-air slot.

    skip_would_bridge evaluates runway on an unauthenticated GET. With an empty
    queue it consults the continuity slot; a transient missing/partial slot file
    must not let that read-only poll clear the safety slot (self_heal=False).
    """
    app = _make_test_app()
    state = app.state.station_state
    missing_slot_path = tmp_path / "vanished-slot.mp3"  # deliberately never created
    reserved_slot = Segment(
        type=SegmentType.MUSIC,
        path=missing_slot_path,
        duration_sec=180.0,
        metadata={"artist": "Slot Artist", "title_only": "Slot Song"},
        ephemeral=False,
    )
    state.continuity_slot = reserved_slot
    state.now_streaming = {"type": "music", "label": "On air", "started": time.time(), "metadata": {}}
    state.current_stream_audible = True

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/public-status")).json()

    # The read path reported no playable runway but left the exact same slot
    # reservation intact (identity, not just non-null — a swap would be a bug too).
    assert body["playback_actions"]["skip_would_bridge"] is True
    assert state.continuity_slot is reserved_slot


@pytest.mark.asyncio
async def test_admin_status_buffered_audio_excludes_blocklisted_queue(tmp_path):
    """Admin buffered_audio_sec must match the governor: banned audio is not runway."""
    app = _make_test_app()
    state = app.state.station_state
    clean_path = tmp_path / "clean.mp3"
    clean_path.write_bytes(b"clean" * 1024)
    clean = Segment(
        type=SegmentType.MUSIC,
        path=clean_path,
        duration_sec=180.0,
        metadata={"artist": "Clean Artist", "title_only": "Clean Song"},
        ephemeral=False,
    )
    banned_path = tmp_path / "banned.mp3"
    banned_path.write_bytes(b"banned" * 1024)
    banned = Segment(
        type=SegmentType.MUSIC,
        path=banned_path,
        duration_sec=180.0,
        metadata={"artist": "Banned Artist", "title_only": "Banned Song"},
        ephemeral=False,
    )
    for segment in (clean, banned):
        app.state.queue.put_nowait(segment)
    state.blocklist = {("banned artist", "banned song"): {"display": "Banned Artist - Banned Song"}}

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        admin_status = (await client.get("/status")).json()

    # Only the clean track counts; the banned queued segment (which the playback
    # loop discards before its first byte) must not inflate the honest readout.
    assert admin_status["buffered_audio_sec"] == 180.0
