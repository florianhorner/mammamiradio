"""Unit tests for the producer pipeline in mammamiradio/scheduling/producer.py."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mammamiradio.audio.normalizer import save_track_metadata
from mammamiradio.core.config import RadioEventRule, load_config
from mammamiradio.core.listener_session import ListenerSession, ListenerSessionCueState
from mammamiradio.core.models import (
    ChaosSubtype,
    DialogueLine,
    GenerationWasteReason,
    HostPersonality,
    InterruptSpec,
    PlaylistSource,
    Segment,
    SegmentLogEntry,
    SegmentType,
    StationState,
    Track,
)
from mammamiradio.home.authorization import HomeAuthorization, HomeAuthorizationMode
from mammamiradio.home.evening_memory import EveningLedger
from mammamiradio.home.ha_enrichment import HomeEvent
from mammamiradio.home.radio_events import RadioEventMatch
from mammamiradio.home.ritual_recipes import clear_ritual_recipe_cooldowns, match_ritual_recipes
from mammamiradio.hosts.ad_creative import AdPart, AdScript, AdVoice, SonicWorld
from mammamiradio.hosts.memory_extractor import MemoryExtractionCommit
from mammamiradio.hosts.scriptwriter import (
    BanterCommit,
    CompanionshipBanterCommit,
    ListenerRequestCommit,
)
from mammamiradio.scheduling.producer import (
    SHAREWARE_CANNED_LIMIT,
    _apply_radio_event_matches,
    _apply_ritual_recipe_matches,
    _listener_truth_guard,
    _pick_canned_clip,
    run_producer,
)

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")
PRODUCER_MODULE = "mammamiradio.scheduling.producer"
SCRIPTWRITER_MODULE = "mammamiradio.hosts.scriptwriter"


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
    """Reset global state that leaks between tests."""
    from mammamiradio.scheduling import producer

    old_runway_floor = producer.RUNWAY_FLOOR_SECONDS
    producer.RUNWAY_FLOOR_SECONDS = 0
    yield
    producer._last_music_file = None
    producer._canned_clip_cache.clear()
    producer._recently_played_clips.clear()
    producer.RUNWAY_FLOOR_SECONDS = old_runway_floor


def _make_state() -> StationState:
    return StationState(
        playlist=[
            Track(title="Canzone Uno", artist="Artista", duration_ms=200_000, spotify_id="demo1"),
            Track(title="Canzone Due", artist="Artista", duration_ms=180_000, spotify_id="demo2"),
        ],
        listeners_active=1,  # simulate a live listener so the producer gate passes
        home_authorization=HomeAuthorization.legacy(),
    )


def _make_config():
    config = load_config(TOML_PATH)
    # Producer unit tests exercise Normal Mode unless a test opts into a
    # special mode explicitly.  Keep them deterministic when another test or
    # the invoking environment leaves the persisted festival flag enabled.
    config.party_mode = None
    config.pacing.lookahead_segments = 1
    config.homeassistant.enabled = False
    config.tmp_dir = Path("/tmp/mammamiradio_test")
    return config


def _manifest_recovery_clip(root: Path, name: str, payload: bytes, *, kind: str = "speech") -> Path:
    """Create one content-addressed speech or tone recovery fixture."""

    recovery = root / "recovery"
    recovery.mkdir(parents=True, exist_ok=True)
    clip = recovery / name
    clip.write_bytes(payload)
    manifest_path = root / "spoken_assets.json"
    assets: list[dict[str, str]] = []
    if manifest_path.is_file():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(existing.get("assets"), list):
            assets = [entry for entry in existing["assets"] if entry.get("path") != f"recovery/{name}"]
    assets.append(
        {
            "path": f"recovery/{name}",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "kind": kind,
            "language": "none" if kind == "tone" else "en",
            "transcript": "" if kind == "tone" else "The station stays on air.",
        }
    )
    manifest_path.write_text(json.dumps({"schema_version": 1, "assets": assets}), encoding="utf-8")
    return clip


def _fake_path(*_args, **_kwargs) -> Path:
    """Return a dummy Path that satisfies type checks."""
    return Path("/tmp/mammamiradio_test/fake.mp3")


async def _run_until_queued(queue: asyncio.Queue, state: StationState, config, timeout: float = 5.0):
    """Run the producer, waiting until at least one segment is queued, then cancel."""
    task = asyncio.create_task(run_producer(queue, state, config))
    try:
        # Poll until at least one segment appears
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


async def _run_until_queue_depth(
    queue: asyncio.Queue,
    state: StationState,
    config,
    depth: int,
    timeout: float = 5.0,
):
    """Run the producer until the queue reaches at least ``depth`` items, then cancel."""
    task = asyncio.create_task(run_producer(queue, state, config))
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while queue.qsize() < depth:
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError(f"Producer did not reach queue depth {depth} in time")
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Music segment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_music_segment_queued():
    """Producer queues a MUSIC segment when next_segment_type returns MUSIC."""
    state = _make_state()
    state.playlist[0].youtube_id = "yt_demo1"
    state.playlist[1].youtube_id = "yt_demo2"
    config = _make_config()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.normalize", side_effect=_fake_path),
        patch(f"{PRODUCER_MODULE}.shutil.copy2"),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=200.0),
    ):
        await _run_until_queued(queue, state, config)

    assert queue.qsize() >= 1
    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC
    assert "title" in seg.metadata
    assert seg.metadata["youtube_id"] in {"yt_demo1", "yt_demo2"}
    assert seg.duration_sec > 0


@pytest.mark.parametrize(
    "natural_type",
    [
        SegmentType.BANTER,
        SegmentType.AD,
        SegmentType.NEWS_FLASH,
        SegmentType.STATION_ID,
        SegmentType.TIME_CHECK,
    ],
)
@pytest.mark.asyncio
async def test_runway_governor_below_floor_airs_music(natural_type, tmp_path):
    """Natural non-music picks wait when the real ready-audio queue is thin."""
    state = _make_state()
    state.playlist[0].youtube_id = "yt_demo1"
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    config.pacing.lookahead_segments = 4
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    source_audio = tmp_path / "source.mp3"
    source_audio.write_bytes(b"\x00" * 2048)

    def fake_normalize(_src: Path, dst: Path, *_args, **_kwargs) -> None:
        dst.write_bytes(b"\x00" * 2048)

    with (
        patch(f"{PRODUCER_MODULE}.RUNWAY_FLOOR_SECONDS", 240),
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=natural_type),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=source_audio),
        patch(f"{PRODUCER_MODULE}.normalize", side_effect=fake_normalize),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=200.0),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC
    assert seg.duration_sec > 0


@pytest.mark.asyncio
async def test_runway_governor_boundary_allows_due_banter(tmp_path):
    """Exactly at the floor is enough runway; the check is strictly below."""
    state = _make_state()
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    config.pacing.lookahead_segments = 2
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    runway = tmp_path / "runway.mp3"
    runway.write_bytes(b"\x00" * 2048)
    queue.put_nowait(Segment(type=SegmentType.MUSIC, path=runway, duration_sec=240.0, metadata={}, ephemeral=False))
    host = config.hosts[0]
    banter_lines = [(host, "La pista è pronta.")]

    with (
        patch(f"{PRODUCER_MODULE}.RUNWAY_FLOOR_SECONDS", 240),
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...", None)
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queue_depth(queue, state, config, 2)

    queue.get_nowait()
    seg = queue.get_nowait()
    assert seg.type == SegmentType.BANTER


@pytest.mark.asyncio
async def test_runway_governor_fills_beyond_lookahead_when_floor_is_reachable(tmp_path):
    """At lookahead count but below the floor, spare queue capacity is used for music runway."""
    state = _make_state()
    state.playlist[0].youtube_id = "yt_demo1"
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    config.pacing.lookahead_segments = 2
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    for idx in range(2):
        path = tmp_path / f"runway_{idx}.mp3"
        path.write_bytes(b"\x00" * 2048)
        queue.put_nowait(Segment(type=SegmentType.MUSIC, path=path, duration_sec=79.0, metadata={}, ephemeral=False))
    source_audio = tmp_path / "source.mp3"
    source_audio.write_bytes(b"\x00" * 2048)

    def fake_normalize(_src: Path, dst: Path, *_args, **_kwargs) -> None:
        dst.write_bytes(b"\x00" * 2048)

    with (
        patch(f"{PRODUCER_MODULE}.RUNWAY_FLOOR_SECONDS", 240),
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=source_audio),
        patch(f"{PRODUCER_MODULE}.normalize", side_effect=fake_normalize),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=200.0),
    ):
        await _run_until_queue_depth(queue, state, config, 3)

    for _ in range(2):
        assert queue.get_nowait().type == SegmentType.MUSIC
    assert queue.get_nowait().type == SegmentType.MUSIC


@pytest.mark.asyncio
async def test_runway_governor_allows_optional_when_observable_queue_is_full(tmp_path):
    """Short tracks below 240s still allow speech once the producer cannot observe more runway."""
    state = _make_state()
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    config.pacing.lookahead_segments = 4
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    for idx in range(3):
        path = tmp_path / f"runway_{idx}.mp3"
        path.write_bytes(b"\x00" * 2048)
        queue.put_nowait(Segment(type=SegmentType.MUSIC, path=path, duration_sec=79.0, metadata={}, ephemeral=False))
    host = config.hosts[0]
    banter_lines = [(host, "La pista e corta ma stabile.")]

    with (
        patch(f"{PRODUCER_MODULE}.RUNWAY_FLOOR_SECONDS", 240),
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...", None)
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queue_depth(queue, state, config, 4)

    for _ in range(3):
        assert queue.get_nowait().type == SegmentType.MUSIC
    assert queue.get_nowait().type == SegmentType.BANTER


@pytest.mark.asyncio
async def test_runway_governor_defers_banter_without_resetting_counter(tmp_path):
    """A governed host break waits; it is not silently dropped."""
    state = _make_state()
    state.segments_produced = 2
    state.songs_since_banter = 2
    state.songs_since_ad = 0
    state.playlist[0].youtube_id = "yt_demo1"
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    config.pacing.songs_between_banter = 2
    config.pacing.songs_between_ads = 99
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    source_audio = tmp_path / "source.mp3"
    source_audio.write_bytes(b"\x00" * 2048)

    def fake_normalize(_src: Path, dst: Path, *_args, **_kwargs) -> None:
        dst.write_bytes(b"\x00" * 2048)

    with (
        patch(f"{PRODUCER_MODULE}.RUNWAY_FLOOR_SECONDS", 240),
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=source_audio),
        patch(f"{PRODUCER_MODULE}.normalize", side_effect=fake_normalize),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert queue.get_nowait().type == SegmentType.MUSIC
    assert state.songs_since_banter >= 3
    assert state.songs_since_banter >= config.pacing.songs_between_banter


@pytest.mark.asyncio
async def test_runway_governor_exempts_operator_force(tmp_path):
    """Operator-forced speech bypasses the natural-pacing governor."""
    state = _make_state()
    state.force_next = SegmentType.BANTER
    state.operator_force_pending = SegmentType.BANTER
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    banter_lines = [(host, "Subito in onda.")]

    with (
        patch(f"{PRODUCER_MODULE}.RUNWAY_FLOOR_SECONDS", 240),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...", None)
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert queue.get_nowait().type == SegmentType.BANTER


@pytest.mark.asyncio
async def test_runway_governor_empty_playlist_falls_back_to_banter(tmp_path):
    """If the governor asks for music but no pool exists, the existing guard keeps audio moving."""
    state = StationState(playlist=[], listeners_active=1)
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    banter_lines = [(host, "Restiamo in onda.")]

    with (
        patch(f"{PRODUCER_MODULE}.RUNWAY_FLOOR_SECONDS", 240),
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...", None)
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config, timeout=2.0)

    assert queue.get_nowait().type == SegmentType.BANTER


def test_producer_buffered_seconds_uses_real_queue_and_fails_safe():
    from mammamiradio.scheduling import producer

    queue: asyncio.Queue[Segment] = asyncio.Queue()
    assert producer._producer_buffered_seconds(queue) == 0.0
    queue.put_nowait(Segment(type=SegmentType.MUSIC, path=Path("/tmp/a.mp3"), duration_sec=0.0))
    queue.put_nowait(Segment(type=SegmentType.MUSIC, path=Path("/tmp/b.mp3"), duration_sec=12.24))
    assert producer._producer_buffered_seconds(queue) == 12.2

    class QueueWithoutInternal:
        pass

    assert producer._producer_buffered_seconds(QueueWithoutInternal()) == 0.0  # type: ignore[arg-type]


def test_runway_governor_defers_until_observable_queue_is_full():
    from mammamiradio.scheduling import producer

    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    queue.put_nowait(Segment(type=SegmentType.MUSIC, path=Path("/tmp/a.mp3"), duration_sec=79.0))
    with patch.object(producer, "RUNWAY_FLOOR_SECONDS", 240):
        should_defer, buffered = producer._should_defer_for_runway(queue, lookahead_segments=4)
        assert should_defer is True
        assert buffered == 79.0

        queue.put_nowait(Segment(type=SegmentType.MUSIC, path=Path("/tmp/b.mp3"), duration_sec=79.0))
        queue.put_nowait(Segment(type=SegmentType.MUSIC, path=Path("/tmp/c.mp3"), duration_sec=79.0))
        should_defer, buffered = producer._should_defer_for_runway(queue, lookahead_segments=4)
        assert should_defer is False
        assert buffered == 237.0


def test_runway_fill_needed_uses_extra_queue_capacity():
    from mammamiradio.scheduling import producer

    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    queue.put_nowait(Segment(type=SegmentType.MUSIC, path=Path("/tmp/a.mp3"), duration_sec=79.0))
    queue.put_nowait(Segment(type=SegmentType.MUSIC, path=Path("/tmp/b.mp3"), duration_sec=79.0))

    with patch.object(producer, "RUNWAY_FLOOR_SECONDS", 240):
        assert producer._runway_fill_needed(queue) is True
        queue.put_nowait(Segment(type=SegmentType.MUSIC, path=Path("/tmp/c.mp3"), duration_sec=79.0))
        queue.put_nowait(Segment(type=SegmentType.MUSIC, path=Path("/tmp/d.mp3"), duration_sec=1.0))
        assert producer._runway_fill_needed(queue) is False


def test_runway_governor_allows_when_floor_is_unobservable():
    from mammamiradio.scheduling import producer

    queue: asyncio.Queue[Segment] = asyncio.Queue()
    with patch.object(producer, "RUNWAY_FLOOR_SECONDS", 240):
        should_defer, buffered = producer._should_defer_for_runway(queue, lookahead_segments=1)
    assert should_defer is False
    assert buffered == 0.0


def test_runway_governed_types_are_exactly_natural_non_music_renders():
    from mammamiradio.scheduling import producer

    assert {
        SegmentType.BANTER,
        SegmentType.AD,
        SegmentType.NEWS_FLASH,
        SegmentType.STATION_ID,
        SegmentType.TIME_CHECK,
    } == producer._RUNWAY_GOVERNED_TYPES
    assert SegmentType.MUSIC not in producer._RUNWAY_GOVERNED_TYPES


# ---------------------------------------------------------------------------
# Banter segment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_banter_segment_queued():
    """Producer queues a BANTER segment with synthesized dialogue."""
    state = _make_state()
    config = _make_config()
    config.anthropic_api_key = "test-key"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")
    banter_lines = [(host, "Che bella giornata!")]

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...", None)
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert queue.qsize() >= 1
    seg = queue.get_nowait()
    assert seg.type == SegmentType.BANTER
    assert seg.metadata.get("type") == "banter"
    assert list(state.recent_banter_paths) == []


@pytest.mark.asyncio
async def test_banter_segment_carries_memory_extraction_with_final_script():
    state = _make_state()
    config = _make_config()
    config.anthropic_api_key = "test-key"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")
    commit = BanterCommit(
        memory_extraction=MemoryExtractionCommit(
            script_lines=[{"host": host.name, "text": "draft"}],
            persona_context="existing",
            interaction_context={"listener_request": "none"},
            youtube_id="yt-memory",
            source_session=2,
        )
    )

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            return_value=([(host, "Dedicato a te!")], commit),
        ),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...", None)
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    payload = seg.metadata["memory_extraction"]
    assert payload["script_lines"] == [
        {"host": host.name, "text": "Allora...", "type": "transition"},
        {"host": host.name, "text": "Dedicato a te!"},
    ]
    assert payload["youtube_id"] == "yt-memory"
    assert payload["source_session"] == 2


@pytest.mark.asyncio
async def test_station_id_uses_host_engine_when_sweeper_voice_is_host_based():
    state = _make_state()
    config = _make_config()
    config.sonic_brand.sweeper_voice = ""
    host = config.hosts[0]
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.STATION_ID),
        patch(f"{PRODUCER_MODULE}.random.choice", return_value=host),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()) as mock_synthesize,
        patch(f"{PRODUCER_MODULE}.generate_station_id_bed", side_effect=_fake_path),
        patch(f"{PRODUCER_MODULE}.mix_voice_with_sting", side_effect=_fake_path),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.STATION_ID
    kwargs = mock_synthesize.call_args.kwargs
    assert kwargs["engine"] == host.engine
    assert kwargs["edge_fallback_voice"] == host.edge_fallback_voice
    assert kwargs["elevenlabs_model"] == host.elevenlabs_model
    assert kwargs["delivery_profile"] == host.delivery_profile
    assert "delivery_cue" not in kwargs


@pytest.mark.asyncio
async def test_station_id_uses_configured_sweeper_engine():
    state = _make_state()
    config = _make_config()
    config.identity.station_name = "Radio Test"
    config.sonic_brand.full_ident = ""
    config.sonic_brand.sweeper_voice = "marin"
    config.sonic_brand.sweeper_engine = "openai"
    config.sonic_brand.sweeper_edge_fallback_voice = "it-IT-GiuseppeMultilingualNeural"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.STATION_ID),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()) as mock_synthesize,
        patch(f"{PRODUCER_MODULE}.generate_station_id_bed", side_effect=_fake_path),
        patch(f"{PRODUCER_MODULE}.mix_voice_with_sting", side_effect=_fake_path),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.STATION_ID
    assert mock_synthesize.call_args.args[0] == "Radio Test"
    kwargs = mock_synthesize.call_args.kwargs
    assert kwargs["engine"] == "openai"
    assert kwargs["edge_fallback_voice"] == "it-IT-GiuseppeMultilingualNeural"


@pytest.mark.asyncio
async def test_sweeper_uses_configured_sweeper_engine():
    state = _make_state()
    config = _make_config()
    config.identity.station_name = "Radio Test"
    config.sonic_brand.sweeper_voice = "marin"
    config.sonic_brand.sweeper_engine = "openai"
    config.sonic_brand.sweeper_edge_fallback_voice = "it-IT-GiuseppeMultilingualNeural"
    config.sonic_brand.sweepers = []
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    imaging = MagicMock()
    imaging.pick_sweeper_sting.side_effect = _fake_path

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.SWEEPER),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()) as mock_synthesize,
        patch(f"{PRODUCER_MODULE}._make_imaging_lib", return_value=imaging),
        patch(f"{PRODUCER_MODULE}.mix_voice_with_sting", side_effect=_fake_path),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.SWEEPER
    assert mock_synthesize.call_args.args[0] == "Radio Test"
    kwargs = mock_synthesize.call_args.kwargs
    assert kwargs["engine"] == "openai"
    assert kwargs["edge_fallback_voice"] == "it-IT-GiuseppeMultilingualNeural"


@pytest.mark.asyncio
async def test_recovery_sweeper_segment_renders_tts_with_sting(tmp_path):
    from mammamiradio.scheduling import producer

    state = _make_state()
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    config.sonic_brand.sweeper_voice = "marin"
    config.sonic_brand.sweeper_engine = "openai"
    config.sonic_brand.sweeper_edge_fallback_voice = "it-IT-GiuseppeMultilingualNeural"
    line = "{station} resta in onda."
    imaging = MagicMock()

    def _write_sting(output_path: Path) -> Path:
        output_path.write_bytes(b"sting")
        return output_path

    def _mix_sweeper(_voice_path: Path, _sting_path: Path, output_path: Path) -> Path:
        output_path.write_bytes(b"mixed")
        return output_path

    async def _write_voice(_text: str, _voice: str, output_path: Path, **_kwargs) -> None:
        output_path.write_bytes(b"voice")

    imaging.pick_sweeper_sting.side_effect = _write_sting
    with (
        patch(f"{PRODUCER_MODULE}.random.choice", return_value=line),
        patch(f"{PRODUCER_MODULE}._make_imaging_lib", return_value=imaging),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=_write_voice) as mock_synthesize,
        patch(f"{PRODUCER_MODULE}.mix_voice_with_sting", side_effect=_mix_sweeper) as mock_mix,
    ):
        segment = await producer._build_recovery_sweeper_segment(config, state)

    assert segment.type == SegmentType.SWEEPER
    assert segment.ephemeral is True
    assert segment.path.name.startswith("recovery_sweeper_mixed_")
    assert segment.path.read_bytes() == b"mixed"
    assert segment.metadata == {
        "type": "sweeper",
        "text": "Mamma Mi Radio resta in onda.",
        "title": "Recovery sweeper",
        "error_recovery": True,
        "rescue": True,
    }
    mock_synthesize.assert_awaited_once()
    assert mock_synthesize.call_args.args[:2] == ("Mamma Mi Radio resta in onda.", "marin")
    assert mock_synthesize.call_args.kwargs["engine"] == "openai"
    assert mock_synthesize.call_args.kwargs["edge_fallback_voice"] == "it-IT-GiuseppeMultilingualNeural"
    assert mock_synthesize.call_args.kwargs["state"] is state
    imaging.pick_sweeper_sting.assert_called_once()
    mock_mix.assert_called_once()


@pytest.mark.asyncio
async def test_time_check_uses_host_engine_for_tts():
    state = _make_state()
    config = _make_config()
    config.identity.station_name = "Radio Test"
    host = config.hosts[0]
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.TIME_CHECK),
        patch(f"{PRODUCER_MODULE}.random.choice", return_value=host),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()) as mock_synthesize,
        patch(f"{PRODUCER_MODULE}.generate_tone", side_effect=_fake_path),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_fake_path),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.TIME_CHECK
    assert "Radio Test" in mock_synthesize.call_args.args[0]
    kwargs = mock_synthesize.call_args.kwargs
    assert kwargs["engine"] == host.engine
    assert kwargs["edge_fallback_voice"] == host.edge_fallback_voice
    assert kwargs["elevenlabs_model"] == host.elevenlabs_model
    assert kwargs["delivery_profile"] == host.delivery_profile
    assert "delivery_cue" not in kwargs


@pytest.mark.asyncio
async def test_ad_promo_tag_uses_configured_ad_voice_engine():
    """The promo tag must not pass a cloud ad voice ID to edge-tts."""
    state = _make_state()
    config = _make_config()
    config.pacing.ad_spots_per_break = 1
    config.ads.voices = [
        AdVoice(
            name="L'Annunciatore",
            voice="elevenlabs-voice-id",
            engine="elevenlabs",
            edge_fallback_voice="it-IT-DiegoNeural",
            style="classic",
            role="hammer",
        )
    ]
    host = config.hosts[0]
    script = AdScript(
        brand=config.ads.brands[0].name,
        parts=[AdPart(type="voice", text="Compra subito.", role="hammer")],
        summary="Promo",
        format="classic_pitch",
        sonic=SonicWorld(),
        roles_used=["hammer"],
    )
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    async def _same_intro_path(path, *_args, **_kwargs):
        return path

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.AD),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Pubblicita.", None)
        ),
        patch(f"{SCRIPTWRITER_MODULE}.write_ad", new_callable=AsyncMock, return_value=script),
        patch(
            f"{PRODUCER_MODULE}._select_ad_creative",
            return_value=("classic_pitch", SonicWorld(), ["hammer"]),
        ),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, side_effect=_same_intro_path),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()) as mock_synthesize,
        patch(f"{PRODUCER_MODULE}.synthesize_ad", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.generate_bumper_jingle", side_effect=_fake_path),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_fake_path),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.AD
    # Normal Mode is English-led even though this station's identity language
    # is Italian.  The promo tag is selected through the shared fallback seam.
    promo_call = next(
        call for call in mock_synthesize.await_args_list if call.args[0] == "A word from our sponsors, amici."
    )
    assert promo_call.args[1] == "elevenlabs-voice-id"
    assert promo_call.kwargs["engine"] == "elevenlabs"
    assert promo_call.kwargs["edge_fallback_voice"] == "it-IT-DiegoNeural"


# ---------------------------------------------------------------------------
# Error recovery — recovery sweeper fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_recovery_queues_recovery_sweeper():
    """When download_track raises, producer inserts a recovery sweeper before emergency tone."""
    state = _make_state()
    config = _make_config()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    recovery = Segment(
        type=SegmentType.SWEEPER,
        path=_fake_path(),
        metadata={"type": "sweeper", "rescue": True, "error_recovery": True, "title": "Recovery sweeper"},
    )

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=RuntimeError("network down")),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
        patch(f"{PRODUCER_MODULE}.select_norm_cache_rescue", return_value=None),
        patch(f"{PRODUCER_MODULE}._build_recovery_sweeper_segment", new_callable=AsyncMock, return_value=recovery),
        patch(
            f"{PRODUCER_MODULE}.generate_silence",
            side_effect=AssertionError("silence should never be a producer recovery fallback"),
            create=True,
        ) as mock_silence,
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert queue.qsize() >= 1
    seg = queue.get_nowait()
    mock_silence.assert_not_called()
    assert seg.type == SegmentType.SWEEPER
    assert seg.metadata.get("title") == "Recovery sweeper"
    assert seg.metadata.get("rescue") is True
    assert state.failed_segments >= 1


@pytest.mark.asyncio
async def test_unavailable_marker_exhaustion_queues_recovery_sweeper(tmp_path):
    """A tiny unavailable marker must enter recovery once it denies the final track."""
    from mammamiradio.playlist import downloader
    from mammamiradio.playlist.downloader import clear_rejected_cache_keys, is_rejected_cache_key

    state = _make_state()
    state.playlist = state.playlist[:1]
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    marker = tmp_path / f"_failed_{state.playlist[0].cache_key}.mp3"
    marker.write_text("yt-dlp unavailable")
    recovery = Segment(
        type=SegmentType.SWEEPER,
        path=_fake_path(),
        metadata={"type": "sweeper", "rescue": True, "error_recovery": True, "title": "Recovery sweeper"},
    )

    clear_rejected_cache_keys()
    try:
        with (
            patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
            patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=marker),
            patch(f"{PRODUCER_MODULE}.validate_download", wraps=downloader.validate_download),
            patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
            patch(f"{PRODUCER_MODULE}.select_norm_cache_rescue", return_value=None),
            patch(f"{PRODUCER_MODULE}._get_last_music_file", return_value=None),
            patch(f"{PRODUCER_MODULE}._build_recovery_sweeper_segment", new_callable=AsyncMock, return_value=recovery),
            patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        ):
            await _run_until_queued(queue, state, config)

        assert is_rejected_cache_key(state.playlist[0].cache_key)
        segment = queue.get_nowait()
        assert segment is recovery
        assert segment.metadata["rescue"] is True
    finally:
        clear_rejected_cache_keys()


@pytest.mark.asyncio
async def test_source_switch_discards_stale_music_segment(tmp_path):
    """A source switch should invalidate any in-flight music from the previous playlist."""
    old_track = Track(title="Old Song", artist="Old Artist", duration_ms=200_000, spotify_id="old1")
    new_track = Track(title="New Song", artist="New Artist", duration_ms=200_000, spotify_id="new1")
    state = StationState(playlist=[old_track], listeners_active=1)
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    first_download_started = asyncio.Event()
    allow_first_download = asyncio.Event()
    second_download_started = asyncio.Event()
    source_audio = tmp_path / "source.mp3"
    source_audio.write_bytes(b"fake audio")
    download_calls = 0

    async def fake_download(track, cache_dir, music_dir=None, **_kwargs):
        nonlocal download_calls
        download_calls += 1
        if download_calls == 1:
            assert track == old_track
            first_download_started.set()
            await allow_first_download.wait()
            return source_audio
        assert track == new_track
        second_download_started.set()
        await asyncio.Event().wait()

    def fake_normalize(src: Path, dst: Path, *_args, **_kwargs) -> None:
        dst.write_bytes(Path(src).read_bytes())

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=fake_download),
        patch(f"{PRODUCER_MODULE}.normalize", side_effect=fake_normalize),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.wait_for(first_download_started.wait(), timeout=1.0)
            state.switch_playlist(
                [new_track],
                PlaylistSource(kind="playlist", source_id="new", label="New Source"),
            )
            allow_first_download.set()
            await asyncio.wait_for(second_download_started.wait(), timeout=1.0)
            assert queue.empty()
            assert len(state.played_tracks) == 0
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_stopped_session_discards_finished_segment_without_advancing_state(tmp_path):
    """A segment finished after /stop should be dropped without mutating playback state."""
    track = Track(title="Late Song", artist="Late Artist", duration_ms=200_000, spotify_id="late1")
    state = StationState(playlist=[track], listeners_active=1)
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    download_started = asyncio.Event()
    allow_download_finish = asyncio.Event()
    source_audio = tmp_path / "source.mp3"
    source_audio.write_bytes(b"fake audio")

    async def fake_download(*_args, **_kwargs):
        download_started.set()
        await allow_download_finish.wait()
        return source_audio

    def fake_normalize(src: Path, dst: Path, *_args, **_kwargs) -> None:
        dst.write_bytes(Path(src).read_bytes())

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=fake_download),
        patch(f"{PRODUCER_MODULE}.normalize", side_effect=fake_normalize),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.wait_for(download_started.wait(), timeout=1.0)
            state.session_stopped = True
            allow_download_finish.set()
            await asyncio.sleep(0.1)
            assert queue.empty()
            assert len(state.played_tracks) == 0
            assert state.segments_produced == 0
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_stopped_session_remains_stopped_until_resume(tmp_path):
    """Producer should not clear a stopped session on its own."""
    state = _make_state()
    state.session_stopped = True
    state.now_streaming = {
        "type": "stopped",
        "label": "Session stopped",
        "started": time.time() - 60.0,
    }
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    source_audio = tmp_path / "source.mp3"
    source_audio.write_bytes(b"fake audio")

    def fake_normalize(src: Path, dst: Path, *_args, **_kwargs) -> None:
        dst.write_bytes(Path(src).read_bytes())

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=source_audio),
        patch(f"{PRODUCER_MODULE}.normalize", side_effect=fake_normalize),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(1.2)
            assert state.session_stopped is True
            assert state.now_streaming["type"] == "stopped"
            assert queue.empty()
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_chart_refresh_waits_full_interval_after_startup():
    """Producer must not trigger chart refresh immediately after startup."""
    state = _make_state()
    state.playlist_source = PlaylistSource(kind="charts", source_id="it", label="Italian charts")
    config = _make_config()
    config.pacing.lookahead_segments = 1
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    await queue.put(
        Segment(
            type=SegmentType.BANTER,
            path=Path("/tmp/mammamiradio_test/seed.mp3"),
            metadata={"type": "banter"},
        )
    )

    fake_loop = MagicMock()
    fake_loop.time.return_value = 10_000.0

    with (
        patch(f"{PRODUCER_MODULE}.asyncio.get_running_loop", return_value=fake_loop),
        patch(f"{PRODUCER_MODULE}.fetch_chart_refresh", return_value=[]) as mock_refresh,
        patch(f"{PRODUCER_MODULE}.evict_cache_lru"),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.1)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    mock_refresh.assert_not_called()


def test_cache_eviction_protects_capacity_exempt_continuity_slot(tmp_path):
    """The eviction protection set includes the out-of-band continuity slot.

    Verifies the protection contract directly through the pure helper the
    producer loop uses, rather than racing a wall clock against `run_producer`
    reaching its eviction branch — that integration race was a CI-only flake
    (evict "not called" / timeout). The loop's call site stays covered by the
    full-loop producer tests.
    """
    from mammamiradio.scheduling.producer import _cache_eviction_protected_paths

    state = _make_state()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    queued_path = tmp_path / "norm_queued_192k.mp3"
    queued_path.write_bytes(b"queued")
    slot_path = tmp_path / "norm_slot_192k.mp3"
    slot_path.write_bytes(b"slot")
    queue.put_nowait(
        Segment(type=SegmentType.MUSIC, path=queued_path, duration_sec=300.0, metadata={"title": "Queued"})
    )
    state.continuity_slot = Segment(
        type=SegmentType.MUSIC,
        path=slot_path,
        duration_sec=180.0,
        metadata={"continuity_reservation": True},
        ephemeral=False,
    )

    protected = _cache_eviction_protected_paths(queue, state)

    assert queued_path in protected
    assert slot_path in protected
    # No slot reserved -> only the real queue is protected.
    state.continuity_slot = None
    assert _cache_eviction_protected_paths(queue, state) == {queued_path}


# ---------------------------------------------------------------------------
# Shareware trial: canned clip limit
# ---------------------------------------------------------------------------


def test_pick_canned_clip_requires_each_kind_to_be_manifested(tmp_path):
    """A recovery manifest never implicitly revives banter or welcome assets."""
    from mammamiradio.scheduling import producer

    recovery = _manifest_recovery_clip(tmp_path, "continuity.mp3", b"reviewed" * 300)

    # Temporarily override demo assets dir and clear caches
    orig = producer._DEMO_ASSETS_DIR
    producer._DEMO_ASSETS_DIR = tmp_path
    producer._recently_played_clips.clear()
    producer._canned_clip_cache.clear()

    try:
        state = StationState()

        assert _pick_canned_clip("recovery", state=state) == recovery
        assert _pick_canned_clip("banter", state=state) is None
        assert _pick_canned_clip("welcome", state=state) is None

        # The approved recovery rung is a safety asset, not shareware banter.
        state.canned_clips_streamed = SHAREWARE_CANNED_LIMIT
        assert _pick_canned_clip("banter", state=state) is None
        assert _pick_canned_clip("welcome", state=state) is None
    finally:
        producer._DEMO_ASSETS_DIR = orig
        producer._recently_played_clips.clear()
        producer._canned_clip_cache.clear()


def test_pick_canned_clip_allows_only_manifested_truth_safe_banter(tmp_path):
    """Neutral canned banter is eligible only through the reviewed manifest."""
    from mammamiradio.scheduling import producer

    banter_dir = tmp_path / "banter"
    banter_dir.mkdir()
    payload = b"reviewed neutral banter" * 200
    clip = banter_dir / "neutral.mp3"
    clip.write_bytes(payload)
    (tmp_path / "spoken_assets.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "assets": [
                    {
                        "path": "banter/neutral.mp3",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "kind": "speech",
                        "language": "en",
                        "transcript": "The music keeps the room warm.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    original_root = producer._DEMO_ASSETS_DIR
    producer._DEMO_ASSETS_DIR = tmp_path
    producer._canned_clip_cache.clear()
    producer._recently_played_clips.clear()
    try:
        state = StationState()
        assert _pick_canned_clip("banter", state=state) == clip
        state.canned_clips_streamed = SHAREWARE_CANNED_LIMIT
        assert _pick_canned_clip("banter", state=state) is None
        assert _pick_canned_clip("welcome", state=StationState()) is None
    finally:
        producer._DEMO_ASSETS_DIR = original_root
        producer._canned_clip_cache.clear()
        producer._recently_played_clips.clear()


def test_pick_canned_clip_returns_none_when_dir_missing(tmp_path):
    """_pick_canned_clip returns None when the banter subdirectory does not exist."""
    from mammamiradio.scheduling import producer

    orig = producer._DEMO_ASSETS_DIR
    producer._DEMO_ASSETS_DIR = tmp_path
    producer._canned_clip_cache.clear()
    producer._recently_played_clips.clear()

    try:
        result = _pick_canned_clip("banter", state=StationState())
        assert result is None
    finally:
        producer._DEMO_ASSETS_DIR = orig
        producer._canned_clip_cache.clear()
        producer._recently_played_clips.clear()


def test_pick_canned_clip_returns_none_when_dir_empty(tmp_path):
    """_pick_canned_clip returns None when banter/ exists but contains no .mp3 files."""
    from mammamiradio.scheduling import producer

    (tmp_path / "banter").mkdir()

    orig = producer._DEMO_ASSETS_DIR
    producer._DEMO_ASSETS_DIR = tmp_path
    producer._canned_clip_cache.clear()
    producer._recently_played_clips.clear()

    try:
        result = _pick_canned_clip("banter", state=StationState())
        assert result is None
        assert producer._canned_clip_cache["banter"] == []
    finally:
        producer._DEMO_ASSETS_DIR = orig
        producer._canned_clip_cache.clear()
        producer._recently_played_clips.clear()


def test_pick_canned_clip_returns_none_when_recovery_dir_empty(tmp_path):
    """_pick_canned_clip returns None when recovery/ exists but has no .mp3 files."""
    from mammamiradio.scheduling import producer

    (tmp_path / "recovery").mkdir()

    orig = producer._DEMO_ASSETS_DIR
    producer._DEMO_ASSETS_DIR = tmp_path
    producer._canned_clip_cache.clear()
    producer._recently_played_clips.clear()

    try:
        result = _pick_canned_clip("recovery", state=StationState())
        assert result is None
        assert producer._canned_clip_cache.get("recovery") == []
    finally:
        producer._DEMO_ASSETS_DIR = orig
        producer._canned_clip_cache.clear()
        producer._recently_played_clips.clear()


@pytest.mark.asyncio
async def test_error_recovery_inserts_recovery_sweeper_when_no_canned_clips(tmp_path):
    """Outer run_producer handler falls through to recovery sweeper when demo_assets/ is empty.

    When produce_one_segment raises (music download fails), the outer handler
    tries packaged recovery clips, then norm-cache music, then synthesizes a
    branded recovery sweeper before the emergency tone last resort.

    Note: banter TTS failures use `continue` internally and never reach the outer
    handler. This test uses MUSIC (whose failures propagate out) to exercise the
    outer recovery path with no canned-clip safety net.
    """
    from mammamiradio.scheduling import producer

    (tmp_path / "recovery").mkdir()
    (tmp_path / "banter").mkdir()
    (tmp_path / "welcome").mkdir()

    state = _make_state()
    config = _make_config()
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    recovery = Segment(
        type=SegmentType.SWEEPER,
        path=_fake_path(),
        metadata={"type": "sweeper", "rescue": True, "error_recovery": True, "title": "Recovery sweeper"},
    )

    orig = producer._DEMO_ASSETS_DIR
    producer._DEMO_ASSETS_DIR = tmp_path
    producer._canned_clip_cache.clear()
    producer._recently_played_clips.clear()

    try:
        with (
            patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
            patch(
                f"{PRODUCER_MODULE}.download_track",
                new_callable=AsyncMock,
                side_effect=RuntimeError("network down"),
            ),
            patch(f"{PRODUCER_MODULE}.select_norm_cache_rescue", return_value=None),
            patch(f"{PRODUCER_MODULE}._build_recovery_sweeper_segment", new_callable=AsyncMock, return_value=recovery),
            patch(
                f"{PRODUCER_MODULE}.generate_silence",
                side_effect=AssertionError("silence should never be a producer recovery fallback"),
                create=True,
            ) as mock_silence,
            patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        ):
            await _run_until_queued(queue, state, config)
    finally:
        producer._DEMO_ASSETS_DIR = orig
        producer._canned_clip_cache.clear()
        producer._recently_played_clips.clear()

    assert queue.qsize() >= 1
    seg = queue.get_nowait()
    mock_silence.assert_not_called()
    assert seg.type == SegmentType.SWEEPER
    assert seg.metadata.get("title") == "Recovery sweeper"
    assert seg.metadata.get("rescue") is True


@pytest.mark.asyncio
async def test_error_recovery_logs_missing_recovery_assets_and_uses_recovery_sweeper(caplog):
    """When approved recovery clips are unavailable, producer logs and inserts a sweeper."""
    from mammamiradio.scheduling import producer

    state = _make_state()
    config = _make_config()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    picked_subdirs: list[str] = []
    recovery = Segment(
        type=SegmentType.SWEEPER,
        path=_fake_path(),
        metadata={"type": "sweeper", "rescue": True, "error_recovery": True, "title": "Recovery sweeper"},
    )

    def _pick_none(subdir: str, state=None):
        picked_subdirs.append(subdir)
        return None

    caplog.set_level(logging.WARNING, logger="mammamiradio.scheduling.producer")
    orig_last_music = producer._last_music_file
    producer._last_music_file = None
    try:
        with (
            patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
            patch(
                f"{PRODUCER_MODULE}.download_track",
                new_callable=AsyncMock,
                side_effect=RuntimeError("network down"),
            ),
            patch(f"{PRODUCER_MODULE}._pick_canned_clip", side_effect=_pick_none),
            patch(f"{PRODUCER_MODULE}.select_norm_cache_rescue", return_value=None),
            patch(f"{PRODUCER_MODULE}._build_recovery_sweeper_segment", new_callable=AsyncMock, return_value=recovery),
            patch(
                f"{PRODUCER_MODULE}.generate_silence",
                side_effect=AssertionError("silence should never be a producer recovery fallback"),
                create=True,
            ) as mock_silence,
            patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        ):
            await _run_until_queued(queue, state, config)
    finally:
        producer._last_music_file = orig_last_music

    assert picked_subdirs == ["recovery"]
    assert any(
        "No packaged recovery clips, norm cache, or last-known-good music available" in record.message
        for record in caplog.records
    )
    mock_silence.assert_not_called()


# ---------------------------------------------------------------------------
# Persona feedback loop in producer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_motif_persists_to_persona(tmp_path):
    """_record_motif stores the played track as a motif in the persona store."""
    from mammamiradio.core.sync import init_db
    from mammamiradio.hosts.persona import PersonaStore
    from mammamiradio.scheduling.producer import _record_motif

    db_path = tmp_path / "persona.db"
    init_db(db_path)
    store = PersonaStore(db_path)
    # Seed the persona row
    await store.update_persona({})

    state = StationState()
    state.persona_store = store

    track = Track(title="Volare", artist="Domenico Modugno", duration_ms=200_000)
    await _record_motif(state, track)

    persona = await store.get_persona()
    assert "Domenico Modugno – Volare" in persona.motifs


@pytest.mark.asyncio
async def test_listener_session_receipt_increments_count(tmp_path):
    """The producer drains one pending station epoch into PersonaStore."""
    from mammamiradio.core.sync import init_db
    from mammamiradio.hosts.persona import PersonaStore
    from mammamiradio.scheduling.producer import _sync_listener_session_persona

    db_path = tmp_path / "persona.db"
    init_db(db_path)
    store = PersonaStore(db_path)
    # Seed the persona row so increment_session has a row to update
    await store.update_persona({})

    state = StationState()
    state.persona_store = store
    state.listener_session.observe_active_count(1, now=0.0)

    _sync_listener_session_persona(state)
    await asyncio.gather(*list(state.listener_session_tasks))

    persona = await store.get_persona()
    assert persona.session_count == 1


@pytest.mark.asyncio
async def test_record_motif_noop_without_store():
    """_record_motif does nothing when no persona_store is set."""
    from mammamiradio.scheduling.producer import _record_motif

    state = StationState()
    track = Track(title="Test", artist="Artist", duration_ms=1000)
    # Should not raise
    await _record_motif(state, track)


# ---------------------------------------------------------------------------
# prewarm_first_segment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prewarm_empty_playlist():
    """prewarm returns False immediately when playlist is empty."""
    from mammamiradio.scheduling.producer import prewarm_first_segment

    state = StationState(playlist=[])
    config = _make_config()
    queue: asyncio.Queue = asyncio.Queue()
    result = await prewarm_first_segment(queue, state, config)
    assert result is False
    assert queue.empty()


@pytest.mark.asyncio
async def test_prewarm_stopped_session():
    """prewarm returns False when session is stopped."""
    from mammamiradio.scheduling.producer import prewarm_first_segment

    state = _make_state()
    state.session_stopped = True
    config = _make_config()
    queue: asyncio.Queue = asyncio.Queue()
    result = await prewarm_first_segment(queue, state, config)
    assert result is False
    assert queue.empty()


@pytest.mark.asyncio
async def test_prewarm_happy_path():
    """prewarm downloads, normalizes, and queues a music segment."""
    from mammamiradio.scheduling.producer import prewarm_first_segment

    state = _make_state()
    config = _make_config()
    config.tmp_dir.mkdir(parents=True, exist_ok=True)
    queue: asyncio.Queue = asyncio.Queue()

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=Path("/tmp/fake.mp3")),
        patch(f"{PRODUCER_MODULE}.normalize"),
        patch(f"{PRODUCER_MODULE}.shutil.copy2"),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio"),
        patch(f"{PRODUCER_MODULE}._set_last_music_file"),
    ):
        result = await prewarm_first_segment(queue, state, config)

    assert result is True
    assert queue.qsize() == 1
    segment = queue.get_nowait()
    assert segment.type == SegmentType.MUSIC
    assert segment.metadata["audio_source"] == "prewarm"


@pytest.mark.asyncio
async def test_prewarm_cache_copy_failure_is_non_fatal(tmp_path):
    """prewarm should still queue audio when normalization cache copy fails."""
    from mammamiradio.scheduling.producer import prewarm_first_segment

    state = _make_state()
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue = asyncio.Queue()
    source = tmp_path / "source.mp3"
    source.write_bytes(b"\x00" * 1000)

    def _norm(_src, dst, *_args, **_kwargs):
        dst.write_bytes(b"\x00" * 1000)

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=source),
        patch(f"{PRODUCER_MODULE}.normalize", side_effect=_norm),
        patch(f"{PRODUCER_MODULE}.shutil.copy2", side_effect=OSError("disk full")),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio"),
        patch(f"{PRODUCER_MODULE}._set_last_music_file"),
    ):
        result = await prewarm_first_segment(queue, state, config)

    assert result is True
    assert queue.qsize() == 1
    assert queue.get_nowait().type == SegmentType.MUSIC


@pytest.mark.asyncio
async def test_prewarm_skips_invalid_download_before_normalize():
    """prewarm should reject invalid downloads without invoking normalize."""
    from mammamiradio.scheduling.producer import prewarm_first_segment

    state = _make_state()
    config = _make_config()
    config.tmp_dir.mkdir(parents=True, exist_ok=True)
    queue: asyncio.Queue = asyncio.Queue()

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=Path("/tmp/fake.mp3")),
        patch(f"{PRODUCER_MODULE}.validate_download", return_value=(False, "too small")) as mock_validate,
        patch(f"{PRODUCER_MODULE}.normalize") as mock_normalize,
    ):
        result = await prewarm_first_segment(queue, state, config)

    assert result is False
    assert queue.empty()
    mock_validate.assert_called_once()
    mock_normalize.assert_not_called()


@pytest.mark.asyncio
async def test_failed_download_marker_skips_normalize_and_denylists_track(tmp_path):
    """A yt-dlp failure marker is rejected before FFprobe or normalization."""
    from mammamiradio.playlist import downloader
    from mammamiradio.playlist.downloader import clear_rejected_cache_keys, is_rejected_cache_key
    from mammamiradio.scheduling.producer import _render_music_track

    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    track = Track(
        title="403 Probe",
        artist="Test Artist",
        duration_ms=180_000,
        youtube_id="dQw4w9WgXcQ",
        source="youtube",
    )
    marker = tmp_path / f"_failed_{track.cache_key}.mp3"
    marker.write_text("yt-dlp failed: HTTP Error 403: Forbidden")

    clear_rejected_cache_keys()
    try:
        with (
            patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=marker),
            patch(f"{PRODUCER_MODULE}.validate_download", wraps=downloader.validate_download),
            patch(f"{PRODUCER_MODULE}.normalize") as mock_normalize,
            patch("mammamiradio.playlist.downloader.subprocess.run") as mock_ffprobe,
        ):
            rendered = await _render_music_track(track, config, temp_prefix="music", context="music")

        assert rendered is None
        assert is_rejected_cache_key(track.cache_key)
        mock_normalize.assert_not_called()
        mock_ffprobe.assert_not_called()
    finally:
        clear_rejected_cache_keys()


@pytest.mark.asyncio
async def test_prewarm_skips_denylisted_track_for_playable_alternative(tmp_path):
    """Prewarm must not spend its bounded startup slots on a failed acquisition."""
    from mammamiradio.playlist.downloader import clear_rejected_cache_keys, reject_cached_download
    from mammamiradio.scheduling.producer import prewarm_first_segment

    state = _make_state()
    rejected, playable = state.playlist
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue = asyncio.Queue()
    source = tmp_path / "source.mp3"
    source.write_bytes(b"downloaded audio")

    def _normalize(_source, destination, *_args, **_kwargs):
        Path(destination).write_bytes(b"normalized audio")

    clear_rejected_cache_keys()
    try:
        reject_cached_download(config.cache_dir, rejected.cache_key, "yt-dlp failed: HTTP Error 403: Forbidden")
        with (
            patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=source) as mock_download,
            patch(f"{PRODUCER_MODULE}.normalize", side_effect=_normalize),
            patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=180.0),
        ):
            result = await prewarm_first_segment(queue, state, config)

        assert result is True
        assert mock_download.await_args.args[0] is playable
        segment = queue.get_nowait()
        assert segment.metadata["title"] == playable.display
    finally:
        clear_rejected_cache_keys()


@pytest.mark.asyncio
async def test_valid_local_recovery_reopens_a_session_denied_track(tmp_path):
    """A newly synced local file heals a denied source only after it validates."""
    from mammamiradio.playlist.downloader import (
        clear_rejected_cache_keys,
        is_rejected_cache_key,
        reject_cached_download,
    )
    from mammamiradio.scheduling.producer import _render_music_track, _select_accepted_music_track

    state = _make_state()
    state.playlist = state.playlist[:1]
    track = state.playlist[0]
    track.source = "local"
    local_file = tmp_path / "recovered.mp3"
    track.local_path = local_file
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    marker = tmp_path / f"_failed_{track.cache_key}.mp3"
    marker.write_text("yt-dlp unavailable")

    def _normalize(_source, destination, *_args, **_kwargs):
        Path(destination).write_bytes(b"normalized recovered audio")

    clear_rejected_cache_keys()
    try:
        reject_cached_download(config.cache_dir, track.cache_key, "yt-dlp unavailable")
        local_file.write_bytes(b"recovered audio")
        assert _select_accepted_music_track(state, config) is track
        assert is_rejected_cache_key(track.cache_key)

        with (
            patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=local_file),
            patch(f"{PRODUCER_MODULE}.normalize", side_effect=_normalize),
        ):
            rendered = await _render_music_track(track, config, temp_prefix="music", context="music")

        assert rendered is not None
        assert not is_rejected_cache_key(track.cache_key)
        assert not marker.exists()
    finally:
        clear_rejected_cache_keys()


@pytest.mark.asyncio
async def test_prewarm_returns_false_when_every_track_is_denylisted(tmp_path):
    """No eligible prewarm track must not trigger another acquisition attempt."""
    from mammamiradio.playlist.downloader import clear_rejected_cache_keys, reject_cached_download
    from mammamiradio.scheduling.producer import prewarm_first_segment

    state = _make_state()
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue = asyncio.Queue()

    clear_rejected_cache_keys()
    try:
        for track in state.playlist:
            reject_cached_download(config.cache_dir, track.cache_key, "yt-dlp failed: HTTP Error 403: Forbidden")
        with (
            patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock) as mock_download,
            patch(f"{PRODUCER_MODULE}.normalize") as mock_normalize,
        ):
            result = await prewarm_first_segment(queue, state, config)

        assert result is False
        assert queue.empty()
        mock_download.assert_not_awaited()
        mock_normalize.assert_not_called()
    finally:
        clear_rejected_cache_keys()


@pytest.mark.asyncio
async def test_prewarm_quality_gate_rejection_purges_normalized_cache(tmp_path):
    """A rejected prewarm render cannot remain available to recovery playback."""
    from mammamiradio.audio.audio_quality import AudioQualityError
    from mammamiradio.scheduling.producer import _normalized_cache_path, prewarm_first_segment

    state = _make_state()
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue = asyncio.Queue()
    track = state.playlist[0]
    source = tmp_path / "source.mp3"
    source.write_bytes(b"downloaded audio")
    norm_cached = _normalized_cache_path(track, config)

    def _reject(*_a, **_kw):
        raise AudioQualityError("silent track")

    def _normalize(_source, destination, *_args, **_kwargs):
        Path(destination).write_bytes(b"silent normalized audio")

    with (
        patch.object(state, "select_next_track", return_value=track),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=source),
        patch(f"{PRODUCER_MODULE}.normalize", side_effect=_normalize),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", side_effect=_reject),
    ):
        result = await prewarm_first_segment(queue, state, config)

    assert result is False
    assert queue.empty()
    assert not norm_cached.exists()
    assert not list(tmp_path.glob("music_*.mp3"))
    # The rejected prewarm render is recorded as generation waste (#397).
    assert state.discard_by_reason.get("quality_gate_reject") == 1
    assert state.discard_by_type.get("music") == 1


@pytest.mark.asyncio
async def test_prewarm_cached_quality_gate_rejection_purges_normalized_cache(tmp_path):
    """A rejected cache-hit prewarm cannot remain a recovery candidate."""
    from mammamiradio.audio.audio_quality import AudioQualityError
    from mammamiradio.scheduling.producer import _normalized_cache_path, prewarm_first_segment

    state = _make_state()
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue = asyncio.Queue()
    track = state.playlist[0]
    source = tmp_path / "source.mp3"
    source.write_bytes(b"downloaded audio")
    norm_cached = _normalized_cache_path(track, config)
    norm_cached.write_bytes(b"stale silent normalized audio")

    def _reject(*_a, **_kw):
        raise AudioQualityError("silent track")

    with (
        patch.object(state, "select_next_track", return_value=track),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=source),
        patch(f"{PRODUCER_MODULE}.reconcile_cached_music") as mock_reconcile,
        patch(f"{PRODUCER_MODULE}.normalize") as mock_normalize,
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", side_effect=_reject),
    ):
        result = await prewarm_first_segment(queue, state, config)

    assert result is False
    assert queue.empty()
    assert not norm_cached.exists()
    mock_reconcile.assert_called_once_with(norm_cached, background=False)
    mock_normalize.assert_not_called()


@pytest.mark.asyncio
async def test_prewarm_download_exception():
    """prewarm returns False (not raises) on download failure."""
    from mammamiradio.scheduling.producer import prewarm_first_segment

    state = _make_state()
    config = _make_config()
    queue: asyncio.Queue = asyncio.Queue()

    with patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=RuntimeError("network")):
        result = await prewarm_first_segment(queue, state, config)

    assert result is False
    assert queue.empty()


@pytest.mark.asyncio
async def test_music_segment_skips_invalid_download_before_normalize():
    """Main producer loop should skip invalid downloads before normalization."""
    state = _make_state()
    state.playlist[0].youtube_id = "yt_demo1"
    state.playlist[1].youtube_id = "yt_demo2"
    config = _make_config()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    validate_results = [(False, "too small"), (True, "ok")]

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.validate_download", side_effect=validate_results) as mock_validate,
        patch(f"{PRODUCER_MODULE}.normalize", side_effect=_fake_path) as mock_normalize,
        patch(f"{PRODUCER_MODULE}.shutil.copy2"),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert queue.qsize() >= 1
    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC
    assert mock_validate.call_count >= 2
    mock_normalize.assert_called_once()


@pytest.mark.asyncio
async def test_music_segment_cache_copy_failure_is_non_fatal(tmp_path):
    """Producer should queue music even when writing normalization cache fails."""
    state = _make_state()
    state.playlist[0].youtube_id = "yt_demo1"
    state.playlist[1].youtube_id = "yt_demo2"
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    source = tmp_path / "source.mp3"
    source.write_bytes(b"\x00" * 1000)

    def _norm(_src, dst, *_args, **_kwargs):
        dst.write_bytes(b"\x00" * 1000)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=source),
        patch(f"{PRODUCER_MODULE}.normalize", side_effect=_norm),
        patch(f"{PRODUCER_MODULE}.shutil.copy2", side_effect=OSError("read-only cache")),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert queue.qsize() >= 1
    assert queue.get_nowait().type == SegmentType.MUSIC


# ---------------------------------------------------------------------------
# Banter success branches — ListenerRequestCommit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_banter_with_listener_request_commit_applies_on_queue():
    """When write_banter returns a ListenerRequestCommit, the callback applies it after queuing."""
    state = _make_state()
    config = _make_config()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    req = {"type": "song_request", "name": "Florian", "song_found": True}
    state.pending_requests.append(req)
    commit = ListenerRequestCommit(request=req, consume=True)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")
    banter_lines = [(host, "Dedicato a te!")]

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, commit)),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...", None)
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    # success_callback fires inline before _run_until_queued returns; request must be consumed
    assert req not in state.pending_requests
    assert state.last_banter_script[0]["host"] == host.name
    assert state.last_banter_script[0]["type"] == "transition"
    assert state.last_banter_script[1]["text"] == "Dedicato a te!"


@pytest.mark.asyncio
async def test_banter_canned_path_does_not_apply_listener_request_commit():
    """When a canned clip is used, the ListenerRequestCommit is never applied."""
    state = _make_state()
    config = _make_config()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    req = {"type": "song_request", "name": "Giulia", "song_found": True}
    state.pending_requests.append(req)

    canned_path = _fake_path()

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=False),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=canned_path),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    # canned path: _used_generated_banter is False, commit is None — request must remain
    assert req in state.pending_requests
    seg = queue.get_nowait()
    assert "memory_extraction" not in seg.metadata


def _ha_ctx_mock():
    """A HomeContext-shaped mock with the fields the producer HA block reads."""
    ctx = MagicMock()
    ctx.authorization_mode = HomeAuthorizationMode.LEGACY.value
    ctx.summary = ""
    ctx.events_summary = ""
    ctx.events = []
    ctx.raw_states = {}
    ctx.scored = []
    ctx.timestamp = 0.0
    ctx.mood = ""
    ctx.weather_arc = ""
    ctx.mood_en = ""
    ctx.weather_arc_en = ""
    ctx.events_summary_en = ""
    ctx.last_event_label_en = ""
    ctx.radio_events = []
    ctx.ritual_recipe_matches = []
    ctx.ritual_public_families = []
    ctx.ritual_recipe_audit = []
    return ctx


def test_radio_event_directive_uses_next_break_path():
    state = _make_state()
    event = HomeEvent("script.kitchen_tts", "Kitchen TTS", "off", "on", time.time())
    match = RadioEventMatch(
        rule_id="tts_script_started",
        mode="directive",
        directive="One of the house voices just spoke.",
        event=event,
        cooldown_seconds=60,
        matched_at=time.time(),
    )

    with patch(f"{PRODUCER_MODULE}.commit_radio_event_directive") as commit:
        gag_events = _apply_radio_event_matches(state, [match])

    assert gag_events == []
    assert state.ha_pending_directive == "One of the house voices just spoke."
    commit.assert_called_once_with(match)


def test_radio_event_directive_clears_stale_ritual_receipt_id():
    state = _make_state()
    state.ha_pending_directive_moment_id = "stale-ritual-id"
    event = HomeEvent("script.kitchen_tts", "Kitchen TTS", "off", "on", time.time())
    match = RadioEventMatch(
        rule_id="tts_script_started",
        mode="directive",
        directive="One of the house voices just spoke.",
        event=event,
        cooldown_seconds=60,
        matched_at=time.time(),
    )

    with patch(f"{PRODUCER_MODULE}.commit_radio_event_directive"):
        _apply_radio_event_matches(state, [match])

    assert state.ha_pending_directive == "One of the house voices just spoke."
    assert state.ha_pending_directive_moment_id == ""


def test_radio_event_directive_does_not_override_existing_directive():
    state = _make_state()
    state.ha_pending_directive = "legacy directive"
    event = HomeEvent("script.kitchen_tts", "Kitchen TTS", "off", "on", time.time())
    match = RadioEventMatch(
        rule_id="tts_script_started",
        mode="directive",
        directive="new directive",
        event=event,
        cooldown_seconds=60,
        matched_at=time.time(),
    )

    with patch(f"{PRODUCER_MODULE}.commit_radio_event_directive") as commit:
        gag_events = _apply_radio_event_matches(state, [match])

    assert gag_events == []
    assert state.ha_pending_directive == "legacy directive"
    commit.assert_not_called()


def test_radio_event_gag_feeds_ledger_event_path():
    state = _make_state()
    event = HomeEvent(
        "binary_sensor.phone_charging",
        "A household device started charging",
        "off",
        "on",
        time.time(),
        force_gag_candidate=True,
        gag_cooldown_seconds=120,
    )
    match = RadioEventMatch(
        rule_id="device_charging",
        mode="gag",
        directive="",
        event=event,
        cooldown_seconds=120,
        matched_at=time.time(),
    )

    assert _apply_radio_event_matches(state, [match]) == [event]
    assert state.ha_pending_directive == ""


def _ha_state(value: object, **attrs: object) -> dict:
    return {"state": value, "attributes": attrs}


def test_ritual_recipe_directive_uses_next_break_path():
    clear_ritual_recipe_cooldowns()
    state = _make_state()
    matches = match_ritual_recipes(
        None,
        {"sensor.kitchen_coffee_power": _ha_state("0", friendly_name="Kitchen coffee machine power")},
        {"sensor.kitchen_coffee_power": _ha_state("80", friendly_name="Kitchen coffee machine power")},
        now=100.0,
    )

    with patch(f"{PRODUCER_MODULE}.commit_ritual_recipe_match") as commit:
        gag_events, interrupt = _apply_ritual_recipe_matches(state, matches)

    assert gag_events == []
    assert interrupt is None
    assert "Morning launch" in state.ha_pending_directive
    commit.assert_called_once_with(matches[0])


def test_ritual_recipe_directive_does_not_override_existing_directive():
    clear_ritual_recipe_cooldowns()
    state = _make_state()
    state.ha_pending_directive = "legacy directive"
    matches = match_ritual_recipes(
        None,
        {"sensor.kitchen_coffee_power": _ha_state("0", friendly_name="Kitchen coffee machine power")},
        {"sensor.kitchen_coffee_power": _ha_state("80", friendly_name="Kitchen coffee machine power")},
        now=100.0,
    )

    with patch(f"{PRODUCER_MODULE}.commit_ritual_recipe_match") as commit:
        gag_events, interrupt = _apply_ritual_recipe_matches(state, matches)

    assert gag_events == []
    assert interrupt is None
    assert state.ha_pending_directive == "legacy directive"
    commit.assert_not_called()


def test_ritual_recipe_gag_feeds_ledger_event_path_without_spending_recipe_cooldown():
    clear_ritual_recipe_cooldowns()
    state = _make_state()
    matches = match_ritual_recipes(
        None,
        {"binary_sensor.fridge_door": _ha_state("off", device_class="door", friendly_name="Kitchen fridge door")},
        {"binary_sensor.fridge_door": _ha_state("on", device_class="door", friendly_name="Kitchen fridge door")},
        now=200.0,
    )

    with patch(f"{PRODUCER_MODULE}.commit_ritual_recipe_match") as commit:
        gag_events, interrupt = _apply_ritual_recipe_matches(state, matches)

    assert interrupt is None
    assert state.ha_pending_directive == ""
    assert len(gag_events) == 1
    assert gag_events[0].label == "Kitchen ritual"
    assert gag_events[0].force_gag_candidate is True
    assert gag_events[0].ritual_family == matches[0].recipe.family
    commit.assert_not_called()


def test_ritual_recipe_safety_interrupt_preserves_interrupt_lane():
    clear_ritual_recipe_cooldowns()
    state = _make_state()
    matches = match_ritual_recipes(
        None,
        {"binary_sensor.sink_leak": _ha_state("off", device_class="moisture", friendly_name="Sink leak")},
        {"binary_sensor.sink_leak": _ha_state("on", device_class="moisture", friendly_name="Sink leak")},
        now=300.0,
    )

    with patch(f"{PRODUCER_MODULE}.commit_ritual_recipe_match") as commit:
        gag_events, interrupt = _apply_ritual_recipe_matches(state, matches)

    assert gag_events == []
    assert interrupt is not None
    assert interrupt.match is matches[0]
    assert isinstance(interrupt.spec, InterruptSpec)
    assert interrupt.spec.urgency == "urgent"
    assert "Safety moment" in interrupt.spec.directive
    assert state.ha_pending_directive == ""
    commit.assert_not_called()


def test_ritual_recipe_safety_interrupt_can_supersede_existing_directive():
    clear_ritual_recipe_cooldowns()
    state = _make_state()
    state.ha_pending_directive = "legacy directive"
    matches = match_ritual_recipes(
        None,
        {"binary_sensor.sink_leak": _ha_state("off", device_class="moisture", friendly_name="Sink leak")},
        {"binary_sensor.sink_leak": _ha_state("on", device_class="moisture", friendly_name="Sink leak")},
        now=310.0,
    )

    with patch(f"{PRODUCER_MODULE}.commit_ritual_recipe_match") as commit:
        gag_events, interrupt = _apply_ritual_recipe_matches(state, matches)

    assert gag_events == []
    assert interrupt is not None
    assert interrupt.match is matches[0]
    assert interrupt.spec.urgency == "urgent"
    assert state.ha_pending_directive == "legacy directive"
    commit.assert_not_called()


@pytest.mark.asyncio
async def test_producer_commits_ritual_interrupt_cooldown_only_after_interrupt_fires():
    clear_ritual_recipe_cooldowns()
    state = _make_state()
    config = _make_config()
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    config.homeassistant.url = "http://ha.local:8123"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    matches = match_ritual_recipes(
        None,
        {"binary_sensor.sink_leak": _ha_state("off", device_class="moisture", friendly_name="Sink leak")},
        {"binary_sensor.sink_leak": _ha_state("on", device_class="moisture", friendly_name="Sink leak")},
        now=320.0,
    )
    ha_context = _ha_ctx_mock()
    ha_context.ritual_recipe_matches = matches

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=False),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=ha_context),
        patch(f"{PRODUCER_MODULE}.check_reactive_triggers", return_value=None),
        patch(f"{PRODUCER_MODULE}._fire_interrupt", new_callable=AsyncMock, return_value=True) as fire,
        patch(f"{PRODUCER_MODULE}.commit_ritual_recipe_match") as commit,
    ):
        await _run_until_queued(queue, state, config)

    fire.assert_awaited_once()
    assert isinstance(fire.await_args.args[1], InterruptSpec)
    commit.assert_called_once_with(matches[0])


@pytest.mark.asyncio
async def test_producer_keeps_ritual_interrupt_cooldown_when_global_cooldown_suppresses():
    clear_ritual_recipe_cooldowns()
    state = _make_state()
    config = _make_config()
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    config.homeassistant.url = "http://ha.local:8123"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    matches = match_ritual_recipes(
        None,
        {"binary_sensor.sink_leak": _ha_state("off", device_class="moisture", friendly_name="Sink leak")},
        {"binary_sensor.sink_leak": _ha_state("on", device_class="moisture", friendly_name="Sink leak")},
        now=330.0,
    )
    ha_context = _ha_ctx_mock()
    ha_context.ritual_recipe_matches = matches

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=False),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=ha_context),
        patch(f"{PRODUCER_MODULE}.check_reactive_triggers", return_value=None),
        patch(f"{PRODUCER_MODULE}._fire_interrupt", new_callable=AsyncMock, return_value=False) as fire,
        patch(f"{PRODUCER_MODULE}.commit_ritual_recipe_match") as commit,
    ):
        await _run_until_queued(queue, state, config)

    fire.assert_awaited_once()
    commit.assert_not_called()


# --- Moment Receipts wiring (elected / dropped rows per delivery lane) ---


def _moment_store():
    from mammamiradio.home.moment_receipts import MomentStore

    return MomentStore()


def _coffee_directive_matches(now: float):
    return match_ritual_recipes(
        None,
        {"sensor.kitchen_coffee_power": _ha_state("0", friendly_name="Kitchen coffee machine power")},
        {"sensor.kitchen_coffee_power": _ha_state("80", friendly_name="Kitchen coffee machine power")},
        now=now,
    )


def test_ritual_directive_records_elected_moment_and_threads_id():
    clear_ritual_recipe_cooldowns()
    state = _make_state()
    state.moment_store = _moment_store()
    matches = _coffee_directive_matches(400.0)

    with patch(f"{PRODUCER_MODULE}.commit_ritual_recipe_match"):
        _apply_ritual_recipe_matches(state, matches)

    (row,) = state.moment_store.rows
    assert row.status == "elected"
    assert row.lane == "directive"
    assert row.family == "morning_launch"
    assert row.public_label == "Morning launch"
    assert row.entity_id == "sensor.kitchen_coffee_power"
    # The receipt id travels with the directive toward the consuming banter.
    assert state.ha_pending_directive_moment_id == row.id


def test_ritual_directive_slot_busy_records_dropped_moment():
    clear_ritual_recipe_cooldowns()
    state = _make_state()
    state.moment_store = _moment_store()
    state.ha_pending_directive = "legacy directive"
    matches = _coffee_directive_matches(410.0)

    with patch(f"{PRODUCER_MODULE}.commit_ritual_recipe_match") as commit:
        _apply_ritual_recipe_matches(state, matches)

    (row,) = state.moment_store.rows
    assert row.status == "dropped"
    assert row.drop_reason == "directive_slot_busy"
    assert state.ha_pending_directive_moment_id == ""
    commit.assert_not_called()


def test_ritual_interrupt_slot_busy_records_dropped_moment():
    clear_ritual_recipe_cooldowns()
    state = _make_state()
    state.moment_store = _moment_store()
    state.force_next = SegmentType.BANTER  # interrupt lane guard trips
    matches = match_ritual_recipes(
        None,
        {"binary_sensor.sink_leak": _ha_state("off", device_class="moisture", friendly_name="Sink leak")},
        {"binary_sensor.sink_leak": _ha_state("on", device_class="moisture", friendly_name="Sink leak")},
        now=420.0,
    )

    gag_events, interrupt = _apply_ritual_recipe_matches(state, matches)

    assert interrupt is None
    assert gag_events == []
    (row,) = state.moment_store.rows
    assert row.status == "dropped"
    assert row.drop_reason == "interrupt_slot_busy"
    assert row.lane == "interrupt"


def test_ritual_moment_recording_survives_store_failure():
    clear_ritual_recipe_cooldowns()
    state = _make_state()
    state.moment_store = _moment_store()
    matches = _coffee_directive_matches(430.0)

    with (
        patch(f"{PRODUCER_MODULE}.commit_ritual_recipe_match"),
        patch.object(state.moment_store, "record", side_effect=RuntimeError("disk on fire")),
    ):
        _apply_ritual_recipe_matches(state, matches)

    # Recording failed silently; the directive still landed (audio-path first).
    assert "Morning launch" in state.ha_pending_directive
    assert state.ha_pending_directive_moment_id == ""


def test_ritual_lanes_work_without_a_store():
    clear_ritual_recipe_cooldowns()
    state = _make_state()
    assert state.moment_store is None
    matches = _coffee_directive_matches(440.0)

    with patch(f"{PRODUCER_MODULE}.commit_ritual_recipe_match"):
        _apply_ritual_recipe_matches(state, matches)

    assert "Morning launch" in state.ha_pending_directive
    assert state.ha_pending_directive_moment_id == ""


@pytest.mark.asyncio
async def test_producer_queues_audio_when_moment_store_save_raises():
    state = _make_state()
    state.moment_store = MagicMock()
    state.moment_store.save_if_dirty.side_effect = RuntimeError("disk full")
    config = _make_config()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=False),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert queue.qsize() == 1
    assert queue.get_nowait().type == SegmentType.BANTER
    state.moment_store.save_if_dirty.assert_called()


@pytest.mark.asyncio
async def test_producer_records_interrupt_moment_elected_on_fire():
    clear_ritual_recipe_cooldowns()
    state = _make_state()
    state.moment_store = _moment_store()
    config = _make_config()
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    config.homeassistant.url = "http://ha.local:8123"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    matches = match_ritual_recipes(
        None,
        {"binary_sensor.sink_leak": _ha_state("off", device_class="moisture", friendly_name="Sink leak")},
        {"binary_sensor.sink_leak": _ha_state("on", device_class="moisture", friendly_name="Sink leak")},
        now=450.0,
    )
    ha_context = _ha_ctx_mock()
    ha_context.ritual_recipe_matches = matches

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=False),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=ha_context),
        patch(f"{PRODUCER_MODULE}.check_reactive_triggers", return_value=None),
        patch(f"{PRODUCER_MODULE}._fire_interrupt", new_callable=AsyncMock, return_value=True),
        patch(f"{PRODUCER_MODULE}.commit_ritual_recipe_match"),
    ):
        await _run_until_queued(queue, state, config)

    elected = [r for r in state.moment_store.rows if r.status == "elected"]
    assert len(elected) == 1
    assert elected[0].lane == "interrupt"
    assert elected[0].family == "safety_saves"
    assert state.ha_pending_directive_moment_id == elected[0].id


@pytest.mark.asyncio
async def test_fire_interrupt_demotes_clobbered_directive_receipt(tmp_path):
    """A live cut-in overwrites any pending directive; if that directive carried
    an elected receipt (its recipe cooldown already spent), the row is demoted —
    it can never air, and 'waiting for its break' would be a lie."""
    from mammamiradio.core.models import InterruptSpec as _Spec
    from mammamiradio.scheduling.producer import _fire_interrupt

    state = _make_state()
    state.moment_store = _moment_store()
    row_id = state.moment_store.record(lane="directive", family="morning_launch", public_label="Morning launch")
    state.ha_pending_directive = "React to the coffee machine."
    state.ha_pending_directive_moment_id = row_id

    fired = await _fire_interrupt(
        state,
        _Spec(directive="Leak! React NOW.", urgency="urgent", cooldown=600),
        asyncio.Queue(maxsize=4),
        None,
        bridge_tmp_dir=tmp_path,
    )

    assert fired is True
    (row,) = state.moment_store.rows
    assert row.status == "dropped"
    assert row.drop_reason == "interrupt_override"
    assert state.ha_pending_directive_moment_id == ""


@pytest.mark.asyncio
async def test_producer_records_interrupt_moment_dropped_on_cooldown_suppression():
    clear_ritual_recipe_cooldowns()
    state = _make_state()
    state.moment_store = _moment_store()
    config = _make_config()
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    config.homeassistant.url = "http://ha.local:8123"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    matches = match_ritual_recipes(
        None,
        {"binary_sensor.sink_leak": _ha_state("off", device_class="moisture", friendly_name="Sink leak")},
        {"binary_sensor.sink_leak": _ha_state("on", device_class="moisture", friendly_name="Sink leak")},
        now=460.0,
    )
    ha_context = _ha_ctx_mock()
    ha_context.ritual_recipe_matches = matches

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=False),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=ha_context),
        patch(f"{PRODUCER_MODULE}.check_reactive_triggers", return_value=None),
        patch(f"{PRODUCER_MODULE}._fire_interrupt", new_callable=AsyncMock, return_value=False),
        patch(f"{PRODUCER_MODULE}.commit_ritual_recipe_match"),
    ):
        await _run_until_queued(queue, state, config)

    (row,) = state.moment_store.rows
    assert row.status == "dropped"
    assert row.drop_reason == "interrupt_cooldown"
    assert state.ha_pending_directive_moment_id == ""


@pytest.mark.asyncio
async def test_producer_records_gag_moment_for_ritual_sourced_bucket_only():
    from mammamiradio.home.evening_memory import EveningLedger, GagBucket

    state = _make_state()
    state.moment_store = _moment_store()
    ledger = EveningLedger()
    ledger.buckets["binary_sensor.fridge_door|off->on"] = GagBucket(
        entity_id="binary_sensor.fridge_door",
        label="Kitchen ritual",
        old_state="chiuso",
        new_state="aperto",
        count=3,
        first_ts=100.0,
        last_ts=200.0,
        ritual_family="fridge_freezer_raid",
    )
    state.evening_ledger = ledger
    config = _make_config()
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    config.homeassistant.url = "http://ha.local:8123"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    ha_context = _ha_ctx_mock()

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=False),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=ha_context),
        patch(f"{PRODUCER_MODULE}.check_reactive_triggers", return_value=None),
        patch.object(
            ledger,
            "offer_gag",
            return_value=("binary_sensor.fridge_door|off->on", "Il frigo, di nuovo stasera."),
        ),
    ):
        await _run_until_queued(queue, state, config)

    (row,) = state.moment_store.rows
    assert row.lane == "running_gag"
    assert row.family == "fridge_freezer_raid"
    assert row.public_label == "Kitchen ritual"
    assert row.count == 3
    # This harness has no LLM, so the banter fell back to a canned clip: the
    # elected row is honestly demoted by the queue-commit callback (the gag
    # never rode the aired banter) and the id slot is consumed.
    assert row.status == "dropped"
    assert row.drop_reason == "canned_fallback"
    assert state.ha_running_gag_moment_id == ""


@pytest.mark.asyncio
async def test_generated_running_gag_carries_receipt_until_stream_marks_airing(tmp_path):
    from mammamiradio.home.evening_memory import EveningLedger, GagBucket

    state = _make_state()
    state.moment_store = _moment_store()
    ledger = EveningLedger()
    key = "binary_sensor.fridge_door|off->on"
    ledger.buckets[key] = GagBucket(
        entity_id="binary_sensor.fridge_door",
        label="Kitchen ritual",
        old_state="chiuso",
        new_state="aperto",
        count=3,
        first_ts=100.0,
        last_ts=200.0,
        ritual_family="fridge_freezer_raid",
    )
    state.evening_ledger = ledger
    config = _make_config()
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    config.homeassistant.url = "http://ha.local:8123"
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    ha_context = _ha_ctx_mock()
    host = config.hosts[0]

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch.object(ledger, "offer_gag", return_value=(key, "Il frigo, di nuovo stasera.")),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=([(host, "Il frigo.")], None)
        ),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...", None)
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=ha_context),
        patch(f"{PRODUCER_MODULE}.check_reactive_triggers", return_value=None),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    (row,) = state.moment_store.rows
    assert seg.metadata["gag_moment_id"] == row.id
    assert seg.metadata["ritual_moment_id"] is None
    assert row.status == "elected"
    assert row.lane == "running_gag"
    assert row.family == "fridge_freezer_raid"
    assert row.public_label == "Kitchen ritual"
    assert ledger.buckets[key].last_spoken_ts > 0
    assert state.ha_running_gag_moment_id == ""


@pytest.mark.asyncio
async def test_producer_records_no_gag_moment_for_plain_bucket():
    from mammamiradio.home.evening_memory import EveningLedger, GagBucket

    state = _make_state()
    state.moment_store = _moment_store()
    ledger = EveningLedger()
    ledger.buckets["switch.fan|off->on"] = GagBucket(
        entity_id="switch.fan",
        label="Ventilatore",
        old_state="spento",
        new_state="acceso",
        count=4,
    )
    state.evening_ledger = ledger
    config = _make_config()
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    config.homeassistant.url = "http://ha.local:8123"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    ha_context = _ha_ctx_mock()

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=False),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=ha_context),
        patch(f"{PRODUCER_MODULE}.check_reactive_triggers", return_value=None),
        patch.object(ledger, "offer_gag", return_value=("switch.fan|off->on", "Il ventilatore, di nuovo.")),
    ):
        await _run_until_queued(queue, state, config)

    # A plain home-event gag has no ritual moment in v1 — no receipt row.
    assert state.moment_store.rows == []
    assert state.ha_running_gag_moment_id == ""


@pytest.mark.asyncio
async def test_stock_copy_fallback_banter_never_wears_a_receipt(tmp_path):
    """REGRESSION (pre-ship audit P0): a stock-copy fallback return from
    write_banter leaves the LIVE directive id restored in state. The segment
    build must read ONLY the scriptwriter handoff slot, or the stock lines air
    wearing the moment's id and mint a false "aired" receipt."""
    state = _make_state()
    state.moment_store = _moment_store()
    row_id = state.moment_store.record(lane="directive", family="morning_launch", public_label="Morning launch")
    state.ha_pending_directive = "React to the coffee machine."
    state.ha_pending_directive_moment_id = row_id
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    # A plain return WITHOUT setting the handoff slot is exactly the stock-copy
    # fallback contract (the real except path clears the slot before returning).
    stock_lines = [(host, "Che serata, amici."), (host, "Davvero.")]

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(stock_lines, None)),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...", None)
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.type == SegmentType.BANTER
    assert seg.metadata.get("ritual_moment_id") is None
    # The row never airs on stock copy; it stays live for the real retry.
    (row,) = state.moment_store.rows
    assert row.status == "elected"


