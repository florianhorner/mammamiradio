"""Producer integration tests for station imaging."""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import HostPersonality, Segment, SegmentType, StationState, Track
from mammamiradio.scheduling.producer import (
    RenderedMusicTrack,
    _crosses_music_speech_boundary,
    _enqueue_with_egress,
    _make_imaging_lib,
    run_producer,
)

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")
PRODUCER_MODULE = "mammamiradio.scheduling.producer"
SCRIPTWRITER_MODULE = "mammamiradio.hosts.scriptwriter"


@pytest.fixture(autouse=True)
def _reset_producer_imaging_globals():
    from mammamiradio.scheduling import producer

    producer._last_music_file = None
    yield
    producer._last_music_file = None


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


async def _run_until_queued(
    queue: asyncio.Queue[Segment],
    state: StationState,
    config,
    timeout: float = 5.0,
    target_qsize: int = 1,
) -> None:
    task = asyncio.create_task(run_producer(queue, state, config))
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while queue.qsize() < target_qsize:
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


async def _write_async_file(_text, _voice, path, **_kwargs):
    path.write_bytes(b"voice")


def test_music_speech_boundary_includes_voice_led_imaging_segments():
    for speech_type in (
        SegmentType.BANTER,
        SegmentType.NEWS_FLASH,
        SegmentType.AD,
        SegmentType.STATION_ID,
        SegmentType.SWEEPER,
        SegmentType.TIME_CHECK,
    ):
        assert _crosses_music_speech_boundary(SegmentType.MUSIC, speech_type)
        assert _crosses_music_speech_boundary(speech_type, SegmentType.MUSIC)


def test_make_imaging_lib_passes_cache_dir(tmp_path):
    config = _make_config(tmp_path)

    lib = _make_imaging_lib(config)

    assert lib.cache_dir == config.cache_dir


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
        patch(f"{PRODUCER_MODULE}.mix_voice_with_bed", side_effect=_mix_bed_side_effect) as mock_mix,
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
    assert mock_mix.call_args.args[3] == config.imaging.bed_volume_db
    imaging.pick_stinger.assert_not_called()


@pytest.mark.asyncio
async def test_imaging_after_prev_seg_type_reset_skips_spurious_transition(tmp_path):
    """Scenario 3: after a restart we cannot prove the previous segment was a song
    (prev_seg_type is None), so a leftover ``last_music_file`` must NOT become the
    bed — that is exactly the stale-song bleed. The banter still beds cleanly via
    the synthetic fallback (no dead air)."""
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
    # Stale leftover music is rejected — adjacency cannot be proven post-restart.
    assert imaging.pick_talk_bed.call_args.args[2] is None
    imaging.pick_stinger.assert_not_called()
    assert mock_concat.call_count == 1


@pytest.mark.asyncio
async def test_banter_talk_bed_severed_after_ad_no_stale_bleed(tmp_path):
    """Bleed regression (Scenario 1): a song that aired before an ad block must NOT
    bleed under a later banter. The previous queued segment is an AD, so the song
    still on disk in ``last_music_file`` is not an eligible bed source. This is the
    live-observed ``MUSIC -> AD -> BANTER`` bleed."""
    stale_song = tmp_path / "stale_song.mp3"
    stale_song.write_bytes(b"music")
    state = _make_state()
    state.last_music_file = stale_song
    config = _make_config(tmp_path)
    config.pacing.lookahead_segments = 2
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    # An ad is the immediately-previous aired segment (the song is now stale).
    queue.put_nowait(Segment(type=SegmentType.AD, path=tmp_path / "prev_ad.mp3", ephemeral=False))
    host = config.hosts[0]
    banter_lines = [(host, "Si torna a noi.")]

    with (
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=tmp_path / "dialogue.mp3"),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_concat_side_effect),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=3.0),
        patch(f"{PRODUCER_MODULE}.mix_voice_with_bed", side_effect=_mix_bed_side_effect),
        patch(f"{PRODUCER_MODULE}.crossfade_voice_over_music") as mock_xfade,
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.ImagingLibrary") as mock_imaging_cls,
    ):
        imaging = mock_imaging_cls.return_value
        imaging.pick_talk_bed.side_effect = lambda _duration, output_path, _source_track=None: output_path

        await _run_until_queued(queue, state, config, target_qsize=2)

    queue.get_nowait()  # the seeded AD
    seg = queue.get_nowait()
    assert seg.type == SegmentType.BANTER
    # The 3-ads-ago song is NOT reused as the bed...
    assert imaging.pick_talk_bed.call_args.args[2] is None
    # ...nor crossfaded under the transition opener.
    mock_xfade.assert_not_called()
    # AD -> BANTER is speech-to-speech: no stinger.
    imaging.pick_stinger.assert_not_called()


