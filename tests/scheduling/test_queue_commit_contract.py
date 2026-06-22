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
   discards generated SPEECH and operator AIR-NEXT, not just music
   (``producer.py:3019`` / ``:3025``);
2. an operator AIR-NEXT discarded by that gate releases ``operator_force_pending``
   so the operator is not locked out (``producer.py:3022-3023``);
3. direct-enqueue paths (prewarm + bridges, via ``_enqueue_with_egress`` with no
   front-insert) air with NO up-next shadow row, while outer error-recovery
   rescue — which flows through the epilogue — DOES get a row (``producer.py:3060``);
4. prewarm discards a stale-source render instead of queueing it (#659);
5. the blocklist funnel drop is propagated by ``_queue_segment`` — a banned song
   dropped mid-commit leaves no shadow row or counter advance (#660).

Mechanism note: the stale gate keys on a closure-local ``generation_revision``
that a unit test cannot poke, so these drive ``run_producer`` (or
``prewarm_first_segment``) and bump ``state.playlist_revision`` /
``chaos_cutover_epoch`` from inside a patched render step — the duration probe
for the epilogue paths, the mocked download for prewarm — to model "a source
switch landed during this segment's build."
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import Segment, SegmentType, StationState, Track
from mammamiradio.scheduling.producer import (
    _enqueue_with_egress,
    prewarm_first_segment,
    run_producer,
)

PRODUCER_MODULE = "mammamiradio.scheduling.producer"
TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


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
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# 1. The stale gate is a shared epilogue — it covers generated SPEECH too.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stale_field", ["playlist_revision", "chaos_cutover_epoch"])
@pytest.mark.asyncio
async def test_stale_gate_discards_generated_speech(tmp_path, stale_field):
    """A TIME_CHECK built before a source switch (``playlist_revision`` bump) OR a
    chaos cutover (``chaos_cutover_epoch`` bump) is discarded by the same shared
    epilogue gate that guards music (``producer.py:3019`` / ``:3025``). Pins that the
    gate is NOT music-specific and covers BOTH stale axes; a discard queues nothing
    and runs no success callback (``state.segments_produced`` stays 0)."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    probe_calls = 0

    def _staling_probe(_path):
        nonlocal probe_calls
        probe_calls += 1
        # a source switch / chaos cutover landed during this build
        setattr(state, stale_field, getattr(state, stale_field) + 1)
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
        finally:
            await _cancel(task)


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


# ---------------------------------------------------------------------------
# 3. Shadow-row visibility: direct-enqueue paths get NO row; epilogue rescue does.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_enqueue_airs_without_shadow_row(tmp_path):
    """The funnel prewarm + bridges use (``_enqueue_with_egress`` without
    front-insert) queues audio but appends NO up-next shadow row — those segments
    air invisibly in the queue projection. Also pins that a rescue fill skips the
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
    assert state.queued_segments == []  # but produced no up-next row
    m_chain.assert_not_called()  # rescue skipped the egress colour pass


@pytest.mark.asyncio
async def test_prewarm_airs_without_shadow_row(tmp_path):
    """A pre-warmed first segment is queued but invisible in the up-next shadow
    list until it airs (``prewarm_first_segment`` never appends a row)."""
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
    assert state.queued_segments == []  # prewarm is invisible in up-next until aired


@pytest.mark.asyncio
async def test_error_recovery_rescue_appends_shadow_row(tmp_path):
    """Outer error-recovery rescue (``rescue=True``) is built inside the main loop
    body and so flows through the epilogue — unlike a bridge it DOES append an
    up-next shadow row (``producer.py:3060``). Also exercises the empty-container
    fallback (Scenario 2): no canned clip available, so the silence rescue fires."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=RuntimeError("network down")),
        patch(f"{PRODUCER_MODULE}.generate_silence", side_effect=lambda p, *_a: Path(p).write_bytes(b"x")),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),  # empty container -> silence rescue
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await _wait_for(lambda: queue.qsize() > 0)
            seg = queue.get_nowait()
            assert seg.metadata.get("rescue") is True
            assert len(state.queued_segments) == 1  # rescue-via-epilogue DID add an up-next row
        finally:
            await _cancel(task)


# ---------------------------------------------------------------------------
# 4. Prewarm stale-source gate (#659) and blocklist drop propagation (#660).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stale_field", ["source_revision", "chaos_cutover_epoch"])
@pytest.mark.asyncio
async def test_prewarm_discards_stale_song_on_source_change(tmp_path, stale_field):
    """A true source switch (``source_revision``, bumped only by ``switch_playlist``) or a
    chaos cutover (``chaos_cutover_epoch``) landing mid-render makes prewarm discard the
    now-stale segment instead of queueing it and advancing played-history (#659). The
    normal producer loop then airs the first real segment from the new source, so a
    discarded prewarm never starves instant audio. (A benign in-place edit that bumps only
    ``playlist_revision`` deliberately does NOT discard — the song is still on-source.)"""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue = asyncio.Queue()

    async def _staling_download(*_args, **_kwargs):
        # The switch / cutover lands DURING the render (the download step), before the
        # post-render stale gate — so the gate sees it and discards.
        setattr(state, stale_field, getattr(state, stale_field) + 1)
        return tmp_path / "fake.mp3"

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=_staling_download),
        patch(f"{PRODUCER_MODULE}.normalize"),
        patch(f"{PRODUCER_MODULE}.shutil.copy2"),
        patch(f"{PRODUCER_MODULE}._set_last_music_file"),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
    ):
        result = await prewarm_first_segment(queue, state, config)

    assert result is False  # the stale prewarm was discarded, not queued
    assert queue.empty()
    assert len(state.played_tracks) == 0  # played-history not advanced


@pytest.mark.asyncio
async def test_prewarm_survives_benign_playlist_edit(tmp_path):
    """A benign in-place edit (shuffle/add/move/enrich) bumps ``playlist_revision`` but NOT
    ``source_revision``, so the prewarm gate must KEEP its on-source render rather than
    throw away the instant-audio pre-roll. Guards against regressing the gate back to the
    broad ``playlist_revision`` signal."""
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
async def test_blocklist_drop_leaves_no_shadow_row(tmp_path):
    """A banned song that slips past the ingest doorways and reaches the main-loop commit
    is dropped by the funnel AND leaves no up-next shadow row or counter advance:
    ``_queue_segment`` now propagates the funnel's drop instead of swallowing it (#660)."""
    state = _make_state()
    state.playlist[0].youtube_id = "yt_demo1"
    state.playlist[1].youtube_id = "yt_demo2"
    # Ban both pool tracks so the producer's selection necessarily lands on a banned song
    # (models the ban-mid-render race the funnel gate backstops).
    state.blocklist = {
        ("artista", "canzone uno"): {"display": "Artista - Canzone Uno"},
        ("artista", "canzone due"): {"display": "Artista - Canzone Due"},
    }
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    download_calls = 0

    async def _count_download(*_args, **_kwargs):
        nonlocal download_calls
        download_calls += 1
        src = tmp_path / "src.mp3"
        src.write_bytes(b"audio")
        return src

    def _norm(_src, dst, *_a, **_k):
        Path(dst).write_bytes(b"audio")

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=_count_download),
        patch(f"{PRODUCER_MODULE}.normalize", side_effect=_norm),
        patch(f"{PRODUCER_MODULE}.shutil.copy2"),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            # Two build attempts prove at least one banned song was built and dropped at
            # the funnel (the producer keeps retrying because the whole pool is banned).
            await _wait_for(lambda: download_calls >= 2)
            assert queue.empty()  # banned audio never reaches the queue
            assert state.queued_segments == []  # nor a phantom up-next shadow row
            assert state.segments_produced == 0  # nor a counter advance
        finally:
            await _cancel(task)