@pytest.mark.asyncio
async def test_quality_gate_canned_fallback_demotes_consumed_directive_receipt(tmp_path):
    """If generated directive banter is replaced by a canned fallback, neither
    receipt it was carrying may stay "waiting for its break": the directive
    (and any running gag riding the same banter) was consumed and the canned
    clip cannot carry either to air. Regression coverage for a gap where only
    the ritual id was demoted inline and the gag id was left dangling until a
    successful queue (never reached if a later stale/enqueue-failure discard
    hits this same segment first)."""
    from mammamiradio.audio.audio_quality import AudioQualityError

    state = _make_state()
    state.moment_store = _moment_store()
    row_id = state.moment_store.record(lane="directive", family="morning_launch", public_label="Morning launch")
    gag_row_id = state.moment_store.record(
        lane="running_gag", family="fridge_freezer_raid", public_label="Kitchen ritual"
    )
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    fallback = tmp_path / "canned.mp3"
    fallback.write_bytes(b"fake")

    async def _write_banter(*_args, **_kwargs):
        state.last_banter_ritual_moment_id = row_id
        state.ha_running_gag_moment_id = gag_row_id
        return [(host, "La macchina del caffe si e svegliata.")], None

    quality_calls = 0

    def _quality_gate(*_args, **_kwargs):
        nonlocal quality_calls
        quality_calls += 1
        if quality_calls == 1:
            raise AudioQualityError("generated break is not airable")
        return None

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, side_effect=_write_banter),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...", None)
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", side_effect=_quality_gate),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=fallback),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.metadata.get("canned") is True
    assert seg.metadata.get("ritual_moment_id") is None
    assert seg.metadata.get("gag_moment_id") is None
    rows_by_id = {row.id: row for row in state.moment_store.rows}
    assert rows_by_id[row_id].status == "dropped"
    assert rows_by_id[row_id].drop_reason == "canned_fallback"
    assert rows_by_id[gag_row_id].status == "dropped"
    assert rows_by_id[gag_row_id].drop_reason == "canned_fallback"
    assert state.last_banter_ritual_moment_id == ""
    assert state.ha_running_gag_moment_id == ""


