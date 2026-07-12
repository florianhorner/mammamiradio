"""Characterization tests for the producer queue-commit contract.

These pin the CURRENT per-path commit behavior of ``run_producer`` /
``prewarm_first_segment`` so the deferred ``production/commit.py`` extraction (or
any other relocation) cannot silently change it. The matching prose lives in the
queue-policy table in ``docs/architecture.md``.

Scope is deliberately the GAPS not already covered elsewhere — we do not re-test
what these own:

* egress skip/colour + tail-adjacency  -> ``tests/scheduling/test_egress_pipeline.py``
* front-insert / air-next mechanics     -> ``tests/scheduling/test_air_next.py``
* stopped-discard + main-path stale     -> ``tests/scheduling/test_producer_unit.py``
* blocklist enqueue gate                -> ``test_producer_unit.py::test_enqueue_funnel_drops_a_banned_music_segment``

What is pinned here:

1. the stale playlist/chaos gate is a SHARED ``run_producer`` epilogue — it
   discards generated SPEECH, not just music (``producer.py:3019``);
2. an operator AIR-NEXT discarded by that gate releases ``operator_force_pending``
   so the operator is not locked out (``producer.py:3022-3023``);
3. direct-enqueue paths (prewarm + bridges, via ``_enqueue_with_egress`` with no
   front-insert) publish the same identity-backed up-next row as the main-loop
   epilogue, while outer error-recovery rescue follows that same contract;
4. prewarm discards a stale segment when the SOURCE switches mid-render — it keys on
   ``source_revision`` (true switches only), not the broad ``playlist_revision``, so a
   benign in-place edit keeps the pre-roll; and a switch landing during the egress encode
   is caught by an opt-in post-egress ``stale_check`` on the funnel (#659/#665);
5. a mid-loop blocklist drop (music only) must NOT overwrite the prior speech-bed
   source — ``state.last_music_file``, ``producer._last_music_file``, and
   ``_adjacent_music_source()`` must all still reference the last successfully committed
   music track, not the banned render (#660/#664).

Mechanism note: the gates key on closure-local generation values a unit test cannot poke,
so these drive ``run_producer`` / ``prewarm_first_segment`` and bump
``state.playlist_revision`` / ``source_revision`` / ``chaos_cutover_epoch`` from inside a
patched render step (the duration probe for the epilogue paths, the mocked download or
``_apply_egress`` for prewarm) to model "a switch landed during this segment's build."
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mammamiradio.audio.audio_quality import AudioQualityError
from mammamiradio.core.config import load_config
from mammamiradio.core.models import GenerationWasteReason, Segment, SegmentType, StationState, Track
from mammamiradio.scheduling import producer
from mammamiradio.scheduling.producer import (
    _adjacent_music_source,
    _enqueue_with_egress,
    _normalized_cache_path,
    prewarm_first_segment,
    run_producer,
)

PRODUCER_MODULE = "mammamiradio.scheduling.producer"
TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")
EXPECTED_CONSECUTIVE_FAILURE_BACKOFF = 4.0
_REAL_ASYNCIO_SLEEP = asyncio.sleep


@pytest.fixture(autouse=True)
def _mock_quality_gate():
    with patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _mock_download_validation():
    with patch(f"{PRODUCER_MODULE}.validate_download", return_value=(True, "ok")):
        yield


@pytest.fixture(autouse=True)
def _clean_producer_globals():
    """Reset module globals that leak between tests, on setup AND teardown.

    Resetting on setup too matters under ``pytest-randomly``: a prior module may
    leave these dirty, which would make the first test here order-dependent.
    """
    from mammamiradio.scheduling import producer

    producer._last_music_file = None
    producer._canned_clip_cache.clear()
    producer._recently_played_clips.clear()
    yield
    producer._last_music_file = None
    producer._canned_clip_cache.clear()
    producer._recently_played_clips.clear()


def _make_state() -> StationState:
    """A station with a 2-track pool and one live listener (passes the production gate)."""
    return StationState(
        playlist=[
            Track(title="Canzone Uno", artist="Artista", duration_ms=200_000, spotify_id="demo1"),
            Track(title="Canzone Due", artist="Artista", duration_ms=180_000, spotify_id="demo2"),
        ],
        listeners_active=1,  # a live listener so the producer's production gate passes
    )


def _make_config(tmp_path: Path):
    """Load the real radio.toml, then scope tmp/cache to ``tmp_path`` and lookahead to 1."""
    config = load_config(TOML_PATH)
    config.pacing.lookahead_segments = 1
    config.homeassistant.enabled = False
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    return config


def _write_concat(paths, out, *_args, **_kwargs):
    """``concat_files`` stand-in: materialize the output so the probe + enqueue see a file."""
    Path(out).write_bytes(b"audio")


async def _cancel(task: asyncio.Task) -> None:
    """Cancel a producer task and swallow the expected CancelledError."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    """Poll ``predicate`` until it is true, or raise TimeoutError after ``timeout`` s."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError("condition not met before timeout")
        await _REAL_ASYNCIO_SLEEP(0.02)


