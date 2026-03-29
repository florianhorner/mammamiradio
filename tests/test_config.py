from __future__ import annotations

import os
from pathlib import Path

from fakeitaliradio.config import load_config, AudioSection, runtime_json


def test_load_config_from_radio_toml():
    """Loading radio.toml should produce a valid StationConfig."""
    # Ensure we load from the project root radio.toml
    toml_path = Path(__file__).parent.parent / "radio.toml"
    config = load_config(str(toml_path))

    assert config.station.name == "Radio Ital\xec"
    assert config.station.language == "it"
    assert config.pacing.songs_between_banter == 2
    assert config.pacing.songs_between_ads == 4
    assert len(config.hosts) == 2
    assert config.hosts[0].name == "Marco"
    assert config.hosts[1].name == "Giulia"
    assert len(config.ads.brands) > 0
    assert len(config.ads.voices) > 0


def test_audio_section_loaded():
    """The [audio] section should be loaded with correct defaults."""
    toml_path = Path(__file__).parent.parent / "radio.toml"
    config = load_config(str(toml_path))

    assert config.audio.sample_rate == 48000
    assert config.audio.channels == 2
    assert config.audio.bitrate == 192
    assert config.audio.spotify_bitrate == 320
    assert config.audio.fifo_path == "/tmp/fakeitaliradio.pcm"
    assert "go-librespot" in config.audio.go_librespot_bin
    assert config.audio.go_librespot_port == 3678
    assert config.audio.claude_model == "claude-haiku-4-5-20251001"


def test_homeassistant_section_loaded():
    """The [homeassistant] section should survive config loading."""
    toml_path = Path(__file__).parent.parent / "radio.toml"
    config = load_config(str(toml_path))

    assert config.homeassistant.enabled is True
    assert config.homeassistant.url == "https://ha.horner.io"
    assert config.homeassistant.poll_interval == 60


def test_audio_section_defaults():
    """AudioSection dataclass defaults should be sensible."""
    audio = AudioSection()
    assert audio.sample_rate == 48000
    assert audio.channels == 2
    assert audio.bitrate == 192


def test_loads_admin_env(monkeypatch):
    toml_path = Path(__file__).parent.parent / "radio.toml"
    monkeypatch.setenv("ADMIN_USERNAME", "radio")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("ADMIN_TOKEN", "token123")

    config = load_config(str(toml_path))

    assert config.admin_username == "radio"
    assert config.admin_password == "secret"
    assert config.admin_token == "token123"


def test_audio_bitrate_is_canonical():
    """audio.bitrate is the single source of truth for bitrate."""
    toml_path = Path(__file__).parent.parent / "radio.toml"
    config = load_config(str(toml_path))
    assert config.audio.bitrate == 192
    assert not hasattr(config.station, "bitrate")


def test_runtime_json_keys():
    toml_path = Path(__file__).parent.parent / "radio.toml"
    config = load_config(str(toml_path))
    result = runtime_json(config)
    assert "bind_host" in result
    assert "fifo_path" in result
    assert "go_librespot_bin" in result


def test_non_local_bind_requires_admin_auth(monkeypatch):
    toml_path = Path(__file__).parent.parent / "radio.toml"
    monkeypatch.setenv("FAKEITALIRADIO_BIND_HOST", "0.0.0.0")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)

    try:
        load_config(str(toml_path))
    except ValueError as exc:
        assert "Non-local bind requires ADMIN_PASSWORD or ADMIN_TOKEN" in str(exc)
    else:
        raise AssertionError("Expected config validation to fail for non-local bind without auth")
