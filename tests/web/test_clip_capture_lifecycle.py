"""Lifecycle and fail-closed coverage for the private Moment Picker protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import patch

import httpx
import pytest

from mammamiradio.core.models import Segment, SegmentType
from mammamiradio.web.mp3_frames import Mp3FrameIndexError, build_mpeg1_layer3_frame_index
from mammamiradio.web.streamer import (
    CAPTURE_COMMIT_GRACE_SECONDS,
    CAPTURE_RATE_PRUNE_SECONDS,
    SegmentMark,
    _append_clip_chunk,
    _build_capture_source,
    _CaptureNoAudioError,
    _CaptureRecord,
    _CaptureState,
    _chapter_payload,
    _ClipSnapshot,
    _collect_expired_captures,
    _context_range,
    _default_choice_range,
    _ensure_clip_capture_state,
    _finalize_capture_choice,
    _frame_range_for_mark,
    _frame_range_inside_bytes,
    _invalidate_pending_captures,
    _mark_clip_segment_start,
    _range_contained_in,
    _release_capture_reader,
    _reset_clip_timeline,
    _safe_voice_run,
    _snapshot_retained_audio,
    _source_choice,
    initialize_clip_capture_runtime,
)
from tests.web.test_streamer_routes import _make_test_app


def _mpeg1_l3_frames(count: int = 1_500) -> bytes:
    """Build a parser-valid CBR frame run; FFmpeg decoding has its own suite."""

    # MPEG-1 Layer III, 128 kbps, 44.1 kHz, no padding: 417-byte frames.
    return (b"\xff\xfb\x90\x00" + (b"\0" * 413)) * count


def _transport(app) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))


def _seed_timeline(
    app,
    *,
    audio_class: str = "speech",
    segment_type: SegmentType = SegmentType.BANTER,
    payload: bytes | None = None,
) -> bytes:
    payload = payload or _mpeg1_l3_frames()
    app.state.clip_ring_buffer = deque()
    app.state.clip_buffer_max_bytes = len(payload) + 4096
    app.state.clip_buffer_bytes = 0
    app.state.clip_buffer_start_byte = 0
    app.state.clip_bytes_total = 0
    app.state.clip_generation = 0
    app.state.clip_marks = []
    _mark_clip_segment_start(
        app,
        Segment(
            type=segment_type,
            path=app.state.config.cache_dir / "unneeded.mp3",
            metadata={"title": "Una frase riuscita", "artist": "Studio", "clip_audio_class": audio_class},
        ),
    )
    for offset in range(0, len(payload), 4096):
        _append_clip_chunk(app, payload[offset : offset + 4096])
    return payload


def _record(
    capture_id: str,
    state: _CaptureState,
    *,
    source_path: Path | None = None,
    active_readers: int = 0,
    expires_in: float = 60.0,
    build_deadline_in: float = 60.0,
    consumed_at: float | None = None,
) -> _CaptureRecord:
    now = time.monotonic()
    return _CaptureRecord(
        capture_id=capture_id,
        nonce=f"nonce-{capture_id}",
        generation=0,
        state=state,
        created_monotonic=now,
        expires_monotonic=now + expires_in,
        build_deadline_monotonic=now + build_deadline_in,
        source_path=source_path,
        active_readers=active_readers,
        consumed_monotonic=consumed_at,
    )


def _snapshot_with_marks(data: bytes, marks: tuple[SegmentMark, ...]) -> _ClipSnapshot:
    return _ClipSnapshot((data,), marks, 0, len(data), 0)


@pytest.mark.asyncio
async def test_capture_runtime_cleans_orphans_and_tracks_one_maintenance_task(tmp_path: Path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    captures = app.state.config.cache_dir / "captures"
    captures.mkdir(parents=True)
    orphan = captures / "old.mp3.part"
    orphan.write_bytes(b"left behind")
    (captures / "nested").mkdir()

    await initialize_clip_capture_runtime(app)
    task = app.state.capture_maintenance_task
    assert not orphan.exists()
    assert task in app.state.background_tasks

    # Startup is idempotent: it must not create competing maintenance loops.
    await initialize_clip_capture_runtime(app)
    assert app.state.capture_maintenance_task is task

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_capture_timeline_ledger_and_snapshot_fail_closed(tmp_path: Path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"

    # Legacy narrow test apps can still use a count-capped deque without an
    # absolute byte ledger; we retain their old append behavior without inventing
    # a capture boundary.
    app.state.clip_ring_buffer = deque(maxlen=1)
    _append_clip_chunk(app, b"first")
    _append_clip_chunk(app, b"second")
    assert list(app.state.clip_ring_buffer) == [b"second"]
    assert _snapshot_retained_audio(app) is None

    _seed_timeline(app)
    first_mark = app.state.clip_marks[-1]
    _mark_clip_segment_start(
        app,
        Segment(
            type=SegmentType.BANTER,
            path=tmp_path / "later.mp3",
            metadata={"clip_audio_class": "not-a-real-class"},
        ),
    )
    assert first_mark.byte_end == app.state.clip_bytes_total
    assert app.state.clip_marks[-1].clip_audio_class == "unknown"

    snapshot = _snapshot_retained_audio(app)
    assert snapshot is not None
    assert snapshot.marks[-1].byte_end == snapshot.buffer_end_byte

    # A bypassed writer or mutated test app cannot make the capture endpoint
    # derive a false byte-to-time mapping.
    app.state.clip_bytes_total += 1
    assert _snapshot_retained_audio(app) is None
    app.state.clip_bytes_total -= 1

    old_generation = app.state.clip_generation
    _reset_clip_timeline(app)
    assert not app.state.clip_ring_buffer
    assert app.state.clip_marks == []
    assert app.state.clip_generation == old_generation + 1


def test_frame_choice_and_provenance_helpers_keep_only_safe_context() -> None:
    data = _mpeg1_l3_frames()
    index = build_mpeg1_layer3_frame_index(data)
    split = index.frames[500].byte_start
    split_end = index.frames[1_000].byte_start
    safe_marks = (
        SegmentMark(0, 0, split, "intro", "banter", "", "", "speech"),
        SegmentMark(0, split, split_end, "news", "news_flash", "Bolletino", "", "station_bed"),
        SegmentMark(0, split_end, len(data), "tail", "banter", "Saluto", "", "speech"),
    )
    snapshot = _snapshot_with_marks(data, safe_marks)

    assert _frame_range_inside_bytes(index, 0, index.frames[5].byte_end) == (0, 6)
    assert _frame_range_inside_bytes(index, 1, 2) is None
    assert _frame_range_for_mark(index, snapshot, safe_marks[0]) == (0, 500)
    assert _frame_range_for_mark(index, snapshot, replace(safe_marks[0], byte_end=None)) is None
    assert _frame_range_for_mark(index, snapshot, replace(safe_marks[0], generation=1)) is None
    assert _frame_range_for_mark(index, snapshot, replace(safe_marks[0], byte_start=-1)) is None
    assert _frame_range_for_mark(index, snapshot, replace(safe_marks[0], byte_end=len(data) + 1)) is None

    assert _range_contained_in(index, 0, len(index.frames), 90.0) == (0, len(index.frames))
    capped = _range_contained_in(index, 0, len(index.frames), 2.0)
    assert capped is not None
    assert index.duration_for(*capped) <= 2.0
    assert _range_contained_in(index, 0, len(index.frames), 0.0) is None

    default = _default_choice_range(index, 0, len(index.frames))
    assert default is not None
    assert _default_choice_range(index, 0, 100) is None
    assert _context_range(index, 0, len(index.frames), default, before=True) is not None
    assert _context_range(index, 0, len(index.frames), default, before=False) is not None
    assert _context_range(index, default[0], default[1], default, before=True) is None
    assert _context_range(index, default[0], default[1], default, before=False) is None

    safe_run = _safe_voice_run(index, snapshot)
    assert safe_run is not None
    (run_first, run_end), run_marks = safe_run
    assert (run_first, run_end) == (0, len(index.frames))
    assert [mark.segment_id for mark in run_marks] == ["intro", "news", "tail"]

    # The last aired class gates named chapters. A past safe segment is never
    # pulled forward across later commercial or unknown bytes.
    commercial = _snapshot_with_marks(
        data,
        (*safe_marks[:-1], replace(safe_marks[-1], clip_audio_class="unknown")),
    )
    assert _safe_voice_run(index, commercial) is None

    gapped = _snapshot_with_marks(
        data,
        (
            safe_marks[0],
            replace(safe_marks[-1], byte_start=split + (index.frames[500].byte_end - split)),
        ),
    )
    gapped_run = _safe_voice_run(index, gapped)
    assert gapped_run is not None
    assert [mark.segment_id for mark in gapped_run[1]] == ["tail"]

    chapters = _chapter_payload(index, snapshot, 0, len(index.frames), list(safe_marks))
    assert [chapter["label"] for chapter in chapters] == ["In studio", "Bolletino", "Saluto"]
    assert _chapter_payload(index, snapshot, 0, len(index.frames), [replace(safe_marks[0], generation=4)]) == []
    choice = _source_choice(index, 0, len(index.frames), "moment", "Il momento", default, ("news",))
    assert choice.byte_start >= 0
    assert choice.byte_end > choice.byte_start
    assert choice.chapter_ids == ("news",)


def test_capture_source_and_finalizer_are_atomic_and_fail_closed(tmp_path: Path, monkeypatch) -> None:
    data = _mpeg1_l3_frames()
    index = build_mpeg1_layer3_frame_index(data)
    safe_snapshot = _snapshot_with_marks(
        data,
        (SegmentMark(0, 0, len(data), "spoken", "banter", "Una frase", "Studio", "speech"),),
    )
    build = _build_capture_source(
        safe_snapshot,
        capture_id="a" * 43,
        captures_dir=tmp_path / "captures",
        station_name="Mamma Mi Radio",
    )
    assert build.source_path.is_file()
    assert not build.source_path.with_name(build.source_path.name + ".part").exists()
    assert list(build.choices) == ["moment", "with_leadin", "with_followup"]
    assert build.chapters[0]["label"] == "Una frase"

    generic = _build_capture_source(
        _snapshot_with_marks(
            data,
            (SegmentMark(0, 0, len(data), "song", "music", "Song", "Band", "commercial_music"),),
        ),
        capture_id="b" * 43,
        captures_dir=tmp_path / "captures",
        station_name="Mamma Mi Radio",
    )
    assert list(generic.choices) == ["moment"]
    assert generic.chapters == []

    short = _snapshot_with_marks(
        _mpeg1_l3_frames(100),
        (SegmentMark(0, 0, 41_700, "short", "banter", "", "", "speech"),),
    )
    with pytest.raises(_CaptureNoAudioError):
        _build_capture_source(short, capture_id="c" * 43, captures_dir=tmp_path / "captures", station_name="Mamma")
    with pytest.raises(Mp3FrameIndexError):
        _build_capture_source(
            _snapshot_with_marks(b"not an mp3", ()),
            capture_id="d" * 43,
            captures_dir=tmp_path / "captures",
            station_name="Mamma",
        )

    original_write_bytes = Path.write_bytes

    def fail_part_write(path: Path, value: bytes) -> int:
        if path.name == f"{'e' * 43}.mp3.part":
            raise OSError("disk full")
        return original_write_bytes(path, value)

    monkeypatch.setattr(Path, "write_bytes", fail_part_write)
    with pytest.raises(OSError):
        _build_capture_source(
            safe_snapshot,
            capture_id="e" * 43,
            captures_dir=tmp_path / "captures",
            station_name="Mamma",
        )
    assert not (tmp_path / "captures" / f"{'e' * 43}.mp3").exists()
    monkeypatch.setattr(Path, "write_bytes", original_write_bytes)

    result = _finalize_capture_choice(
        build.source_path,
        build.choices["moment"],
        build.frozen_metadata,
        tmp_path / "clips",
    )
    assert (tmp_path / "clips" / f"{result['clip_id']}.mp3").is_file()
    sidecar = json.loads((tmp_path / "clips" / f"{result['clip_id']}.json").read_text())
    assert sidecar["clip_label"] == "Il momento"

    invalid_choice = replace(build.choices["moment"], byte_end=build.source_path.stat().st_size + 1)
    with pytest.raises(OSError):
        _finalize_capture_choice(build.source_path, invalid_choice, build.frozen_metadata, tmp_path / "clips")

    original_replace = __import__("os").replace
    calls = 0
    renames: list[tuple[str, str]] = []

    def fail_second_rename(source: str | Path, destination: str | Path) -> None:
        nonlocal calls
        calls += 1
        renames.append((str(source), str(destination)))
        if calls == 2:
            raise OSError("second atomic publish failed")
        original_replace(source, destination)

    with patch("mammamiradio.web.streamer.os.replace", side_effect=fail_second_rename), pytest.raises(OSError):
        _finalize_capture_choice(build.source_path, build.choices["moment"], build.frozen_metadata, tmp_path / "failed")
    # The public MP3 is the reachability signal, so publish the frozen sidecar
    # first and never allow a landing page to observe a nameless final clip.
    assert renames[0][0].endswith(".json.part")
    assert renames[1][0].endswith(".mp3.part")
    assert not list((tmp_path / "failed").glob("*.part"))
    assert not list((tmp_path / "failed").glob("*.mp3"))
    assert not list((tmp_path / "failed").glob("*.json"))
    assert index.duration_sec > 0


@pytest.mark.asyncio
async def test_capture_expiry_respects_reader_leases_and_stop_invalidation(tmp_path: Path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _ensure_clip_capture_state(app)
    now = time.monotonic()
    reader_path = tmp_path / "reader.mp3"
    reader_path.write_bytes(b"reader")
    consumed_path = tmp_path / "consumed.mp3"
    consumed_path.write_bytes(b"consumed")
    app.state.capture_records = {
        "creating": _record("creating", _CaptureState.CREATING, build_deadline_in=-1),
        "reader": _record("reader", _CaptureState.READY, source_path=reader_path, active_readers=1, expires_in=-1),
        "consumed": _record(
            "consumed",
            _CaptureState.CONSUMED,
            source_path=consumed_path,
            consumed_at=now - CAPTURE_COMMIT_GRACE_SECONDS - 1,
        ),
        "claimed": _record("claimed", _CaptureState.CLAIMED, expires_in=-1),
    }
    app.state.capture_rate = {"stale": now - CAPTURE_RATE_PRUNE_SECONDS - 1, "fresh": now}

    await _collect_expired_captures(app)
    assert "creating" not in app.state.capture_records
    assert "consumed" not in app.state.capture_records
    assert not consumed_path.exists()
    assert app.state.capture_records["reader"].state == _CaptureState.EXPIRED
    assert reader_path.exists()
    assert app.state.capture_records["claimed"].state == _CaptureState.CLAIMED
    assert app.state.capture_rate == {"fresh": now}

    await _release_capture_reader(app, "reader", "wrong-nonce")
    assert app.state.capture_records["reader"].active_readers == 1
    await _release_capture_reader(app, "reader", "nonce-reader")
    assert "reader" not in app.state.capture_records
    assert not reader_path.exists()

    # A station stop expires ready/creating capabilities but cannot steal a
    # claimed finalizer or a prior, already-consumed share result.
    ready = _record("ready", _CaptureState.READY, active_readers=1)
    creating = _record("new", _CaptureState.CREATING, active_readers=1)
    app.state.capture_records = {"ready": ready, "new": creating, "claimed": app.state.capture_records["claimed"]}
    await _invalidate_pending_captures(app)
    assert ready.state == _CaptureState.EXPIRED
    assert creating.state == _CaptureState.EXPIRED
    assert app.state.capture_records["claimed"].state == _CaptureState.CLAIMED


@pytest.mark.asyncio
async def test_capture_routes_refuse_unsafe_states_and_rollback_worker_failures(tmp_path: Path) -> None:
    empty = _make_test_app()
    empty.state.config.cache_dir = tmp_path / "empty"
    async with httpx.AsyncClient(transport=_transport(empty), base_url="http://testserver") as client:
        response = await client.post("/api/clip/capture")
    assert response.status_code == 409
    assert response.json() == {"ok": False, "reason": "no_audio"}

    for error, expected in [
        (_CaptureNoAudioError("short"), (409, "no_audio")),
        (Mp3FrameIndexError("bad"), (409, "format_unavailable")),
        (OSError("disk"), (503, "write_failed")),
    ]:
        app = _make_test_app()
        app.state.config.cache_dir = tmp_path / type(error).__name__
        _seed_timeline(app)
        with patch("mammamiradio.web.streamer._build_capture_source", side_effect=error):
            async with httpx.AsyncClient(transport=_transport(app), base_url="http://testserver") as client:
                response = await client.post("/api/clip/capture")
        assert (response.status_code, response.json()["reason"]) == expected
        assert app.state.capture_records == {}
        assert app.state.capture_rate == {}

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "routes"
    _seed_timeline(app)
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://testserver") as client:
        first = await client.post("/api/clip/capture")
        assert first.status_code == 201
        limited = await client.post("/api/clip/capture")
        assert limited.status_code == 429
        assert limited.headers["retry-after"]
        assert limited.json()["reason"] == "rate_limited"
        assert (await client.get("/captures/not-a-capability.mp3")).status_code == 400

        capture = first.json()
        record = app.state.capture_records[capture["capture_id"]]
        record.state = _CaptureState.CLAIMED
        claimed = await client.get(capture["audio_path"])
        assert claimed.json() == {"ok": False, "reason": "capture_claimed"}

        record.state = _CaptureState.READY
        record.source_path = None
        missing_source = await client.get(capture["audio_path"])
        assert missing_source.json()["reason"] == "capture_busy"

        malformed = await client.post("/api/clip/commit", content=b"{", headers={"content-type": "application/json"})
        assert malformed.json() == {"ok": False, "reason": "invalid_request"}
        expired = await client.post(
            "/api/clip/commit",
            json={"capture_id": capture["capture_id"], "choice_id": "moment"},
        )
        assert expired.json() == {"ok": False, "reason": "capture_busy", "retry_after": 3}


@pytest.mark.asyncio
async def test_capture_commit_write_retry_and_preview_share_race(tmp_path: Path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _seed_timeline(app)
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://testserver") as client:
        captured = await client.post("/api/clip/capture")
        capture = captured.json()
        with patch("mammamiradio.web.streamer._finalize_capture_choice", side_effect=OSError("disk")):
            failed = await client.post(
                "/api/clip/commit",
                json={"capture_id": capture["capture_id"], "choice_id": "moment"},
            )
        assert failed.status_code == 503
        record = app.state.capture_records[capture["capture_id"]]
        assert record.state == _CaptureState.READY
        assert record.claimed_choice_id is None

        original_finalizer = _finalize_capture_choice
        started = Event()
        release = Event()

        def paused_finalizer(*args, **kwargs):
            started.set()
            assert release.wait(timeout=2)
            return original_finalizer(*args, **kwargs)

        with patch("mammamiradio.web.streamer._finalize_capture_choice", side_effect=paused_finalizer):
            committing = asyncio.create_task(
                client.post(
                    "/api/clip/commit",
                    json={"capture_id": capture["capture_id"], "choice_id": "moment"},
                )
            )
            await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=2)
            # A native preview that reaches the server after commit claim must
            # not read a source the finalizer may soon delete.
            preview = await client.get(capture["audio_path"])
            assert preview.status_code == 409
            second_commit = await client.post(
                "/api/clip/commit",
                json={"capture_id": capture["capture_id"], "choice_id": "with_leadin"},
            )
            assert second_commit.status_code == 409
            release.set()
            committed = await committing
        assert committed.status_code == 201

        consumed_preview = await client.get(capture["audio_path"])
        assert consumed_preview.status_code == 409
        retry = await client.post(
            "/api/clip/commit",
            json={"capture_id": capture["capture_id"], "choice_id": "moment"},
        )
        assert retry.status_code == 200
        assert retry.json()["idempotent"] is True
