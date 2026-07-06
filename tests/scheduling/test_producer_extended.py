"""Extended tests for the producer pipeline — ad breaks, HA context, Spotify path, error recovery."""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mammamiradio.audio.audio_quality import AudioQualityError, AudioToolError
from mammamiradio.core.config import load_config
from mammamiradio.core.models import (
    AdHistoryEntry,
    HostPersonality,
    Segment,
    SegmentType,
    StationState,
    Track,
)
from mammamiradio.home.ha_context import HomeContext, ScoredEntity
from mammamiradio.home.ha_enrichment import HomeEvent
from mammamiradio.hosts.ad_creative import (
    AdBrand,
    AdFormat,
    AdPart,
    AdScript,
    AdVoice,
    CampaignSpine,
    _cast_voices,
    _pick_brand,
    _select_ad_creative,
)
from mammamiradio.scheduling.producer import (
    FIRST_HOME_CONTEXT_MOMENT_DIRECTIVE,
    _home_context_ready_for_first_moment,
    _maybe_arm_first_home_context_moment,
    run_producer,
)

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")
MODULE = "mammamiradio.scheduling.producer"
SCRIPTWRITER_MODULE = "mammamiradio.hosts.scriptwriter"


@pytest.fixture(autouse=True)
def _mock_quality_gate():
    with patch(f"{MODULE}.validate_segment_audio", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _mock_download_validation():
    with patch(f"{MODULE}.validate_download", return_value=(True, "ok")):
        yield


def _make_state() -> StationState:
    return StationState(
        playlist=[
            Track(title="Canzone Uno", artist="Artista", duration_ms=200_000, spotify_id="demo1"),
            Track(title="Canzone Due", artist="Artista", duration_ms=180_000, spotify_id="demo2"),
        ],
        listeners_active=1,  # simulate a live listener so the producer gate passes
    )


def _make_config(tmp_path: Path | None = None):
    config = load_config(TOML_PATH)
    config.pacing.lookahead_segments = 1
    config.homeassistant.enabled = False
    if tmp_path:
        config.tmp_dir = tmp_path
    else:
        config.tmp_dir = Path("/tmp/mammamiradio_test")
    return config


def _fake_path(*_args, **_kwargs) -> Path:
    return Path("/tmp/mammamiradio_test/fake.mp3")


class _NoArea:
    """Sentinel type: 'area not passed' (-> default Room N) vs explicit area=None."""


_NO_AREA = _NoArea()


def _scored_home_entity(
    idx: int,
    *,
    area: str | None | _NoArea = _NO_AREA,
    label_it: str | None = None,
    label_en: str | None = None,
) -> ScoredEntity:
    room: str | None = f"Room {idx}" if isinstance(area, _NoArea) else area
    label_base = room or f"Entity {idx}"
    return ScoredEntity(
        entity_id=f"light.room_{idx}",
        area=room,
        domain="light",
        score=1.0,
        raw_state={"state": "on", "attributes": {"friendly_name": f"{label_base} light", "area": room}},
        label_it=f"Luce {label_base}" if label_it is None else label_it,
        label_en=f"{label_base} light" if label_en is None else label_en,
        summary_line=f"{label_base} light: accese",
    )


def _first_home_context(*, scored_count: int = 3, summary: str = "Home context ready") -> HomeContext:
    return HomeContext(
        summary=summary,
        timestamp=1234.5,
        scored=[_scored_home_entity(idx) for idx in range(scored_count)],
    )


async def _run_until_queued(
    queue: asyncio.Queue,
    state: StationState,
    config,
    timeout: float = 5.0,
):
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


# ---------------------------------------------------------------------------
# _pick_brand
# ---------------------------------------------------------------------------


def test_pick_brand_avoids_recent():
    brands = [AdBrand(name="A", tagline="a"), AdBrand(name="B", tagline="b"), AdBrand(name="C", tagline="c")]
    history = [
        AdHistoryEntry(brand="A", summary="", timestamp=0),
        AdHistoryEntry(brand="B", summary="", timestamp=0),
        AdHistoryEntry(brand="C", summary="", timestamp=0),
    ]
    # All 3 recent, so any brand is eligible (pool exhausted fallback)
    result = _pick_brand(brands, history)
    assert result.name in {"A", "B", "C"}


def test_pick_brand_prefers_recurring():
    brands = [
        AdBrand(name="R1", tagline="r1", recurring=True),
        AdBrand(name="NR1", tagline="nr1", recurring=False),
    ]
    # Run many times — recurring should appear more often
    results = [_pick_brand(brands, []).name for _ in range(100)]
    assert results.count("R1") > results.count("NR1")


def test_pick_brand_skips_last_three():
    brands = [
        AdBrand(name="A", tagline="a"),
        AdBrand(name="B", tagline="b"),
        AdBrand(name="C", tagline="c"),
        AdBrand(name="D", tagline="d"),
    ]
    history = [
        AdHistoryEntry(brand="A", summary="", timestamp=0),
        AdHistoryEntry(brand="B", summary="", timestamp=0),
        AdHistoryEntry(brand="C", summary="", timestamp=0),
    ]
    # Only D should be eligible
    for _ in range(20):
        assert _pick_brand(brands, history).name == "D"


# ---------------------------------------------------------------------------
# Ad break segment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ad_break_segment_queued(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    # Need brands for ad production
    config.ads.brands = [AdBrand(name="TestBrand", tagline="Buy it")]
    config.ads.voices = [AdVoice(name="VoiceGuy", voice="it-IT-DiegoNeural", style="energetic")]
    config.pacing.ad_spots_per_break = 1
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    fake_script = AdScript(
        brand="TestBrand",
        summary="Test ad",
        parts=[AdPart(type="voice", text="Buy TestBrand today!")],
    )

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.AD),
        patch(f"{SCRIPTWRITER_MODULE}.write_ad", new_callable=AsyncMock, return_value=fake_script),
        patch(f"{MODULE}.synthesize_ad", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock) as mock_synthesize,
        patch(f"{MODULE}.generate_bumper_jingle", side_effect=_fake_path),
        patch(f"{MODULE}.concat_files", side_effect=_fake_path),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert queue.qsize() >= 1
    seg = queue.get_nowait()
    assert seg.type == SegmentType.AD
    assert "brands" in seg.metadata
    assert "TestBrand" in seg.metadata["brands"]
    assert mock_synthesize.call_count >= 2
    assert all("engine" in call.kwargs for call in mock_synthesize.call_args_list)