# ---------------------------------------------------------------------------
# 1. The stale gate is a shared epilogue — it covers generated SPEECH too.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stale_fields", "expected_reason"),
    [
        # A true source switch (switch_playlist) bumps BOTH source_revision and
        # playlist_revision → classified stale_source.
        (["source_revision", "playlist_revision"], GenerationWasteReason.STALE_SOURCE),
        # A same-source playlist edit (shuffle/add/move/enrich) bumps only
        # playlist_revision → classified stale_playlist (#397 split).
        (["playlist_revision"], GenerationWasteReason.STALE_PLAYLIST),
        (["chaos_cutover_epoch"], GenerationWasteReason.STALE_CHAOS),
    ],
)
@pytest.mark.asyncio
async def test_stale_gate_discards_generated_speech(tmp_path, stale_fields, expected_reason):
    """A TIME_CHECK built before a source switch, a same-source playlist edit, or a
    chaos cutover is discarded by the same shared epilogue gate that guards music
    (``producer.py``). Pins that the gate is NOT music-specific, covers all stale
    axes, and classifies a true source switch (``stale_source``) apart from a
    same-source edit (``stale_playlist``); a discard queues nothing and runs no
    success callback (``state.segments_produced`` stays 0)."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    probe_calls = 0

    def _staling_probe(_path):
        nonlocal probe_calls
        probe_calls += 1
        # a source switch / same-source playlist edit / chaos cutover landed during this build
        for field in stale_fields:
            setattr(state, field, getattr(state, field) + 1)
        return 1.0

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.TIME_CHECK),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.generate_tone", MagicMock()),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_write_concat),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", side_effect=_staling_probe),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            # lookahead is 1, so a segment that actually QUEUED would halt production at
            # qsize 1. Reaching a 2nd probe proves the 1st build ran the gate and was
            # discarded (the queue stayed empty) — deterministic, no fixed-sleep race.
            await _wait_for(lambda: probe_calls >= 2)
            assert queue.empty()  # discarded, never queued
            assert state.queued_segments == []  # no up-next row
            assert state.segments_produced == 0  # success callback (after_time_check) not run
            assert state.discarded_segments_total >= 1
            assert state.discard_by_reason.get(expected_reason, 0) >= 1
        finally:
            await _cancel(task)


@pytest.mark.asyncio
async def test_unavailable_music_render_closes_its_timing(tmp_path):
    """An unavailable music render is a failed attempt, not an abandoned next-cycle artifact."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    async def _unavailable_render(*_args, **_kwargs):
        await _REAL_ASYNCIO_SLEEP(0)
        return None

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}._render_music_track", side_effect=_unavailable_render),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await _wait_for(
                lambda: any(
                    timing.get("kind") == SegmentType.MUSIC.value and timing.get("reason") == "render_unavailable"
                    for timing in state.render_timings
                )
            )
        finally:
            await _cancel(task)

    timing = next(timing for timing in state.render_timings if timing.get("reason") == "render_unavailable")
    assert timing["outcome"] == "failed"
    assert not any(timing.get("reason") == "abandoned" for timing in state.render_timings)


@pytest.mark.asyncio
async def test_cancelled_producer_closes_in_flight_render_timing(tmp_path):
    """Shutdown during an awaited render cannot leave timing open into a later task."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    render_started = asyncio.Event()
    hold_render = asyncio.Event()

    async def _blocked_render(*_args, **_kwargs):
        render_started.set()
        await hold_render.wait()

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}._render_music_track", side_effect=_blocked_render),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        await asyncio.wait_for(render_started.wait(), timeout=1.0)
        assert state._render_timing_started > 0
        await _cancel(task)
        await _REAL_ASYNCIO_SLEEP(0)

    assert state._render_timing_started == 0
    assert state.render_timings[0]["outcome"] == "failed"
    assert state.render_timings[0]["reason"] == "cancelled"


@pytest.mark.asyncio
async def test_music_quality_rejection_closes_its_timing(tmp_path):
    """A quality rejection is recorded at rejection time, not as an abandoned attempt later."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    rendered_path = tmp_path / "rejected.mp3"
    rendered_path.write_bytes(b"audio")

    async def _render_rejected_track(track, *_args, **_kwargs):
        await _REAL_ASYNCIO_SLEEP(0)
        return producer.RenderedMusicTrack(
            track=track,
            path=rendered_path,
            cache_path=rendered_path,
            cache_hit=False,
        )

    def _reject_quality(*_args, **_kwargs):
        raise AudioQualityError("too quiet")

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}._render_music_track", side_effect=_render_rejected_track),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", side_effect=_reject_quality),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await _wait_for(
                lambda: any(
                    timing.get("kind") == SegmentType.MUSIC.value
                    and timing.get("reason") == GenerationWasteReason.QUALITY_GATE_REJECT
                    for timing in state.render_timings
                )
            )
        finally:
            await _cancel(task)

    timing = next(
        timing
        for timing in state.render_timings
        if timing.get("kind") == SegmentType.MUSIC.value
        and timing.get("reason") == GenerationWasteReason.QUALITY_GATE_REJECT
    )
    assert timing["outcome"] == "discarded"
    assert not any(timing.get("reason") == "abandoned" for timing in state.render_timings)


