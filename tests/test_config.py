from __future__ import annotations

import os
from pathlib import Path

from fakeitaliradio.config import load_config, AudioSection


def test_load_config_from_radio_toml():
    """Loading radio.toml should produce a valid StationConfig."""
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
    assert len(config.ads.brand_pool) > 0


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


def test_audio_section_defaults():
    """AudioSection dataclass defaults should be sensible."""
    audio = AudioSection()
    assert audio.sample_rate == 48000
    assert audio.channels == 2
    assert audio.bitrate == 192