@pytest.mark.asyncio
async def test_stale_discard_demotes_carried_moment_receipt(tmp_path):
    """A generated banter carrying an elected ritual receipt that loses the
    stale-playlist race in the shared discard epilogue (producer.py's
    ``_drop_segment_moment_receipts`` call at the stale-gate) must have its
    row demoted — otherwise the admin Moments panel shows it "waiting for its
    break" for up to a week for a moment that was actually discarded."""
    state = _make_state()
    state.moment_store = _moment_store()
    row_id = state.moment_store.record(lane="directive", family="morning_launch", public_label="Morning launch")
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]

    async def _write_banter(*_args, **_kwargs):
        state.last_banter_ritual_moment_id = row_id
        return [(host, "La macchina del caffe si e svegliata.")], None

    def _staling_probe(_path):
        # A same-source playlist edit lands mid-build — the shared epilogue
        # gate discards this segment before it ever reaches the queue.
        state.playlist_revision += 1
        return 1.0

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, side_effect=_write_banter),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...", None)
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", side_effect=_staling_probe),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            deadline = asyncio.get_event_loop().time() + 5.0
            while not (state.moment_store.rows and state.moment_store.rows[0].status == "dropped"):
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("moment receipt was never demoted after the stale discard")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    assert queue.empty()  # discarded, never queued
    (row,) = state.moment_store.rows
    assert row.status == "dropped"
    assert row.drop_reason == "stale_playlist"


