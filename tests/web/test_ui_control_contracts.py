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
 - Capabilities: reports BOTH key presence (`anthropic_key`) AND runtime auth
   health (`anthropic_degraded`, `anthropic_retry_after_s`). The admin UI
   renders three states — connected / suspended / not configured — so the
   dot no longer lies while the API is suspended after a 401 (Item 11).
 - Pending requests: cleared silently on playlist switch (request can be lost)
 - Trigger: sets force_next, not consumed until next producer cycle
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mammamiradio.core.config import load_config
from mammamiradio.core.models import Segment, SegmentType, StationState, Track
from mammamiradio.web.streamer import LiveStreamHub, router

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")
WEB_ROOT = Path(__file__).resolve().parents[2] / "mammamiradio" / "web"
ADMIN_HTML = WEB_ROOT / "templates" / "admin.html"
LISTENER_HTML = WEB_ROOT / "templates" / "listener.html"
TOKEN = "test-admin-token"
AUTH = {"X-Radio-Admin-Token": TOKEN}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_function_block(name: str) -> str:
    html = ADMIN_HTML.read_text()
    start = html.find(f"function {name}")
    assert start != -1, f"could not locate {name}() in admin.html"
    next_function = re.search(r"\n(?:async\s+)?function\s+", html[start + 1 :])
    end = start + 1 + next_function.start() if next_function is not None else len(html)
    return html[start:end]


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
    config.ha_token = "test-ha-token" if ha_enabled else ""

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
    async def test_skip_bridges_when_queue_empty(self):
        """Empty queue + no shadow -> skip forces next music before cutting (the
        shared _request_skip bridge). Pins the bridge on the /api/skip caller, the
        same contract ban-now-playing relies on."""
        app = _make_app(
            now_streaming={"type": "music", "label": "Song A", "started": time.time(), "metadata": {}},
            queue_items=0,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/skip", headers=AUTH)
        assert resp.json()["bridged"] is True
        assert app.state.station_state.force_next is SegmentType.MUSIC

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
# Ban-now-playing endpoint (on-air console "Ban" button = ban + immediate skip)
# ---------------------------------------------------------------------------


class TestBanNowPlayingEndpoint:
    @pytest.mark.asyncio
    async def test_ban_now_sets_skip_event_and_skipping_state(self):
        """Mirrors the Skip contract: the airing music segment is cut synchronously —
        skip_event set, now_streaming flips to 'skipping' — in one atomic response."""
        app = _make_app(
            now_streaming={
                "type": "music",
                "label": "Modugno — Volare",
                "started": time.time(),
                "metadata": {"artist": "Modugno", "title_only": "Volare"},
            }
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/track/ban-now-playing", headers=AUTH)
        body = resp.json()
        assert body["ok"] is True and body["skipped"] is True
        assert app.state.skip_event.is_set()
        assert app.state.station_state.now_streaming["type"] == "skipping"
        assert ("modugno", "volare") in app.state.station_state.blocklist

    @pytest.mark.asyncio
    async def test_ban_now_rejects_non_music_without_skipping(self):
        """Banter/stopped on air -> reject, no spurious skip, blocklist untouched."""
        app = _make_app(
            now_streaming={"type": "banter", "label": "Sofia talking", "started": time.time(), "metadata": {}}
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/track/ban-now-playing", headers=AUTH)
        assert resp.json()["ok"] is False
        assert not app.state.skip_event.is_set()
        assert app.state.station_state.blocklist == {}

    def test_admin_html_has_ban_now_button_and_handler(self):
        """The console wiring must survive HTML refactors: the button calls the
        handler, and the handler hits the dedicated endpoint."""
        html = ADMIN_HTML.read_text()
        assert 'id="banNowBtn"' in html
        assert "doBanNowPlaying(this)" in html
        assert "/api/track/ban-now-playing" in _admin_function_block("doBanNowPlaying")


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

    @pytest.mark.asyncio
    async def test_stop_bumps_last_state_change_at(self):
        """The integration-contract ETag relies on this timestamp moving forward."""
        app = _make_app(now_streaming={"type": "music", "label": "Song", "started": time.time(), "metadata": {}})
        app.state.station_state.last_state_change_at = 0.0
        before = time.time()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post("/api/stop", headers=AUTH)

        assert app.state.station_state.last_state_change_at >= before


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

    @pytest.mark.asyncio
    async def test_resume_bumps_last_state_change_at(self):
        """Integration-contract ETag invalidation depends on this timestamp moving forward."""
        app = _make_app(session_stopped=True)
        app.state.station_state.last_state_change_at = 0.0
        before = time.time()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post("/api/resume", headers=AUTH)

        assert app.state.station_state.last_state_change_at >= before


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
# Queue remove endpoint
# ---------------------------------------------------------------------------


class TestQueueRemoveEndpoint:
    @staticmethod
    def _make_queue_app(labels: list[str]) -> FastAPI:
        shadow = [{"type": "music", "label": label, "metadata": {"title": label}} for label in labels]
        app = _make_app(shadow=shadow, queue_items=0)
        for label in labels:
            app.state.queue.put_nowait(_make_seg(label))
        return app

    @staticmethod
    def _queue_titles(app: FastAPI) -> list[str]:
        return [seg.metadata["title"] for seg in list(app.state.queue._queue)]

    @pytest.mark.asyncio
    async def test_remove_by_index_preserves_remaining_order_and_depth_sync(self):
        app = self._make_queue_app(["Alpha", "Beta", "Gamma", "Delta"])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/queue/remove", json={"index": 1}, headers=AUTH)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "removed": "Beta"}
        assert [item["label"] for item in app.state.station_state.queued_segments] == [
            "Alpha",
            "Gamma",
            "Delta",
        ]
        assert self._queue_titles(app) == ["Alpha", "Gamma", "Delta"]
        assert app.state.queue.qsize() == len(app.state.station_state.queued_segments) == 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [{"index": "1"}, {"index": 1.5}, {"index": None}, {}])
    async def test_remove_rejects_non_integer_index_without_mutating_queue(self, payload: dict):
        app = self._make_queue_app(["Alpha", "Beta"])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/queue/remove", json=payload, headers=AUTH)

        assert resp.status_code == 422
        assert resp.json()["detail"] == "index must be an integer"
        assert [item["label"] for item in app.state.station_state.queued_segments] == ["Alpha", "Beta"]
        assert self._queue_titles(app) == ["Alpha", "Beta"]
        assert app.state.queue.qsize() == len(app.state.station_state.queued_segments) == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("index", [-1, 3])
    async def test_remove_rejects_out_of_range_index_without_mutating_queue(self, index: int):
        app = self._make_queue_app(["Alpha", "Beta", "Gamma"])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/queue/remove", json={"index": index}, headers=AUTH)

        assert resp.status_code == 422
        assert resp.json()["detail"] == f"index {index} out of range (queue has 3 items)"
        assert [item["label"] for item in app.state.station_state.queued_segments] == ["Alpha", "Beta", "Gamma"]
        assert self._queue_titles(app) == ["Alpha", "Beta", "Gamma"]
        assert app.state.queue.qsize() == len(app.state.station_state.queued_segments) == 3

    @staticmethod
    def _make_id_queue_app(entries: list[tuple[str, str]]) -> FastAPI:
        """Build an app whose shadow list and real queue carry queue ids.

        ``entries`` is a list of ``(queue_id, label)`` pairs.
        """
        shadow = [{"id": qid, "type": "music", "label": label, "metadata": {"title": label}} for qid, label in entries]
        app = _make_app(shadow=shadow, queue_items=0)
        for qid, label in entries:
            seg = _make_seg(label)
            seg.metadata["queue_id"] = qid
            app.state.queue.put_nowait(seg)
        return app

    @pytest.mark.asyncio
    async def test_remove_by_id_targets_the_named_segment(self):
        app = self._make_id_queue_app([("q-a", "Alpha"), ("q-b", "Beta"), ("q-c", "Gamma")])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/queue/remove", json={"id": "q-b"}, headers=AUTH)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "removed": "Beta"}
        assert [item["label"] for item in app.state.station_state.queued_segments] == ["Alpha", "Gamma"]
        assert self._queue_titles(app) == ["Alpha", "Gamma"]

    @pytest.mark.asyncio
    async def test_remove_by_id_after_head_consumed_removes_correct_segment(self):
        """Stale-index regression guard.

        The streamer consumes the head segment between the admin UI rendering a
        row and the click landing. A position-based remove would then drop the
        wrong track; an id-based remove must still hit the intended one.
        """
        app = self._make_id_queue_app([("q-a", "Alpha"), ("q-b", "Beta"), ("q-c", "Gamma")])

        # Streamer consumes the head — real queue and shadow list both lose idx 0.
        app.state.queue.get_nowait()
        app.state.station_state.queued_segments.pop(0)

        # "Gamma" was rendered at index 2; it is now index 1. Removing by id must
        # still remove Gamma — never Beta (whatever now sits at the stale index).
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/queue/remove", json={"id": "q-c"}, headers=AUTH)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "removed": "Gamma"}
        assert [item["label"] for item in app.state.station_state.queued_segments] == ["Beta"]
        assert self._queue_titles(app) == ["Beta"]

    @pytest.mark.asyncio
    async def test_remove_by_id_for_already_played_segment_is_noop_success(self):
        """An id that no longer exists (segment already played out) is a no-op
        success, not a 422 — the operator's intent is already satisfied."""
        app = self._make_id_queue_app([("q-a", "Alpha"), ("q-b", "Beta")])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/queue/remove", json={"id": "gone"}, headers=AUTH)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "removed": None}
        assert [item["label"] for item in app.state.station_state.queued_segments] == ["Alpha", "Beta"]
        assert self._queue_titles(app) == ["Alpha", "Beta"]

    @pytest.mark.asyncio
    async def test_remove_prefers_id_over_index_when_both_provided(self):
        """When a payload carries both `id` and `index`, `id` wins — it is the
        authoritative, position-independent identifier."""
        app = self._make_id_queue_app([("q-a", "Alpha"), ("q-b", "Beta"), ("q-c", "Gamma")])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            # id points at Beta, index points at Gamma — id must win.
            resp = await c.post("/api/queue/remove", json={"id": "q-b", "index": 2}, headers=AUTH)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "removed": "Beta"}
        assert [item["label"] for item in app.state.station_state.queued_segments] == ["Alpha", "Gamma"]
        assert self._queue_titles(app) == ["Alpha", "Gamma"]

    @staticmethod
    def _make_ephemeral_queue_app(tmp_path: Path, labels: list[str]) -> FastAPI:
        shadow = [{"type": "music", "label": label, "metadata": {"title": label}} for label in labels]
        app = _make_app(shadow=shadow, queue_items=0)
        for label in labels:
            path = tmp_path / f"{label}.mp3"
            path.write_bytes(b"audio")
            seg = Segment(
                type=SegmentType.MUSIC,
                path=path,
                ephemeral=True,
                metadata={"title": label},
            )
            app.state.queue.put_nowait(seg)
        return app

    @pytest.mark.asyncio
    async def test_remove_unlinks_ephemeral_segment_file(self, tmp_path: Path):
        """Removing an ephemeral pre-produced segment must unlink its temp MP3 (#412)."""
        app = self._make_ephemeral_queue_app(tmp_path, ["Alpha", "Beta", "Gamma"])
        beta_path = tmp_path / "Beta.mp3"
        assert beta_path.exists()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/queue/remove", json={"index": 1}, headers=AUTH)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "removed": "Beta"}
        assert not beta_path.exists()
        assert (tmp_path / "Alpha.mp3").exists()
        assert (tmp_path / "Gamma.mp3").exists()

    @pytest.mark.asyncio
    async def test_repeated_removes_do_not_inflate_unfinished_task_count(self, tmp_path: Path):
        """Each remove must balance asyncio.Queue unfinished_tasks via task_done (#412)."""
        app = self._make_ephemeral_queue_app(tmp_path, ["One", "Two", "Three"])
        q = app.state.queue

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            for index in (2, 1, 0):
                resp = await c.post("/api/queue/remove", json={"index": index}, headers=AUTH)
                assert resp.status_code == 200

        assert q.empty()
        assert q._unfinished_tasks == 0
        assert app.state.station_state.queued_segments == []


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


class TestSourceControlVisibilityContract:
    @pytest.mark.asyncio
    async def test_capabilities_expose_admin_source_control_flags(self):
        app = _make_app()
        app.state.config.playlist.jamendo_client_id = "jamendo-client"
        app.state.config.allow_ytdlp = False

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/capabilities", headers=AUTH)

        data = resp.json()["capabilities"]
        assert data["jamendo"] is True
        assert data["charts_reload"] is False

        app.state.config.allow_ytdlp = True
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/capabilities", headers=AUTH)

        data = resp.json()["capabilities"]
        assert data["jamendo"] is True
        assert data["charts_reload"] is True

    @pytest.mark.asyncio
    async def test_admin_html_binds_source_buttons_to_capability_flags_only(self):
        app = _make_app()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/admin", headers=AUTH)

        html = resp.text
        assert re.search(r'id="sourceJamendoBtn"[^>]*\bdata-capability="jamendo"', html)
        assert re.search(r'id="sourceChartsBtn"[^>]*\bdata-capability="charts_reload"', html)
        assert "function sourceControlVisibility(caps)" in html
        assert "Boolean(capabilities.jamendo)" in html
        assert "Boolean(capabilities.charts_reload)" in html
        assert "jamendoSourceAvailable" not in html


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

    def test_admin_duration_rendering_has_no_type_based_fake_fallbacks(self):
        html = ADMIN_HTML.read_text()
        forbidden = [
            r"typeKey==='music'\?240",
            r"typeKey==='banter'\?30",
            r"typeKey==='ad'\?60",
            r"typeKey==='news_flash'\?20",
        ]
        for pattern in forbidden:
            assert not re.search(pattern, html), f"admin.html reintroduced fake duration fallback: {pattern}"
        assert "function durationSec(item)" in html


# ── Item 21: scheduler reason strings must not leak into admin queue rows ─────


class TestSchedulerReasonsDoNotLeakToUI:
    """Scheduler exposes a `reason` field on each upcoming entry for debugging.
    The admin UI must not render those developer-copy strings as row text.
    """

    def test_scheduler_reason_strings_are_developer_copy(self):
        # Anchor the exact strings that must never appear in listener/admin-visible text.
        from mammamiradio.scheduling.scheduler import _reason_for_decision

        assert _reason_for_decision("ad_due") == "Ad pacing threshold reached."
        assert _reason_for_decision("music_default") == "No pacing trigger active; continue music flow."

    def test_admin_html_does_not_render_it_reason_into_rows(self):
        # Regression guard: the admin queue render path stripped out `it.reason`
        # on 2026-04-17. Re-adding it would re-introduce "pacing threshold reached"
        # and "No pacing trigger active" rows in the up-next queue.
        html = ADMIN_HTML.read_text()

        # Find the upcoming-rows render section (starts at `upFiltered.slice(0,8).forEach`).
        start = html.find("upFiltered.slice(0,8).forEach")
        assert start != -1, "could not locate upcoming-rows render section"
        # Scan through to the end of that forEach block.
        end = html.find("});", start) + 3
        block = html[start:end]

        # The scheduler's `reason` field must not be templated into the row HTML.
        assert "${reason}" not in block, (
            "admin.html upcoming-rows render is interpolating scheduler "
            "reason strings again — these are developer copy and must stay "
            "out of user-facing row text (Item 21)."
        )
        assert "||reason}" not in block, (
            "admin.html upcoming-rows render is falling back to reason text "
            "when artist is empty — this re-leaks scheduler internals."
        )
        assert "it.reason" not in block


class TestPacingControlsMatchServerContract:
    def test_admin_banter_slider_uses_server_floor(self):
        html = ADMIN_HTML.read_text()

        assert 'id="pBanter" min="2"' in html
        assert 'id="pacingMeta">Banter every 2 tracks' in html
        assert "pacing.songs_between_banter||2" in html

    def test_admin_pacing_changes_send_partial_patch(self):
        html = ADMIN_HTML.read_text()

        assert "savePacingField(PACE_FIELDS[n],el.value)" in html
        assert "api('PATCH','/api/pacing',{[field]:+value})" in html
        assert "songs_between_banter:+document.getElementById('pBanter').value" not in html

    def test_admin_pacing_save_applies_response_and_resyncs_on_rejection(self):
        """Slider saves must re-render only their own field from the server
        response and roll that field back when a PATCH is rejected."""
        html = ADMIN_HTML.read_text()

        # A save re-renders only the field it patched, never sibling sliders.
        assert "applyPacingResponse(r,field)" in html
        # On a rejected save the field re-syncs from the last server snapshot.
        assert "if(!r||r.detail||r.ok===false){" in html
        assert "if(_st.pacing)applyPacingResponse(_st.pacing,field);" in html

    def test_admin_quick_pacing_actions_check_save_result(self):
        """less_banter / too_many_ads must not toast success on a failed save."""
        html = ADMIN_HTML.read_text()

        assert "savePacingField('songs_between_banter',v,{silent:true})" in html
        assert "savePacingField('songs_between_ads',v,{silent:true})" in html
        # Per-field sequence guard: a stale response cannot roll the field back.
        assert "_paceSeq[field]" in html
        assert "if(mySeq===_paceSeq[field])applyPacingResponse(r,field)" in html


class TestPoolDiagnosticsStayHidden:
    """Scheduler pool diagnostics are internal state, not operator programme copy."""

    def test_admin_html_does_not_render_pool_pass_annotations(self):
        html = ADMIN_HTML.read_text()
        start = html.find("upFiltered.slice(0,8).forEach")
        assert start != -1, "could not locate upcoming-rows render section"
        end = html.find("});", start) + 3
        block = html[start:end]

        assert "pool-pass" not in block
        assert "not selected this pass" not in block
        assert "moreSuffix" not in block
        assert "pool.length" not in block


# ── Item 11: capabilities status reflects runtime health, not just key presence ─


class TestCapabilitiesStatusIsHonest:
    """The admin engine room must show three states for Anthropic — connected,
    suspended (auth failed, OpenAI fallback active), not configured — instead of
    claiming "connected" whenever a key is set in config.
    """

    def test_admin_html_reads_anthropic_degraded_flag(self):
        # Regression guard: the render for the Anthropic line must consult
        # `anthropic_degraded`, not just key presence. Re-introducing a
        # presence-only render would be Item 11 regression.
        html = ADMIN_HTML.read_text()
        assert "anthropic_degraded" in html, (
            "admin.html engine-room capabilities render must consult "
            "`c.anthropic_degraded` so the dot can't lie about a 401'd key "
            "(Item 11). If you removed the runtime-health check, UI will "
            "once again show ✓ connected while scriptwriter is suspended."
        )

    def test_admin_html_renders_suspended_state_label(self):
        # Anchor the copy so a future refactor can't silently collapse the
        # three-state render back into connected/not-set.
        html = ADMIN_HTML.read_text()
        assert "suspended" in html, (
            "admin.html should render a suspended-state label when Anthropic "
            "auth failed and we're falling back to OpenAI (Item 11)."
        )
        assert "retry in" in html, (
            "admin.html should surface the retry countdown when Anthropic is in backoff (Item 11)."
        )

    def test_admin_html_renders_key_not_working_state(self):
        # A bogus key (active-validation verdict "rejected") must render a distinct,
        # persistent not-working state keyed on key_status — NOT reuse the transient
        # amber "suspended" path. Both Anthropic and OpenAI consult the verdict.
        html = ADMIN_HTML.read_text()
        assert "anthropic_key_status" in html and "openai_key_status" in html, (
            "admin.html engine-room render must consult `c.anthropic_key_status` and "
            "`c.openai_key_status` so a key refused at boot reads as not-working before "
            "any banter fails."
        )
        assert "key not working" in html, (
            "admin.html must render a plain 'key not working' state for an auth-rejected key, "
            "distinct from the transient 'suspended' fallback."
        )
        assert "rejected" in html, "the not-working branch must key on the 'rejected' verdict value."


class TestRuntimeProviderTransparencyUI:
    def test_admin_html_has_runtime_status_card_and_header_health(self):
        html = ADMIN_HTML.read_text()

        assert 'id="runtimeStatusCard"' in html
        assert 'id="headerHealth"' in html
        assert 'class="status-dot working"' in html
        assert '<span class="dot"></span>Checking' in html

    def test_runtime_status_render_uses_normalized_status_contract(self):
        html = ADMIN_HTML.read_text()

        assert "function updateRuntimeStatus(st)" in html
        assert "st?.runtime_status" in html
        assert "providers.audio_source" in html
        assert "providers.script_provider" in html
        assert "providers.tts_provider" in html
        assert "no_failover_message" in html
        assert "status_card_render_errors" in html

    @pytest.mark.asyncio
    async def test_status_endpoint_exposes_runtime_status_contract(self):
        app = _make_app(
            now_streaming={
                "type": "music",
                "label": "Song",
                "started": time.time(),
                "metadata": {"audio_source": "charts"},
            },
            anthropic_key="sk-ant-test-key",
            openai_key="sk-openai-test-key",
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/status", headers=AUTH)

        assert resp.status_code == 200
        runtime_status = resp.json()["runtime_status"]
        assert runtime_status["health_state"] in {"ready", "degraded", "blocked"}
        assert isinstance(runtime_status["station_on_air"], bool)
        assert isinstance(runtime_status["failover_events"], list)
        assert "no_failover_message" in runtime_status
        assert set(runtime_status["providers"]) == {"audio_source", "script_provider", "tts_provider"}
        for name, provider in runtime_status["providers"].items():
            assert provider["provider_class"] == name
            assert "primary_provider" in provider
            assert "primary_label" in provider
            assert "current_provider" in provider
            assert "current_label" in provider
            assert "fallback_active" in provider
            assert "last_switch_timestamp" in provider
            assert "switch_reason" in provider
            assert "recovery_mode" in provider
            assert "retry_in_seconds" in provider
            assert "action_guidance" in provider

        # #547: producer rescue-bridge health rides along for the Queue rescue card.
        bridge_health = runtime_status["bridge_health"]
        assert isinstance(bridge_health["session_count"], int)
        assert isinstance(bridge_health["window_count"], int)
        assert isinstance(bridge_health["unhealthy"], bool)
        assert set(bridge_health["by_type"]) == {"drain", "resume", "idle"}
        assert bridge_health["threshold"] >= 1
        assert bridge_health["window_seconds"] > 0
        assert "last_fire" in bridge_health
        assert "queue_empty_elapsed_s" in bridge_health
        assert "unhealthy_reasons" in bridge_health

        # #547: producer headroom makes the runway target visible instead of
        # relying on a raw queue-depth count hidden in logs.
        producer_headroom = runtime_status["producer_headroom"]
        assert isinstance(producer_headroom["queue_depth"], int)
        assert isinstance(producer_headroom["queue_capacity"], int)
        assert producer_headroom["lookahead_target"] >= 4
        assert isinstance(producer_headroom["buffered_audio_sec"], int | float)
        assert isinstance(producer_headroom["headroom_ok"], bool)
        assert producer_headroom["reason"] in {"ready runway", "building runway"}

    def test_status_helpers_emit_accessible_state_labels(self):
        html = ADMIN_HTML.read_text()

        assert 'aria-label="${esc(safeTitle)}"' in html
        assert 'aria-label="status: working"' in html
        assert "header.setAttribute('aria-label',headerDetail)" in html


# ── Item 19: stopped-state UI actually stops (timer, waveform, producer btns) ──


class TestStoppedStateQuietsTheUI:
    """When the station is paused, the UI must visibly stop too — animations
    freeze, the elapsed timer stops ticking, and producer action buttons
    (Banter/Ad/News triggers, quick actions) dim/disable because firing any of
    them against a stopped stream is a no-op footgun.
    """

    def test_admin_html_toggles_data_stopped_on_body(self):
        html = ADMIN_HTML.read_text()
        assert "setAttribute('data-stopped'" in html, (
            "admin.html updateStopState() must flip a global `data-stopped` "
            "attribute on <body> so CSS can freeze animations + dim producer "
            "controls declaratively (Item 19)."
        )

    def test_admin_html_has_css_rules_for_stopped_animations_and_buttons(self):
        html = ADMIN_HTML.read_text()
        # Animations pause
        assert 'body[data-stopped="true"]' in html and "animation-play-state: paused" in html, (
            "admin.html must pause animations under the stopped state (Item 19)."
        )
        # Producer buttons dim + become unclickable
        assert 'body[data-stopped="true"] .btn-trigger' in html, (
            "admin.html must dim producer trigger buttons when stopped (Item 19)."
        )
        assert "pointer-events: none" in html, "admin.html must disable producer buttons under stopped state (Item 19)."

    def test_admin_html_clears_tick_interval_on_stop(self):
        html = ADMIN_HTML.read_text()
        assert "clearInterval(_tick)" in html, (
            "admin.html updateStopState() must clearInterval the elapsed-timer "
            "tick so the top-left counter freezes instead of counting past a "
            "stopped stream (Item 19)."
        )

    def test_listener_html_toggles_data_stopped_on_body(self):
        # Post site-v1 refactor the listener CSS + JS live in static/listener.*;
        # the data-stopped invariant is still required but the markers now
        # live across html + css + js. Read all three so this test still
        # fires if the stopped-state feature gets stripped silently.
        base = WEB_ROOT
        blob = (
            LISTENER_HTML.read_text()
            + (base / "static" / "listener.css").read_text()
            + (base / "static" / "listener.js").read_text()
            # Also consult base.css — the unified waveform pause rule lives there.
            + (base / "static" / "base.css").read_text()
        )
        assert "setAttribute('data-stopped'" in blob, (
            "listener fetchStatus must flip `data-stopped` on <body> "
            "so the waveform freezes when the station is paused (Item 19)."
        )
        # After the waveform unification the class name is `.waveform`; the
        # canonical pause rule in base.css targets
        # `body[data-stopped="true"] .waveform .waveform-bar`.
        assert 'body[data-stopped="true"] .waveform .waveform-bar' in blob, (
            "listener must pause the unified waveform under stopped state."
        )

    def test_admin_banner_copy_does_not_use_harsh_error_tone(self):
        # "Session stopped — hit Resume to continue" read as an error.
        # Current copy is calmer and localized.
        html = ADMIN_HTML.read_text()
        assert "Station paused" in html, "admin.html stopped banner should use calm paused-state copy (Item 19)."
        # Only check the banner element's text, not toast strings in JS callbacks.
        banner_start = html.find('id="stoppedBanner"')
        banner_end = html.find("</div>", banner_start)
        banner_block = html[banner_start:banner_end]
        assert "Session stopped" not in banner_block, (
            "admin.html stopped banner element should not use the harsh "
            "'Session stopped — hit Resume' phrasing (Item 19)."
        )

    def test_listener_stopped_state_never_leaks_internal_label(self):
        # The now-playing strip and the Media Session metadata (lock screen /
        # Bluetooth / CarPlay) must both sanitize the stopped state. If a
        # future refactor drops either branch, the internal "Session stopped"
        # label flows back through `np.label` and lands in front of listeners.
        js = (WEB_ROOT / "static" / "listener.js").read_text()

        # Both surfaces must explicitly handle np.type === 'stopped' and
        # render the brand-voice paused copy — not fall through to a
        # default branch that re-emits np.label. The actual displayed string
        # comes from the Super-Italian-Mode copy bag (np_paused), which renders
        # "Paused" by default and "In pausa" when the toggle is on. Both modes
        # share the same key, so we assert the lookup, not the literal.
        assert js.count("np.type === 'stopped'") >= 2, (
            "listener.js must handle the stopped type in BOTH renderNowPlayingStrip "
            "and updateMediaSession; a single branch leaks the raw label to whichever "
            "surface lacks the guard."
        )
        assert "_t('np_paused'" in js, (
            "listener.js stopped branches must render the np_paused copy key — "
            "either branch falling back to np.label would leak the internal "
            "'Session stopped' label."
        )

        # The Media Session album field is broadcast to lock screen / Bluetooth
        # / CarPlay. Hardcoding any city or frequency here re-leaks brand state
        # that should come from radio.toml [brand] (and the public-status feed).
        # Guard the boundary lookups so a future rename can't make this assertion
        # pass vacuously against an empty slice.
        ms_start = js.find("function updateMediaSession")
        assert ms_start != -1, (
            "could not locate function updateMediaSession() in listener.js — "
            "rename or refactor likely; update this guard."
        )
        ms_end = js.find("\n  }\n", ms_start)
        assert ms_end != -1, (
            "could not locate the closing brace of updateMediaSession() — "
            "indentation/formatting changed; update this guard."
        )
        ms_block = js[ms_start:ms_end]
        for leaked in ("Milano", "Napoli", "96,7 FM", "Session stopped", "STOPPED"):
            assert leaked not in ms_block, (
                f"updateMediaSession() must not hardcode {leaked!r} — Media Session "
                "metadata is broadcast to OS-level surfaces and brand state belongs in "
                "radio.toml / /public-status."
            )

    def test_admin_station_card_uses_status_stream_metadata(self):
        html = ADMIN_HTML.read_text()
        assert "96.7" not in html
        assert "320 kbps" not in html
        assert 'id="stationSignal"' in html
        assert "st?.stream?.frequency" in html
        assert "st?.stream?.bitrate_kbps" in html

    def test_admin_scaletta_is_forward_only(self):
        html = ADMIN_HTML.read_text()
        block = html[html.index("function renderProgramme") : html.index("async function removeQueueItem")]
        assert "st?.upcoming" in block
        assert "stream_log" not in block
        assert "now_streaming" not in block

    def test_render_programme_has_distinct_stopped_empty_state(self):
        """When the queue is empty AND the station is stopped, the Scaletta must
        show the paused copy — not the generic "preparing next segment" copy.
        A listener-facing illusion bug if the two states collapse."""
        html = ADMIN_HTML.read_text()
        block = html[html.index("function renderProgramme") : html.index("async function removeQueueItem")]
        assert "data-stopped" in block, (
            "renderProgramme() must branch on body[data-stopped] so the empty "
            "Scaletta distinguishes a paused station from one building its queue."
        )
        assert "Station paused" in block, "renderProgramme() stopped branch must render the paused-state copy."
        assert "Preparing the next segment" in block, (
            "renderProgramme() must keep the building-queue copy for the running-but-empty case."
        )

    def test_render_programme_memo_hash_tracks_id_and_stop_state(self):
        """The renderProgramme() memoization hash must include each row's queue
        `id` and the `session_stopped` flag.

        Row actions are generated from `it.id`; if the hash omits `id`, a queue
        advance that swaps in a segment with identical visible fields leaves the
        table un-rendered with a stale id, so `/api/queue/remove` no-ops. If it
        omits `session_stopped`, the empty-state copy can stick across a
        stop/resume.
        """
        html = ADMIN_HTML.read_text()
        block = html[html.index("function renderProgramme") : html.index("async function removeQueueItem")]
        hash_line = next(line for line in block.splitlines() if "const hash=" in line)
        assert "u.id" in hash_line, "renderProgramme() memo hash must include each row's queue id."
        assert "session_stopped" in hash_line, "renderProgramme() memo hash must include the session_stopped flag."

    def test_refresh_fast_syncs_stop_state_from_status_poll(self):
        """The status poll must drive `data-stopped` from `session_stopped` so a
        stop/resume triggered elsewhere (other tab, HA, API) is reflected without
        a button press — otherwise admin and listener disagree on stream state."""
        html = ADMIN_HTML.read_text()
        block = html[html.index("async function refreshFast") :]
        block = block[: block.index("\n}")]
        assert "session_stopped" in block, "refreshFast() must read session_stopped from the status payload."
        assert "updateStopState" in block, (
            "refreshFast() must call updateStopState() so the poll keeps the stopped UI in sync with the server."
        )


class TestFaderDownEmptyRoomUI:
    """When the transport pauses because no one is listening, the admin should
    show a cued, waiting state without treating the station as stopped."""

    def test_admin_derives_fader_down_from_status_listener_count(self):
        helper = _admin_function_block("activeListenerCount")
        refresh = _admin_function_block("refreshFast")

        assert "st?.listeners?.active" in helper, (
            "Fader Down must read the canonical /status listeners.active value, "
            "not infer empty-room state from playback metadata alone."
        )
        assert "st?.listeners_active" in helper, "legacy listener-count fallback should remain harmless."
        assert "updateFaderDownState(_st)" in refresh, "refreshFast() must derive Fader Down from each /status poll."

    def test_fader_down_copy_is_distinct_from_stopped_and_building(self):
        html = ADMIN_HTML.read_text()
        programme = _admin_function_block("renderProgramme")

        assert 'data-fader-down="true"' in html
        assert "The record is cued. Waiting for someone to tune in." in html
        assert "Fader Down — the record is cued for the next listener." in programme
        assert "Station paused — press Start to resume." in programme
        assert "Preparing the next segment..." in programme

    def test_fader_down_freezes_elapsed_timer_without_waiting_for_stop(self):
        freeze = _admin_function_block("freezeFaderDownProgress")
        update_now = _admin_function_block("updateNow")

        assert "clearInterval(_tick)" in freeze
        assert "_tick=null" in freeze
        assert "if(_faderDownActive){freezeFaderDownProgress();return;}" in update_now, (
            "updateNow() must not restart the elapsed interval while the empty-room state is active."
        )

    def test_fader_down_snapshot_is_read_back_so_the_value_holds_steady(self):
        # Regression guard: a captured-but-never-read snapshot let the "frozen"
        # counter climb on every /status poll (it recomputed elapsed from
        # Date.now()-_nowStart each call). The freeze must REUSE the snapshot,
        # not just write it, so the displayed elapsed/width hold steady.
        freeze = _admin_function_block("freezeFaderDownProgress")
        update_now = _admin_function_block("updateNow")

        assert "if(_faderDownSnapshot)" in freeze, (
            "freezeFaderDownProgress() must read _faderDownSnapshot back to hold "
            "the value steady, not recompute elapsed from wall-clock every poll."
        )
        assert "({elapsed,width}=_faderDownSnapshot)" in freeze, (
            "the held snapshot must be consumed as the displayed elapsed/width."
        )
        assert "_faderDownSnapshot=null" in update_now, (
            "a newly cued record must invalidate the snapshot so it re-snapshots "
            "near 0:00 instead of freezing on the prior record's value."
        )
        # Record-change detection must key on more than the bare label: two
        # different cued records can share a label, and the stale snapshot has
        # to drop anyway. Guard against regressing to label-only comparison.
        assert "_prevNowKey" in update_now, "updateNow() must compare a composite record key, not the bare label."
        assert "ns.started" in update_now and "typeKey" in update_now, (
            "the record key must fold in type and start time so a same-label "
            "different record still invalidates the frozen snapshot."
        )

    def test_fader_down_does_not_take_over_stop_resume_authority(self):
        refresh = _admin_function_block("refreshFast")
        fader = _admin_function_block("updateFaderDownState")

        assert "updateStopState(_st.session_stopped)" in refresh, (
            "session_stopped must remain the only authority for the stopped UI."
        )
        assert "/api/stop" not in fader
        assert "/api/resume" not in fader
        assert "updateStopState(" not in fader
        assert "session_stopped=" not in fader

    @pytest.mark.asyncio
    async def test_status_exposes_listener_counts_for_admin_fader_down(self):
        app = _make_app(now_streaming={"type": "music", "label": "Song A", "started": time.time(), "metadata": {}})
        app.state.station_state.listeners_active = 0
        app.state.station_state.listeners_peak = 2
        app.state.station_state.listeners_total = 5

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/status", headers=AUTH)

        assert resp.status_code == 200
        assert resp.json()["listeners"] == {"active": 0, "peak": 2, "total": 5}


class TestHostBlockSelectorScoping:
    """Two elements share `data-h="<host>"`: the script preview's
    `<span class="script-host">` (renders inside #recentBody, higher in the DOM)
    and the Hosts card's `<div class="host-block">` (lower in the DOM). A bare
    `[data-h="..."]` selector resolves to the script-host span first, so preset
    buttons, slider commits, and reset all silently target an element with no
    range inputs and no `.host-preset` children. Scope the selectors to
    `.host-block[data-h=...]` to keep them pointing at the Hosts card.
    """

    def test_host_block_selectors_are_scoped(self):
        html = ADMIN_HTML.read_text()
        for line in html.splitlines():
            stripped = line.strip()
            if "data-h=" not in stripped or "querySelector" not in stripped:
                continue
            assert ".host-block[data-h=" in stripped, (
                "querySelector lookups by data-h must be scoped to .host-block — "
                "otherwise they collide with <span class='script-host' "
                f"data-h='...'> in the recent-script preview. Offending line:\n  {stripped}"
            )
