"""Shared fixtures and environment isolation for all test modules."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_env():
    """Prevent real API keys from leaking into tests via load_dotenv."""
    sensitive = [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "AZURE_SPEECH_KEY",
        "AZURE_SPEECH_REGION",
        "ELEVENLABS_API_KEY",
        "HA_TOKEN",
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
