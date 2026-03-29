from __future__ import annotations

import re
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
class Segment:
    type: SegmentType
    path: Path
    duration_sec: float = 0.0
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

    def after_music(self, track: Track) -> None:
        self.played_tracks.append(track)
        self.current_track = track
        self.songs_since_banter += 1
        self.songs_since_ad += 1
        self.segments_produced += 1

    def after_banter(self) -> None:
        self.songs_since_banter = 0
        self.segments_produced += 1

    def after_ad(self) -> None:
        self.songs_since_ad = 0
        self.segments_produced += 1

    def add_joke(self, joke: str) -> None:
        self.running_jokes.append(joke)
        self.running_jokes = self.running_jokes[-5:]
