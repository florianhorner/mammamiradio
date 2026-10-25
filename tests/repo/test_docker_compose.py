"""docker-compose.yml must forward the speech keys the shipped cast can use."""

from __future__ import annotations

from pathlib import Path

import yaml

from mammamiradio.core.config import load_config
from mammamiradio.core.setup_status import cast_cloud_speech_engines

ROOT = Path(__file__).resolve().parents[2]

# The env vars each cloud speech engine reads at render time (audio/tts.py).
SPEECH_KEYS = {
    "elevenlabs": ("ELEVENLABS_API_KEY",),
    "azure": ("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION"),
    "openai": ("OPENAI_API_KEY",),
}


def _forwarded_names() -> set[str]:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    environment = compose["services"]["mammamiradio"]["environment"]
    return {entry.split("=", 1)[0] for entry in environment}


def test_compose_forwards_every_speech_key_the_shipped_cast_uses():
    """A key Docker never passes leaves those voices on Edge for every Docker install."""
    engines = cast_cloud_speech_engines(load_config(str(ROOT / "radio.toml")))
    forwarded = _forwarded_names()

    assert engines, "the shipped cast should use at least one premium voice for this guard to mean anything"
    missing = [key for engine in engines for key in SPEECH_KEYS[engine] if key not in forwarded]
    assert not missing, f"docker-compose.yml does not forward {missing}"
