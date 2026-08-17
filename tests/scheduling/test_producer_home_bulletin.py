"""Producer-level contract tests for Il Bollettino di Casa."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mammamiradio.audio.audio_quality import AudioToolError
from mammamiradio.core.config import load_config
from mammamiradio.core.listener_session import CasaBulletinState
from mammamiradio.core.models import HostPersonality, Segment, SegmentType, StationState, Track
from mammamiradio.home.authorization import HomeAuthorization
from mammamiradio.home.context_director import DirectorObservation, HomeContextDirector, PromptFact
from mammamiradio.home.ha_context import HomeContext
from mammamiradio.scheduling.producer import (
    RenderedMusicTrack,
    _home_bulletin_admission_reachable,
    _home_bulletin_admission_stale_reason,
    _home_bulletin_ready,
    _mark_home_bulletin_segment_queued,
    run_producer,
)
from mammamiradio.scheduling.scheduler import next_segment_type as schedule_next_segment_type

PRODUCER_MODULE = "mammamiradio.scheduling.producer"
SCRIPTWRITER_MODULE = "mammamiradio.hosts.scriptwriter"
TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def _config(tmp_path: Path):
    config = load_config(TOML_PATH)
    config.party_mode = None
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    config.pacing.lookahead_segments = 1
    config.homeassistant.enabled = True
    config.homeassistant.context_enabled = True
    config.homeassistant.url = "http://ha.test:8123"
    config.ha_token = "test-token"
    return config


def _earned_state() -> StationState:
    track = Track(
        title="Canzone Casa",
        artist="Artista",
        duration_ms=180_000,
        spotify_id="casa-track",
        youtube_id="yt-casa-track",
    )
    state = StationState(
        playlist=[track],
        listeners_active=1,
        home_authorization=HomeAuthorization.legacy(),
    )
    state.listener_session.observe_active_count(1)
    for _ in range(3):
        state.listener_session.record_casa_music_audible()
    state.segments_produced = 1
    return state


def _fact() -> PromptFact:
    return PromptFact(
        fact_id="fact-casa",
        entity_id="sensor.cucina_temperatura",
        topic_key="temperature:cucina",
        fingerprint="cucina:21.5",
        prompt="In cucina ci sono ventuno gradi e mezzo.",
        policy_revision=0,
    )


async def _run_until_queued(
    queue: asyncio.Queue[Segment],
    state: StationState,
    config,
    *,
    timeout: float = 3.0,
) -> None:
    task = asyncio.create_task(run_producer(queue, state, config))
    try:
        async with asyncio.timeout(timeout):
            while queue.empty():
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_home_bulletin_readiness_is_pure_and_requires_every_dependency(tmp_path):
    state = _earned_state()
    config = _config(tmp_path)
    director = MagicMock()
    director.has_eligible_casual_fact.return_value = True
    state.home_context_director = director
    before = (state.listener_session.casa_state, state.listener_session.casa_music_count)

    with patch(f"{SCRIPTWRITER_MODULE}.home_bulletin_provider_ready", return_value=True):
        assert _home_bulletin_ready(state, config)
        assert _home_bulletin_ready(state, config)

    assert (state.listener_session.casa_state, state.listener_session.casa_music_count) == before
    director.select.assert_not_called()
    director.has_eligible_casual_fact.assert_called()

    director.reset_mock()
    with patch(f"{SCRIPTWRITER_MODULE}.home_bulletin_provider_ready", return_value=False):
        assert not _home_bulletin_ready(state, config)
    director.has_eligible_casual_fact.assert_not_called()


def test_home_bulletin_admission_requires_current_build_claim():
    state = _earned_state()
    epoch = state.listener_session.claim_casa()
    assert epoch is not None
    segment = Segment(
        type=SegmentType.HOME_BULLETIN,
        path=Path("/tmp/casa.mp3"),
        metadata={"listener_session_cue": "casa", "listener_session_epoch": epoch},
    )

    assert _home_bulletin_admission_stale_reason(state, segment) is None
    _mark_home_bulletin_segment_queued(state, segment)
    assert state.listener_session.casa_state is CasaBulletinState.QUEUED
    assert _home_bulletin_admission_stale_reason(state, segment) is not None


def test_home_bulletin_admission_is_reachable_until_bounded_queue_is_full(tmp_path):
    state = _earned_state()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=3)
    runway = tmp_path / "short-song.mp3"
    runway.write_bytes(b"runway")
    queue.put_nowait(Segment(type=SegmentType.MUSIC, path=runway, duration_sec=180.0))

    assert _home_bulletin_admission_reachable(queue, state) is True

    queue.put_nowait(Segment(type=SegmentType.MUSIC, path=runway, duration_sec=180.0))
    queue.put_nowait(Segment(type=SegmentType.MUSIC, path=runway, duration_sec=180.0))
    assert _home_bulletin_admission_reachable(queue, state) is False

    epoch = state.listener_session.claim_casa()
    assert epoch is not None
    assert _home_bulletin_admission_reachable(queue, state) is False


@pytest.mark.parametrize("lookahead_segments", [1, 2])
@pytest.mark.asyncio
async def test_casa_is_not_starved_when_short_music_restores_runway_floor(tmp_path, lookahead_segments):
    """An earned Casa slot gets a reachable queue slot below/at the floor.

    With lookahead=1 or 2, a single 180-second song leaves producer capacity.
    The old governor replaced Casa with another short song, reached the 240s
    floor, then idled before Casa could ever be selected.
    """

    state = _earned_state()
    config = _config(tmp_path)
    config.pacing.lookahead_segments = lookahead_segments
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=lookahead_segments + 2)
    runway = tmp_path / "short-song.mp3"
    runway.write_bytes(b"runway")
    queue.put_nowait(
        Segment(
            type=SegmentType.MUSIC,
            path=runway,
            duration_sec=180.0,
            metadata={"title": "Canzone breve"},
            ephemeral=False,
        )
    )
    state.now_streaming = {"type": "music", "metadata": {"title": "Canzone breve"}}
    director = MagicMock()
    director.has_eligible_casual_fact.return_value = True
    director.select.return_value = _fact()
    director.reserve_by_id.return_value = True
    state.home_context_director = director
    host: HostPersonality = config.hosts[0]
    text = "Il Bollettino di Casa. " + " ".join(["casa"] * 72)
    imaging = MagicMock()

    async def _synthesize(_text, _voice, output_path: Path, **_kwargs) -> Path:
        output_path.write_bytes(b"voice")
        return output_path

    async def _talk_bed(audio_path: Path, *_args, **_kwargs) -> Path:
        return audio_path

    def _home_sting(output_path: Path) -> Path:
        output_path.write_bytes(b"home motif")
        return output_path

    def _transition_sting(_previous, _current, output_path: Path) -> Path:
        output_path.write_bytes(b"transition")
        return output_path

    def _concat(paths: list[Path], output_path: Path, *_args, **_kwargs) -> Path:
        output_path.write_bytes(b"mixed")
        return output_path

    imaging.pick_home_bulletin_sting.side_effect = _home_sting
    imaging.pick_stinger.side_effect = _transition_sting

    async def _write_home_bulletin(*_args, submission_guard, **_kwargs):
        assert submission_guard() is True
        return host, text

    with (
        patch(f"{PRODUCER_MODULE}.RUNWAY_FLOOR_SECONDS", 240),
        patch(f"{SCRIPTWRITER_MODULE}.home_bulletin_provider_ready", return_value=True),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_home_bulletin",
            new_callable=AsyncMock,
            side_effect=_write_home_bulletin,
        ),
        patch(
            f"{PRODUCER_MODULE}._HAContextRefreshCoordinator.prepare_for_segment",
            new_callable=AsyncMock,
            return_value=(HomeContext(), False),
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", side_effect=_synthesize),
        patch(f"{PRODUCER_MODULE}._apply_talk_bed", side_effect=_talk_bed),
        patch(f"{PRODUCER_MODULE}._make_imaging_lib", return_value=imaging),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_concat),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio"),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=38.0),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            async with asyncio.timeout(3.0):
                while not any(segment.type is SegmentType.HOME_BULLETIN for segment in list(queue._queue)):
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    queued = list(queue._queue)
    assert [segment.type for segment in queued] == [SegmentType.MUSIC, SegmentType.HOME_BULLETIN]
    assert state.listener_session.casa_state is CasaBulletinState.QUEUED


@pytest.mark.asyncio
async def test_casa_fallback_is_audible_in_assetless_container(tmp_path):
    """Casa failure still reaches the branded sweeper when no assets/cache exist."""

    state = _earned_state()
    config = _config(tmp_path / "tmp")
    config.cache_dir = tmp_path / "empty-cache"
    config.cache_dir.mkdir()
    empty_assets = tmp_path / "empty-assets"
    (empty_assets / "recovery").mkdir(parents=True)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=3)
    director = MagicMock()
    director.has_eligible_casual_fact.return_value = True
    director.select.return_value = _fact()
    state.home_context_director = director
    recovery_path = tmp_path / "recovery-sweeper.mp3"
    recovery_path.write_bytes(b"non-silent branded recovery audio")
    recovery = Segment(
        type=SegmentType.SWEEPER,
        path=recovery_path,
        duration_sec=22.0,
        metadata={"type": "sweeper", "title": "Recovery sweeper", "error_recovery": True, "rescue": True},
    )
    segment_types = iter((SegmentType.HOME_BULLETIN, SegmentType.MUSIC))

    async def _writer_fails(*_args, **_kwargs):
        raise RuntimeError("Casa provider unavailable")

    with (
        patch(f"{PRODUCER_MODULE}._DEMO_ASSETS_DIR", empty_assets),
        patch(
            f"{PRODUCER_MODULE}.next_segment_type",
            side_effect=lambda *_args, **_kwargs: next(segment_types, SegmentType.MUSIC),
        ),
        patch(f"{SCRIPTWRITER_MODULE}.home_bulletin_provider_ready", return_value=True),
        patch(f"{SCRIPTWRITER_MODULE}.write_home_bulletin", new_callable=AsyncMock, side_effect=_writer_fails),
        patch(
            f"{PRODUCER_MODULE}._HAContextRefreshCoordinator.prepare_for_segment",
            new_callable=AsyncMock,
            return_value=(HomeContext(), False),
        ),
        patch(f"{PRODUCER_MODULE}._render_music_track", new_callable=AsyncMock, side_effect=RuntimeError("no music")),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
        patch(f"{PRODUCER_MODULE}.select_norm_cache_rescue", return_value=None),
        patch(
            f"{PRODUCER_MODULE}._build_recovery_sweeper_segment",
            new_callable=AsyncMock,
            return_value=recovery,
        ) as build,
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=22.0),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            async with asyncio.timeout(3.0):
                while queue.empty():
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    queued = queue.get_nowait()
    assert queued.type is SegmentType.SWEEPER
    assert queued.metadata["error_recovery"] is True
    assert queued.metadata["rescue"] is True
    assert queued.path == recovery_path
    assert queued.path.read_bytes()
    build.assert_awaited()


@pytest.mark.asyncio
async def test_casa_delivery_after_persisted_stop_resumes_with_audio(tmp_path):
    """Scenario 3: a watchdog-restart stop marker cannot leave a resumed listener silent."""
    track = Track(
        title="Canzone dopo il riavvio",
        artist="Artista",
        duration_ms=180_000,
        spotify_id="restart-track",
    )
    state = StationState(playlist=[track], listeners_active=1, session_stopped=True)
    config = _config(tmp_path)
    config.homeassistant.enabled = False
    stop_flag = config.cache_dir / "session_stopped.flag"
    stop_flag.touch()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=3)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}._pick_recovery_clip", return_value=None),
        patch(f"{PRODUCER_MODULE}.select_norm_cache_rescue", return_value=None),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=2.0),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.05)
            assert queue.empty()
            assert stop_flag.exists()

            state.session_stopped = False
            stop_flag.unlink()
            state.resume_event.set()
            async with asyncio.timeout(3.0):
                while queue.empty():
                    await asyncio.sleep(0.01)
            resumed = queue.get_nowait()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert resumed.metadata.get("resume_bridge") is True
    assert resumed.metadata.get("rescue") is True
    assert resumed.metadata.get("audio_source") == "emergency_tone"
    assert resumed.duration_sec > 0


@pytest.mark.asyncio
async def test_home_bulletin_queues_only_after_final_mixed_quality_gate(tmp_path):
    state = _earned_state()
    config = _config(tmp_path)
    state.now_streaming = {"type": "music", "metadata": {"title": "Canzone Casa"}}
    director = MagicMock()
    director.has_eligible_casual_fact.return_value = True
    director.select.return_value = _fact()
    director.reserve_by_id.return_value = True
    state.home_context_director = director
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=1)
    host: HostPersonality = config.hosts[0]
    text = "Il Bollettino di Casa. " + " ".join(["casa"] * 72)
    imaging = MagicMock()
    concat_calls: list[tuple[list[Path], Path]] = []
    quality_calls: list[tuple[Path, SegmentType]] = []
    submission_guard_values: list[bool] = []

    async def _synthesize(_text, _voice, output_path: Path, **_kwargs) -> Path:
        output_path.write_bytes(b"voice")
        return output_path

    async def _talk_bed(audio_path: Path, *_args, **_kwargs) -> Path:
        return audio_path

    def _home_sting(output_path: Path) -> Path:
        output_path.write_bytes(b"home motif")
        return output_path

    def _transition_sting(_previous, _current, output_path: Path) -> Path:
        output_path.write_bytes(b"transition")
        return output_path

    def _concat(paths: list[Path], output_path: Path, *_args, **_kwargs) -> Path:
        concat_calls.append((list(paths), output_path))
        output_path.write_bytes(b"mixed")
        return output_path

    def _quality(path: Path, segment_type: SegmentType, **_kwargs) -> None:
        quality_calls.append((path, segment_type))

    async def _write_home_bulletin(*_args, submission_guard, **_kwargs):
        submission_guard_values.append(submission_guard())
        return host, text

    imaging.pick_home_bulletin_sting.side_effect = _home_sting
    imaging.pick_stinger.side_effect = _transition_sting

    with (
        patch(f"{PRODUCER_MODULE}.RUNWAY_FLOOR_SECONDS", 0),
        patch(f"{SCRIPTWRITER_MODULE}.home_bulletin_provider_ready", return_value=True),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_home_bulletin",
            new_callable=AsyncMock,
            side_effect=_write_home_bulletin,
        ) as writer,
        patch(
            f"{PRODUCER_MODULE}._HAContextRefreshCoordinator.prepare_for_segment",
            new_callable=AsyncMock,
            return_value=(HomeContext(), False),
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", side_effect=_synthesize),
        patch(f"{PRODUCER_MODULE}._apply_talk_bed", side_effect=_talk_bed),
        patch(f"{PRODUCER_MODULE}._make_imaging_lib", return_value=imaging),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_concat),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", side_effect=_quality),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=38.0),
    ):
        await _run_until_queued(queue, state, config)

    segment = queue.get_nowait()
    assert segment.type is SegmentType.HOME_BULLETIN
    assert segment.metadata["title"] == "Il Bollettino di Casa"
    assert segment.metadata["home_fact_id"] == "fact-casa"
    assert segment.metadata["listener_session_epoch"] == state.listener_session.epoch
    assert segment.metadata["listener_session_cue"] == "casa"
    assert segment.metadata["home_context_generation"] == state.home_context_policy_generation
    assert state.listener_session.casa_state is CasaBulletinState.QUEUED
    assert state.segments_produced == 2

    writer.assert_awaited_once()
    assert writer.await_args.kwargs["prompt_fact"] == _fact()
    assert submission_guard_values == [True]
    director.select.assert_called_once_with(lane="casual")
    director.reserve_by_id.assert_called_once_with(segment.metadata["queue_id"], "fact-casa")
    assert len(concat_calls) == 2  # dedicated Casa motif, then normal transition sting
    assert concat_calls[0][0][0].name.startswith("home_bulletin_motif_")
    assert quality_calls == [(segment.path, SegmentType.HOME_BULLETIN)]
    assert segment.path.name.startswith("segment_with_sting_")


@pytest.mark.asyncio
async def test_readiness_loss_releases_claim_then_runs_one_non_casa_cycle(tmp_path):
    state = _earned_state()
    config = _config(tmp_path)
    director = MagicMock()
    director.has_eligible_casual_fact.return_value = True
    state.home_context_director = director
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=1)
    rendered_path = tmp_path / "ordinary.mp3"
    rendered_path.write_bytes(b"music")
    selected_ready_values: list[bool] = []

    def _schedule(state_arg, pacing_arg, *, casa_ready: bool = False):
        selected_ready_values.append(casa_ready)
        return schedule_next_segment_type(state_arg, pacing_arg, casa_ready=casa_ready)

    async def _render(track: Track, *_args, **_kwargs) -> RenderedMusicTrack:
        return RenderedMusicTrack(track=track, path=rendered_path, cache_path=rendered_path, cache_hit=True)

    with (
        patch(f"{PRODUCER_MODULE}.RUNWAY_FLOOR_SECONDS", 0),
        patch(f"{PRODUCER_MODULE}.next_segment_type", side_effect=_schedule),
        patch(
            f"{SCRIPTWRITER_MODULE}.home_bulletin_provider_ready",
            side_effect=[True, False],
        ),
        patch(
            f"{PRODUCER_MODULE}._HAContextRefreshCoordinator.prepare_for_segment",
            new_callable=AsyncMock,
            return_value=(HomeContext(), False),
        ),
        patch(f"{PRODUCER_MODULE}._render_music_track", side_effect=_render),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio"),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=180.0),
    ):
        await _run_until_queued(queue, state, config)

    segment = queue.get_nowait()
    assert segment.type is SegmentType.MUSIC
    assert selected_ready_values == [True, False]
    assert state.listener_session.casa_state is CasaBulletinState.EARNED
    director.select.assert_not_called()


@pytest.mark.asyncio
async def test_cancellation_during_egress_releases_casa_and_home_fact_reservation(tmp_path):
    state = _earned_state()
    config = _config(tmp_path)
    director = HomeContextDirector(id_factory=lambda: "fact-cancel")
    director.observe(
        [
            DirectorObservation(
                entity_id="weather.forecast_home",
                domain="weather",
                state="sunny",
                score=9.0,
                temperature_c=22.0,
            )
        ],
        policy_revision=0,
    )
    state.home_context_director = director
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=1)
    host: HostPersonality = config.hosts[0]
    egress_started = asyncio.Event()
    never_finish = asyncio.Event()

    async def _synthesize(_text, _voice, output_path: Path, **_kwargs) -> Path:
        output_path.write_bytes(b"voice")
        return output_path

    async def _talk_bed(audio_path: Path, *_args, **_kwargs) -> Path:
        return audio_path

    def _home_sting(output_path: Path) -> Path:
        output_path.write_bytes(b"motif")
        return output_path

    def _concat(_paths: list[Path], output_path: Path, *_args, **_kwargs) -> Path:
        output_path.write_bytes(b"mixed")
        return output_path

    async def _blocking_egress(_segment: Segment, _config):
        egress_started.set()
        await never_finish.wait()

    imaging = MagicMock()
    imaging.pick_home_bulletin_sting.side_effect = _home_sting

    with (
        patch(f"{PRODUCER_MODULE}.RUNWAY_FLOOR_SECONDS", 0),
        patch(f"{SCRIPTWRITER_MODULE}.home_bulletin_provider_ready", return_value=True),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_home_bulletin",
            new_callable=AsyncMock,
            return_value=(host, "Il Bollettino di Casa. " + " ".join(["casa"] * 72)),
        ),
        patch(
            f"{PRODUCER_MODULE}._HAContextRefreshCoordinator.prepare_for_segment",
            new_callable=AsyncMock,
            return_value=(HomeContext(), False),
        ),
        patch(f"{PRODUCER_MODULE}._observe_home_context_director"),
        patch(f"{PRODUCER_MODULE}.synthesize", side_effect=_synthesize),
        patch(f"{PRODUCER_MODULE}._apply_talk_bed", side_effect=_talk_bed),
        patch(f"{PRODUCER_MODULE}._make_imaging_lib", return_value=imaging),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_concat),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio"),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=38.0),
        patch(f"{PRODUCER_MODULE}._apply_egress", side_effect=_blocking_egress),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        await asyncio.wait_for(egress_started.wait(), timeout=3.0)
        assert director.admin_status()["reserved_count"] == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert state.listener_session.casa_state is CasaBulletinState.EARNED
    assert director.admin_status()["reserved_count"] == 0
    assert queue.empty()


@pytest.mark.asyncio
async def test_casa_audio_tool_failure_releases_and_runs_one_ordinary_cycle(tmp_path):
    state = _earned_state()
    config = _config(tmp_path)
    director = HomeContextDirector(id_factory=lambda: "fact-quality-tool")
    director.observe(
        [
            DirectorObservation(
                entity_id="weather.forecast_home",
                domain="weather",
                state="sunny",
                score=9.0,
                temperature_c=22.0,
            )
        ],
        policy_revision=0,
    )
    state.home_context_director = director
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=1)
    host: HostPersonality = config.hosts[0]
    rendered_path = tmp_path / "ordinary-after-quality-tool.mp3"
    rendered_path.write_bytes(b"music")
    selected_ready_values: list[bool] = []

    def _schedule(state_arg, pacing_arg, *, casa_ready: bool = False):
        selected_ready_values.append(casa_ready)
        return schedule_next_segment_type(state_arg, pacing_arg, casa_ready=casa_ready)

    async def _render(track: Track, *_args, **_kwargs) -> RenderedMusicTrack:
        return RenderedMusicTrack(track=track, path=rendered_path, cache_path=rendered_path, cache_hit=True)

    async def _synthesize(_text, _voice, output_path: Path, **_kwargs) -> Path:
        output_path.write_bytes(b"voice")
        return output_path

    async def _talk_bed(audio_path: Path, *_args, **_kwargs) -> Path:
        return audio_path

    def _home_sting(output_path: Path) -> Path:
        output_path.write_bytes(b"motif")
        return output_path

    def _concat(_paths: list[Path], output_path: Path, *_args, **_kwargs) -> Path:
        output_path.write_bytes(b"mixed")
        return output_path

    def _quality(_path: Path, segment_type: SegmentType, **_kwargs) -> None:
        if segment_type is SegmentType.HOME_BULLETIN:
            raise AudioToolError("ffprobe timed out")

    imaging = MagicMock()
    imaging.pick_home_bulletin_sting.side_effect = _home_sting

    with (
        patch(f"{PRODUCER_MODULE}.RUNWAY_FLOOR_SECONDS", 0),
        patch(f"{PRODUCER_MODULE}.next_segment_type", side_effect=_schedule),
        patch(f"{SCRIPTWRITER_MODULE}.home_bulletin_provider_ready", return_value=True),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_home_bulletin",
            new_callable=AsyncMock,
            return_value=(host, "Il Bollettino di Casa. " + " ".join(["casa"] * 72)),
        ),
        patch(
            f"{PRODUCER_MODULE}._HAContextRefreshCoordinator.prepare_for_segment",
            new_callable=AsyncMock,
            return_value=(HomeContext(), False),
        ),
        patch(f"{PRODUCER_MODULE}._observe_home_context_director"),
        patch(f"{PRODUCER_MODULE}.synthesize", side_effect=_synthesize),
        patch(f"{PRODUCER_MODULE}._apply_talk_bed", side_effect=_talk_bed),
        patch(f"{PRODUCER_MODULE}._make_imaging_lib", return_value=imaging),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_concat),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", side_effect=_quality),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=38.0),
        patch(f"{PRODUCER_MODULE}._render_music_track", side_effect=_render),
    ):
        await _run_until_queued(queue, state, config)

    assert queue.get_nowait().type is SegmentType.MUSIC
    assert selected_ready_values == [True, False]
    assert state.listener_session.casa_state is CasaBulletinState.EARNED
    assert director.admin_status()["reserved_count"] == 0
