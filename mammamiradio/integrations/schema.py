"""TypedDicts for the v1 now-playing integration contract.

These shapes are part of the public contract — additive within ``v1.*``,
breaking changes ship at ``/api/integrations/v2/``. See the **Versioning
policy** in ``docs/integrations/README.md`` for the written-in-stone rules.
"""

from __future__ import annotations

from typing import Literal, TypedDict

SegmentClass = Literal["music", "voice", "interstitial", "unavailable"]
SessionState = Literal["live", "stopped", "empty_queue"]


class HostEntry(TypedDict, total=False):
    engine_host: str
    display_name: str
    description: str


class StationBlock(TypedDict, total=False):
    name: str
    frequency: str
    theme: str
    hosts: list[HostEntry]


class AudioFormat(TypedDict):
    codec: str
    mime_type: str
    bitrate_kbps: int
    sample_rate_hz: int
    channels: int


class StreamBlock(TypedDict, total=False):
    relative_url: str
    absolute_url: str
    audio_format: AudioFormat


class NowPlayingBlock(TypedDict, total=False):
    segment_class: SegmentClass
    segment_type: str
    title: str | None
    started_at: float | None
    duration_estimate_sec: float | None
    artist: str | None
    artwork: str | None
    album: str | None
    year: int | None
    external_ids: dict[str, str]
    host: str | None
    context: dict


class UpNextItem(TypedDict, total=False):
    segment_class: SegmentClass
    segment_type: str
    title: str
    predicted: bool


class NowPlayingResponse(TypedDict, total=False):
    schema_version: str
    station: StationBlock
    stream: StreamBlock
    now_playing: NowPlayingBlock | None
    up_next: list[UpNextItem]
    session_state: SessionState
    changed_at: float
