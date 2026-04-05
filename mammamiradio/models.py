"""Core data models shared across playback, scripting, and streaming."""

from __future__ import annotations

import math
import random
import re
import time
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from pathlib import Path
from typing import ClassVar


class SegmentType(Enum):
    """Kinds of segments that can appear on the station timeline."""

    MUSIC = "music"
    BANTER = "banter"
    AD = "ad"
    NEWS_FLASH = "news_flash"
    STATION_ID = "station_id"
    SWEEPER = "sweeper"
    TIME_CHECK = "time_check"


@dataclass
class Track:
    """A playable track sourced from Spotify, cache, or local files."""

    title: str
    artist: str
    duration_ms: int
    spotify_id: str = ""
    youtube_id: str = ""
    local_path: Path | None = None
    position_ms: int = 0
    album_art: str = ""
    album: str = ""
    explicit: bool = False
    popularity: int = 0

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
    engine: str = "edge"  # "edge" for edge-tts, "openai" for OpenAI gpt-4o-mini-tts
    edge_fallback_voice: str = ""  # edge-tts voice used when OpenAI engine falls back


class AdFormat(StrEnum):
    """Available ad creative formats that shape how the joke is delivered."""

    CLASSIC_PITCH = "classic_pitch"
    TESTIMONIAL = "testimonial"
    DUO_SCENE = "duo_scene"
    LIVE_REMOTE = "live_remote"
    LATE_NIGHT_WHISPER = "late_night_whisper"
    INSTITUTIONAL_PSA = "institutional_psa"

    @property
    def voice_count(self) -> int:
        """Number of distinct voices this format needs."""
        return 2 if self in (AdFormat.DUO_SCENE, AdFormat.TESTIMONIAL) else 1


@dataclass
class SonicWorld:
    """Sonic palette for an ad: environment, music bed, and transition motif."""

    environment: str = ""
    music_bed: str = "lounge"
    transition_motif: str = "chime"
    sonic_signature: str = ""  # e.g. "ice_clink+startup_synth" for brand motif generation


@dataclass
class CampaignSpine:
    """Per-brand creative memory that shapes recurring ad campaigns."""

    premise: str = ""
    sonic_signature: str = ""  # e.g. "ice_clink+startup_synth"
    format_pool: list[str] = field(default_factory=list)
    spokesperson: str = ""  # speaker role name
    escalation_rule: str = ""  # natural language for prompt


@dataclass
class AdBrand:
    """A fictional advertiser that can recur across breaks."""

    name: str
    tagline: str
    category: str = "general"
    recurring: bool = True
    campaign: CampaignSpine | None = None


@dataclass
class AdVoice:
    """A non-host voice used to perform commercial copy."""

    name: str
    voice: str  # edge-tts voice ID
    style: str  # character description for the prompt
    role: str = ""  # speaker role: "hammer", "seductress", etc.


@dataclass
class AdPart:
    """One structured unit inside an ad script: voice, SFX, pause, or environment."""

    type: str  # "voice", "sfx", "pause", "environment"
    text: str = ""
    voice: str = ""
    sfx: str = ""
    duration: float = 0.0
    role: str = ""  # which speaker role delivers this part
    environment: str = ""  # environment cue for ambience


@dataclass
class AdScript:
    """Structured ad script returned by the LLM before audio synthesis."""

    brand: str
    parts: list[AdPart] = field(default_factory=list)
    summary: str = ""
    mood: str = ""  # legacy alias, set to sonic.music_bed
    format: str = "classic_pitch"
    sonic: SonicWorld = field(default_factory=SonicWorld)
    roles_used: list[str] = field(default_factory=list)


@dataclass
class AdHistoryEntry:
    """Minimal history item used to build cross-ad campaign callbacks."""

    brand: str
    summary: str
    timestamp: float = 0.0
    format: str = ""
    sonic_signature: str = ""


@dataclass
class Segment:
    """A rendered audio file queued for live playback."""

    type: SegmentType
    path: Path
    duration_sec: float = 0.0
    metadata: dict = field(default_factory=dict)
    ephemeral: bool = True


@dataclass
class SegmentLogEntry:
    """Compact log event for produced or streamed segments."""

    type: str
    label: str
    timestamp: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass
