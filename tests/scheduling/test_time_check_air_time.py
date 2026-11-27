"""A time check names the minute the listener hears it, not the minute it was rendered.

A time check is frozen into audio when the producer renders it, but it airs only
after the rest of the segment on air and every segment queued ahead of it. The
runway governor renders a natural time check only behind at least four minutes
of queued audio, so announcing the render minute is wrong by construction.

These drive ``run_producer`` with a pinned wall clock, a queue of known segment
durations and a known on-air segment, then assert the exact line handed to TTS.

Audio delivery scenarios:
  1. Normal: natural time check behind a buffered queue plus the on-air remainder,
     and an operator air-next pick that only waits for the on-air remainder.
  2. Empty fallback: nothing queued and nothing on air, so the projection is the
     render minute itself (no lead is invented).
  3. Post-restart: ``now_streaming`` is empty or carries no usable duration after a
     restart, so only the queued audio counts.
"""

from __future__ import annotations

import asyncio
import datetime
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import URGENT_INTERRUPT_PRIORITY_KEY, Segment, SegmentType, StationState, Track
from mammamiradio.scheduling import producer
from mammamiradio.scheduling.producer import run_producer

PRODUCER_MODULE = "mammamiradio.scheduling.producer"
TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")
_REAL_ASYNCIO_SLEEP = asyncio.sleep


@pytest.fixture(autouse=True)
def _mock_quality_gate():
    with patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _clean_producer_globals():
    producer._last_music_file = None
    producer._canned_clip_cache.clear()
    producer._recently_played_clips.clear()
    yield
    producer._last_music_file = None
    producer._canned_clip_cache.clear()
    producer._recently_played_clips.clear()


def _frozen_clock(moment: datetime.datetime) -> Any:
    """A stand-in for the producer's ``datetime`` module whose ``now()`` is pinned."""

    class _FrozenDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return moment if tz is None else moment.replace(tzinfo=tz)

    class _ClockModule:
        datetime = _FrozenDatetime

        def __getattr__(self, name: str) -> Any:
            return getattr(datetime, name)

    return _ClockModule()


def _make_state() -> StationState:
    return StationState(
        playlist=[
            Track(title="Canzone Uno", artist="Artista", duration_ms=200_000, spotify_id="demo1"),
            Track(title="Canzone Due", artist="Artista", duration_ms=180_000, spotify_id="demo2"),
        ],
        listeners_active=1,
    )


def _make_config(tmp_path: Path):
    config = load_config(TOML_PATH)
    config.pacing.lookahead_segments = 4
    config.homeassistant.enabled = False
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    return config


def _write_concat(paths, out, *_args, **_kwargs):
    Path(out).write_bytes(b"audio")


def _prefill(
    queue: asyncio.Queue[Segment],
    state: StationState,
    tmp_path: Path,
    durations: list[float],
) -> list[Segment]:
    queued: list[Segment] = []
    for index, duration in enumerate(durations):
        path = tmp_path / f"queued-{index}.mp3"
        path.write_bytes(b"queued")
        segment = Segment(
            type=SegmentType.MUSIC,
            path=path,
            duration_sec=duration,
            metadata={"title": f"Queued {index}", "queue_id": f"queued-{index}"},
            ephemeral=False,
        )
        queue.put_nowait(segment)
        state.queued_segments.append(
            {"id": f"queued-{index}", "type": "music", "label": f"Queued {index}", "duration_sec": duration}
        )
        queued.append(segment)
    return queued


def _on_air(state: StationState, *, elapsed: float, duration: float | None) -> None:
    state.now_streaming = {
        "type": "music",
        "label": "On air",
        "started": time.time() - elapsed,
        "epoch": 1,
        "duration_sec": duration,
        "metadata": {},
    }


async def _first_time_check_text(
    tmp_path: Path,
    state: StationState,
    queue: asyncio.Queue[Segment],
    moment: datetime.datetime,
) -> tuple[str, list[Segment]]:
    """Run the producer until a time check queues; return the TTS line and the queue."""
    config = _make_config(tmp_path)
    synth = AsyncMock()
    imaging = MagicMock()
    imaging.pick_time_check_sting.side_effect = lambda path: Path(path).write_bytes(b"sting")

    def _has_time_check() -> bool:
        return any(seg.type is SegmentType.TIME_CHECK for seg in list(queue._queue))  # type: ignore[attr-defined]

    with (
        patch(f"{PRODUCER_MODULE}.datetime", _frozen_clock(moment)),
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.TIME_CHECK),
        patch(f"{PRODUCER_MODULE}.synthesize", synth),
        patch(f"{PRODUCER_MODULE}._make_imaging_lib", return_value=imaging),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_write_concat),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=4.0),
        patch(f"{PRODUCER_MODULE}._apply_egress", new_callable=AsyncMock, side_effect=lambda segment, _: segment),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + 5.0
            while not _has_time_check():
                if loop.time() > deadline:
                    raise TimeoutError("no time check queued before timeout")
                await _REAL_ASYNCIO_SLEEP(0.02)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    assert synth.await_args_list, "the time check never reached TTS"
    spoken = synth.await_args_list[0].args[0]
    return spoken, list(queue._queue)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_natural_time_check_names_the_minute_it_airs(tmp_path):
    """Scenario 1: 14:02:00 render, 170 s left on air, 390 s queued -> airs 14:11:20."""
    state = _make_state()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    queued = _prefill(queue, state, tmp_path, [200.0, 190.0])
    _on_air(state, elapsed=30.0, duration=200.0)
    station = _make_config(tmp_path).display_station_name

    spoken, items = await _first_time_check_text(tmp_path, state, queue, datetime.datetime(2026, 5, 4, 14, 2, 0))

    assert spoken == f"Sono le 14 e 11 su {station}."
    # It airs where the projection assumed: behind everything that was queued.
    assert items[: len(queued)] == queued
    assert items[len(queued)].type is SegmentType.TIME_CHECK
    assert items[len(queued)].metadata["time"] == spoken