@pytest.mark.asyncio
async def test_banter_talk_bed_uses_song_when_music_adjacent(tmp_path):
    """Positive (no over-fix): a direct MUSIC -> BANTER still beds the just-played
    song — the legitimate 'host over the outro' direction is preserved."""
    song = tmp_path / "adjacent_song.mp3"
    song.write_bytes(b"music")
    state = _make_state()
    state.last_music_file = song
    config = _make_config(tmp_path)
    config.pacing.lookahead_segments = 2
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    # A song is the immediately-previous aired segment.
    queue.put_nowait(Segment(type=SegmentType.MUSIC, path=song, ephemeral=False))
    host = config.hosts[0]
    banter_lines = [(host, "Che pezzo!")]

    xfade_sources: list[Path] = []

    def _xfade_writes_output(_music, _voice, output_path, *_a, **_k):
        xfade_sources.append(_music)
        Path(output_path).write_bytes(b"crossfaded")
        return output_path

    with (
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=tmp_path / "dialogue.mp3"),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_concat_side_effect),
        patch(f"{PRODUCER_MODULE}.crossfade_voice_over_music", side_effect=_xfade_writes_output),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=3.0),
        patch(f"{PRODUCER_MODULE}.mix_voice_with_bed", side_effect=_mix_bed_side_effect),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.ImagingLibrary") as mock_imaging_cls,
    ):
        imaging = mock_imaging_cls.return_value
        imaging.pick_talk_bed.side_effect = lambda _duration, output_path, _source_track=None: output_path
        imaging.pick_stinger.side_effect = lambda _from_seg, _to_seg, output_path: output_path

        await _run_until_queued(queue, state, config, target_qsize=2)

    queue.get_nowait()  # the seeded MUSIC
    seg = queue.get_nowait()
    assert seg.type == SegmentType.BANTER
    # The adjacent song IS used as the bed AND crossfaded under the opener —
    # the legitimate "host over the outro" direction still works.
    assert imaging.pick_talk_bed.call_args.args[2] == song
    assert xfade_sources == [song]


@pytest.mark.asyncio
async def test_banter_talk_bed_uses_norm_cache_rescue_song_not_prior_render(tmp_path):
    """Residual #641: a rescue song queued through the funnel becomes the bed source."""
    prior_render = tmp_path / "prior_render_a.mp3"
    prior_render.write_bytes(b"old music")
    rescue_song = tmp_path / "norm_rescue_b_192k.mp3"
    rescue_song.write_bytes(b"rescue music")
    state = _make_state()
    state.last_music_file = prior_render
    config = _make_config(tmp_path)
    config.pacing.lookahead_segments = 2
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    rescue = Segment(
        type=SegmentType.MUSIC,
        path=rescue_song,
        metadata={"title": "Rescue B", "queue_drain_recovery": True, "rescue": True},
        ephemeral=False,
    )
    with patch(f"{PRODUCER_MODULE}._apply_egress", new_callable=AsyncMock, return_value=rescue):
        assert await _enqueue_with_egress(queue, state, config, rescue) is True

    assert state.last_enqueued_type == SegmentType.MUSIC
    assert state.last_music_file == rescue_song

    host = config.hosts[0]
    banter_lines = [(host, "Rientriamo sul pezzo giusto.")]
    xfade_sources: list[Path] = []

    def _xfade_writes_output(_music, _voice, output_path, *_a, **_k):
        xfade_sources.append(_music)
        Path(output_path).write_bytes(b"crossfaded")
        return output_path

    with (
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        # Force the empty-fallback path (no canned clip on disk) so the generated talk-bed
        # logic under test actually runs, per the audio-delivery coverage rule.
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=tmp_path / "dialogue.mp3"),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_concat_side_effect),
        patch(f"{PRODUCER_MODULE}.crossfade_voice_over_music", side_effect=_xfade_writes_output),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=3.0),
        patch(f"{PRODUCER_MODULE}.mix_voice_with_bed", side_effect=_mix_bed_side_effect),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.ImagingLibrary") as mock_imaging_cls,
    ):
        imaging = mock_imaging_cls.return_value
        imaging.pick_talk_bed.side_effect = lambda _duration, output_path, _source_track=None: output_path
        imaging.pick_stinger.side_effect = lambda _from_seg, _to_seg, output_path: output_path

        await _run_until_queued(queue, state, config, target_qsize=2)

    queue.get_nowait()  # the rescue MUSIC
    seg = queue.get_nowait()
    assert seg.type == SegmentType.BANTER
    assert imaging.pick_talk_bed.call_args.args[2] == rescue_song
    assert xfade_sources == [rescue_song]