class ListenerProfile:
    """Aggregate listener behavior patterns inferred from playback signals.

    These are generic pattern labels — never personal data. The station uses
    them to choose tracks and generate eerily on-point host commentary.
    """

    songs_played: int = 0
    songs_skipped: int = 0
    # Rolling window of (was_skipped, duration_ms, genre_hint) for last 20 tracks
    recent_outcomes: list[dict] = field(default_factory=list)
    # Last psychic prediction made + whether it was correct
    last_prediction: str = ""
    last_prediction_correct: bool | None = None
    # Taste mirror cooldown (segments since last taste mirror)
    segments_since_taste_mirror: int = 0

    @property
    def skip_rate(self) -> float:
        """Fraction of tracks skipped (0.0-1.0)."""
        if self.songs_played == 0:
            return 0.0
        return self.songs_skipped / self.songs_played

    @property
    def patterns(self) -> list[str]:
        """Derive human-readable behavior labels from recent outcomes."""
        if len(self.recent_outcomes) < 3:
            return []

        labels: list[str] = []
        recent = self.recent_outcomes[-10:]
        skips = [r for r in recent if r.get("skipped")]

        # Skip patterns
        if len(skips) >= 4:
            labels.append("restless_skipper")
        elif len(skips) == 0 and len(recent) >= 5:
            labels.append("rides_every_song")

        # Intro bail detection (skipped in first 30s)
        intro_bails = [r for r in skips if r.get("listen_sec", 999) < 30]
        if len(intro_bails) >= 2:
            labels.append("bails_on_intros")

        # Ballad loyalty (slow songs rarely skipped)
        slow = [r for r in recent if r.get("energy_hint") == "low"]
        slow_skips = [r for r in slow if r.get("skipped")]
        if len(slow) >= 2 and len(slow_skips) == 0:
            labels.append("ballad_lover")

        # High-energy preference
        fast = [r for r in recent if r.get("energy_hint") == "high"]
        fast_completions = [r for r in fast if not r.get("skipped")]
        if len(fast_completions) >= 3:
            labels.append("energy_seeker")

        # Guilty pleasure (claims to skip genre but never does)
        # This is set externally when specific artists survive despite pattern

        return labels

    def record_outcome(
        self,
        *,
        skipped: bool,
        listen_sec: float = 0.0,
        energy_hint: str = "",
        track_display: str = "",
    ) -> None:
        """Record the outcome of a track play (skipped or completed)."""
        self.songs_played += 1
        if skipped:
            self.songs_skipped += 1
        self.recent_outcomes.append(
            {
                "skipped": skipped,
                "listen_sec": listen_sec,
                "energy_hint": energy_hint,
                "track": track_display,
            }
        )
        if len(self.recent_outcomes) > 20:
            self.recent_outcomes = self.recent_outcomes[-20:]

    def describe_for_prompt(self) -> str:
        """Natural-language summary of listener patterns for LLM injection."""
        pats = self.patterns
        if not pats:
            return ""

        descriptions = {
            "restless_skipper": "l'ascoltatore salta spesso le canzoni — impaziente, vuole il pezzo giusto subito",
            "rides_every_song": "l'ascoltatore ascolta ogni canzone fino alla fine — paziente, si fida della radio",
            "bails_on_intros": "l'ascoltatore molla le canzoni nei primi secondi — se l'intro non convince, via",
            "ballad_lover": "l'ascoltatore non salta mai le ballate — ama i pezzi lenti, romantici",
            "energy_seeker": "l'ascoltatore preferisce pezzi ad alta energia — vuole ritmo, movimento",
        }

        lines = [descriptions[p] for p in pats if p in descriptions]
        if not lines:
            return ""

        prediction_callback = ""
        if self.last_prediction and self.last_prediction_correct is not None:
            if self.last_prediction_correct:
                prediction_callback = (
                    f'\nPREDIZIONE PRECEDENTE CORRETTA: avevamo detto "{self.last_prediction}" '
                    "e avevamo ragione. Potete vantarvi brevemente."
                )
            else:
                prediction_callback = (
                    f'\nPREDIZIONE PRECEDENTE SBAGLIATA: avevamo detto "{self.last_prediction}" '
                    "ma ci siamo sbagliati. Potete scherzarci sopra."
                )

        return (
            "LISTENER BEHAVIOR PATTERNS (generic, never name or identify the listener):\n"
            + "\n".join(f"- {line}" for line in lines)
            + prediction_callback
        )


