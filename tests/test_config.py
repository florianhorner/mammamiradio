from __future__ import annotations

from pathlib import Path

import pytest

from fakeitaliradio.config import load_config, AudioSection


def test_load_config_from_radio_toml(monkeypatch):
    """Loading radio.toml should produce a valid StationConfig."""
    monkeypatch.setenv("FAKEITALIRADIO_HOST", "127.0.0.1")
    monkeypatch.setenv("FAKEITALIRADIO_PORT", "8000")
    # Ensure we load from the project root radio.toml
    toml_path = Path(__file__).parent.parent / "radio.toml"
    config = load_config(str(toml_path))

    assert config.station.name == "Radio Ital\xec"
    assert config.station.language == "it"
    assert config.station.bitrate == 192
    assert config.pacing.songs_between_banter == 2
    assert config.pacing.songs_between_ads == 4
    assert len(config.hosts) == 2
    assert config.hosts[0].name == "Marco"
    assert config.hosts[1].name == "Giulia"
    assert len(config.ads.brands) > 0
    assert len(config.ads.voices) > 0


def test_audio_section_loaded(monkeypatch):
    """The [audio] section should be loaded with correct defaults."""
    monkeypatch.setenv("FAKEITALIRADIO_HOST", "127.0.0.1")
    monkeypatch.setenv("FAKEITALIRADIO_PORT", "8000")
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


def test_audio_section_defaults():
    """AudioSection dataclass defaults should be sensible."""
    audio = AudioSection()
    assert audio.sample_rate == 48000
    assert audio.channels == 2
    assert audio.bitrate == 192


def test_unknown_section_keys_raise_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKEITALIRADIO_HOST", "127.0.0.1")
    monkeypatch.setenv("FAKEITALIRADIO_PORT", "8000")
    config_path = tmp_path / "invalid.toml"
    config_path.write_text(
        """
[station]
name = "Test"
language = "it"
extra = "unexpected"

[[hosts]]
name = "Marco"
voice = "it-IT-DiegoNeural"
style = "warm"
""".strip()
    )

    with pytest.raises(ValueError, match=r"Unknown keys in \[station\]: extra"):
        load_config(str(config_path))


def test_missing_hosts_raise_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKEITALIRADIO_HOST", "127.0.0.1")
    monkeypatch.setenv("FAKEITALIRADIO_PORT", "8000")
    config_path = tmp_path / "missing-hosts.toml"
    config_path.write_text(
        """
[station]
name = "Test"
language = "it"
""".strip()
    )

    with pytest.raises(ValueError, match="No hosts configured"):
        load_config(str(config_path))