# ---------------------------------------------------------------------------
# 2. Air-next discarded by the stale gate releases the operator one-at-a-time guard.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_air_next_stale_discard_releases_operator_guard(tmp_path):
    """An operator air-next (``operator_force_pending`` set) built against a
    now-stale revision flows through the SAME epilogue gate and, on discard,
    clears ``operator_force_pending`` (``producer.py:3022-3023``) so a retry isn't
    locked out until restart."""
    state = _make_state()
    state.force_next = SegmentType.TIME_CHECK
    state.operator_force_pending = SegmentType.TIME_CHECK
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    def _staling_probe(_path):
        state.playlist_revision += 1
        return 1.0

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.TIME_CHECK),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.generate_tone", MagicMock()),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_write_concat),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", side_effect=_staling_probe),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await _wait_for(lambda: state.operator_force_pending is None)
            assert state.operator_force_pending is None  # guard released on stale discard
            assert queue.empty()  # the forced pick was discarded, not aired
            assert state.queued_segments == []
        finally:
            await _cancel(task)


@pytest.mark.asyncio
async def test_air_next_rejected_during_egress_releases_operator_guard(tmp_path):
    """A cutover during final egress cannot leave every later trigger locked out."""
    state = _make_state()
    state.force_next = SegmentType.TIME_CHECK
    state.operator_force_pending = SegmentType.TIME_CHECK
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    async def _invalidate_during_egress(segment, _config):
        state.continuity_epoch += 1
        return segment

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.TIME_CHECK),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.generate_tone", MagicMock()),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_write_concat),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}._apply_egress", side_effect=_invalidate_during_egress),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await _wait_for(
                lambda: any(
                    item.get("reason") == GenerationWasteReason.STALE_CONTINUITY for item in state.render_timings
                )
            )
        finally:
            await _cancel(task)

    assert state.operator_force_pending is None
    assert queue.empty()
    timing = next(item for item in state.render_timings if item.get("reason") == GenerationWasteReason.STALE_CONTINUITY)
    assert timing["outcome"] == "discarded"


@pytest.mark.asyncio
async def test_air_next_capacity_rejection_uses_one_consistent_reason(tmp_path):
    """Preserving an earlier air-next reports overflow in both waste and timing."""
    state = _make_state()
    state.force_next = SegmentType.TIME_CHECK
    state.operator_force_pending = SegmentType.TIME_CHECK
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=1)
    existing = Segment(
        type=SegmentType.BANTER,
        path=tmp_path / "existing-air-next.mp3",
        duration_sec=30.0,
        metadata={"title": "Existing air-next", "air_next": True, "queue_id": "existing"},
        ephemeral=False,
    )
    existing.path.write_bytes(b"existing")
    queue.put_nowait(existing)
    state.queued_segments = [{"id": "existing", "type": "banter", "label": "Existing air-next"}]

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.TIME_CHECK),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.generate_tone", MagicMock()),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_write_concat),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}._apply_egress", new_callable=AsyncMock, side_effect=lambda segment, _: segment),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await _wait_for(
                lambda: any(
                    item.get("reason") == GenerationWasteReason.AIR_NEXT_OVERFLOW for item in state.render_timings
                )
            )
        finally:
            await _cancel(task)

    assert state.operator_force_pending is None
    assert list(queue._queue) == [existing]
    assert state.discard_by_reason.get(GenerationWasteReason.AIR_NEXT_OVERFLOW) == 1
    assert state.render_timings[0]["reason"] == GenerationWasteReason.AIR_NEXT_OVERFLOW


# ---------------------------------------------------------------------------
# 3. Shadow-row visibility: every successful admission gets an identity-backed row.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_enqueue_publishes_matching_shadow_row(tmp_path):
    """A direct bridge is real queued audio, so Scaletta receives the same stable
    identity-backed row as a normal producer commit. The rescue still skips the
    egress colour pass (patch ``apply_broadcast_chain``, never ``_apply_egress`` —
    mocking the latter would hide the rescue-skip branch)."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    bridge = tmp_path / "bridge.mp3"
    bridge.write_bytes(b"audio")
    seg = Segment(
        type=SegmentType.MUSIC,
        path=bridge,
        ephemeral=False,
        metadata={"rescue": True, "queue_drain_recovery": True, "title": "Resume bridge"},
    )

    with patch(f"{PRODUCER_MODULE}.apply_broadcast_chain") as m_chain:
        ok = await _enqueue_with_egress(queue, state, config, seg)

    assert ok is True
    assert queue.qsize() == 1  # the bridge audio aired
    assert len(state.queued_segments) == 1
    assert state.queued_segments[0]["id"] == seg.metadata["queue_id"]
    assert state.queued_segments[0]["label"] == "Resume bridge"
    m_chain.assert_not_called()  # rescue skipped the egress colour pass


@pytest.mark.asyncio
async def test_prewarm_publishes_matching_shadow_row(tmp_path):
    """A pre-warmed first segment is real queued audio, so it must be visible in
    Scaletta with the stable id carried by the matching Segment."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue()

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=tmp_path / "fake.mp3"),
        patch(f"{PRODUCER_MODULE}.normalize"),
        patch(f"{PRODUCER_MODULE}.shutil.copy2"),
        patch(f"{PRODUCER_MODULE}._set_last_music_file"),
    ):
        result = await prewarm_first_segment(queue, state, config)

    assert result is True
    assert queue.qsize() == 1
    queued = queue.get_nowait()
    assert len(state.queued_segments) == 1
    assert state.queued_segments[0]["id"] == queued.metadata["queue_id"]
    assert state.queued_segments[0]["label"] == queued.metadata["title"]


