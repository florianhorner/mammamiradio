"""Core data models shared across playback, scripting, and streaming."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import ClassVar


class SegmentType(Enum):
    """Kinds of segments that can appear on the station timeline."""

    MUSIC = "music"
    BANTER = "banter"
    AD = "ad"


@dataclass
class Track:
    """A playable track sourced from Spotify, cache, or local files."""

    title: str
    artist: str
    duration_ms: int
    spotify_id: str
    local_path: Path | None = None

    @property
    def cache_key(self) -> str:
        """Stable filesystem-friendly key used for caching fallback audio."""
        raw = f"{self.artist} {self.title}".lower()
        return re.sub(r"[^a-z0-9]+", "_", raw).strip("_")[:80]

    @property
    def display(self) -> str:
        """Human-readable label used in logs and APIs."""
        return f"{self.artist} – {self.title}"


@dataclass
class PlaylistSource:
    """The user-visible source backing the currently loaded playlist."""

    kind: str
    source_id: str = ""
    url: str = ""
    label: str = ""
    track_count: int = 0
    selected_at: float = 0.0


@dataclass
class PersonalityAxes:
    """Tunable personality dimensions that shape how a host delivers dialogue.

    Each axis is 0-100.  The default (50) produces neutral behaviour that
    matches whatever the freeform ``style`` string already describes.
    """

    energy: int = 50
    chaos: int = 50
    warmth: int = 50
    verbosity: int = 50
    nostalgia: int = 50

    AXIS_NAMES: ClassVar[list[str]] = ["energy", "chaos", "warmth", "verbosity", "nostalgia"]

    def to_dict(self) -> dict[str, int]:
        return {a: getattr(self, a) for a in self.AXIS_NAMES}

    @classmethod
    def from_dict(cls, d: dict[str, int]) -> PersonalityAxes:
        kwargs = {k: max(0, min(100, int(v))) for k, v in d.items() if k in cls.AXIS_NAMES}
        return cls(**kwargs)


@dataclass
class HostPersonality:
    """Prompt and TTS inputs that define an on-air host persona."""

    name: str
    voice: str
    style: str
    personality: PersonalityAxes = field(default_factory=PersonalityAxes)


@dataclass
class AdBrand:
    """A fictional advertiser that can recur across breaks."""

    name: str
    tagline: str
    category: str = "general"
    recurring: bool = True


@dataclass
class AdVoice:
    """A non-host voice used to perform commercial copy."""

    name: str
    voice: str  # edge-tts voice ID
    style: str  # character description for the prompt


@dataclass
class AdPart:
    """One structured unit inside an ad script: voice, SFX, or silence."""

    type: str  # "voice", "sfx", "pause"
    text: str = ""
    voice: str = ""
    sfx: str = ""  # "chime", "sweep", "ding", "cash_register", "whoosh"
    duration: float = 0.0


@dataclass
class AdScript:
    """Structured ad script returned by the LLM before audio synthesis."""

    brand: str
    parts: list[AdPart] = field(default_factory=list)
    summary: str = ""  # 1-sentence for history/cross-ref
    mood: str = ""  # music bed mood: "dramatic", "lounge", "upbeat", "mysterious", "epic"


@dataclass
class AdHistoryEntry:
    """Minimal history item used to build cross-ad campaign callbacks."""

    brand: str
    summary: str
    timestamp: float = 0.0


@dataclass
class Segment:
    """A rendered audio file queued for live playback."""

    type: SegmentType
    path: Path
    duration_sec: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass
class SegmentLogEntry:
    """Compact log event for produced or streamed segments."""

    type: str
    label: str
    timestamp: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass
class StationState:
    """Mutable in-memory state shared by producer and streamer tasks."""

    playlist: list[Track] = field(default_factory=list)
    played_tracks: list[Track] = field(default_factory=list)
    songs_since_banter: int = 0
    songs_since_ad: int = 0
    running_jokes: list[str] = field(default_factory=list)
    current_track: Track | None = None
    segments_produced: int = 0
    failed_segments: int = 0
    segment_log: list[SegmentLogEntry] = field(default_factory=list)
    # Last banter/ad scripts for display
    last_banter_script: list[dict] = field(default_factory=list)
    last_ad_script: dict = field(default_factory=dict)
    ad_history: list[AdHistoryEntry] = field(default_factory=list)
    spotify_connected: bool = False
    playlist_source: PlaylistSource | None = None
    startup_source_error: str = ""
    # What the listener is hearing RIGHT NOW
    now_streaming: dict = field(default_factory=dict)
    # Stream-side log (when segments actually play, not when produced)
    stream_log: list[SegmentLogEntry] = field(default_factory=list)
    # Home Assistant context (natural language summary of home state)
    ha_context: str = ""
    # Force-trigger: producer will use this type instead of scheduler for the next segment
    force_next: SegmentType | None = None
    # Consumption metrics
    api_calls: int = 0
    api_input_tokens: int = 0
    api_output_tokens: int = 0
    tts_characters: int = 0

    def _log(self, seg_type: str, label: str, metadata: dict | None = None) -> None:
        """Append a bounded producer-side log entry."""
        self.segment_log.append(
            SegmentLogEntry(
                type=seg_type,
                label=label,
                timestamp=time.time(),
                metadata=metadata or {},
            )
        )
        if len(self.segment_log) > 50:
            self.segment_log = self.segment_log[-50:]

    def on_stream_segment(self, segment: Segment) -> None:
        """Called by the streamer when it starts sending a segment to the listener."""
        seg_type = segment.type.value
        label = segment.metadata.get("title", segment.metadata.get("brand", seg_type))
        self.now_streaming = {
            "type": seg_type,
            "label": label,
            "started": time.time(),
            "metadata": segment.metadata,
        }
        self.stream_log.append(
            SegmentLogEntry(
                type=seg_type,
                label=label,
                timestamp=time.time(),
                metadata=segment.metadata,
            )
        )
        if len(self.stream_log) > 50:
            self.stream_log = self.stream_log[-50:]

    def reserve_next_track(self) -> Track:
        """Rotate the upcoming playlist order when a music segment is queued."""
        if not self.playlist:
            raise RuntimeError("Playlist is empty")
        track = self.playlist.pop(0)
        self.playlist.append(track)
        return track

    def after_music(self, track: Track) -> None:
        """Advance state after successfully queuing a music segment."""
        self.played_tracks.append(track)
        self.current_track = track
        self.songs_since_banter += 1
        self.songs_since_ad += 1
        self.segments_produced += 1
        self._log("music", track.display)

    def after_banter(self) -> None:
        """Advance counters after successfully queuing host banter."""
        self.songs_since_banter = 0
        self.segments_produced += 1
        self._log("banter", "Host banter")

    def record_ad_spot(self, brand: str, summary: str = "") -> None:
        """Record a single ad spot in history (called per-spot within a break)."""
        self.ad_history.append(
            AdHistoryEntry(
                brand=brand,
                summary=summary,
                timestamp=time.time(),
            )
        )
        if len(self.ad_history) > 20:
            self.ad_history = self.ad_history[-20:]

    def after_ad(self, brands: list[str] | None = None) -> None:
        """Mark one full ad break as produced (called once per break, not per-spot)."""
        self.songs_since_ad = 0
        self.segments_produced += 1
        label = ", ".join(brands) if brands else "Ad break"
        self._log("ad", f"Ad: {label}")

    def add_joke(self, joke: str) -> None:
        """Keep a short rolling buffer of running jokes for prompt callbacks."""
        self.running_jokes.append(joke)
        self.running_jokes = self.running_jokes[-5:]
