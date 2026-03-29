from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class SegmentType(Enum):
    MUSIC = "music"
    BANTER = "banter"
    AD = "ad"


@dataclass
class Track:
    title: str
    artist: str
    duration_ms: int
    spotify_id: str
    local_path: Path | None = None

    @property
    def cache_key(self) -> str:
        raw = f"{self.artist} {self.title}".lower()
        return re.sub(r"[^a-z0-9]+", "_", raw).strip("_")[:80]

    @property
    def display(self) -> str:
        return f"{self.artist} – {self.title}"


@dataclass
class HostPersonality:
    name: str
    voice: str
    style: str


@dataclass
class AdBrand:
    name: str
    tagline: str
    category: str = "general"
    recurring: bool = True


@dataclass
class AdVoice:
    name: str
    voice: str  # edge-tts voice ID
    style: str  # character description for the prompt


@dataclass
class AdPart:
    type: str  # "voice", "sfx", "pause"
    text: str = ""
    voice: str = ""
    sfx: str = ""  # "chime", "sweep", "ding", "cash_register", "whoosh"
    duration: float = 0.0


@dataclass
class AdScript:
    brand: str
    parts: list[AdPart] = field(default_factory=list)
    summary: str = ""  # 1-sentence for history/cross-ref
    mood: str = ""  # music bed mood: "dramatic", "lounge", "upbeat", "mysterious", "epic"


@dataclass
class AdHistoryEntry:
    brand: str
    summary: str
    timestamp: float = 0.0


@dataclass
class Segment:
    type: SegmentType
    path: Path
    duration_sec: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass
class SegmentLogEntry:
    type: str
    label: str
    timestamp: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass
class StationState:
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
    # What the listener is hearing RIGHT NOW
    now_streaming: dict = field(default_factory=dict)
    # Stream-side log (when segments actually play, not when produced)
    stream_log: list[SegmentLogEntry] = field(default_factory=list)
    # Home Assistant context (natural language summary of home state)
    ha_context: str = ""

    def _log(self, seg_type: str, label: str, metadata: dict | None = None) -> None:
        self.segment_log.append(SegmentLogEntry(
            type=seg_type, label=label,
            timestamp=time.time(), metadata=metadata or {},
        ))
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
        self.stream_log.append(SegmentLogEntry(
            type=seg_type, label=label,
            timestamp=time.time(), metadata=segment.metadata,
        ))
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
        self.played_tracks.append(track)
        self.current_track = track
        self.songs_since_banter += 1
        self.songs_since_ad += 1
        self.segments_produced += 1
        self._log("music", track.display)

    def after_banter(self) -> None:
        self.songs_since_banter = 0
        self.segments_produced += 1
        self._log("banter", "Host banter")

    def record_ad_spot(self, brand: str, summary: str = "") -> None:
        """Record a single ad spot in history (called per-spot within a break)."""
        self.ad_history.append(AdHistoryEntry(
            brand=brand, summary=summary, timestamp=time.time(),
        ))
        if len(self.ad_history) > 20:
            self.ad_history = self.ad_history[-20:]

    def after_ad(self, brands: list[str] | None = None) -> None:
        """Mark one full ad break as produced (called once per break, not per-spot)."""
        self.songs_since_ad = 0
        self.segments_produced += 1
        label = ", ".join(brands) if brands else "Ad break"
        self._log("ad", f"Ad: {label}")

    def add_joke(self, joke: str) -> None:
        self.running_jokes.append(joke)
        self.running_jokes = self.running_jokes[-5:]
