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
    from mammamiradio.tts import reset_voice_failures

    reset_voice_failures()
    yield
    reset_voice_failures()