@pytest.mark.asyncio
async def test_ad_intro_crossfade_severed_when_no_adjacent_song(tmp_path):
    """The ad-break intro must not crossfade its host opener over a stale song
    when the previous segment wasn't music (prev_seg_type is None on a cold run)."""
    state = _make_state()
    stale = tmp_path / "stale_song.mp3"
    stale.write_bytes(b"music")
    state.last_music_file = stale
    config = _make_config(tmp_path)
    config.ads.brands = [AdBrand(name="TestBrand", tagline="Buy it")]
    config.ads.voices = [AdVoice(name="VoiceGuy", voice="it-IT-DiegoNeural", style="energetic")]
    config.pacing.ad_spots_per_break = 1
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    fake_script = AdScript(
        brand="TestBrand",
        summary="Test ad",
        parts=[AdPart(type="voice", text="Buy TestBrand today!")],
    )

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.AD),
        patch(f"{SCRIPTWRITER_MODULE}.write_ad", new_callable=AsyncMock, return_value=fake_script),
        patch(f"{MODULE}.synthesize_ad", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{MODULE}.generate_bumper_jingle", side_effect=_fake_path),
        patch(f"{MODULE}.concat_files", side_effect=_fake_path),
        patch(f"{MODULE}.crossfade_voice_over_music") as mock_xfade,
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.AD
    mock_xfade.assert_not_called()


@pytest.mark.asyncio
async def test_ad_break_skipped_without_brands(tmp_path):
    """When no brands configured, ad segment is skipped and producer continues."""
    state = _make_state()
    config = _make_config(tmp_path)
    config.ads.brands = []
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    # After one AD skip (no brands), return MUSIC
    call_count = 0

    def alternating_type(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return SegmentType.AD
        return SegmentType.MUSIC

    with (
        patch(f"{MODULE}.next_segment_type", side_effect=alternating_type),
        patch(f"{MODULE}.download_track", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.normalize", side_effect=_fake_path),
        patch(f"{MODULE}.shutil.copy2"),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC


# ---------------------------------------------------------------------------
# Ad break with host fallback voice (no dedicated ad voices)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ad_break_host_fallback_voice(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    config.ads.brands = [AdBrand(name="HostBrand", tagline="host-brand")]
    config.ads.voices = []  # No dedicated ad voices → use host voice
    config.pacing.ad_spots_per_break = 1
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    fake_script = AdScript(
        brand="HostBrand",
        summary="Host ad",
        parts=[AdPart(type="voice", text="Host reads the ad")],
    )

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.AD),
        patch(f"{SCRIPTWRITER_MODULE}.write_ad", new_callable=AsyncMock, return_value=fake_script),
        patch(f"{MODULE}.synthesize_ad", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{MODULE}.generate_bumper_jingle", side_effect=_fake_path),
        patch(f"{MODULE}.concat_files", side_effect=_fake_path),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.AD


# ---------------------------------------------------------------------------
# HA context refresh
# ---------------------------------------------------------------------------


def test_first_home_context_moment_arms_current_banter():
    state = _make_state()
    ha_context = _first_home_context()

    _maybe_arm_first_home_context_moment(state, ha_context, SegmentType.BANTER)

    assert state.ha_pending_directive == FIRST_HOME_CONTEXT_MOMENT_DIRECTIVE
    assert state.force_next is None
    assert state.ha_first_home_context_moment_fired is False


def test_first_home_context_moment_forces_next_banter_from_ad():
    state = _make_state()
    ha_context = _first_home_context()

    _maybe_arm_first_home_context_moment(state, ha_context, SegmentType.AD)

    assert state.ha_pending_directive == FIRST_HOME_CONTEXT_MOMENT_DIRECTIVE
    assert state.force_next == SegmentType.BANTER
    assert state.ha_first_home_context_moment_fired is False


def test_first_home_context_moment_waits_for_safe_context():
    state = _make_state()

    _maybe_arm_first_home_context_moment(state, _first_home_context(scored_count=2), SegmentType.BANTER)

    assert state.ha_pending_directive == ""
    assert state.force_next is None
    assert state.ha_first_home_context_moment_fired is False

    _maybe_arm_first_home_context_moment(state, _first_home_context(summary=""), SegmentType.BANTER)

    assert state.ha_pending_directive == ""
    assert state.force_next is None
    assert state.ha_first_home_context_moment_fired is False

    context_without_area_or_label = HomeContext(
        summary="Context without a narratable label",
        scored=[_scored_home_entity(idx, area=None, label_it="", label_en="") for idx in range(3)],
    )
    _maybe_arm_first_home_context_moment(state, context_without_area_or_label, SegmentType.BANTER)

    assert state.ha_pending_directive == ""
    assert state.force_next is None
    assert state.ha_first_home_context_moment_fired is False


def test_first_home_context_ready_accepts_label_only_entity():
    # A safe entity that has only an Italian label — no area, no English label —
    # still narratable, so it must qualify for the first-home moment.
    entities = [_scored_home_entity(idx, area=None, label_it="", label_en="") for idx in range(3)]
    entities[0].label_it = "Luce corridoio"

    ha_context = HomeContext(summary="Label-only home context", scored=entities)

    assert _home_context_ready_for_first_moment(ha_context) is True


def test_first_home_context_moment_requires_generated_banter():
    state = _make_state()

    _maybe_arm_first_home_context_moment(
        state,
        _first_home_context(),
        SegmentType.BANTER,
        can_generate_banter=False,
    )

    assert state.ha_pending_directive == ""
    assert state.force_next is None
    assert state.ha_first_home_context_moment_fired is False


def test_first_home_context_moment_preserves_pending_actions():
    for attr, value in (
        ("ha_pending_directive", "Dinner is ready"),
        ("chaos_pending", True),
        ("force_next", SegmentType.MUSIC),
        ("operator_force_pending", SegmentType.BANTER),
    ):
        state = _make_state()
        setattr(state, attr, value)

        _maybe_arm_first_home_context_moment(state, _first_home_context(), SegmentType.BANTER)

        assert getattr(state, attr) == value
        assert state.ha_first_home_context_moment_fired is False


def test_first_home_context_moment_rearms_when_arm_was_lost_before_success():
    state = _make_state()
    _maybe_arm_first_home_context_moment(state, _first_home_context(), SegmentType.BANTER)
    state.ha_pending_directive = ""

    _maybe_arm_first_home_context_moment(state, _first_home_context(), SegmentType.AD)

    assert state.ha_pending_directive == FIRST_HOME_CONTEXT_MOMENT_DIRECTIVE
    assert state.force_next == SegmentType.BANTER
    assert state.ha_first_home_context_moment_fired is False


def test_first_home_context_moment_does_not_rearm_after_success():
    state = _make_state()
    state.ha_first_home_context_moment_fired = True

    _maybe_arm_first_home_context_moment(state, _first_home_context(), SegmentType.AD)

    assert state.ha_pending_directive == ""
    assert state.force_next is None
    assert state.ha_first_home_context_moment_fired is True


@pytest.mark.asyncio
async def test_ha_context_refreshed_for_banter(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")
    banter_lines = [(host, "Che bella giornata!")]

    mock_context = MagicMock()
    mock_context.summary = "Il tempo e' bello"
    mock_context.events_summary = "- La macchina del caffe: spento/a -> acceso/a (1 min fa)"
    mock_context.mood = "Caffe in preparazione"
    mock_context.weather_arc = "Meteo: soleggiato, 22C."
    mock_context.timestamp = 1234.5
    mock_context.mood_en = "Coffee brewing"
    mock_context.weather_arc_en = "Weather: sunny, 22C."
    mock_context.events_summary_en = "- Coffee machine: off -> on (1 min ago)"
    mock_context.scored = [
        ScoredEntity(
            entity_id="switch.bar_kaffeemaschine_steckdose",
            area="Kitchen",
            domain="switch",
            score=1.4,
            raw_state={"state": "on", "attributes": {"friendly_name": "Coffee machine"}},
            label_it="La macchina del caffe",
            label_en="Coffee machine",
            summary_line="La macchina del caffe: acceso/a",
        )
    ]
    mock_context.denylist_hits = {"privacy:person": 1}
    mock_context.catalog_hit_rate = 0.0
    mock_context.events = deque(
        [
            HomeEvent(
                entity_id="switch.bar_kaffeemaschine_steckdose",
                label="La macchina del caffe",
                old_state="spento/a",
                new_state="acceso/a",
                timestamp=1.0,
            )
        ]
    )

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=mock_context) as mock_fetch,
        patch(f"{MODULE}.resolve_home_mood", return_value=("Scena LLM", "LLM scene")) as mock_resolve_mood,
    ):
        await _run_until_queued(queue, state, config)

    mock_fetch.assert_called_once()
    mock_resolve_mood.assert_called_once_with(config, state, mock_context)
    assert state.ha_context == "Il tempo e' bello"
    assert state.ha_events_summary == "- La macchina del caffe: spento/a -> acceso/a (1 min fa)"
    assert state.ha_home_mood == "Scena LLM"
    assert state.ha_home_mood_en == "LLM scene"
    assert state.ha_scored_entities[0]["label"] == "Coffee machine"
    assert state.ha_denylist_hits == {"privacy:person": 1}
    assert state.ha_context_entity_count == 1
    assert state.ha_context_char_count == len("Il tempo e' bello")
    assert state.ha_context_last_updated == 1234.5
    assert state.ha_last_event_label == "La macchina del caffe"
    assert state.ha_last_event_label_en == "Coffee machine"


@pytest.mark.asyncio
async def test_ha_context_disabled_skips_full_state_refresh(tmp_path):
    """Operators can keep HA entity publishing while disabling full /api/states prompt context."""
    state = _make_state()
    config = _make_config(tmp_path)
    config.homeassistant.enabled = True
    config.homeassistant.context_enabled = False
    config.ha_token = "fake-token"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")
    banter_lines = [(host, "Che bella giornata!")]

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock) as mock_fetch,
        patch(f"{MODULE}.resolve_home_mood") as mock_resolve_mood,
    ):
        await _run_until_queued(queue, state, config)

    mock_fetch.assert_not_called()
    mock_resolve_mood.assert_not_called()
    assert state.ha_context == ""
    assert state.ha_context_last_updated == 0.0


@pytest.mark.asyncio
async def test_mood_resolution_failure_never_stops_segment_production(tmp_path):
    """resolve_home_mood runs outside the segment-render try — a raise there
    must degrade to the ladder mood, not kill the producer (INSTANT AUDIO)."""
    state = _make_state()
    config = _make_config(tmp_path)
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")
    banter_lines = [(host, "Che bella giornata!")]

    mock_context = MagicMock()
    mock_context.summary = "Il tempo e' bello"
    mock_context.events_summary = ""
    mock_context.mood = "Caffe in preparazione"
    mock_context.weather_arc = ""
    mock_context.timestamp = 1234.5
    mock_context.mood_en = "Coffee brewing"
    mock_context.weather_arc_en = ""
    mock_context.events_summary_en = ""
    mock_context.scored = []
    mock_context.denylist_hits = {}
    mock_context.catalog_hit_rate = 0.0
    mock_context.events = deque()

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=mock_context),
        patch(f"{MODULE}.resolve_home_mood", side_effect=RuntimeError("scene namer exploded")),
    ):
        await _run_until_queued(queue, state, config)

    assert state.ha_home_mood == "Caffe in preparazione"
    assert state.ha_home_mood_en == "Coffee brewing"
    assert not queue.empty()


