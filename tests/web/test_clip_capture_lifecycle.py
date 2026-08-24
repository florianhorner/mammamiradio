"""Lifecycle and fail-closed coverage for the private Moment Picker protocol."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock
from unittest.mock import patch

import httpx
import pytest

from mammamiradio.core.models import Segment, SegmentType
from mammamiradio.web.mp3_frames import Mp3FrameIndexError, build_mpeg1_layer3_frame_index
from mammamiradio.web.streamer import (
    CAPTURE_COMMIT_GRACE_SECONDS,
    CAPTURE_MAX_RECORDS_PER_IP,
    CAPTURE_RATE_PRUNE_SECONDS,
    SegmentMark,
    _append_clip_chunk,
    _build_capture_source,
    _capture_choice_is_share_safe,
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
    _render_capture_choice,
    _reset_clip_timeline,
    _safe_voice_run,
    _settle_capture_claim,
    _snapshot_retained_audio,
    _source_choice,
    initialize_clip_capture_runtime,
    shutdown_clip_capture_runtime,
)
from tests.web.test_streamer_routes import _make_test_app


def _mpeg1_l3_frames(count: int = 1_500) -> bytes:
    """Build a parser-valid CBR frame run; FFmpeg decoding has its own suite."""

    # MPEG-1 Layer III, 128 kbps, 44.1 kHz, no padding: 417-byte frames.
    return (b"\xff\xfb\x90\x00" + (b"\0" * 413)) * count


def _transport(app) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))


@pytest.fixture(autouse=True)
def _stub_decoder_safe_renderer(monkeypatch):
    """Use frame copies here; real decoder/onset behavior is FFmpeg-marked."""

    def render(input_path, output_path, *, preroll_samples, sample_count, sample_rate):
        del sample_rate
        data = input_path.read_bytes()
        index = build_mpeg1_layer3_frame_index(data)
        first = preroll_samples // 1152
        end = first + (sample_count // 1152)
        byte_start, byte_end = index.byte_range(first, end)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data[byte_start:byte_end])
        return output_path

    monkeypatch.setattr("mammamiradio.web.streamer.render_decoder_safe_mp3_window", render)


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
    active_writers: int = 0,
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
        active_writers=active_writers,
        consumed_monotonic=consumed_at,
    )


def _snapshot_with_marks(data: bytes, marks: tuple[SegmentMark, ...]) -> _ClipSnapshot:
    return _ClipSnapshot((data,), marks, 0, len(data), 0)


@pytest.mark.asyncio
async def test_capture_runtime_cleans_orphans_and_owns_one_maintenance_task(tmp_path: Path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    captures = app.state.config.cache_dir / "captures"
    captures.mkdir(parents=True)
    orphan = captures / "old.mp3.part"
    orphan.write_bytes(b"left behind")
    (captures / "nested").mkdir()
    clips = app.state.config.cache_dir / "clips"
    clips.mkdir(parents=True)
    stale_publication_scratch = clips / ".old-deadbeef.mp3.part"
    fresh_publication_scratch = clips / ".fresh-deadbeef.json.part"
    stale_publication_scratch.write_bytes(b"old scratch")
    fresh_publication_scratch.write_bytes(b"active scratch")
    old_time = time.time() - 7 * 3600
    stale_publication_scratch.touch()
    os.utime(stale_publication_scratch, (old_time, old_time))

    await initialize_clip_capture_runtime(app)
    task = app.state.capture_maintenance_task
    assert not orphan.exists()
    assert not stale_publication_scratch.exists()
    assert fresh_publication_scratch.exists()
    assert task is not None
    # This loop is lifecycle-owned and intentionally infinite.  The generic
    # background-task set contains finite fire-and-forget jobs that callers may
    # await, so putting the maintenance loop there would make those joins hang.
    assert task not in getattr(app.state, "background_tasks", set())

    # Startup is idempotent: it must not create competing maintenance loops.
    await initialize_clip_capture_runtime(app)
    assert app.state.capture_maintenance_task is task

    await shutdown_clip_capture_runtime(app)
    assert task.done()


@pytest.mark.asyncio
async def test_capture_runtime_shutdown_resets_same_app_lifespan(tmp_path: Path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    await initialize_clip_capture_runtime(app)
    first_task = app.state.capture_maintenance_task
    assert first_task is not None
    source = app.state.config.cache_dir / "captures" / "live.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"private")
    app.state.capture_records = {"live": _record("live", _CaptureState.READY, source_path=source)}
    app.state.capture_rate = {"127.0.0.1": time.monotonic()}

    await shutdown_clip_capture_runtime(app)

    assert app.state.capture_maintenance_task is None
    assert first_task.done()
    assert app.state.capture_records == {}
    assert app.state.capture_rate == {}
    assert not source.exists()

    await initialize_clip_capture_runtime(app)
    second_task = app.state.capture_maintenance_task
    assert second_task is not None
    assert second_task is not first_task
    assert not second_task.done()
    await shutdown_clip_capture_runtime(app)


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

    with pytest.raises(_CaptureNoAudioError):
        _build_capture_source(
            _snapshot_with_marks(
                data,
                (SegmentMark(0, 0, len(data), "song", "music", "Song", "Band", "commercial_music"),),
            ),
            capture_id="b" * 43,
            captures_dir=tmp_path / "captures",
            station_name="Mamma Mi Radio",
        )

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
        if path.name == f".{'e' * 43}.decoder.mp3.part":
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

    invalid_choice = replace(build.choices["moment"], sample_count=build.choices["moment"].source_sample_count + 1)
    with pytest.raises(ValueError):
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


def test_frozen_choice_metadata_contains_only_intersecting_chapters(tmp_path: Path) -> None:
    data = _mpeg1_l3_frames(2_000)
    index = build_mpeg1_layer3_frame_index(data)
    boundaries = [0, *(index.frames[offset].byte_start for offset in (500, 1_000, 1_500)), len(data)]
    marks = tuple(
        SegmentMark(0, boundaries[i], boundaries[i + 1], f"chapter-{i}", "banter", label, "Studio", "speech")
        for i, label in enumerate(("Uno", "Due", "Tre", "Quattro"))
    )
    build = _build_capture_source(
        _snapshot_with_marks(data, marks),
        capture_id="m" * 43,
        captures_dir=tmp_path / "captures",
        station_name="Mamma Mi Radio",
    )

    moment = build.choices["moment"]
    assert _capture_choice_is_share_safe(moment)
    assert moment.chapter_summary == ("Tre", "Quattro")
    assert moment.track_title == "Quattro"
    assert build.chapters[-3:][0]["label"] == "Due"

    result = _finalize_capture_choice(build.source_path, moment, build.frozen_metadata, tmp_path / "clips")
    sidecar = json.loads((tmp_path / "clips" / f"{result['clip_id']}.json").read_text())
    assert sidecar["chapter_summary"] == ["Tre", "Quattro"]
    assert sidecar["track_title"] == "Quattro"
    assert result["track_title"] == "Quattro"


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
        "claimed": _record("claimed", _CaptureState.CLAIMED, expires_in=-1, active_writers=1),
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

    # A station stop revokes every unfinished capability. A writer lease keeps
    # a claimed source alive only until its in-flight renderer settles.
    ready = _record("ready", _CaptureState.READY, active_readers=1)
    creating = _record("new", _CaptureState.CREATING, active_readers=1)
    app.state.capture_records = {"ready": ready, "new": creating, "claimed": app.state.capture_records["claimed"]}
    await _invalidate_pending_captures(app)
    assert ready.state == _CaptureState.EXPIRED
    assert creating.state == _CaptureState.EXPIRED
    assert app.state.capture_records["claimed"].state == _CaptureState.EXPIRED

    await _settle_capture_claim(app, "claimed", "nonce-claimed", restore_ready=True)
    assert "claimed" not in app.state.capture_records


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
async def test_capture_range_errors_and_successes_always_release_reader_lease(tmp_path: Path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _seed_timeline(app)
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://testserver") as client:
        capture = (await client.post("/api/clip/capture")).json()
        record = app.state.capture_records[capture["capture_id"]]

        malformed = await client.get(capture["audio_path"], headers={"Range": "bytes=not-a-range"})
        assert malformed.status_code == 400
        assert record.active_readers == 0

        unsatisfiable = await client.get(capture["audio_path"], headers={"Range": "bytes=999999999-"})
        assert unsatisfiable.status_code == 416
        assert record.active_readers == 0

        partial = await client.get(capture["audio_path"], headers={"Range": "bytes=0-9"})
        assert partial.status_code == 206
        assert len(partial.content) == 10
        assert record.active_readers == 0

        complete = await client.get(capture["audio_path"])
        assert complete.status_code == 200
        assert record.active_readers == 0


@pytest.mark.asyncio
async def test_capture_owner_quota_and_idempotent_release_preserve_global_capacity(tmp_path: Path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _seed_timeline(app)
    _ensure_clip_capture_state(app)
    owned_ids = [f"{index:043d}" for index in range(CAPTURE_MAX_RECORDS_PER_IP)]
    for capture_id in owned_ids:
        record = _record(capture_id, _CaptureState.READY)
        record.owner_key = "127.0.0.1"
        app.state.capture_records[capture_id] = record

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://testserver") as client:
        limited = await client.post("/api/clip/capture")
        assert limited.status_code == 429
        assert limited.json()["reason"] == "owner_capacity"

        released = await client.delete(f"/api/clip/capture/{owned_ids[0]}")
        assert released.json() == {"ok": True, "released": True}
        repeated = await client.delete(f"/api/clip/capture/{owned_ids[0]}")
        assert repeated.json() == {"ok": True, "released": False}

        admitted = await client.post("/api/clip/capture")
        assert admitted.status_code == 201

    other_transport = httpx.ASGITransport(app=app, client=("203.0.113.8", 12345))
    async with httpx.AsyncClient(transport=other_transport, base_url="http://testserver") as other:
        other_ip = await other.post("/api/clip/capture")
    assert other_ip.status_code == 201


@pytest.mark.asyncio
async def test_capture_identity_ignores_forwarded_spoof_from_untrusted_peer(tmp_path: Path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _seed_timeline(app)
    direct_ip = "203.0.113.50"
    first_spoof = "198.51.100.10"
    second_spoof = "198.51.100.11"
    transport = httpx.ASGITransport(app=app, client=(direct_ip, 12345))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post("/api/clip/capture", headers={"X-Forwarded-For": first_spoof})
        second = await client.post("/api/clip/capture", headers={"X-Forwarded-For": second_spoof})

    assert first.status_code == 201
    assert second.status_code == 429
    assert second.json()["reason"] == "rate_limited"
    record = app.state.capture_records[first.json()["capture_id"]]
    assert record.owner_key == direct_ip
    public_text = first.text + second.text
    assert all(identity not in public_text for identity in (direct_ip, first_spoof, second_spoof))


@pytest.mark.asyncio
async def test_capture_owner_quota_separates_forwarded_listeners_from_trusted_ha_proxy(tmp_path: Path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _seed_timeline(app)
    _ensure_clip_capture_state(app)
    owner_at_capacity = "198.51.100.21"
    other_owner = "198.51.100.22"
    for index in range(CAPTURE_MAX_RECORDS_PER_IP):
        capture_id = f"{index:043d}"
        record = _record(capture_id, _CaptureState.READY)
        record.owner_key = owner_at_capacity
        app.state.capture_records[capture_id] = record

    transport = httpx.ASGITransport(app=app, client=("172.30.32.5", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        limited = await client.post("/api/clip/capture", headers={"X-Forwarded-For": owner_at_capacity})
        admitted = await client.post("/api/clip/capture", headers={"X-Forwarded-For": other_owner})

    assert limited.status_code == 429
    assert limited.json()["reason"] == "owner_capacity"
    assert admitted.status_code == 201
    admitted_record = app.state.capture_records[admitted.json()["capture_id"]]
    assert admitted_record.owner_key == other_owner
    public_text = limited.text + admitted.text
    assert all(identity not in public_text for identity in (owner_at_capacity, other_owner))


@pytest.mark.asyncio
async def test_capture_identity_malformed_forwarded_header_falls_back_to_real_ip(tmp_path: Path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _seed_timeline(app)
    real_ip = "198.51.100.77"
    transport = httpx.ASGITransport(app=app, client=("172.30.32.5", 12345))
    headers = {
        "X-Forwarded-For": " , not-an-ip, 999.999.999.999",
        "X-Real-IP": real_ip,
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post("/api/clip/capture", headers=headers)
        second = await client.post("/api/clip/capture", headers=headers)

    assert first.status_code == 201
    assert second.status_code == 429
    assert second.json()["reason"] == "rate_limited"
    record = app.state.capture_records[first.json()["capture_id"]]
    assert record.owner_key == real_ip
    assert app.state.capture_rate.keys() == {real_ip}
    assert real_ip not in first.text + second.text


@pytest.mark.asyncio
async def test_capture_release_defers_unlink_for_reader_and_never_deletes_final_clip(tmp_path: Path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _ensure_clip_capture_state(app)
    capture_id = "r" * 43
    source = tmp_path / "capture.mp3"
    source.write_bytes(b"private")
    record = _record(capture_id, _CaptureState.READY, source_path=source, active_readers=1)
    app.state.capture_records[capture_id] = record
    final_clip = app.state.config.cache_dir / "clips" / "public.mp3"
    final_clip.parent.mkdir(parents=True)
    final_clip.write_bytes(b"public")

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://testserver") as client:
        released = await client.delete(f"/api/clip/capture/{capture_id}")
    assert released.json() == {"ok": True, "released": True}
    assert record.state == _CaptureState.EXPIRED
    assert source.exists()
    assert final_clip.exists()

    await _release_capture_reader(app, capture_id, record.nonce)
    assert capture_id not in app.state.capture_records
    assert not source.exists()
    assert final_clip.exists()


@pytest.mark.asyncio
async def test_capture_commit_rechecks_choice_local_rights_before_claiming(tmp_path: Path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _seed_timeline(app)
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://testserver") as client:
        capture = (await client.post("/api/clip/capture")).json()
        record = app.state.capture_records[capture["capture_id"]]
        record.choices["moment"] = replace(record.choices["moment"], audio_classes=("unknown",))
        with patch("mammamiradio.web.streamer._finalize_capture_choice") as finalizer:
            refused = await client.post(
                "/api/clip/commit",
                json={"capture_id": capture["capture_id"], "choice_id": "moment"},
            )

    assert refused.status_code == 403
    assert refused.json() == {"ok": False, "reason": "share_not_allowed"}
    assert record.state == _CaptureState.READY
    finalizer.assert_not_called()


@pytest.mark.asyncio
async def test_release_during_capture_build_discards_late_source_without_refunding_rate_limit(tmp_path: Path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _seed_timeline(app)
    original_builder = _build_capture_source
    started = Event()
    release_worker = Event()

    def paused_builder(*args, **kwargs):
        started.set()
        assert release_worker.wait(timeout=2)
        return original_builder(*args, **kwargs)

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://testserver") as client:
        with patch("mammamiradio.web.streamer._build_capture_source", side_effect=paused_builder):
            creating = asyncio.create_task(client.post("/api/clip/capture"))
            await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=2)
            capture_id = next(iter(app.state.capture_records))
            released = await client.delete(f"/api/clip/capture/{capture_id}")
            assert released.json() == {"ok": True, "released": True}
            release_worker.set()
            response = await creating

    assert response.status_code == 503
    assert response.json()["reason"] == "capture_busy"
    assert app.state.capture_records == {}
    assert "127.0.0.1" in app.state.capture_rate
    assert not list((app.state.config.cache_dir / "captures").glob("*"))


@pytest.mark.asyncio
async def test_capture_timeout_returns_before_worker_and_deletes_late_source(tmp_path: Path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _seed_timeline(app)
    original_builder = _build_capture_source
    started = Event()
    release_worker = Event()
    finished = Event()

    def paused_builder(*args, **kwargs):
        started.set()
        assert release_worker.wait(timeout=2)
        build = original_builder(*args, **kwargs)
        finished.set()
        return build

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://testserver") as client:
        with (
            patch("mammamiradio.web.streamer.CAPTURE_CREATE_TIMEOUT_SECONDS", 0.05),
            patch("mammamiradio.web.streamer._build_capture_source", side_effect=paused_builder),
        ):
            response = await asyncio.wait_for(client.post("/api/clip/capture"), timeout=1)
            assert started.is_set()
            assert response.status_code == 503
            assert response.json() == {"ok": False, "reason": "capture_busy", "retry_after": 3}
            record = next(iter(app.state.capture_records.values()))
            assert record.state == _CaptureState.DRAINING
            assert app.state.capture_rate == {"127.0.0.1": record.created_monotonic}

            release_worker.set()
            assert await asyncio.wait_for(asyncio.to_thread(finished.wait, 1), timeout=2)
            for _ in range(100):
                if not app.state.capture_records:
                    break
                await asyncio.sleep(0.01)

    assert app.state.capture_records == {}
    assert app.state.capture_rate == {}
    assert not list((app.state.config.cache_dir / "captures").glob("*"))


@pytest.mark.asyncio
async def test_cancelled_commit_drains_renderer_and_restores_ready(tmp_path: Path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _seed_timeline(app)
    started = Event()
    release_renderer = Event()

    def paused_renderer(*args, **kwargs):
        started.set()
        assert release_renderer.wait(timeout=2)
        return _render_capture_choice(*args, **kwargs)

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://testserver") as client:
        capture = (await client.post("/api/clip/capture")).json()
        with patch("mammamiradio.web.streamer._render_capture_choice", side_effect=paused_renderer):
            committing = asyncio.create_task(
                client.post(
                    "/api/clip/commit",
                    json={"capture_id": capture["capture_id"], "choice_id": "moment"},
                )
            )
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=2)
            committing.cancel()
            await asyncio.sleep(0)
            record = app.state.capture_records[capture["capture_id"]]
            assert record.state == _CaptureState.CLAIMED
            assert record.active_writers == 1
            release_renderer.set()
            with pytest.raises(asyncio.CancelledError):
                await committing

        record = app.state.capture_records[capture["capture_id"]]
        assert record.state == _CaptureState.READY
        assert record.active_writers == 0
        assert record.claimed_choice_id is None
        assert not list((app.state.config.cache_dir / "clips").glob("*.mp3"))

        retried = await client.post(
            "/api/clip/commit",
            json={"capture_id": capture["capture_id"], "choice_id": "moment"},
        )
    assert retried.status_code == 201


@pytest.mark.asyncio
async def test_station_stop_revokes_claimed_capture_before_publication(tmp_path: Path) -> None:
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _seed_timeline(app)
    started = Event()
    release_renderer = Event()

    def paused_renderer(*args, **kwargs):
        started.set()
        assert release_renderer.wait(timeout=2)
        return _render_capture_choice(*args, **kwargs)

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://testserver") as client:
        capture = (await client.post("/api/clip/capture")).json()
        source_path = app.state.capture_records[capture["capture_id"]].source_path
        with patch("mammamiradio.web.streamer._render_capture_choice", side_effect=paused_renderer):
            committing = asyncio.create_task(
                client.post(
                    "/api/clip/commit",
                    json={"capture_id": capture["capture_id"], "choice_id": "moment"},
                )
            )
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=2)
            stopped = await client.post("/api/stop")
            assert stopped.status_code == 200
            record = app.state.capture_records[capture["capture_id"]]
            assert record.state == _CaptureState.EXPIRED
            assert record.active_writers == 1
            assert source_path is not None and source_path.exists()
            release_renderer.set()
            committed = await committing

    assert committed.status_code == 404
    assert committed.json() == {"ok": False, "reason": "capture_expired"}
    assert capture["capture_id"] not in app.state.capture_records
    assert source_path is not None and not source_path.exists()
    assert not list((app.state.config.cache_dir / "clips").glob("*.mp3"))


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


@pytest.mark.asyncio
async def test_legacy_and_moment_publications_share_one_serialized_fifty_file_cap(tmp_path: Path) -> None:
    from mammamiradio.web import streamer as streamer_mod

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _seed_timeline(app)
    clips_dir = app.state.config.cache_dir / "clips"
    clips_dir.mkdir(parents=True)
    for index in range(49):
        (clips_dir / f"old-{index:02d}.mp3").write_bytes(b"old")
        (clips_dir / f"old-{index:02d}.json").write_text("{}")
    app.state.last_shareworthy_starter = {
        "type": "starter",
        "ended_monotonic": time.monotonic(),
        "title": "Carefree",
        "artist": "Kevin MacLeod",
    }
    streamer_mod._clip_rate.clear()
    publication_guard = Lock()
    active_publishers = 0
    max_active_publishers = 0
    real_publish = streamer_mod.publish_clip

    def tracked_publish(*args, **kwargs):
        nonlocal active_publishers, max_active_publishers
        with publication_guard:
            active_publishers += 1
            max_active_publishers = max(max_active_publishers, active_publishers)
        try:
            time.sleep(0.05)
            return real_publish(*args, **kwargs)
        finally:
            with publication_guard:
                active_publishers -= 1

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://testserver") as client:
        capture = (await client.post("/api/clip/capture")).json()
        with (
            patch("mammamiradio.web.streamer._read_validated_starter_share", return_value=b"starter-audio"),
            patch("mammamiradio.web.streamer.publish_clip", side_effect=tracked_publish),
        ):
            moment_response, legacy_response = await asyncio.gather(
                client.post(
                    "/api/clip/commit",
                    json={"capture_id": capture["capture_id"], "choice_id": "moment"},
                ),
                client.post("/api/clip"),
            )

    assert moment_response.status_code == 201
    assert legacy_response.status_code == 200
    assert max_active_publishers == 1
    assert len(list(clips_dir.glob("*.mp3"))) == 50
    assert len(list(clips_dir.glob("*.json"))) == 50
    streamer_mod._clip_rate.clear()


@pytest.mark.asyncio
async def test_legacy_clip_read_finishing_after_stop_cannot_publish(tmp_path: Path) -> None:
    from mammamiradio.web import streamer as streamer_mod

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    _seed_timeline(app)
    app.state.last_shareworthy_starter = {
        "type": "starter",
        "ended_monotonic": time.monotonic(),
        "title": "Carefree",
        "artist": "Kevin MacLeod",
    }
    streamer_mod._clip_rate.clear()
    started = Event()
    release_reader = Event()

    def paused_reader(_snapshot):
        started.set()
        assert release_reader.wait(timeout=2)
        return b"complete-starter-audio"

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://testserver") as client:
        with patch("mammamiradio.web.streamer._read_validated_starter_share", side_effect=paused_reader):
            sharing = asyncio.create_task(client.post("/api/clip"))
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=2)
            stopped = await client.post("/api/stop")
            assert stopped.status_code == 200
            release_reader.set()
            response = await sharing

    assert response.status_code == 403
    assert response.json()["error_code"] == "music_share_unavailable"
    assert not list((app.state.config.cache_dir / "clips").glob("*.mp3"))
    streamer_mod._clip_rate.clear()