@dataclass
class StationState:
    """Mutable in-memory state shared by producer and streamer tasks."""

    playlist: list[Track] = field(default_factory=list)
    playlist_revision: int = 0
    played_tracks: list[Track] = field(default_factory=list)
    songs_since_banter: int = 0
    songs_since_ad: int = 0
    songs_since_news: int = 0
    segments_since_station_id: int = 0
    segments_since_time_check: int = 0
    running_jokes: list[str] = field(default_factory=list)
    current_track: Track | None = None
    segments_produced: int = 0
    failed_segments: int = 0
    segment_log: list[SegmentLogEntry] = field(default_factory=list)
    listener: ListenerProfile = field(default_factory=ListenerProfile)
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
    # Shareware trial: counts canned banter clips actually streamed to listener
    canned_clips_streamed: int = 0
    # Consumption metrics
    api_calls: int = 0
    api_input_tokens: int = 0
    api_output_tokens: int = 0
    tts_characters: int = 0

    def switch_playlist(self, tracks: list[Track], source: PlaylistSource | None = None) -> None:
        """Replace the active playlist and bump revision counter.

        In-flight producer segments are discarded on next commit check.
        """
        self.playlist_revision += 1
        self.playlist = tracks
        self.playlist_source = source
        self.startup_source_error = ""
        self.songs_since_banter = 0
        self.songs_since_ad = 0
        self.songs_since_news = 0

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
        now = time.time()
        seg_type = segment.type.value
        label = segment.metadata.get("title", segment.metadata.get("brand", seg_type))
        # Record previous music segment as completed (not skipped) in listener profile
        prev = self.now_streaming
        if prev.get("type") == "music" and prev.get("started"):
            self.listener.record_outcome(
                skipped=False,
                listen_sec=now - prev["started"],
                track_display=prev.get("label", ""),
            )
            self.listener.segments_since_taste_mirror += 1
        # Track canned banter clips at stream time (shareware trial)
        if segment.metadata.get("canned"):
            self.canned_clips_streamed += 1
        self.now_streaming = {
            "type": seg_type,
            "label": label,
            "started": now,
            "metadata": segment.metadata,
        }
        self.stream_log.append(
            SegmentLogEntry(
                type=seg_type,
                label=label,
                timestamp=now,
                metadata=segment.metadata,
            )
        )
        if len(self.stream_log) > 50:
            self.stream_log = self.stream_log[-50:]

    def reserve_next_track(self) -> Track:
        """Legacy FIFO rotation — use select_next_track() for weighted shuffle."""
        if not self.playlist:
            raise RuntimeError("Playlist is empty")
        track = self.playlist.pop(0)
        self.playlist.append(track)
        return track

    def select_next_track(
        self,
        *,
        allow_explicit: bool = True,
        repeat_cooldown: int = 5,
        artist_cooldown: int = 3,
        max_artist_per_hour: int = 3,
    ) -> Track:
        """Pick the next track using weighted random selection with diversity rules.

        Hard filters remove ineligible tracks, then soft weights bias toward
        tracks that haven't played recently, from under-represented artists,
        and with smooth energy transitions.  Falls back to progressively
        relaxed filters if the pool is too small.
        """
        if not self.playlist:
            raise RuntimeError("Playlist is empty")

        pool = list(self.playlist)

        # Build all filter/weight data in a single pass over played_tracks.
        # Each track is visited once; sets and counters are accumulated per-index.
        n_played = len(self.played_tracks)
        recent_keys: set[str] = set()
        recent_artist_set: set[str] = set()
        artist_hour_counts: dict[str, int] = {}
        last_play_pos: dict[str, int] = {}
        recent_artist_10: dict[str, int] = {}

        hour_start = max(0, n_played - 17)
        cooldown_start = max(0, n_played - repeat_cooldown) if repeat_cooldown else n_played
        artist_cd_start = max(0, n_played - artist_cooldown) if artist_cooldown else n_played
        artist_10_start = max(0, n_played - 10)

        for i, t in enumerate(self.played_tracks):
            key = t.cache_key
            last_play_pos[key] = i  # last occurrence wins (dict overwrite)
            if i >= cooldown_start:
                recent_keys.add(key)
            if i >= artist_cd_start:
                recent_artist_set.add(t.artist)
            if i >= hour_start:
                artist_hour_counts[t.artist] = artist_hour_counts.get(t.artist, 0) + 1
            if i >= artist_10_start:
                recent_artist_10[t.artist] = recent_artist_10.get(t.artist, 0) + 1

        # --- Hard filters (progressively relaxed) ---
        def _apply_filters(candidates: list[Track], *, strict: bool = True) -> list[Track]:
            result = candidates
            if not allow_explicit:
                result = [t for t in result if not t.explicit]
            if strict and repeat_cooldown:
                result = [t for t in result if t.cache_key not in recent_keys]
            if strict and artist_cooldown:
                result = [t for t in result if t.artist not in recent_artist_set]
            if strict and max_artist_per_hour:
                result = [t for t in result if artist_hour_counts.get(t.artist, 0) < max_artist_per_hour]
            return result

        candidates = _apply_filters(pool, strict=True)
        if not candidates:
            # Relax: drop artist cooldown + hourly cap
            candidates = _apply_filters(pool, strict=False)
        if not candidates:
            # Final fallback: entire pool (even explicit if filtered out everything)
            candidates = pool

        # --- Soft weights (all lookups are O(1) via dicts built in the single pass above) ---
        weights: list[float] = []
        for track in candidates:
            w = 1.0

            # Recency decay: approaches 1.0 as time since last play grows (1 ago→0.1, 10→0.65, 20+→~1.0)
            if track.cache_key in last_play_pos:
                songs_ago = n_played - last_play_pos[track.cache_key]
                w *= 1.0 - math.exp(-0.1 * songs_ago)
            else:
                w *= 1.2  # Never-played bonus

            # Artist diversity: penalize over-represented artists in recent history
            recent_artist_count = recent_artist_10.get(track.artist, 0)
            if recent_artist_count >= 2:
                w *= 0.3
            elif recent_artist_count == 1:
                w *= 0.7

            # Popularity boost: slight preference for popular tracks
            if track.popularity:
                w *= 0.8 + 0.2 * (track.popularity / 100.0)

            weights.append(max(w, 0.01))  # Floor to avoid zero weights

        return random.choices(candidates, weights=weights, k=1)[0]

    def after_music(self, track: Track) -> None:
        """Advance state after successfully queuing a music segment."""
        self.played_tracks.append(track)
        if len(self.played_tracks) > 50:
            self.played_tracks = self.played_tracks[-50:]
        self.current_track = track
        self.songs_since_banter += 1
        self.songs_since_ad += 1
        self.songs_since_news += 1
        self.segments_since_station_id += 1
        self.segments_since_time_check += 1
        self.segments_produced += 1
        self._log("music", track.display)

    def after_banter(self) -> None:
        """Advance counters after successfully queuing host banter."""
        self.songs_since_banter = 0
        self.segments_produced += 1
        self._log("banter", "Host banter")

    def after_news_flash(self, category: str = "") -> None:
        """Advance counters after successfully queuing a news flash."""
        self.songs_since_banter = 0
        self.songs_since_news = 0
        self.segments_produced += 1
        self._log("news_flash", f"News flash: {category}")

    def record_ad_spot(
        self,
        brand: str,
        summary: str = "",
        format: str = "",
        sonic_signature: str = "",
    ) -> None:
        """Record a single ad spot in history (called per-spot within a break)."""
        self.ad_history.append(
            AdHistoryEntry(
                brand=brand,
                summary=summary,
                timestamp=time.time(),
                format=format,
                sonic_signature=sonic_signature,
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

    def after_station_id(self) -> None:
        """Advance counters after a station ID stinger."""
        self.segments_since_station_id = 0
        self.segments_produced += 1
        self._log("station_id", "Station ID")

    def after_sweeper(self) -> None:
        """Advance counters after a short station sweeper voice drop."""
        self.segments_produced += 1
        self._log("sweeper", "Station sweeper")

    def after_time_check(self) -> None:
        """Advance counters after a time check."""
        self.segments_since_time_check = 0
        self.segments_produced += 1
        self._log("time_check", "Time check")

    def add_joke(self, joke: str) -> None:
        """Keep a short rolling buffer of running jokes for prompt callbacks."""
        self.running_jokes.append(joke)
        self.running_jokes = self.running_jokes[-5:]


@dataclass(frozen=True)
class Capabilities:
    """Runtime capability flags derived from config + live state.

    These replace the old 64-state mode system with independent boolean flags.
    UI tier labels are derived from the flags for display only.
    """

    spotify_connected: bool = False
    """go-librespot zeroconf auth active (streaming and playback control work)."""

    spotify_api: bool = False
    """Spotify Client ID/secret present (playlist browsing, search, metadata)."""

    anthropic: bool = False
    """Anthropic API key available for live Claude-generated banter and ads."""

    ha: bool = False
    """Home Assistant token present and integration enabled."""

    @property
    def tier(self) -> str:
        """Derive a human-friendly tier label from capability flags."""
        if self.spotify_connected and self.spotify_api and self.anthropic:
            return "full_ai"
        if self.spotify_connected and self.spotify_api:
            return "your_music_full"
        if self.spotify_connected:
            return "your_music_basic"
        if self.anthropic:
            return "demo_ai"
        return "demo"

    @property
    def tier_label(self) -> str:
        """Display name for the current tier — station language, not product language."""
        return {
            "full_ai": "Live Broadcast",
            "your_music_full": "Your Station",
            "your_music_basic": "Your Station",
            "demo_ai": "On Air",
            "demo": "On Air",
        }[self.tier]
