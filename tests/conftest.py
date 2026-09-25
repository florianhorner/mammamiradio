"""Shared fixtures and environment isolation for all test modules."""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock

import pytest

# core/config.py:37 runs a bare load_dotenv() at import time, which is during
# pytest collection, before any fixture. The workspace .env would leak keys such
# as HA_URL and HA_ENABLED into every test and fail six of them locally while CI
# (no .env) stays green. python-dotenv honours this switch inside load_dotenv();
# it needs python-dotenv >= 1.2 and must be a direct assignment: an inherited
# "0" is a truthy-looking value that python-dotenv treats as "not disabled".
# Keep this at module level. A fixture runs too late, and the spawned HA
# projection worker (home/ha_context.py) inherits whatever this process holds.
# tests/core/test_config_env_overrides.py asserts both the site and the effect.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"


@pytest.fixture
def external_media_installed(monkeypatch):
    """Simulate the optional external-media module (yt-dlp) being importable.

    The default distribution ships without the external-media extra, so ambient
    importability differs between environments (a developer venv with the extra
    vs. the clean CI install). Tests that assert present-path behavior request
    this fixture instead of inheriting whatever the running interpreter has;
    ``monkeypatch.setitem`` restores ``sys.modules`` afterwards, so nothing
    leaks into other tests.
    """
    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = MagicMock()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    return module


@pytest.fixture
def external_media_missing(monkeypatch):
    """Simulate the optional external-media module (yt-dlp) being uninstalled.

    A ``None`` entry in ``sys.modules`` makes every import attempt raise
    ``ImportError``, even in a venv that has the real package — so the
    designed degrade path is testable deterministically everywhere.
    """
    monkeypatch.setitem(sys.modules, "yt_dlp", None)


@pytest.fixture(autouse=True, scope="session")
def _isolate_env():
    """Keep a developer's exported provider credentials out of the session.

    The workspace ``.env`` is already blocked at collection time by the
    ``PYTHON_DOTENV_DISABLED`` assignment at the top of this file; this fixture
    covers keys the developer exported in their shell instead.
    """
    sensitive = [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "AZURE_SPEECH_KEY",
        "AZURE_SPEECH_REGION",
        "ELEVENLABS_API_KEY",
        "HA_TOKEN",
        "STATION_NAME",
        "STATION_THEME",
    ]
    saved = {k: os.environ.pop(k, None) for k in sensitive}
    yield
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v


@pytest.fixture(autouse=True)
def _reset_tts_voice_memoization():
    """Clear runtime voice-failure memoization between tests to prevent
    state leaking across tests that share the same edge voice IDs."""
    from mammamiradio.audio.tts import reset_voice_failures

    reset_voice_failures()
    yield
    reset_voice_failures()


@pytest.fixture(autouse=True)
def _reset_rejected_cache_denylist():
    """Clear the session-scoped rejected-download denylist between tests so
    that a track rejected by one test does not poison selection in the next."""
    from mammamiradio.playlist.downloader import clear_rejected_cache_keys

    clear_rejected_cache_keys()
    yield
    clear_rejected_cache_keys()


@pytest.fixture(autouse=True)
def _reset_loudness_reconcile():
    """Keep the normalizer's module-level loudness-reconcile target out of
    cross-test state: default it off before and after every test, so a test (or a
    full-app lifespan) that enables it can't change another test's audio output."""
    from mammamiradio.audio.normalizer import configure_loudness_reconcile

    configure_loudness_reconcile(None, None)
    yield
    configure_loudness_reconcile(None, None)


@pytest.fixture(autouse=True)
def _reset_broadcast_chain():
    """Keep the normalizer's module-level FM broadcast-chain state out of cross-test
    state: default it OFF before and after every test so a test (or a full-app
    lifespan) that enables it can't colour another test's audio output."""
    from mammamiradio.audio.normalizer import configure_broadcast_chain

    configure_broadcast_chain(False)
    yield
    configure_broadcast_chain(False)


@pytest.fixture(autouse=True)
def _restore_station_env():
    """Restore station-owned env namespaces after each test.

    Production paths can write these settings after a test has deleted an absent
    key through ``monkeypatch``. Pytest did not perform that later write, so its
    undo stack cannot restore the pre-test state. Snapshot before the test and
    restore at teardown to keep each worker isolated.
    """
    prefixes = ("MAMMAMIRADIO_", "JAMENDO_")
    saved = {k: v for k, v in os.environ.items() if k.startswith(prefixes)}
    yield
    for key in [k for k in os.environ if k.startswith(prefixes)]:
        if key not in saved:
            del os.environ[key]
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _reset_ha_projection_executor():
    """Keep the HA projection worker pool out of cross-test state.

    The pool is a module-level singleton holding a real spawned process. Without
    this, one test that breaks its worker cascades BrokenProcessPool into every
    later test that reaches a projection, and the order is randomised. Retiring
    after each test also makes the worker exit cleanly, which is what lets it
    write its coverage data (see concurrency/parallel in pyproject.toml).

    A test that never touched the pool leaves the global at None, so this is a
    no-op for all but the handful of tests that project for real.

    Teardown-only on purpose. Retiring on setup too would forbid any two tests
    from sharing a worker, paying a full cold spawn each time for no isolation
    the teardown does not already give.
    """
    import mammamiradio.home.ha_context as ha_context

    yield
    # shutdown(wait=True): the worker must finish exiting before the next test
    # runs, because coverage data for the child is written during that exit.
    # The module's own retire uses wait=False (it must never block the audio
    # loop); here the race would silently cost coverage and trip the ratchet.
    executor = ha_context._ha_projection_executor
    if executor is not None and ha_context._retire_ha_projection_executor(executor):
        executor.shutdown(wait=True)


@pytest.fixture(autouse=True)
def _disable_runway_governor_by_default():
    """Keep legacy producer tests focused unless they explicitly opt into runway gating."""
    from mammamiradio.scheduling import producer

    old_runway_floor = producer.RUNWAY_FLOOR_SECONDS
    producer.RUNWAY_FLOOR_SECONDS = 0
    yield
    producer.RUNWAY_FLOOR_SECONDS = old_runway_floor
