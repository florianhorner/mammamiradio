"""Tests for control-plane contracts that drive the UI's sense of truth.

The listener and admin dashboards build their mental model of station state
from a handful of server fields: now_streaming, session_stopped, queued_segments,
and capabilities.  Each test here pins one contract so that future refactors
cannot silently break the mapping between a control action and what the UI will
show on the next poll.

Disconnects targeted (findings from UI audit):
 - Skip: now_streaming flips to "skipping" synchronously; skip_event is set
 - Skip on nothing: rejected, state unchanged
 - Stop: clears queue (both real and shadow), sets session_stopped, writes
   now_streaming type="stopped" — all in one atomic response
 - Resume: clears session_stopped ONLY — does NOT reset now_streaming, queue,
   or producer state (documented gap: UI flips to ON AIR but now_streaming
   still says "stopped" until the playback loop overwrites it)
 - Purge: clears both real queue and shadow list, reports count
 - Capabilities: reports key *presence* (bool), not runtime API health —
   dot can show "connected" while the API is actually down
 - Pending requests: cleared silently on playlist switch (request can be lost)
 - Trigger: sets force_next, not consumed until next producer cycle
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mammamiradio.config import load_config
from mammamiradio.models import Segment, SegmentType, StationState, Track
from mammamiradio.streamer import LiveStreamHub, router

TOML_PATH = str(Path(__file__).parent.parent / "radio.toml")
TOKEN = "test-admin-token"
AUTH = {"X-Radio-Admin-Token": TOKEN}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_seg(title: str = "Track") -> Segment:
    return Segment(
        type=SegmentType.MUSIC,
        path=Path(f"/tmp/ui_test_{title}.mp3"),
        metadata={"title": title},
    )


def _make_app(
    *,
    now_streaming: dict | None = None,
    session_stopped: bool = False,
    shadow: list[dict] | None = None,
    queue_items: int = 0,
    anthropic_key: str = "",
    openai_key: str = "",
    ha_enabled: bool = False,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    config = load_config(TOML_PATH)
    config.admin_token = TOKEN
    config.admin_password = ""
    config.anthropic_api_key = anthropic_key
    config.openai_api_key = openai_key
    config.homeassistant.enabled = ha_enabled

    state = StationState(
        playlist=[Track(title="Song A", artist="Artist", duration_ms=180_000, spotify_id="s1")],
        session_stopped=session_stopped,
    )
    if now_streaming is not None:
        state.now_streaming = now_streaming
    if shadow is not None:
        state.queued_segments = list(shadow)

    q: asyncio.Queue = asyncio.Queue()
    for _ in range(queue_items):
        q.put_nowait(_make_seg())

    app.state.queue = q
    app.state.skip_event = asyncio.Event()
    app.state.source_switch_lock = asyncio.Lock()
    hub = LiveStreamHub()
    hub.bind_state(state)
    app.state.stream_hub = hub
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    return app


# ---------------------------------------------------------------------------
# Skip endpoint
# ---------------------------------------------------------------------------


class TestSkipEndpoint:
    @pytest.mark.asyncio
    async def test_skip_when_streaming_sets_skip_event(self):
        """skip_event is set synchronously — playback loop picks it up."""
        app = _make_app(now_streaming={"type": "music", "label": "Song A", "started": time.time(), "metadata": {}})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/skip", headers=AUTH)

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert app.state.skip_event.is_set()

    @pytest.mark.asyncio
    async def test_skip_writes_skipping_state_to_now_streaming(self):
        """After skip, now_streaming type becomes 'skipping' immediately.

        The UI reads this on next poll and shows a transitional state.
        The actual audio cutoff is async (may lag 1-3s), so the UI will show
        'Skipping...' while the listener may still hear the tail of the segment.
        This test documents that gap as intentional behaviour.
        """
        app = _make_app(now_streaming={"type": "music", "label": "Song A", "started": time.time(), "metadata": {}})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post("/api/skip", headers=AUTH)

        ns = app.state.station_state.now_streaming
        assert ns["type"] == "skipping"
        assert ns["label"] == "Skipping..."

    @pytest.mark.asyncio
    async def test_skip_when_nothing_streaming_returns_error(self):
        """Skip with empty now_streaming is rejected; state unchanged."""
        app = _make_app(now_streaming={})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/skip", headers=AUTH)

        assert resp.json()["ok"] is False
        assert not app.state.skip_event.is_set()

    @pytest.mark.asyncio
    async def test_skip_does_not_purge_queue(self):
        """Skip only interrupts the current segment; queued segments survive."""
        shadow = [{"type": "music", "label": "Next Up", "metadata": {}}]
        app = _make_app(
            now_streaming={"type": "music", "label": "Song A", "started": time.time(), "metadata": {}},
            shadow=shadow,
            queue_items=1,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post("/api/skip", headers=AUTH)

        assert len(app.state.station_state.queued_segments) == 1
        assert not app.state.queue.empty()

    @pytest.mark.asyncio
    async def test_skip_records_skip_outcome_for_music_segment(self):
        """Listener profile records the skip so host avoids the track."""
        app = _make_app(
            now_streaming={
                "type": "music",
                "label": "Unwanted Track",
                "started": time.time() - 10,
                "metadata": {},
            }
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post("/api/skip", headers=AUTH)

        # songs_skipped counter on the listener profile should be incremented
        profile = app.state.station_state.listener
        assert profile.songs_skipped >= 1

    @pytest.mark.asyncio
    async def test_skip_does_not_record_outcome_for_non_music_segment(self):
        """Skip of banter/ad/station_id should not pollute listener music history."""
        app = _make_app(
            now_streaming={"type": "banter", "label": "Sofia talking", "started": time.time(), "metadata": {}}
        )
        profile_before = app.state.station_state.listener.songs_skipped

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post("/api/skip", headers=AUTH)

        assert app.state.station_state.listener.songs_skipped == profile_before


# ---------------------------------------------------------------------------
# Stop endpoint
# ---------------------------------------------------------------------------


class TestStopEndpoint:
    @pytest.mark.asyncio
    async def test_stop_sets_session_stopped(self):
        app = _make_app(now_streaming={"type": "music", "label": "Song", "started": time.time(), "metadata": {}})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/stop", headers=AUTH)

        assert resp.json()["ok"] is True
        assert app.state.station_state.session_stopped is True

    @pytest.mark.asyncio
    async def test_stop_writes_stopped_type_to_now_streaming(self):
        """After stop, now_streaming.type == 'stopped' — UI shows stopped banner.

        This is synchronous.  The UI must NOT infer 'stopped' from session_stopped
        alone; it must also check now_streaming.type to show the right state.
        """
        app = _make_app(now_streaming={"type": "music", "label": "Song", "started": time.time(), "metadata": {}})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post("/api/stop", headers=AUTH)

        ns = app.state.station_state.now_streaming
        assert ns["type"] == "stopped"

    @pytest.mark.asyncio
    async def test_stop_clears_shadow_and_real_queue(self):
        """Stop purges both the real asyncio.Queue and the shadow list."""
        app = _make_app(
            now_streaming={"type": "music", "label": "Song", "started": time.time(), "metadata": {}},
            shadow=[{"type": "music", "label": "Next", "metadata": {}}],
            queue_items=1,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/stop", headers=AUTH)

        assert app.state.station_state.queued_segments == []
        assert app.state.queue.empty()
        assert resp.json()["purged"] == 1

    @pytest.mark.asyncio
    async def test_stop_reports_zero_purged_on_empty_queue(self):
        app = _make_app(
            now_streaming={"type": "music", "label": "Song", "started": time.time(), "metadata": {}},
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/stop", headers=AUTH)

        assert resp.json()["purged"] == 0

    @pytest.mark.asyncio
    async def test_stop_sets_skip_event_when_streaming(self):
        app = _make_app(now_streaming={"type": "music", "label": "Song", "started": time.time(), "metadata": {}})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post("/api/stop", headers=AUTH)

        assert app.state.skip_event.is_set()

    @pytest.mark.asyncio
    async def test_stop_does_not_set_skip_event_when_idle(self):
        """If nothing is streaming, skip_event should not be set on stop."""
        app = _make_app(now_streaming={})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post("/api/stop", headers=AUTH)

        assert not app.state.skip_event.is_set()


# ---------------------------------------------------------------------------
# Resume endpoint — documents the "resume gap"
# ---------------------------------------------------------------------------


class TestResumeEndpoint:
    @pytest.mark.asyncio
    async def test_resume_clears_session_stopped(self):
        app = _make_app(session_stopped=True)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/resume", headers=AUTH)

        assert resp.json()["ok"] is True
        assert app.state.station_state.session_stopped is False

    @pytest.mark.asyncio
    async def test_resume_does_not_reset_now_streaming(self):
        """DOCUMENTED GAP: Resume does NOT clear now_streaming.

        After stop, now_streaming.type == 'stopped'.  After resume, it stays
        'stopped' until the playback loop picks up the next segment and calls
        on_stream_segment().  This means the UI will briefly show the stopped
        banner even after clicking Resume — potentially for several seconds.

        Any UI fix (e.g., resetting now_streaming on resume) must be done in
        the endpoint; the playback loop cannot be relied on for immediate UI update.
        """
        stopped_state = {"type": "stopped", "label": "Session stopped", "started": time.time(), "metadata": {}}
        app = _make_app(session_stopped=True, now_streaming=stopped_state)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post("/api/resume", headers=AUTH)

        # now_streaming is still "stopped" — the gap
        assert app.state.station_state.now_streaming["type"] == "stopped"

    @pytest.mark.asyncio
    async def test_resume_does_not_re_populate_queue(self):
        """Resume does NOT restore the queue that was cleared by stop.

        The producer loop must restart producing segments organically.
        If the producer is stuck (e.g., all workers timed out), resume
        will clear the stopped flag but the queue stays empty and nothing plays.
        """
        app = _make_app(session_stopped=True, shadow=[], queue_items=0)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post("/api/resume", headers=AUTH)

        assert app.state.station_state.queued_segments == []
        assert app.state.queue.empty()

    @pytest.mark.asyncio
    async def test_resume_does_not_clear_force_next(self):
        """force_next set before stop survives resume unchanged.

        This can cause the wrong segment type to play after resume if a
        trigger was set before the stop.
        """
        app = _make_app(session_stopped=True)
        app.state.station_state.force_next = SegmentType.AD

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post("/api/resume", headers=AUTH)

        assert app.state.station_state.force_next == SegmentType.AD


# ---------------------------------------------------------------------------
# Purge endpoint
# ---------------------------------------------------------------------------


class TestPurgeEndpoint:
    @pytest.mark.asyncio
    async def test_purge_clears_shadow_and_real_queue(self):
        app = _make_app(
            shadow=[{"type": "music", "label": "A", "metadata": {}}, {"type": "music", "label": "B", "metadata": {}}],
            queue_items=2,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/purge", headers=AUTH)

        assert resp.json()["ok"] is True
        assert resp.json()["purged"] == 2
        assert app.state.station_state.queued_segments == []
        assert app.state.queue.empty()

    @pytest.mark.asyncio
    async def test_purge_does_not_affect_now_streaming(self):
        """Purge only clears the queue; the current segment keeps playing."""
        app = _make_app(
            now_streaming={"type": "music", "label": "Now", "started": time.time(), "metadata": {}},
            shadow=[{"type": "music", "label": "Queued", "metadata": {}}],
            queue_items=1,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post("/api/purge", headers=AUTH)

        ns = app.state.station_state.now_streaming
        assert ns["type"] == "music"
        assert ns["label"] == "Now"

    @pytest.mark.asyncio
    async def test_purge_does_not_set_skip_event(self):
        """Purge must not interrupt the currently playing segment."""
        app = _make_app(
            now_streaming={"type": "music", "label": "Now", "started": time.time(), "metadata": {}},
            queue_items=2,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post("/api/purge", headers=AUTH)

        assert not app.state.skip_event.is_set()


# ---------------------------------------------------------------------------
# Capabilities — static key presence, not runtime health
# ---------------------------------------------------------------------------


class TestCapabilitiesEndpoint:
    @pytest.mark.asyncio
    async def test_capabilities_true_when_anthropic_key_set(self):
        """Pipeline dot shows 'connected' when key exists — even if API is down.

        This is the documented gap: the dot reflects configuration, not health.
        If Anthropic is unreachable, the dot stays green until banter fails and
        the UI has no direct way to learn about it.
        """
        app = _make_app(anthropic_key="sk-ant-test-key")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/capabilities", headers=AUTH)

        data = resp.json()
        assert data["capabilities"]["anthropic_key"] is True
        assert data["capabilities"]["script_llm"] is True

    @pytest.mark.asyncio
    async def test_capabilities_false_with_no_keys(self):
        app = _make_app(anthropic_key="", openai_key="")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/capabilities", headers=AUTH)

        data = resp.json()
        assert data["capabilities"]["anthropic_key"] is False
        assert data["capabilities"]["openai"] is False
        assert data["capabilities"]["script_llm"] is False

    @pytest.mark.asyncio
    async def test_capabilities_openai_only_sets_script_llm(self):
        """OpenAI key without Anthropic key still enables script_llm flag."""
        app = _make_app(openai_key="sk-openai-test-key")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/capabilities", headers=AUTH)

        data = resp.json()
        assert data["capabilities"]["openai"] is True
        assert data["capabilities"]["anthropic_key"] is False
        assert data["capabilities"]["script_llm"] is True


# ---------------------------------------------------------------------------
# Trigger endpoint — force_next is set but not immediately applied
# ---------------------------------------------------------------------------


class TestTriggerEndpoint:
    @pytest.mark.asyncio
    async def test_trigger_banter_sets_force_next(self):
        """Trigger sets force_next on state; the NEXT producer cycle uses it."""
        app = _make_app()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/trigger", json={"type": "banter"}, headers=AUTH)

        assert resp.json()["ok"] is True
        assert app.state.station_state.force_next == SegmentType.BANTER

    @pytest.mark.asyncio
    async def test_trigger_does_not_purge_existing_queue(self):
        """Trigger does not skip or purge; it only affects the next produced segment."""
        app = _make_app(shadow=[{"type": "music", "label": "A", "metadata": {}}], queue_items=1)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post("/api/trigger", json={"type": "ad"}, headers=AUTH)

        assert not app.state.queue.empty()
        assert len(app.state.station_state.queued_segments) == 1

    @pytest.mark.asyncio
    async def test_trigger_invalid_type_returns_error(self):
        app = _make_app()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/trigger", json={"type": "invalid"}, headers=AUTH)

        assert resp.json()["ok"] is False
        assert app.state.station_state.force_next is None


# ---------------------------------------------------------------------------
# Pending requests — silently cleared on playlist switch
# ---------------------------------------------------------------------------


class TestPendingRequestLifecycle:
    def test_pending_requests_cleared_on_switch_playlist(self):
        """DOCUMENTED GAP: A pending listener request is silently discarded when
        the admin loads a new playlist.  The listener sees 'Canzone in arrivo!'
        but the request is lost and never played.
        """
        state = StationState(
            playlist=[Track(title="Old Song", artist="A", duration_ms=1000, spotify_id="o1")],
            pending_requests=[{"type": "song_wish", "text": "Play Ti Amo"}],
        )
        new_tracks = [Track(title="New Song", artist="B", duration_ms=1000, spotify_id="n1")]

        state.switch_playlist(new_tracks)

        assert state.pending_requests == []

    def test_pinned_track_cleared_on_switch_playlist(self):
        """Pinned track from old playlist is discarded on source switch."""
        pinned = Track(title="Volare", artist="Modugno", duration_ms=1000, spotify_id="p1")
        state = StationState(
            playlist=[pinned],
            pinned_track=pinned,
        )
        new_tracks = [Track(title="Other", artist="B", duration_ms=1000, spotify_id="n1")]

        state.switch_playlist(new_tracks)

        assert state.pinned_track is None

    def test_force_next_cleared_on_switch_playlist(self):
        """force_next from old context is discarded on source switch."""
        state = StationState(
            playlist=[Track(title="Old", artist="A", duration_ms=1000, spotify_id="o1")],
        )
        state.force_next = SegmentType.NEWS_FLASH
        new_tracks = [Track(title="New", artist="B", duration_ms=1000, spotify_id="n1")]

        state.switch_playlist(new_tracks)

        assert state.force_next is None


# ---------------------------------------------------------------------------
# now_streaming field invariants
# ---------------------------------------------------------------------------


class TestNowStreamingInvariants:
    def test_on_stream_segment_bumps_playback_epoch(self):
        """Each new segment increments playback_epoch — used as a version fence."""
        state = StationState(
            playlist=[Track(title="Song", artist="A", duration_ms=1000, spotify_id="s1")],
        )
        epoch_before = state.playback_epoch
        seg = Segment(type=SegmentType.MUSIC, path=Path("/tmp/test.mp3"), metadata={"title": "Song"})

        state.on_stream_segment(seg)

        assert state.playback_epoch == epoch_before + 1

    def test_on_stream_segment_updates_now_streaming_type(self):
        state = StationState(
            playlist=[Track(title="Song", artist="A", duration_ms=1000, spotify_id="s1")],
        )
        seg = Segment(type=SegmentType.AD, path=Path("/tmp/test.mp3"), metadata={"brand": "Lavazza"})

        state.on_stream_segment(seg)

        assert state.now_streaming["type"] == "ad"

    def test_skipping_state_is_overwritten_by_next_on_stream_segment(self):
        """The 'skipping' transitional state is replaced when the next segment starts.

        This confirms that the UI gap (showing 'Skipping...' while the next segment
        is already playing) closes as soon as on_stream_segment is called by the
        playback loop.
        """
        state = StationState(
            playlist=[Track(title="Song", artist="A", duration_ms=1000, spotify_id="s1")],
        )
        state.now_streaming = {"type": "skipping", "label": "Skipping...", "started": time.time(), "metadata": {}}
        next_seg = Segment(type=SegmentType.MUSIC, path=Path("/tmp/next.mp3"), metadata={"title": "Next Song"})

        state.on_stream_segment(next_seg)

        assert state.now_streaming["type"] == "music"
        assert state.now_streaming.get("label") != "Skipping..."