@pytest.mark.asyncio
async def test_transition_sting_prepended_at_music_to_banter_boundary(tmp_path):
    state = _make_state()
    state.segments_produced = 1
    config = _make_config(tmp_path)
    config.pacing.lookahead_segments = 2
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    previous_music = tmp_path / "previous_music.mp3"
    previous_music.write_bytes(b"music")
    queue.put_nowait(Segment(type=SegmentType.MUSIC, path=previous_music, ephemeral=False))
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

        await _run_until_queued(queue, state, config, target_qsize=2)

    queue.get_nowait()
    seg = queue.get_nowait()
    assert seg.type == SegmentType.BANTER
    assert seg.path.name.startswith("segment_with_sting_")
    imaging.pick_stinger.assert_called_once()
    assert imaging.pick_stinger.call_args.args[:2] == (SegmentType.MUSIC, SegmentType.BANTER)
    assert mock_concat.call_count == 2
    stinger_path = imaging.pick_stinger.call_args.args[2]
    final_concat_inputs = mock_concat.call_args_list[-1].args[0]
    assert final_concat_inputs[0] == stinger_path


@pytest.mark.asyncio
async def test_transition_sting_preserves_cached_music_file(tmp_path):
    state = _make_state()
    state.segments_produced = 1
    config = _make_config(tmp_path)
    config.pacing.lookahead_segments = 2
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    previous_banter = tmp_path / "previous_banter.mp3"
    previous_banter.write_bytes(b"banter")
    queue.put_nowait(Segment(type=SegmentType.BANTER, path=previous_banter, ephemeral=False))
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

        await _run_until_queued(queue, state, config, target_qsize=2)

    queue.get_nowait()
    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC
    assert seg.path.name.startswith("segment_with_sting_")
    assert seg.ephemeral is True
    assert cached_music.exists()
    imaging.pick_stinger.assert_called_once()
    assert imaging.pick_stinger.call_args.args[:2] == (SegmentType.BANTER, SegmentType.MUSIC)


@pytest.mark.asyncio
async def test_banter_talk_bed_failure_queues_dry_banter(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    banter_lines = [(host, "Restiamo asciutti.")]

    with (
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora.")),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=tmp_path / "dialogue.mp3"),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_concat_side_effect),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=2.7),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.ImagingLibrary") as mock_imaging_cls,
    ):
        imaging = mock_imaging_cls.return_value
        imaging.pick_talk_bed.side_effect = RuntimeError("bed failed")

        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.BANTER
    assert seg.path.name.startswith("banter_full_")
    imaging.pick_talk_bed.assert_called_once()


@pytest.mark.asyncio
async def test_news_flash_uses_talk_bed_when_crossfade_unavailable(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.NEWS_FLASH),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_news_flash",
            new_callable=AsyncMock,
            return_value=(host, "Notizia.", "traffic"),
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=_write_async_file),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.8),
        patch(f"{PRODUCER_MODULE}.mix_voice_with_bed", side_effect=_mix_bed_side_effect) as mock_mix,
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.ImagingLibrary") as mock_imaging_cls,
    ):
        imaging = mock_imaging_cls.return_value
        imaging.pick_talk_bed.side_effect = lambda _duration, output_path, _source_track=None: output_path

        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.NEWS_FLASH
    assert seg.path.name.startswith("news_bedded_")
    assert mock_mix.call_args.args[3] == config.imaging.bed_volume_db
    imaging.pick_talk_bed.assert_called_once()


