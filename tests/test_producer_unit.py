"""Unit tests for the producer pipeline in mammamiradio/producer.py."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
