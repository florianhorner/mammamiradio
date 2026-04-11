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
