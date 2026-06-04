"""Tests for _normalize_tts_voices degradation branches — the operator-honesty
guard that flips a misconfigured cloud-TTS voice to a safe Edge fallback at load
time (so synthesis never hits a runtime engine it can't satisfy) and records the
downgrade on config.tts_degraded_voices."""

from __future__ import annotations

from copy import deepcopy

from mammamiradio.core.config import _normalize_tts_voices, load_config

TOML_PATH = "radio.toml"


def _base():
    """Load the real config, then neutralize hosts/ads/sweeper so each test
    drives exactly one normalization branch."""
    config = load_config(TOML_PATH)
    config.ads.voices = []
    config.sonic_brand.sweeper_voice = ""
    config.sonic_brand.sweeper_engine = "edge"
    for host in config.hosts:
        host.engine = "edge"
        host.voice = ""
        host.edge_fallback_voice = ""
    return config


def _ad_voice(**overrides):
    """Deep-copy a real ad voice shape and apply overrides."""
    voice = deepcopy(load_config(TOML_PATH).ads.voices[0])
    for key, value in overrides.items():
        setattr(voice, key, value)
    return voice


def test_host_azure_without_voice_flips_to_edge_fallback():
    config = _base()
    host = config.hosts[0]
    host.name = "AzureHost"
    host.engine = "azure"
    host.voice = ""
    host.edge_fallback_voice = "it-IT-DiegoNeural"

    _normalize_tts_voices(config)

    assert host.engine == "edge"
    assert host.voice == "it-IT-DiegoNeural"
    assert "AzureHost" in config.tts_degraded_voices


def test_host_openai_with_non_openai_voice_flips_to_edge():
    config = _base()
    host = config.hosts[0]
    host.name = "OpenAIHost"
    host.engine = "openai"
    host.voice = "it-IT-DiegoNeural"  # not an OpenAI voice id
    host.edge_fallback_voice = "it-IT-IsabellaNeural"

    _normalize_tts_voices(config)

    assert host.engine == "edge"
    assert host.voice == "it-IT-IsabellaNeural"
    assert "OpenAIHost" in config.tts_degraded_voices


def test_host_azure_unknown_local_voice_stays_azure_with_fallback():
    config = _base()
    host = config.hosts[0]
    host.name = "AzureExoticHost"
    host.engine = "azure"
    host.voice = "it-IT-FakeAzureNeural"  # it-IT- prefix, outside curated catalog
    host.edge_fallback_voice = "it-IT-DiegoNeural"

    _normalize_tts_voices(config)

    # Azure is honored at runtime; only the edge fallback is normalized.
    assert host.engine == "azure"
    assert host.voice == "it-IT-FakeAzureNeural"
    assert host.edge_fallback_voice == "it-IT-DiegoNeural"
    assert "AzureExoticHost" not in config.tts_degraded_voices


def test_host_cloud_engine_without_fallback_defaults_fallback_voice():
    config = _base()
    host = config.hosts[0]
    host.name = "ElevenHost"
    host.engine = "elevenlabs"
    host.voice = ""
    host.edge_fallback_voice = ""  # no fallback configured → defaulted

    _normalize_tts_voices(config)

    assert host.engine == "edge"
    assert host.voice  # defaulted to a concrete edge voice, never empty
    assert "ElevenHost" in config.tts_degraded_voices


def test_host_openai_without_voice_flips_to_edge():
    config = _base()
    host = config.hosts[0]
    host.name = "OpenAINoVoice"
    host.engine = "openai"
    host.voice = ""
    host.edge_fallback_voice = "it-IT-DiegoNeural"

    _normalize_tts_voices(config)

    assert host.engine == "edge"
    assert host.voice == "it-IT-DiegoNeural"
    assert "OpenAINoVoice" in config.tts_degraded_voices


def test_ad_voice_elevenlabs_without_voice_flips_to_edge():
    config = _base()
    voice = _ad_voice(name="ElevenAd", engine="elevenlabs", voice="", edge_fallback_voice="it-IT-DiegoNeural")
    config.ads.voices = [voice]

    _normalize_tts_voices(config)

    assert voice.engine == "edge"
    assert voice.voice == "it-IT-DiegoNeural"
    assert "ElevenAd" in config.tts_degraded_voices