@pytest.mark.asyncio
async def test_ha_context_schedules_label_generation_fire_and_forget(tmp_path):
    """The producer schedules a background label refresh from the raw HA states,
    and a scheduling failure must never break the segment build (audio path)."""
    state = _make_state()
    config = _make_config(tmp_path)
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")
    banter_lines = [(host, "Che bella giornata!")]

    raw_states = {
        "switch.bar_kaffeemaschine_steckdose": {"state": "on", "attributes": {"friendly_name": "Coffee machine"}}
    }
    mock_context = MagicMock()
    mock_context.summary = "Il tempo e' bello"
    mock_context.events_summary = ""
    mock_context.mood = ""
    mock_context.weather_arc = ""
    mock_context.timestamp = 1234.5
    mock_context.mood_en = ""
    mock_context.weather_arc_en = ""
    mock_context.events_summary_en = ""
    mock_context.scored = [
        ScoredEntity(
            entity_id="switch.bar_kaffeemaschine_steckdose",
            area="Kitchen",
            domain="switch",
            score=1.4,
            raw_state=raw_states["switch.bar_kaffeemaschine_steckdose"],
            label_it="La macchina del caffe",
            label_en="Coffee machine",
            summary_line="La macchina del caffe: acceso/a",
        )
    ]
    mock_context.denylist_hits = {}
    mock_context.catalog_hit_rate = 0.0
    mock_context.raw_states = raw_states
    mock_context.events = deque()

    # schedule raising must not propagate into the producer loop.
    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=mock_context),
        patch(f"{MODULE}.schedule_label_generation", return_value=True) as mock_schedule,
    ):
        await _run_until_queued(queue, state, config)

    mock_schedule.assert_called_once()
    assert mock_schedule.call_args.args[0] == raw_states
    assert mock_schedule.call_args.kwargs["cache_dir"] == config.cache_dir
    score_map = mock_schedule.call_args.kwargs["score_by_entity"]
    assert score_map["switch.bar_kaffeemaschine_steckdose"] == 1.4


@pytest.mark.asyncio
async def test_ha_context_scheduling_exception_does_not_stop_production(tmp_path):
    """If schedule_label_generation raises (it does synchronous preflight work),
    the producer must still build and queue the segment — fail-soft audio path."""
    state = _make_state()
    config = _make_config(tmp_path)
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")
    banter_lines = [(host, "Che bella giornata!")]

    raw_states = {
        "switch.bar_kaffeemaschine_steckdose": {"state": "on", "attributes": {"friendly_name": "Coffee machine"}}
    }
    mock_context = MagicMock()
    mock_context.summary = "Il tempo e' bello"
    mock_context.events_summary = ""
    mock_context.mood = ""
    mock_context.weather_arc = ""
    mock_context.timestamp = 1234.5
    mock_context.mood_en = ""
    mock_context.weather_arc_en = ""
    mock_context.events_summary_en = ""
    mock_context.scored = []
    mock_context.denylist_hits = {}
    mock_context.catalog_hit_rate = 0.0
    mock_context.raw_states = raw_states
    mock_context.events = deque()

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=mock_context),
        patch(f"{MODULE}.schedule_label_generation", side_effect=RuntimeError("boom")) as mock_schedule,
    ):
        await _run_until_queued(queue, state, config)

    mock_schedule.assert_called_once()
    assert not queue.empty()


@pytest.mark.asyncio
async def test_ha_context_first_home_moment_armed_during_banter(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")
    mock_context = _first_home_context(
        summary="- Kitchen light: accese\n- Bedroom presence: occupata\n- Vacuum: pulisce"
    )
    mock_context.denylist_hits = {}
    mock_context.catalog_hit_rate = 0.0

    async def _write_banter_consumes_directive(*args, **_kwargs):
        args[0].ha_pending_directive = ""
        return [(host, "Ciao!")], None

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{MODULE}._sw.has_script_llm", return_value=True),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            side_effect=_write_banter_consumes_directive,
        ),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=mock_context),
    ):
        await _run_until_queued(queue, state, config)

    assert state.ha_pending_directive == ""
    assert state.force_next is None
    assert state.ha_first_home_context_moment_fired is True


@pytest.mark.asyncio
async def test_ha_context_first_home_moment_not_fired_when_directive_restored_after_fallback(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")
    mock_context = _first_home_context(
        summary="- Kitchen light: accese\n- Bedroom presence: occupata\n- Vacuum: pulisce"
    )
    mock_context.denylist_hits = {}
    mock_context.catalog_hit_rate = 0.0

    async def _write_banter_restores_directive_after_fallback(*args, **_kwargs):
        args[0].ha_pending_directive = FIRST_HOME_CONTEXT_MOMENT_DIRECTIVE
        return [(host, "Anyway. Music.")], None

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{MODULE}._sw.has_script_llm", return_value=True),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            side_effect=_write_banter_restores_directive_after_fallback,
        ),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=mock_context),
    ):
        await _run_until_queued(queue, state, config)

    assert state.ha_pending_directive == FIRST_HOME_CONTEXT_MOMENT_DIRECTIVE
    assert state.ha_first_home_context_moment_fired is False


