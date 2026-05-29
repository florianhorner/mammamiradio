"""Canonical stream audio format metadata.

Single source of truth for the audio format mammamiradio publishes on `/stream`,
consumed by both the API metadata in `_public_status_payload()` and the response
headers of the `/stream` route so the two cannot drift.

`codec` and `mime_type` are MP3-only constants because there is no codec config
knob today; the schema is shaped so a future codec change updates only this
helper and its tests.
"""

from __future__ import annotations

from typing import TypedDict

from mammamiradio.core.config import StationConfig


class StreamAudioFormat(TypedDict):
    codec: str
    mime_type: str
    bitrate_kbps: int
    sample_rate_hz: int
    channels: int


def stream_audio_metadata(config: StationConfig) -> StreamAudioFormat:
    """Return the canonical audio format the station encodes and serves."""
    return {
        "codec": "mp3",
        "mime_type": "audio/mpeg",
        "bitrate_kbps": config.audio.bitrate,
        "sample_rate_hz": config.audio.sample_rate,
        "channels": config.audio.channels,
    }