def test_ad_voice_openai_without_voice_flips_to_edge():
    config = _base()
    voice = _ad_voice(name="OpenAINoVoiceAd", engine="openai", voice="", edge_fallback_voice="it-IT-DiegoNeural")
    config.ads.voices = [voice]

    _normalize_tts_voices(config)

    assert voice.engine == "edge"
    assert voice.voice == "it-IT-DiegoNeural"
    assert "OpenAINoVoiceAd" in config.tts_degraded_voices


def test_ad_voice_openai_with_non_openai_voice_flips_to_edge():
    config = _base()
    voice = _ad_voice(
        name="OpenAIWrongAd", engine="openai", voice="it-IT-DiegoNeural", edge_fallback_voice="it-IT-IsabellaNeural"
    )
    config.ads.voices = [voice]

    _normalize_tts_voices(config)

    assert voice.engine == "edge"
    assert voice.voice == "it-IT-IsabellaNeural"
    assert "OpenAIWrongAd" in config.tts_degraded_voices


def test_ad_voice_azure_without_voice_flips_to_edge():
    config = _base()
    voice = _ad_voice(name="AzureNoVoiceAd", engine="azure", voice="", edge_fallback_voice="it-IT-DiegoNeural")
    config.ads.voices = [voice]

    _normalize_tts_voices(config)

    assert voice.engine == "edge"
    assert voice.voice == "it-IT-DiegoNeural"
    assert "AzureNoVoiceAd" in config.tts_degraded_voices


def test_ad_voice_azure_unknown_local_voice_stays_azure():
    config = _base()
    voice = _ad_voice(
        name="AzureExoticAd", engine="azure", voice="it-IT-FakeAzureNeural", edge_fallback_voice="it-IT-DiegoNeural"
    )
    config.ads.voices = [voice]

    _normalize_tts_voices(config)

    assert voice.engine == "azure"
    assert voice.voice == "it-IT-FakeAzureNeural"
    assert "AzureExoticAd" not in config.tts_degraded_voices


def test_sweeper_azure_voice_sets_edge_fallback_default():
    config = _base()
    sb = config.sonic_brand
    sb.sweeper_engine = "azure"
    sb.sweeper_voice = "it-IT-Isabella:DragonHDLatestNeural"
    sb.sweeper_edge_fallback_voice = ""  # defaulted by _cloud_fallback

    _normalize_tts_voices(config)

    assert sb.sweeper_engine == "azure"
    assert sb.sweeper_voice == "it-IT-Isabella:DragonHDLatestNeural"
    assert sb.sweeper_edge_fallback_voice  # concrete edge fallback, never empty


def test_sweeper_edge_with_unknown_voice_normalizes_to_fallback():
    config = _base()
    sb = config.sonic_brand
    sb.sweeper_engine = "edge"
    sb.sweeper_voice = "it-IT-FakeUnknownNeural"  # not a known edge voice
    sb.sweeper_edge_fallback_voice = "it-IT-DiegoNeural"

    _normalize_tts_voices(config)

    assert sb.sweeper_engine == "edge"
    assert sb.sweeper_voice == "it-IT-DiegoNeural"


def test_sweeper_cloud_engine_without_voice_resets_to_edge():
    config = _base()
    sb = config.sonic_brand
    sb.sweeper_engine = "azure"
    sb.sweeper_voice = ""

    _normalize_tts_voices(config)

    assert sb.sweeper_engine == "edge"


def test_sweeper_openai_with_non_openai_voice_flips_to_edge():
    config = _base()
    sb = config.sonic_brand
    sb.sweeper_engine = "openai"
    sb.sweeper_voice = "it-IT-DiegoNeural"  # not an OpenAI voice id
    sb.sweeper_edge_fallback_voice = "it-IT-IsabellaNeural"

    _normalize_tts_voices(config)

    assert sb.sweeper_engine == "edge"
    assert sb.sweeper_voice == "it-IT-IsabellaNeural"
    assert "sonic_brand.sweeper_voice" in config.tts_degraded_voices
