"""Fast contract coverage for the private Moment Picker capture lifecycle."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque

import httpx
import pytest

from mammamiradio.core.models import Segment, SegmentType
from mammamiradio.web.streamer import (
    CAPTURE_MAX_RECORDS,
    SegmentMark,
    _append_clip_chunk,
    _CaptureRecord,
    _CaptureState,
)
from tests.web.test_streamer_routes import _make_test_app


def _mpeg1_l3_frames(count: int = 1_100) -> bytes:
    """Build parser-valid CBR frame bytes; decoder proof lives in FFmpeg tests."""

    # MPEG-1 Layer III, 128 kbps, 44.1 kHz, no padding: 417-byte frames.
    header = b"\xff\xfb\x90\x00"
    return (header + (b"\0" * 413)) * count


def _seed_capture_timeline(
    app, *, audio_class: str = "speech", segment_type: SegmentType = SegmentType.BANTER
) -> bytes:
    payload = _mpeg1_l3_frames()
    app.state.clip_ring_buffer = deque()
    app.state.clip_buffer_max_bytes = len(payload) + 4096
    app.state.clip_buffer_bytes = 0
    app.state.clip_buffer_start_byte = 0
    app.state.clip_bytes_total = 0
    app.state.clip_generation = 0
    app.state.clip_marks = []
    segment = Segment(
        type=segment_type,
        path=app.state.config.cache_dir / "unneeded.mp3",
        metadata={"title": "Una frase riuscita", "artist": "Studio", "clip_audio_class": audio_class},
    )
    # The helper under test normally receives this from run_playback_loop right
    # after on_stream_segment. Keeping it explicit makes the capture fixture
    # frame-only and independent of a background playback task.
    from mammamiradio.web.streamer import _mark_clip_segment_start

    _mark_clip_segment_start(app, segment)
    for offset in range(0, len(payload), 4096):
        _append_clip_chunk(app, payload[offset : offset + 4096])
    return payload


def _transport(app) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))


@pytest.mark.asyncio
async def test_capture_creates_frame_aligned_preview_and_idempotent_frozen_commit(tmp_path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _seed_capture_timeline(app)

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://testserver") as client:
        capture_response = await client.post("/api/clip/capture")
        assert capture_response.status_code == 201
        capture = capture_response.json()
        assert capture["ok"] is True
        assert len(capture["capture_id"]) == 43
        assert capture["audio_path"] == f"/captures/{capture['capture_id']}.mp3"
        assert [choice["choice_id"] for choice in capture["choices"]] == [
            "moment",
            "with_leadin",
            "with_followup",
        ]
        assert all(choice["duration_sec"] > 0 for choice in capture["choices"])
        assert capture["chapters"]

        preview = await client.get(capture["audio_path"])
        assert preview.status_code == 200
        assert preview.headers["cache-control"].startswith("no-store")
        # The preview starts with an actual stored frame header, not a synthetic
        # one reconstructed at a byte offset.
        assert preview.content.startswith(b"\xff\xfb\x90\x00")

        commit_response = await client.post(
            "/api/clip/commit",
            json={"capture_id": capture["capture_id"], "choice_id": "moment"},
        )
        assert commit_response.status_code == 201
        commit = commit_response.json()
        assert commit["ok"] is True
        assert commit["idempotent"] is False

        retry = await client.post(
            "/api/clip/commit",
            json={"capture_id": capture["capture_id"], "choice_id": "moment"},
        )
        assert retry.status_code == 200
        assert retry.json() == {**commit, "idempotent": True}

        competing = await client.post(
            "/api/clip/commit",
            json={"capture_id": capture["capture_id"], "choice_id": "with_leadin"},
        )
        assert competing.status_code == 409
        assert competing.json() == {"ok": False, "reason": "capture_claimed"}

        landing = await client.get(commit["share_url"])
        assert landing.status_code == 200
        assert "Il momento" in landing.text
        assert "Questo momento è durato" in landing.text

    sidecar = json.loads((app.state.config.cache_dir / "clips" / f"{commit['clip_id']}.json").read_text())
    assert sidecar["clip_label"] == "Il momento"
    assert sidecar["duration_sec"] > 0
    assert sidecar["chapter_summary"] == ["Una frase riuscita"]


@pytest.mark.asyncio
async def test_capture_rejects_raw_ranges_and_unknown_choices(tmp_path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _seed_capture_timeline(app)

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://testserver") as client:
        capture = (await client.post("/api/clip/capture")).json()
        invalid = await client.post(
            "/api/clip/commit",
            json={"capture_id": capture["capture_id"], "choice_id": "moment", "in_sec": 0},
        )
        assert invalid.status_code == 400
        assert invalid.json() == {"ok": False, "reason": "invalid_request"}

        unknown = await client.post(
            "/api/clip/commit",
            json={"capture_id": capture["capture_id"], "choice_id": "browser-made-range"},
        )
        assert unknown.status_code == 409
        assert unknown.json() == {"ok": False, "reason": "invalid_choice"}


@pytest.mark.asyncio
async def test_unknown_or_commercial_audio_is_generic_and_capped(tmp_path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _seed_capture_timeline(app, audio_class="commercial_music", segment_type=SegmentType.MUSIC)

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://testserver") as client:
        response = await client.post("/api/clip/capture")
    assert response.status_code == 201
    body = response.json()
    assert [choice["choice_id"] for choice in body["choices"]] == ["moment"]
    assert body["duration_sec"] <= 60
    assert body["chapters"] == []


@pytest.mark.asyncio
async def test_capture_expiry_denies_new_preview_requests(tmp_path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _seed_capture_timeline(app)

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://testserver") as client:
        capture = (await client.post("/api/clip/capture")).json()
        app.state.capture_records[capture["capture_id"]].expires_monotonic = time.monotonic() - 1
        response = await client.get(capture["audio_path"])
    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "capture_expired"}


@pytest.mark.asyncio
async def test_station_stop_invalidates_uncommitted_capture(tmp_path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _seed_capture_timeline(app)

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://testserver") as client:
        capture = (await client.post("/api/clip/capture")).json()
        stopped = await client.post("/api/stop")
        assert stopped.status_code == 200
        preview = await client.get(capture["audio_path"])
    assert preview.status_code == 404
    assert preview.json() == {"ok": False, "reason": "capture_expired"}


@pytest.mark.asyncio
async def test_capture_capacity_is_reserved_before_worker_runs(tmp_path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _seed_capture_timeline(app)
    app.state.capture_lock = asyncio.Lock()
    app.state.capture_rate = {}
    app.state.capture_records = {
        f"{index:043d}"[-43:]: _CaptureRecord(
            capture_id=f"{index:043d}"[-43:],
            nonce=str(index),
            generation=0,
            state=_CaptureState.CREATING,
            created_monotonic=time.monotonic(),
            expires_monotonic=time.monotonic() + 60,
            build_deadline_monotonic=time.monotonic() + 60,
        )
        for index in range(CAPTURE_MAX_RECORDS)
    }

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://testserver") as client:
        response = await client.post("/api/clip/capture")
    assert response.status_code == 503
    assert response.json() == {"ok": False, "reason": "capture_busy", "retry_after": 3}
    assert response.headers["retry-after"] == "3"


def test_manual_byte_ledger_prunes_only_fully_evicted_closed_marks(tmp_path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    app.state.clip_ring_buffer = deque()
    app.state.clip_buffer_max_bytes = 5
    app.state.clip_buffer_bytes = 0
    app.state.clip_buffer_start_byte = 0
    app.state.clip_bytes_total = 0
    app.state.clip_generation = 0
    app.state.clip_marks = [
        SegmentMark(0, 0, 3, "closed", "banter", "Old", "", "speech"),
        SegmentMark(0, 3, None, "open", "banter", "Live", "", "speech"),
    ]

    _append_clip_chunk(app, b"abc")
    _append_clip_chunk(app, b"def")

    assert list(app.state.clip_ring_buffer) == [b"def"]
    assert app.state.clip_buffer_start_byte == 3
    # Closed mark ending exactly at the retained edge is pruned; the active
    # mark remains as the only boundary the next segment can close.
    assert [mark.segment_id for mark in app.state.clip_marks] == ["open"]