@pytest.mark.asyncio
async def test_news_flash_crossfade_severed_after_ad(tmp_path):
    """A news flash after an ad block must NOT crossfade over the now-stale song —
    it falls back to a talk bed with no song source (the live song->news bleed)."""
    stale = tmp_path / "stale_song.mp3"
    stale.write_bytes(b"music")
    state = _make_state()
    state.last_music_file = stale
    config = _make_config(tmp_path)
    config.pacing.lookahead_segments = 2
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    queue.put_nowait(Segment(type=SegmentType.AD, path=tmp_path / "prev_ad.mp3", ephemeral=False))
    host = config.hosts[0]

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.NEWS_FLASH),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_news_flash",
            new_callable=AsyncMock,
            return_value=(host, "Notizia.", "traffic"),
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=_write_async_file),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.8),
        patch(f"{PRODUCER_MODULE}.mix_voice_with_bed", side_effect=_mix_bed_side_effect),
        patch(f"{PRODUCER_MODULE}.crossfade_voice_over_music") as mock_xfade,
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.ImagingLibrary") as mock_imaging_cls,
    ):
        imaging = mock_imaging_cls.return_value
        imaging.pick_talk_bed.side_effect = lambda _duration, output_path, _source_track=None: output_path

        await _run_until_queued(queue, state, config, target_qsize=2)

    queue.get_nowait()  # the seeded AD
    seg = queue.get_nowait()
    assert seg.type == SegmentType.NEWS_FLASH
    mock_xfade.assert_not_called()
    assert imaging.pick_talk_bed.call_args.args[2] is None


@pytest.mark.asyncio
async def test_news_flash_crossfade_uses_adjacent_song(tmp_path):
    """A news flash directly after a song crossfades over THAT song (preserved)."""
    song = tmp_path / "adjacent_song.mp3"
    song.write_bytes(b"music")
    state = _make_state()
    state.last_music_file = song
    config = _make_config(tmp_path)
    config.pacing.lookahead_segments = 2
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    queue.put_nowait(Segment(type=SegmentType.MUSIC, path=song, ephemeral=False))
    host = config.hosts[0]
    xfade_sources: list[Path] = []

    def _xf(_music, _voice, output_path, *_a, **_k):
        xfade_sources.append(_music)
        Path(output_path).write_bytes(b"crossfaded")
        return output_path

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.NEWS_FLASH),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_news_flash",
            new_callable=AsyncMock,
            return_value=(host, "Notizia.", "traffic"),
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=_write_async_file),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.8),
        patch(f"{PRODUCER_MODULE}.crossfade_voice_over_music", side_effect=_xf),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.ImagingLibrary") as mock_imaging_cls,
    ):
        imaging = mock_imaging_cls.return_value
        imaging.pick_stinger.side_effect = lambda _from_seg, _to_seg, output_path: output_path

        await _run_until_queued(queue, state, config, target_qsize=2)

    queue.get_nowait()  # the seeded MUSIC
    seg = queue.get_nowait()
    assert seg.type == SegmentType.NEWS_FLASH
    assert xfade_sources == [song]


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["weather", "sports"])
async def test_ha_context_refreshed_for_news_flash(tmp_path, category):
    """#626: NEWS_FLASH now participates in the HA refresh gate, so the meteo flash
    grounds itself in a freshly refreshed forecast instead of the startup snapshot.
    Previously the gate covered only BANTER/AD and a flash could air stale weather.

    The gate fires for EVERY flash category (the category isn't known until
    write_news_flash runs), so a non-weather flash refreshes HA too — guarded here
    so narrowing the gate to weather-only would fail."""
    state = _make_state()
    config = _make_config(tmp_path)
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]

    mock_context = MagicMock()
    mock_context.summary = "Il tempo e' bello"
    mock_context.events_summary = ""
    mock_context.events_summary_en = ""
    mock_context.mood = "Caffe in preparazione"
    mock_context.mood_en = "Coffee brewing"
    mock_context.weather_arc = "Meteo: soleggiato, 22C."
    mock_context.weather_arc_en = "Weather: sunny, 22C."
    mock_context.timestamp = 1234.5
    mock_context.scored = []
    mock_context.denylist_hits = {}
    mock_context.catalog_hit_rate = 0.0
    mock_context.label_stats = {}
    mock_context.registry_source = ""
    mock_context.raw_states = {}
    mock_context.events = deque()
    mock_context.last_event_label_en = ""

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.NEWS_FLASH),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_news_flash",
            new_callable=AsyncMock,
            return_value=(host, "Flash.", category),
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=_write_async_file),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.8),
        patch(f"{PRODUCER_MODULE}.mix_voice_with_bed", side_effect=_mix_bed_side_effect),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=mock_context) as mock_fetch,
        patch(f"{PRODUCER_MODULE}.ImagingLibrary") as mock_imaging_cls,
    ):
        imaging = mock_imaging_cls.return_value
        imaging.pick_talk_bed.side_effect = lambda _duration, output_path, _source_track=None: output_path

        await _run_until_queued(queue, state, config)

    # The gate fired for NEWS_FLASH: the forecast was refreshed onto state.
    mock_fetch.assert_called_once()
    assert state.ha_weather_arc == "Meteo: soleggiato, 22C."
    assert state.ha_weather_arc_en == "Weather: sunny, 22C."
    assert queue.get_nowait().type == SegmentType.NEWS_FLASH