@pytest.mark.asyncio
async def test_enqueue_failure_demotes_carried_moment_receipt(tmp_path):
    """A generated banter carrying an elected ritual receipt whose enqueue
    fails (operator stopped the session mid-build) must have its row demoted
    at the ``_queue_segment`` failure site — the directive was already
    consumed and no retry will carry this receipt to air."""
    state = _make_state()
    state.moment_store = _moment_store()
    row_id = state.moment_store.record(lane="directive", family="morning_launch", public_label="Morning launch")
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]

    async def _write_banter(*_args, **_kwargs):
        state.last_banter_ritual_moment_id = row_id
        # Operator hit Stop while this banter was mid-build: the top-of-loop
        # gate already passed for this iteration, so this reaches the
        # _queue_segment() failure path instead of being caught earlier.
        state.session_stopped = True
        return [(host, "La macchina del caffe si e svegliata.")], None

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, side_effect=_write_banter),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...", None)
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._apply_talk_bed", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            deadline = asyncio.get_event_loop().time() + 5.0
            while not (state.moment_store.rows and state.moment_store.rows[0].status == "dropped"):
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("moment receipt was never demoted after the enqueue failure")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    assert queue.empty()  # session stopped — never queued
    (row,) = state.moment_store.rows
    assert row.status == "dropped"
    assert row.drop_reason == "generation_failed"


@pytest.mark.asyncio
async def test_stale_handoff_id_never_leaks_onto_unrelated_banter(tmp_path):
    """REGRESSION (pre-ship audit P1): a prior cycle that consumed a directive
    but died before the build (TTS failure, quality reject) must not leave its
    handoff id behind for the NEXT, unrelated banter to wear on air."""
    state = _make_state()
    state.moment_store = _moment_store()
    state.last_banter_ritual_moment_id = "stale-id-from-dead-cycle"
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            return_value=([(host, "Nessuna direttiva qui.")], None),
        ),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...", None)
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.metadata.get("ritual_moment_id") is None


@pytest.mark.asyncio
async def test_interrupt_stock_copy_demotes_receipt_at_queue_commit(tmp_path):
    """REGRESSION (pre-ship audit P0, interrupt lane): an urgent-interrupt
    banter that fell back to stock copy queues without its id AND consumes the
    directive at commit — no retry exists, so the elected row must be demoted
    honestly instead of claiming to wait for a break that will never come."""
    from mammamiradio.core.models import ChaosSubtype

    state = _make_state()
    state.moment_store = _moment_store()
    row_id = state.moment_store.record(lane="interrupt", family="safety_saves", public_label="Safety moment")
    state.ha_pending_directive = "Safety moment. React NOW."
    state.ha_pending_directive_moment_id = row_id
    state.chaos_pending = ChaosSubtype.URGENT_INTERRUPT
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        # Stock-copy contract: returns lines, handoff slot NOT set.
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            return_value=([(host, "Momento, amici...")], None),
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    assert seg.metadata.get("ritual_moment_id") is None
    (row,) = state.moment_store.rows
    assert row.status == "dropped"
    assert row.drop_reason == "generation_failed"
    assert state.ha_pending_directive_moment_id == ""


@pytest.mark.asyncio
async def test_urgent_interrupt_retry_preserves_receipt_for_generated_banter(tmp_path):
    """A retry may enter the next loop with the same id in the stale handoff
    slot and the live urgent directive. Cleanup must not demote that receipt
    before the successful generated retry can attach it to the segment."""
    from mammamiradio.core.models import ChaosSubtype

    state = _make_state()
    state.moment_store = _moment_store()
    row_id = state.moment_store.record(lane="interrupt", family="safety_saves", public_label="Safety moment")
    state.ha_pending_directive = "Safety moment. React NOW."
    state.ha_pending_directive_moment_id = row_id
    state.last_banter_ritual_moment_id = row_id
    state.chaos_pending = ChaosSubtype.URGENT_INTERRUPT
    state.force_next = SegmentType.BANTER
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    observed_statuses: list[str] = []

    async def _write_banter(*_args, **_kwargs):
        (row,) = state.moment_store.rows
        observed_statuses.append(row.status)
        state.last_banter_ritual_moment_id = state.ha_pending_directive_moment_id
        return [(host, "Momento, amici...")], None

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, side_effect=_write_banter),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    seg = queue.get_nowait()
    (row,) = state.moment_store.rows
    assert observed_statuses == ["elected"]
    assert seg.metadata["ritual_moment_id"] == row_id
    assert row.status == "elected"
    assert state.ha_pending_directive == ""
    assert state.ha_pending_directive_moment_id == ""
    assert state.force_next is None


@pytest.mark.asyncio
async def test_producer_passes_radio_event_rules_and_applies_directive_match():
    """Producer loop wires configured radio-event rules through HA refresh."""
    state = _make_state()
    config = _make_config()
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    config.homeassistant.url = "http://ha.local:8123"
    config.radio_events = [
        RadioEventRule(
            id="tts_script_started",
            label="Kitchen TTS",
            mode="directive",
            entity_glob="script.*tts*",
            trigger="state",
            from_state="off",
            to_state="on",
            cooldown_seconds=60,
            directive="One of the house voices just spoke.",
        )
    ]
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    event = HomeEvent("script.kitchen_tts", "Kitchen TTS", "off", "on", time.time())
    match = RadioEventMatch(
        rule_id="tts_script_started",
        mode="directive",
        directive="One of the house voices just spoke.",
        event=event,
        cooldown_seconds=60,
        matched_at=time.time(),
    )
    ha_context = _ha_ctx_mock()
    ha_context.radio_events = [match]

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=False),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=ha_context) as mock_fetch,
        patch(f"{PRODUCER_MODULE}.check_reactive_triggers", return_value=None),
        patch(f"{PRODUCER_MODULE}.commit_radio_event_directive") as mock_commit,
    ):
        await _run_until_queued(queue, state, config)

    assert mock_fetch.await_args.kwargs["radio_event_rules"] == config.radio_events
    assert state.ha_pending_directive == "One of the house voices just spoke."
    mock_commit.assert_called_once_with(match)


@pytest.mark.asyncio
async def test_running_gag_marked_spoken_only_when_generated_banter_airs():
    """The evening-gag cooldown is spent only when generated banter actually airs.

    Invariant (EveningLedger.offer_gag contract): the producer offers a gag, but
    mark_spoken runs in the banter success callback — so generated banter that
    reaches air spends the cooldown.
    """
    state = _make_state()
    config = _make_config()
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    config.homeassistant.url = "http://ha.local:8123"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    ledger = MagicMock(spec=EveningLedger)
    ledger.offer_gag.return_value = ("k_coffee", "La macchina del caffè di nuovo stasera.")
    state.evening_ledger = ledger

    host = config.hosts[0]
    banter_lines = [(host, "Ancora caffè!")]

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...", None)
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=_ha_ctx_mock()),
        patch(f"{PRODUCER_MODULE}.check_reactive_triggers", return_value=None),
    ):
        await _run_until_queued(queue, state, config)

    ledger.offer_gag.assert_called()
    ledger.mark_spoken.assert_called_once()
    assert ledger.mark_spoken.call_args.args[0] == "k_coffee"


@pytest.mark.asyncio
async def test_running_gag_not_marked_on_canned_fallback():
    """A canned-clip fallback must NOT spend the gag cooldown — the gag never aired."""
    state = _make_state()
    config = _make_config()
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    config.homeassistant.url = "http://ha.local:8123"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    ledger = MagicMock(spec=EveningLedger)
    ledger.offer_gag.return_value = ("k_coffee", "La macchina del caffè di nuovo stasera.")
    state.evening_ledger = ledger

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=False),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=_ha_ctx_mock()),
        patch(f"{PRODUCER_MODULE}.check_reactive_triggers", return_value=None),
    ):
        await _run_until_queued(queue, state, config)

    ledger.offer_gag.assert_called()
    ledger.mark_spoken.assert_not_called()


@pytest.mark.asyncio
async def test_running_gag_not_marked_on_llm_stock_copy_fallback():
    """An LLM fallback to stock copy must NOT spend the gag cooldown.

    Unlike a canned-clip fallback, a stock-copy fallback keeps ``canned is None``,
    so ``_used_generated_banter`` is True. The only thing stopping the producer
    from burning the cooldown is ``write_banter`` releasing ``ha_running_gag_key``
    before the success callback captures it. This locks that ordering: capturing
    the key before ``write_banter`` ran would wrongly mark the gag spoken.
    """
    state = _make_state()
    config = _make_config()
    config.homeassistant.enabled = True
    config.ha_token = "fake-token"
    config.homeassistant.url = "http://ha.local:8123"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    ledger = MagicMock(spec=EveningLedger)
    ledger.offer_gag.return_value = ("k_coffee", "La macchina del caffè di nuovo stasera.")
    state.evening_ledger = ledger

    host = config.hosts[0]

    async def _fallback_releases_gag_key(*_args, **_kwargs):
        # Mirror the real write_banter fallback: release the gag bucket and
        # return stock copy (canned stays None -> _used_generated_banter is True).
        state.ha_running_gag_key = ""
        state.ha_running_gag = ""
        return [(host, "Comunque, mica male questa.")], None

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, side_effect=_fallback_releases_gag_key),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...", None)
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock, return_value=_ha_ctx_mock()),
        patch(f"{PRODUCER_MODULE}.check_reactive_triggers", return_value=None),
    ):
        await _run_until_queued(queue, state, config)

    ledger.offer_gag.assert_called()
    ledger.mark_spoken.assert_not_called()