@pytest.mark.asyncio
async def test_public_status_only_surfaces_curated_event_labels(tmp_path):
    # /public-status exposes state.ha_last_event_label to listeners. Phase A
    # widened the ingest to all HA entities; only curated entities are listener-safe.
    state = _make_state()
    config = _make_config(tmp_path)
    config.anthropic_api_key = "test-key"
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")

    mock_context = MagicMock()
    mock_context.summary = ""
    mock_context.events_summary = "- Hallway Motion: spento/a -> acceso/a (1 min fa)"
    mock_context.mood = ""
    mock_context.weather_arc = ""
    mock_context.mood_en = ""
    mock_context.weather_arc_en = ""
    mock_context.events_summary_en = ""
    mock_context.scored = []
    mock_context.denylist_hits = {}
    mock_context.catalog_hit_rate = 0.0
    mock_context.last_event_label_en = ""
    # Uncurated entity with a friendly_name — must not surface on /public-status.
    mock_context.events = deque(
        [
            HomeEvent(
                entity_id="binary_sensor.bedroom_motion",
                label="Hallway Motion",
                old_state="spento/a",
                new_state="acceso/a",
                timestamp=1.0,
            )
        ]
    )

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=([(host, "Ciao!")], None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=mock_context),
    ):
        await _run_until_queued(queue, state, config)

    # Uncurated event must not leak to listener-facing fields.
    assert state.ha_last_event_label == ""
    assert state.ha_last_event_label_en == ""


@pytest.mark.asyncio
async def test_banter_quality_reject_uses_canned_fallback(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    config.anthropic_api_key = "test-key"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")
    generated = tmp_path / "banter_generated.mp3"
    generated.write_bytes(b"\x00" * 2048)
    canned = tmp_path / "banter_canned.mp3"
    canned.write_bytes(b"\x00" * 2048)

    quality_calls = 0

    def _quality_side_effect(path, seg_type):
        nonlocal quality_calls
        if seg_type == SegmentType.BANTER:
            quality_calls += 1
            if quality_calls == 1:
                raise AudioQualityError("too much silence")
        return None

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            return_value=([(host, "Linea test")], None),
        ),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=generated),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=generated),
        patch(f"{MODULE}.concat_files", return_value=generated),
        patch(f"{MODULE}._pick_canned_clip", return_value=canned),
        patch(f"{MODULE}.validate_segment_audio", side_effect=_quality_side_effect),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.BANTER
    assert seg.metadata.get("canned") is True
    assert seg.path == canned


def _write_concat_output(paths, output_path, silence_ms=300, loudnorm=True, **kwargs):
    output_path.write_bytes(b"\x00" * 2048)
    return output_path


def _long_banter_lines(host: HostPersonality) -> list[tuple[HostPersonality, str]]:
    return [
        (host, "Prima linea con abbastanza parole per stimare durata."),
        (host, "Seconda linea ancora piena di contenuto radiofonico."),
        (host, "Terza linea che continua lo scambio tra conduttori."),
        (host, "Quarta linea con una battuta sul brano appena passato."),
        (host, "Quinta linea che tiene viva la scena in studio."),
        (host, "Sesta linea prima di tornare alla musica."),
    ]


@pytest.mark.asyncio
async def test_banter_generated_audio_passes_expected_duration_context(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    config.anthropic_api_key = "test-key"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0]
    trans_path = tmp_path / "transition.mp3"
    trans_path.write_bytes(b"\x00" * 2048)
    banter_path = tmp_path / "dialogue.mp3"
    banter_path.write_bytes(b"\x00" * 2048)
    validate_calls: list[dict] = []

    def _validate_side_effect(path, seg_type, **kwargs):
        if seg_type == SegmentType.BANTER:
            validate_calls.append({"path": path, **kwargs})

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(_long_banter_lines(host), None)
        ),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Bentornati.")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=trans_path),
        patch(f"{MODULE}._try_crossfade", new_callable=AsyncMock, return_value=trans_path),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=banter_path),
        patch(f"{MODULE}.concat_files", side_effect=_write_concat_output) as mock_concat,
        patch(f"{MODULE}._pick_canned_clip", return_value=None),
        patch(f"{MODULE}._apply_talk_bed", new_callable=AsyncMock, side_effect=lambda path, *_a, **_k: path),
        patch(f"{MODULE}.validate_segment_audio", side_effect=_validate_side_effect),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.BANTER
    assert seg.metadata.get("canned") is False
    assert validate_calls
    assert validate_calls[0]["expected_line_count"] == 7
    assert validate_calls[0]["expected_min_duration_sec"] > 0
    assert mock_concat.call_args.kwargs["strict_duration"] is True


@pytest.mark.asyncio
async def test_banter_implausibly_short_with_no_canned_fallback_is_not_queued(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    config.anthropic_api_key = "test-key"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0]
    trans_path = tmp_path / "transition.mp3"
    trans_path.write_bytes(b"\x00" * 2048)
    banter_path = tmp_path / "dialogue.mp3"
    banter_path.write_bytes(b"\x00" * 2048)

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(_long_banter_lines(host), None)
        ),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Bentornati.")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=trans_path),
        patch(f"{MODULE}._try_crossfade", new_callable=AsyncMock, return_value=trans_path),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=banter_path),
        patch(f"{MODULE}.concat_files", side_effect=_write_concat_output),
        patch(f"{MODULE}._pick_canned_clip", return_value=None),
        patch(f"{MODULE}.validate_segment_audio", side_effect=AudioQualityError("implausibly short banter")),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.25)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    assert queue.empty()


@pytest.mark.asyncio
async def test_banter_concat_duration_failure_cleans_temporary_parts(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    config.anthropic_api_key = "test-key"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0]
    trans_path = tmp_path / "transition.mp3"
    trans_path.write_bytes(b"\x00" * 2048)
    banter_path = tmp_path / "dialogue.mp3"
    banter_path.write_bytes(b"\x00" * 2048)
    concat_calls = 0

    def _concat_fails(paths, output_path, silence_ms=300, loudnorm=True, **kwargs):
        nonlocal concat_calls
        concat_calls += 1
        output_path.write_bytes(b"\x00" * 2048)
        state.listeners_active = 0
        raise RuntimeError("duration shortfall")

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(_long_banter_lines(host), None)
        ),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Bentornati.")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=trans_path),
        patch(f"{MODULE}._try_crossfade", new_callable=AsyncMock, return_value=trans_path),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=banter_path),
        patch(f"{MODULE}.concat_files", side_effect=_concat_fails),
        patch(f"{MODULE}._pick_canned_clip", return_value=None),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.25)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    assert queue.empty()
    assert concat_calls == 1
    assert not trans_path.exists()
    assert not banter_path.exists()
    assert not list(tmp_path.glob("banter_full_*.mp3"))


