"""Unit tests for the canonical stream audio format helper."""

from __future__ import annotations

from pathlib import Path

from mammamiradio.audio.stream_format import stream_audio_metadata
from mammamiradio.core.config import load_config

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def test_stream_audio_metadata_default_config():
    """Default config yields the documented MP3 contract."""
    config = load_config(TOML_PATH)
    meta = stream_audio_metadata(config)
    assert meta == {
        "codec": "mp3",
        "mime_type": "audio/mpeg",
        "bitrate_kbps": 192,
        "sample_rate_hz": 48000,
        "channels": 2,
    }


def test_stream_audio_metadata_reads_every_config_field():
    """Mutating bitrate, sample_rate, and channels all propagate to the payload."""
    config = load_config(TOML_PATH)
    config.audio.bitrate = 96
    config.audio.sample_rate = 44100
    config.audio.channels = 1
    meta = stream_audio_metadata(config)
    assert meta["bitrate_kbps"] == 96
    assert meta["sample_rate_hz"] == 44100
    assert meta["channels"] == 1
    # MP3-only constants stay pinned.
    assert meta["codec"] == "mp3"
    assert meta["mime_type"] == "audio/mpeg"
