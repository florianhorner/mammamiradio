"""Producer regressions for fail-closed required speech."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mammamiradio.audio.tts import TTSUnavailableError
from mammamiradio.core.config import load_config
from mammamiradio.core.models import ChaosSubtype, Segment, SegmentType, StationState, Track
from mammamiradio.home.authorization import HomeAuthorization
from mammamiradio.hosts.ad_creative import AdPart, AdScript, SonicWorld
from mammamiradio.scheduling import producer

PRODUCER_MODULE = "mammamiradio.scheduling.producer"
SCRIPTWRITER_MODULE = "mammamiradio.hosts.scriptwriter"
TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


@pytest.fixture(autouse=True)
def _clean_producer_globals():
    old_runway_floor = producer.RUNWAY_FLOOR_SECONDS
    producer.RUNWAY_FLOOR_SECONDS = 0
    yield
    producer._last_music_file = None
    producer._canned_clip_cache.clear()
    producer._recently_played_clips.clear()
    producer.RUNWAY_FLOOR_SECONDS = old_runway_floor


def _make_state() -> StationState:
    return StationState(
        playlist=[Track(title="Canzone", artist="Artista", duration_ms=180_000, spotify_id="demo")],
        listeners_active=1,
        home_authorization=HomeAuthorization.legacy(),
    )


def _make_config(tmp_path: Path):
    config = load_config(TOML_PATH)
    config.party_mode = None
    config.pacing.lookahead_segments = 1
    config.homeassistant.enabled = False
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    return config


def _recovery_segment(tmp_path: Path) -> Segment:
    path = tmp_path / "recovery.mp3"
    path.write_bytes(b"recovery")
    return Segment(
        type=SegmentType.SWEEPER,
        path=path,
        metadata={"type": "sweeper", "rescue": True, "error_recovery": True, "title": "Recovery sweeper"},
        ephemeral=False,
    )


async def _wait_for_queue(queue: asyncio.Queue[Segment], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while queue.empty():
            await asyncio.sleep(0.01)


async def _wait_for_thread_event(event: threading.Event, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not event.is_set():
            await asyncio.sleep(0.001)


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    ("seg_type", "expected"),
    [
        (SegmentType.AD, {"songs_since_ad": 0}),
        (SegmentType.NEWS_FLASH, {"songs_since_news": 0, "songs_since_banter": 0}),
        (SegmentType.BANTER, {"songs_since_banter": 0}),
        (SegmentType.STATION_ID, {"segments_since_station_id": 0}),
        (SegmentType.TIME_CHECK, {"segments_since_time_check": 0}),
        (SegmentType.SWEEPER, {"songs_since_ad": 9, "segments_since_time_check": 9}),
    ],
)
def test_tts_failure_counter_reset_is_narrow(seg_type: SegmentType, expected: dict[str, int]) -> None:
    state = StationState(
        songs_since_ad=9,
        songs_since_news=9,
        songs_since_banter=9,
        segments_since_station_id=9,
        segments_since_time_check=9,
    )

    producer._reset_due_counters_after_tts_failure(state, seg_type)

    for field, value in expected.items():
        assert getattr(state, field) == value


@pytest.mark.asyncio
async def test_banter_tts_failure_waits_for_sibling_then_cleans_and_queues_recovery(tmp_path: Path) -> None:
    state = _make_state()
    state.songs_since_banter = 7
    config = _make_config(tmp_path)
    config.anthropic_api_key = "test-key"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    recovery = _recovery_segment(tmp_path)
    dialogue_started = asyncio.Event()
    release_dialogue = asyncio.Event()
    late_dialogue = tmp_path / "late_dialogue.mp3"

    async def _late_dialogue(*_args, **_kwargs) -> Path:
        dialogue_started.set()
        await release_dialogue.wait()
        late_dialogue.write_bytes(b"late dialogue")
        return late_dialogue

    with (
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition",
            new_callable=AsyncMock,
            return_value=(host, "Allora.", None),
        ),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            return_value=([(host, "Restiamo in onda.")], None),
        ),
        patch(
            f"{PRODUCER_MODULE}.synthesize",
            new_callable=AsyncMock,
            side_effect=TTSUnavailableError("no voice route"),
        ),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, side_effect=_late_dialogue),
        patch(
            f"{PRODUCER_MODULE}._producer_error_recovery_segment",
            new_callable=AsyncMock,
            return_value=recovery,
        ),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
    ):
        task = asyncio.create_task(producer.run_producer(queue, state, config))
        await asyncio.wait_for(dialogue_started.wait(), timeout=2.0)
        await asyncio.sleep(0)
        assert queue.empty(), "recovery must wait until the sibling renderer has settled"
        release_dialogue.set()
        await _wait_for_queue(queue)
        await _cancel(task)

    queued = queue.get_nowait()
    assert queued is recovery
    assert state.songs_since_banter == 0
    assert state.segments_produced == 0
    assert not late_dialogue.exists(), "a completed sibling output must be removed before recovery"
    assert not list(tmp_path.glob("trans_*.mp3"))
    assert not list(tmp_path.glob("banter_trans_*.mp3"))


@pytest.mark.asyncio
async def test_banter_cancellation_waits_for_sibling_before_scratch_cleanup(tmp_path: Path) -> None:
    state = _make_state()
    config = _make_config(tmp_path)
    config.anthropic_api_key = "test-key"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    dialogue_started = asyncio.Event()
    release_dialogue = asyncio.Event()
    late_dialogue = tmp_path / "cancelled_late_dialogue.mp3"

    async def _late_dialogue(*_args, **_kwargs) -> Path:
        dialogue_started.set()
        await release_dialogue.wait()
        late_dialogue.write_bytes(b"late dialogue")
        return late_dialogue

    with (
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition",
            new_callable=AsyncMock,
            return_value=(host, "Allora.", None),
        ),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            return_value=([(host, "Restiamo in onda.")], None),
        ),
        patch(
            f"{PRODUCER_MODULE}.synthesize",
            new_callable=AsyncMock,
            side_effect=TTSUnavailableError("no voice route"),
        ),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, side_effect=_late_dialogue),
        patch(
            f"{PRODUCER_MODULE}._producer_error_recovery_segment",
            new_callable=AsyncMock,
        ) as recovery,
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
    ):
        task = asyncio.create_task(producer.run_producer(queue, state, config))
        await asyncio.wait_for(dialogue_started.wait(), timeout=2.0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancellation must wait for the owned sibling renderer"
        release_dialogue.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    recovery.assert_not_awaited()
    assert queue.empty()
    assert not late_dialogue.exists()
    assert not list(tmp_path.glob("trans_*.mp3"))
    assert not list(tmp_path.glob("banter_trans_*.mp3"))


@pytest.mark.parametrize(
    ("seg_type", "counter_fields"),
    [
        (SegmentType.NEWS_FLASH, ("songs_since_news", "songs_since_banter")),
        (SegmentType.STATION_ID, ("segments_since_station_id",)),
        (SegmentType.SWEEPER, ()),
        (SegmentType.TIME_CHECK, ("segments_since_time_check",)),
    ],
)
@pytest.mark.asyncio
async def test_required_imaging_voice_failure_uses_recovery_not_local_bed(
    tmp_path: Path,
    seg_type: SegmentType,
    counter_fields: tuple[str, ...],
) -> None:
    state = _make_state()
    state.songs_since_news = 8
    state.songs_since_banter = 8
    state.segments_since_station_id = 8
    state.segments_since_time_check = 8
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    recovery = _recovery_segment(tmp_path)
    host = config.hosts[0]

    def _write_local_layer(path: Path, *_args, **_kwargs) -> Path:
        time.sleep(0.01)
        path.write_bytes(b"local layer")
        return path

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=seg_type),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_news_flash",
            new_callable=AsyncMock,
            return_value=(host, "Aggiornamento.", "traffic"),
        ),
        patch(
            f"{PRODUCER_MODULE}.synthesize",
            new_callable=AsyncMock,
            side_effect=TTSUnavailableError("no voice route"),
        ),
        patch(f"{PRODUCER_MODULE}.generate_station_id_bed", side_effect=_write_local_layer),
        patch(f"{PRODUCER_MODULE}.generate_tone", side_effect=_write_local_layer),
        patch(
            f"{PRODUCER_MODULE}._producer_error_recovery_segment",
            new_callable=AsyncMock,
            return_value=recovery,
        ),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
    ):
        task = asyncio.create_task(producer.run_producer(queue, state, config))
        await _wait_for_queue(queue)
        await _cancel(task)

    assert queue.get_nowait() is recovery
    assert state.segments_produced == 0
    for field in counter_fields:
        assert getattr(state, field) == 0
    assert not list(tmp_path.glob("stid_sting_*.mp3"))
    assert not list(tmp_path.glob("time_chime_*.mp3"))


def _ad_fixture(config):
    brand = config.ads.brands[0]
    voice = config.ads.voices[0]
    role = voice.role or "announcer"
    voice_map = {role: voice}
    script = AdScript(
        brand=brand.name,
        parts=[AdPart(type="voice", text="Compra subito.", role=role)],
        summary="Promo",
        format="classic_pitch",
        sonic=SonicWorld(),
        roles_used=[role],
    )
    return brand, voice_map, script


def _write_layer(path: Path, *_args, **_kwargs) -> Path:
    path.write_bytes(b"audio")
    return path


def _write_concat(_parts, path: Path, *_args, **_kwargs) -> Path:
    path.write_bytes(b"joined")
    return path


@pytest.mark.asyncio
async def test_banter_final_concat_cancellation_waits_before_cleanup(tmp_path: Path) -> None:
    state = _make_state()
    config = _make_config(tmp_path)
    config.anthropic_api_key = "test-key"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    concat_started = threading.Event()
    release_concat = threading.Event()

    async def _voice(_text: str, _voice: str, path: Path, **_kwargs) -> None:
        path.write_bytes(b"voice")

    async def _dialogue(_lines, _tmp_dir: Path, **_kwargs) -> Path:
        path = tmp_path / "dialogue_result.mp3"
        path.write_bytes(b"dialogue")
        return path

    def _slow_concat(_parts, path: Path, *_args, **_kwargs) -> Path:
        concat_started.set()
        assert release_concat.wait(timeout=2.0)
        path.write_bytes(b"joined")
        return path

    with (
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition",
            new_callable=AsyncMock,
            return_value=(host, "Allora.", None),
        ),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            return_value=([(host, "Restiamo in onda.")], None),
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=_voice),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, side_effect=_dialogue),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, side_effect=lambda path, *_a: path),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_slow_concat),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
    ):
        task = asyncio.create_task(producer.run_producer(queue, state, config))
        await _wait_for_thread_event(concat_started)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancellation must wait for final banter concat"
        release_concat.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert not list(tmp_path.glob("banter_full_*.mp3"))
    assert not list(tmp_path.glob("trans_*.mp3"))
    assert not (tmp_path / "dialogue_result.mp3").exists()


@pytest.mark.asyncio
async def test_station_id_final_mix_cancellation_waits_before_cleanup(tmp_path: Path) -> None:
    state = _make_state()
    state.segments_since_station_id = 9
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    mix_started = threading.Event()
    release_mix = threading.Event()

    async def _voice(_text: str, _voice: str, path: Path, **_kwargs) -> None:
        path.write_bytes(b"voice")

    def _slow_mix(_voice_path: Path, _sting_path: Path, output_path: Path) -> Path:
        mix_started.set()
        assert release_mix.wait(timeout=2.0)
        output_path.write_bytes(b"mixed")
        return output_path

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.STATION_ID),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=_voice),
        patch(f"{PRODUCER_MODULE}.generate_station_id_bed", side_effect=_write_layer),
        patch(f"{PRODUCER_MODULE}.mix_voice_with_sting", side_effect=_slow_mix),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
    ):
        task = asyncio.create_task(producer.run_producer(queue, state, config))
        await _wait_for_thread_event(mix_started)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancellation must wait for final station ID mix"
        release_mix.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert not list(tmp_path.glob("stid_*.mp3"))


@pytest.mark.asyncio
async def test_time_check_final_concat_cancellation_waits_before_cleanup(tmp_path: Path) -> None:
    state = _make_state()
    state.segments_since_time_check = 9
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    concat_started = threading.Event()
    release_concat = threading.Event()

    async def _voice(_text: str, _voice: str, path: Path, **_kwargs) -> None:
        path.write_bytes(b"voice")

    def _slow_concat(_parts, path: Path, *_args, **_kwargs) -> Path:
        concat_started.set()
        assert release_concat.wait(timeout=2.0)
        path.write_bytes(b"joined")
        return path

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.TIME_CHECK),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=_voice),
        patch(f"{PRODUCER_MODULE}.generate_tone", side_effect=_write_layer),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_slow_concat),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
    ):
        task = asyncio.create_task(producer.run_producer(queue, state, config))
        await _wait_for_thread_event(concat_started)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancellation must wait for final time-check concat"
        release_concat.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert not list(tmp_path.glob("time_*.mp3"))


@pytest.mark.asyncio
async def test_ad_final_concat_cancellation_waits_before_cleanup(tmp_path: Path) -> None:
    state = _make_state()
    state.songs_since_ad = 9
    config = _make_config(tmp_path)
    config.pacing.ad_spots_per_break = 1
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    brand, voice_map, script = _ad_fixture(config)
    concat_started = threading.Event()
    release_concat = threading.Event()

    async def _voice(_text: str, _voice: str, path: Path, **_kwargs) -> None:
        path.write_bytes(b"voice")

    async def _ad_voice(*_args, **_kwargs) -> Path:
        path = tmp_path / "required_ad_voice.mp3"
        path.write_bytes(b"ad voice")
        return path

    def _slow_concat(_parts, path: Path, *_args, **_kwargs) -> Path:
        concat_started.set()
        assert release_concat.wait(timeout=2.0)
        path.write_bytes(b"joined")
        return path

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.AD),
        patch(f"{PRODUCER_MODULE}._select_safe_ad_spot", return_value=(brand, script.format, script.sonic, voice_map)),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition",
            new_callable=AsyncMock,
            return_value=(host, "Pubblicita.", None),
        ),
        patch(f"{SCRIPTWRITER_MODULE}.write_ad", new_callable=AsyncMock, return_value=script),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=_voice),
        patch(f"{PRODUCER_MODULE}.synthesize_ad", new_callable=AsyncMock, side_effect=_ad_voice),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, side_effect=lambda path, *_a: path),
        patch(f"{PRODUCER_MODULE}.generate_bumper_jingle", side_effect=_write_layer),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_slow_concat),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
    ):
        task = asyncio.create_task(producer.run_producer(queue, state, config))
        await _wait_for_thread_event(concat_started)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancellation must wait for final ad concat"
        release_concat.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert not list(tmp_path.glob("adbreak_*.mp3"))
    assert not list(tmp_path.glob("ad_intro_*.mp3"))
    assert not list(tmp_path.glob("bumper_*.mp3"))
    assert not list(tmp_path.glob("ad_outro_*.mp3"))
    assert not (tmp_path / "required_ad_voice.mp3").exists()


@pytest.mark.asyncio
async def test_optional_promo_tts_failure_does_not_drop_complete_ad(tmp_path: Path) -> None:
    state = _make_state()
    state.songs_since_ad = 9
    config = _make_config(tmp_path)
    config.pacing.ad_spots_per_break = 1
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    brand, voice_map, script = _ad_fixture(config)

    async def _voice(text: str, _voice: str, path: Path, **kwargs) -> None:
        if kwargs.get("rate") == "+40%":
            path.write_bytes(b"partial promo")
            raise TTSUnavailableError("optional promo unavailable")
        path.write_bytes(text.encode())

    async def _ad_voice(*_args, **_kwargs) -> Path:
        path = tmp_path / "required_ad_voice.mp3"
        path.write_bytes(b"required ad voice")
        return path

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.AD),
        patch(f"{PRODUCER_MODULE}._select_safe_ad_spot", return_value=(brand, script.format, script.sonic, voice_map)),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition",
            new_callable=AsyncMock,
            return_value=(host, "Pubblicita.", None),
        ),
        patch(f"{SCRIPTWRITER_MODULE}.write_ad", new_callable=AsyncMock, return_value=script),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=_voice),
        patch(f"{PRODUCER_MODULE}.synthesize_ad", new_callable=AsyncMock, side_effect=_ad_voice),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, side_effect=lambda path, *_a: path),
        patch(f"{PRODUCER_MODULE}.generate_bumper_jingle", side_effect=_write_layer),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_write_concat),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=4.0),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
    ):
        task = asyncio.create_task(producer.run_producer(queue, state, config))
        await _wait_for_queue(queue)
        await _cancel(task)

    queued = queue.get_nowait()
    assert queued.type == SegmentType.AD
    assert state.songs_since_ad == 0
    assert not list(tmp_path.glob("promo_tag_*.mp3"))


@pytest.mark.asyncio
async def test_required_ad_voice_failure_uses_recovery_and_resets_due_counter(tmp_path: Path) -> None:
    state = _make_state()
    state.songs_since_ad = 9
    config = _make_config(tmp_path)
    config.pacing.ad_spots_per_break = 1
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    recovery = _recovery_segment(tmp_path)
    host = config.hosts[0]
    brand, voice_map, script = _ad_fixture(config)

    async def _voice(text: str, _voice: str, path: Path, **_kwargs) -> None:
        path.write_bytes(text.encode())

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.AD),
        patch(f"{PRODUCER_MODULE}._select_safe_ad_spot", return_value=(brand, script.format, script.sonic, voice_map)),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition",
            new_callable=AsyncMock,
            return_value=(host, "Pubblicita.", None),
        ),
        patch(f"{SCRIPTWRITER_MODULE}.write_ad", new_callable=AsyncMock, return_value=script),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=_voice),
        patch(
            f"{PRODUCER_MODULE}.synthesize_ad",
            new_callable=AsyncMock,
            side_effect=TTSUnavailableError("required ad voice unavailable"),
        ),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, side_effect=lambda path, *_a: path),
        patch(f"{PRODUCER_MODULE}.generate_bumper_jingle", side_effect=_write_layer),
        patch(
            f"{PRODUCER_MODULE}._producer_error_recovery_segment",
            new_callable=AsyncMock,
            return_value=recovery,
        ),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
    ):
        task = asyncio.create_task(producer.run_producer(queue, state, config))
        await _wait_for_queue(queue)
        await _cancel(task)

    assert queue.get_nowait() is recovery
    assert state.songs_since_ad == 0
    assert state.segments_produced == 0
    assert not list(tmp_path.glob("ad_intro_*.mp3"))
    assert not list(tmp_path.glob("promo_tag_*.mp3"))
    assert not list(tmp_path.glob("bumper_*.mp3"))


@pytest.mark.asyncio
async def test_multi_spot_ad_partial_success_then_failure_cleans_up_first_spot(tmp_path: Path) -> None:
    """A later ad spot's TTS outage must clean up an earlier spot's rendered audio too."""
    state = _make_state()
    state.songs_since_ad = 9
    config = _make_config(tmp_path)
    config.pacing.ad_spots_per_break = 2
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    recovery = _recovery_segment(tmp_path)
    host = config.hosts[0]
    brand, voice_map, script = _ad_fixture(config)

    first_spot_path = tmp_path / "ad_spot_first.mp3"
    call_count = 0

    async def _voice(text: str, _voice: str, path: Path, **_kwargs) -> None:
        path.write_bytes(text.encode())

    async def _ad_voice(*_args, **_kwargs) -> Path:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            first_spot_path.write_bytes(b"first ad spot rendered")
            return first_spot_path
        raise TTSUnavailableError("second ad spot voice unavailable")

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.AD),
        patch(f"{PRODUCER_MODULE}._select_safe_ad_spot", return_value=(brand, script.format, script.sonic, voice_map)),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition",
            new_callable=AsyncMock,
            return_value=(host, "Pubblicita.", None),
        ),
        patch(f"{SCRIPTWRITER_MODULE}.write_ad", new_callable=AsyncMock, return_value=script),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=_voice),
        patch(f"{PRODUCER_MODULE}.synthesize_ad", new_callable=AsyncMock, side_effect=_ad_voice),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, side_effect=lambda path, *_a: path),
        patch(f"{PRODUCER_MODULE}.generate_bumper_jingle", side_effect=_write_layer),
        patch(
            f"{PRODUCER_MODULE}._producer_error_recovery_segment",
            new_callable=AsyncMock,
            return_value=recovery,
        ),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
    ):
        task = asyncio.create_task(producer.run_producer(queue, state, config))
        await _wait_for_queue(queue)
        await _cancel(task)

    assert call_count == 2
    assert queue.get_nowait() is recovery
    assert state.songs_since_ad == 0
    assert not first_spot_path.exists(), "the successfully-rendered first spot must be cleaned up too"
    assert not list(tmp_path.glob("ad_intro_*.mp3"))
    assert not list(tmp_path.glob("promo_tag_*.mp3"))
    assert not list(tmp_path.glob("bumper_*.mp3"))


@pytest.mark.asyncio
async def test_tts_unavailable_recovery_sweeper_falls_through_to_emergency_tone(tmp_path: Path) -> None:
    state = _make_state()
    config = _make_config(tmp_path)

    with (
        patch(f"{PRODUCER_MODULE}._pick_recovery_clip", return_value=None),
        patch(f"{PRODUCER_MODULE}.select_norm_cache_rescue", return_value=None),
        patch(
            f"{PRODUCER_MODULE}._build_recovery_sweeper_segment",
            new_callable=AsyncMock,
            side_effect=TTSUnavailableError("no recovery voice"),
        ),
    ):
        segment = await producer._producer_error_recovery_segment(state, config)

    assert segment is not None
    assert segment.metadata["audio_source"] == "emergency_tone"
    assert segment.metadata["rescue"] is True


@pytest.mark.parametrize("canned_available", [True, False], ids=["canned", "propagate"])
@pytest.mark.asyncio
async def test_impossible_tts_unavailable_uses_canned_or_propagates_to_recovery(
    tmp_path: Path,
    canned_available: bool,
) -> None:
    state = _make_state()
    state.force_next = SegmentType.BANTER
    state.songs_since_banter = 7
    state.canned_clips_streamed = producer.SHAREWARE_CANNED_LIMIT - 1
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=1)
    canned = tmp_path / "impossible_canned.mp3"
    canned.write_bytes(b"canned")
    recovery = _recovery_segment(tmp_path)

    with (
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=False),
        patch(f"{PRODUCER_MODULE}.generate_impossible_line", return_value="Una notte impossibile."),
        patch(
            f"{PRODUCER_MODULE}._synthesize_impossible_moment",
            new_callable=AsyncMock,
            side_effect=TTSUnavailableError("impossible voice unavailable"),
        ),
        patch(
            f"{PRODUCER_MODULE}._pick_canned_clip",
            return_value=canned if canned_available else None,
        ),
        patch(
            f"{PRODUCER_MODULE}._producer_error_recovery_segment",
            new_callable=AsyncMock,
            return_value=recovery,
        ) as recovery_builder,
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
    ):
        task = asyncio.create_task(producer.run_producer(queue, state, config))
        await _wait_for_queue(queue)
        await _cancel(task)

    queued = queue.get_nowait()
    if canned_available:
        assert queued.type is SegmentType.BANTER
        assert queued.path == canned
        assert state.last_banter_script == [{"host": "Radio", "text": "(pre-recorded banter)"}]
        recovery_builder.assert_not_awaited()
    else:
        assert queued is recovery
        assert state.songs_since_banter == 0
        recovery_builder.assert_awaited_once_with(state, config)


@pytest.mark.parametrize("canned_available", [True, False], ids=["canned", "propagate"])
@pytest.mark.asyncio
async def test_chaos_tts_unavailable_uses_canned_or_propagates_to_recovery(
    tmp_path: Path,
    canned_available: bool,
) -> None:
    state = _make_state()
    state.chaos_pending = ChaosSubtype.FOURTH_WALL
    state.songs_since_banter = 7
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=1)
    canned = tmp_path / "chaos_canned.mp3"
    canned.write_bytes(b"canned")
    recovery = _recovery_segment(tmp_path)
    host = config.hosts[0]

    with (
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            return_value=([(host, "Fuori copione.")], None),
        ),
        patch(
            f"{PRODUCER_MODULE}.synthesize_dialogue",
            new_callable=AsyncMock,
            side_effect=TTSUnavailableError("chaos voice unavailable"),
        ),
        patch(
            f"{PRODUCER_MODULE}._pick_canned_clip",
            return_value=canned if canned_available else None,
        ),
        patch(
            f"{PRODUCER_MODULE}._producer_error_recovery_segment",
            new_callable=AsyncMock,
            return_value=recovery,
        ) as recovery_builder,
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
    ):
        task = asyncio.create_task(producer.run_producer(queue, state, config))
        await _wait_for_queue(queue)
        await _cancel(task)

    queued = queue.get_nowait()
    assert state.chaos_audio_failures == 1
    if canned_available:
        assert queued.type is SegmentType.BANTER
        assert queued.path == canned
        assert state.last_banter_script == [
            {
                "host": "Radio",
                "text": "(pre-recorded chaos fallback)",
                "type": "chaos_audio_fallback",
                "chaos_subtype": ChaosSubtype.FOURTH_WALL.value,
            }
        ]
        assert state.chaos_pending is None
        recovery_builder.assert_not_awaited()
    else:
        assert queued is recovery
        assert state.songs_since_banter == 0
        assert state.chaos_pending is None
        recovery_builder.assert_awaited_once_with(state, config)


async def _run_ad_wrapper_failure(
    tmp_path: Path,
    *,
    voice_side_effect,
    bumper_side_effect=_write_layer,
) -> tuple[StationState, Segment]:
    state = _make_state()
    state.force_next = SegmentType.AD
    state.songs_since_ad = 9
    config = _make_config(tmp_path)
    config.pacing.ad_spots_per_break = 1
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=1)
    recovery = _recovery_segment(tmp_path)
    host = config.hosts[0]
    brand, voice_map, script = _ad_fixture(config)

    async def _ad_voice(*_args, **_kwargs) -> Path:
        path = tmp_path / "required_ad_voice.mp3"
        path.write_bytes(b"required ad voice")
        return path

    with (
        patch(
            f"{PRODUCER_MODULE}._select_safe_ad_spot",
            return_value=(brand, script.format, script.sonic, voice_map),
        ),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition",
            new_callable=AsyncMock,
            return_value=(host, "Pubblicita.", None),
        ),
        patch(f"{SCRIPTWRITER_MODULE}.write_ad", new_callable=AsyncMock, return_value=script),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=voice_side_effect),
        patch(f"{PRODUCER_MODULE}.synthesize_ad", new_callable=AsyncMock, side_effect=_ad_voice),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, side_effect=lambda path, *_a: path),
        patch(f"{PRODUCER_MODULE}.generate_bumper_jingle", side_effect=bumper_side_effect),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_write_concat),
        patch(
            f"{PRODUCER_MODULE}._producer_error_recovery_segment",
            new_callable=AsyncMock,
            return_value=recovery,
        ),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
    ):
        task = asyncio.create_task(producer.run_producer(queue, state, config))
        await _wait_for_queue(queue)
        await _cancel(task)

    return state, queue.get_nowait()


@pytest.mark.asyncio
async def test_required_ad_intro_tts_failure_uses_recovery_and_resets_due_counter(tmp_path: Path) -> None:
    async def _voice(text: str, _voice: str, path: Path, **_kwargs) -> None:
        path.write_bytes(text.encode())
        if path.name.startswith("ad_intro_"):
            raise TTSUnavailableError("required ad intro unavailable")

    state, queued = await _run_ad_wrapper_failure(tmp_path, voice_side_effect=_voice)

    assert queued.metadata["error_recovery"] is True
    assert state.songs_since_ad == 0
    assert state.segments_produced == 0
    assert not list(tmp_path.glob("ad_intro_*.mp3"))
    assert not list(tmp_path.glob("bumper_in_*.mp3"))


@pytest.mark.asyncio
async def test_required_ad_outro_tts_failure_uses_recovery_and_resets_due_counter(tmp_path: Path) -> None:
    async def _voice(text: str, _voice: str, path: Path, **_kwargs) -> None:
        path.write_bytes(text.encode())
        if path.name.startswith("ad_outro_"):
            raise TTSUnavailableError("required ad outro unavailable")

    state, queued = await _run_ad_wrapper_failure(tmp_path, voice_side_effect=_voice)

    assert queued.metadata["error_recovery"] is True
    assert state.songs_since_ad == 0
    assert state.segments_produced == 0
    assert not list(tmp_path.glob("ad_outro_*.mp3"))
    assert not list(tmp_path.glob("bumper_out_*.mp3"))


@pytest.mark.asyncio
async def test_ad_closing_failures_prioritize_tts_unavailable_and_reset_due_counter(tmp_path: Path) -> None:
    async def _voice(text: str, _voice: str, path: Path, **_kwargs) -> None:
        path.write_bytes(text.encode())
        if path.name.startswith("ad_outro_"):
            raise TTSUnavailableError("required ad outro unavailable")

    def _bumper(path: Path, *_args, **_kwargs) -> Path:
        path.write_bytes(b"bumper")
        if path.name.startswith("bumper_out_"):
            raise RuntimeError("closing bumper failed")
        return path

    state, queued = await _run_ad_wrapper_failure(
        tmp_path,
        voice_side_effect=_voice,
        bumper_side_effect=_bumper,
    )

    assert queued.metadata["error_recovery"] is True
    assert state.songs_since_ad == 0
    assert not list(tmp_path.glob("ad_outro_*.mp3"))
    assert not list(tmp_path.glob("bumper_out_*.mp3"))


@pytest.mark.asyncio
async def test_scratch_unlink_oserror_does_not_mask_tts_recovery(tmp_path: Path) -> None:
    state = _make_state()
    state.force_next = SegmentType.STATION_ID
    state.segments_since_station_id = 9
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=1)
    recovery = _recovery_segment(tmp_path)
    original_unlink = Path.unlink
    unlink_failures: list[Path] = []

    async def _voice(_text: str, _voice: str, path: Path, **_kwargs) -> None:
        path.write_bytes(b"partial required voice")
        raise TTSUnavailableError("station voice unavailable")

    def _flaky_unlink(path: Path, *args, **kwargs) -> None:
        if path.name.startswith("stid_voice_"):
            unlink_failures.append(path)
            raise OSError("scratch filesystem unavailable")
        original_unlink(path, *args, **kwargs)

    with (
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=_voice),
        patch(f"{PRODUCER_MODULE}.generate_station_id_bed", side_effect=_write_layer),
        patch(
            f"{PRODUCER_MODULE}._producer_error_recovery_segment",
            new_callable=AsyncMock,
            return_value=recovery,
        ),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
        patch.object(Path, "unlink", new=_flaky_unlink),
    ):
        task = asyncio.create_task(producer.run_producer(queue, state, config))
        await _wait_for_queue(queue)
        await _cancel(task)

    assert queue.get_nowait() is recovery
    assert state.segments_since_station_id == 0
    assert unlink_failures


@pytest.mark.asyncio
async def test_tts_failure_norm_cache_music_recovery_schedules_restart_handoff(tmp_path: Path) -> None:
    state = _make_state()
    state.force_next = SegmentType.STATION_ID
    state.segments_since_station_id = 9
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=1)
    norm_path = tmp_path / "norm_artist_song_192k.mp3"
    norm_path.write_bytes(b"normalized music")

    async def _voice(*_args, **_kwargs) -> None:
        raise TTSUnavailableError("station voice unavailable")

    async def _identity_egress(segment: Segment, _config) -> Segment:
        return segment

    with (
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=_voice),
        patch(f"{PRODUCER_MODULE}.generate_station_id_bed", side_effect=_write_layer),
        patch(f"{PRODUCER_MODULE}._pick_recovery_clip", return_value=None),
        patch(f"{PRODUCER_MODULE}.select_norm_cache_rescue", return_value=norm_path),
        patch(
            f"{PRODUCER_MODULE}.load_track_metadata",
            return_value={"title": "Song", "artist": "Artist"},
        ),
        patch(f"{PRODUCER_MODULE}.norm_cache_duration_sec", return_value=120.0),
        patch(f"{PRODUCER_MODULE}._apply_egress", new_callable=AsyncMock, side_effect=_identity_egress),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=120.0),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
        patch(f"{PRODUCER_MODULE}.try_write_restart_handoff_spool", return_value=True) as write_spool,
    ):
        task = asyncio.create_task(producer.run_producer(queue, state, config))
        await _wait_for_queue(queue)
        async with asyncio.timeout(2.0):
            while not write_spool.called:
                await asyncio.sleep(0.01)
        await _cancel(task)
        pending_spools = list(getattr(state, "_restart_handoff_tasks", set()))
        if pending_spools:
            await asyncio.gather(*pending_spools)

    queued = queue.get_nowait()
    assert queued.type is SegmentType.MUSIC
    assert queued.path == norm_path
    assert queued.metadata["audio_source"] == "norm_cache"
    assert queued.metadata["error_recovery"] is True
    assert state.segments_since_station_id == 0
    write_spool.assert_called_once()
    cache_dir, candidates = write_spool.call_args.args
    assert cache_dir == config.cache_dir
    assert len(candidates) == 1
    assert candidates[0].path == norm_path


@pytest.mark.asyncio
async def test_fail_closed_holds_across_resume_from_stopped(tmp_path: Path) -> None:
    """Scenario 3 (post-restart): with ``session_stopped`` set from a prior run the
    producer stays idle and never synthesizes a silent speech segment; once a
    listener resumes, a total TTS outage still fails closed to non-silent recovery
    rather than silence. The resume music bridge is isolated so the assertion pins
    the produced segment's fail-closed behavior specifically.
    """
    state = _make_state()
    state.session_stopped = True
    state.segments_since_station_id = 9
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    recovery = _recovery_segment(tmp_path)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.STATION_ID),
        patch(
            f"{PRODUCER_MODULE}.synthesize",
            new_callable=AsyncMock,
            side_effect=TTSUnavailableError("no voice route after restart"),
        ) as synth,
        patch(f"{PRODUCER_MODULE}.generate_station_id_bed", side_effect=_write_layer),
        patch(f"{PRODUCER_MODULE}.generate_tone", side_effect=_write_layer),
        # Isolate the fail-closed produced segment from the resume music bridge.
        patch(f"{PRODUCER_MODULE}._queue_continuity_bridge", new_callable=AsyncMock),
        patch(
            f"{PRODUCER_MODULE}._producer_error_recovery_segment",
            new_callable=AsyncMock,
            return_value=recovery,
        ),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
    ):
        task = asyncio.create_task(producer.run_producer(queue, state, config))
        # While stopped the producer must neither produce nor synthesize: no silent
        # speech segment, no fail-closed churn.
        for _ in range(10):
            await asyncio.sleep(0)
        assert queue.empty()
        assert synth.await_count == 0

        # A listener resumes after the restart — fail-closed must still hold.
        state.session_stopped = False
        state.resume_event.set()
        await _wait_for_queue(queue)
        await _cancel(task)

    queued = queue.get_nowait()
    assert queued is recovery
    assert queued.metadata["rescue"] is True
    assert synth.await_count >= 1
    assert state.segments_since_station_id == 0
    assert not list(tmp_path.glob("stid_*.mp3"))