@pytest.mark.asyncio
async def test_banter_after_session_resume_uses_expected_duration_context(tmp_path):
    state = _make_state()
    state.session_stopped = True
    config = _make_config(tmp_path)
    config.cache_dir = tmp_path
    config.anthropic_api_key = "test-key"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0]
    trans_path = tmp_path / "transition.mp3"
    trans_path.write_bytes(b"\x00" * 2048)
    banter_path = tmp_path / "dialogue.mp3"
    banter_path.write_bytes(b"\x00" * 2048)
    validate_calls: list[dict] = []

    def _validate_side_effect(path, seg_type, **kwargs):
        if seg_type == SegmentType.BANTER:
            validate_calls.append({"path": path, **kwargs})

    def _fake_tone(path: Path, *_args, **_kwargs):
        path.write_bytes(b"tone")
        return path

    seg: Segment | None = None
    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(_long_banter_lines(host), None)
        ),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Bentornati.")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=trans_path),
        patch(f"{MODULE}._try_crossfade", new_callable=AsyncMock, return_value=trans_path),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=banter_path),
        patch(f"{MODULE}.concat_files", side_effect=_write_concat_output),
        patch(f"{MODULE}._pick_canned_clip", return_value=None),
        patch(f"{MODULE}.generate_tone", side_effect=_fake_tone),
        patch(f"{MODULE}._apply_talk_bed", new_callable=AsyncMock, side_effect=lambda path, *_a, **_k: path),
        patch(f"{MODULE}.validate_segment_audio", side_effect=_validate_side_effect),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{MODULE}.ImagingLibrary") as mock_imaging_cls,
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.05)
            state.session_stopped = False
            state.resume_event.set()
            deadline = asyncio.get_event_loop().time() + 8.0
            while seg is None:
                if not queue.empty():
                    candidate = queue.get_nowait()
                    if candidate.metadata.get("resume_bridge") is True:
                        assert candidate.metadata.get("audio_source") == "emergency_tone"
                        continue
                    seg = candidate
                    break
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("Producer did not produce BANTER after resume")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    assert seg is not None
    assert seg.type == SegmentType.BANTER
    assert seg.metadata.get("canned") is False
    assert validate_calls
    assert validate_calls[0]["expected_line_count"] == 7
    assert validate_calls[0]["expected_min_duration_sec"] > 0
    mock_imaging_cls.return_value.pick_stinger.assert_not_called()


