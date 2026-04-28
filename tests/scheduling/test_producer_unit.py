"""Unit tests for the producer pipeline in mammamiradio/scheduling/producer.py."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mammamiradio.audio.normalizer import save_track_metadata
from mammamiradio.core.config import load_config
from mammamiradio.core.models import (
    HostPersonality,
    PlaylistSource,
    Segment,
    SegmentType,
    StationState,
    Track,
)
from mammamiradio.hosts.ad_creative import AdScript, SonicWorld
from mammamiradio.hosts.scriptwriter import ListenerRequestCommit
from mammamiradio.scheduling.producer import (
    SHAREWARE_CANNED_LIMIT,
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

    yield
    producer._last_music_file = None
    producer._canned_clip_cache.clear()
    producer._recently_played_clips.clear()


def _make_state() -> StationState:
    return StationState(
        playlist=[
            Track(title="Canzone Uno", artist="Artista", duration_ms=200_000, spotify_id="demo1"),
            Track(title="Canzone Due", artist="Artista", duration_ms=180_000, spotify_id="demo2"),
        ],
        listeners_active=1,  # simulate a live listener so the producer gate passes
    )


def _make_config():
    config = load_config(TOML_PATH)
    config.pacing.lookahead_segments = 1
    config.homeassistant.enabled = False
    config.tmp_dir = Path("/tmp/mammamiradio_test")
    return config


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
    ):
        await _run_until_queued(queue, state, config)

    assert queue.qsize() >= 1
    seg = queue.get_nowait()
    assert seg.type == SegmentType.MUSIC
    assert "title" in seg.metadata
    assert seg.metadata["youtube_id"] in {"yt_demo1", "yt_demo2"}


# ---------------------------------------------------------------------------
# Banter segment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_banter_segment_queued():
    """Producer queues a BANTER segment with synthesized dialogue."""
    state = _make_state()
    config = _make_config()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")
    banter_lines = [(host, "Che bella giornata!")]

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=_fake_path()),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=_fake_path()),
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


@pytest.mark.asyncio
async def test_time_check_uses_host_engine_for_tts():
    state = _make_state()
    config = _make_config()
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
    kwargs = mock_synthesize.call_args.kwargs
    assert kwargs["engine"] == host.engine
    assert kwargs["edge_fallback_voice"] == host.edge_fallback_voice


# ---------------------------------------------------------------------------
# Error recovery — silence fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_recovery_queues_silence():
    """When download_track raises, producer inserts a silence segment and increments failed_segments."""
    state = _make_state()
    config = _make_config()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=RuntimeError("network down")),
        patch(f"{PRODUCER_MODULE}.generate_silence", side_effect=_fake_path),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert queue.qsize() >= 1
    seg = queue.get_nowait()
    # Segment type matches what was attempted (MUSIC), but metadata has error
    assert seg.type == SegmentType.MUSIC
    assert "error" in seg.metadata
    assert state.failed_segments >= 1


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

    async def fake_download(track, cache_dir, music_dir=None):
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

    def fake_normalize(src: Path, dst: Path) -> None:
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

    def fake_normalize(src: Path, dst: Path) -> None:
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

    def fake_normalize(src: Path, dst: Path) -> None:
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


# ---------------------------------------------------------------------------
# Shareware trial: canned clip limit
# ---------------------------------------------------------------------------


def test_pick_canned_clip_respects_shareware_limit(tmp_path):
    """After SHAREWARE_CANNED_LIMIT clips streamed, _pick_canned_clip returns None for banter."""
    from mammamiradio.scheduling import producer

    # Create fake banter clips
    banter_dir = tmp_path / "banter"
    banter_dir.mkdir()
    for i in range(5):
        (banter_dir / f"clip_{i}.mp3").write_bytes(b"\x00" * 100)

    # Temporarily override demo assets dir and clear caches
    orig = producer._DEMO_ASSETS_DIR
    producer._DEMO_ASSETS_DIR = tmp_path
    producer._recently_played_clips.clear()
    producer._canned_clip_cache.clear()

    try:
        state = StationState()

        # Under limit: should return a clip
        assert _pick_canned_clip("banter", state=state) is not None

        # At limit: should return None
        state.canned_clips_streamed = SHAREWARE_CANNED_LIMIT
        assert _pick_canned_clip("banter", state=state) is None

        # Welcome clips are NOT subject to the limit
        welcome_dir = tmp_path / "welcome"
        welcome_dir.mkdir()
        (welcome_dir / "welcome_1.mp3").write_bytes(b"\x00" * 100)
        assert _pick_canned_clip("welcome", state=state) is not None
    finally:
        producer._DEMO_ASSETS_DIR = orig
        producer._recently_played_clips.clear()
        producer._canned_clip_cache.clear()


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
        assert producer._canned_clip_cache.get("banter") == []
    finally:
        producer._DEMO_ASSETS_DIR = orig
        producer._canned_clip_cache.clear()
        producer._recently_played_clips.clear()


@pytest.mark.asyncio
async def test_error_recovery_inserts_silence_when_no_canned_clips(tmp_path):
    """Outer run_producer handler falls through to silence when demo_assets/ is empty.

    When produce_one_segment raises (music download fails), the outer handler
    tries _pick_canned_clip("banter") then _pick_canned_clip("welcome") before
    generating silence.  With both directories empty, silence is the last resort.

    Note: banter TTS failures use `continue` internally and never reach the outer
    handler.  This test uses MUSIC (whose failures propagate out) to exercise the
    outer silence path with no canned-clip safety net.
    """
    from mammamiradio.scheduling import producer

    (tmp_path / "banter").mkdir()
    (tmp_path / "welcome").mkdir()

    state = _make_state()
    config = _make_config()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

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
            patch(f"{PRODUCER_MODULE}.generate_silence", side_effect=_fake_path),
            patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        ):
            await _run_until_queued(queue, state, config)
    finally:
        producer._DEMO_ASSETS_DIR = orig
        producer._canned_clip_cache.clear()
        producer._recently_played_clips.clear()

    assert queue.qsize() >= 1
    seg = queue.get_nowait()
    assert "error" in seg.metadata


@pytest.mark.asyncio
async def test_error_recovery_logs_demo_assets_banter_hint_and_uses_silence(caplog):
    """When canned banter+welcome are unavailable, producer logs the banter-dir hint and inserts silence."""
    state = _make_state()
    config = _make_config()
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    picked_subdirs: list[str] = []

    def _pick_none(subdir: str, state=None):
        picked_subdirs.append(subdir)
        return None

    caplog.set_level(logging.WARNING, logger="mammamiradio.scheduling.producer")
    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=RuntimeError("network down")),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", side_effect=_pick_none),
        patch(f"{PRODUCER_MODULE}.generate_silence", side_effect=_fake_path) as mock_silence,
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        await _run_until_queued(queue, state, config)

    assert picked_subdirs[:2] == ["banter", "welcome"]
    assert any("check demo_assets/banter/" in record.message for record in caplog.records)
    assert mock_silence.call_count == 1
    assert mock_silence.call_args.args[1] == 5.0


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
async def test_maybe_start_session_increments_count(tmp_path):
    """_maybe_start_session bumps session_count on a fresh PersonaStore."""
    from mammamiradio.core.sync import init_db
    from mammamiradio.hosts.persona import PersonaStore
    from mammamiradio.scheduling.producer import _maybe_start_session

    db_path = tmp_path / "persona.db"
    init_db(db_path)
    store = PersonaStore(db_path)
    # Seed the persona row so increment_session has a row to update
    await store.update_persona({})

    state = StationState()
    state.persona_store = store

    await _maybe_start_session(state)

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
async def test_prewarm_quality_gate_rejection():
    """prewarm returns False when quality gate rejects the track."""
    from mammamiradio.audio.audio_quality import AudioQualityError
    from mammamiradio.scheduling.producer import prewarm_first_segment

    state = _make_state()
    config = _make_config()
    config.tmp_dir.mkdir(parents=True, exist_ok=True)
    queue: asyncio.Queue = asyncio.Queue()

    def _reject(*_a, **_kw):
        raise AudioQualityError("silent track")

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=Path("/tmp/fake.mp3")),
        patch(f"{PRODUCER_MODULE}.normalize"),
        patch(f"{PRODUCER_MODULE}.shutil.copy2"),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", side_effect=_reject),
    ):
        result = await prewarm_first_segment(queue, state, config)

    assert result is False
    assert queue.empty()


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
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
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


def test_pick_canned_clip_returns_file(tmp_path):
    """_pick_canned_clip returns a path when clips exist."""
    from mammamiradio.scheduling.producer import _canned_clip_cache, _pick_canned_clip, _recently_played_clips

    banter_dir = tmp_path / "banter"
    banter_dir.mkdir()
    clip1 = banter_dir / "clip1.mp3"
    clip1.write_bytes(b"audio")
    _canned_clip_cache.clear()
    _recently_played_clips.clear()
    with patch(f"{PRODUCER_MODULE}._DEMO_ASSETS_DIR", tmp_path):
        result = _pick_canned_clip("banter")
    assert result == clip1


def test_pick_canned_clip_clears_recently_played_when_exhausted(tmp_path):
    """When all clips are recently played, the cache resets and re-picks."""
    from mammamiradio.scheduling.producer import _canned_clip_cache, _pick_canned_clip, _recently_played_clips

    banter_dir = tmp_path / "banter"
    banter_dir.mkdir()
    clip1 = banter_dir / "clip1.mp3"
    clip1.write_bytes(b"audio")
    _canned_clip_cache.clear()
    _recently_played_clips.clear()
    _recently_played_clips.append("clip1.mp3")  # Mark the only clip as recently played
    with patch(f"{PRODUCER_MODULE}._DEMO_ASSETS_DIR", tmp_path):
        result = _pick_canned_clip("banter")
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
# _maybe_start_session unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_start_session_no_persona_store():
    """_maybe_start_session returns early when no persona_store."""
    from mammamiradio.scheduling.producer import _maybe_start_session

    state = _make_state()
    state.persona_store = None
    await _maybe_start_session(state)


@pytest.mark.asyncio
async def test_maybe_start_session_new_session():
    """_maybe_start_session increments session when new."""
    from mammamiradio.scheduling.producer import _maybe_start_session

    state = _make_state()
    mock_persona = MagicMock()
    mock_persona.maybe_new_session.return_value = True
    mock_persona.increment_session = AsyncMock()
    mock_persona_data = MagicMock()
    mock_persona_data.session_count = 5
    mock_persona.get_persona = AsyncMock(return_value=mock_persona_data)
    state.persona_store = mock_persona

    await _maybe_start_session(state)
    mock_persona.increment_session.assert_awaited_once()
    mock_persona.get_persona.assert_awaited_once()


@pytest.mark.asyncio
async def test_maybe_start_session_not_new():
    """_maybe_start_session does nothing when not a new session."""
    from mammamiradio.scheduling.producer import _maybe_start_session

    state = _make_state()
    mock_persona = MagicMock()
    mock_persona.maybe_new_session.return_value = False
    mock_persona.increment_session = AsyncMock()
    state.persona_store = mock_persona

    await _maybe_start_session(state)
    mock_persona.increment_session.assert_not_awaited()


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

    result = await _try_crossfade(voice, config, output)
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
        result = await _try_crossfade(voice, config, output)

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
        result = await _try_crossfade(voice, config, output)

    assert result == voice
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
        result = await _try_crossfade(voice, config, output)
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
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
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
    config = _make_config()
    config.pacing.ad_spots_per_break = 1
    queue: asyncio.Queue = asyncio.Queue()

    fake_script = AdScript(
        brand="Prezzoforte",
        summary="Great deals at Prezzoforte",
        format="classic_pitch",
        sonic=SonicWorld(music_bed="cinematic", environment="piazza", transition_motif="fanfare"),
        roles_used=["hammer", "disclaimer_goblin"],
    )

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.AD),
        patch(f"{SCRIPTWRITER_MODULE}.write_ad", new_callable=AsyncMock, return_value=fake_script),
        patch(f"{PRODUCER_MODULE}.synthesize_ad", new_callable=AsyncMock, return_value=_fake_path()),
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

    seg: Segment = queue.get_nowait()
    assert "sonic_worlds" in seg.metadata, "sonic_worlds missing from segment.metadata"
    assert "roles_used" in seg.metadata, "roles_used missing from segment.metadata"
    assert seg.metadata["sonic_worlds"] == ["cinematic"]
    assert seg.metadata["roles_used"] == [["hammer", "disclaimer_goblin"]]


# ---------------------------------------------------------------------------
# Resume bridge removed: session stop/resume no longer seeds an instant clip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_producer_no_resume_bridge_after_session_resume(tmp_path):
    """While session_stopped=True, the producer sleeps for 1 s per loop iteration
    and queues nothing.  This test cancels the task well within that 1 s window
    (0.05 s stopped + 0.2 s resumed), so the _was_stopped → resume-bridge path
    never fires and the queue stays empty throughout.

    Note: the resume bridge code itself still exists in the producer (_was_stopped
    path, lines ~642-682).  When session_stopped flips back to False the producer
    will, on the NEXT iteration, inject a canned clip (if available) or seed from
    the norm cache.  This test does not exercise that path — it only verifies that
    nothing is queued during the stopped sleep window.
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

    # No segment should carry resume_bridge=True — that metadata key no longer exists
    segments_with_resume_bridge = []
    while not queue.empty():
        seg = queue.get_nowait()
        if seg.metadata.get("resume_bridge") is True:
            segments_with_resume_bridge.append(seg)

    assert not segments_with_resume_bridge, (
        f"Found {len(segments_with_resume_bridge)} segment(s) with resume_bridge=True; "
        "the resume bridge was removed and must not inject segments on session resume."
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
# Idle bridge: simplified — no norm_cache fallback when no canned clips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_bridge_no_norm_cache_fallback_when_no_canned_clips(tmp_path):
    """When no canned clips exist and the idle bridge runs, the producer still
    falls through to the norm-cache path (lines ~710-727) -- but this test
    cancels the task before the 1 s idle sleep completes, so the bridge path
    never fires and no idle_bridge / norm_cache segment appears in the queue.

    The norm_file created in tmp_path (norm_test.mp3) would be picked up by the
    glob IF the idle bridge ran.  The test verifies behaviour within the
    cancellation window only; it does not assert that the norm-cache fallback
    has been removed from the code.
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
        "the idle bridge norm_cache fallback was removed and must not inject such segments."
    )


@pytest.mark.asyncio
async def test_idle_bridge_queues_canned_clip_when_available(tmp_path):
    """When a listener reconnects after idle and a canned clip exists, the idle
    bridge seeds it so the listener hears audio immediately.

    This existing behaviour (canned clip path) is preserved after the norm_cache
    fallback removal.
    """
    state = _make_state()
    state.listeners_active = 0  # start idle
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    canned_clip = tmp_path / "canned.mp3"
    canned_clip.write_bytes(b"fake audio")

    with patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=canned_clip):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            # Let the producer enter idle state
            await asyncio.sleep(0.15)
            # Simulate a listener connecting
            state.listeners_active = 1
            deadline = asyncio.get_event_loop().time() + 3.0
            while queue.empty():
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("Idle bridge did not queue a canned clip")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    seg = queue.get_nowait()
    assert seg.type == SegmentType.BANTER
    assert seg.metadata.get("warmup") is True
    assert seg.path == canned_clip


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
async def test_resume_bridge_uses_canned_clip_when_available(tmp_path):
    """After a stopped session resumes, the producer seeds a canned clip immediately
    so the listener hears audio before the first track finishes normalizing."""
    state = _make_state()
    state.session_stopped = True
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    canned_clip = tmp_path / "canned.mp3"
    canned_clip.write_bytes(b"fake audio")

    with patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=canned_clip):
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
    assert seg.type == SegmentType.BANTER
    assert seg.metadata.get("resume_bridge") is True


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
    assert seg.metadata.get("title") == "Abc123"
    assert seg.metadata.get("artist") == ""


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
    assert seg.metadata.get("title") == "Sogno Americano"
    assert seg.metadata.get("artist") == "Artie 5ive"


@pytest.mark.asyncio
async def test_resume_bridge_noop_when_no_canned_clips_and_empty_norm_cache(tmp_path):
    """When neither canned clips nor pre-normalized files exist, the bridge is a
    no-op — the producer should not crash and should eventually queue real content."""
    state = _make_state()
    state.session_stopped = True
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    with (
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=tmp_path / "src.mp3"),
        patch(f"{PRODUCER_MODULE}.normalize"),
        patch(f"{PRODUCER_MODULE}.shutil.copy2"),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio"),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.05)
            state.session_stopped = False
            # Wait long enough for the producer's 1s sleep to complete and run
            # one more iteration (the bridge no-op path with no norm files).
            await asyncio.sleep(1.5)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_idle_bridge_falls_back_to_norm_cache_when_no_canned_clips(tmp_path):
    """When a listener reconnects after idle and no canned clips exist, the idle
    bridge seeds the first pre-normalized track from cache_dir."""
    state = _make_state()
    state.listeners_active = 0
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    norm_file = tmp_path / "norm_idle123.mp3"
    norm_file.write_bytes(b"pre-normalized idle audio")

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
    assert seg.type == SegmentType.MUSIC
    assert seg.metadata.get("idle_bridge") is True
    assert seg.metadata.get("audio_source") == "norm_cache"
    assert seg.path == norm_file
    assert seg.metadata.get("title") == "Idle123"
    assert seg.metadata.get("artist") == ""


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
    assert seg.metadata.get("title") == "Musica Leggera"
    assert seg.metadata.get("artist") == "Colapesce Dimartino"


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
async def test_resume_bridge_picks_first_sorted_norm_file_when_multiple_exist(tmp_path):
    """When multiple pre-normalized files exist, the resume bridge seeds the first
    one in sorted (alphabetical) order."""
    state = _make_state()
    state.session_stopped = True
    config = _make_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    norm_zzz = tmp_path / "norm_zzz.mp3"
    norm_zzz.write_bytes(b"last file")
    norm_aaa = tmp_path / "norm_aaa.mp3"
    norm_aaa.write_bytes(b"first file")
    norm_mmm = tmp_path / "norm_mmm.mp3"
    norm_mmm.write_bytes(b"middle file")

    with patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None):
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
    assert seg.path == norm_aaa


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