@pytest.mark.asyncio
async def test_error_recovery_uses_norm_cache_rescue_and_appends_shadow_row(tmp_path):
    """Outer error-recovery rescue (``rescue=True``) is built inside the main loop
    body and so flows through the epilogue — unlike a bridge it DOES append an
    up-next shadow row (``producer.py:3060``). Also exercises the empty-container
    fallback (Scenario 2): no canned clip available, so recovery must still use
    real audio instead of generated silence."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    norm_file = tmp_path / "norm_cached_192k.mp3"
    norm_file.write_bytes(b"fake norm audio" * 100)
    producer.save_track_metadata(norm_file, title="Cached", artist="Cache Artist")

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=RuntimeError("network down")),
        patch(
            f"{PRODUCER_MODULE}.generate_silence",
            side_effect=AssertionError("silence should never be a producer recovery fallback"),
            create=True,
        ) as mock_silence,
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),  # empty container -> norm-cache rescue
        patch(f"{PRODUCER_MODULE}.select_norm_cache_rescue", return_value=norm_file),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=5.0),
        patch(f"{PRODUCER_MODULE}._build_recovery_sweeper_segment", new_callable=AsyncMock) as mock_sweeper,
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await _wait_for(lambda: queue.qsize() > 0)
            seg = queue.get_nowait()
            mock_silence.assert_not_called()
            mock_sweeper.assert_not_called()
            assert seg.type == SegmentType.MUSIC
            assert seg.path == norm_file
            assert seg.metadata.get("rescue") is True
            assert seg.metadata.get("error_recovery") is True
            assert seg.metadata.get("audio_source") == "norm_cache"
            assert len(state.queued_segments) == 1  # rescue-via-epilogue DID add an up-next row
        finally:
            await _cancel(task)


@pytest.mark.asyncio
async def test_error_recovery_queues_rescue_before_consecutive_failure_backoff(tmp_path):
    """A second consecutive producer failure queues rescue audio before awaiting
    the CPU-throttle backoff, preserving continuity while still slowing retries."""
    state = _make_state()
    state.failed_segments = 1
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    original_sleep = asyncio.sleep
    backoff_queue_sizes: list[int] = []

    recovery = Segment(
        type=SegmentType.SWEEPER,
        path=tmp_path / "recovery.mp3",
        metadata={"type": "sweeper", "rescue": True, "error_recovery": True, "title": "Recovery sweeper"},
    )
    recovery.path.write_bytes(b"recovery")

    async def _record_sleep(delay: float, *_args, **_kwargs):
        if delay == EXPECTED_CONSECUTIVE_FAILURE_BACKOFF:
            backoff_queue_sizes.append(queue.qsize())
        await original_sleep(0)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=RuntimeError("network down")),
        patch(
            f"{PRODUCER_MODULE}.generate_silence",
            side_effect=AssertionError("silence should never be a producer recovery fallback"),
            create=True,
        ),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
        patch(f"{PRODUCER_MODULE}.select_norm_cache_rescue", return_value=None),
        patch(f"{PRODUCER_MODULE}._build_recovery_sweeper_segment", new_callable=AsyncMock, return_value=recovery),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=5.0),
        patch(f"{PRODUCER_MODULE}.asyncio.sleep", side_effect=_record_sleep),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await _wait_for(lambda: bool(backoff_queue_sizes))
            assert backoff_queue_sizes[0] == 1
            seg = queue.get_nowait()
            assert seg.metadata.get("rescue") is True
            assert seg.metadata.get("title") == "Recovery sweeper"
            assert len(state.queued_segments) == 1
            assert state.failed_segments == 2
        finally:
            await _cancel(task)


@pytest.mark.asyncio
async def test_error_recovery_queues_canned_rescue_before_consecutive_failure_backoff(tmp_path):
    """The canned recovery branch also queues cover audio before the failure backoff."""
    state = _make_state()
    state.failed_segments = 1
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    canned_clip = tmp_path / "canned_recovery.mp3"
    canned_clip.write_bytes(b"canned")
    original_sleep = asyncio.sleep
    backoff_queue_sizes: list[int] = []

    async def _record_sleep(delay: float, *_args, **_kwargs):
        if delay == EXPECTED_CONSECUTIVE_FAILURE_BACKOFF:
            backoff_queue_sizes.append(queue.qsize())
        await original_sleep(0)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=RuntimeError("network down")),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=canned_clip),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=5.0),
        patch(f"{PRODUCER_MODULE}.asyncio.sleep", side_effect=_record_sleep),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await _wait_for(lambda: bool(backoff_queue_sizes))
            assert backoff_queue_sizes[0] == 1
            seg = queue.get_nowait()
            assert seg.type == SegmentType.BANTER
            assert seg.path == canned_clip
            assert seg.metadata.get("error_recovery") is True
            assert seg.metadata.get("rescue") is True
            assert seg.metadata.get("title") == "Station continuity"
            assert len(state.queued_segments) == 1
            assert state.failed_segments == 2
        finally:
            await _cancel(task)


@pytest.mark.asyncio
async def test_operator_error_recovery_front_inserts_rescue_before_consecutive_failure_backoff(tmp_path):
    """Operator air-next recovery uses the same queue-before-backoff invariant."""
    state = _make_state()
    state.failed_segments = 1
    state.force_next = SegmentType.MUSIC
    state.operator_force_pending = SegmentType.MUSIC
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    canned_clip = tmp_path / "operator_recovery.mp3"
    canned_clip.write_bytes(b"canned")
    placeholder_clip = tmp_path / "buffered_placeholder.mp3"
    placeholder_clip.write_bytes(b"buffered")
    queue.put_nowait(
        Segment(
            type=SegmentType.MUSIC,
            path=placeholder_clip,
            metadata={"placeholder": True, "title": "Buffered placeholder"},
        )
    )
    state.queued_segments.append(
        {
            "id": "placeholder",
            "type": SegmentType.MUSIC.value,
            "label": "Buffered placeholder",
            "spotify_id": "",
            "reason": "Already queued.",
            "playlist_index": -1,
            "source_kind": "",
            "duration_sec": 5.0,
        }
    )
    original_sleep = asyncio.sleep
    backoff_snapshots: list[tuple[int, SegmentType | None, int]] = []
    backoff_seen = asyncio.Event()

    async def _record_sleep(delay: float, *_args, **_kwargs):
        if delay == EXPECTED_CONSECUTIVE_FAILURE_BACKOFF:
            backoff_snapshots.append((queue.qsize(), state.operator_force_pending, len(state.queued_segments)))
            backoff_seen.set()
        await original_sleep(0)

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=RuntimeError("network down")),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=canned_clip),
        # This test is about operator recovery queue ordering. Keep the unrelated
        # transition-stinger path from trying to inspect the fake MP3 fixture.
        patch(f"{PRODUCER_MODULE}._crosses_music_speech_boundary", return_value=False),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=5.0),
        patch(f"{PRODUCER_MODULE}.asyncio.sleep", side_effect=_record_sleep),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.wait_for(backoff_seen.wait(), timeout=5.0)
            assert backoff_snapshots[0] == (2, None, 2)
            rescue = queue.get_nowait()
            placeholder = queue.get_nowait()
            assert rescue.type == SegmentType.BANTER
            assert rescue.path == canned_clip
            assert rescue.metadata.get("error_recovery") is True
            assert rescue.metadata.get("rescue") is True
            assert placeholder.metadata.get("placeholder") is True
            assert state.queued_segments[0]["label"] == "Station continuity"
            assert state.queued_segments[1]["label"] == "Buffered placeholder"
            assert state.failed_segments == 2
        finally:
            await _cancel(task)


@pytest.mark.parametrize(
    ("stale_field", "expected_reason"),
    [
        ("playlist", GenerationWasteReason.STALE_PLAYLIST),
        ("source", GenerationWasteReason.STALE_SOURCE),
        ("chaos", GenerationWasteReason.STALE_CHAOS),
    ],
)
@pytest.mark.asyncio
async def test_error_recovery_stale_discard_awaits_consecutive_failure_backoff(tmp_path, stale_field, expected_reason):
    """Persistent recovery failures are throttled even when the rescue becomes stale."""
    state = _make_state()
    state.failed_segments = 1
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    original_sleep = asyncio.sleep
    backoff_sleeps: list[float] = []

    recovery = Segment(
        type=SegmentType.SWEEPER,
        path=tmp_path / "recovery.mp3",
        metadata={"type": "sweeper", "rescue": True, "error_recovery": True, "title": "Recovery sweeper"},
    )
    recovery.path.write_bytes(b"recovery")

    def _stale_probe(_path: Path) -> float:
        if stale_field == "source":
            state.source_revision += 1
            state.playlist_revision += 1
        elif stale_field == "playlist":
            state.playlist_revision += 1
        else:
            state.chaos_cutover_epoch += 1
        return 5.0

    async def _record_sleep(delay: float, *_args, **_kwargs):
        if delay == EXPECTED_CONSECUTIVE_FAILURE_BACKOFF:
            backoff_sleeps.append(delay)
        await original_sleep(0)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=RuntimeError("network down")),
        patch(
            f"{PRODUCER_MODULE}.generate_silence",
            side_effect=AssertionError("silence should never be a producer recovery fallback"),
            create=True,
        ),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
        patch(f"{PRODUCER_MODULE}.select_norm_cache_rescue", return_value=None),
        patch(f"{PRODUCER_MODULE}._build_recovery_sweeper_segment", new_callable=AsyncMock, return_value=recovery),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", side_effect=_stale_probe),
        patch(f"{PRODUCER_MODULE}.asyncio.sleep", side_effect=_record_sleep),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await _wait_for(lambda: bool(backoff_sleeps))
            assert queue.empty()
            assert state.discard_by_reason.get(expected_reason, 0) >= 1
            assert backoff_sleeps[0] == EXPECTED_CONSECUTIVE_FAILURE_BACKOFF
        finally:
            await _cancel(task)


@pytest.mark.asyncio
async def test_error_recovery_enqueue_failure_awaits_consecutive_failure_backoff(tmp_path):
    """Persistent recovery failures are throttled even when the enqueue funnel rejects the rescue."""
    state = _make_state()
    state.failed_segments = 1
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    original_sleep = asyncio.sleep
    backoff_sleeps: list[float] = []
    enqueue_attempts = 0

    recovery = Segment(
        type=SegmentType.SWEEPER,
        path=tmp_path / "recovery.mp3",
        metadata={"type": "sweeper", "rescue": True, "error_recovery": True, "title": "Recovery sweeper"},
    )
    recovery.path.write_bytes(b"recovery")

    async def _reject_enqueue(*_args, **_kwargs) -> bool:
        nonlocal enqueue_attempts
        enqueue_attempts += 1
        return False

    async def _record_sleep(delay: float, *_args, **_kwargs):
        if delay == EXPECTED_CONSECUTIVE_FAILURE_BACKOFF:
            backoff_sleeps.append(delay)
        await original_sleep(0)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=RuntimeError("network down")),
        patch(
            f"{PRODUCER_MODULE}.generate_silence",
            side_effect=AssertionError("silence should never be a producer recovery fallback"),
            create=True,
        ),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
        patch(f"{PRODUCER_MODULE}.select_norm_cache_rescue", return_value=None),
        patch(f"{PRODUCER_MODULE}._build_recovery_sweeper_segment", new_callable=AsyncMock, return_value=recovery),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=5.0),
        patch(f"{PRODUCER_MODULE}._enqueue_with_egress", side_effect=_reject_enqueue),
        patch(f"{PRODUCER_MODULE}.asyncio.sleep", side_effect=_record_sleep),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await _wait_for(lambda: bool(backoff_sleeps))
            assert queue.empty()
            assert enqueue_attempts >= 1
            assert backoff_sleeps[0] == EXPECTED_CONSECUTIVE_FAILURE_BACKOFF
        finally:
            await _cancel(task)


# ---------------------------------------------------------------------------
# 4. Prewarm stale gate (#659).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expected_reason",
    [
        GenerationWasteReason.STALE_CONTINUITY,
        GenerationWasteReason.STALE_SOURCE,
        GenerationWasteReason.STALE_CHAOS,
        GenerationWasteReason.SESSION_STOPPED,
        GenerationWasteReason.BLOCKLIST_GATE,
    ],
)
@pytest.mark.asyncio
async def test_blocked_queue_put_retracts_segment_if_it_becomes_stale_before_capacity(tmp_path, expected_reason):
    """A full queue can make admission await after the first stale check.

    The exact segment admitted when capacity opens must be removed synchronously
    if a live cutover landed during that wait, before its shadow row or commit
    side effects become visible.
    """
    state = _make_state()
    state.begin_render_timing(SegmentType.MUSIC.value)
    config = _make_config(tmp_path)
    blocker = Segment(type=SegmentType.MUSIC, path=tmp_path / "blocker.mp3", metadata={"title": "Blocker"})
    blocker.path.write_bytes(b"blocker")
    candidate = Segment(
        type=SegmentType.MUSIC,
        path=tmp_path / "candidate.mp3",
        metadata={"title": "Candidate", "title_only": "Candidate", "artist": "Artist"},
        ephemeral=True,
    )
    candidate.path.write_bytes(b"candidate")
    put_started = asyncio.Event()

    class ObservedQueue(asyncio.Queue[Segment]):
        async def put(self, item: Segment) -> None:
            if item is candidate:
                put_started.set()
            await super().put(item)

    queue = ObservedQueue(maxsize=1)
    queue.put_nowait(blocker)
    stale_reason: str | None = None

    def _stale_reason() -> str | None:
        return stale_reason

    with (
        patch(f"{PRODUCER_MODULE}._apply_egress", new_callable=AsyncMock, return_value=candidate),
        patch(f"{PRODUCER_MODULE}._schedule_restart_handoff_spool") as schedule_spool,
    ):
        enqueue_task = asyncio.create_task(
            _enqueue_with_egress(
                queue,
                state,
                config,
                candidate,
                shadow_entry={"id": "candidate", "type": "music", "label": "Candidate"},
                stale_check=_stale_reason,
            )
        )
        await asyncio.wait_for(put_started.wait(), timeout=1.0)
        await _REAL_ASYNCIO_SLEEP(0.01)
        if expected_reason == GenerationWasteReason.SESSION_STOPPED:
            state.session_stopped = True
        elif expected_reason == GenerationWasteReason.BLOCKLIST_GATE:
            state.blocklist[("artist", "candidate")] = {"display": "Artist - Candidate"}
        else:
            stale_reason = expected_reason
        assert queue.get_nowait() is blocker
        queue.task_done()
        admitted = await asyncio.wait_for(enqueue_task, timeout=1.0)

    state.finish_render_timing("discarded", reason=expected_reason)
    assert admitted is False
    assert queue.empty()
    await asyncio.wait_for(queue.join(), timeout=1.0)
    assert state.queued_segments == []
    assert state.discard_by_reason == {expected_reason: 1}
    assert not candidate.path.exists()
    schedule_spool.assert_not_called()
    assert state.render_timings[0]["stages_ms"]["admission"] >= 1


@pytest.mark.asyncio
async def test_prewarm_discards_stale_song_on_revision_bump(tmp_path):
    """A ``/api/playlist/load`` (a true source switch, bumping ``source_revision``) landing
    mid-render causes prewarm to discard the now-stale segment instead of queueing it and
    advancing ``played_tracks``."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue = asyncio.Queue()

    async def _staling_download(*_args, **_kwargs):
        # The source switch lands DURING the render (the download step), before the
        # post-render stale gate — so the gate sees the new source_revision and discards.
        state.source_revision += 1
        return tmp_path / "fake.mp3"

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=_staling_download),
        patch(f"{PRODUCER_MODULE}.normalize"),
        patch(f"{PRODUCER_MODULE}.shutil.copy2"),
        patch(f"{PRODUCER_MODULE}._set_last_music_file"),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
    ):
        await prewarm_first_segment(queue, state, config)

    assert queue.empty()  # the stale prewarm should be dropped
    assert len(state.played_tracks) == 0  # and must not advance played-history