@pytest.mark.asyncio
async def test_ad_quality_reject_resets_pacing_and_continues(tmp_path):
    state = _make_state()
    state.songs_since_ad = 9
    config = _make_config(tmp_path)
    config.ads.brands = [AdBrand(name="TestBrand", tagline="Buy it")]
    config.ads.voices = [AdVoice(name="VoiceGuy", voice="it-IT-DiegoNeural", style="energetic")]
    config.pacing.ad_spots_per_break = 1
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    fake_script = AdScript(
        brand="TestBrand",
        summary="Test ad",
        parts=[AdPart(type="voice", text="Buy TestBrand today!")],
    )

    call_count = 0

    def _seg_type(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return SegmentType.AD
        return SegmentType.MUSIC

    def _quality_side_effect(path, seg_type):
        if seg_type == SegmentType.AD:
            raise AudioQualityError("silent ad break")
        return None

    with (
        patch(f"{MODULE}.next_segment_type", side_effect=_seg_type),
        patch(f"{SCRIPTWRITER_MODULE}.write_ad", new_callable=AsyncMock, return_value=fake_script),
        patch(f"{MODULE}.synthesize_ad", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{MODULE}.generate_bumper_jingle", side_effect=_fake_path),
        patch(f"{MODULE}.concat_files", side_effect=_fake_path),
        patch(f"{MODULE}.validate_segment_audio", side_effect=_quality_side_effect),
        patch(f"{MODULE}.download_track", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.normalize", side_effect=_fake_path),
        patch(f"{MODULE}.shutil.copy2"),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC
    assert state.songs_since_ad == 1


# ---------------------------------------------------------------------------
# Error recovery — emergency tone generation also fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_recovery_emergency_tone_failure_is_contained(tmp_path):
    """When download, recovery sweeper, and emergency tone fail, producer continues."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    call_count = 0

    def segment_type_with_recovery(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return SegmentType.MUSIC
        return SegmentType.MUSIC

    with (
        patch(f"{MODULE}.next_segment_type", side_effect=segment_type_with_recovery),
        patch(
            f"{MODULE}.download_track",
            new_callable=AsyncMock,
            side_effect=[
                RuntimeError("network down"),
                _fake_path(),
            ],
        ),
        patch(f"{MODULE}._pick_canned_clip", return_value=None),
        patch(f"{MODULE}.select_norm_cache_rescue", return_value=None),
        patch(
            f"{MODULE}._build_recovery_sweeper_segment",
            new_callable=AsyncMock,
            side_effect=RuntimeError("tts down"),
        ),
        patch(f"{MODULE}.generate_tone", side_effect=[RuntimeError("ffmpeg broken"), _fake_path]),
        patch(
            f"{MODULE}.generate_silence",
            side_effect=AssertionError("silence should never be a producer recovery fallback"),
            create=True,
        ) as mock_silence,
        patch(f"{MODULE}.normalize", side_effect=_fake_path),
        patch(f"{MODULE}.shutil.copy2"),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config, timeout=10.0)

    mock_silence.assert_not_called()
    # Should eventually get a segment (either tone from 2nd attempt or music)
    assert queue.qsize() >= 1


# ---------------------------------------------------------------------------
# Backoff on consecutive failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consecutive_failures_increment_counter(tmp_path):
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    recovery_path = tmp_path / "recovery.mp3"
    recovery_path.write_bytes(b"recovery")
    recovery = Segment(
        type=SegmentType.SWEEPER,
        path=recovery_path,
        metadata={"type": "sweeper", "rescue": True, "error_recovery": True, "title": "Recovery sweeper"},
    )

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{MODULE}.download_track", new_callable=AsyncMock, side_effect=RuntimeError("fail")),
        patch(f"{MODULE}._pick_canned_clip", return_value=None),
        patch(f"{MODULE}.select_norm_cache_rescue", return_value=None),
        patch(f"{MODULE}._build_recovery_sweeper_segment", new_callable=AsyncMock, return_value=recovery),
        patch(
            f"{MODULE}.generate_silence",
            side_effect=AssertionError("silence should never be a producer recovery fallback"),
            create=True,
        ) as mock_silence,
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    mock_silence.assert_not_called()
    seg = queue.get_nowait()
    assert seg.type == SegmentType.SWEEPER
    assert state.failed_segments >= 1


@pytest.mark.asyncio
async def test_success_resets_failure_counter(tmp_path):
    state = _make_state()
    state.failed_segments = 5
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{MODULE}.download_track", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.normalize", side_effect=_fake_path),
        patch(f"{MODULE}.shutil.copy2"),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert state.failed_segments == 0


# ---------------------------------------------------------------------------
# AudioToolError pass-through — tool absent should not drop content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audio_tool_error_in_banter_does_not_drop_segment(tmp_path):
    """If ffmpeg is absent during banter quality check, segment should still be queued."""
    state = _make_state()
    config = _make_config(tmp_path)
    config.anthropic_api_key = "test-key"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")
    generated = tmp_path / "banter_generated.mp3"
    generated.write_bytes(b"\x00" * 2048)

    def _quality_side_effect(path, seg_type, **_kwargs):
        if seg_type == SegmentType.BANTER:
            raise AudioToolError("ffmpeg not found")
        return None

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=([(host, "Ciao!")], None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=generated),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=generated),
        patch(f"{MODULE}.concat_files", return_value=generated),
        patch(f"{MODULE}.validate_segment_audio", side_effect=_quality_side_effect),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.BANTER


@pytest.mark.asyncio
async def test_audio_tool_error_in_ad_does_not_drop_segment(tmp_path):
    """If ffmpeg is absent during ad quality check, ad break should still be queued."""
    state = _make_state()
    state.songs_since_ad = 9
    config = _make_config(tmp_path)
    config.ads.brands = [AdBrand(name="TestBrand", tagline="Buy it")]
    config.ads.voices = [AdVoice(name="VoiceGuy", voice="it-IT-DiegoNeural", style="energetic")]
    config.pacing.ad_spots_per_break = 1
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    fake_script = AdScript(
        brand="TestBrand",
        summary="Test ad",
        parts=[AdPart(type="voice", text="Buy TestBrand today!")],
    )

    def _quality_side_effect(path, seg_type):
        if seg_type == SegmentType.AD:
            raise AudioToolError("ffmpeg not found")
        return None

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.AD),
        patch(f"{SCRIPTWRITER_MODULE}.write_ad", new_callable=AsyncMock, return_value=fake_script),
        patch(f"{MODULE}.synthesize_ad", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{MODULE}.generate_bumper_jingle", side_effect=_fake_path),
        patch(f"{MODULE}.concat_files", side_effect=_fake_path),
        patch(f"{MODULE}.validate_segment_audio", side_effect=_quality_side_effect),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.AD


# ---------------------------------------------------------------------------
# MUSIC quality gate — corrupt/silent downloads rejected before queueing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_music_quality_reject_retries_next_track(tmp_path):
    """A music track that fails the quality gate should be skipped; producer retries the next track."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    call_count = 0

    def _quality_side_effect(path, seg_type):
        nonlocal call_count
        if seg_type == SegmentType.MUSIC:
            call_count += 1
            if call_count == 1:
                raise AudioQualityError("too short (10.00s < 30.00s)")
        return None

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{MODULE}.download_track", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.normalize", side_effect=_fake_path),
        patch(f"{MODULE}.shutil.copy2"),
        patch(f"{MODULE}.validate_segment_audio", side_effect=_quality_side_effect),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC
    # The first call rejected, second call passed — so quality was checked at least twice
    assert call_count >= 2


@pytest.mark.asyncio
async def test_music_quality_circuit_breaker_recycles_last_good_music(tmp_path):
    """Silence-rejection circuit breaker recycles last-known-good music instead of
    queueing silent audio (leadership principle #1 — NEVER BREAK THE ILLUSION).

    When three consecutive tracks fail the silence gate, no canned banter is available,
    but a prior music norm exists, the breaker must queue that file — not the silent one.
    """
    state = _make_state()
    state.playlist = [
        Track(title=f"Track {i}", artist="A", duration_ms=200_000, spotify_id=f"demo{i}") for i in range(6)
    ]
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    last_good = tmp_path / "prior_good_norm.mp3"
    last_good.write_bytes(b"real audio bytes")
    state.last_music_file = last_good

    call_count = 0

    def _always_reject(path, seg_type):
        nonlocal call_count
        if seg_type == SegmentType.MUSIC:
            call_count += 1
            raise AudioQualityError("music has too much silence (100% > 95%)")
        return None

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{MODULE}.download_track", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.normalize", side_effect=_fake_path),
        patch(f"{MODULE}.shutil.copy2"),
        patch(f"{MODULE}.validate_segment_audio", side_effect=_always_reject),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{MODULE}._pick_canned_clip", return_value=None),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC
    assert seg.path == last_good, "breaker must recycle last-known-good, not silent track"
    assert seg.metadata.get("recycled") is True
    assert seg.metadata.get("silence_fallback") is True
    assert call_count == 3


@pytest.mark.asyncio
async def test_circuit_breaker_silence_prefers_packaged_recovery_clip(tmp_path):
    """When all-silence rejections hit the circuit breaker, the breaker must use
    the full recovery clip ladder instead of the older banter/welcome-only path."""
    state = _make_state()
    state.playlist = [
        Track(title=f"Track {i}", artist="A", duration_ms=200_000, spotify_id=f"demo{i}") for i in range(6)
    ]
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    recovery_clip = tmp_path / "continuity_1.mp3"
    recovery_clip.write_bytes(b"fake")
    picked_subdirs: list[str] = []

    def _always_silence(path, seg_type):
        if seg_type == SegmentType.MUSIC:
            raise AudioQualityError("music has too much silence (100% > 95%)")

    def _pick_recovery_only(subdir: str, *, state=None):
        picked_subdirs.append(subdir)
        return recovery_clip if subdir == "recovery" else None

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{MODULE}.download_track", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.normalize", side_effect=_fake_path),
        patch(f"{MODULE}.shutil.copy2"),
        patch(f"{MODULE}.validate_segment_audio", side_effect=_always_silence),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{MODULE}._pick_canned_clip", side_effect=_pick_recovery_only),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    # Packaged recovery was available -> circuit breaker must queue BANTER not the silent track.
    assert seg.type == SegmentType.BANTER
    assert seg.path == recovery_clip
    assert seg.metadata.get("silence_fallback") is True
    assert seg.metadata.get("rescue") is True
    assert seg.metadata.get("title") == "Station continuity"
    assert picked_subdirs == ["recovery"]


# ---------------------------------------------------------------------------
# Signature ad system: _select_ad_creative and _cast_voices
# ---------------------------------------------------------------------------


def test_select_ad_creative_uses_format_pool():
    """When brand has a campaign.format_pool, format is picked from it."""
    brand = AdBrand(
        name="Test",
        tagline="T",
        campaign=CampaignSpine(format_pool=["late_night_whisper", "institutional_psa"]),
    )
    state = StationState()
    config = _make_config(Path("/tmp"))
    config.ads.voices = [
        AdVoice(name="A", voice="v1", style="s", role="hammer"),
        AdVoice(name="B", voice="v2", style="s", role="seductress"),
    ]

    for _ in range(20):
        fmt, _sonic, _roles = _select_ad_creative(brand, state, len(config.ads.voices))
        assert fmt in ("late_night_whisper", "institutional_psa")


def test_select_ad_creative_default_format():
    """Brand without campaign gets a format from the full list."""
    brand = AdBrand(name="Test", tagline="T")
    state = StationState()
    config = _make_config(Path("/tmp"))

    fmt, _sonic, _roles = _select_ad_creative(brand, state, len(config.ads.voices))
    all_formats = [f.value for f in AdFormat]
    assert fmt in all_formats


def test_select_ad_creative_voice_count_guard():
    """With < 2 voices, duo_scene and testimonial should be excluded."""
    brand = AdBrand(
        name="Test",
        tagline="T",
        campaign=CampaignSpine(format_pool=["duo_scene", "testimonial", "classic_pitch"]),
    )
    state = StationState()
    config = _make_config(Path("/tmp"))
    config.ads.voices = [AdVoice(name="Solo", voice="v1", style="s")]  # only 1 voice

    for _ in range(20):
        fmt, _sonic, _roles = _select_ad_creative(brand, state, len(config.ads.voices))
        assert fmt not in ("duo_scene", "testimonial")


def test_select_ad_creative_speaker_count():
    """duo_scene should request 2 roles."""
    brand = AdBrand(
        name="Test",
        tagline="T",
        campaign=CampaignSpine(format_pool=["duo_scene"], spokesperson="hammer"),
    )
    state = StationState()
    config = _make_config(Path("/tmp"))
    config.ads.voices = [
        AdVoice(name="A", voice="v1", style="s", role="hammer"),
        AdVoice(name="B", voice="v2", style="s", role="maniac"),
    ]

    fmt, _sonic, roles = _select_ad_creative(brand, state, len(config.ads.voices))
    assert fmt == "duo_scene"
    assert len(roles) == 2


def test_cast_voices_with_spokesperson():
    """Brand with spokesperson gets that role's voice as primary."""
    brand = AdBrand(
        name="Test",
        tagline="T",
        campaign=CampaignSpine(spokesperson="hammer"),
    )
    config = _make_config(Path("/tmp"))
    config.ads.voices = [
        AdVoice(name="Roberto", voice="it-IT-GianniNeural", style="booming", role="hammer"),
        AdVoice(name="Fiamma", voice="it-IT-FiammaNeural", style="enthusiastic", role="maniac"),
    ]

    voice_map = _cast_voices(brand, config.ads.voices, config.hosts, ["hammer"])
    assert "hammer" in voice_map
    assert voice_map["hammer"].name == "Roberto"


def test_cast_voices_fallback_random():
    """When no voice matches a role, a random voice is used."""
    brand = AdBrand(name="Test", tagline="T")
    config = _make_config(Path("/tmp"))
    config.ads.voices = [
        AdVoice(name="Roberto", voice="it-IT-GianniNeural", style="booming", role="hammer"),
    ]

    voice_map = _cast_voices(brand, config.ads.voices, [], ["unknown_role"])
    assert "unknown_role" in voice_map
    assert voice_map["unknown_role"].name == "Roberto"  # only option


def test_select_ad_creative_avoids_last_format():
    """_select_ad_creative avoids the last-used format for a brand."""
    brand = AdBrand(
        name="Test",
        tagline="T",
        campaign=CampaignSpine(format_pool=["classic_pitch", "live_remote"]),
    )
    state = StationState()
    config = _make_config(Path("/tmp"))

    # Seed history so last format for this brand was classic_pitch
    state.ad_history.append(AdHistoryEntry(brand="Test", summary="test", timestamp=0.0, format="classic_pitch"))

    # With only 2 options and last-used excluded, should always pick live_remote
    for _ in range(20):
        fmt, _sonic, _roles = _select_ad_creative(brand, state, len(config.ads.voices))
        assert fmt == "live_remote"


def test_select_ad_creative_category_sonic_defaults():
    """Brand without campaign but with known category gets one of the configured sonic variants."""
    brand = AdBrand(name="Test", tagline="T", category="food")
    state = StationState()
    config = _make_config(Path("/tmp"))

    _fmt, sonic, _roles = _select_ad_creative(brand, state, len(config.ads.voices))
    assert sonic.environment in {"cafe", "shopping_channel"}
    assert sonic.music_bed in {"tarantella_pop", "cheap_synth_romance", "upbeat"}
    assert sonic.transition_motif in {"register_hit", "ice_clink", "mandolin_sting"}


def test_select_ad_creative_avoids_last_sonic_variant_when_possible():
    brand = AdBrand(name="Test", tagline="T", category="food")
    state = StationState()
    config = _make_config(Path("/tmp"))
    state.record_ad_spot(
        brand="Test",
        environment="cafe",
        music_bed="tarantella_pop",
        transition_motif="register_hit",
    )

    for _ in range(20):
        _fmt, sonic, _roles = _select_ad_creative(brand, state, len(config.ads.voices))
        assert not (
            sonic.environment == "cafe"
            and sonic.music_bed == "tarantella_pop"
            and sonic.transition_motif == "register_hit"
        )


@pytest.mark.asyncio
async def test_news_flash_segment_is_produced(tmp_path):
    """Producer produces a NEWS_FLASH segment when the scheduler asks for one."""
    state = _make_state()
    config = _make_config(tmp_path)
    config.anthropic_api_key = "test-key"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0]
    flash_path = tmp_path / "flash.mp3"
    flash_path.write_bytes(b"\x00" * 2048)

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.NEWS_FLASH),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_news_flash",
            new_callable=AsyncMock,
            return_value=(host, "Aggiornamento traffico!", "traffic"),
        ),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=flash_path) as mock_synthesize,
        patch(f"{MODULE}._try_crossfade", new_callable=AsyncMock, return_value=flash_path),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.NEWS_FLASH
    assert seg.metadata.get("category") == "traffic"
    assert seg.metadata.get("host") == host.name
    assert mock_synthesize.await_args.kwargs.get("rate") == "+10%"
    assert mock_synthesize.await_args.kwargs.get("pitch") is None