@pytest.mark.asyncio
async def test_sweeper_sting_failure_cleans_temp_files_and_skips_segment(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    track = state.playlist[0]
    cached_music = tmp_path / "norm_after_sweeper_failure_192k.mp3"
    cached_music.write_bytes(b"cached music")

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", side_effect=[SegmentType.SWEEPER, SegmentType.MUSIC]),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=_write_async_file),
        patch(
            f"{PRODUCER_MODULE}._render_music_track",
            new_callable=AsyncMock,
            return_value=RenderedMusicTrack(track=track, path=cached_music, cache_path=cached_music, cache_hit=True),
        ),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=120.0),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.ImagingLibrary") as mock_imaging_cls,
    ):
        imaging = mock_imaging_cls.return_value
        imaging.pick_sweeper_sting.side_effect = RuntimeError("sting failed")

        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC
    assert not list(tmp_path.glob("sweeper_*.mp3"))
    assert not list(tmp_path.glob("sweeper_sting_*.mp3"))
    assert not list(tmp_path.glob("sweeper_mixed_*.mp3"))


@pytest.mark.asyncio
async def test_music_last_file_uses_temp_path_when_cache_write_failed(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    track = state.playlist[0]
    temp_music = tmp_path / "music_uncached.mp3"
    temp_music.write_bytes(b"music")
    missing_cache = tmp_path / "norm_missing_cache_192k.mp3"

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(
            f"{PRODUCER_MODULE}._render_music_track",
            new_callable=AsyncMock,
            return_value=RenderedMusicTrack(track=track, path=temp_music, cache_path=missing_cache, cache_hit=False),
        ),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=120.0),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert queue.get_nowait().path == temp_music
    assert state.last_music_file == temp_music


@pytest.mark.asyncio
async def test_idle_bridge_updates_boundary_before_next_music(tmp_path):
    state = _make_state()
    state.listeners_active = 0
    state.segments_produced = 1
    config = _make_config(tmp_path)
    config.pacing.lookahead_segments = 2
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    track = state.playlist[0]
    bridge = tmp_path / "bridge.mp3"
    bridge.write_bytes(b"bridge")
    cached_music = tmp_path / "norm_after_bridge_192k.mp3"
    cached_music.write_bytes(b"cached music")

    async def _activate_listener():
        await asyncio.sleep(0.15)
        state.listeners_active = 1

    activation_task = asyncio.create_task(_activate_listener())
    try:
        with (
            patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=bridge),
            patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
            patch(
                f"{PRODUCER_MODULE}._render_music_track",
                new_callable=AsyncMock,
                return_value=RenderedMusicTrack(
                    track=track,
                    path=cached_music,
                    cache_path=cached_music,
                    cache_hit=True,
                ),
            ),
            patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_concat_side_effect),
            patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=120.0),
            patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
            patch(f"{PRODUCER_MODULE}.ImagingLibrary") as mock_imaging_cls,
        ):
            imaging = mock_imaging_cls.return_value
            imaging.pick_stinger.side_effect = lambda _from_seg, _to_seg, output_path: output_path

            await _run_until_queued(queue, state, config, target_qsize=2)
    finally:
        await activation_task

    bridge_seg = queue.get_nowait()
    music_seg = queue.get_nowait()
    assert bridge_seg.type == SegmentType.BANTER
    assert music_seg.type == SegmentType.MUSIC
    assert music_seg.path.name.startswith("segment_with_sting_")
    imaging.pick_stinger.assert_called_once()
    assert imaging.pick_stinger.call_args.args[:2] == (SegmentType.BANTER, SegmentType.MUSIC)
