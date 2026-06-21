"""Shared fixtures for the Mamma Mi Radio HA custom-integration tests.

These run under `pytest-homeassistant-custom-component` (a real HA test
harness), NOT the app's `pytest tests/`. The app suite's `testpaths = ["tests"]`
never collects this tree; a dedicated CI job installs PHACC and runs it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_SAMPLE_DIR = Path(__file__).resolve().parents[1] / "docs" / "integrations" / "sample-payloads"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ so HA can discover the integration (mandatory)."""
    yield


def load_payload(name: str) -> dict:
    """Load a sample now-playing payload by name (e.g. 'music', 'stopped')."""
    return json.loads((_SAMPLE_DIR / f"{name}.json").read_text(encoding="utf-8"))