@pytest.mark.asyncio
async def test_news_flash_sports_uses_neutral_tts_prosody(tmp_path):
    """Producer does not spike sports news with extra TTS rate or pitch."""
    state = _make_state()
    config = _make_config(tmp_path)
    config.anthropic_api_key = "test-key"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0]
    flash_path = tmp_path / "flash.mp3"
    flash_path.write_bytes(b"\x00" * 2048)

    synthesize_calls: list[dict] = []

    async def _capture_synthesize(text, voice, path, rate=None, pitch=None, **kw):
        synthesize_calls.append({"rate": rate, "pitch": pitch})
        return path

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.NEWS_FLASH),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_news_flash",
            new_callable=AsyncMock,
            return_value=(host, "Il Borgo Sud pareggia con ordine.", "sports"),
        ),
        patch(f"{MODULE}.synthesize", side_effect=_capture_synthesize),
        patch(f"{MODULE}._try_crossfade", new_callable=AsyncMock, return_value=flash_path),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert synthesize_calls, "synthesize must be called"
    assert synthesize_calls[0]["rate"] is None
    assert synthesize_calls[0]["pitch"] is None


@pytest.mark.asyncio
async def test_news_flash_tts_failure_skips_gracefully(tmp_path):
    """Scenario 2 (empty fallback): when write_news_flash raises, producer skips the
    NEWS_FLASH and continues — no crash, no dead air, next iteration produces audio."""
    state = _make_state()
    config = _make_config(tmp_path)
    config.anthropic_api_key = "test-key"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0]
    banter_path = tmp_path / "banter.mp3"
    banter_path.write_bytes(b"\x00" * 2048)

    call_count = 0

    def _seg_type_switch(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        # First iteration: NEWS_FLASH that will fail; subsequent: BANTER
        return SegmentType.NEWS_FLASH if call_count <= 1 else SegmentType.BANTER

    with (
        patch(f"{MODULE}.next_segment_type", side_effect=_seg_type_switch),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_news_flash",
            new_callable=AsyncMock,
            side_effect=RuntimeError("news backend down"),
        ),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            return_value=([(host, "Dopo il guasto, eccoci!")], None),
        ),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition",
            new_callable=AsyncMock,
            return_value=(host, "Bentornati."),
        ),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=banter_path),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=banter_path),
        patch(f"{MODULE}.concat_files", return_value=banter_path),
        patch(f"{MODULE}._pick_canned_clip", return_value=None),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config, timeout=8.0)

    # Producer must recover: queue must have a non-NEWS_FLASH segment
    assert not queue.empty(), "Producer must enqueue a segment after NEWS_FLASH failure"
    seg = queue.get_nowait()
    assert seg.type != SegmentType.NEWS_FLASH, "Failed NEWS_FLASH must not appear in queue"