@pytest.mark.asyncio
async def test_banter_impossible_tts_path_does_not_apply_listener_request_commit():
    """Impossible-TTS fallback should queue banter without mutating listener requests."""
    state = _make_state()
    config = _make_config()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    req = {
        "type": "song_request",
        "name": "Giulia",
        "message": "metti Eros Ramazzotti",
        "song_found": True,
    }
    state.pending_requests.append(req)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=False),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
        patch(f"{PRODUCER_MODULE}.generate_impossible_line", return_value="Linea impossibile"),
        patch(f"{PRODUCER_MODULE}._synthesize_impossible_moment", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert req in state.pending_requests
    seg = queue.get_nowait()
    assert seg.type == SegmentType.BANTER
    assert "memory_extraction" not in seg.metadata


# ---------------------------------------------------------------------------
# Import refactor invariant tests (P0)
# ---------------------------------------------------------------------------


def test_producer_imports_cleanly():
    """producer.py must import without NameError after the _sw module refactor."""
    import importlib

    import mammamiradio.scheduling.producer as _prod

    importlib.reload(_prod)  # raises NameError if any bare scriptwriter ref was missed


def test_write_banter_resolves_after_scriptwriter_reload():
    """_sw.write_banter must resolve to the new function body after importlib.reload().

    Core invariant for hot-reload: producer.py references scriptwriter via module
    object (_sw), so reloading scriptwriter updates the function reference used by
    all subsequent calls in the producer loop.
    """
    import importlib

    import mammamiradio.hosts.scriptwriter as _sw

    original_fn = _sw.write_banter
    importlib.reload(_sw)
    reloaded_fn = _sw.write_banter

    # After reload the module is re-executed; the function object is new.
    assert reloaded_fn is not original_fn, (
        "write_banter should be a new function object after reload — "
        "if this fails, the producer still holds a stale name-bound reference"
    )


# ---------------------------------------------------------------------------
# _pick_canned_clip unit tests
# ---------------------------------------------------------------------------


def test_pick_canned_clip_returns_none_for_empty_dir(tmp_path):
    """_pick_canned_clip returns None when the subdir has no clips."""
    from mammamiradio.scheduling.producer import _canned_clip_cache, _pick_canned_clip

    empty_dir = tmp_path / "banter"
    empty_dir.mkdir()
    _canned_clip_cache.clear()
    with patch(f"{PRODUCER_MODULE}._DEMO_ASSETS_DIR", tmp_path):
        result = _pick_canned_clip("banter")
    assert result is None


def test_pick_canned_clip_returns_manifested_recovery_file(tmp_path):
    """_pick_canned_clip returns a reviewed, hash-valid recovery path."""
    from mammamiradio.scheduling.producer import _canned_clip_cache, _pick_canned_clip, _recently_played_clips

    clip1 = _manifest_recovery_clip(tmp_path, "clip1.mp3", b"reviewed" * 300)
    _canned_clip_cache.clear()
    _recently_played_clips.clear()
    with patch(f"{PRODUCER_MODULE}._DEMO_ASSETS_DIR", tmp_path):
        result = _pick_canned_clip("recovery")
    assert result == clip1
    assert list(_recently_played_clips) == ["clip1.mp3"]


def test_pick_canned_clip_rejects_deleted_cached_path(tmp_path):
    """A cached Path that vanished is skipped instead of returned."""
    from mammamiradio.scheduling.producer import _canned_clip_cache, _pick_canned_clip, _recently_played_clips

    clip = _manifest_recovery_clip(tmp_path, "gone.mp3", b"reviewed" * 300)
    _canned_clip_cache.clear()
    _recently_played_clips.clear()
    with patch(f"{PRODUCER_MODULE}._DEMO_ASSETS_DIR", tmp_path):
        assert _pick_canned_clip("recovery") == clip
        clip.unlink()
        _recently_played_clips.clear()
        assert _pick_canned_clip("recovery") is None
    assert list(_recently_played_clips) == []


def test_pick_canned_clip_rejects_tiny_cached_path(tmp_path):
    """A truncated cached clip is skipped without ffprobe work."""
    from mammamiradio.scheduling.producer import _canned_clip_cache, _pick_canned_clip, _recently_played_clips

    _manifest_recovery_clip(tmp_path, "tiny.mp3", b"x" * 1024)
    _canned_clip_cache.clear()
    _recently_played_clips.clear()
    with patch(f"{PRODUCER_MODULE}._DEMO_ASSETS_DIR", tmp_path):
        assert _pick_canned_clip("recovery") is None
    assert list(_recently_played_clips) == []


def test_pick_canned_clip_clears_recently_played_when_exhausted(tmp_path):
    """When all clips are recently played, the cache resets and re-picks."""
    from mammamiradio.scheduling.producer import _canned_clip_cache, _pick_canned_clip, _recently_played_clips

    clip1 = _manifest_recovery_clip(tmp_path, "clip1.mp3", b"reviewed" * 300)
    _canned_clip_cache.clear()
    _recently_played_clips.clear()
    _recently_played_clips.append("clip1.mp3")  # Mark the only clip as recently played
    with patch(f"{PRODUCER_MODULE}._DEMO_ASSETS_DIR", tmp_path):
        result = _pick_canned_clip("recovery")
    assert result == clip1
    # recently_played should have been cleared and then clip1 re-added
    assert "clip1.mp3" in _recently_played_clips


def test_pick_canned_clip_nonexistent_dir(tmp_path):
    """_pick_canned_clip returns None when the subdir doesn't exist."""
    from mammamiradio.scheduling.producer import _canned_clip_cache, _pick_canned_clip

    _canned_clip_cache.clear()
    with patch(f"{PRODUCER_MODULE}._DEMO_ASSETS_DIR", tmp_path):
        result = _pick_canned_clip("nonexistent")
    assert result is None


def test_packaged_asset_paths_are_never_tmp_renders(tmp_path):
    """Packaged demo assets must not be classified as deletable temp renders."""
    from mammamiradio.scheduling import producer

    demo_root = tmp_path / "assets" / "demo"
    packaged = demo_root / "recovery" / "continuity_1.mp3"
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(b"\x00" * 2048)
    tmp_render = tmp_path / "tmp" / "render.mp3"
    tmp_render.parent.mkdir()
    tmp_render.write_bytes(b"\x00" * 2048)

    with patch(f"{PRODUCER_MODULE}._DEMO_ASSETS_DIR", demo_root):
        assert producer._is_packaged_asset(packaged) is True
        assert producer._is_packaged_asset(tmp_render) is False
        assert (
            producer._is_tmp_render(
                Segment(type=SegmentType.BANTER, path=packaged, ephemeral=True),
                tmp_render.parent,
            )
            is False
        )
        assert (
            producer._is_tmp_render(
                Segment(type=SegmentType.BANTER, path=tmp_render, ephemeral=True),
                tmp_render.parent,
            )
            is True
        )


def test_unlink_if_tmp_render_keeps_packaged_assets(tmp_path):
    """Even a wrongly-ephemeral packaged clip must survive cleanup."""
    from mammamiradio.scheduling import producer

    demo_root = tmp_path / "assets" / "demo"
    packaged = demo_root / "recovery" / "continuity_1.mp3"
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(b"\x00" * 2048)
    tmp_render = tmp_path / "tmp" / "render.mp3"
    tmp_render.parent.mkdir()
    tmp_render.write_bytes(b"\x00" * 2048)

    with patch(f"{PRODUCER_MODULE}._DEMO_ASSETS_DIR", demo_root):
        producer._unlink_if_tmp_render(
            Segment(type=SegmentType.BANTER, path=packaged, ephemeral=True),
            tmp_render.parent,
        )
        producer._unlink_if_tmp_render(
            Segment(type=SegmentType.BANTER, path=tmp_render, ephemeral=True),
            tmp_render.parent,
        )

    assert packaged.exists()
    assert not tmp_render.exists()


# ---------------------------------------------------------------------------
# _record_motif unit test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_motif_handles_exception():
    """_record_motif catches exceptions without propagating."""
    from mammamiradio.scheduling.producer import _record_motif

    state = _make_state()
    mock_persona = AsyncMock()
    mock_persona.record_motif = AsyncMock(side_effect=RuntimeError("db error"))
    state.persona_store = mock_persona

    track = Track(title="Test", artist="Artist", duration_ms=1000, spotify_id="t1")
    # Should not raise
    await _record_motif(state, track)
    mock_persona.record_motif.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_motif_skips_when_no_persona_store():
    """_record_motif returns early when no persona_store."""
    from mammamiradio.scheduling.producer import _record_motif

    state = _make_state()
    state.persona_store = None

    track = Track(title="Test", artist="Artist", duration_ms=1000, spotify_id="t1")
    await _record_motif(state, track)


# ---------------------------------------------------------------------------
# _sync_listener_session_persona unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listener_session_receipt_no_persona_store():
    """The receipt scheduler returns early when no persona_store exists."""
    from mammamiradio.scheduling.producer import _sync_listener_session_persona

    state = _make_state()
    state.persona_store = None
    _sync_listener_session_persona(state)


@pytest.mark.asyncio
async def test_listener_session_receipt_schedules_one_pending_epoch():
    """Only the station epoch receipt, not a timer, schedules persistence."""
    from mammamiradio.scheduling.producer import _sync_listener_session_persona

    state = _make_state()
    mock_persona = MagicMock()
    mock_persona.start_session = AsyncMock(return_value=True)
    state.persona_store = mock_persona
    state.listener_session.observe_active_count(1, now=0.0)

    _sync_listener_session_persona(state)
    tasks = list(state.listener_session_tasks)
    assert len(tasks) == 1
    await asyncio.gather(*tasks)
    mock_persona.start_session.assert_awaited_once_with("listener-epoch-1")
    assert state.listener_session.pending_persona_epochs == ()


@pytest.mark.asyncio
async def test_listener_session_receipt_does_nothing_without_pending_epoch():
    """No active epoch means there is no persona receipt to schedule."""
    from mammamiradio.scheduling.producer import _sync_listener_session_persona

    state = _make_state()
    mock_persona = MagicMock()
    mock_persona.start_session = AsyncMock()
    state.persona_store = mock_persona

    _sync_listener_session_persona(state)
    mock_persona.start_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_listener_session_receipt_failure_uses_capped_monotonic_backoff():
    """A failed commit stays pending without hot-looping the database."""
    from mammamiradio.core.listener_session import ListenerSession
    from mammamiradio.scheduling.producer import _sync_listener_session_persona

    clock = [0.0]
    state = _make_state()
    state.listener_session = ListenerSession(monotonic=lambda: clock[0])
    state.listener_session.observe_active_count(1)
    mock_persona = MagicMock()
    mock_persona.start_session = AsyncMock(side_effect=[False, True])
    state.persona_store = mock_persona

    _sync_listener_session_persona(state)
    await asyncio.gather(*list(state.listener_session_tasks))
    assert state.listener_session.pending_persona_epochs == (1,)
    assert state.listener_session_persona_retry_at == pytest.approx(1.0)

    _sync_listener_session_persona(state)
    assert mock_persona.start_session.await_count == 1
    clock[0] = 0.999
    _sync_listener_session_persona(state)
    assert mock_persona.start_session.await_count == 1

    clock[0] = 1.0
    _sync_listener_session_persona(state)
    await asyncio.gather(*list(state.listener_session_tasks))
    assert mock_persona.start_session.await_count == 2
    assert state.listener_session.pending_persona_epochs == ()
    assert state.listener_session_persona_retry_at == 0.0
    assert state.listener_session_persona_retry_attempts == 0


@pytest.mark.asyncio
async def test_listener_session_receipt_exception_keeps_epoch_pending():
    from mammamiradio.core.listener_session import ListenerSession
    from mammamiradio.scheduling.producer import _sync_listener_session_persona

    state = _make_state()
    state.listener_session = ListenerSession(monotonic=lambda: 10.0)
    state.listener_session.observe_active_count(1)
    mock_persona = MagicMock()
    mock_persona.start_session = AsyncMock(side_effect=RuntimeError("database unavailable"))
    state.persona_store = mock_persona

    _sync_listener_session_persona(state)
    await asyncio.gather(*list(state.listener_session_tasks))
    assert state.listener_session.pending_persona_epochs == (1,)
    assert state.listener_session_persona_retry_at == pytest.approx(11.0)


def _claimed_companionship_state():
    from mammamiradio.core.listener_session import ListenerSession

    state = _make_state()
    state.listener_session = ListenerSession(monotonic=lambda: 1_800.0)
    state.listener_session.observe_active_count(1, now=0.0)
    claim = state.listener_session.claim_companionship(now=1_800.0)
    assert claim is not None
    return state, claim


@pytest.mark.parametrize(
    "blocked_field",
    [
        "not_natural",
        "chaos",
        "operator",
        "no_llm",
        "prompt_fact",
        "directive",
        "request",
        "heading",
        "ritual_gag",
        "special_mode",
        "release_campaign",
    ],
)
def test_companionship_claim_eligibility_excludes_priority_lanes(blocked_field):
    from mammamiradio.home.context_director import PromptFact
    from mammamiradio.scheduling.producer import _companionship_banter_eligible

    state = _make_state()
    kwargs = {
        "natural_banter": True,
        "chaos_subtype": None,
        "is_operator_forced": False,
        "prompt_fact": None,
        "script_llm_available": True,
        "special_mode_active": False,
    }
    if blocked_field == "not_natural":
        kwargs["natural_banter"] = False
    elif blocked_field == "chaos":
        kwargs["chaos_subtype"] = ChaosSubtype.FOURTH_WALL
    elif blocked_field == "operator":
        kwargs["is_operator_forced"] = True
    elif blocked_field == "no_llm":
        kwargs["script_llm_available"] = False
    elif blocked_field == "prompt_fact":
        kwargs["prompt_fact"] = PromptFact("f", "weather.home", "weather", "x", "Rain", 1)
    elif blocked_field == "directive":
        state.ha_pending_directive = "React now"
    elif blocked_field == "request":
        state.pending_requests.append({"type": "message"})
    elif blocked_field == "heading":
        state.heading_pending_announcement = "New direction"
    elif blocked_field == "ritual_gag":
        state.ha_running_gag = "A ritual callback"
    elif blocked_field == "special_mode":
        kwargs["special_mode_active"] = True
    elif blocked_field == "release_campaign":
        state.release_campaign = SimpleNamespace(enabled=True, is_due=lambda: True)

    assert _companionship_banter_eligible(state, **kwargs) is False


def test_companionship_generated_proof_stamps_segment_and_admission_marks_queued():
    from mammamiradio.scheduling.producer import (
        _companionship_metadata_for_generated_banter,
        _mark_companionship_segment_queued,
    )

    state, claim = _claimed_companionship_state()
    commit = SimpleNamespace(
        companionship=SimpleNamespace(duration_bucket=claim.prompt_context.duration_bucket),
    )
    metadata = _companionship_metadata_for_generated_banter(state, claim, commit)
    segment = Segment(type=SegmentType.BANTER, path=Path("cue.mp3"), metadata=metadata)

    assert metadata == {"listener_session_epoch": 1, "listener_session_cue": "companionship"}
    _mark_companionship_segment_queued(state, segment)
    assert state.listener_session.companionship_cue_state.value == "queued"


@pytest.mark.asyncio
async def test_natural_banter_claims_generates_and_queues_companionship_once(tmp_path):
    """The full producer lane transfers one claim through proof to queue admission."""

    state = _make_state()
    now = [0.0]
    state.listener_session = ListenerSession(monotonic=lambda: now[0])
    state.listener_session.observe_active_count(1)
    consume_milestone = AsyncMock()
    state.persona_store = SimpleNamespace(consume_milestone=consume_milestone)
    now[0] = 1800.0
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    generated_audio = tmp_path / "generated.mp3"
    generated_audio.write_bytes(b"generated audio" * 200)
    seen_contexts = []

    async def _write_banter(*_args, **kwargs):
        context = kwargs.get("companionship_context")
        seen_contexts.append(context)
        assert context is not None
        return (
            [(host, "Siamo ancora qui, insieme alla musica.")],
            BanterCommit(
                companionship=CompanionshipBanterCommit(duration_bucket=context.duration_bucket),
                persona_milestone=5,
                pending_joke={"text": "the queued studio umbrella", "punch": 4.0},
            ),
        )

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, side_effect=_write_banter),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition",
            new_callable=AsyncMock,
            return_value=(host, "Continuiamo.", None),
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=generated_audio),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=generated_audio),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=generated_audio),
        patch(f"{PRODUCER_MODULE}.concat_files"),
        patch(f"{PRODUCER_MODULE}._apply_talk_bed", new_callable=AsyncMock, return_value=generated_audio),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    segment = queue.get_nowait()
    assert len(seen_contexts) == 1
    assert segment.metadata["listener_session_epoch"] == 1
    assert segment.metadata["listener_session_cue"] == "companionship"
    assert state.listener_session.companionship_cue_state is ListenerSessionCueState.QUEUED
    assert state.listener_session.claim_companionship() is None
    assert "the queued studio umbrella" in state.running_jokes
    consume_milestone.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_companionship_stale_after_egress_records_listener_session_reason(tmp_path):
    """The main queue funnel must preserve the cue's concrete stale reason."""

    state = _make_state()
    now = [0.0]
    state.listener_session = ListenerSession(monotonic=lambda: now[0])
    state.listener_session.observe_active_count(1)
    now[0] = 1800.0
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    generated_audio = tmp_path / "generated.mp3"
    generated_audio.write_bytes(b"generated audio" * 200)

    async def _write_banter(*_args, **kwargs):
        context = kwargs["companionship_context"]
        return (
            [(host, "We've had company for roughly half an hour.")],
            BanterCommit(
                companionship=CompanionshipBanterCommit(duration_bucket=context.duration_bucket),
            ),
        )

    async def _disconnect_during_egress(segment, _config):
        state.listener_session.observe_active_count(0, now=1801.0)
        state.listeners_active = 0
        return segment

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, side_effect=_write_banter),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition",
            new_callable=AsyncMock,
            return_value=(host, "The studio keeps moving.", None),
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=generated_audio),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=generated_audio),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=generated_audio),
        patch(f"{PRODUCER_MODULE}.concat_files"),
        patch(f"{PRODUCER_MODULE}._apply_talk_bed", new_callable=AsyncMock, return_value=generated_audio),
        patch(f"{PRODUCER_MODULE}._apply_egress", new_callable=AsyncMock, side_effect=_disconnect_during_egress),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            deadline = asyncio.get_running_loop().time() + 2.0
            while not state.discard_by_reason.get(GenerationWasteReason.LISTENER_SESSION_STALE):
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("listener-session stale admission was not recorded")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert queue.empty()
    assert state.discard_by_reason[GenerationWasteReason.LISTENER_SESSION_STALE] == 1
    assert not state.discard_by_reason.get(GenerationWasteReason.EGRESS_STALE)
    assert state.listener_session.companionship_cue_state is ListenerSessionCueState.ABANDONED


def test_companionship_fallback_without_generated_proof_abandons_and_is_not_stamped():
    from mammamiradio.scheduling.producer import _companionship_metadata_for_generated_banter

    state, claim = _claimed_companionship_state()
    assert _companionship_metadata_for_generated_banter(state, claim, None) == {}
    assert state.listener_session.companionship_cue_state.value == "abandoned"


def test_unowned_companionship_attempt_is_settled_once_and_never_reopened():
    from mammamiradio.scheduling.producer import _abandon_unowned_companionship_attempt

    state, _claim = _claimed_companionship_state()
    assert _abandon_unowned_companionship_attempt(state) is True
    assert state.listener_session.companionship_cue_state is ListenerSessionCueState.ABANDONED
    assert _abandon_unowned_companionship_attempt(state) is False
    assert state.listener_session.claim_companionship() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_stage", ["generation", "tts", "quality"])
async def test_companionship_generation_pipeline_failures_abandon_without_retry(tmp_path, failed_stage):
    from mammamiradio.audio.audio_quality import AudioQualityError

    state = _make_state()
    now = [0.0]
    state.listener_session = ListenerSession(monotonic=lambda: now[0])
    state.listener_session.observe_active_count(1)
    consume_milestone = AsyncMock()
    state.persona_store = SimpleNamespace(consume_milestone=consume_milestone)
    now[0] = 1800.0
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    generated_audio = tmp_path / "generated.mp3"
    generated_audio.write_bytes(b"generated audio" * 200)

    async def _write_banter(*_args, **kwargs):
        if failed_stage == "generation":
            raise RuntimeError("writer failed")
        context = kwargs["companionship_context"]
        return (
            [(host, "We have had company for a while, piano piano.")],
            BanterCommit(
                companionship=CompanionshipBanterCommit(duration_bucket=context.duration_bucket),
                persona_milestone=5,
                pending_joke={"text": "the unheard studio umbrella", "punch": 4.0},
            ),
        )

    async def _synthesize(*_args, **_kwargs):
        if failed_stage == "tts":
            raise RuntimeError("voice failed")
        return generated_audio

    def _quality(*_args, **_kwargs):
        if failed_stage == "quality":
            raise AudioQualityError("generated break is not airable")
        return None

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, side_effect=_write_banter),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition",
            new_callable=AsyncMock,
            return_value=(host, "The studio keeps moving.", None),
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=_synthesize),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, side_effect=_synthesize),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=generated_audio),
        patch(f"{PRODUCER_MODULE}.concat_files"),
        patch(f"{PRODUCER_MODULE}._apply_talk_bed", new_callable=AsyncMock, return_value=generated_audio),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", side_effect=_quality),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            deadline = asyncio.get_running_loop().time() + 2.0
            while state.listener_session.companionship_cue_state is not ListenerSessionCueState.ABANDONED:
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError(f"{failed_stage} failure did not settle the companionship cue")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert queue.empty()
    assert state.listener_session.claim_companionship() is None
    assert "the unheard studio umbrella" not in state.running_jokes
    assert state.pending_verbal_gag is None
    consume_milestone.assert_not_awaited()


@pytest.mark.asyncio
async def test_companionship_quality_fallback_is_ordinary_and_does_not_reuse_failed_cue(tmp_path):
    from mammamiradio.audio.audio_quality import AudioQualityError

    state = _make_state()
    now = [0.0]
    state.listener_session = ListenerSession(monotonic=lambda: now[0])
    state.listener_session.observe_active_count(1)
    now[0] = 1800.0
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    generated_audio = tmp_path / "generated.mp3"
    generated_audio.write_bytes(b"generated audio" * 200)
    canned_audio = tmp_path / "canned.mp3"
    canned_audio.write_bytes(b"canned audio" * 200)

    async def _write_banter(*_args, **kwargs):
        context = kwargs["companionship_context"]
        return (
            [(host, "We have had company for a while, piano piano.")],
            BanterCommit(
                companionship=CompanionshipBanterCommit(duration_bucket=context.duration_bucket),
            ),
        )

    quality_calls = 0

    def _quality(*_args, **_kwargs):
        nonlocal quality_calls
        quality_calls += 1
        if quality_calls == 1:
            raise AudioQualityError("generated break is not airable")

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, side_effect=_write_banter),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition",
            new_callable=AsyncMock,
            return_value=(host, "The studio keeps moving.", None),
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=generated_audio),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=generated_audio),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=generated_audio),
        patch(f"{PRODUCER_MODULE}.concat_files"),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", side_effect=_quality),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=canned_audio),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    segment = queue.get_nowait()
    assert segment.path == canned_audio
    assert segment.metadata["canned"] is True
    assert "listener_session_epoch" not in segment.metadata
    assert "listener_session_cue" not in segment.metadata
    assert state.listener_session.companionship_cue_state is ListenerSessionCueState.ABANDONED
    assert state.listener_session.claim_companionship() is None


def test_companionship_admission_fence_rejects_disconnect_before_queue():
    from mammamiradio.scheduling.producer import _companionship_admission_stale_reason

    state, claim = _claimed_companionship_state()
    segment = Segment(
        type=SegmentType.BANTER,
        path=Path("cue.mp3"),
        metadata={"listener_session_epoch": claim.epoch, "listener_session_cue": "companionship"},
    )
    state.listener_session.observe_active_count(0, now=1_801.0)

    assert _companionship_admission_stale_reason(state, segment) == GenerationWasteReason.LISTENER_SESSION_STALE


@pytest.mark.parametrize(
    "reason",
    [
        GenerationWasteReason.QUALITY_GATE_REJECT,
        GenerationWasteReason.SESSION_STOPPED,
        GenerationWasteReason.STALE_SOURCE,
        GenerationWasteReason.STALE_PLAYLIST,
        GenerationWasteReason.STALE_CHAOS,
        GenerationWasteReason.STALE_CONTINUITY,
        GenerationWasteReason.AIR_NEXT_OVERFLOW,
        GenerationWasteReason.EGRESS_STALE,
        GenerationWasteReason.OPERATOR_PURGE,
        GenerationWasteReason.OPERATOR_STOP,
        GenerationWasteReason.OPERATOR_PANIC,
        GenerationWasteReason.OPERATOR_QUEUE_REMOVE,
        GenerationWasteReason.LISTENER_SESSION_STALE,
    ],
)
def test_record_discard_centrally_abandons_companionship_for_every_queue_path(reason):
    state, claim = _claimed_companionship_state()
    assert state.listener_session.mark_companionship_queued(claim.epoch) is True
    segment = Segment(
        type=SegmentType.BANTER,
        path=Path("cue.mp3"),
        metadata={"listener_session_epoch": claim.epoch, "listener_session_cue": "companionship"},
    )

    state.record_discard(segment, reason=reason)
    assert state.listener_session.companionship_cue_state.value == "abandoned"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_text",
    [
        "Someone just tuned in, apparently.",
        "Looks like we have a new friend with us.",
        "Rieccoti con noi!",
    ],
)
async def test_listener_truth_guard_repairs_final_assembled_copy(unsafe_text):
    config = _make_config()
    state = _make_state()
    safe_lines = [(config.hosts[0], "Abbiamo compagnia da un po', ma la musica decide tutto.")]
    unsafe_lines = [(config.hosts[0], unsafe_text)]
    with patch(
        f"{PRODUCER_MODULE}._sw.repair_banter_without_listener_context",
        new=AsyncMock(return_value=safe_lines),
    ) as repair:
        lines, transition, changed, transition_replaced = await _listener_truth_guard(
            state,
            config,
            unsafe_lines,
            transition_text="And back to the music.",
        )

    assert changed is True
    assert transition_replaced is False
    assert transition == "And back to the music."
    assert [(line.host, line.text) for line in lines] == safe_lines
    assert all(line.delivery == "neutral" for line in lines)
    repair.assert_awaited_once()


@pytest.mark.asyncio
async def test_listener_truth_guard_clears_delivery_from_repaired_copy():
    """A safety replacement must reach TTS as clean, neutral dialogue metadata."""
    config = _make_config()
    state = _make_state()
    host = config.hosts[0]
    unsafe_lines = [DialogueLine(host, "Someone just tuned in, apparently.", "energetic")]
    repaired_lines = [DialogueLine(host, "The studio keeps moving, amici.", "playful")]

    with patch(
        f"{PRODUCER_MODULE}._sw.repair_banter_without_listener_context",
        new=AsyncMock(return_value=repaired_lines),
    ):
        lines, _transition, changed, _transition_replaced = await _listener_truth_guard(
            state,
            config,
            unsafe_lines,
        )

    assert changed is True
    assert lines == [DialogueLine(host, "The studio keeps moving, amici.")]
    assert lines[0].text == "The studio keeps moving, amici."
    assert lines[0].delivery == "neutral"


@pytest.mark.asyncio
async def test_listener_truth_guard_allows_one_fact_bound_named_resident_return():
    from mammamiradio.core.listener_truth import home_return_authority_for_directive

    config = _make_config()
    state = _make_state()
    state.last_banter_return_authority = home_return_authority_for_directive(
        "ha:person.florian_horner",
        "Florian è appena tornato a casa. Un caloroso bentornato.",
    )
    lines = [(config.hosts[0], "Bentornato Florian.")]

    with patch(
        f"{PRODUCER_MODULE}._sw.repair_banter_without_listener_context",
        new=AsyncMock(),
    ) as repair:
        guarded, transition, changed, transition_replaced = await _listener_truth_guard(
            state,
            config,
            lines,
        )

    assert [(line.host, line.text) for line in guarded] == lines
    assert guarded[0].delivery == "neutral"
    assert transition is None
    assert changed is False
    assert transition_replaced is False
    assert state.last_banter_return_authority is not None
    repair.assert_not_awaited()