@pytest.mark.asyncio
async def test_prewarm_survives_benign_playlist_edit(tmp_path):
    """A benign in-place edit (shuffle/add/move/enrich) bumps ``playlist_revision`` but NOT
    ``source_revision``, so prewarm must KEEP its on-source render rather than throw away
    the instant-audio pre-roll. Guards against regressing the gate to ``playlist_revision``."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue = asyncio.Queue()

    async def _benign_edit_download(*_args, **_kwargs):
        state.playlist_revision += 1  # a shuffle/add during the render — same source
        return tmp_path / "fake.mp3"

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=_benign_edit_download),
        patch(f"{PRODUCER_MODULE}.normalize"),
        patch(f"{PRODUCER_MODULE}.shutil.copy2"),
        patch(f"{PRODUCER_MODULE}._set_last_music_file"),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
    ):
        result = await prewarm_first_segment(queue, state, config)

    assert result is True  # kept — a benign edit did not change the source
    assert queue.qsize() == 1


@pytest.mark.asyncio
async def test_prewarm_discards_on_source_switch_during_egress(tmp_path):
    """The egress encode runs inside the funnel after the pre-egress gate, and the FM
    broadcast chain can make it slow. A source switch landing DURING that encode is caught
    by the funnel's opt-in post-egress stale check, so a stale prewarm is never put into the
    queue the switch route just purged (#665). Simulates the switch from inside the patched
    ``_apply_egress`` (the post-egress check runs right after it)."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue = asyncio.Queue()

    async def _switch_during_egress(seg, _config):
        state.source_revision += 1  # operator switched sources while egress was encoding
        return seg

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=tmp_path / "fake.mp3"),
        patch(f"{PRODUCER_MODULE}.normalize"),
        patch(f"{PRODUCER_MODULE}.shutil.copy2"),
        patch(f"{PRODUCER_MODULE}._set_last_music_file"),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}._apply_egress", new_callable=AsyncMock, side_effect=_switch_during_egress),
    ):
        result = await prewarm_first_segment(queue, state, config)

    assert result is False  # caught by the post-egress stale check, not queued
    assert queue.empty()
    assert len(state.played_tracks) == 0
    assert state.discard_by_reason.get(GenerationWasteReason.STALE_SOURCE) == 1