@pytest.mark.asyncio
async def test_news_flash_produced_after_session_resume(tmp_path):
    """Scenario 3 (post-restart): producer pauses with session_stopped=True, then resumes
    and produces NEWS_FLASH once session_stopped is cleared."""
    state = _make_state()
    state.session_stopped = True  # simulate post-restart stopped state
    config = _make_config(tmp_path)
    config.cache_dir = tmp_path  # isolate from real norm cache so resume uses emergency bridge
    config.anthropic_api_key = "test-key"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0]
    flash_path = tmp_path / "flash.mp3"
    flash_path.write_bytes(b"\x00" * 2048)

    def _fake_tone(path: Path, *_args, **_kwargs):
        path.write_bytes(b"tone")
        return path

    seg: Segment | None = None
    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.NEWS_FLASH),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_news_flash",
            new_callable=AsyncMock,
            return_value=(host, "Notizie aggiornate!", "traffic"),
        ),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=flash_path),
        patch(f"{MODULE}._try_crossfade", new_callable=AsyncMock, return_value=flash_path),
        patch(f"{MODULE}._pick_canned_clip", return_value=None),
        patch(f"{MODULE}.select_norm_cache_rescue", return_value=None),
        patch(f"{MODULE}.generate_tone", side_effect=_fake_tone),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.05)  # let producer enter the stopped loop
            state.session_stopped = False  # simulate operator resuming session
            deadline = asyncio.get_event_loop().time() + 8.0
            while seg is None:
                if not queue.empty():
                    candidate = queue.get_nowait()
                    if candidate.metadata.get("resume_bridge") is True:
                        assert candidate.metadata.get("audio_source") == "emergency_tone"
                        continue
                    seg = candidate
                    break
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("Producer did not produce NEWS_FLASH after resume")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    assert seg is not None, "Producer must have queued a segment after session resume"
    assert seg.type == SegmentType.NEWS_FLASH
    assert seg.metadata.get("category") == "traffic"


def test_cast_voices_host_fallback():
    """When no ad voices are configured, _cast_voices falls back to a host voice."""
    brand = AdBrand(name="Test", tagline="T")
    config = _make_config(Path("/tmp"))
    config.ads.voices = []  # no ad voices

    voice_map = _cast_voices(brand, config.ads.voices, config.hosts, ["hammer"])
    assert "hammer" in voice_map
    # Should be one of the configured hosts
    host_names = {h.name for h in config.hosts}
    assert voice_map["hammer"].name in host_names


# ---------------------------------------------------------------------------
# Interrupt trigger call paths in producer loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_producer_calls_fire_interrupt_when_check_reactive_returns_spec(tmp_path):
    """check_reactive_triggers → InterruptSpec fires _fire_interrupt in the producer loop."""
    from mammamiradio.core.models import InterruptSpec

    state = _make_state()
    config = _make_config(tmp_path)
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    config.homeassistant.url = "http://ha.local:8123"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")

    mock_context = MagicMock()
    mock_context.summary = ""
    mock_context.events_summary = ""
    mock_context.events = []
    mock_context.raw_states = {}
    mock_context.mood = ""
    mock_context.weather_arc = ""
    mock_context.mood_en = ""
    mock_context.weather_arc_en = ""
    mock_context.events_summary_en = ""
    mock_context.last_event_label_en = ""

    interrupt_spec = InterruptSpec(directive="La pasta scotta!", urgency="pissed", cooldown=60)

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=([(host, "Allora!")], None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Bene...")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=mock_context),
        patch(f"{MODULE}.check_reactive_triggers", return_value=interrupt_spec),
        patch(f"{MODULE}._fire_interrupt", new_callable=AsyncMock) as mock_fire,
    ):
        await _run_until_queued(queue, state, config)

    mock_fire.assert_awaited_once()
    call_args = mock_fire.call_args
    assert call_args.args[1] is interrupt_spec


@pytest.mark.asyncio
async def test_producer_sets_ha_directive_when_check_reactive_returns_str(tmp_path):
    """check_reactive_triggers → str sets ha_pending_directive in the producer loop."""
    state = _make_state()
    config = _make_config(tmp_path)
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    config.homeassistant.url = "http://ha.local:8123"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")

    mock_context = MagicMock()
    mock_context.summary = ""
    mock_context.events_summary = ""
    mock_context.events = []
    mock_context.raw_states = {}
    mock_context.mood = ""
    mock_context.weather_arc = ""
    mock_context.mood_en = ""
    mock_context.weather_arc_en = ""
    mock_context.events_summary_en = ""
    mock_context.last_event_label_en = ""

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=([(host, "Allora!")], None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Bene...")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=mock_context),
        patch(f"{MODULE}.check_reactive_triggers", return_value="Cena pronta!"),
    ):
        await _run_until_queued(queue, state, config)

    assert state.ha_pending_directive == "Cena pronta!"


@pytest.mark.asyncio
async def test_timer_interrupt_poll_task_starts_when_configured(tmp_path):
    """Timer poll task is created and runs when timer_interrupts are configured."""
    from mammamiradio.core.config import TimerInterruptConfig

    state = _make_state()
    config = _make_config(tmp_path)
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    config.homeassistant.url = "http://ha.local:8123"
    config.homeassistant.timer_poll_interval = 1  # fast enough to start; test cancels before it fires
    config.homeassistant.timer_interrupts = [
        TimerInterruptConfig(
            entity_id="timer.pasta_timer",
            directive="La pasta scotta!",
            urgency="pissed",
            cooldown=60,
        )
    ]
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")

    mock_context = MagicMock()
    mock_context.summary = ""
    mock_context.events_summary = ""
    mock_context.events = []
    mock_context.raw_states = {}
    mock_context.mood = ""
    mock_context.weather_arc = ""
    mock_context.mood_en = ""
    mock_context.weather_arc_en = ""
    mock_context.events_summary_en = ""
    mock_context.last_event_label_en = ""

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock(), json=MagicMock(return_value=[])))
    mock_client.aclose = AsyncMock()

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=([(host, "Ciao!")], None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=mock_context),
        patch(f"{MODULE}.check_reactive_triggers", return_value=None),
        patch("mammamiradio.scheduling.producer.httpx.AsyncClient", return_value=mock_client),
    ):
        await _run_until_queued(queue, state, config)

    assert not queue.empty(), "Producer should queue a segment even when timer_interrupts are configured"


@pytest.mark.asyncio
async def test_timer_interrupt_poll_uses_wall_clock_timestamps(tmp_path):
    """Timer poll events must use wall-clock time so reactive age checks can see them."""
    from mammamiradio.core.config import TimerInterruptConfig

    state = _make_state()
    config = _make_config(tmp_path)
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    config.homeassistant.url = "http://ha.local:8123"
    config.homeassistant.timer_poll_interval = 0.01
    config.homeassistant.timer_interrupts = [
        TimerInterruptConfig(
            entity_id="timer.pasta_timer",
            directive="La pasta scotta!",
            urgency="pissed",
            cooldown=60,
        )
    ]
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")

    mock_context = MagicMock()
    mock_context.summary = ""
    mock_context.events_summary = ""
    mock_context.events = []
    mock_context.raw_states = {}
    mock_context.mood = ""
    mock_context.weather_arc = ""
    mock_context.mood_en = ""
    mock_context.weather_arc_en = ""
    mock_context.events_summary_en = ""
    mock_context.last_event_label_en = ""

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=MagicMock(raise_for_status=MagicMock(), json=MagicMock(return_value=[])))
    mock_client.aclose = AsyncMock()

    seen = asyncio.Event()
    captured_now: list[float | None] = []

    def _capture_diff_states(*_args, now=None, **_kwargs):
        captured_now.append(now)
        seen.set()
        return deque(maxlen=20)

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=([(host, "Ciao!")], None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora")),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=mock_context),
        patch(f"{MODULE}.check_reactive_triggers", return_value=None),
        patch("mammamiradio.home.ha_enrichment.diff_states", side_effect=_capture_diff_states),
        patch("mammamiradio.scheduling.producer.httpx.AsyncClient", return_value=mock_client),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.wait_for(seen.wait(), timeout=2.0)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    assert captured_now
    assert captured_now[0] is not None
    assert captured_now[0] > 1_000_000_000