@pytest.mark.asyncio
async def test_listener_truth_guard_rejects_return_line_that_names_another_resident():
    from mammamiradio.core.listener_truth import home_return_authority_for_directive

    config = _make_config()
    state = _make_state()
    state.last_banter_return_authority = home_return_authority_for_directive(
        "ha:person.florian_horner",
        "Florian è appena tornato a casa. Un caloroso bentornato.",
    )
    safe_lines = [(config.hosts[0], "The studio keeps moving, amici.")]

    with patch(
        f"{PRODUCER_MODULE}._sw.repair_banter_without_listener_context",
        new=AsyncMock(return_value=safe_lines),
    ) as repair:
        guarded, _transition, changed, _transition_replaced = await _listener_truth_guard(
            state,
            config,
            [(config.hosts[0], "Florian is back with Sabrina.")],
        )

    assert [(line.host, line.text) for line in guarded] == safe_lines
    assert all(line.delivery == "neutral" for line in guarded)
    assert changed is True
    assert state.last_banter_return_authority is None
    repair.assert_awaited_once()


@pytest.mark.asyncio
async def test_listener_truth_guard_rejects_unbound_named_return():
    config = _make_config()
    state = _make_state()
    safe_lines = [(config.hosts[0], "The music continues, amici.")]

    with patch(
        f"{PRODUCER_MODULE}._sw.repair_banter_without_listener_context",
        new=AsyncMock(return_value=safe_lines),
    ) as repair:
        guarded, _transition, changed, _transition_replaced = await _listener_truth_guard(
            state,
            config,
            [(config.hosts[0], "Bentornato Florian.")],
        )

    assert [(line.host, line.text) for line in guarded] == safe_lines
    assert all(line.delivery == "neutral" for line in guarded)
    assert changed is True
    repair.assert_awaited_once()


@pytest.mark.asyncio
async def test_listener_truth_guard_abandons_second_unsafe_generation():
    config = _make_config()
    state = _make_state()
    unsafe_lines = [(config.hosts[0], "Qualcuno si è appena sintonizzato.")]
    with patch(
        f"{PRODUCER_MODULE}._sw.repair_banter_without_listener_context",
        new=AsyncMock(return_value=unsafe_lines),
    ):
        lines, transition, changed, _transition_replaced = await _listener_truth_guard(
            state,
            config,
            unsafe_lines,
        )

    from mammamiradio.core.listener_truth import contains_unsafe_listener_claims

    assert changed is True
    assert transition is None
    assert not contains_unsafe_listener_claims(text for _host, text in lines)


@pytest.mark.asyncio
async def test_listener_truth_guard_replaces_unsafe_transition():
    config = _make_config()
    state = _make_state()
    safe_lines = [(config.hosts[0], "La musica continua, e noi pure.")]
    with patch(
        f"{PRODUCER_MODULE}._sw.repair_banter_without_listener_context",
        new=AsyncMock(return_value=safe_lines),
    ):
        lines, transition, changed, transition_replaced = await _listener_truth_guard(
            state,
            config,
            safe_lines,
            transition_text="Welcome back, amici.",
        )

    assert changed is True
    assert transition_replaced is True
    assert transition is not None
    assert "Welcome back" not in transition
    assert [(line.host, line.text) for line in lines] == safe_lines
    assert all(line.delivery == "neutral" for line in lines)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("generated_text", "expected_accepted"),
    [
        ("The studio keeps moving, piano piano.", True),
        ("Someone just tuned in, apparently.", False),
    ],
)
async def test_producer_applies_banter_mutations_only_after_final_truth_acceptance(
    tmp_path,
    generated_text,
    expected_accepted,
):
    state = _make_state()
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    safe_repair = [(host, "The music continues without guessing who is listening.")]
    commit = BanterCommit(pending_joke={"text": "the studio umbrella", "punch": 4.0})

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            return_value=([(host, generated_text)], commit),
        ),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition",
            new_callable=AsyncMock,
            return_value=(host, "The studio keeps moving.", None),
        ),
        patch(
            f"{SCRIPTWRITER_MODULE}.repair_banter_without_listener_context",
            new_callable=AsyncMock,
            return_value=safe_repair,
        ) as repair,
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=1.0),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert ("the studio umbrella" in state.running_jokes) is expected_accepted
    if expected_accepted:
        repair.assert_not_awaited()
    else:
        repair.assert_awaited_once()


# ---------------------------------------------------------------------------
# _pick_brand unit tests
# ---------------------------------------------------------------------------


def test_pick_brand_avoids_recent():
    """_pick_brand avoids recently-aired brands."""
    from mammamiradio.hosts.ad_creative import AdBrand, _pick_brand

    brands = [
        AdBrand(name="BrandA", tagline="A", category="tech"),
        AdBrand(name="BrandB", tagline="B", category="food"),
    ]

    class FakeHistory:
        def __init__(self, brand_name):
            self.brand = brand_name

    # BrandA was just aired, so BrandB should be picked
    history = [FakeHistory("BrandA")]
    # Run multiple times to verify BrandA is avoided
    for _ in range(10):
        result = _pick_brand(brands, history)
        assert result.name == "BrandB"


def test_pick_brand_all_recent_allows_repeats():
    """_pick_brand allows repeats when all brands are recently aired."""
    from mammamiradio.hosts.ad_creative import AdBrand, _pick_brand

    brands = [
        AdBrand(name="BrandA", tagline="A", category="tech"),
    ]

    class FakeHistory:
        def __init__(self, brand_name):
            self.brand = brand_name

    history = [FakeHistory("BrandA")]
    result = _pick_brand(brands, history)
    assert result.name == "BrandA"


# ---------------------------------------------------------------------------
# _latest_music_file / _try_crossfade unit tests
# ---------------------------------------------------------------------------


def test_latest_music_file_returns_none_for_empty_dir(tmp_path):
    """_latest_music_file returns None when no music files exist."""
    from mammamiradio.scheduling import producer

    producer._last_music_file = None
    from mammamiradio.scheduling.producer import _latest_music_file

    result = _latest_music_file(tmp_path)
    assert result is None


def test_latest_music_file_returns_most_recent(tmp_path):
    """_latest_music_file returns the most recent music file."""
    import time

    from mammamiradio.scheduling import producer

    producer._last_music_file = None
    from mammamiradio.scheduling.producer import _latest_music_file

    old = tmp_path / "music_old.mp3"
    old.write_bytes(b"old")
    time.sleep(0.02)
    new = tmp_path / "music_new.mp3"
    new.write_bytes(b"new")

    result = _latest_music_file(tmp_path)
    assert result == new


def test_latest_music_file_uses_cache():
    """_latest_music_file returns cached path when available."""
    from mammamiradio.scheduling import producer
    from mammamiradio.scheduling.producer import _latest_music_file

    fake_path = Path("/tmp/music_cached.mp3")
    fake_path.parent.mkdir(parents=True, exist_ok=True)
    fake_path.write_bytes(b"cached")
    producer._last_music_file = fake_path

    result = _latest_music_file(Path("/tmp/nonexistent"))
    assert result == fake_path

    # Cleanup
    producer._last_music_file = None
    fake_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_try_crossfade_no_music_file():
    """_try_crossfade returns voice_path when no music file exists."""
    from mammamiradio.scheduling import producer
    from mammamiradio.scheduling.producer import _try_crossfade

    producer._last_music_file = None
    config = _make_config()
    config.tmp_dir = Path("/tmp/mammamiradio_test_xfade")
    config.tmp_dir.mkdir(exist_ok=True)
    voice = config.tmp_dir / "voice.mp3"
    voice.write_bytes(b"voice")
    output = config.tmp_dir / "output.mp3"

    result = await _try_crossfade(voice, config, output, None)
    assert result == voice

    # Cleanup
    voice.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_try_crossfade_success(tmp_path):
    """_try_crossfade returns output_path on successful crossfade."""
    from mammamiradio.scheduling import producer
    from mammamiradio.scheduling.producer import _try_crossfade

    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"voice")
    music = tmp_path / "music_test.mp3"
    music.write_bytes(b"music")
    output = tmp_path / "output.mp3"

    producer._last_music_file = music
    config = _make_config()
    config.tmp_dir = tmp_path

    def fake_crossfade(*args, **kwargs):
        output.write_bytes(b"crossfaded")

    with patch(f"{PRODUCER_MODULE}.crossfade_voice_over_music", side_effect=fake_crossfade):
        result = await _try_crossfade(voice, config, output, music)

    assert result == output
    producer._last_music_file = None


@pytest.mark.asyncio
async def test_try_crossfade_failure_returns_voice(tmp_path):
    """_try_crossfade returns voice_path when crossfade fails."""
    from mammamiradio.scheduling import producer
    from mammamiradio.scheduling.producer import _try_crossfade

    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"voice")
    music = tmp_path / "music_test.mp3"
    music.write_bytes(b"music")
    output = tmp_path / "output.mp3"

    producer._last_music_file = music
    config = _make_config()
    config.tmp_dir = tmp_path

    with patch(f"{PRODUCER_MODULE}.crossfade_voice_over_music", side_effect=RuntimeError("ffmpeg failed")):
        result = await _try_crossfade(voice, config, output, music)

    assert result == voice
    producer._last_music_file = None


# ---------------------------------------------------------------------------
# _adjacent_music_source — the stale-bleed eligibility rule
# ---------------------------------------------------------------------------


def test_adjacent_music_source_returns_song_when_prev_is_music(tmp_path):
    from mammamiradio.scheduling import producer
    from mammamiradio.scheduling.producer import _adjacent_music_source

    song = tmp_path / "song.mp3"
    song.write_bytes(b"music")
    state = StationState()
    state.last_music_file = song
    state.last_enqueued_type = SegmentType.MUSIC
    producer._last_music_file = None
    try:
        assert _adjacent_music_source(state) == song
    finally:
        producer._last_music_file = None


@pytest.mark.parametrize(
    "prev",
    ["AD", "NEWS_FLASH", "BANTER", "STATION_ID", "SWEEPER", "TIME_CHECK", None],
)
def test_adjacent_music_source_none_when_prev_not_music(tmp_path, prev):
    """A song is stale the moment any non-music segment intervenes (or prev is
    unknown). It must never be offered as a bed/crossfade source."""
    from mammamiradio.scheduling import producer
    from mammamiradio.scheduling.producer import _adjacent_music_source

    song = tmp_path / "song.mp3"
    song.write_bytes(b"music")
    state = StationState()
    state.last_music_file = song  # song still on disk, but stale
    state.last_enqueued_type = getattr(SegmentType, prev) if prev else None
    producer._last_music_file = song
    try:
        assert _adjacent_music_source(state) is None
    finally:
        producer._last_music_file = None


# ---------------------------------------------------------------------------
# _synthesize_impossible_moment unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_impossible_moment(tmp_path):
    """_synthesize_impossible_moment synthesizes and returns a path."""
    from mammamiradio.scheduling.producer import _synthesize_impossible_moment

    config = _make_config()
    config.tmp_dir = tmp_path
    state = _make_state()

    with (
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=lambda *a, **kw: _fake_path()),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=_fake_path()),
    ):
        result = await _synthesize_impossible_moment("Che succede!", config, state)

    assert result == _fake_path()
    assert len(state.last_banter_script) == 1
    assert state.last_banter_script[0]["type"] == "impossible"


@pytest.mark.asyncio
async def test_synthesize_impossible_moment_forwards_music_path(tmp_path):
    """The eligible (adjacent-only) song the caller computes must reach the
    crossfade — so an impossible moment never beds over a stale track."""
    from mammamiradio.scheduling.producer import _synthesize_impossible_moment

    config = _make_config()
    config.tmp_dir = tmp_path
    state = _make_state()
    song = tmp_path / "adjacent_song.mp3"
    song.write_bytes(b"music")

    with (
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=lambda *a, **kw: _fake_path()),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=_fake_path()) as mock_xf,
    ):
        await _synthesize_impossible_moment("Che succede!", config, state, song)

    # 4th positional arg to _try_crossfade is the music source.
    assert mock_xf.call_args.args[3] == song


@pytest.mark.asyncio
async def test_synthesize_impossible_moment_defaults_to_no_music(tmp_path):
    """With no music passed, the crossfade gets None (dry line, no stale bleed)."""
    from mammamiradio.scheduling.producer import _synthesize_impossible_moment

    config = _make_config()
    config.tmp_dir = tmp_path
    state = _make_state()

    with (
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, side_effect=lambda *a, **kw: _fake_path()),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=_fake_path()) as mock_xf,
    ):
        await _synthesize_impossible_moment("Che succede!", config, state)

    assert mock_xf.call_args.args[3] is None


# ---------------------------------------------------------------------------
# _pick_brand weight tests
# ---------------------------------------------------------------------------


def test_pick_brand_weights_recurring():
    """_pick_brand weights recurring brands 3x higher."""
    from mammamiradio.hosts.ad_creative import AdBrand, _pick_brand

    brands = [
        AdBrand(name="Recurring", tagline="R", category="tech", recurring=True),
        AdBrand(name="OneShot", tagline="O", category="food", recurring=False),
    ]
    # With many picks, recurring should appear ~3x more often
    picks = [_pick_brand(brands, []).name for _ in range(100)]
    assert picks.count("Recurring") > picks.count("OneShot")


# ---------------------------------------------------------------------------
# _select_ad_creative unit tests
# ---------------------------------------------------------------------------


def test_select_ad_creative_voice_count_guard():
    """_select_ad_creative excludes multi-voice formats when only 1 voice available."""
    from mammamiradio.hosts.ad_creative import AdBrand, _select_ad_creative

    brand = AdBrand(name="TestBrand", tagline="Test", category="tech")
    state = _make_state()
    config = _make_config()
    # Ensure only 1 voice
    config.ads.voices = [MagicMock(role="hammer")]

    ad_format, sonic, roles = _select_ad_creative(brand, state, len(config.ads.voices))
    assert ad_format is not None
    assert sonic is not None
    assert len(roles) >= 1


def test_select_ad_creative_no_voices():
    """_select_ad_creative handles empty voice list."""
    from mammamiradio.hosts.ad_creative import AdBrand, _select_ad_creative

    brand = AdBrand(name="TestBrand", tagline="Test", category="food")
    state = _make_state()
    config = _make_config()
    config.ads.voices = []

    ad_format, _sonic, _roles = _select_ad_creative(brand, state, len(config.ads.voices))
    assert ad_format is not None


def test_select_ad_creative_multivoice_only_fallback():
    """When format_pool is all multi-voice and only 1 voice, falls back to a 1-voice format."""
    from mammamiradio.hosts.ad_creative import AdBrand, AdFormat, CampaignSpine, _select_ad_creative

    brand = AdBrand(
        name="MultiVoiceBrand",
        tagline="Test",
        category="tech",
        campaign=CampaignSpine(format_pool=["duo_scene", "testimonial"]),
    )
    state = _make_state()
    config = _make_config()
    config.ads.voices = [MagicMock(role="hammer")]  # only 1 voice

    ad_format, _sonic, _roles = _select_ad_creative(brand, state, len(config.ads.voices))
    # CLASSIC_PITCH is now 2-voice; fallback is one of the genuine 1-voice formats
    assert AdFormat(ad_format).voice_count < 2
    assert ad_format != AdFormat.CLASSIC_PITCH


# ---------------------------------------------------------------------------
# _cast_voices unit tests
# ---------------------------------------------------------------------------


def test_cast_voices_no_voices_configured():
    """_cast_voices falls back to host when no ad voices configured."""
    from mammamiradio.hosts.ad_creative import AdBrand, _cast_voices

    brand = AdBrand(name="TestBrand", tagline="Test", category="tech")
    config = _make_config()
    config.ads.voices = []

    result = _cast_voices(brand, config.ads.voices, config.hosts, ["hammer"])
    assert "hammer" in result
    assert result["hammer"].voice is not None


def test_cast_voices_reuses_when_exhausted():
    """_cast_voices reuses voices when more roles than available voices."""
    from mammamiradio.hosts.ad_creative import AdBrand, AdVoice, _cast_voices

    brand = AdBrand(name="TestBrand", tagline="Test", category="tech")
    config = _make_config()
    config.ads.voices = [AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="warm", role="hammer")]

    result = _cast_voices(brand, config.ads.voices, config.hosts, ["hammer", "maniac", "bureaucrat"])
    assert len(result) == 3
    # All three should have a voice assigned
    for role in ["hammer", "maniac", "bureaucrat"]:
        assert role in result


@pytest.mark.asyncio
async def test_record_motif_exception_is_swallowed():
    """_record_motif swallows exceptions from persona store."""
    from mammamiradio.scheduling.producer import _record_motif

    state = _make_state()
    mock_store = AsyncMock()
    mock_store.record_motif = AsyncMock(side_effect=Exception("DB error"))
    state.persona_store = mock_store
    track = Track(title="Song", artist="Artist", duration_ms=200_000, spotify_id="t1")
    # Should not raise
    await _record_motif(state, track)


@pytest.mark.asyncio
async def test_try_crossfade_success_with_music_file(tmp_path):
    """_try_crossfade returns the crossfade output when last music exists."""
    from mammamiradio.scheduling.producer import _set_last_music_file, _try_crossfade

    # Create fake music file and voice file
    music = tmp_path / "last_music.mp3"
    music.write_bytes(b"\x00" * 1000)
    _set_last_music_file(music)

    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"\x00" * 500)

    output = tmp_path / "crossfade.mp3"
    config = _make_config()
    config.tmp_dir = tmp_path

    with patch(f"{PRODUCER_MODULE}.crossfade_voice_over_music") as mock_xf:
        mock_xf.return_value = output
        output.write_bytes(b"\x00" * 800)  # simulate ffmpeg output
        result = await _try_crossfade(voice, config, output, music)
    assert result == output
    mock_xf.assert_called_once()


@pytest.mark.asyncio
async def test_prewarm_exception_returns_false(tmp_path):
    """prewarm returns False and logs when download throws."""
    from mammamiradio.scheduling.producer import prewarm_first_segment

    state = _make_state()
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue = asyncio.Queue()
    with patch(f"{PRODUCER_MODULE}.download_track", side_effect=Exception("network error")):
        result = await prewarm_first_segment(queue, state, config)
    assert result is False
    assert queue.qsize() == 0


@pytest.mark.asyncio
async def test_prewarm_success(tmp_path):
    """prewarm queues a segment on success."""
    from mammamiradio.scheduling.producer import prewarm_first_segment

    state = _make_state()
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue = asyncio.Queue()
    fake = tmp_path / "fake.mp3"
    fake.write_bytes(b"\x00" * 500)

    with (
        patch(f"{PRODUCER_MODULE}.download_track", return_value=fake),
        patch(f"{PRODUCER_MODULE}.normalize", side_effect=lambda src, dst, *a, **kw: dst.write_bytes(b"\x00" * 500)),
        patch(f"{PRODUCER_MODULE}.generate_track_rationale", return_value="great track"),
        patch(f"{PRODUCER_MODULE}.classify_track_crate", return_value="charts"),
    ):
        result = await prewarm_first_segment(queue, state, config)
    assert result is True
    assert queue.qsize() == 1


def test_select_ad_creative_single_voice_excludes_multi():
    """_select_ad_creative excludes duo formats when only 1 voice available."""
    from mammamiradio.hosts.ad_creative import AdBrand, AdFormat, _select_ad_creative

    brand = AdBrand(name="Test", tagline="test", category="food")
    state = _make_state()

    fmt, _sonic, _roles = _select_ad_creative(brand, state, num_voices=1)

    # Should not select a format that needs 2+ voices
    assert AdFormat(fmt).voice_count < 2


# ---------------------------------------------------------------------------
# force_next bypasses queue-full gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_next_bypasses_full_queue(tmp_path):
    """force_next is consumed even when the queue is at lookahead capacity.

    Regression test for the bug where setting state.force_next while the
    queue was full caused the trigger to be silently ignored — the producer
    loop hit the queue-full gate before reaching the force_next check.
    """
    queue: asyncio.Queue = asyncio.Queue()
    state = _make_state()
    config = _make_config()
    config.pacing.lookahead_segments = 1

    # Pre-fill the queue to capacity with a dummy music segment so the
    # producer would normally skip production and sleep.
    dummy = tmp_path / "dummy.mp3"
    dummy.write_bytes(b"\x00" * 200)
    await queue.put(Segment(type=SegmentType.MUSIC, path=dummy, metadata={}))
    assert queue.qsize() == 1

    # Set a forced trigger while the queue is already full.
    state.force_next = SegmentType.BANTER

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")
    banter_lines = [(host, "Ciao!")]

    with (
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...", None)
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            # Wait until force_next is consumed (producer processed the trigger).
            deadline = asyncio.get_event_loop().time() + 5.0
            while state.force_next is not None:
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("force_next was never consumed — queue-full gate blocked the trigger")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # force_next was consumed — the trigger was not silently dropped.
    assert state.force_next is None


# ---------------------------------------------------------------------------
# Ad break metadata — sonic_worlds and roles_used
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ad_break_sets_sonic_worlds_and_roles_in_last_ad_script():
    """last_ad_script and segment.metadata must include sonic_worlds and roles_used.

    Regression guard: ensures future refactors of the ad assembly loop
    cannot silently drop these fields from the dashboard state.
    """
    state = _make_state()

    class _Ledger:
        enabled = True

        def __init__(self):
            self.rows = []

        def record(self, row):
            self.rows.append(row)

    state.ledger = _Ledger()
    config = _make_config()
    config.pacing.ad_spots_per_break = 1
    queue: asyncio.Queue = asyncio.Queue()

    fake_script = AdScript(
        brand="Prezzoforte",
        parts=[AdPart(type="voice", text="Try Prezzoforte today.", role="hammer")],
        summary="Great deals at Prezzoforte",
        format="classic_pitch",
        sonic=SonicWorld(music_bed="cinematic", environment="piazza", transition_motif="fanfare"),
        roles_used=["hammer", "disclaimer_goblin"],
    )

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.AD),
        patch(f"{SCRIPTWRITER_MODULE}.write_ad", new_callable=AsyncMock, return_value=fake_script),
        patch(f"{PRODUCER_MODULE}.synthesize_ad", new_callable=AsyncMock, return_value=_fake_path()) as mock_synth_ad,
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.generate_bumper_jingle", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio"),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch.dict("os.environ", {"MAMMAMIRADIO_SKIP_QUALITY_GATE": "1"}),
    ):
        await _run_until_queued(queue, state, config)

    assert state.last_ad_script is not None, "last_ad_script was not set"
    assert "sonic_worlds" in state.last_ad_script, "sonic_worlds missing from last_ad_script"
    assert "roles_used" in state.last_ad_script, "roles_used missing from last_ad_script"
    assert state.last_ad_script["sonic_worlds"] == ["cinematic"]
    assert state.last_ad_script["roles_used"] == [["hammer", "disclaimer_goblin"]]
    assert mock_synth_ad.call_args.kwargs["cache_dir"] == config.cache_dir

    seg: Segment = queue.get_nowait()
    assert "sonic_worlds" in seg.metadata, "sonic_worlds missing from segment.metadata"
    assert "roles_used" in seg.metadata, "roles_used missing from segment.metadata"
    assert seg.metadata["sonic_worlds"] == ["cinematic"]
    assert seg.metadata["roles_used"] == [["hammer", "disclaimer_goblin"]]

    prepared = [row for row in state.ledger.rows if row.get("record") == "segment_prepared"]
    assert len(prepared) == 1
    final_script = prepared[0]["final_script"]
    # Tier-2 describes the complete spoken ad break, while last_ad_script/texts
    # above remains spot-only for the dashboard contract.
    assert final_script[0] == final_script[0].strip()
    assert final_script[1] == "A word from our sponsors, amici."
    assert "Great deals at Prezzoforte" not in final_script  # summary is not spoken copy
    assert final_script[-1] == final_script[-1].strip()
    assert len(final_script) == 4  # intro, promo, one spot, outro
    assert "language_assessment" in prepared[0]


# ---------------------------------------------------------------------------
# Resume bridge timing: stopped sessions stay quiet until the producer wakes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_producer_no_resume_bridge_after_session_resume(tmp_path):
    """While session_stopped=True, the producer sleeps for 1 s per loop iteration
    and queues nothing.  This test cancels the task well within that 1 s window
    (0.05 s stopped + 0.2 s resumed), so the _was_stopped → resume-bridge path
    never fires and the queue stays empty throughout.

    The resume bridge code itself still exists in the producer.  This test only
    verifies that nothing is queued during the stopped sleep window before the
    producer observes the resumed state.
    """
    state = _make_state()
    state.session_stopped = True
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    canned_clip = tmp_path / "canned.mp3"
    canned_clip.write_bytes(b"fake audio")

    with (
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=canned_clip),
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=tmp_path / "src.mp3"),
        patch(f"{PRODUCER_MODULE}.normalize"),
        patch(f"{PRODUCER_MODULE}.shutil.copy2"),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio"),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            # Wait for the producer to begin its stopped loop
            await asyncio.sleep(0.05)
            # Resume the session
            state.session_stopped = False
            # Give the producer a few iterations to run
            await asyncio.sleep(0.2)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # The task was cancelled before the stopped-state wait woke up, so the resume
    # bridge should not have had a chance to seed anything.
    segments_with_resume_bridge = []
    while not queue.empty():
        seg = queue.get_nowait()
        if seg.metadata.get("resume_bridge") is True:
            segments_with_resume_bridge.append(seg)

    assert not segments_with_resume_bridge, (
        f"Found {len(segments_with_resume_bridge)} segment(s) with resume_bridge=True; "
        "the resume bridge should not fire before the stopped-state wait wakes."
    )


