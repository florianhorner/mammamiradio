"""Unit tests for streamer._tts_provider_status — the operator-honesty surface
that tells the Engine Room whether premium TTS voices are actually working or
silently falling back to Edge."""

from __future__ import annotations

from mammamiradio.core.config import load_config
from mammamiradio.core.models import StationState
from mammamiradio.web.streamer import _tts_provider_status

TOML_PATH = "radio.toml"


def _status(host_engines, *, openai=False, azure=False, eleven=False):
    """Build a status dict for the given host engine mix and key availability.

    radio.toml ships 2 hosts; we drive the engine set through their `engine`
    fields and neutralize the ad voices + sweeper so the test controls exactly
    which cloud engines are in play.
    """
    config = load_config(TOML_PATH)
    config.ads.voices = []
    config.sonic_brand.sweeper_voice = ""
    for i, host in enumerate(config.hosts):
        host.engine = host_engines[i % len(host_engines)]
    config.openai_api_key = "openai-key" if openai else ""
    config.azure_speech_key = "azure-key" if azure else ""
    config.azure_speech_region = "westeurope" if azure else ""
    config.elevenlabs_api_key = "eleven-key" if eleven else ""
    return _tts_provider_status(config, StationState())


def test_single_cloud_engine_with_key_is_primary():
    status = _status(["azure"], azure=True)
    assert status["primary_provider"] == "azure"
    assert status["current_provider"] == "azure"
    assert status["fallback_active"] is False


def test_single_cloud_engine_missing_key_falls_back_to_edge():
    status = _status(["azure"], azure=False)
    assert status["current_provider"] == "edge"
    assert status["fallback_active"] is True
    assert "key missing" in status["switch_reason"].lower()


def test_mixed_cloud_engines_all_keyed_reports_mixed_tts():
    status = _status(["azure", "elevenlabs"], azure=True, eleven=True)
    assert status["primary_provider"] == "mixed_tts"
    assert status["current_provider"] == "mixed_tts"
    assert status["fallback_active"] is False


def test_mixed_cloud_engines_partial_keys_flags_partial_fallback():
    status = _status(["azure", "elevenlabs"], azure=True, eleven=False)
    assert status["primary_provider"] == "mixed_tts"
    assert status["current_provider"] == "mixed_tts"
    assert status["fallback_active"] is True
    assert "falling back to edge" in status["switch_reason"].lower()


def test_all_edge_hosts_report_edge_no_fallback():
    status = _status(["edge"])
    assert status["primary_provider"] == "edge"
    assert status["current_provider"] == "edge"
    assert status["fallback_active"] is False


def test_openai_tts_without_registry_model_falls_back_to_edge():
    """OpenAI TTS needs a registry-selected speech model, not just a key. Without
    it, provider checks report model_routing_unavailable — runtime status must
    agree and show Edge fallback instead of 'openai TTS configured'."""
    config = load_config(TOML_PATH)
    config.ads.voices = []
    config.sonic_brand.sweeper_voice = ""
    for host in config.hosts:
        host.engine = "openai"
    config.openai_api_key = "openai-key"
    config.models.tts_models = {}  # registry TTS route unavailable (legacy/broken registry)
    status = _tts_provider_status(config, StationState())
    assert status["current_provider"] == "edge"
    assert status["fallback_active"] is True


def test_runtime_fallback_overrides_configured_mixed_tts():
    """Configured keys are not a health proof once a live render falls back."""
    config = load_config(TOML_PATH)
    state = StationState()
    for host in config.hosts:
        host.engine = "edge"
    config.ads.voices = []
    config.sonic_brand.sweeper_voice = ""
    config.openai_api_key = "openai-key"
    config.azure_speech_key = "azure-key"
    config.azure_speech_region = "westeurope"
    config.elevenlabs_api_key = "eleven-key"
    config.hosts[0].engine = "elevenlabs"
    config.hosts[1].engine = "azure"

    state.update_runtime_provider(
        "tts_provider",
        current_provider="edge",
        primary_provider="mixed_tts",
        fallback_active=True,
        reason="elevenlabs:missing_credentials",
    )

    status = _tts_provider_status(config, state)

    assert status["primary_provider"] == "mixed_tts"
    assert status["current_provider"] == "edge"
    assert status["fallback_active"] is True
    assert status["switch_reason"] == "elevenlabs:missing_credentials"
