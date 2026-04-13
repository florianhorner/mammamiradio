"""Unit tests for the producer pipeline in mammamiradio/producer.py."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mammamiradio.config import load_config
from mammamiradio.models import (
    HostPersonality,
    PlaylistSource,
    Segment,
    SegmentType,
    StationState,
    Track,
)
from mammamiradio.producer import (
    SHAREWARE_CANNED_LIMIT,
    _pick_canned_clip,
    run_producer,
)
from mammamiradio.scriptwriter import ListenerRequestCommit

TOML_PATH = str(Path(__file__).parent.parent / "radio.toml")
PRODUCER_MODULE = "mammamiradio.producer"


@pytest.fixture(autouse=True)
def _mock_quality_gate():
    with patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _clean_producer_globals():
    """Reset global state that leaks between tests."""
    from mammamiradio import producer

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
        patch(f"{PRODUCER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, None)),
        patch(f"{PRODUCER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
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
    from mammamiradio import producer

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
    from mammamiradio import producer

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
    from mammamiradio import producer

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
    from mammamiradio import producer

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

    caplog.set_level(logging.WARNING, logger="mammamiradio.producer")
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
    from mammamiradio.persona import PersonaStore
    from mammamiradio.producer import _record_motif
    from mammamiradio.sync import init_db

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
    from mammamiradio.persona import PersonaStore
    from mammamiradio.producer import _maybe_start_session
    from mammamiradio.sync import init_db

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
    from mammamiradio.producer import _record_motif

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
    from mammamiradio.producer import prewarm_first_segment

    state = StationState(playlist=[])
    config = _make_config()
    queue: asyncio.Queue = asyncio.Queue()
    result = await prewarm_first_segment(queue, state, config)
    assert result is False
    assert queue.empty()


@pytest.mark.asyncio
async def test_prewarm_stopped_session():
    """prewarm returns False when session is stopped."""
    from mammamiradio.producer import prewarm_first_segment

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
    from mammamiradio.producer import prewarm_first_segment

    state = _make_state()
    config = _make_config()
    config.tmp_dir.mkdir(parents=True, exist_ok=True)
    queue: asyncio.Queue = asyncio.Queue()

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=Path("/tmp/fake.mp3")),
        patch(f"{PRODUCER_MODULE}.normalize"),
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
async def test_prewarm_quality_gate_rejection():
    """prewarm returns False when quality gate rejects the track."""
    from mammamiradio.audio_quality import AudioQualityError
    from mammamiradio.producer import prewarm_first_segment

    state = _make_state()
    config = _make_config()
    config.tmp_dir.mkdir(parents=True, exist_ok=True)
    queue: asyncio.Queue = asyncio.Queue()

    def _reject(*_a, **_kw):
        raise AudioQualityError("silent track")

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=Path("/tmp/fake.mp3")),
        patch(f"{PRODUCER_MODULE}.normalize"),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", side_effect=_reject),
    ):
        result = await prewarm_first_segment(queue, state, config)

    assert result is False
    assert queue.empty()


@pytest.mark.asyncio
async def test_prewarm_download_exception():
    """prewarm returns False (not raises) on download failure."""
    from mammamiradio.producer import prewarm_first_segment

    state = _make_state()
    config = _make_config()
    queue: asyncio.Queue = asyncio.Queue()

    with patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, side_effect=RuntimeError("network")):
        result = await prewarm_first_segment(queue, state, config)

    assert result is False
    assert queue.empty()


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
        patch(f"{PRODUCER_MODULE}._has_script_llm", return_value=True),
        patch(f"{PRODUCER_MODULE}.write_banter", new_callable=AsyncMock, return_value=(banter_lines, commit)),
        patch(f"{PRODUCER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
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
        patch(f"{PRODUCER_MODULE}._has_script_llm", return_value=False),
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
        patch(f"{PRODUCER_MODULE}._has_script_llm", return_value=False),
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
# _pick_canned_clip unit tests
# ---------------------------------------------------------------------------


def test_pick_canned_clip_returns_none_for_empty_dir(tmp_path):
    """_pick_canned_clip returns None when the subdir has no clips."""
    from mammamiradio.producer import _canned_clip_cache, _pick_canned_clip

    empty_dir = tmp_path / "banter"
    empty_dir.mkdir()
    _canned_clip_cache.clear()
    with patch(f"{PRODUCER_MODULE}._DEMO_ASSETS_DIR", tmp_path):
        result = _pick_canned_clip("banter")
    assert result is None


def test_pick_canned_clip_returns_file(tmp_path):
    """_pick_canned_clip returns a path when clips exist."""
    from mammamiradio.producer import _canned_clip_cache, _pick_canned_clip, _recently_played_clips

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
    from mammamiradio.producer import _canned_clip_cache, _pick_canned_clip, _recently_played_clips

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
    from mammamiradio.producer import _canned_clip_cache, _pick_canned_clip

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
    from mammamiradio.producer import _record_motif

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
    from mammamiradio.producer import _record_motif

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
    from mammamiradio.producer import _maybe_start_session

    state = _make_state()
    state.persona_store = None
    await _maybe_start_session(state)


@pytest.mark.asyncio
async def test_maybe_start_session_new_session():
    """_maybe_start_session increments session when new."""
    from mammamiradio.producer import _maybe_start_session

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
    from mammamiradio.producer import _maybe_start_session

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
    from mammamiradio.models import AdBrand
    from mammamiradio.producer import _pick_brand

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
    from mammamiradio.models import AdBrand
    from mammamiradio.producer import _pick_brand

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
    from mammamiradio import producer

    producer._last_music_file = None
    from mammamiradio.producer import _latest_music_file

    result = _latest_music_file(tmp_path)
    assert result is None


def test_latest_music_file_returns_most_recent(tmp_path):
    """_latest_music_file returns the most recent music file."""
    import time

    from mammamiradio import producer

    producer._last_music_file = None
    from mammamiradio.producer import _latest_music_file

    old = tmp_path / "music_old.mp3"
    old.write_bytes(b"old")
    time.sleep(0.02)
    new = tmp_path / "music_new.mp3"
    new.write_bytes(b"new")

    result = _latest_music_file(tmp_path)
    assert result == new


def test_latest_music_file_uses_cache():
    """_latest_music_file returns cached path when available."""
    from mammamiradio import producer
    from mammamiradio.producer import _latest_music_file

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
    from mammamiradio import producer
    from mammamiradio.producer import _try_crossfade

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
    from mammamiradio import producer
    from mammamiradio.producer import _try_crossfade

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
    from mammamiradio import producer
    from mammamiradio.producer import _try_crossfade

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
    from mammamiradio.producer import _synthesize_impossible_moment

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
    from mammamiradio.models import AdBrand
    from mammamiradio.producer import _pick_brand

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
    from mammamiradio.models import AdBrand
    from mammamiradio.producer import _select_ad_creative

    brand = AdBrand(name="TestBrand", tagline="Test", category="tech")
    state = _make_state()
    config = _make_config()
    # Ensure only 1 voice
    config.ads.voices = [MagicMock(role="hammer")]

    ad_format, sonic, roles = _select_ad_creative(brand, state, config)
    assert ad_format is not None
    assert sonic is not None
    assert len(roles) >= 1


def test_select_ad_creative_no_voices():
    """_select_ad_creative handles empty voice list."""
    from mammamiradio.models import AdBrand
    from mammamiradio.producer import _select_ad_creative

    brand = AdBrand(name="TestBrand", tagline="Test", category="food")
    state = _make_state()
    config = _make_config()
    config.ads.voices = []

    ad_format, _sonic, _roles = _select_ad_creative(brand, state, config)
    assert ad_format is not None


def test_select_ad_creative_multivoice_only_fallback():
    """When format_pool is all multi-voice and only 1 voice, falls back to CLASSIC_PITCH."""
    from mammamiradio.models import AdBrand, AdFormat, CampaignSpine
    from mammamiradio.producer import _select_ad_creative

    brand = AdBrand(
        name="MultiVoiceBrand",
        tagline="Test",
        category="tech",
        campaign=CampaignSpine(format_pool=["duo_scene", "testimonial"]),
    )
    state = _make_state()
    config = _make_config()
    config.ads.voices = [MagicMock(role="hammer")]  # only 1 voice

    ad_format, _sonic, _roles = _select_ad_creative(brand, state, config)
    assert ad_format == AdFormat.CLASSIC_PITCH


# ---------------------------------------------------------------------------
# _cast_voices unit tests
# ---------------------------------------------------------------------------


def test_cast_voices_no_voices_configured():
    """_cast_voices falls back to host when no ad voices configured."""
    from mammamiradio.models import AdBrand
    from mammamiradio.producer import _cast_voices

    brand = AdBrand(name="TestBrand", tagline="Test", category="tech")
    config = _make_config()
    config.ads.voices = []

    result = _cast_voices(brand, config, ["hammer"])
    assert "hammer" in result
    assert result["hammer"].voice is not None


def test_cast_voices_reuses_when_exhausted():
    """_cast_voices reuses voices when more roles than available voices."""
    from mammamiradio.models import AdBrand, AdVoice
    from mammamiradio.producer import _cast_voices

    brand = AdBrand(name="TestBrand", tagline="Test", category="tech")
    config = _make_config()
    config.ads.voices = [AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="warm", role="hammer")]

    result = _cast_voices(brand, config, ["hammer", "maniac", "bureaucrat"])
    assert len(result) == 3
    # All three should have a voice assigned
    for role in ["hammer", "maniac", "bureaucrat"]:
        assert role in result


@pytest.mark.asyncio
async def test_record_motif_exception_is_swallowed():
    """_record_motif swallows exceptions from persona store."""
    from mammamiradio.producer import _record_motif

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
    from mammamiradio.producer import _set_last_music_file, _try_crossfade

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
    from mammamiradio.producer import prewarm_first_segment

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
    from mammamiradio.producer import prewarm_first_segment

    state = _make_state()
    config = _make_config()
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    queue: asyncio.Queue = asyncio.Queue()
    fake = tmp_path / "fake.mp3"
    fake.write_bytes(b"\x00" * 500)

    with (
        patch(f"{PRODUCER_MODULE}.download_track", return_value=fake),
        patch(f"{PRODUCER_MODULE}.normalize", side_effect=lambda src, dst: dst.write_bytes(b"\x00" * 500)),
        patch(f"{PRODUCER_MODULE}.generate_track_rationale", return_value="great track"),
        patch(f"{PRODUCER_MODULE}.classify_track_crate", return_value="charts"),
    ):
        result = await prewarm_first_segment(queue, state, config)
    assert result is True
    assert queue.qsize() == 1


def test_select_ad_creative_single_voice_excludes_multi():
    """_select_ad_creative excludes duo formats when only 1 voice available."""
    from mammamiradio.models import AdBrand, AdVoice
    from mammamiradio.producer import _select_ad_creative

    brand = AdBrand(name="Test", tagline="test", category="food")
    state = _make_state()
    config = _make_config()
    # Only 1 voice = multi-voice formats should be excluded
    config.ads.voices = [AdVoice(name="Solo", voice="it-IT-DiegoNeural", style="warm", role="hammer")]

    fmt, _sonic, _roles = _select_ad_creative(brand, state, config)
    from mammamiradio.models import AdFormat

    # Should not select a format that needs 2+ voices
    assert AdFormat(fmt).voice_count < 2