@pytest.mark.asyncio
async def test_producer_session_stopped_state_pauses_production(tmp_path):
    """While session_stopped=True, the producer loop sleeps and produces nothing.

    After a session is stopped, the producer must not add any segment to the queue
    (no bridge, no regular segment). The queue stays empty until session_stopped=False.
    """
    state = _make_state()
    state.session_stopped = True
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    with patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            # Let the producer run in stopped state for several loop iterations
            await asyncio.sleep(0.15)
            # Queue must be empty while stopped
            assert queue.empty(), (
                "Producer queued a segment while session_stopped=True; it must sleep without producing anything."
            )
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# Idle bridge timing: idle sessions stay quiet until the idle poll wakes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_bridge_does_not_run_before_idle_poll_wakes(tmp_path):
    """When no canned clips exist and the idle bridge runs, the producer still
    falls through to the norm-cache path (lines ~710-727) -- but this test
    cancels the task before the 1 s idle sleep completes, so the bridge path
    never fires and no idle_bridge / norm_cache segment appears in the queue.

    The norm_file created in tmp_path (norm_test.mp3) would be eligible if the
    idle bridge ran. The test verifies behaviour within the cancellation window
    only.
    """
    state = _make_state()
    state.listeners_active = 0  # start idle
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    # Create a norm file that the OLD code would have used as fallback
    norm_file = tmp_path / "norm_test.mp3"
    norm_file.write_bytes(b"pre-normalized audio")

    with (
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
        # Prevent the producer from generating real segments so we can observe
        # the idle bridge behaviour in isolation
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=tmp_path / "src.mp3"),
        patch(f"{PRODUCER_MODULE}.normalize"),
        patch(f"{PRODUCER_MODULE}.shutil.copy2"),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            # Let the producer enter idle state
            await asyncio.sleep(0.15)
            # Simulate a listener connecting
            state.listeners_active = 1
            # Give the producer time to process the idle→active transition
            await asyncio.sleep(0.15)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # The idle bridge must NOT have seeded the norm_cache file.
    # Any segment that ends up in the queue should NOT have idle_bridge or audio_source=norm_cache.
    norm_cache_segments = []
    while not queue.empty():
        seg = queue.get_nowait()
        if seg.metadata.get("idle_bridge") is True or seg.metadata.get("audio_source") == "norm_cache":
            norm_cache_segments.append(seg)

    assert not norm_cache_segments, (
        f"Found {len(norm_cache_segments)} norm_cache idle_bridge segment(s); "
        "the idle bridge should not fire before the idle-state wait wakes."
    )


@pytest.mark.asyncio
async def test_idle_bridge_queues_canned_clip_then_norm_cache_runway_when_available(tmp_path):
    """Idle wake-up gets a branded clip plus cached music runway."""
    state = _make_state()
    state.listeners_active = 0  # start idle
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    demo_root = tmp_path / "assets" / "demo"
    canned_clip = demo_root / "recovery" / "canned.mp3"
    canned_clip.parent.mkdir(parents=True)
    canned_clip.write_bytes(b"fake audio" * 256)
    norm_file = tmp_path / "norm_idle_runway.mp3"
    norm_file.write_bytes(b"pre-normalized idle runway")
    save_track_metadata(norm_file, title="Idle Runway", artist="Runway Artist")

    with (
        patch(f"{PRODUCER_MODULE}._DEMO_ASSETS_DIR", demo_root),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=canned_clip),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=7.5),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            # Let the producer enter idle state
            await asyncio.sleep(0.15)
            # Simulate a listener connecting
            state.listeners_active = 1
            deadline = asyncio.get_event_loop().time() + 3.0
            while queue.qsize() < 2:
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("Idle bridge did not queue clip plus cached music")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    clip = queue.get_nowait()
    runway = queue.get_nowait()
    assert clip.type == SegmentType.BANTER
    assert clip.metadata.get("warmup") is True
    # #547: idle_bridge marks the warm-up clip as rescue audio so the fallback
    # classifier does not report it as the primary station; warmup stays for the
    # display contract.
    assert clip.metadata.get("idle_bridge") is True
    assert clip.path == canned_clip
    assert clip.ephemeral is False
    assert clip.duration_sec == 7.5
    assert clip.metadata["duration_ms"] == 7500
    assert runway.type == SegmentType.MUSIC
    assert runway.path == norm_file
    assert runway.metadata.get("idle_bridge") is True
    assert runway.metadata.get("audio_source") == "norm_cache"
    assert runway.metadata.get("title") == "Idle Runway"
    assert runway.metadata.get("artist") == "Runway Artist"
    from mammamiradio.scheduling import producer

    with patch(f"{PRODUCER_MODULE}._DEMO_ASSETS_DIR", demo_root):
        producer._unlink_if_tmp_render(clip, config.tmp_dir)
        assert canned_clip.exists()
    # The bridge itself is recorded once; the cached song is runway behind it.
    assert state.bridge_fires_total >= 1
    last = state.bridge_events[-1]
    assert (last["bridge_type"], last["source"]) == ("idle", "canned")


@pytest.mark.asyncio
async def test_continuity_bridge_canned_metadata_cannot_override_rescue_invariants(tmp_path):
    """Extra canned metadata may add display markers but cannot weaken bridge flags."""
    from mammamiradio.scheduling import producer

    state = _make_state()
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    canned_clip = tmp_path / "canned.mp3"
    canned_clip.write_bytes(b"fake audio" * 256)
    queued: list[Segment] = []

    async def _capture(segment: Segment) -> bool:
        queued.append(segment)
        return True

    with (
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=canned_clip),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=6.2),
    ):
        ok = await producer._queue_continuity_bridge(
            _capture,
            state,
            config,
            bridge_type="idle",
            bridge_flag="idle_bridge",
            canned_title="Station warm-up",
            canned_metadata={
                "warmup": True,
                "type": "music",
                "canned": False,
                "idle_bridge": False,
                "rescue": False,
                "title": "Override attempt",
                "duration_ms": 1,
            },
        )

    assert ok is True
    seg = queued[0]
    assert seg.metadata["type"] == "banter"
    assert seg.metadata["canned"] is True
    assert seg.metadata["idle_bridge"] is True
    assert seg.metadata["rescue"] is True
    assert seg.metadata["title"] == "Station warm-up"
    assert seg.metadata["warmup"] is True
    assert seg.ephemeral is False
    assert seg.duration_sec == 6.2
    assert seg.metadata["duration_ms"] == 6200
    assert state.bridge_events[-1]["bridge_type"] == "idle"


@pytest.mark.asyncio
async def test_drain_bridge_queues_cache_music_runway_when_warm(tmp_path):
    """A drain clip is immediately followed by cache music, never another clip."""
    from mammamiradio.scheduling import producer

    state = _make_state()
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    canned_clip = tmp_path / "canned.mp3"
    canned_clip.write_bytes(b"fake audio" * 256)
    norm_file = tmp_path / "norm_drain_runway.mp3"
    norm_file.write_bytes(b"pre-normalized drain runway")
    save_track_metadata(norm_file, title="Runway Song", artist="Cache Artist", duration_ms=180_000)
    queued: list[Segment] = []

    async def _capture(segment: Segment) -> bool:
        queued.append(segment)
        return True

    with (
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=canned_clip),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=6.2),
    ):
        ok = await producer._queue_drain_recovery_bridge(_capture, state, config)

    assert ok is True
    assert [seg.path for seg in queued] == [canned_clip, norm_file]
    assert [seg.type for seg in queued] == [SegmentType.BANTER, SegmentType.MUSIC]
    assert queued[0].metadata.get("queue_drain_recovery") is True
    assert queued[1].metadata.get("queue_drain_recovery") is True
    assert queued[1].metadata.get("audio_source") == "norm_cache"
    assert queued[1].metadata.get("title") == "Runway Song"
    assert queued[1].metadata.get("artist") == "Cache Artist"
    assert [(event["bridge_type"], event["source"]) for event in state.bridge_events] == [("drain", "canned")]


@pytest.mark.asyncio
async def test_drain_bridge_queues_only_canned_clip_when_cache_is_cold(tmp_path):
    """A cold cache preserves the single-clip drain bridge fallback."""
    from mammamiradio.scheduling import producer

    state = _make_state()
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    canned_clip = tmp_path / "canned.mp3"
    canned_clip.write_bytes(b"fake audio" * 256)
    queued: list[Segment] = []

    async def _capture(segment: Segment) -> bool:
        queued.append(segment)
        return True

    with (
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=canned_clip),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=6.2),
        patch(f"{PRODUCER_MODULE}.select_norm_cache_rescue", return_value=None),
    ):
        ok = await producer._queue_drain_recovery_bridge(_capture, state, config)

    assert ok is True
    assert [seg.path for seg in queued] == [canned_clip]
    assert queued[0].metadata.get("queue_drain_recovery") is True
    assert [(event["bridge_type"], event["source"]) for event in state.bridge_events] == [("drain", "canned")]


@pytest.mark.asyncio
async def test_resume_bridge_music_runway_queues_only_canned_clip_when_cache_cold(tmp_path):
    """music_runway=True with a cold norm cache still queues just the canned clip."""
    from mammamiradio.scheduling import producer

    state = _make_state()
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    canned_clip = tmp_path / "canned.mp3"
    canned_clip.write_bytes(b"fake audio" * 256)
    queued: list[Segment] = []

    async def _capture(segment: Segment) -> bool:
        queued.append(segment)
        return True

    with (
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=canned_clip),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=8.0),
        patch(f"{PRODUCER_MODULE}.select_norm_cache_rescue", return_value=None),
    ):
        ok = await producer._queue_continuity_bridge(
            _capture,
            state,
            config,
            bridge_type="resume",
            bridge_flag="resume_bridge",
            canned_title="Resume bridge",
            music_runway=True,
        )

    assert ok is True
    assert [seg.path for seg in queued] == [canned_clip]
    assert state.bridge_events[-1]["bridge_type"] == "resume"
    assert state.bridge_events[-1]["source"] == "canned"


@pytest.mark.asyncio
async def test_idle_bridge_music_runway_queues_only_canned_clip_when_cache_cold(tmp_path):
    """music_runway=True with a cold norm cache still queues just the canned clip."""
    from mammamiradio.scheduling import producer

    state = _make_state()
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    canned_clip = tmp_path / "canned.mp3"
    canned_clip.write_bytes(b"fake audio" * 256)
    queued: list[Segment] = []

    async def _capture(segment: Segment) -> bool:
        queued.append(segment)
        return True

    with (
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=canned_clip),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=8.0),
        patch(f"{PRODUCER_MODULE}.select_norm_cache_rescue", return_value=None),
    ):
        ok = await producer._queue_continuity_bridge(
            _capture,
            state,
            config,
            bridge_type="idle",
            bridge_flag="idle_bridge",
            canned_title="Station warm-up",
            canned_metadata={"warmup": True, "rescue": True},
            music_runway=True,
        )

    assert ok is True
    assert [seg.path for seg in queued] == [canned_clip]
    assert state.bridge_events[-1]["bridge_type"] == "idle"
    assert state.bridge_events[-1]["source"] == "canned"


@pytest.mark.asyncio
async def test_continuity_bridge_logs_when_runway_segment_fails_to_enqueue(tmp_path, caplog):
    """A rejected runway enqueue (e.g. a full queue) is logged, not silently dropped.

    The canned clip's own success is still the only thing that counts as a bridge
    fire — the runway segment is a bonus behind it, so a failed runway enqueue must
    not raise, must not double-fire telemetry, and must not fail the bridge.
    """
    from mammamiradio.scheduling import producer

    state = _make_state()
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    canned_clip = tmp_path / "canned.mp3"
    canned_clip.write_bytes(b"fake audio" * 256)
    norm_file = tmp_path / "norm_resume_runway.mp3"
    norm_file.write_bytes(b"pre-normalized resume runway")
    queued: list[Segment] = []

    async def _reject_second(segment: Segment) -> bool:
        if queued:
            return False
        queued.append(segment)
        return True

    caplog.set_level(logging.INFO, logger="mammamiradio.scheduling.producer")
    with (
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=canned_clip),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=8.0),
        patch(f"{PRODUCER_MODULE}.select_norm_cache_rescue", return_value=norm_file),
    ):
        ok = await producer._queue_continuity_bridge(
            _reject_second,
            state,
            config,
            bridge_type="resume",
            bridge_flag="resume_bridge",
            canned_title="Resume bridge",
            music_runway=True,
        )

    assert ok is True
    assert [seg.path for seg in queued] == [canned_clip]
    assert state.bridge_fires_total == 1
    assert state.bridge_events[-1]["bridge_type"] == "resume"
    assert state.bridge_events[-1]["source"] == "canned"
    assert any("no runway music segment queued behind the canned clip" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# P0: hot-reload import refactor invariants
# ---------------------------------------------------------------------------


def test_producer_imports_scriptwriter_as_module() -> None:
    """producer.py must import scriptwriter as a module object, not via name binding.

    This is the P0 invariant for hot-reload: importlib.reload(mammamiradio.scriptwriter)
    only takes effect if producer.py accesses functions via the module reference (_sw.*).
    Name-bound imports (from ... import fn) hold stale references after reload.
    """
    import mammamiradio.hosts.scriptwriter as _sw_mod
    import mammamiradio.scheduling.producer as _prod_mod

    assert hasattr(_prod_mod, "_sw"), (
        "producer.py must expose '_sw' (import mammamiradio.hosts.scriptwriter as _sw). "
        "Name-bound imports break hot-reload."
    )
    assert _prod_mod._sw is _sw_mod, "producer._sw must be the same object as mammamiradio.scriptwriter module."


def test_write_banter_resolves_via_module_after_reload() -> None:
    """After importlib.reload, _sw.write_banter must resolve to the new function body.

    This verifies that the module-reference import pattern makes hot-reload effective.
    """
    import importlib

    import mammamiradio.hosts.scriptwriter as _sw_mod
    import mammamiradio.scheduling.producer as _prod_mod

    original_fn = _sw_mod.write_banter

    # Reload the module
    importlib.reload(_sw_mod)

    # producer._sw is the same module object — its attributes now point to new functions
    assert _prod_mod._sw.write_banter is _sw_mod.write_banter, (
        "_sw.write_banter should resolve to the reloaded function via the module reference."
    )

    # Cleanup: restore original (module is now reloaded but functions are equivalent)
    _ = original_fn  # referenced to avoid unused-variable lint


# ---------------------------------------------------------------------------
# Resume bridge — covers producer.py lines 599-637 deleted by CodeRabbit #182
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_bridge_queues_canned_clip_then_norm_cache_runway_when_available(tmp_path):
    """Resume gets a branded clip plus cached music runway."""
    state = _make_state()
    state.session_stopped = True
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    canned_clip = tmp_path / "canned.mp3"
    canned_clip.write_bytes(b"fake audio" * 256)
    norm_file = tmp_path / "norm_resume_runway.mp3"
    norm_file.write_bytes(b"pre-normalized resume runway")
    save_track_metadata(norm_file, title="Resume Runway", artist="Runway Artist")

    with (
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=canned_clip),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=8.0),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.05)
            state.session_stopped = False
            deadline = asyncio.get_event_loop().time() + 3.0
            while queue.qsize() < 2:
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("Resume bridge did not queue clip plus cached music")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    clip = queue.get_nowait()
    runway = queue.get_nowait()
    assert clip.type == SegmentType.BANTER
    assert clip.metadata.get("resume_bridge") is True
    assert clip.duration_sec == 8.0
    assert clip.metadata["duration_ms"] == 8000
    assert runway.type == SegmentType.MUSIC
    assert runway.path == norm_file
    assert runway.metadata.get("resume_bridge") is True
    assert runway.metadata.get("audio_source") == "norm_cache"
    assert runway.metadata.get("title") == "Resume Runway"
    assert runway.metadata.get("artist") == "Runway Artist"
    assert [row["id"] for row in state.queued_segments] == [clip.metadata["queue_id"], runway.metadata["queue_id"]]
    assert [row["label"] for row in state.queued_segments] == ["Resume bridge", "Resume Runway"]
    # #547: the bridge itself is recorded once for observability.
    assert state.bridge_fires_total >= 1
    last = state.bridge_events[-1]
    assert (last["bridge_type"], last["source"]) == ("resume", "canned")


@pytest.mark.asyncio
async def test_resume_bridge_falls_back_to_norm_cache_when_no_canned_clips(tmp_path):
    """When no canned clips exist, the bridge seeds the first pre-normalized track
    from cache_dir so the queue isn't empty after resume."""
    state = _make_state()
    state.session_stopped = True
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    norm_file = tmp_path / "norm_abc123.mp3"
    norm_file.write_bytes(b"pre-normalized audio")

    with patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.05)
            state.session_stopped = False
            deadline = asyncio.get_event_loop().time() + 3.0
            while queue.empty():
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("Norm-cache resume bridge did not queue a segment")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC
    assert seg.metadata.get("resume_bridge") is True
    assert seg.metadata.get("audio_source") == "norm_cache"
    assert seg.path == norm_file
    assert seg.duration_sec > 0
    assert seg.metadata.get("duration_ms") == round(seg.duration_sec * 1000)
    assert seg.metadata.get("title") == "Abc123"
    assert seg.metadata.get("artist") == ""
    # #547: the norm-cache resume bridge fire is recorded.
    assert state.bridge_fires_total >= 1
    last = state.bridge_events[-1]
    assert (last["bridge_type"], last["source"]) == ("resume", "norm_cache")


@pytest.mark.asyncio
async def test_resume_bridge_uses_norm_sidecar_metadata_when_available(tmp_path):
    """Resume bridge should restore title and artist from the norm-cache sidecar."""
    state = _make_state()
    state.session_stopped = True
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    norm_file = tmp_path / "norm_rescue_track_192k.mp3"
    norm_file.write_bytes(b"pre-normalized audio")
    save_track_metadata(norm_file, title="Sogno Americano", artist="Artie 5ive")

    with patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.05)
            state.session_stopped = False
            deadline = asyncio.get_event_loop().time() + 3.0
            while queue.empty():
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("Norm-cache resume bridge did not queue a segment")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    seg = queue.get_nowait()
    assert seg.metadata.get("resume_bridge") is True
    assert seg.duration_sec > 0
    assert seg.metadata.get("duration_ms") == round(seg.duration_sec * 1000)
    assert seg.metadata.get("title") == "Sogno Americano"
    assert seg.metadata.get("artist") == "Artie 5ive"


@pytest.mark.asyncio
async def test_resume_bridge_uses_emergency_tone_when_no_canned_clips_and_empty_norm_cache(tmp_path):
    """When neither canned clips nor pre-normalized files exist, the bridge is a
    generated rescue tone so the resumed session does not wait on real content."""
    state = _make_state()
    state.session_stopped = True
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    with patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.05)
            state.session_stopped = False
            deadline = asyncio.get_event_loop().time() + 3.0
            while queue.empty():
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("Resume emergency bridge did not queue a segment")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC
    assert seg.metadata.get("resume_bridge") is True
    assert seg.metadata.get("rescue") is True
    assert seg.metadata.get("audio_source") == "emergency_tone"
    assert seg.ephemeral is False
    assert seg.path.name == "emergency_tone.mp3"
    assert seg.duration_sec == 2.0
    assert seg.metadata["duration_ms"] == 2000
    assert state.bridge_fires_total >= 1
    last = state.bridge_events[-1]
    assert (last["bridge_type"], last["source"]) == ("resume", "emergency_tone")


@pytest.mark.asyncio
async def test_resume_bridge_never_airs_a_banned_norm_cache_song_after_restart(tmp_path):
    """Audio-delivery Scenario 3 (post-restart): a banned song must not re-air even
    through the norm-cache resume bridge. The only cached file on disk is banned, so
    the rescue selector returns None and the bridge falls through to emergency tone
    instead. (The selector itself is unit-tested both ways in tests/audio/test_norm_cache.py;
    this proves the blocklist filter composes with the producer's bridge guard end to end.)"""
    state = _make_state()
    state.session_stopped = True
    state.blocklist = {("alex warren", "ordinary"): {"display": "Alex Warren - Ordinary"}}
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    banned_file = tmp_path / "norm_banned.mp3"
    banned_file.write_bytes(b"pre-normalized banned audio")
    save_track_metadata(banned_file, title="Ordinary", artist="Alex Warren")

    def _fake_tone(path: Path, *_args, **_kwargs):
        path.write_bytes(b"tone")
        return path

    with (
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
        patch(f"{PRODUCER_MODULE}.generate_tone", side_effect=_fake_tone),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.05)
            state.session_stopped = False
            deadline = asyncio.get_event_loop().time() + 3.0
            while queue.empty():
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("Resume emergency bridge did not queue a segment")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # The banned norm file must never have been queued, and no resume bridge may
    # have fired off the norm cache (the only file there was banned).
    queued = []
    while not queue.empty():
        queued.append(queue.get_nowait())
    assert all(seg.path != banned_file for seg in queued)
    assert any(seg.metadata.get("audio_source") == "emergency_tone" for seg in queued)
    assert any(seg.duration_sec == 2.0 for seg in queued)
    assert all(
        not (ev.get("bridge_type") == "resume" and ev.get("source") == "norm_cache") for ev in state.bridge_events
    )
    assert any(ev.get("bridge_type") == "resume" and ev.get("source") == "emergency_tone" for ev in state.bridge_events)


@pytest.mark.asyncio
async def test_idle_bridge_falls_back_to_norm_cache_when_no_canned_clips(tmp_path):
    """When a listener reconnects after idle and no canned clips exist, the idle
    bridge seeds a recent-aware pre-normalized track from cache_dir."""
    state = _make_state()
    state.listeners_active = 0
    state.stream_log.append(
        SegmentLogEntry(
            type=SegmentType.MUSIC.value,
            label="Alex Warren - Ordinary",
            metadata={"title": "Ordinary", "artist": "Alex Warren"},
        )
    )
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    recent_norm_file = tmp_path / "norm_aaa_ordinary.mp3"
    recent_norm_file.write_bytes(b"pre-normalized current idle audio")
    save_track_metadata(recent_norm_file, title="Ordinary", artist="Alex Warren")
    norm_file = tmp_path / "norm_idle123.mp3"
    norm_file.write_bytes(b"pre-normalized idle audio")

    with (
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
        patch("mammamiradio.audio.norm_cache.random.choice", side_effect=lambda items: items[0]),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.15)
            state.listeners_active = 1
            deadline = asyncio.get_event_loop().time() + 3.0
            while queue.empty():
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("Idle norm-cache bridge did not queue a segment")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC
    assert seg.metadata.get("idle_bridge") is True
    assert seg.metadata.get("audio_source") == "norm_cache"
    assert seg.path == norm_file
    assert seg.duration_sec > 0
    assert seg.metadata.get("duration_ms") == round(seg.duration_sec * 1000)
    assert seg.metadata.get("title") == "Idle123"
    assert seg.metadata.get("artist") == ""
    # #547: the norm-cache idle bridge fire is recorded.
    assert state.bridge_fires_total >= 1
    last = state.bridge_events[-1]
    assert (last["bridge_type"], last["source"]) == ("idle", "norm_cache")


@pytest.mark.asyncio
async def test_idle_bridge_uses_norm_sidecar_metadata_when_available(tmp_path):
    """Idle bridge should restore title and artist from the norm-cache sidecar."""
    state = _make_state()
    state.listeners_active = 0
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    norm_file = tmp_path / "norm_idle_rescue_track_192k.mp3"
    norm_file.write_bytes(b"pre-normalized idle audio")
    save_track_metadata(norm_file, title="Musica Leggera", artist="Colapesce Dimartino")

    with patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.15)
            state.listeners_active = 1
            deadline = asyncio.get_event_loop().time() + 3.0
            while queue.empty():
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("Idle norm-cache bridge did not queue a segment")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    seg = queue.get_nowait()
    assert seg.metadata.get("idle_bridge") is True
    assert seg.duration_sec > 0
    assert seg.metadata.get("duration_ms") == round(seg.duration_sec * 1000)
    assert seg.metadata.get("title") == "Musica Leggera"
    assert seg.metadata.get("artist") == "Colapesce Dimartino"


