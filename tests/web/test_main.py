"""Tests for the FastAPI app lifecycle in mammamiradio/main.py."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# Capture the real verdict runner at import time, before the autouse stub below
# patches the module attribute — Scenario-3 test needs the genuine function.
from mammamiradio.web.streamer import _run_provider_verdict as _real_run_provider_verdict

MODULE = "mammamiradio.main"
TEST_TMP = Path("/tmp/mammamiradio-test-main-tmp")
TEST_CACHE = Path("/tmp/mammamiradio-test-main-cache")


@pytest.fixture(autouse=True)
def _stub_provider_verdict():
    """Stop startup() from firing a real key-validation probe in lifecycle tests.

    The MagicMock configs below have truthy api-key attributes, so startup schedules
    the background verdict task. Stub the runner so it never reaches the network (it
    is looked up via a late `from ...streamer import _run_provider_verdict` inside
    startup, so patch the streamer module where it lives — not main).
    """
    with patch("mammamiradio.web.streamer._run_provider_verdict", new=AsyncMock()):
        yield


@pytest.mark.parametrize(
    ("env_value", "expected_level"),
    [
        (None, logging.WARNING),
        ("INFO", logging.INFO),
        ("invalid", logging.WARNING),
        ("BASIC_FORMAT", logging.WARNING),
    ],
)
def test_http_dependency_loggers_default_to_warning_with_env_override(monkeypatch, env_value, expected_level):
    """Successful httpx/httpcore request logs stay quiet unless explicitly enabled."""
    original_levels = {logger_name: logging.getLogger(logger_name).level for logger_name in ("httpx", "httpcore")}
    try:
        from mammamiradio.main import _configure_http_logging

        for logger_name in ("httpx", "httpcore"):
            logging.getLogger(logger_name).setLevel(logging.NOTSET)

        if env_value is None:
            monkeypatch.delenv("MAMMAMIRADIO_HTTP_LOG_LEVEL", raising=False)
        else:
            monkeypatch.setenv("MAMMAMIRADIO_HTTP_LOG_LEVEL", env_value)

        _configure_http_logging()

        assert logging.getLogger("httpx").level == expected_level
        assert logging.getLogger("httpcore").level == expected_level
    finally:
        for logger_name, level in original_levels.items():
            logging.getLogger(logger_name).setLevel(level)


def test_module_import_applies_http_logging_configuration(monkeypatch):
    """Removing the module-level _configure_http_logging() call must break this test."""
    import importlib

    import mammamiradio.main

    original_levels = {logger_name: logging.getLogger(logger_name).level for logger_name in ("httpx", "httpcore")}
    try:
        for logger_name in ("httpx", "httpcore"):
            logging.getLogger(logger_name).setLevel(logging.NOTSET)
        monkeypatch.setenv("MAMMAMIRADIO_HTTP_LOG_LEVEL", "DEBUG")

        importlib.reload(mammamiradio.main)

        assert logging.getLogger("httpx").level == logging.DEBUG
        assert logging.getLogger("httpcore").level == logging.DEBUG
    finally:
        for logger_name, level in original_levels.items():
            logging.getLogger(logger_name).setLevel(level)


@pytest.mark.asyncio
async def test_startup_creates_state_and_tasks():
    """startup() loads config, fetches playlist, sets app.state, creates tasks."""
    from mammamiradio.core.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = TEST_TMP
    mock_config.cache_dir = TEST_CACHE

    demo_tracks = [Track(title="Song", artist="Art", duration_ms=1000, spotify_id="t1")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(demo_tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
    ):
        from mammamiradio.main import app, startup

        await startup()

        # Verify app.state was populated
        assert hasattr(app.state, "queue")
        assert hasattr(app.state, "stream_hub")
        assert hasattr(app.state, "station_state")
        assert hasattr(app.state, "config")
        assert hasattr(app.state, "producer_task")
        assert hasattr(app.state, "playback_task")
        assert app.state.station_state.playlist == demo_tracks


@pytest.mark.asyncio
async def test_startup_wires_release_campaign_from_cache_dir():
    """Release campaign state is startup-owned and shared with streamer/producer."""
    from mammamiradio.core.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = TEST_TMP
    mock_config.cache_dir = TEST_CACHE

    campaign = MagicMock()
    demo_tracks = [Track(title="Song", artist="Art", duration_ms=1000, spotify_id="t1")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(demo_tracks, None, "")),
        patch(f"{MODULE}.ReleaseCampaign") as m_campaign,
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
    ):
        m_campaign.load.return_value = campaign
        from mammamiradio.main import app, startup

        await startup()

    m_campaign.load.assert_called_once_with(TEST_CACHE)
    assert app.state.station_state.release_campaign is campaign
    assert app.state.release_campaign is campaign


@pytest.mark.asyncio
async def test_startup_survives_release_campaign_load_failure(tmp_path: Path):
    """A corrupt/unreadable manifest or ledger must never abort startup (INSTANT
    AUDIO) — startup falls back to a fully inert campaign instead of crashing."""
    from mammamiradio.core.models import Track
    from mammamiradio.release_campaign import ReleaseCampaign

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"
    demo_tracks = [Track(title="Song", artist="Art", duration_ms=1000, spotify_id="t1")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(demo_tracks, None, "")),
        patch.object(ReleaseCampaign, "load", side_effect=RuntimeError("ledger corrupt")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
    ):
        from mammamiradio.main import app, startup

        await startup()

    assert app.state.station_state.release_campaign is not None
    assert app.state.station_state.release_campaign.enabled is False


@pytest.mark.asyncio
async def test_startup_survives_restart_handoff_admission_failure(tmp_path: Path):
    """An unexpected exception admitting restart-handoff entries must not abort
    startup — the station boots without the cold-open bridge instead."""
    from mammamiradio.core.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"
    demo_tracks = [Track(title="Song", artist="Art", duration_ms=1000, spotify_id="t1")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(demo_tracks, None, "")),
        patch(f"{MODULE}.admit_restart_handoff_entries", side_effect=RuntimeError("cache dir unreadable")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
    ):
        from mammamiradio.main import app, startup

        await startup()

    assert app.state.queue.qsize() == 0


@pytest.mark.asyncio
async def test_startup_admits_restart_handoff_before_tasks(tmp_path: Path):
    """Safe restart handoff enters the real queue and shadow before prewarm starts."""
    from mammamiradio.core.models import Segment, SegmentType, Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"

    handoff = Segment(
        type=SegmentType.MUSIC,
        path=tmp_path / "cache" / "restart_handoff" / "segments" / "song.mp3",
        duration_sec=120.0,
        metadata={"title": "Artist - Song", "source_kind": "restart_handoff"},
        ephemeral=False,
    )
    handoff.path.parent.mkdir(parents=True, exist_ok=True)
    handoff.path.write_bytes(b"audio")
    demo_tracks = [Track(title="Song", artist="Art", duration_ms=1000, spotify_id="t1")]
    prewarm_depths: list[int] = []

    async def _prewarm(queue, state, config):
        prewarm_depths.append(queue.qsize())

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(demo_tracks, None, "")),
        patch(f"{MODULE}.admit_restart_handoff_entries") as m_admit,
        patch(f"{MODULE}.prewarm_first_segment", side_effect=_prewarm),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
    ):
        m_admit.return_value.to_segments.return_value = [handoff]
        m_admit.return_value.rejected = ()
        from mammamiradio.main import app, startup

        await startup()
        await app.state.prewarm_task

    assert prewarm_depths and prewarm_depths[0] == 1
    assert app.state.queue.qsize() == 1
    assert app.state.station_state.queued_segments[0]["source_kind"] == "restart_handoff"
    assert app.state.station_state.queued_segments[0]["id"] == handoff.metadata["queue_id"]
    assert app.state.station_state.last_enqueued_type is SegmentType.MUSIC
    assert app.state.station_state.last_music_file == handoff.path


@pytest.mark.asyncio
async def test_startup_skips_restart_handoff_when_session_stopped(tmp_path: Path):
    from mammamiradio.core.models import Segment, SegmentType, Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"
    mock_config.cache_dir.mkdir(parents=True, exist_ok=True)
    (mock_config.cache_dir / "session_stopped.flag").write_text("stopped")

    handoff = Segment(
        type=SegmentType.MUSIC,
        path=tmp_path / "song.mp3",
        duration_sec=120.0,
        metadata={"title": "Artist - Song"},
        ephemeral=False,
    )
    tracks = [Track(title="Song", artist="Art", duration_ms=1000, spotify_id="t1")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(tracks, None, "")),
        patch(f"{MODULE}.admit_restart_handoff_entries") as m_admit,
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
    ):
        m_admit.return_value.to_segments.return_value = [handoff]
        from mammamiradio.main import app, startup

        await startup()

    m_admit.assert_not_called()
    assert app.state.queue.qsize() == 0
    assert app.state.station_state.session_stopped is True


def test_restart_handoff_admission_stops_when_queue_is_full(tmp_path: Path):
    from mammamiradio.core.models import Segment, SegmentType, StationState
    from mammamiradio.main import _admit_restart_handoff

    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    queue.put_nowait(Segment(type=SegmentType.MUSIC, path=tmp_path / "already.mp3"))
    state = StationState()
    config = MagicMock()
    config.cache_dir = tmp_path
    handoff = Segment(
        type=SegmentType.MUSIC,
        path=tmp_path / "restart_handoff" / "segments" / "song.mp3",
        metadata={"title": "Artist - Song"},
    )

    with patch(f"{MODULE}.admit_restart_handoff_entries") as m_admit:
        m_admit.return_value.to_segments.return_value = [handoff]
        m_admit.return_value.rejected = ()
        accepted = _admit_restart_handoff(queue, state, config)

    assert accepted == 0
    assert queue.qsize() == 1
    assert state.queued_segments == []


def test_restart_handoff_admission_records_admitted_paths(tmp_path: Path):
    """F2: each queued handoff file's resolved path is recorded so the per-enqueue
    spool prune can protect it — this is the exact snapshot the producer passes as
    protected_paths."""
    from mammamiradio.core.models import Segment, SegmentType, StationState
    from mammamiradio.main import _admit_restart_handoff

    queue: asyncio.Queue = asyncio.Queue(maxsize=4)
    state = StationState()
    config = MagicMock()
    config.cache_dir = tmp_path
    handoff = Segment(
        type=SegmentType.MUSIC,
        path=tmp_path / "restart_handoff" / "segments" / "song.mp3",
        metadata={"title": "Artist - Song"},
    )

    with patch(f"{MODULE}.admit_restart_handoff_entries") as m_admit:
        m_admit.return_value.to_segments.return_value = [handoff]
        m_admit.return_value.rejected = ()
        accepted = _admit_restart_handoff(queue, state, config)

    assert accepted == 1
    assert state.restart_handoff_admitted_paths == {handoff.path.resolve(strict=False)}


def test_restart_handoff_admission_falls_back_when_resolve_raises(tmp_path: Path):
    """A resolve() failure (e.g. symlink loop) must not break startup — the
    unresolved path is protected instead (INSTANT AUDIO: never fail the cold open)."""
    from mammamiradio.core.models import Segment, SegmentType, StationState
    from mammamiradio.main import _admit_restart_handoff

    queue: asyncio.Queue = asyncio.Queue(maxsize=4)
    state = StationState()
    config = MagicMock()
    config.cache_dir = tmp_path
    bad_path = MagicMock(spec=Path)
    bad_path.resolve.side_effect = OSError("too many symlinks")
    handoff = Segment(type=SegmentType.MUSIC, path=bad_path, metadata={"title": "Artist - Song"})

    with patch(f"{MODULE}.admit_restart_handoff_entries") as m_admit:
        m_admit.return_value.to_segments.return_value = [handoff]
        m_admit.return_value.rejected = ()
        accepted = _admit_restart_handoff(queue, state, config)

    assert accepted == 1
    assert state.restart_handoff_admitted_paths == {bad_path}


def test_restart_handoff_logs_when_only_rejected_segments_exist(tmp_path: Path, caplog):
    from mammamiradio.core.models import StationState
    from mammamiradio.main import _admit_restart_handoff

    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    state = StationState()
    config = MagicMock()
    config.cache_dir = tmp_path

    caplog.set_level(logging.INFO, logger="mammamiradio")
    with patch(f"{MODULE}.admit_restart_handoff_entries") as m_admit:
        m_admit.return_value.to_segments.return_value = []
        m_admit.return_value.rejected = ("blocked",)
        accepted = _admit_restart_handoff(queue, state, config)

    assert accepted == 0
    assert "Restart handoff: no segments admitted (1 rejected)" in caplog.text


@pytest.mark.asyncio
async def test_startup_filters_blocklisted_tracks_from_pool():
    """A banned song must not survive the cold-start re-fetch (the reported bug):
    startup() loads the persisted blocklist and filters the fresh pool before it
    reaches the producer."""
    from mammamiradio.core.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = TEST_TMP
    mock_config.cache_dir = TEST_CACHE

    pool = [
        Track(title="Volare", artist="Modugno", duration_ms=1000, spotify_id="t1"),
        Track(title="Felicità", artist="Al Bano", duration_ms=1000, spotify_id="t2"),
    ]
    blocklist = {("modugno", "volare"): {"display": "Modugno - Volare", "banned_by": "operator", "banned_at": 1.0}}

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(pool, None, "")),
        patch(f"{MODULE}.load_blocklist", return_value=blocklist),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
    ):
        from mammamiradio.main import app, startup

        await startup()

        titles = [t.title for t in app.state.station_state.playlist]
        assert titles == ["Felicità"]
        assert app.state.station_state.blocklist == blocklist


@pytest.mark.asyncio
async def test_startup_wires_loudness_targets_from_config():
    """startup() must thread radio.toml's [audio] LUFS targets into the normalizer
    (the config -> startup -> normalizer-global seam). Patched on the normalizer
    module because startup() imports the function via a late local import."""
    from mammamiradio.core.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = TEST_TMP
    mock_config.cache_dir = TEST_CACHE
    mock_config.audio.lufs_target = -16.0
    mock_config.audio.ad_lufs_target = -15.0
    mock_config.audio.sample_rate = 48000
    mock_config.audio.channels = 2
    mock_config.audio.bitrate = 192

    demo_tracks = [Track(title="Song", artist="Art", duration_ms=1000, spotify_id="t1")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(demo_tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch("mammamiradio.audio.normalizer.configure_loudness_reconcile") as m_configure,
    ):
        from mammamiradio.main import startup

        await startup()

    # The encoding params must thread through too, so reconcile preserves a
    # non-default sample rate / channels / bitrate.
    m_configure.assert_called_once_with(-16.0, -15.0, sample_rate=48000, channels=2, bitrate=192)


@pytest.mark.asyncio
async def test_startup_wires_running_gag_policy_from_config():
    """startup() must thread [home.running_gags] into EveningLedger.load(), and an
    empty override list must become None so the built-in domain default applies
    (the config -> startup -> ledger seam, incl. the []→None translation)."""
    from mammamiradio.core.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = TEST_TMP
    mock_config.cache_dir = TEST_CACHE
    # Real lists (not Mock attrs) so the `... or None` translation is exercised.
    mock_config.running_gags.domain_allowlist = ["light"]
    mock_config.running_gags.entity_allowlist = []  # empty → None
    mock_config.running_gags.entity_denylist = ["binary_sensor.flappy"]

    demo_tracks = [Track(title="Song", artist="Art", duration_ms=1000, spotify_id="t1")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(demo_tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch(f"{MODULE}.EveningLedger") as m_ledger,
    ):
        from mammamiradio.main import startup

        await startup()

    m_ledger.load.assert_called_once_with(
        TEST_CACHE,
        domain_allowlist=["light"],
        entity_allowlist=None,
        entity_denylist=["binary_sensor.flappy"],
    )


@pytest.mark.asyncio
async def test_startup_skips_provider_verdict_when_no_keys():
    """With no AI key configured, startup() must NOT schedule a validation probe."""
    from mammamiradio.core.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = TEST_TMP
    mock_config.cache_dir = TEST_CACHE
    mock_config.anthropic_api_key = ""
    mock_config.openai_api_key = ""

    demo_tracks = [Track(title="Song", artist="Art", duration_ms=1000, spotify_id="t1")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(demo_tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch("mammamiradio.web.streamer._run_provider_verdict", new=AsyncMock()) as verdict,
    ):
        from mammamiradio.main import startup

        await startup()

        verdict.assert_not_called()


@pytest.mark.asyncio
async def test_startup_with_key_schedules_provider_verdict():
    """With a key configured, startup() must schedule the background validation probe."""
    from mammamiradio.core.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = TEST_TMP
    mock_config.cache_dir = TEST_CACHE
    mock_config.anthropic_api_key = "sk-ant-x"
    mock_config.openai_api_key = ""

    demo_tracks = [Track(title="Song", artist="Art", duration_ms=1000, spotify_id="t1")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(demo_tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch("mammamiradio.web.streamer._run_provider_verdict", new=AsyncMock()) as verdict,
    ):
        from mammamiradio.main import app, startup

        await startup()

        verdict.assert_called_once()
        assert hasattr(app.state, "provider_verdict_task")


@pytest.mark.asyncio
async def test_startup_persisted_bogus_key_reads_rejected_after_boot():
    """Scenario 3 (post-restart): a bogus key persisted in .env surfaces as rejected on boot.

    Simulates the HA-watchdog-restart path — fresh StationState, key already on disk —
    and proves the admin would show "not working" without waiting for a banter to fail.
    """
    from mammamiradio.core.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = TEST_TMP
    mock_config.cache_dir = TEST_CACHE
    mock_config.anthropic_api_key = "sk-ant-persisted-bogus"
    mock_config.openai_api_key = ""

    demo_tracks = [Track(title="Song", artist="Art", duration_ms=1000, spotify_id="t1")]
    probe = {
        "ok": False,
        "providers": {
            "anthropic": {
                "provider": "anthropic",
                "configured": True,
                "ok": False,
                "status_code": 401,
                "error_type": "authentication_error",
                "detail": "",
            },
            "openai_chat": {
                "provider": "openai_chat",
                "configured": False,
                "ok": False,
                "status_code": None,
                "error_type": "not_configured",
                "detail": "",
            },
            "openai_tts": {
                "provider": "openai_tts",
                "configured": False,
                "ok": False,
                "status_code": None,
                "error_type": "not_configured",
                "detail": "",
            },
        },
    }

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(demo_tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        # Real _run_provider_verdict (autouse stub bypassed) with a mocked probe boundary.
        patch("mammamiradio.web.streamer._run_provider_verdict", new=_real_run_provider_verdict),
        patch("mammamiradio.web.provider_verdict.check_provider_keys", new=AsyncMock(return_value=probe)),
    ):
        from mammamiradio.main import app, startup

        await startup()
        await app.state.provider_verdict_task

    assert app.state.station_state.anthropic_key_status == "rejected"


@pytest.mark.asyncio
async def test_startup_prewarm_is_capped_to_two_on_addon(tmp_path: Path):
    """startup() prewarms exactly two segments even when running as HA addon."""
    from mammamiradio.core.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"
    mock_config.audio.bitrate = 192
    mock_config.is_addon = True
    mock_config.allow_ytdlp = False
    mock_config.homeassistant.enabled = False

    tracks = [Track(title="S", artist="A", duration_ms=1, spotify_id="x")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch(f"{MODULE}.prewarm_first_segment", new_callable=AsyncMock) as mock_prewarm,
    ):
        from mammamiradio.main import app, startup

        await startup()
        await app.state.prewarm_task

    assert mock_prewarm.await_count == 2


@pytest.mark.asyncio
async def test_startup_reads_persisted_source_before_fetching():
    from mammamiradio.core.models import PlaylistSource, Track

    order: list[str] = []
    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = TEST_TMP
    mock_config.cache_dir = TEST_CACHE
    persisted = PlaylistSource(kind="playlist", source_id="abc", label="Roadtrip")

    def _read(_cache_dir):
        order.append("read")
        return persisted

    def _fetch(_config, received):
        order.append("fetch")
        assert received is persisted
        return [Track(title="S", artist="A", duration_ms=1, spotify_id="x")], persisted, ""

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", side_effect=_read),
        patch(f"{MODULE}.fetch_startup_playlist", side_effect=_fetch),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
    ):
        from mammamiradio.main import startup

        await startup()

    assert order == ["read", "fetch"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("flag_exists", "expected"), [(True, True), (False, False)])
async def test_startup_restores_stopped_session_flag(tmp_path: Path, flag_exists: bool, expected: bool):
    """startup() preserves session_stopped across restarts so operator stop survives crashes."""
    from mammamiradio.core.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"

    flag_file = mock_config.cache_dir / "session_stopped.flag"
    if flag_exists:
        flag_file.parent.mkdir(parents=True, exist_ok=True)
        flag_file.touch()

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(
            f"{MODULE}.fetch_startup_playlist",
            return_value=([Track(title="S", artist="A", duration_ms=1, spotify_id="x")], None, ""),
        ),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
    ):
        from mammamiradio.main import app, startup

        await startup()

    assert app.state.station_state.session_stopped is expected
    # Flag file is preserved when it existed (operator stop survives restart)
    assert flag_file.exists() == flag_exists


@pytest.mark.asyncio
async def test_startup_boot_summary_and_purge(tmp_path: Path):
    """startup() calls purge_suspect_cache_files and logs boot summary."""
    from mammamiradio.core.models import PlaylistSource, Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"
    mock_config.homeassistant.enabled = False
    mock_config.allow_ytdlp = True
    mock_config.audio.bitrate = 192

    ps = PlaylistSource(kind="charts", source_id="it", label="Italian charts")
    tracks = [Track(title="S", artist="A", duration_ms=1, spotify_id="x")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(tracks, ps, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch(f"{MODULE}.purge_suspect_cache_files", return_value=3) as mock_purge,
        patch(f"{MODULE}.prune_stale_tmp_files", return_value=2) as mock_prune_tmp,
        patch(f"{MODULE}.prune_stale_handoff_tmp_files", return_value=4) as mock_prune_handoff_tmp,
        patch(f"{MODULE}.prewarm_first_segment", new_callable=AsyncMock),
    ):
        from mammamiradio.main import startup

        await startup()
        mock_purge.assert_called_once()
        # Stale temp render scratch is pruned once at startup (#407).
        mock_prune_tmp.assert_called_once_with(mock_config.tmp_dir)
        mock_prune_handoff_tmp.assert_called_once_with(mock_config.cache_dir)

        # Verify clip ring buffer was created
        from mammamiradio.main import app
        from mammamiradio.web.streamer import CLIP_MAX_SEGMENT_SECONDS

        assert hasattr(app.state, "clip_ring_buffer")
        # Happy-path maxlen is sized for the longest shareable ad/banter segment
        # (not the 240 fallback), and the lookback slot starts empty.
        expected_maxlen = max(240, 192 * 1000 // 8 * CLIP_MAX_SEGMENT_SECONDS // 4096)
        assert app.state.clip_ring_buffer.maxlen == expected_maxlen
        assert expected_maxlen > 240
        assert app.state.last_shareworthy_clip is None


@pytest.mark.asyncio
async def test_startup_clip_ring_buffer_type_error(tmp_path: Path):
    """Clip ring buffer init handles TypeError from config.audio.bitrate."""
    from mammamiradio.core.models import Track

    class _BadBitrate:
        def __int__(self) -> int:
            raise TypeError("cannot convert")

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"
    mock_config.homeassistant.enabled = False
    mock_config.allow_ytdlp = False
    # Use a real __int__ failure; MagicMock coerces to 1 here and misses the fallback branch.
    mock_config.audio.bitrate = _BadBitrate()

    tracks = [Track(title="S", artist="A", duration_ms=1, spotify_id="x")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch(f"{MODULE}.prewarm_first_segment", new_callable=AsyncMock),
    ):
        from mammamiradio.main import app, startup

        await startup()
        # Should have fallen back to maxlen=240
        assert app.state.clip_ring_buffer.maxlen == 240


@pytest.mark.asyncio
async def test_healthz_and_readyz_contract_after_startup(tmp_path: Path):
    """/healthz and /readyz honour their contracts after the real startup() lifecycle.

    The existing health-probe unit tests build a synthetic app with direct state
    injection.  This test goes through startup() so regressions in the lifespan
    path (e.g. a missing app.state field, a broken start_time assignment) are
    caught before they reach production.

    Expected behaviour:
    - /healthz always returns 200 once startup completes
    - /readyz returns 503 "starting" when the queue is empty (prewarm mocked away)
    """
    from mammamiradio.core.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"
    mock_config.homeassistant.enabled = False
    mock_config.allow_ytdlp = False
    mock_config.audio.bitrate = 192

    tracks = [Track(title="S", artist="A", duration_ms=1, spotify_id="x")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch(f"{MODULE}.prewarm_first_segment", new_callable=AsyncMock),
    ):
        from mammamiradio.main import app, startup

        await startup()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        health = await client.get("/healthz")
        ready = await client.get("/readyz")

    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert "uptime_s" in body

    # prewarm was mocked away → queue is empty → station is still starting
    assert ready.status_code == 503
    assert ready.json()["status"] == "starting"


@pytest.mark.asyncio
async def test_shutdown_cancels_tasks():
    """shutdown() cancels all lifecycle tasks and clears app.state handles."""
    import mammamiradio.main as main_mod

    prewarm_task = AsyncMock()
    producer_task = AsyncMock()
    playback_task = AsyncMock()
    for task in (prewarm_task, producer_task, playback_task):
        task.cancel = MagicMock()

    main_mod._prewarm_task = prewarm_task
    main_mod._producer_task = producer_task
    main_mod._playback_task = playback_task
    main_mod.app.state.prewarm_task = prewarm_task
    main_mod.app.state.producer_task = producer_task
    main_mod.app.state.playback_task = playback_task
    main_mod.app.state.stream_hub = MagicMock()
    # Isolate from prior tests that may have left a verdict probe / background
    # tasks on the shared app.state — this test asserts exactly the 3 lifecycle
    # tasks are gathered.
    main_mod.app.state.provider_verdict_task = None
    main_mod.app.state.background_tasks = set()

    with patch("asyncio.gather", new_callable=AsyncMock) as mock_gather:
        await main_mod.shutdown()

    for task in (prewarm_task, producer_task, playback_task):
        task.cancel.assert_called_once()
    mock_gather.assert_called_once_with(prewarm_task, producer_task, playback_task, return_exceptions=True)
    assert main_mod.app.state.prewarm_task is None
    assert main_mod.app.state.producer_task is None
    assert main_mod.app.state.playback_task is None
    main_mod.app.state.stream_hub.close.assert_called_once()

    # Cleanup
    main_mod._prewarm_task = None
    main_mod._producer_task = None
    main_mod._playback_task = None


@pytest.mark.asyncio
async def test_shutdown_cancels_background_tasks():
    """shutdown() also cancels fire-and-forget background tasks (queue-from-search
    / listener song downloads) so an in-flight yt-dlp fetch can't write to
    app.state after teardown begins."""
    import mammamiradio.main as main_mod

    main_mod._prewarm_task = None
    main_mod._producer_task = None
    main_mod._playback_task = None
    bg_task = AsyncMock()
    bg_task.cancel = MagicMock()
    # The provider-verdict probe lives outside the background_tasks set; shutdown
    # must cancel it too.
    verdict_task = AsyncMock()
    verdict_task.cancel = MagicMock()
    main_mod.app.state.provider_verdict_task = verdict_task
    main_mod.app.state.background_tasks = {bg_task}
    main_mod.app.state.stream_hub = MagicMock()

    with patch("asyncio.gather", new_callable=AsyncMock) as mock_gather:
        await main_mod.shutdown()

    bg_task.cancel.assert_called_once()
    verdict_task.cancel.assert_called_once()
    _args, _kwargs = mock_gather.call_args
    assert bg_task in _args
    assert verdict_task in _args
    assert _kwargs.get("return_exceptions") is True

    # Cleanup
    main_mod.app.state.background_tasks = set()
    main_mod.app.state.provider_verdict_task = None


@pytest.mark.asyncio
async def test_shutdown_cancels_restart_handoff_tasks():
    """shutdown() also cancels in-flight restart-handoff spool writes (same
    write-after-shutdown race as the background download tasks) so a spool
    write can't still be touching disk once teardown proceeds."""
    import mammamiradio.main as main_mod
    from mammamiradio.core.models import StationState

    main_mod._prewarm_task = None
    main_mod._producer_task = None
    main_mod._playback_task = None
    rh_task = AsyncMock()
    rh_task.cancel = MagicMock()
    main_mod.app.state.provider_verdict_task = None
    main_mod.app.state.background_tasks = set()
    main_mod.app.state.stream_hub = MagicMock()
    main_mod.app.state.station_state = StationState()
    main_mod.app.state.station_state._restart_handoff_tasks = {rh_task}

    with patch("asyncio.gather", new_callable=AsyncMock) as mock_gather:
        await main_mod.shutdown()

    rh_task.cancel.assert_called_once()
    _args, _kwargs = mock_gather.call_args
    assert rh_task in _args
    assert _kwargs.get("return_exceptions") is True

    # Cleanup
    main_mod.app.state.station_state._restart_handoff_tasks = set()