@pytest.mark.asyncio
async def test_prewarm_discards_stale_song_on_chaos_epoch_bump(tmp_path):
    """A chaos cutover landing mid-render causes prewarm to discard instead of queueing."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue = asyncio.Queue()

    async def _staling_download(*_args, **_kwargs):
        state.chaos_cutover_epoch += 1
        return tmp_path / "fake.mp3"

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=_staling_download),
        patch(f"{PRODUCER_MODULE}.normalize"),
        patch(f"{PRODUCER_MODULE}.shutil.copy2"),
        patch(f"{PRODUCER_MODULE}._set_last_music_file"),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
    ):
        result = await prewarm_first_segment(queue, state, config)

    assert result is False
    assert queue.empty()
    assert len(state.played_tracks) == 0
    assert state.discard_by_reason.get(GenerationWasteReason.STALE_CHAOS) == 1


@pytest.mark.asyncio
async def test_prewarm_discards_on_continuity_reservation_during_render(tmp_path):
    """A live continuity reservation landing mid-render must not be refilled by prewarm."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue = asyncio.Queue()

    async def _reserve_continuity(*_args, **_kwargs):
        state.continuity_epoch += 1
        return tmp_path / "fake.mp3"

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=_reserve_continuity),
        patch(f"{PRODUCER_MODULE}.normalize"),
        patch(f"{PRODUCER_MODULE}.shutil.copy2"),
        patch(f"{PRODUCER_MODULE}._set_last_music_file"),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
    ):
        result = await prewarm_first_segment(queue, state, config)

    assert result is False
    assert queue.empty()
    assert len(state.played_tracks) == 0
    assert state.discard_by_reason.get(GenerationWasteReason.STALE_CONTINUITY) == 1