@pytest.mark.asyncio
async def test_time_check_rolls_over_midnight(tmp_path):
    """23:58:00 render with 300 s queued airs at 00:03 on the next day."""
    state = _make_state()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    _prefill(queue, state, tmp_path, [180.0, 120.0])
    station = _make_config(tmp_path).display_station_name

    spoken, _items = await _first_time_check_text(tmp_path, state, queue, datetime.datetime(2026, 5, 4, 23, 58, 0))

    assert spoken == f"Sono le 0 e 3 su {station}."


@pytest.mark.asyncio
async def test_time_check_crossing_into_one_oclock_keeps_singular_form(tmp_path):
    """12:58:00 render with 300 s queued airs at 13:03: "È l'una", not "Sono le 13"."""
    state = _make_state()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    _prefill(queue, state, tmp_path, [180.0, 120.0])
    station = _make_config(tmp_path).display_station_name

    spoken, _items = await _first_time_check_text(tmp_path, state, queue, datetime.datetime(2026, 5, 4, 12, 58, 0))

    assert spoken == f"È l'una e 3 su {station}."


@pytest.mark.asyncio
async def test_empty_queue_and_nothing_on_air_announces_render_minute(tmp_path):
    """Scenario 2: no queued audio and nothing on air, so no lead is invented."""
    state = _make_state()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    station = _make_config(tmp_path).display_station_name

    spoken, items = await _first_time_check_text(tmp_path, state, queue, datetime.datetime(2026, 5, 4, 14, 2, 0))

    assert spoken == f"Sono le 14 e 2 su {station}."
    assert items[0].type is SegmentType.TIME_CHECK


@pytest.mark.parametrize(
    "now_streaming",
    [
        {},  # fresh process after a restart: nothing selected yet
        {"type": "music", "label": "On air", "started": 0, "duration_sec": None},  # placeholder row
    ],
)
@pytest.mark.asyncio
async def test_after_restart_without_on_air_duration_counts_queued_audio_only(tmp_path, now_streaming):
    """Scenario 3: a restarted station has no usable on-air remainder; queued audio still counts."""
    state = _make_state()
    state.now_streaming = now_streaming
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    _prefill(queue, state, tmp_path, [200.0, 190.0])
    station = _make_config(tmp_path).display_station_name

    spoken, _items = await _first_time_check_text(tmp_path, state, queue, datetime.datetime(2026, 5, 4, 14, 2, 0))

    # 390 s queued -> 14:08:30
    assert spoken == f"Sono le 14 e 8 su {station}."


@pytest.mark.asyncio
async def test_operator_air_next_time_check_waits_only_for_the_on_air_remainder(tmp_path):
    """An operator pick is front-inserted, so the queued songs air after it, not before."""
    state = _make_state()
    state.force_next = SegmentType.TIME_CHECK
    state.operator_force_pending = SegmentType.TIME_CHECK
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    queued = _prefill(queue, state, tmp_path, [200.0, 190.0])
    _on_air(state, elapsed=30.0, duration=200.0)
    station = _make_config(tmp_path).display_station_name

    spoken, items = await _first_time_check_text(tmp_path, state, queue, datetime.datetime(2026, 5, 4, 14, 2, 0))

    # 170 s left on air -> 14:04:50
    assert spoken == f"Sono le 14 e 4 su {station}."
    assert items[0].type is SegmentType.TIME_CHECK
    assert items[1 : 1 + len(queued)] == queued


def test_air_next_lead_counts_urgent_warnings_that_keep_the_head():
    """Urgent warnings stay ahead of an air-next pick, so their audio is part of the lead."""
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    urgent = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/urgent.mp3"),
        duration_sec=20.0,
        metadata={URGENT_INTERRUPT_PRIORITY_KEY: True},
    )
    song = Segment(type=SegmentType.MUSIC, path=Path("/tmp/song.mp3"), duration_sec=200.0, metadata={})
    queue.put_nowait(urgent)
    queue.put_nowait(song)
    state = StationState()
    now_ts = 1_000_000.0
    state.now_streaming = {"started": now_ts - 50.0, "duration_sec": 80.0}

    assert producer._time_check_lead_seconds(queue, state, air_next=True, now_ts=now_ts) == pytest.approx(50.0)
    assert producer._time_check_lead_seconds(queue, state, air_next=False, now_ts=now_ts) == pytest.approx(250.0)


@pytest.mark.parametrize("lead", [0.0, -5.0, float("nan"), float("inf")])
def test_projection_ignores_unusable_leads(lead):
    moment = datetime.datetime(2026, 5, 4, 14, 2, 0)
    assert producer._projected_air_time(moment, lead) == moment


def test_time_check_text_keeps_existing_phrasing():
    assert producer._time_check_text(datetime.datetime(2026, 5, 4, 1, 0), "Radio") == "È l'una su Radio."
    assert producer._time_check_text(datetime.datetime(2026, 5, 4, 9, 0), "Radio") == "Sono le 9 su Radio."
    assert producer._time_check_text(datetime.datetime(2026, 5, 4, 21, 7), "Radio") == "Sono le 21 e 7 su Radio."