@pytest.mark.asyncio
async def test_shutdown_flushes_release_campaign():
    import mammamiradio.main as main_mod

    main_mod._producer_task = None
    main_mod._playback_task = None
    main_mod._prewarm_task = None
    for attr in ("producer_task", "prewarm_task", "playback_task", "stream_hub", "background_tasks", "ledger"):
        if hasattr(main_mod.app.state, attr):
            delattr(main_mod.app.state, attr)
    main_mod.app.state.provider_verdict_task = None

    campaign = MagicMock()
    main_mod.app.state.release_campaign = campaign

    with patch("asyncio.gather", new_callable=AsyncMock):
        await main_mod.shutdown()

    campaign.save_if_dirty.assert_called_once()


@pytest.mark.asyncio
async def test_shutdown_logs_release_campaign_flush_failure(caplog):
    import mammamiradio.main as main_mod

    main_mod._producer_task = None
    main_mod._playback_task = None
    main_mod._prewarm_task = None
    for attr in ("producer_task", "prewarm_task", "playback_task", "stream_hub", "background_tasks", "ledger"):
        if hasattr(main_mod.app.state, attr):
            delattr(main_mod.app.state, attr)
    main_mod.app.state.provider_verdict_task = None

    campaign = MagicMock()
    campaign.save_if_dirty.side_effect = RuntimeError("disk full")
    main_mod.app.state.release_campaign = campaign

    caplog.set_level(logging.WARNING, logger=MODULE)
    with patch("asyncio.gather", new_callable=AsyncMock):
        await main_mod.shutdown()

    campaign.save_if_dirty.assert_called_once()
    assert "Failed to flush release campaign ledger during shutdown" in caplog.text