@pytest.mark.asyncio
async def test_prewarm_discards_on_continuity_reservation_during_egress(tmp_path):
    """The post-egress prewarm stale gate also honors a live continuity reservation."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue = asyncio.Queue()

    async def _reserve_continuity_during_egress(segment, _config):
        state.continuity_epoch += 1
        return segment

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=tmp_path / "fake.mp3"),
        patch(f"{PRODUCER_MODULE}.normalize"),
        patch(f"{PRODUCER_MODULE}.shutil.copy2"),
        patch(f"{PRODUCER_MODULE}._set_last_music_file"),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(
            f"{PRODUCER_MODULE}._apply_egress",
            new_callable=AsyncMock,
            side_effect=_reserve_continuity_during_egress,
        ),
    ):
        result = await prewarm_first_segment(queue, state, config)

    assert result is False
    assert queue.empty()
    assert len(state.played_tracks) == 0
    assert state.discard_by_reason.get(GenerationWasteReason.STALE_CONTINUITY) == 1


@pytest.mark.asyncio
async def test_prewarm_skipped_when_session_stopped(tmp_path):
    """Post-restart scenario (audio-delivery rule): a session left stopped — a watchdog/HA
    restart that persisted ``session_stopped`` — short-circuits prewarm at entry, BEFORE the
    source gate, so it queues nothing and the resume path stays in control."""
    state = _make_state()
    state.session_stopped = True
    config = _make_config(tmp_path)
    queue: asyncio.Queue = asyncio.Queue()
    result = await prewarm_first_segment(queue, state, config)
    assert result is False
    assert queue.empty()


@pytest.mark.asyncio
async def test_prewarm_returns_false_when_render_unavailable(tmp_path):
    """Empty-fallback scenario (audio-delivery rule): prewarm has no canned/norm-cache
    fallback of its own — when nothing can be rendered (``_render_music_track`` returns
    None), it returns False and queues nothing rather than airing silence. The normal
    producer loop then supplies first audio, so instant audio is preserved."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue = asyncio.Queue()
    with patch(f"{PRODUCER_MODULE}._render_music_track", new_callable=AsyncMock, return_value=None):
        result = await prewarm_first_segment(queue, state, config)
    assert result is False
    assert queue.empty()


