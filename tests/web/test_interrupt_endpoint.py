"""Tests for POST /api/interrupt — auth, cooldown, queue drain, skip_event."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mammamiradio.core.config import load_config
from mammamiradio.core.models import (
    ChaosSubtype,
    GenerationWasteReason,
    InterruptSpec,
    Segment,
    SegmentType,
    StationState,
    Track,
)
from mammamiradio.scheduling.handoff import PreparedMusicHandoff, commit_music_handoff
from mammamiradio.web.listener_requests import router as listener_requests_router
from mammamiradio.web.mp3_frames import Mp3HandoffSplit
from mammamiradio.web.streamer import LiveStreamHub, router

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def _fake_tone(path: Path, *_args, **_kwargs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def _make_test_app(*, admin_token: str = "test-token") -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.include_router(listener_requests_router)

    config = load_config(TOML_PATH)
    config.admin_token = admin_token
    config.admin_password = ""

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
    return app


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_interrupt_requires_auth():
    app = _make_test_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post("/api/interrupt", json={"directive": "test"})
    assert resp.status_code == 401


def test_interrupt_accepts_valid_token():
    app = _make_test_app()
    with (
        TestClient(app, raise_server_exceptions=True) as client,
        patch("mammamiradio.scheduling.producer._fire_interrupt", new_callable=AsyncMock) as mock_fire,
    ):
        resp = client.post(
            "/api/interrupt",
            json={"directive": "La pasta scotta!"},
            headers={"X-Radio-Admin-Token": "test-token"},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    mock_fire.assert_awaited_once()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_interrupt_missing_directive_returns_422():
    app = _make_test_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post(
            "/api/interrupt",
            json={"urgency": "pissed"},
            headers={"X-Radio-Admin-Token": "test-token"},
        )
    assert resp.status_code == 422


def test_interrupt_empty_directive_returns_422():
    app = _make_test_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post(
            "/api/interrupt",
            json={"directive": "   "},
            headers={"X-Radio-Admin-Token": "test-token"},
        )
    assert resp.status_code == 422


def test_interrupt_non_object_body_returns_422():
    """A JSON array (or any non-object body) is rejected before .get() can blow up."""
    app = _make_test_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post(
            "/api/interrupt",
            json=["not", "an", "object"],
            headers={"X-Radio-Admin-Token": "test-token"},
        )
    assert resp.status_code == 422
    assert resp.json()["ok"] is False
    assert resp.json()["error"]


# ---------------------------------------------------------------------------
# Cooldown (429)
# ---------------------------------------------------------------------------


def test_interrupt_cooldown_returns_429():
    app = _make_test_app()
    state: StationState = app.state.station_state
    # Simulate interrupt fired 5 seconds ago
    state.last_interrupt_ts = time.time() - 5

    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post(
            "/api/interrupt",
            json={"directive": "Again!"},
            headers={"X-Radio-Admin-Token": "test-token"},
        )
    assert resp.status_code == 429
    body = resp.json()
    assert body["ok"] is False
    assert "retry_after" in body
    assert body["retry_after"] > 0


def test_interrupt_fires_after_cooldown_expires():
    app = _make_test_app()
    state: StationState = app.state.station_state
    # Simulate interrupt fired 120 seconds ago (well past 60s cooldown)
    state.last_interrupt_ts = time.time() - 120

    with (
        TestClient(app, raise_server_exceptions=True) as client,
        patch("mammamiradio.scheduling.producer._fire_interrupt", new_callable=AsyncMock) as mock_fire,
    ):
        resp = client.post(
            "/api/interrupt",
            json={"directive": "La pasta scotta!"},
            headers={"X-Radio-Admin-Token": "test-token"},
        )
    assert resp.status_code == 200
    mock_fire.assert_awaited_once()


# ---------------------------------------------------------------------------
# Scenario 1 (normal): queue drain + skip_event + directive injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_interrupt_drains_queue_and_fires_skip(tmp_path: Path):
    """Scenario 1: interrupt fires → queue drained, skip_event set, directive injected."""
    from mammamiradio.scheduling.producer import _fire_interrupt

    state = StationState(
        playlist=[Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="t1")],
    )
    queue: asyncio.Queue[Segment] = asyncio.Queue()
    skip_event = asyncio.Event()

    # Pre-fill queue with buffered segments
    dummy_path = tmp_path / "dummy_test_segment.mp3"
    dummy_path.touch()
    for _ in range(3):
        await queue.put(Segment(type=SegmentType.MUSIC, path=dummy_path, metadata={"type": "music"}, ephemeral=False))
    state.queued_segments = [{"type": "music", "label": f"Queued {idx}"} for idx in range(3)]

    spec = InterruptSpec(directive="La pasta sta bruciando!", urgency="pissed", cooldown=60)
    with patch("mammamiradio.scheduling.producer.generate_tone", side_effect=_fake_tone):
        fired = await _fire_interrupt(state, spec, queue, skip_event, bridge_tmp_dir=tmp_path)

    assert fired is True
    assert queue.empty(), "queue must be drained after interrupt"
    assert state.queued_segments == [], "shadow queue must be cleared with the real queue"
    assert state.discarded_segments_total == 3
    assert state.discarded_unproduced_segments_total == 0
    assert state.discard_by_reason == {GenerationWasteReason.INTERRUPT: 3}
    assert skip_event.is_set(), "skip_event must be set"
    assert state.ha_pending_directive == "La pasta sta bruciando!"
    assert state.chaos_pending == ChaosSubtype.URGENT_INTERRUPT
    assert state.chaos_cutover_epoch == 1
    assert state.force_next == SegmentType.BANTER, (
        "force_next safety belt must be set; producer clears it after URGENT_INTERRUPT renders"
    )
    assert state.last_interrupt_ts > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["unlink", "accounting", "orphan_accounting"])
async def test_fire_interrupt_item_failure_still_drains_and_reconciles_handoff(
    tmp_path: Path,
    failure_mode: str,
):
    """One broken item cannot strand its successor or unfinished queue work."""
    from mammamiradio.scheduling.producer import _fire_interrupt

    source = tmp_path / "song.mp3"
    head = tmp_path / "song_head.mp3"
    tail = tmp_path / "song_tail.mp3"
    successor_path = tmp_path / "banter.mp3"
    for path, payload in (
        (source, b"song"),
        (head, b"head"),
        (tail, b"tail"),
        (successor_path, b"banter"),
    ):
        path.write_bytes(payload)

    music = Segment(type=SegmentType.MUSIC, path=source, duration_sec=120.0, ephemeral=False)
    successor = Segment(type=SegmentType.BANTER, path=successor_path, ephemeral=True)
    prepared = PreparedMusicHandoff(
        music_segment=music,
        source_path=source,
        split=Mp3HandoffSplit(
            head_path=head,
            tail_path=tail,
            playable_start_byte=0,
            head_end_byte=4,
            playable_end_byte=8,
            head_duration_sec=112.0,
            tail_duration_sec=8.0,
            source_duration_sec=120.0,
            frame_count=10,
            head_frame_count=8,
            tail_frame_count=2,
        ),
    )
    state = StationState(
        playlist=[Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="t1")],
        queued_segments=[{"id": "music", "type": "music", "label": "Song"}],
    )
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    queue.put_nowait(music)
    assert commit_music_handoff(
        queue,
        state,
        prepared,
        successor,
        {"id": "banter", "type": "banter", "label": "Banter"},
    )
    # A committed successor already contains the tail; the live control owns
    # only the queued head and successor paths at this point.
    tail.unlink()
    skip_event = asyncio.Event()
    real_unlink = Path.unlink
    real_record_discard = state.record_discard
    real_get_nowait = queue.get_nowait
    head_exists_after_interrupt = False
    get_calls = 0

    def _flaky_unlink(path: Path, *args, **kwargs):
        if failure_mode == "unlink" and path == head:
            raise PermissionError(head)
        return real_unlink(path, *args, **kwargs)

    def _flaky_record_discard(segment: Segment, **kwargs):
        if failure_mode == "accounting" and segment is music:
            raise RuntimeError("accounting unavailable")
        if failure_mode == "orphan_accounting" and segment is successor:
            raise RuntimeError("orphan accounting unavailable")
        return real_record_discard(segment, **kwargs)

    def _flaky_get_nowait():
        nonlocal get_calls
        get_calls += 1
        if failure_mode == "orphan_accounting" and get_calls == 2:
            raise RuntimeError("one-shot queue read failure")
        return real_get_nowait()

    try:
        with (
            patch.object(Path, "unlink", _flaky_unlink),
            patch.object(state, "record_discard", side_effect=_flaky_record_discard),
            patch.object(queue, "get_nowait", side_effect=_flaky_get_nowait),
        ):
            fired = await _fire_interrupt(
                state,
                InterruptSpec(directive="Urgente!", urgency="urgent", cooldown=60),
                queue,
                skip_event,
                bridge_tmp_dir=tmp_path,
            )
            head_exists_after_interrupt = head.exists()
    finally:
        real_unlink(head, missing_ok=True)

    assert fired is True
    assert queue.empty()
    assert queue._unfinished_tasks == 0
    assert state.queued_segments == []
    assert state.handoff_reservations == {}
    assert music.handoff_id is None
    assert successor.handoff_id is None
    assert not successor_path.exists()
    assert skip_event.is_set()
    assert head_exists_after_interrupt is (failure_mode == "unlink")


@pytest.mark.asyncio
async def test_fire_interrupt_clears_prior_capacity_exempt_continuity_slot(tmp_path: Path):
    """The urgent bridge must not be followed by stale audio from an older control."""
    from mammamiradio.scheduling.producer import _fire_interrupt

    stale_path = tmp_path / "stale_continuity.mp3"
    stale_path.write_bytes(b"stale")
    state = StationState(
        playlist=[Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="t1")],
        continuity_slot=Segment(
            type=SegmentType.BANTER,
            path=stale_path,
            metadata={"continuity_reservation": True},
            ephemeral=False,
        ),
    )
    queue: asyncio.Queue[Segment] = asyncio.Queue()
    skip_event = asyncio.Event()

    fired = await _fire_interrupt(
        state,
        InterruptSpec(directive="Urgent!", urgency="urgent", cooldown=60),
        queue,
        skip_event,
        bridge_tmp_dir=tmp_path,
    )

    assert fired is True
    assert state.continuity_slot is None
    assert state.interrupt_slot is not None
    assert skip_event.is_set()


# ---------------------------------------------------------------------------
# Scenario 2: alert.mp3 absent → packaged bridge tone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_interrupt_uses_packaged_bridge_when_alert_missing(tmp_path: Path):
    """Scenario 2: alert.mp3 absent → bundled tone still gives immediate audio."""
    from mammamiradio.scheduling.producer import _fire_interrupt

    state = StationState(
        playlist=[Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="t1")],
    )
    queue: asyncio.Queue[Segment] = asyncio.Queue()
    skip_event = asyncio.Event()
    spec = InterruptSpec(directive="Svegliati!", urgency="urgent", cooldown=60)

    with patch("mammamiradio.scheduling.producer._SFX_DIR", Path("/nonexistent")):
        await _fire_interrupt(state, spec, queue, skip_event, bridge_tmp_dir=tmp_path)

    assert state.interrupt_slot is not None
    assert state.interrupt_slot.exists()
    assert state.interrupt_slot.name == "emergency_tone.mp3"
    assert state.interrupt_slot_ephemeral is False
    assert skip_event.is_set()
    assert state.ha_pending_directive == "Svegliati!"


@pytest.mark.asyncio
async def test_fire_interrupt_uses_packaged_bridge_when_ffmpeg_is_unavailable(tmp_path: Path):
    """A bundled bridge makes FFmpeg availability irrelevant to an interrupt."""
    from mammamiradio.scheduling.producer import _fire_interrupt

    state = StationState(
        playlist=[Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="t1")],
    )
    queue: asyncio.Queue[Segment] = asyncio.Queue()
    skip_event = asyncio.Event()
    spec = InterruptSpec(directive="Svegliati!", urgency="urgent", cooldown=60)

    with patch("mammamiradio.scheduling.producer._SFX_DIR", Path("/nonexistent")):
        await _fire_interrupt(state, spec, queue, skip_event, bridge_tmp_dir=tmp_path)

    assert state.interrupt_slot is not None
    assert state.interrupt_slot.name == "emergency_tone.mp3"
    assert state.interrupt_slot_ephemeral is False
    assert skip_event.is_set()
    assert state.ha_pending_directive == "Svegliati!"


@pytest.mark.asyncio
async def test_fire_interrupt_keeps_bundled_alert_reusable(tmp_path: Path):
    """A checked-in alert.mp3 asset must not be marked ephemeral and deleted after first use."""
    from mammamiradio.scheduling.producer import _fire_interrupt

    sfx_dir = tmp_path / "sfx"
    sfx_dir.mkdir()
    alert = sfx_dir / "alert.mp3"
    alert.touch()
    state = StationState(
        playlist=[Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="t1")],
    )
    queue: asyncio.Queue[Segment] = asyncio.Queue()
    skip_event = asyncio.Event()
    spec = InterruptSpec(directive="Svegliati!", urgency="urgent", cooldown=60)

    with patch("mammamiradio.scheduling.producer._SFX_DIR", sfx_dir):
        await _fire_interrupt(state, spec, queue, skip_event, bridge_tmp_dir=tmp_path)

    assert state.interrupt_slot == alert
    assert state.interrupt_slot_ephemeral is False


# ---------------------------------------------------------------------------
# Scenario 3 (post-restart): session_stopped state cleared on interrupt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_interrupt_works_after_session_stopped(tmp_path: Path):
    """Scenario 3: interrupt fires even when session was previously stopped."""
    from mammamiradio.scheduling.producer import _fire_interrupt

    state = StationState(
        playlist=[Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="t1")],
    )
    state.session_stopped = True  # simulate post-restart state
    queue: asyncio.Queue[Segment] = asyncio.Queue()
    skip_event = asyncio.Event()
    spec = InterruptSpec(directive="Alzati!", urgency="pissed", cooldown=60)

    with patch("mammamiradio.scheduling.producer.generate_tone", side_effect=_fake_tone):
        await _fire_interrupt(state, spec, queue, skip_event, bridge_tmp_dir=tmp_path)

    # Interrupt still fires — producer will resume from stopped state after skip
    assert state.ha_pending_directive == "Alzati!"
    assert skip_event.is_set()


# ---------------------------------------------------------------------------
# Scenario: API cooldown enforcement remains opt-in for _fire_interrupt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_interrupt_respects_global_cooldown_when_requested():
    from mammamiradio.scheduling.producer import _fire_interrupt

    state = StationState(
        playlist=[Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="t1")],
    )
    state.last_interrupt_ts = time.time() - 5  # 5s ago, 60s cooldown not expired
    queue: asyncio.Queue[Segment] = asyncio.Queue()
    skip_event = asyncio.Event()
    spec = InterruptSpec(directive="Di nuovo!", urgency="pissed", cooldown=60)

    fired = await _fire_interrupt(state, spec, queue, skip_event, enforce_global_cooldown=True)

    assert fired is False, "suppressed call must return False so the endpoint can 429"
    assert not skip_event.is_set(), "skip_event must NOT be set when cooldown is active"
    assert state.ha_pending_directive == "", "directive must NOT be injected during cooldown"


@pytest.mark.asyncio
async def test_fire_interrupt_global_cooldown_uses_fixed_window_not_spec(tmp_path: Path):
    """spec.cooldown is for per-entity gating upstream; the global window stays at 60s.

    Regression: a timer configured with cooldown=300 used to push the *global*
    suppression window to 5 minutes, blocking unrelated interrupts.
    """
    from mammamiradio.scheduling.producer import _fire_interrupt

    state = StationState(
        playlist=[Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="t1")],
    )
    state.last_interrupt_ts = time.time() - 90  # 90s ago: past the 60s global window
    queue: asyncio.Queue[Segment] = asyncio.Queue()
    skip_event = asyncio.Event()
    spec = InterruptSpec(directive="Lavatrice!", urgency="urgent", cooldown=300)

    with patch("mammamiradio.scheduling.producer.generate_tone", side_effect=_fake_tone):
        fired = await _fire_interrupt(
            state,
            spec,
            queue,
            skip_event,
            enforce_global_cooldown=True,
            bridge_tmp_dir=tmp_path,
        )

    assert fired is True
    assert skip_event.is_set()
    assert state.ha_pending_directive == "Lavatrice!"


@pytest.mark.asyncio
async def test_fire_interrupt_global_cooldown_blocks_distinct_ha_timer(tmp_path: Path):
    """Global cooldown holds across distinct trigger sources so back-to-back timers
    don't cut the stream twice in seconds."""
    from mammamiradio.scheduling.producer import _fire_interrupt

    state = StationState(
        playlist=[Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="t1")],
    )
    state.last_interrupt_ts = time.time() - 5
    queue: asyncio.Queue[Segment] = asyncio.Queue()
    skip_event = asyncio.Event()
    spec = InterruptSpec(directive="Lavatrice finita!", urgency="urgent", cooldown=60)

    with patch("mammamiradio.scheduling.producer.generate_tone", side_effect=_fake_tone):
        await _fire_interrupt(
            state,
            spec,
            queue,
            skip_event,
            enforce_global_cooldown=True,
            bridge_tmp_dir=tmp_path,
        )

    assert not skip_event.is_set()
    assert state.ha_pending_directive == ""
