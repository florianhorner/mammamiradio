"""Producer integration tests for station imaging."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import HostPersonality, Segment, SegmentType, StationState, Track
from mammamiradio.scheduling.producer import RenderedMusicTrack, run_producer

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")
PRODUCER_MODULE = "mammamiradio.scheduling.producer"
SCRIPTWRITER_MODULE = "mammamiradio.hosts.scriptwriter"


@pytest.fixture(autouse=True)
def _reset_producer_imaging_globals():
    from mammamiradio.scheduling import producer

    producer._last_music_file = None
    producer._prev_seg_type = None
    yield
    producer._last_music_file = None
    producer._prev_seg_type = None


@pytest.fixture(autouse=True)
def _mock_quality_gate():
    with patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None):
        yield


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
    config.pacing.lookahead_segments = 1
    config.homeassistant.enabled = False
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    return config


async def _run_until_queued(queue: asyncio.Queue[Segment], state: StationState, config, timeout: float = 5.0) -> None:
    task = asyncio.create_task(run_producer(queue, state, config))
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while queue.qsize() == 0:
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError("Producer did not queue a segment in time")
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _concat_side_effect(_paths, output_path, *_args, **_kwargs):
    return output_path


def _mix_bed_side_effect(_voice_path, _bed_path, output_path, _bed_db=-18.0):
    return output_path


@pytest.mark.asyncio
async def test_banter_talk_bed_cold_start_no_last_music_file(tmp_path):
    """Scenario 2: banter still gets a synthetic talk bed with no last music file."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")
    banter_lines = [(host, "Che bella giornata!")]

    with (
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=tmp_path / "dialogue.mp3"),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_concat_side_effect),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=3.2),
        patch(f"{PRODUCER_MODULE}.mix_voice_with_bed", side_effect=_mix_bed_side_effect),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.ImagingLibrary") as mock_imaging_cls,
    ):
        imaging = mock_imaging_cls.return_value
        imaging.pick_talk_bed.side_effect = lambda _duration, output_path, _source_track=None: output_path

        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.BANTER
    assert seg.path.name.startswith("banter_bedded_")
    args = imaging.pick_talk_bed.call_args.args
    assert args[0] == 3.2
    assert args[2] is None
    imaging.pick_stinger.assert_not_called()


@pytest.mark.asyncio
async def test_imaging_after_prev_seg_type_reset_skips_spurious_transition(tmp_path):
    """Scenario 3: process restart resets _prev_seg_type and first segment still beds cleanly."""
    from mammamiradio.scheduling import producer

    producer._prev_seg_type = None
    last_music = tmp_path / "previous_music.mp3"
    last_music.write_bytes(b"music")
    state = _make_state()
    state.last_music_file = last_music
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    banter_lines = [(host, "Ripartiamo subito.")]

    with (
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Siamo tornati.")),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=tmp_path / "dialogue.mp3"),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_concat_side_effect) as mock_concat,
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=2.8),
        patch(f"{PRODUCER_MODULE}.mix_voice_with_bed", side_effect=_mix_bed_side_effect),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.ImagingLibrary") as mock_imaging_cls,
    ):
        imaging = mock_imaging_cls.return_value
        imaging.pick_talk_bed.side_effect = lambda _duration, output_path, _source_track=None: output_path

        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.BANTER
    assert imaging.pick_talk_bed.call_args.args[2] == last_music
    imaging.pick_stinger.assert_not_called()
    assert mock_concat.call_count == 1


@pytest.mark.asyncio
async def test_transition_sting_prepended_at_music_to_banter_boundary(tmp_path):
    from mammamiradio.scheduling import producer

    producer._prev_seg_type = SegmentType.MUSIC
    state = _make_state()
    state.segments_produced = 1
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    banter_lines = [(host, "E adesso parliamo.")]

    with (
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition",
            new_callable=AsyncMock,
            return_value=(host, "Prima di tutto."),
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=tmp_path / "dialogue.mp3"),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_concat_side_effect) as mock_concat,
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=3.5),
        patch(f"{PRODUCER_MODULE}.mix_voice_with_bed", side_effect=_mix_bed_side_effect),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.ImagingLibrary") as mock_imaging_cls,
    ):
        imaging = mock_imaging_cls.return_value
        imaging.pick_talk_bed.side_effect = lambda _duration, output_path, _source_track=None: output_path
        imaging.pick_stinger.side_effect = lambda _from_seg, _to_seg, output_path: output_path

        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.BANTER
    assert seg.path.name.startswith("segment_with_sting_")
    imaging.pick_stinger.assert_called_once()
    assert imaging.pick_stinger.call_args.args[:2] == (SegmentType.MUSIC, SegmentType.BANTER)
    assert mock_concat.call_count == 2


@pytest.mark.asyncio
async def test_transition_sting_preserves_cached_music_file(tmp_path):
    from mammamiradio.scheduling import producer

    producer._prev_seg_type = SegmentType.BANTER
    state = _make_state()
    state.segments_produced = 1
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    track = state.playlist[0]
    cached_music = tmp_path / "norm_cached_track_192k.mp3"
    cached_music.write_bytes(b"cached music")

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(
            f"{PRODUCER_MODULE}._render_music_track",
            new_callable=AsyncMock,
            return_value=RenderedMusicTrack(track=track, path=cached_music, cache_path=cached_music, cache_hit=True),
        ),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_concat_side_effect),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=120.0),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.ImagingLibrary") as mock_imaging_cls,
    ):
        imaging = mock_imaging_cls.return_value
        imaging.pick_stinger.side_effect = lambda _from_seg, _to_seg, output_path: output_path

        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC
    assert seg.path.name.startswith("segment_with_sting_")
    assert seg.ephemeral is True
    assert cached_music.exists()
    imaging.pick_stinger.assert_called_once()
    assert imaging.pick_stinger.call_args.args[:2] == (SegmentType.BANTER, SegmentType.MUSIC)