@pytest.mark.asyncio
async def test_idle_bridge_uses_emergency_tone_when_no_canned_clips_and_empty_norm_cache(tmp_path):
    """When a listener reconnects after idle and no instant audio source exists,
    the idle bridge queues an emergency tone instead of waiting on production."""
    state = _make_state()
    state.listeners_active = 0
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    with patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.15)
            state.listeners_active = 1
            deadline = asyncio.get_event_loop().time() + 3.0
            while queue.empty():
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("Idle emergency bridge did not queue a segment")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC
    assert seg.metadata.get("idle_bridge") is True
    assert seg.metadata.get("rescue") is True
    assert seg.metadata.get("audio_source") == "emergency_tone"
    assert seg.path.name == "emergency_tone.mp3"
    assert seg.ephemeral is False
    assert seg.duration_sec == 2.0
    assert seg.metadata["duration_ms"] == 2000
    assert state.bridge_fires_total >= 1
    last = state.bridge_events[-1]
    assert (last["bridge_type"], last["source"]) == ("idle", "emergency_tone")


@pytest.mark.asyncio
async def test_resume_bridge_skipped_when_queue_already_has_items(tmp_path):
    """The resume bridge must NOT queue an additional segment when the queue is
    already non-empty. Seeding into a non-empty queue would cause duplicate audio."""
    state = _make_state()
    state.session_stopped = True
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    canned_clip = tmp_path / "canned.mp3"
    canned_clip.write_bytes(b"fake audio")

    pre_existing = Segment(
        type=SegmentType.BANTER,
        path=canned_clip,
        metadata={"type": "banter", "pre_existing": True},
    )
    queue.put_nowait(pre_existing)

    with patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=canned_clip):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.05)
            state.session_stopped = False
            # Wait long enough for the producer's 1s sleep to complete and check
            # queue.empty() == False (bridge skipped path).
            await asyncio.sleep(1.5)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    assert queue.qsize() == 1
    seg = queue.get_nowait()
    assert seg.metadata.get("pre_existing") is True


@pytest.mark.asyncio
async def test_resume_bridge_skips_first_sorted_norm_file_when_current_or_recent(tmp_path):
    """When multiple pre-normalized files exist, resume avoids the current/recent
    song instead of blindly seeding the first sorted cache file."""
    state = _make_state()
    state.session_stopped = True
    state.now_streaming = {
        "type": "music",
        "label": "Alex Warren - Ordinary",
        "metadata": {"title": "Ordinary", "artist": "Alex Warren"},
    }
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    norm_aaa = tmp_path / "norm_aaa_ordinary.mp3"
    norm_aaa.write_bytes(b"first sorted current file")
    save_track_metadata(norm_aaa, title="Ordinary", artist="Alex Warren")
    norm_mmm = tmp_path / "norm_mmm_alt.mp3"
    norm_mmm.write_bytes(b"middle alternative file")
    save_track_metadata(norm_mmm, title="Musica Leggera", artist="Colapesce Dimartino")
    norm_zzz = tmp_path / "norm_zzz_alt.mp3"
    norm_zzz.write_bytes(b"last alternative file")
    save_track_metadata(norm_zzz, title="A far l amore", artist="Raffaella Carra")

    with (
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
        patch("mammamiradio.audio.norm_cache.random.choice", side_effect=lambda items: items[0]),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.05)
            state.session_stopped = False
            deadline = asyncio.get_event_loop().time() + 3.0
            while queue.empty():
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("Resume bridge did not queue a segment")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    seg = queue.get_nowait()
    assert seg.path == norm_mmm
    assert seg.metadata.get("resume_bridge") is True
    assert seg.metadata.get("title") == "Musica Leggera"
    assert seg.metadata.get("artist") == "Colapesce Dimartino"


@pytest.mark.asyncio
async def test_was_stopped_initialized_true_when_session_already_stopped(tmp_path):
    """_was_stopped is initialised from state.session_stopped at producer startup.

    If the producer starts with session_stopped=True (e.g. after an HA watchdog
    restart where the flag file was re-read), _was_stopped must already be True so
    that the resume bridge fires immediately on the first transition to not-stopped."""
    state = _make_state()
    state.session_stopped = True
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    norm_file = tmp_path / "norm_startup.mp3"
    norm_file.write_bytes(b"startup track")

    with patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.05)
            state.session_stopped = False
            deadline = asyncio.get_event_loop().time() + 3.0
            while queue.empty():
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError(
                        "_was_stopped not initialised from session_stopped — bridge did not fire on first resume"
                    )
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    seg = queue.get_nowait()
    assert seg.metadata.get("resume_bridge") is True
    assert seg.path == norm_file


class TestBanterTitle:
    """Item #8: BANTER segments must render a meaningful label in queue rows
    rather than the bare segment-type name (e.g., "banter")."""

    def test_single_host_from_script(self):
        from mammamiradio.scheduling.producer import _banter_title

        script = [{"host": "Marco", "text": "ciao"}]
        assert _banter_title(script, canned=False) == "Marco"

    def test_two_unique_hosts_joined(self):
        from mammamiradio.scheduling.producer import _banter_title

        script = [
            {"host": "Marco", "text": "a"},
            {"host": "Luca", "text": "b"},
            {"host": "Marco", "text": "c"},
        ]
        assert _banter_title(script, canned=False) == "Marco & Luca"

    def test_three_hosts_caps_at_two(self):
        from mammamiradio.scheduling.producer import _banter_title

        script = [
            {"host": "A", "text": "1"},
            {"host": "B", "text": "2"},
            {"host": "C", "text": "3"},
        ]
        assert _banter_title(script, canned=False) == "A & B"

    def test_canned_fallback_when_script_empty(self):
        from mammamiradio.scheduling.producer import _banter_title

        assert _banter_title([], canned=True) == "Pre-recorded banter"
        assert _banter_title(None, canned=True) == "Pre-recorded banter"

    def test_canned_wins_over_sentinel_host(self):
        """When the quality-gate rescue path swaps in a canned clip it also
        overwrites `state.last_banter_script` with a synthetic `Radio` host.
        Queue label must reflect the canned nature, not the sentinel host."""
        from mammamiradio.scheduling.producer import _banter_title

        sentinel = [{"host": "Radio", "text": "(pre-recorded banter)"}]
        assert _banter_title(sentinel, canned=True) == "Pre-recorded banter"

    def test_generic_fallback_when_no_signal(self):
        from mammamiradio.scheduling.producer import _banter_title

        assert _banter_title(None, canned=False) == "Banter"
        assert _banter_title([{"text": "no host"}], canned=False) == "Banter"

    def test_host_order_sorts_to_config_order(self):
        """LLM opened with Marco but config lists Giulia first — display shows Giulia & Marco."""
        from mammamiradio.scheduling.producer import _banter_title

        script = [{"host": "Marco", "text": "Ciao"}, {"host": "Giulia", "text": "Benvenuti"}]
        assert _banter_title(script, canned=False, host_order=["Giulia", "Marco"]) == "Giulia & Marco"

    def test_host_not_in_order_sorts_to_end(self):
        """A host absent from host_order still appears, sorted after known hosts."""
        from mammamiradio.scheduling.producer import _banter_title

        script = [{"host": "Giulia", "text": "Ciao"}, {"host": "Unknown", "text": "Hey"}]
        result = _banter_title(script, canned=False, host_order=["Giulia", "Marco"])
        assert result == "Giulia & Unknown"

    def test_host_order_empty_list_falls_back_to_script_order(self):
        """Empty host_order is falsy — sorting is skipped, script order is preserved."""
        from mammamiradio.scheduling.producer import _banter_title

        script = [{"host": "Marco", "text": "Ciao"}, {"host": "Giulia", "text": "Benvenuti"}]
        assert _banter_title(script, canned=False, host_order=[]) == "Marco & Giulia"

    def test_host_order_caps_at_two_with_three_hosts(self):
        """Cap-at-2 persists even with host_order; first two in config order are shown."""
        from mammamiradio.scheduling.producer import _banter_title

        script = [
            {"host": "Marco", "text": "a"},
            {"host": "Giulia", "text": "b"},
            {"host": "Luca", "text": "c"},
        ]
        result = _banter_title(script, canned=False, host_order=["Giulia", "Luca", "Marco"])
        assert result == "Giulia & Luca"


class TestAdTitle:
    """Item #8: AD break segments must render a brand-aware label rather than
    the bare segment-type name (e.g., "ad")."""

    def test_single_brand(self):
        from mammamiradio.scheduling.producer import _ad_title

        assert _ad_title(["Barella Pasta"]) == "Ad: Barella Pasta"

    def test_two_brands_summarized(self):
        from mammamiradio.scheduling.producer import _ad_title

        assert _ad_title(["Brand A", "Brand B"]) == "Ad: Brand A +1 more"

    def test_four_brands_summarized(self):
        from mammamiradio.scheduling.producer import _ad_title

        assert _ad_title(["A", "B", "C", "D"]) == "Ad: A +3 more"

    def test_empty_brands_falls_back(self):
        from mammamiradio.scheduling.producer import _ad_title

        assert _ad_title([]) == "Ad break"
        assert _ad_title(None) == "Ad break"

    def test_whitespace_only_brand_skipped(self):
        from mammamiradio.scheduling.producer import _ad_title

        assert _ad_title(["   ", "Real Brand"]) == "Ad: Real Brand"


@pytest.mark.asyncio
async def test_enqueue_funnel_drops_a_banned_music_segment(tmp_path):
    """Final blocklist gate: a banned MUSIC segment must never reach the queue, even
    if a selection-path race (ban mid-render / zombie pin / purge-missed rescue) got
    it to the funnel. Music with a banned (artist, title) is dropped (returns False,
    queue stays empty, ephemeral render unlinked); a non-banned song still enqueues."""
    from mammamiradio.scheduling.producer import _enqueue_with_egress

    state = _make_state()
    state.blocklist = {("artista", "canzone uno"): {"display": "Artista - Canzone Uno"}}
    config = _make_config()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    banned_path = tmp_path / "norm_banned.mp3"
    banned_path.write_bytes(b"x")
    banned_seg = Segment(
        type=SegmentType.MUSIC,
        path=banned_path,
        ephemeral=True,
        metadata={"artist": "Artista", "title_only": "Canzone Uno"},
    )
    # Banned: dropped before egress, queue empty, the ephemeral render cleaned up.
    assert await _enqueue_with_egress(queue, state, config, banned_seg) is False
    assert queue.empty()
    assert not banned_path.exists()
    assert state.last_enqueued_type is None
    assert state.last_music_file is None

    # Control: a non-banned song passes the gate and is queued (egress stubbed out).
    ok_path = tmp_path / "norm_ok.mp3"
    ok_path.write_bytes(b"x")  # a real queued song exists on disk (bed source must persist)
    ok_seg = Segment(
        type=SegmentType.MUSIC,
        path=ok_path,
        ephemeral=False,
        metadata={"artist": "Artista", "title_only": "Canzone Due"},
    )
    with patch(f"{PRODUCER_MODULE}._apply_egress", new_callable=AsyncMock, return_value=ok_seg):
        assert await _enqueue_with_egress(queue, state, config, ok_seg) is True
    assert queue.qsize() == 1
    # The non-banned song reaches the queue and flips the timeline-tail type. (last_music_file
    # for normally-rendered music is owned by _remember_rendered_music, not the funnel.)
    assert state.last_enqueued_type == SegmentType.MUSIC


@pytest.mark.asyncio
async def test_enqueue_funnel_blocklist_keeps_packaged_asset_even_if_ephemeral(tmp_path):
    """The blocklist discard path must not delete packaged demo assets."""
    from mammamiradio.scheduling import producer
    from mammamiradio.scheduling.producer import _enqueue_with_egress

    demo_root = tmp_path / "assets" / "demo"
    packaged = demo_root / "recovery" / "continuity_1.mp3"
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(b"\x00" * 2048)
    state = _make_state()
    state.blocklist = {("artista", "canzone uno"): {"display": "Artista - Canzone Uno"}}
    config = _make_config()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    segment = Segment(
        type=SegmentType.MUSIC,
        path=packaged,
        ephemeral=True,
        metadata={"artist": "Artista", "title_only": "Canzone Uno"},
    )

    with patch.object(producer, "_DEMO_ASSETS_DIR", demo_root):
        assert await _enqueue_with_egress(queue, state, config, segment) is False

    assert packaged.exists()


@pytest.mark.asyncio
async def test_enqueue_funnel_post_egress_stale_keeps_packaged_asset_even_if_ephemeral(tmp_path):
    """The post-egress stale discard path must not delete packaged demo assets."""
    from mammamiradio.scheduling import producer
    from mammamiradio.scheduling.producer import _enqueue_with_egress

    demo_root = tmp_path / "assets" / "demo"
    packaged = demo_root / "recovery" / "continuity_1.mp3"
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(b"\x00" * 2048)
    state = _make_state()
    config = _make_config()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    segment = Segment(type=SegmentType.BANTER, path=packaged, ephemeral=True, metadata={"type": "banter"})

    with (
        patch.object(producer, "_DEMO_ASSETS_DIR", demo_root),
        patch(f"{PRODUCER_MODULE}._apply_egress", new_callable=AsyncMock, return_value=segment),
    ):
        assert await _enqueue_with_egress(queue, state, config, segment, stale_check=lambda: True) is False

    assert packaged.exists()


async def test_fire_interrupt_clears_music_adjacency(tmp_path):
    """An urgent interrupt purges the buffered tail and cuts the current segment — a hard
    continuity break. The urgent banter that follows must not bed a purged/cut song, so
    music adjacency is cleared (same stale-bleed class as the front-insert tail drop, #641)."""
    from mammamiradio.core.models import InterruptSpec
    from mammamiradio.scheduling.producer import _adjacent_music_source, _fire_interrupt

    song = tmp_path / "buffered_song.mp3"
    song.write_bytes(b"MUSIC")  # cache-backed: survives the purge on disk
    state = _make_state()
    state.last_music_file = song
    state.last_enqueued_type = SegmentType.MUSIC
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    queue.put_nowait(Segment(type=SegmentType.MUSIC, path=song, metadata={"title": "Buffered"}, ephemeral=False))
    state.queued_segments = [{"id": "buffered", "type": "music"}]

    spec = InterruptSpec(directive="La pasta scotta!", urgency="pissed", cooldown=60)
    # Isolate the emergency-tone branch: with no alert.mp3 available, the bridge
    # must fall to the packaged emergency tone regardless of a real _SFX_DIR asset.
    empty_sfx = tmp_path / "empty_sfx"
    empty_sfx.mkdir()
    with patch(f"{PRODUCER_MODULE}._SFX_DIR", empty_sfx):
        assert await _fire_interrupt(state, spec, queue, None, bridge_tmp_dir=tmp_path) is True

    assert queue.empty()  # buffered tail purged
    assert state.last_enqueued_type is None
    # The song file still exists, but nothing bleeds under the urgent banter.
    assert _adjacent_music_source(state) is None
    # The interrupt bridge is immediately playable packaged audio, so it cannot
    # queue behind routine FFmpeg work.
    assert state.interrupt_slot is not None
    assert state.interrupt_slot.name == "emergency_tone.mp3"
    assert state.interrupt_slot_ephemeral is False


@pytest.mark.asyncio
async def test_fire_interrupt_abandons_all_queued_cues_when_one_unlink_fails(tmp_path):
    """Cleanup failure cannot strand later cue work or corrupt queue accounting."""
    from mammamiradio.scheduling import producer
    from mammamiradio.scheduling.producer import _fire_interrupt

    state, claim = _claimed_companionship_state()
    assert state.listener_session.mark_companionship_queued(claim.epoch) is True
    bad_path = tmp_path / "locked.mp3"
    bad_path.write_bytes(b"locked")
    cue_path = tmp_path / "cue.mp3"
    cue_path.write_bytes(b"cue")
    bad = Segment(type=SegmentType.MUSIC, path=bad_path, ephemeral=True, metadata={"queue_id": "bad"})
    cue = Segment(
        type=SegmentType.BANTER,
        path=cue_path,
        ephemeral=False,
        metadata={
            "queue_id": "cue",
            "listener_session_epoch": claim.epoch,
            "listener_session_cue": "companionship",
        },
    )
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    queue.put_nowait(bad)
    queue.put_nowait(cue)
    state.queued_segments = [{"id": "bad"}, {"id": "cue"}]

    demo_root = tmp_path / "demo"
    _manifest_recovery_clip(demo_root, "emergency_tone.mp3", b"tone", kind="tone")
    empty_sfx = tmp_path / "empty_sfx"
    empty_sfx.mkdir()
    original_unlink = Path.unlink

    def _unlink_with_one_failure(path: Path, *args, **kwargs):
        if path == bad_path:
            raise PermissionError("locked test render")
        return original_unlink(path, *args, **kwargs)

    spec = InterruptSpec(directive="Urgent update", urgency="urgent", cooldown=60)
    with (
        patch.object(producer, "_DEMO_ASSETS_DIR", demo_root),
        patch.object(producer, "_SFX_DIR", empty_sfx),
        patch.object(Path, "unlink", new=_unlink_with_one_failure),
    ):
        fired = await _fire_interrupt(state, spec, queue, None, bridge_tmp_dir=tmp_path)

    assert fired is True
    assert queue.empty()
    assert queue._unfinished_tasks == 0
    assert state.queued_segments == []
    assert state.listener_session.companionship_cue_state is ListenerSessionCueState.ABANDONED
    assert state.discard_by_reason[GenerationWasteReason.INTERRUPT] == 2
    assert bad_path.exists()


@pytest.mark.asyncio
async def test_fire_interrupt_keeps_packaged_asset_even_if_ephemeral(tmp_path):
    """Interrupt queue purges must not delete packaged demo assets."""
    from mammamiradio.core.models import InterruptSpec
    from mammamiradio.scheduling import producer
    from mammamiradio.scheduling.producer import _fire_interrupt

    demo_root = tmp_path / "assets" / "demo"
    packaged = _manifest_recovery_clip(demo_root, "continuity_1.mp3", b"\x00" * 2048)
    emergency_tone = _manifest_recovery_clip(
        demo_root,
        "emergency_tone.mp3",
        b"\x00" * 2048,
        kind="tone",
    )
    state = _make_state()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    queue.put_nowait(Segment(type=SegmentType.BANTER, path=packaged, metadata={}, ephemeral=True))
    state.queued_segments = [{"id": "asset", "type": "banter"}]
    spec = InterruptSpec(directive="La pasta scotta!", urgency="pissed", cooldown=60)

    empty_sfx = tmp_path / "empty_sfx"
    empty_sfx.mkdir()
    with (
        patch.object(producer, "_DEMO_ASSETS_DIR", demo_root),
        patch.object(producer, "_SFX_DIR", empty_sfx),
        patch("mammamiradio.scheduling.queue_mutations._DEMO_ASSETS_DIR", demo_root),
    ):
        assert await _fire_interrupt(state, spec, queue, None, bridge_tmp_dir=tmp_path) is True

    assert packaged.exists()
    assert queue.empty()
    assert state.interrupt_slot == emergency_tone


async def test_fire_interrupt_rejects_tampered_manifested_emergency_tone(tmp_path):
    """A modified packaged tone cannot justify draining the live queue."""
    from mammamiradio.core.models import InterruptSpec
    from mammamiradio.scheduling import producer
    from mammamiradio.scheduling.producer import _fire_interrupt

    demo_root = tmp_path / "assets" / "demo"
    emergency_tone = _manifest_recovery_clip(demo_root, "emergency_tone.mp3", b"reviewed", kind="tone")
    emergency_tone.write_bytes(b"tampered")
    empty_sfx = tmp_path / "empty_sfx"
    empty_sfx.mkdir()
    state = _make_state()
    buffered = Segment(type=SegmentType.MUSIC, path=tmp_path / "song.mp3", metadata={"title": "Buffered"})
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    queue.put_nowait(buffered)
    state.queued_segments = [{"id": "buffered", "type": "music"}]
    spec = InterruptSpec(directive="Urgent update", urgency="urgent", cooldown=60)

    with (
        patch.object(producer, "_DEMO_ASSETS_DIR", demo_root),
        patch.object(producer, "_SFX_DIR", empty_sfx),
    ):
        fired = await _fire_interrupt(state, spec, queue, None, bridge_tmp_dir=tmp_path)

    assert fired is False
    assert list(queue._queue) == [buffered]
    assert state.interrupt_slot is None


async def test_fire_interrupt_aborts_when_no_bridge_asset_available(tmp_path):
    """Both bridge assets missing must abort the interrupt, not cut to dead air.

    With neither alert.mp3 nor the packaged emergency tone available, hard-cutting
    would drain the queue and fire skip_event with nothing to air. The interrupt
    aborts instead, preserving whatever is already queued (INSTANT AUDIO).
    """
    from mammamiradio.core.models import InterruptSpec
    from mammamiradio.scheduling import producer
    from mammamiradio.scheduling.producer import _fire_interrupt

    empty_sfx = tmp_path / "empty_sfx"
    empty_sfx.mkdir()
    empty_demo = tmp_path / "empty_demo"
    empty_demo.mkdir()
    state = _make_state()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    buffered = Segment(
        type=SegmentType.MUSIC, path=tmp_path / "song.mp3", metadata={"title": "Buffered"}, ephemeral=False
    )
    queue.put_nowait(buffered)
    state.queued_segments = [{"id": "buffered", "type": "music"}]
    spec = InterruptSpec(directive="La pasta scotta!", urgency="pissed", cooldown=60)

    with (
        patch.object(producer, "_SFX_DIR", empty_sfx),
        patch.object(producer, "_DEMO_ASSETS_DIR", empty_demo),
    ):
        result = await _fire_interrupt(state, spec, queue, None, bridge_tmp_dir=tmp_path)

    assert result is False
    assert list(queue._queue) == [buffered]  # queue preserved — no dead-air cut
    assert state.interrupt_slot is None


def test_remember_rendered_music_populates_immediate_audio_index(tmp_path):
    """The live data source feeding the instant-audio continuity reservation must populate.

    Every continuity test injects ``immediate_audio_index`` directly, so a
    regression in the sole production writer for normally-rendered music would
    silently degrade the reservation to clip/emergency-tone only with nothing
    failing. Pin the writer, not just the consumer.
    """
    from mammamiradio.scheduling.producer import RenderedMusicTrack, _remember_rendered_music

    state = StationState(playlist=[])
    cache_path = tmp_path / "norm_rendered_128k.mp3"
    cache_path.write_bytes(b"cached")
    track = Track(title="Rendered Song", artist="Rendered Artist", duration_ms=200_000, spotify_id="r1")
    rendered = RenderedMusicTrack(track=track, path=cache_path, cache_path=cache_path, cache_hit=True)

    _remember_rendered_music(rendered, state)

    assert state.immediate_audio_index[cache_path] == pytest.approx(200.0)


def test_remember_enqueued_indexes_rescue_music_and_skips_breaks(tmp_path):
    """Rescue/recycled music tails populate the index; tones, errors, missing paths do not."""
    from mammamiradio.scheduling.producer import _remember_enqueued

    state = StationState(playlist=[])
    music_path = tmp_path / "norm_rescue_128k.mp3"
    music_path.write_bytes(b"cached")
    music = Segment(
        type=SegmentType.MUSIC,
        path=music_path,
        duration_sec=180.0,
        metadata={"rescue": True, "artist": "A", "title": "T"},
    )
    _remember_enqueued(state, music, music_path)
    assert state.immediate_audio_index[music_path] == pytest.approx(180.0)

    # An emergency-tone continuity break is not indexable music (audio_source guard).
    tone_path = tmp_path / "emergency_tone.mp3"
    tone_path.write_bytes(b"tone")
    tone = Segment(
        type=SegmentType.MUSIC,
        path=tone_path,
        duration_sec=2.0,
        metadata={"audio_source": "emergency_tone", "rescue": True},
    )
    _remember_enqueued(state, tone, tone_path)
    assert tone_path not in state.immediate_audio_index

    # A missing source path cannot be reserved later, so it is never indexed.
    missing = tmp_path / "norm_missing_128k.mp3"
    missing_music = Segment(type=SegmentType.MUSIC, path=missing, duration_sec=180.0, metadata={"rescue": True})
    _remember_enqueued(state, missing_music, missing)
    assert missing not in state.immediate_audio_index

    # A non-music tail never enters the music-only index.
    banter_path = tmp_path / "norm_banter_128k.mp3"
    banter_path.write_bytes(b"cached")
    banter = Segment(type=SegmentType.BANTER, path=banter_path, duration_sec=12.0, metadata={})
    _remember_enqueued(state, banter, banter_path)
    assert banter_path not in state.immediate_audio_index