@pytest.mark.asyncio
async def test_startup_demo_fallback_on_fetch_exception(tmp_path: Path):
    """When fetch_startup_playlist raises, startup falls back to DEMO_TRACKS."""
    from mammamiradio.main import app, startup
    from mammamiradio.playlist.playlist import DEMO_TRACKS

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"
    mock_config.homeassistant.enabled = False
    mock_config.allow_ytdlp = False
    mock_config.audio.bitrate = 128

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", side_effect=RuntimeError("network down")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch(f"{MODULE}.prewarm_first_segment", new_callable=AsyncMock),
    ):
        await startup()

    # State should contain the demo tracks, not an empty list
    assert app.state.station_state.playlist == list(DEMO_TRACKS)
    assert app.state.station_state.startup_source_error == "network down"
    # playlist_source should be demo kind
    assert app.state.station_state.playlist_source.kind == "demo"


@pytest.mark.asyncio
async def test_startup_clip_ring_buffer_fallback_to_240(tmp_path: Path):
    """Ring buffer maxlen falls back to 240 when config.audio.bitrate raises ValueError."""
    from mammamiradio.core.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"
    mock_config.homeassistant.enabled = False
    mock_config.allow_ytdlp = False
    # Simulate a config value that causes ValueError when int() is called
    mock_config.audio.bitrate = MagicMock(side_effect=ValueError("not a number"))

    tracks = [Track(title="S", artist="A", duration_ms=1, spotify_id="x")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch(f"{MODULE}.prewarm_first_segment", new_callable=AsyncMock),
    ):
        from mammamiradio.main import app, startup

        await startup()

    assert app.state.clip_ring_buffer.maxlen == 240