@pytest.mark.asyncio
async def test_prewarm_blocklist_drop_does_not_set_last_music_file(tmp_path):
    """A banned song dropped at the prewarm enqueue funnel must not seed last_music_file."""
    state = _make_state()
    state.playlist = state.playlist[:1]  # deterministic: only the banned track is eligible
    state.blocklist = {("artista", "canzone uno"): {"display": "Artista - Canzone Uno"}}
    config = _make_config(tmp_path)
    queue: asyncio.Queue = asyncio.Queue()
    cache_path = _normalized_cache_path(state.playlist[0], config)

    def _fake_copy2(_src, dst):
        Path(dst).write_bytes(b"cached")

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=tmp_path / "fake.mp3"),
        patch(f"{PRODUCER_MODULE}.normalize"),
        patch(f"{PRODUCER_MODULE}.shutil.copy2", side_effect=_fake_copy2),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
    ):
        result = await prewarm_first_segment(queue, state, config)

    assert result is False
    assert queue.empty()
    assert state.last_music_file is None
    assert producer._last_music_file is None
    assert cache_path.exists()  # cache copy may succeed; it must not become the bed source


# ---------------------------------------------------------------------------
# 5. Blocklist drop on main-loop commit must not append a shadow row (#660).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocklist_drop_on_main_loop_does_not_append_shadow_row(tmp_path):
    """A banned song dropped at the enqueue funnel must not leave a ghost up-next row
    or overwrite the prior valid music bed used by speech-bed adjacency (#660, #664)."""
    state = _make_state()
    previous_song = tmp_path / "previous_song.mp3"
    previous_song.write_bytes(b"prior-music")
    state.last_music_file = previous_song
    state.last_enqueued_type = SegmentType.MUSIC
    state.current_track = Track(title="Previous Song", artist="Prior Artist", duration_ms=180_000, spotify_id="prev")
    producer._last_music_file = previous_song
    state.blocklist = {
        ("artista", "canzone uno"): {"display": "Artista - Canzone Uno"},
        ("artista", "canzone due"): {"display": "Artista - Canzone Due"},
    }
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    cache_path = _normalized_cache_path(state.playlist[0], config)

    probe_calls = 0

    def _probe(_path):
        nonlocal probe_calls
        probe_calls += 1
        return 1.0

    def _fake_copy2(_src, dst):
        Path(dst).write_bytes(b"cached")

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=tmp_path / "fake.mp3"),
        patch(f"{PRODUCER_MODULE}.normalize"),
        patch(f"{PRODUCER_MODULE}.shutil.copy2", side_effect=_fake_copy2),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", side_effect=_probe),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await _wait_for(lambda: probe_calls >= 2)
            assert queue.empty()
            assert state.queued_segments == []
            assert state.last_music_file == previous_song
            assert producer._last_music_file == previous_song
            assert producer._last_music_file != cache_path
            assert _adjacent_music_source(state) == previous_song
            assert cache_path.exists()
        finally:
            await _cancel(task)