@pytest.mark.asyncio
async def test_startup_no_ffmpeg_warning_when_found(tmp_path: Path, caplog):
    """startup() skips the FFmpeg warning when shutil.which finds ffmpeg."""
    import logging

    from mammamiradio.core.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"
    mock_config.homeassistant.enabled = False
    mock_config.allow_ytdlp = False
    mock_config.audio.bitrate = 192

    tracks = [Track(title="S", artist="A", duration_ms=1, spotify_id="x")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch(f"{MODULE}.prewarm_first_segment", new_callable=AsyncMock),
        patch(f"{MODULE}.shutil.which", return_value="/usr/bin/ffmpeg"),
        caplog.at_level(logging.WARNING, logger="mammamiradio"),
    ):
        from mammamiradio.main import startup

        await startup()

    assert not any("FFmpeg not found" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_startup_warns_when_ytdlp_missing_but_allowed(tmp_path: Path, caplog):
    """startup() warns when yt-dlp is allowed in config but the binary is not installed."""
    import logging

    from mammamiradio.core.models import PlaylistSource, Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"
    mock_config.homeassistant.enabled = False
    mock_config.allow_ytdlp = True
    mock_config.audio.bitrate = 192

    ps = PlaylistSource(kind="charts", source_id="it", label="Italian charts")
    tracks = [Track(title="S", artist="A", duration_ms=1, spotify_id="x")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(tracks, ps, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch(f"{MODULE}.prewarm_first_segment", new_callable=AsyncMock),
        patch(f"{MODULE}.shutil.which", return_value=None),
        caplog.at_level(logging.WARNING, logger="mammamiradio"),
    ):
        from mammamiradio.main import startup

        await startup()

    assert any("yt-dlp" in r.message and "not found" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_startup_no_ytdlp_warning_when_blocked(tmp_path: Path, caplog):
    """startup() does not warn about missing yt-dlp when allow_ytdlp is False."""
    import logging

    from mammamiradio.core.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"
    mock_config.homeassistant.enabled = False
    mock_config.allow_ytdlp = False
    mock_config.audio.bitrate = 192

    tracks = [Track(title="S", artist="A", duration_ms=1, spotify_id="x")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch(f"{MODULE}.prewarm_first_segment", new_callable=AsyncMock),
        patch(f"{MODULE}.shutil.which", return_value=None),
        caplog.at_level(logging.WARNING, logger="mammamiradio"),
    ):
        from mammamiradio.main import startup

        await startup()

    assert not any("yt-dlp" in r.message and "not found" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_startup_no_ffmpeg_warning_when_ffmpeg_found(tmp_path: Path, caplog):
    """startup() does not warn about missing FFmpeg when it is available on PATH."""
    import logging

    from mammamiradio.core.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"
    mock_config.homeassistant.enabled = False
    mock_config.allow_ytdlp = True
    mock_config.audio.bitrate = 192

    tracks = [Track(title="S", artist="A", duration_ms=1, spotify_id="x")]

    def _which(name: str):
        return "/usr/bin/ffmpeg" if name == "ffmpeg" else None

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch(f"{MODULE}.prewarm_first_segment", new_callable=AsyncMock),
        patch(f"{MODULE}.shutil.which", side_effect=_which),
        caplog.at_level(logging.WARNING, logger="mammamiradio"),
    ):
        from mammamiradio.main import startup

        await startup()

    assert not any("FFmpeg" in r.message and "not found" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_lifespan_calls_startup_and_shutdown(tmp_path):
    """_lifespan context manager calls startup() then shutdown() around the yield."""

    from mammamiradio.core.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"

    tracks = [Track(title="S", artist="A", duration_ms=1, spotify_id="x")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
    ):
        from mammamiradio.main import _lifespan, app

        async with _lifespan(app):
            assert hasattr(app.state, "station_state")


@pytest.mark.asyncio
async def test_startup_clip_ring_buffer_invalid_string_bitrate(tmp_path: Path):
    """Ring buffer maxlen falls back to 240 when config.audio.bitrate is an unparseable string."""
    from mammamiradio.core.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"
    mock_config.homeassistant.enabled = False
    mock_config.allow_ytdlp = False
    # A plain string causes ValueError from int()
    mock_config.audio.bitrate = "not-a-number"

    tracks = [Track(title="S", artist="A", duration_ms=1, spotify_id="x")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch(f"{MODULE}.prewarm_first_segment", new_callable=AsyncMock),
    ):
        from mammamiradio.main import app, startup

        await startup()

    assert app.state.clip_ring_buffer.maxlen == 240


def test_read_persisted_chaos_mode_no_env_non_addon(monkeypatch):
    """No env var, is_addon=False: returns False without touching the filesystem."""
    from mammamiradio.main import _read_persisted_chaos_mode

    monkeypatch.delenv("MAMMAMIRADIO_CHAOS_MODE", raising=False)
    config = MagicMock(is_addon=False)
    assert _read_persisted_chaos_mode(config) is False


def test_read_persisted_chaos_mode_env_false(monkeypatch):
    """MAMMAMIRADIO_CHAOS_MODE=false returns False without reading any files."""
    from mammamiradio.main import _read_persisted_chaos_mode

    monkeypatch.setenv("MAMMAMIRADIO_CHAOS_MODE", "false")
    config = MagicMock(is_addon=False)
    assert _read_persisted_chaos_mode(config) is False


def test_read_persisted_chaos_mode_env_true(monkeypatch):
    """MAMMAMIRADIO_CHAOS_MODE=true returns True without reading any files."""
    from mammamiradio.main import _read_persisted_chaos_mode

    monkeypatch.setenv("MAMMAMIRADIO_CHAOS_MODE", "true")
    config = MagicMock(is_addon=False)
    assert _read_persisted_chaos_mode(config) is True


def test_read_persisted_chaos_mode_addon_file_missing(monkeypatch, tmp_path):
    """Addon mode with no options.json returns False."""
    from mammamiradio.main import _read_persisted_chaos_mode

    monkeypatch.delenv("MAMMAMIRADIO_CHAOS_MODE", raising=False)
    config = MagicMock(is_addon=True)
    with patch("mammamiradio.main.Path") as mock_path_cls:
        fake_path = MagicMock()
        fake_path.exists.return_value = False
        mock_path_cls.return_value = fake_path
        result = _read_persisted_chaos_mode(config)
    assert result is False


def test_read_persisted_chaos_mode_addon_file_malformed(monkeypatch, tmp_path):
    """Addon mode with malformed options.json returns False instead of raising."""
    from mammamiradio.main import _read_persisted_chaos_mode

    monkeypatch.delenv("MAMMAMIRADIO_CHAOS_MODE", raising=False)
    config = MagicMock(is_addon=True)
    with patch("mammamiradio.main.Path") as mock_path_cls:
        fake_path = MagicMock()
        fake_path.exists.return_value = True
        fake_path.read_text.return_value = "not-json{"
        mock_path_cls.return_value = fake_path
        result = _read_persisted_chaos_mode(config)
    assert result is False


def test_read_persisted_chaos_mode_addon_file_non_object(monkeypatch, tmp_path):
    """Addon mode with non-object options JSON returns False instead of raising."""
    from mammamiradio.main import _read_persisted_chaos_mode

    monkeypatch.delenv("MAMMAMIRADIO_CHAOS_MODE", raising=False)
    config = MagicMock(is_addon=True)
    with patch("mammamiradio.main.Path") as mock_path_cls:
        fake_path = MagicMock()
        fake_path.exists.return_value = True
        fake_path.read_text.return_value = "[]"
        mock_path_cls.return_value = fake_path
        result = _read_persisted_chaos_mode(config)
    assert result is False


def test_read_persisted_chaos_mode_addon_returns_persisted_value(monkeypatch, tmp_path):
    """Addon mode reads chaos_mode_active from options.json when present."""
    import json

    from mammamiradio.main import _read_persisted_chaos_mode

    monkeypatch.delenv("MAMMAMIRADIO_CHAOS_MODE", raising=False)
    config = MagicMock(is_addon=True)
    with patch("mammamiradio.main.Path") as mock_path_cls:
        fake_path = MagicMock()
        fake_path.exists.return_value = True
        fake_path.read_text.return_value = json.dumps({"chaos_mode_active": False})
        mock_path_cls.return_value = fake_path
        result = _read_persisted_chaos_mode(config)
    assert result is False


@pytest.mark.asyncio
async def test_shutdown_with_no_tasks_set():
    """shutdown() handles the case where all module-level task refs are None."""
    import mammamiradio.main as main_mod

    main_mod._producer_task = None
    main_mod._playback_task = None
    main_mod._prewarm_task = None

    for attr in (
        "producer_task",
        "prewarm_task",
        "playback_task",
        "stream_hub",
        "provider_verdict_task",
        "background_tasks",
        "ledger",
        "release_campaign",
    ):
        if hasattr(main_mod.app.state, attr):
            delattr(main_mod.app.state, attr)

    # Should complete without calling asyncio.gather (no tasks to cancel)
    with patch("asyncio.gather", new_callable=AsyncMock) as mock_gather:
        await main_mod.shutdown()

    mock_gather.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_stops_and_clears_ledger():
    """shutdown() stops the provenance ledger and clears it; a second shutdown
    with no ledger is a safe no-op. Covers both arcs of the ledger guard
    deterministically (it must not depend on leftover app.state from prior tests).
    """
    import mammamiradio.main as main_mod

    main_mod._producer_task = None
    main_mod._playback_task = None
    main_mod._prewarm_task = None
    for attr in ("producer_task", "prewarm_task", "playback_task", "stream_hub", "background_tasks"):
        if hasattr(main_mod.app.state, attr):
            delattr(main_mod.app.state, attr)
    main_mod.app.state.provider_verdict_task = None

    fake_ledger = MagicMock()
    main_mod.app.state.ledger = fake_ledger
    with patch("asyncio.gather", new_callable=AsyncMock):
        await main_mod.shutdown()  # ledger present → True arc
    fake_ledger.stop.assert_called_once()
    assert main_mod.app.state.ledger is None

    # ledger already None → False arc, must not raise
    with patch("asyncio.gather", new_callable=AsyncMock):
        await main_mod.shutdown()
    assert main_mod.app.state.ledger is None


def test_fastapi_title_uses_canonical_station_name():
    """The OpenAPI/app title is the canonical station name, sourced from the single constant."""
    from mammamiradio.core.config import DEFAULT_STATION_NAME
    from mammamiradio.main import app

    assert DEFAULT_STATION_NAME == "Mamma Mi Radio"
    assert app.title == DEFAULT_STATION_NAME


def _heading_startup_config(tmp_path: Path) -> MagicMock:
    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"
    mock_config.homeassistant.enabled = False
    mock_config.allow_ytdlp = False
    mock_config.audio.bitrate = 192
    return mock_config


@pytest.mark.asyncio
async def test_startup_clears_heading_when_restore_fetch_raises(tmp_path: Path):
    """A persisted heading whose source re-fetch raises on boot is cleared, not aired:
    startup() returns to auto so the course banner never lies (Scenario 3)."""
    from mammamiradio.core.models import Heading, Track

    mock_config = _heading_startup_config(tmp_path)
    heading = Heading("h1", "classic://italian/80s", "Anni '80", 1.0, "operator")
    tracks = [Track(title="S", artist="A", duration_ms=1, spotify_id="x")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(tracks, None, "")),
        patch(f"{MODULE}.read_persisted_heading", return_value=heading),
        patch(f"{MODULE}.load_explicit_source", side_effect=RuntimeError("yt-dlp down")),
        patch(f"{MODULE}._clear_persisted_heading") as m_clear,
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch(f"{MODULE}.prewarm_first_segment", new_callable=AsyncMock),
    ):
        from mammamiradio.main import app, startup

        await startup()

    m_clear.assert_called_once()
    assert app.state.station_state.heading is None


@pytest.mark.asyncio
async def test_startup_clears_heading_when_restore_yields_no_tracks(tmp_path: Path):
    """A persisted heading whose source re-fetch returns nothing playable is cleared."""
    from mammamiradio.core.models import Heading, Track

    mock_config = _heading_startup_config(tmp_path)
    heading = Heading("h2", "classic://italian/90s", "Anni '90", 1.0, "operator")
    tracks = [Track(title="S", artist="A", duration_ms=1, spotify_id="x")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(tracks, None, "")),
        patch(f"{MODULE}.read_persisted_heading", return_value=heading),
        patch(f"{MODULE}.load_explicit_source", return_value=([], None)),
        patch(f"{MODULE}._clear_persisted_heading") as m_clear,
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch(f"{MODULE}.prewarm_first_segment", new_callable=AsyncMock),
    ):
        from mammamiradio.main import app, startup

        await startup()

    m_clear.assert_called_once()
    assert app.state.station_state.heading is None


def test_clear_persisted_heading_swallows_oserror():
    """_clear_persisted_heading never raises into startup when the unlink fails."""
    from mammamiradio.main import _clear_persisted_heading

    config = MagicMock()
    bad_path = MagicMock()
    bad_path.unlink.side_effect = OSError("read-only filesystem")
    config.cache_dir.__truediv__.return_value = bad_path

    _clear_persisted_heading(config)  # must not raise

    bad_path.unlink.assert_called_once()
