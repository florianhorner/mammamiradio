"""Core data models shared across playback, scripting, and streaming."""

from __future__ import annotations

import asyncio
import datetime
import logging
import math
import random
import re
import time
from collections import deque
from collections.abc import Callable, Collection, Iterator
from dataclasses import asdict, dataclass, field
from enum import Enum
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, TypedDict
from urllib.parse import urlsplit, urlunsplit

from mammamiradio.core.listener_session import ListenerSession
from mammamiradio.core.segment_status import is_fallback_active
from mammamiradio.playlist.preferences import preference_score_map, preference_weight

if TYPE_CHECKING:
    from mammamiradio.core.listener_truth import HomeReturnAuthority
    from mammamiradio.home.authorization import HomeAuthorization
    from mammamiradio.home.context_director import HomeContextDirector, PromptFact
    from mammamiradio.home.evening_memory import EveningLedger
    from mammamiradio.home.moment_receipts import MomentStore
    from mammamiradio.hosts.persona import PersonaStore
    from mammamiradio.hosts.verbal_gag_ledger import VerbalGagLedger
    from mammamiradio.release_campaign import ReleaseCampaign


logger = logging.getLogger("mammamiradio.render_timing")


PartyMode = Literal["festival"]

# Record Hunt ("Find records") selection lift. The multiplier applied to heading-matched
# tracks in select_next_track() is adaptive: sized from the live pool so the hunt set
# reliably lands ~HEADING_TARGET_SHARE of picks no matter how large rotation is, then
# clamped to [MIN, MAX]. MIN preserves the historical fixed x4 floor for small pools;
# MAX stops a tiny hunt set from making one song dominate the station.
HEADING_TARGET_SHARE = 0.45
HEADING_MIN_LIFT = 4.0
HEADING_MAX_LIFT = 60.0

CostCategory = Literal["script_banter", "script_transition", "script_ads", "script_home_mood", "script_memory", "tts"]
LLM_COST_CATEGORIES: tuple[CostCategory, ...] = (
    "script_banter",
    "script_transition",
    "script_ads",
    "script_home_mood",
    "script_memory",
)
TTS_COST_CATEGORY: CostCategory = "tts"


class GenerationWasteReason:
    """Canonical discard reasons for generated-but-unbroadcast segment waste."""

    STALE_SOURCE = "stale_source"
    STALE_PLAYLIST = "stale_playlist"
    STALE_CONTINUITY = "stale_continuity"
    STALE_CHAOS = "stale_chaos"
    QUALITY_GATE_REJECT = "quality_gate_reject"
    SESSION_STOPPED = "session_stopped"
    INTERRUPT = "interrupt"
    AIR_NEXT_OVERFLOW = "air_next_overflow"
    EGRESS_STALE = "egress_stale"
    BLOCKLIST_GATE = "blocklist_gate"
    OPERATOR_STOP = "operator_stop"
    OPERATOR_PANIC = "operator_panic"
    OPERATOR_PURGE = "operator_purge"
    SOURCE_SWITCH = "source_switch"
    OPERATOR_BAN = "operator_ban"
    OPERATOR_QUEUE_REMOVE = "operator_queue_remove"
    STALE_PLAYED_TRACK_REF = "stale_played_track_ref"
    LISTENER_SESSION_STALE = "listener_session_stale"


class SegmentType(Enum):
    """Kinds of segments that can appear on the station timeline."""

    MUSIC = "music"
    BANTER = "banter"
    AD = "ad"
    NEWS_FLASH = "news_flash"
    STATION_ID = "station_id"
    SWEEPER = "sweeper"
    TIME_CHECK = "time_check"

    @property
    def segment_class(self) -> Literal["music", "voice", "interstitial"]:
        """Stable display bucket consumed by the v1 integration contract.

        Maps every internal SegmentType to one of three renderer buckets so
        integration consumers (Music Assistant, custom HA cards, future
        provider authors) can branch on a small stable enum instead of the
        full internal taxonomy. Transient runtime states like ``stopped`` or
        ``skipping`` are mapped to ``unavailable`` by the serializer, not by
        this property.
        """
        if self is SegmentType.MUSIC:
            return "music"
        if self in (SegmentType.BANTER, SegmentType.NEWS_FLASH):
            return "voice"
        return "interstitial"


class ChaosSubtype(Enum):
    """Host-chaos flavors carried by BANTER segments."""

    FOURTH_WALL = "chaos_fourth_wall"
    ABANDONED_STORM = "chaos_abandoned_storm"
    IMPOSSIBLE_RECALL = "chaos_impossible_recall"
    ICON_MOMENT = "chaos_icon_moment"
    URGENT_INTERRUPT = "urgent_interrupt"


@dataclass
class InterruptSpec:
    """Describes a pending host interrupt triggered by an HA automation or timer."""

    directive: str
    urgency: str = "pissed"  # "pissed" | "urgent" | "gentle"
    cooldown: int = 60  # seconds before this entity can fire again


@dataclass
class Track:
    """A playable track sourced from charts, cache, or local files."""

    title: str
    artist: str
    duration_ms: int
    spotify_id: str = ""
    youtube_id: str = ""
    direct_url: str = ""
    local_path: Path | None = None
    position_ms: int = 0
    album_art: str = ""
    album: str = ""
    explicit: bool = False
    popularity: int = 0
    year: int = 0
    source: Literal["youtube", "jamendo", "local", "demo", "classic"] = "youtube"
    heading_id: str = ""

    @staticmethod
    def _slugify_cache_value(raw: str, *, max_length: int = 160) -> str:
        return re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")[:max_length]

    @staticmethod
    def _normalize_cache_url(url: str) -> str:
        parsed = urlsplit((url or "").strip())
        host = (parsed.hostname or "").lower()
        netloc = host
        if parsed.port:
            netloc = f"{host}:{parsed.port}"
        path = parsed.path.rstrip("/")
        if not path and parsed.path:
            path = "/"
        return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))

    @property
    def legacy_cache_key(self) -> str:
        """Pre-source cache key kept for backwards-compatible cache lookups."""
        return self._slugify_cache_value(f"{self.artist} {self.title}", max_length=80)

    @property
    def cache_key(self) -> str:
        """Stable filesystem-friendly key used for caching source-specific audio."""
        if self.youtube_id:
            return self._slugify_cache_value(f"youtube|{self.youtube_id}")
        if self.source == "jamendo":
            jamendo_id = self.spotify_id.strip()
            if jamendo_id:
                return self._slugify_cache_value(f"jamendo|{jamendo_id}")
            if self.direct_url:
                return self._slugify_cache_value(f"jamendo|{self._normalize_cache_url(self.direct_url)}")
        if self.local_path is not None:
            return self._slugify_cache_value(f"{self.source or 'local'}|{self.local_path.as_posix()}")
        return self._slugify_cache_value(f"{self.artist}|{self.title}|{self.source or 'youtube'}")

    @property
    def display(self) -> str:
        """Human-readable label used in logs and APIs."""
        return f"{self.artist} – {self.title}"

    @cached_property
    def normalized_key(self) -> tuple[str, str]:
        """Canonical station song identity used by bans, preferences, and dedupe."""
        return (self.artist.strip().lower(), self.title.strip().lower())


def normalized_track_key(track: Track) -> tuple[str, str]:
    """Canonical station song identity used by bans, preferences, and dedupe."""
    return track.normalized_key


@dataclass
class PlayedEntry:
    """Track heard by listeners, recorded at stream-start time."""

    track: Track
    played_at: float


@dataclass
class RuntimeProviderEvent:
    """Operator-visible runtime provider transition for the current session."""

    event: str
    provider_class: str
    from_provider: str
    to_provider: str
    reason: str
    fallback_active: bool
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


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
class Heading:
    """Active operator course overlay for the rotation pool."""

    id: str
    seed: str
    label: str
    set_at: float
    set_by: str
    announced: bool = False
    selection_budget: int = 0
    selection_spent: int = 0
    targets: list[dict[str, str]] = field(default_factory=list)
    phase: str = "hunting"
    hunt_started_announced: bool = False
    first_found_at: float = 0.0
    last_narrated_at: float = 0.0
    narration_count: int = 0


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
    engine: str = "edge"  # edge|openai|azure|elevenlabs
    edge_fallback_voice: str = ""  # edge-tts voice used when a cloud TTS engine falls back
    voice_settings: dict = field(default_factory=dict)  # per-host ElevenLabs overrides, e.g. {"stability": 0.6}
    # ElevenLabs v2 remains the backwards-compatible default for every existing
    # host. V3 is opt-in per host because its compatible tuning and delivery
    # controls differ from v2.
    elevenlabs_model: str = "eleven_multilingual_v2"
    # A profile authorizes the small, code-owned V3 performance cue vocabulary.
    # It is deliberately separate from the canonical spoken text.
    delivery_profile: str = "none"


@dataclass(frozen=True)
class DialogueLine:
    """One clean host line plus an optional semantic delivery cue.

    Iteration intentionally exposes only the historic ``(host, text)`` pair so
    existing callers keep their clean-text contract while the audio boundary
    can consume ``delivery`` as sidecar metadata.
    """

    host: HostPersonality
    text: str
    delivery: str = "neutral"

    def __iter__(self) -> Iterator[HostPersonality | str]:
        yield self.host
        yield self.text

    def __getitem__(self, index: int | slice) -> HostPersonality | str | tuple[HostPersonality, str]:
        return (self.host, self.text)[index]

    def __len__(self) -> int:
        return 2


@dataclass
class AdHistoryEntry:
    """Minimal history item used to build cross-ad campaign callbacks."""

    brand: str
    summary: str
    timestamp: float = 0.0
    format: str = ""
    sonic_signature: str = ""
    environment: str = ""
    music_bed: str = ""
    transition_motif: str = ""


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
    duration_sec: float = 0.0


@dataclass
class ListenerProfile:
    """Aggregate listener behavior patterns inferred from playback signals.

    These are generic pattern labels — never personal data. The station uses
    them to choose tracks and generate eerily on-point host commentary.
    """

    songs_played: int = 0
    songs_skipped: int = 0
    # Rolling window of (was_skipped, duration_ms, genre_hint) for last 20 tracks
    recent_outcomes: deque[dict] = field(default_factory=lambda: deque(maxlen=20))
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
        recent = list(self.recent_outcomes)[-10:]
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


# Tri-state verdict from the active key-validation probe. Typed so route logic,
# UI shaping, and tests share one contract and mypy catches drift/typos.
KeyStatus = Literal["unverified", "valid", "rejected"]


class ScoredEntityStatus(TypedDict):
    """Admin-only telemetry shape for a budgeted HA entity (see ScoredEntity.to_status_dict)."""

    entity_id: str
    area: str | None
    domain: str
    score: float
    state: object
    label: str
    label_tier: str
    summary: str
    device_class: object


class ExternalAddNotice(TypedDict):
    """A failed/dropped background queue-from-search outcome surfaced in /status."""

    display: str
    ok: bool
    reason: str
    ts: float


RECENTLY_CONSUMED_RETENTION_SECONDS = 300
STREAM_DELIVERY_WINDOW_SECONDS = 15 * 60
STREAM_PACING_EVENT_KINDS = ("late", "underrun", "overrun_rebased")
HA_REFRESH_STAGES = ("states_request", "enrichment_wait", "projection", "idle")


class ConsumedListenerRequest(TypedDict):
    """A consumed listener request retained for 5-minute admin visibility."""

    id: str
    name: str | None
    message: str | None
    song_track: str | None
    type: str | None
    status: str  # "sent_to_hosts" | "song_not_found" | "source_changed"
    song_error_reason: str
    consumed_at: float


@dataclass
class StationState:
    """Mutable in-memory state shared by producer and streamer tasks."""

    playlist: list[Track] = field(default_factory=list)
    playlist_revision: int = 0
    # Bumped ONLY when the playlist source is replaced (switch_playlist), never
    # on in-place mutations like enrich / move-to-next / festival toggle. Used by
    # background external downloads to tell a real source switch (drop the pick)
    # from a benign edit (keep it). See _commit_external_download.
    source_revision: int = 0
    played_tracks: deque[Track] = field(default_factory=lambda: deque(maxlen=50))
    played_track_log: deque[PlayedEntry] = field(default_factory=lambda: deque(maxlen=100))
    songs_since_banter: int = 0
    songs_since_ad: int = 0
    songs_since_news: int = 0
    segments_since_station_id: int = 0
    segments_since_time_check: int = 0
    guest_host_banter_cooldown_remaining: int = 0
    running_jokes: deque[str] = field(default_factory=lambda: deque(maxlen=5))
    recent_transition_texts: deque[str] = field(default_factory=lambda: deque(maxlen=8))
    current_track: Track | None = None
    segments_produced: int = 0
    failed_segments: int = 0
    segment_log: deque[SegmentLogEntry] = field(default_factory=lambda: deque(maxlen=50))
    listener: ListenerProfile = field(default_factory=ListenerProfile)
    # Last banter/ad scripts for display
    last_banter_script: list[dict] = field(default_factory=list)
    last_ad_script: dict = field(default_factory=dict)
    ad_history: deque[AdHistoryEntry] = field(default_factory=lambda: deque(maxlen=20))
    session_stopped: bool = False
    # Set by streamer when session_stopped flips False, so producer's
    # stopped-state sleep wakes immediately instead of polling up to 1s.
    resume_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Last successful music norm, recycled when every chart candidate is silent.
    last_music_file: Path | None = None
    # Type of the most recently enqueued (queue-tail) segment; drives speech-bed adjacency.
    # None means adjacency is CLEARED — a continuity break (emergency tone, errored fill,
    # urgent interrupt, or front-insert overflow drop), not merely "unset".
    last_enqueued_type: SegmentType | None = None
    playlist_source: PlaylistSource | None = None
    startup_source_error: str = ""
    heading: Heading | None = None
    heading_revision: int = 0
    heading_persist_callback: Callable[[Heading], None] | None = None
    heading_pending_announcement: str = ""
    heading_pending_narration_kind: str = ""
    heading_announced_id: str = ""
    # What the listener is hearing RIGHT NOW
    now_streaming: dict = field(default_factory=dict)
    # Pre-produced segments waiting to play (shadow of asyncio.Queue for UI display)
    queued_segments: list[dict] = field(default_factory=list)
    # Every live control-plane change that can invalidate queued/in-flight audio
    # bumps this generation. Producer commits compare it before admission.
    continuity_epoch: int = 0
    # Capacity-exempt immediate fallback. Playback consumes this only after the
    # real queue drains, so a full queue cannot prevent a safety reservation.
    continuity_slot: Segment | None = None
    # Paths admitted or normalized by the current process, with their known
    # playable duration. The control-plane guard uses this index instead of
    # probing or walking the cache during an operator action.
    immediate_audio_index: dict[Path, float] = field(default_factory=dict)
    # Session-scoped rescue rotation. Maps normalized-cache paths to the monotonic
    # time they last aired as a norm-cache rescue. Selection groups bitrate-only
    # path variants by cache key, so the same cached track cannot air three times
    # in twenty minutes when the producer stalls (the illusion break this closes).
    # Cleared on restart; no persistence. Pruned on record. See audio/norm_cache.py.
    rescue_airplay: dict[Path, float] = field(default_factory=dict)
    # Stream-side log (when segments actually play, not when produced)
    stream_log: deque[SegmentLogEntry] = field(default_factory=lambda: deque(maxlen=50))
    # Recent generated banter clips that have actually started streaming.
    # Producer may mix these under future music for "studio bleed".
    recent_banter_paths: deque[Path] = field(default_factory=lambda: deque(maxlen=5))
    # Home Assistant context (natural language summary of home state)
    ha_context: str = ""
    ha_events_summary: str = ""
    # Phase 1: recent state-change events
    # Phase 2: home mood scene classification
    ha_home_mood: str = ""
    # Phase 3: weather narrative arc
    ha_weather_arc: str = ""
    # Phase 4: pending reactive directive (consumed after one use)
    ha_pending_directive: str = ""
    # Moment Receipt id travelling WITH the pending directive (ritual lanes
    # only; empty for radio-event/reactive/skip directives, which have no
    # receipt in v1). Set and cleared strictly alongside ha_pending_directive.
    ha_pending_directive_moment_id: str = ""
    # Handoff slot: the moment id the scriptwriter actually consumed for the
    # banter it just wrote (same lifetime as last_banter_script). The producer
    # copies it into the segment's metadata at build time and clears it —
    # never read live at build, so a fresh HA poll can't cross the wires.
    last_banter_ritual_moment_id: str = ""
    # Impossible Moments v2 (A): one rendered evening running-gag for the next
    # banter (consumed after one use); populated by the producer from the ledger.
    ha_running_gag: str = ""
    # Ledger bucket key for the offered gag, so the producer can spend its
    # cooldown (mark_spoken) only after generated banter actually airs.
    ha_running_gag_key: str = ""
    # Moment Receipt id for the offered gag (empty when the offered bucket has
    # no ritual provenance). Lifecycle mirrors ha_running_gag_key exactly.
    ha_running_gag_moment_id: str = ""
    # Dashboard HA moments: last notable event (for Casa card)
    ha_recent_event_count: int = 0
    ha_last_event_label: str = ""
    ha_last_event_ts: float = 0.0
    # English equivalents for admin Engine Room display
    ha_home_mood_en: str = ""
    ha_weather_arc_en: str = ""
    ha_events_summary_en: str = ""
    ha_last_event_label_en: str = ""
    ha_scored_entities: list[ScoredEntityStatus] = field(default_factory=list)
    ha_denylist_hits: dict[str, int] = field(default_factory=dict)
    ha_catalog_hit_rate: float = 0.0
    ha_label_stats: dict[str, int | float] = field(default_factory=dict)
    ha_registry_source: str = ""
    ha_context_last_updated: float = 0.0
    ha_context_entity_count: int = 0
    ha_context_char_count: int = 0
    # Producer-owned Home Assistant refresh telemetry. These fields describe
    # the refresh coordinator only; `ha_context_last_updated` remains the
    # source-snapshot timestamp consumed by legacy status callers.
    ha_context_refresh_in_flight: bool = False
    ha_context_refresh_last_attempt_at: float = 0.0
    ha_context_refresh_active_foreground_timed_out: bool = False
    ha_context_refresh_last_result: str = ""
    ha_context_refresh_last_result_duration_ms: int | None = None
    ha_context_refresh_last_result_used_background: bool = False
    # Coarse coordinator-owned stage telemetry for private stream-delivery
    # correlation. It never contains entity data and is never scheduling input.
    ha_context_refresh_stage: str = "idle"
    ha_context_refresh_stage_started_monotonic: float = 0.0
    # Kept by the producer against max(2 * poll_interval, 120s), so status
    # does not guess at a device-specific prompt-safety threshold.
    ha_context_refresh_stale: bool = False
    ha_context_refresh_stale_after_seconds: float = 0.0
    # Lets the admin show the honest first-update state before any eligible
    # host segment has started a refresh. It is internal coordinator metadata,
    # not a user-facing configuration option.
    ha_context_refresh_configured: bool = False
    # Provenance prevents an aged HA event directive from being mistaken for a
    # non-HA cue such as the listener skip-bit when stale prompt context is
    # withheld.
    ha_pending_directive_source: str = ""
    # Non-serialised producer-owned object used by the admin serializer for a
    # read-only mailbox completion check. It is cleared at producer shutdown.
    ha_context_refresh_mailbox: object | None = field(default=None, repr=False, compare=False)
    ha_first_home_context_moment_fired: bool = False
    # Session-only ambient Home Assistant fact rotation. The director is owned
    # by main.py and deliberately resets when the add-on restarts.
    home_context_director: HomeContextDirector | None = None
    # R0 install-scoped authorization. Cold installs get only normalized
    # weather/daylight; pre-existing databases retain legacy behavior until the
    # provenance-gated Home Profile migration lands.
    home_authorization: HomeAuthorization | None = None
    # R0 migration bridge callback. Receives IDs only (never raw states or
    # labels) after a successful full HA snapshot.
    home_entity_ids_observer: Callable[[frozenset[str]], None] | None = None
    # Handoff from the scriptwriter to the producer's queue-admission seam.
    # It is cleared on every new banter attempt so a failed render cannot attach
    # an older fact to unrelated speech.
    last_banter_home_fact: PromptFact | None = None
    last_banter_return_authority: HomeReturnAuthority | None = None
    # Community-inspired Impossible Moments recipe telemetry. Public surfaces
    # may expose only the coarse family labels; recipe internals stay admin-only.
    ha_ritual_context: str = ""
    ha_ritual_public_families: list[str] = field(default_factory=list)
    ha_ritual_matches: list[dict[str, object]] = field(default_factory=list)
    ha_ritual_recipe_audit: list[dict[str, object]] = field(default_factory=list)
    # Force-trigger: producer will use this type instead of scheduler for the next segment
    force_next: SegmentType | None = None
    # Operator-attributed pending trigger: set ONLY by the /api/trigger endpoint so the
    # admin panel can honestly surface "you triggered X" without false-lighting on internal
    # forces — the 60s-silence dead-air rescue and stop/skip/resume all set force_next too.
    # Cleared the moment the producer consumes any force, or on stop (bounds staleness to
    # one production cycle).
    operator_force_pending: SegmentType | None = None
    # Host interrupt: pre-generated bridge clip to play immediately on interrupt
    interrupt_slot: Path | None = None
    # Whether the current interrupt bridge clip is a generated temp file
    interrupt_slot_ephemeral: bool = False
    # Timestamp of last fired interrupt (for cooldown enforcement)
    last_interrupt_ts: float = 0.0
    # Chaos Mode: station-wide host-chaos toggle plus first-strike handoff.
    chaos_mode_active: bool = False
    chaos_pending: ChaosSubtype | None = None
    chaos_cutover_epoch: int = 0
    chaos_script_fallbacks: int = 0
    chaos_audio_failures: int = 0
    chaos_last_degraded_reason: str = ""
    # Pinned track: select_next_track returns this immediately then clears it
    pinned_track: Track | None = None
    # Persistent operator blocklist: normalized (artist, title) -> {display,
    # banned_by, banned_at}. A banned song never re-enters the rotation pool, across
    # HA restarts and every music source. Loaded from blocklist.json at startup
    # (main.py) and enforced at every ingest doorway via playlist.filter_blocklisted.
    # Mutated ONLY by the ban/unban endpoints, synchronously (no await between the
    # read-modify-write and the disk save), so handlers cannot interleave and lose an
    # update — the same single-loop discipline switch_playlist / queue_remove_item use.
    blocklist: dict[tuple[str, str], dict] = field(default_factory=dict)
    # Persistent operator taste: normalized (artist, title) -> {score, display,
    # updated_at, updated_by}. Scores are soft scheduler weights only; bans remain
    # the sole hard exclusion.
    song_preferences: dict[tuple[str, str], dict] = field(default_factory=dict)
    # In-session version for cheap admin polling. Startup-loaded preferences start
    # at 0; every real operator mutation bumps this once.
    song_preferences_revision: int = 0
    # Listener requests: shoutouts and song wishes submitted via the dashboard
    pending_requests: list[dict] = field(default_factory=list)
    # Recently consumed requests kept for 5 min so the admin can see what happened
    # to a request that left Pending (sent to hosts, or song not found).
    recently_consumed_requests: list[ConsumedListenerRequest] = field(default_factory=list)
    # Operator-visible pending actions/directives. This mirrors legacy single
    # slots while the producer still consumes those slots for compatibility.
    pending_actions: deque[dict] = field(default_factory=lambda: deque(maxlen=200))
    # Recent background external-add outcomes the admin couldn't see synchronously
    # (the request returned 200 before the download finished). Each entry:
    # {"display": str, "ok": bool, "reason": str, "ts": float}. Surfaced in
    # /status so the admin UI can toast a failed/dropped queue-from-search.
    external_add_notices: deque[ExternalAddNotice] = field(default_factory=lambda: deque(maxlen=10))
    # IP-based rate limiting for /api/listener-request {ip: last_ts}
    _listener_request_rl: dict = field(default_factory=dict)
    # Shareware trial: counts canned banter clips actually streamed to listener
    canned_clips_streamed: int = 0
    # Persona store for compounding listener memory (set by main.py at startup)
    persona_store: PersonaStore | None = None
    # Evening running-gag ledger (Impossible Moments v2 A); set by main.py at startup
    evening_ledger: EveningLedger | None = None
    # Moment Receipts store (ritual-recipe moment trail); set by main.py at startup.
    # Streamer paths only mutate it in memory (dirty flag) — disk writes happen at
    # the producer's save site so the playback loop never does JSON I/O.
    moment_store: MomentStore | None = None
    # Verbal running-gag ledger — cross-domain banter callbacks; set by main.py.
    # In-memory only (session-ephemeral), so a restart correctly forgets gags.
    verbal_gag_ledger: VerbalGagLedger | None = None
    # Release beat campaign state; persisted separately from the optional
    # provenance ledger so post-update announcements still count when Show Memory
    # is disabled.
    release_campaign: ReleaseCampaign | None = None
    # Best-effort background writes for the post-restart music handoff spool.
    _restart_handoff_tasks: set[asyncio.Task[bool]] = field(default_factory=set)
    # Resolved paths of restart-handoff segments admitted into the live queue at
    # startup. The per-enqueue spool prune protects these so it can't delete a
    # handoff file still queued for playback (dead air on the cold open).
    restart_handoff_admitted_paths: set[Path] = field(default_factory=set)
    # Pending banter-seeded verbal gag {text, punch}, committed to the ledger by
    # the producer's banter success callback at QUEUE time (so a discarded banter
    # never plants a travelable gag whose setup never aired). Mirrors
    # ha_running_gag_key's stash->commit lifecycle.
    pending_verbal_gag: dict | None = None
    # Model-reported: did the just-generated flash/ad actually land the offered
    # cross-domain callback gag? The producer resets this False before each
    # flash/ad and retires the gag only when the generator set it True (queue-time
    # is not air-time, and the model may ignore the callback instruction).
    pending_callback_landed: bool = False
    # Consumption metrics
    api_calls: int = 0
    api_input_tokens: int = 0
    api_output_tokens: int = 0
    # Per-model token tallies (model_id → {"input": n, "output": n}) so the cost
    # counter prices each model it actually used, not a flat single rate. Dynamic
    # routing means different segments run different models within one session.
    api_tokens_by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    tts_characters: int = 0
    # Same spend, split by operator-meaningful work category. LLM remains
    # model-aware so a category can price Anthropic and OpenAI fallback correctly.
    api_calls_by_category: dict[str, int] = field(default_factory=dict)
    api_tokens_by_category_model: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    tts_characters_by_category: dict[str, int] = field(default_factory=dict)
    # Provider health telemetry (for /status and /api/capabilities diagnostics)
    anthropic_disabled_until: float = 0.0
    anthropic_last_error: str = ""
    anthropic_last_error_at: float = 0.0
    anthropic_auth_failures: int = 0
    # Active key-validation verdict (set by a startup/on-save/on-demand auth ping;
    # distinct from the time-based suspend above). "rejected" means the provider
    # actively refused the key (401) — a persistent "replace the key" condition the
    # operator can see WITHOUT waiting for a banter segment to fail. "unverified"
    # means not-yet-checked or a non-auth probe failure (quota/rate-limit/network).
    anthropic_key_status: KeyStatus = "unverified"
    anthropic_key_checked_at: float = 0.0
    openai_key_status: KeyStatus = "unverified"
    openai_key_checked_at: float = 0.0
    # Listener connection telemetry.  The hub is authoritative for membership;
    # listener_session is the identity-free station epoch used by prompts and
    # persona receipts.
    listeners_active: int = 0
    listeners_peak: int = 0
    listeners_total: int = 0
    listener_session: ListenerSession = field(default_factory=ListenerSession, repr=False)
    listener_session_tasks: set[asyncio.Task] = field(default_factory=set, repr=False)
    listener_session_persona_retry_at: float = 0.0
    listener_session_persona_retry_attempts: int = 0
    # Bounded, anonymous stream-delivery diagnostics. These are session-local
    # and exposed only through authenticated /status. Raw listener identity,
    # segment labels/titles, and Home Assistant values never enter these rows.
    stream_pacing_counts: dict[str, int] = field(
        default_factory=lambda: {kind: 0 for kind in STREAM_PACING_EVENT_KINDS}
    )
    stream_pacing_events: deque[dict] = field(default_factory=lambda: deque(maxlen=20))
    _stream_pacing_window_events: deque[tuple[float, str, int]] = field(
        default_factory=lambda: deque(maxlen=2700), repr=False
    )
    stream_outcome_history: deque[dict] = field(default_factory=lambda: deque(maxlen=20))
    slow_listener_drops_total: int = 0
    slow_listener_last_drop_at: float = 0.0
    _slow_listener_drop_events: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=900), repr=False)
    queue_empty_since: float | None = None
    # Monotonic stamp of the last segment the playback loop started airing —
    # including continuity clips and rescue fills. The /healthz - /readyz
    # silence gate needs "is anything reaching listeners", not "is the queue
    # empty": queue_empty_since keeps running across clip serves (so the
    # rescue ladder can escalate), but a station audibly airing bridge clips
    # is not silent and must not trip the watchdog.
    last_air_monotonic: float | None = None
    # Runtime integrity counters for long-lived sessions
    runtime_sync_events: int = 0
    shadow_queue_corrections: int = 0
    playback_epoch: int = 0
    # Producer rescue-bridge telemetry (#547 observability). Every time a
    # drain/resume/idle bridge enqueues rescue audio the station is, briefly,
    # not the real radio (leadership principle #1). These count how often that
    # happens so the operator can see "running on rescue" instead of a station
    # that merely looks healthy because audio is playing. Session-local by
    # design: a restart clears them. bridge_fires_total is the lifetime count
    # (survives deque eviction); bridge_events backs the rolling-window health
    # check. record_bridge_fire appends only AFTER a successful enqueue.
    bridge_fires_total: int = 0
    bridge_fires_by_type: dict[str, int] = field(default_factory=lambda: {"drain": 0, "resume": 0, "idle": 0})
    bridge_events: deque[dict] = field(default_factory=lambda: deque(maxlen=50))
    # Generated segment waste telemetry: rendered audio discarded before broadcast.
    # Session-local counters mirror the bridge-health pattern — discard_events backs
    # the rolling-window readout in admin Runtime Status.
    discarded_segments_total: int = 0
    discarded_duration_total_sec: float = 0.0
    discarded_unproduced_segments_total: int = 0
    discard_by_reason: dict[str, int] = field(default_factory=dict)
    discard_by_type: dict[str, int] = field(default_factory=dict)
    discard_events: deque[dict] = field(default_factory=lambda: deque(maxlen=100))
    # Recent producer-stage timing, retained only for authenticated admin status.
    # This is diagnostics, never scheduling input: a broken timer must not affect
    # audio admission or playback.
    render_timings: deque[dict] = field(default_factory=lambda: deque(maxlen=20))
    _render_timing_started: float = 0.0
    _render_timing_kind: str = ""
    _render_timing_stages: dict[str, float] = field(default_factory=dict)
    _render_stage_started: float = 0.0
    _render_stage_name: str = ""
    # Most recent observable state change for the v1 integration contract.
    # Updated by on_stream_segment, /api/stop, and /api/resume so the
    # changed_at field and weak ETag in /api/integrations/v1/now-playing
    # reflect any consumer-visible mutation.
    last_state_change_at: float = 0.0
    runtime_events: deque[RuntimeProviderEvent] = field(default_factory=lambda: deque(maxlen=50))
    runtime_provider_state: dict[str, dict] = field(default_factory=dict)
    runtime_health_state: str = ""
    # Live production tracking — what the producer is building right now, surfaced
    # in /api/status so the admin "In produzione" feed can show backstage work.
    # gen_phase is a stable machine key (tests + badge mapping); gen_label is the
    # human English line shown to the operator. All cleared (idle) by end_gen.
    gen_phase: str = ""  # "writing"|"voicing"|"finding"|"mastering"|"checking"|""
    gen_kind: str = ""  # segment type for the badge: "music"|"banter"|"ad"|"news_flash"|""
    gen_label: str = ""  # human English incl. subject, e.g. "Writing the Velocino spot"
    gen_started: float = 0.0  # time.monotonic() when the current phase began; 0.0 when idle
    gen_recent: deque[dict] = field(default_factory=lambda: deque(maxlen=3))
    # each entry: {"phase": str, "kind": str, "label": str, "ok": bool}

    def set_gen(self, phase: str, kind: str, label: str, *, track_timing: bool = True) -> None:
        """Mark the producer as actively building a segment (drives 'In produzione').

        Best-effort display state only — never gates the audio path.
        """
        now = time.monotonic()
        self._finish_render_stage(now)
        if not self._render_timing_started:
            self.begin_render_timing(kind, started=now)
        self.gen_phase, self.gen_kind, self.gen_label = phase, kind, label
        self.gen_started = now
        self._render_stage_name = (
            {
                "finding": "source",
                "writing": "script",
                "voicing": "tts",
                "mastering": "mix",
                "checking": "quality",
            }.get(phase, "")
            if track_timing
            else ""
        )
        self._render_stage_started = now if self._render_stage_name else 0.0

    def set_ha_context_refresh_stage(self, stage: str, *, started: float | None = None) -> None:
        """Set privacy-safe HA refresh stage telemetry from its coordinator."""
        normalized = stage if stage in HA_REFRESH_STAGES else "idle"
        self.ha_context_refresh_stage = normalized
        self.ha_context_refresh_stage_started_monotonic = (
            0.0 if normalized == "idle" else time.monotonic() if started is None else max(0.0, started)
        )

    def record_stream_pacing_event(
        self,
        kind: str,
        *,
        lateness_ms: float,
        remaining_lead_ms: float,
        segment_type: str,
        deficit_ms: float = 0.0,
        timestamp: float | None = None,
        monotonic_now: float | None = None,
    ) -> None:
        """Record one bounded pacing signal without retaining content or identity."""
        if kind not in STREAM_PACING_EVENT_KINDS:
            return
        ts = time.time() if timestamp is None else float(timestamp)
        mono = time.monotonic() if monotonic_now is None else float(monotonic_now)
        self.stream_pacing_counts[kind] = self.stream_pacing_counts.get(kind, 0) + 1
        if self._stream_pacing_window_events and self._stream_pacing_window_events[-1][1] == kind:
            previous_ts, _, previous_count = self._stream_pacing_window_events[-1]
            if ts - previous_ts <= 1.0:
                self._stream_pacing_window_events[-1] = (ts, kind, previous_count + 1)
            else:
                self._stream_pacing_window_events.append((ts, kind, 1))
        else:
            self._stream_pacing_window_events.append((ts, kind, 1))

        stage = self.ha_context_refresh_stage if self.ha_context_refresh_stage in HA_REFRESH_STAGES else "idle"
        stage_elapsed_ms = (
            max(0, round((mono - self.ha_context_refresh_stage_started_monotonic) * 1000))
            if stage != "idle" and self.ha_context_refresh_stage_started_monotonic > 0
            else 0
        )
        event = {
            "timestamp": ts,
            "kind": kind,
            "lateness_ms": max(0, round(float(lateness_ms), 1)),
            "remaining_lead_ms": max(0, round(float(remaining_lead_ms), 1)),
            "deficit_ms": max(0, round(float(deficit_ms), 1)),
            "segment_type": str(segment_type or "unknown"),
            "playback_epoch": int(self.playback_epoch),
            "listener_count": max(0, int(self.listeners_active)),
            "generator": {"phase": str(self.gen_phase or "idle"), "kind": str(self.gen_kind or "idle")},
            "ha_refresh": {
                "in_flight": bool(self.ha_context_refresh_in_flight),
                "foreground_timed_out": bool(self.ha_context_refresh_active_foreground_timed_out),
                "stage": stage,
                "stage_elapsed_ms": stage_elapsed_ms,
            },
            "count": 1,
        }
        if self.stream_pacing_events:
            previous = self.stream_pacing_events[-1]
            coalesce_keys = ("kind", "segment_type", "playback_epoch")
            same_context = all(previous.get(key) == event[key] for key in coalesce_keys)
            same_context = same_context and previous.get("generator") == event["generator"]
            same_context = same_context and previous.get("ha_refresh") == event["ha_refresh"]
            if same_context and ts - float(previous.get("timestamp", 0.0)) <= 1.0:
                previous["timestamp"] = ts
                previous["lateness_ms"] = max(previous.get("lateness_ms", 0.0), event["lateness_ms"])
                previous["remaining_lead_ms"] = min(
                    previous.get("remaining_lead_ms", event["remaining_lead_ms"]),
                    event["remaining_lead_ms"],
                )
                previous["deficit_ms"] = max(previous.get("deficit_ms", 0.0), event["deficit_ms"])
                previous["count"] = int(previous.get("count", 1)) + 1
                return
        self.stream_pacing_events.append(event)

    def record_stream_outcome(
        self,
        *,
        segment_type: str,
        result: str,
        bytes_sent: int,
        starting_listener_count: int,
        terminal_reason: str,
        timestamp: float | None = None,
    ) -> None:
        """Append one anonymous completed-send result to the bounded history."""
        reason = (
            terminal_reason
            if terminal_reason in {"eof", "skip", "file_error", "cancelled", "aborted"}
            else "file_error"
        )
        self.stream_outcome_history.append(
            {
                "timestamp": time.time() if timestamp is None else float(timestamp),
                "segment_type": str(segment_type or "unknown"),
                "result": str(result or "not_streamed"),
                "bytes_sent": max(0, int(bytes_sent)),
                "starting_listener_count": max(0, int(starting_listener_count)),
                "terminal_reason": reason,
            }
        )

    def record_slow_listener_drops(self, count: int = 1, *, timestamp: float | None = None) -> None:
        """Count queue-overflow drops without retaining which listener lagged."""
        amount = max(0, int(count))
        if amount <= 0:
            return
        ts = time.time() if timestamp is None else float(timestamp)
        self.slow_listener_drops_total += amount
        self.slow_listener_last_drop_at = ts
        if self._slow_listener_drop_events and ts - self._slow_listener_drop_events[-1][0] <= 1.0:
            previous_ts, previous_count = self._slow_listener_drop_events[-1]
            self._slow_listener_drop_events[-1] = (max(previous_ts, ts), previous_count + amount)
        else:
            self._slow_listener_drop_events.append((ts, amount))

    def stream_delivery_snapshot(self, *, now: float | None = None, monotonic_now: float | None = None) -> dict:
        """Return the zero-safe authenticated stream-delivery diagnostic shape."""
        ts = time.time() if now is None else float(now)
        mono = time.monotonic() if monotonic_now is None else float(monotonic_now)
        cutoff = ts - STREAM_DELIVERY_WINDOW_SECONDS
        window_counts = {kind: 0 for kind in STREAM_PACING_EVENT_KINDS}
        for event_ts, kind, count in self._stream_pacing_window_events:
            if event_ts >= cutoff and kind in window_counts:
                window_counts[kind] += count
        slow_window = sum(count for event_ts, count in self._slow_listener_drop_events if event_ts >= cutoff)
        stage = self.ha_context_refresh_stage if self.ha_context_refresh_stage in HA_REFRESH_STAGES else "idle"
        stage_elapsed_ms = (
            max(0, round((mono - self.ha_context_refresh_stage_started_monotonic) * 1000))
            if stage != "idle" and self.ha_context_refresh_stage_started_monotonic > 0
            else 0
        )
        session_counts = {kind: int(self.stream_pacing_counts.get(kind, 0)) for kind in STREAM_PACING_EVENT_KINDS}
        return {
            "target_lead_ms": 500,
            "late_threshold_ms": 50,
            "session": {**session_counts, "total": sum(session_counts.values())},
            "window_15m": {**window_counts, "total": sum(window_counts.values())},
            "recent": list(self.stream_pacing_events),
            "recent_stream_outcomes": list(self.stream_outcome_history),
            "slow_listener_drops": {
                "session": int(self.slow_listener_drops_total),
                "window_15m": int(slow_window),
                "last_drop_at": self.slow_listener_last_drop_at or None,
            },
            "ha_refresh": {
                "in_flight": bool(self.ha_context_refresh_in_flight),
                "foreground_timed_out": bool(self.ha_context_refresh_active_foreground_timed_out),
                "stage": stage,
                "stage_elapsed_ms": stage_elapsed_ms,
            },
        }

    def end_gen(self, ok: bool = True) -> None:
        """Clear the current production phase, pushing it onto the recent trail.

        ok=False records a blocked (✗) outcome for operator honesty. A crash that
        skips end_gen does not wedge anything: the next set_gen overwrites state.
        """
        now = time.monotonic()
        self._finish_render_stage(now)
        if self.gen_phase:
            self.gen_recent.appendleft(
                {"phase": self.gen_phase, "kind": self.gen_kind, "label": self.gen_label, "ok": ok}
            )
        self.gen_phase = self.gen_kind = self.gen_label = ""
        self.gen_started = 0.0

    def begin_render_timing(self, kind: str, *, started: float | None = None) -> None:
        """Begin one producer attempt; later stage timings remain best-effort."""
        # A recoverable branch can return to the producer loop without a single
        # shared ``finally``. Preserve that terminal evidence rather than
        # silently overwriting it when the next attempt starts. Close the
        # abandoned attempt at the new attempt's start so its elapsed time is
        # bounded by real work, not the wall clock at the next begin call.
        now = time.monotonic() if started is None else started
        if self._render_timing_started:
            self.finish_render_timing("failed", reason="abandoned", started=now)
        self._render_timing_started = now
        self._render_timing_kind = str(kind)
        self._render_timing_stages.clear()
        self._render_stage_started = 0.0
        self._render_stage_name = ""

    def add_render_stage_timing(self, stage: str, elapsed_ms: float) -> None:
        """Accumulate an independently measured diagnostic stage duration."""
        if not self._render_timing_started:
            return
        try:
            self._render_timing_stages[stage] = self._render_timing_stages.get(stage, 0.0) + max(0.0, elapsed_ms)
        except Exception:
            logger.debug("Render timing stage measurement failed", exc_info=True)

    def _finish_render_stage(self, now: float | None = None) -> None:
        if not self._render_stage_name or not self._render_stage_started:
            return
        current = time.monotonic() if now is None else now
        self.add_render_stage_timing(self._render_stage_name, (current - self._render_stage_started) * 1000)
        self._render_stage_started = 0.0
        self._render_stage_name = ""

    def finish_render_timing(self, outcome: str, *, reason: str = "", started: float | None = None) -> None:
        """Close the current producer attempt without allowing diagnostics to raise."""
        if not self._render_timing_started:
            return
        now = time.monotonic() if started is None else started
        self._finish_render_stage(now)
        self.record_render_timing(
            kind=self._render_timing_kind,
            outcome=outcome,
            total_elapsed_ms=(now - self._render_timing_started) * 1000,
            stages_ms=self._render_timing_stages,
            reason=reason,
        )
        self._render_timing_started = 0.0
        self._render_timing_kind = ""
        self._render_timing_stages.clear()

    def record_llm_usage(self, category: CostCategory, model: str, input_tokens: int, output_tokens: int) -> None:
        """Record one billable LLM usage event in aggregate and category counters.

        Keep the global counters and category split in one synchronous mutation so
        /status can reconcile raw units. This must be called where provider usage
        is reported, even if the generated JSON later fails and another provider
        fallback also bills.
        """
        if category not in LLM_COST_CATEGORIES:
            raise ValueError(f"Unknown LLM cost category: {category!r}")

        input_count = max(int(input_tokens or 0), 0)
        output_count = max(int(output_tokens or 0), 0)
        model_id = str(model or "unknown")

        self.api_calls += 1
        self.api_input_tokens += input_count
        self.api_output_tokens += output_count

        self.api_calls_by_category[category] = self.api_calls_by_category.get(category, 0) + 1

        model_bucket = self.api_tokens_by_model.setdefault(model_id, {"input": 0, "output": 0})
        model_bucket["input"] += input_count
        model_bucket["output"] += output_count

        category_models = self.api_tokens_by_category_model.setdefault(category, {})
        category_bucket = category_models.setdefault(model_id, {"input": 0, "output": 0})
        category_bucket["input"] += input_count
        category_bucket["output"] += output_count

    def record_tts_usage(self, characters: int) -> None:
        """Record paid cloud TTS characters without risking the audio path."""
        try:
            count = max(int(characters or 0), 0)
            if count <= 0:
                return
            self.tts_characters += count
            self.tts_characters_by_category[TTS_COST_CATEGORY] = (
                self.tts_characters_by_category.get(TTS_COST_CATEGORY, 0) + count
            )
        except Exception:
            return

    def record_bridge_fire(self, bridge_type: str, source: str, timestamp: float | None = None) -> None:
        """Record one producer rescue-bridge fire after a successful enqueue.

        Best-effort observability for #547 — never gates the audio path. Called
        once per bridge that actually queued rescue audio:

            bridge_type ∈ {"drain", "resume", "idle"}   (which rescue site fired)
            source      ∈ {"canned", "norm_cache", "emergency_tone"}  (what aired)

        bridge_fires_total is the lifetime session count; bridge_events is a
        bounded trail (maxlen=50) that the runtime status snapshot windows to
        decide whether the station is "running on rescue".
        """
        ts = timestamp if timestamp is not None else time.time()
        self.bridge_fires_total += 1
        if bridge_type in self.bridge_fires_by_type:
            self.bridge_fires_by_type[bridge_type] += 1
        self.bridge_events.append({"bridge_type": bridge_type, "source": source, "timestamp": ts})

    def record_discard(
        self,
        segment: Segment,
        reason: str,
        timestamp: str | float | None = None,
        *,
        already_counted_in_produced: bool = False,
    ) -> None:
        """Record one generated segment discarded before it started broadcasting.

        Best-effort observability — never gates the audio path. Called at every
        pre-air drop site (stale gates, queue purges, operator actions). Lifetime
        totals survive deque eviction; discard_events backs the rolling-window
        waste readout in admin Runtime Status.
        """
        # Semantic settlement is not telemetry. Do it before every best-effort
        # observer below so queue removal, overflow, mode changes, and playback
        # rejection cannot leave a claimed companionship cue retryable.
        metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
        if metadata.get("listener_session_cue") == "companionship":
            cue_epoch = metadata.get("listener_session_epoch")
            if isinstance(cue_epoch, int) and not isinstance(cue_epoch, bool):
                self.listener_session.abandon_companionship(cue_epoch)
        try:
            # Isolated from the accounting body below: a director bug must not
            # skip the waste telemetry for this discard (mirrors the guard in
            # on_stream_segment around activate()).
            director = self.home_context_director
            home_fact_id = str(metadata.get("home_fact_id") or "")
            # Only a segment carrying a home fact ever holds a reservation. Gate on
            # its id so an ordinary segment's queue_id can never match and release
            # an unrelated fact via the fact_id=None wildcard.
            if director is not None and home_fact_id:
                director.release(str(metadata.get("queue_id") or ""), fact_id=home_fact_id)
        except Exception:
            logging.getLogger("mammamiradio.home_context_director").debug(
                "Home context director release failed", exc_info=True
            )
        try:
            ts = timestamp if timestamp is not None else time.time()
            duration = float(segment.duration_sec or 0.0)
            seg_type = segment.type.value
            self.discarded_segments_total += 1
            self.discarded_duration_total_sec += duration
            if not already_counted_in_produced:
                self.discarded_unproduced_segments_total += 1
            self.discard_by_reason[reason] = self.discard_by_reason.get(reason, 0) + 1
            self.discard_by_type[seg_type] = self.discard_by_type.get(seg_type, 0) + 1
            self.discard_events.append(
                {
                    "reason": reason,
                    "type": seg_type,
                    "duration_sec": duration,
                    "timestamp": ts,
                    "already_counted_in_produced": already_counted_in_produced,
                }
            )
            campaign = getattr(self, "release_campaign", None)
            if campaign is not None:
                try:
                    if campaign.record_queue_discard(segment.metadata or {}):
                        campaign.save_if_dirty()
                except Exception:
                    pass
        except Exception:
            pass

    def record_render_timing(
        self,
        *,
        kind: str,
        outcome: str,
        total_elapsed_ms: float,
        stages_ms: dict[str, float] | None = None,
        reason: str = "",
        timestamp: float | None = None,
    ) -> None:
        """Record one bounded, best-effort producer timing result.

        Stage durations are independently measured and may overlap, so consumers
        must not infer that their sum equals wall-clock elapsed time.  This helper
        deliberately swallows malformed diagnostics to keep the audio path safe.
        """
        try:
            if outcome not in {"produced", "discarded", "failed"}:
                return
            allowed = {"source", "normalize", "script", "tts", "mix", "quality", "egress", "admission"}
            stages: dict[str, int] = {}
            for name, value in (stages_ms or {}).items():
                if name not in allowed:
                    continue
                elapsed = max(0, round(float(value)))
                stages[name] = elapsed
            entry = {
                "timestamp": (
                    datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
                    if timestamp is None
                    else timestamp
                ),
                "kind": str(kind),
                "outcome": outcome,
                "total_elapsed_ms": max(0, round(float(total_elapsed_ms))),
                "stages_ms": stages,
            }
            if outcome != "produced" and reason:
                entry["reason"] = str(reason)
            self.render_timings.appendleft(entry)
            logger.info(
                "render_timing kind=%s outcome=%s total_elapsed_ms=%s stages_ms=%s reason=%s",
                entry["kind"],
                entry["outcome"],
                entry["total_elapsed_ms"],
                entry["stages_ms"],
                entry.get("reason", ""),
            )
        except Exception:
            logger.debug("Render timing event failed", exc_info=True)

    def update_runtime_provider(
        self,
        provider_class: str,
        *,
        current_provider: str,
        primary_provider: str,
        fallback_active: bool,
        reason: str,
        event: str = "provider_switch_event",
        timestamp: float | None = None,
    ) -> RuntimeProviderEvent | None:
        """Record a bounded provider transition when runtime truth changes."""
        now = time.time() if timestamp is None else timestamp
        previous = self.runtime_provider_state.get(provider_class, {})
        previous_provider = str(previous.get("current_provider") or "")
        previous_fallback = bool(previous.get("fallback_active", False))
        previous_switch_timestamp = previous.get("last_switch_timestamp")
        changed = (
            previous_provider != current_provider or previous_fallback != fallback_active
            if previous
            else fallback_active or current_provider != primary_provider
        )

        self.runtime_provider_state[provider_class] = {
            "current_provider": current_provider,
            "primary_provider": primary_provider,
            "fallback_active": fallback_active,
            "reason": reason,
            "last_observed": now,
            "last_switch_timestamp": now if changed else previous_switch_timestamp,
        }
        if not changed:
            return None

        entry = RuntimeProviderEvent(
            event=event,
            provider_class=provider_class,
            from_provider=previous_provider or primary_provider,
            to_provider=current_provider,
            reason=reason,
            fallback_active=fallback_active,
            timestamp=now,
        )
        self.runtime_events.append(entry)
        return entry

    def switch_playlist(self, tracks: list[Track], source: PlaylistSource | None = None) -> None:
        """Replace the active playlist and bump revision counter.

        In-flight producer segments are discarded on next commit check.
        """
        self.playlist_revision += 1
        self.source_revision += 1
        self.playlist = tracks
        self.playlist_source = source
        self.startup_source_error = ""
        self.songs_since_banter = 0
        self.songs_since_ad = 0
        self.songs_since_news = 0
        # Clear play history so diversity filters start fresh for the new
        # playlist context.  Without this, a 20-track playlist loops after
        # ~30-40 min because the deque fills and recency weights flatten.
        self.played_tracks.clear()
        self.played_track_log.clear()
        # Clear listener requests and pinned track so in-flight background
        # download tasks from the old source can't zombie-pin a track into
        # the new playlist context. Keep an admin-visible trail so accepted
        # listener requests never disappear without an outcome.
        self._mark_pending_requests_source_changed()
        self.pending_actions.clear()
        self._listener_request_rl.clear()
        self.pinned_track = None
        self.force_next = None
        self.operator_force_pending = None
        self.heading = None
        self.heading_revision += 1
        self.heading_pending_announcement = ""
        self.heading_pending_narration_kind = ""
        self.heading_announced_id = ""

    def _mark_pending_requests_source_changed(self) -> None:
        if not self.pending_requests:
            return
        now = time.time()
        for request in self.pending_requests:
            self.recently_consumed_requests.append(
                {
                    "id": request.get("request_id") or str(request.get("ts", "")),
                    "name": request.get("name"),
                    "message": request.get("message") or request.get("text"),
                    "song_track": request.get("song_track"),
                    "type": request.get("type"),
                    "status": "source_changed",
                    "song_error_reason": "",
                    "consumed_at": now,
                }
            )
        cutoff = now - RECENTLY_CONSUMED_RETENTION_SECONDS
        self.recently_consumed_requests = [
            request for request in self.recently_consumed_requests if request.get("consumed_at", 0) >= cutoff
        ]
        self.pending_requests.clear()

    def _arm_heading_announcement_if_needed(self, track: Track) -> None:
        heading = self.heading
        if heading is None or not heading.id or self.heading_pending_announcement:
            return
        if track.heading_id == heading.id:
            if heading.phase == "hunting":
                heading.phase = "steering"
            if heading.first_found_at <= 0:
                heading.first_found_at = time.time()
            if heading.announced or self.heading_announced_id == heading.id:
                now = time.time()
                if heading.narration_count > 0 and now - heading.last_narrated_at >= 1800:
                    self.heading_pending_announcement = heading.label
                    self.heading_pending_narration_kind = "crate_beat"
                return
            self.heading_pending_announcement = heading.label
            self.heading_pending_narration_kind = "first_found"

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

    def on_stream_segment(self, segment: Segment) -> None:
        """Called by the streamer when it starts sending a segment to the listener."""
        now = time.time()
        try:
            director = self.home_context_director
            metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
            home_fact_id = str(metadata.get("home_fact_id") or "")
            # Only a home-fact segment holds a reservation; gate on its id so an
            # ordinary segment can never activate an unrelated fact's cooldown.
            if director is not None and home_fact_id:
                director.activate(str(metadata.get("queue_id") or ""), fact_id=home_fact_id)
        except Exception:
            logging.getLogger("mammamiradio.home_context_director").debug(
                "Home context director activation failed", exc_info=True
            )
        self.playback_epoch += 1
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
        # Track only ordinary canned banter at stream time. Packaged recovery
        # speech is operational safety audio, never shareware trial content.
        if segment.type == SegmentType.BANTER and segment.metadata.get("canned") and not segment.metadata.get("rescue"):
            self.canned_clips_streamed += 1
        raw_audio_source = str(segment.metadata.get("audio_source") or "")
        fallback_active = is_fallback_active(segment.metadata)
        if raw_audio_source or segment.metadata.get("fallback") or fallback_active or segment.type == SegmentType.MUSIC:
            audio_source = raw_audio_source
            if not audio_source and fallback_active:
                audio_source = "canned"
            elif (
                segment.type == SegmentType.MUSIC
                and self.playlist_source is not None
                and (not audio_source or (not fallback_active and audio_source == "download"))
            ):
                audio_source = self.playlist_source.kind
            self.update_runtime_provider(
                "audio_source",
                current_provider=audio_source or "stream",
                primary_provider=self.playlist_source.kind if self.playlist_source is not None else "stream",
                fallback_active=fallback_active,
                reason=(
                    str(segment.metadata.get("fallback_reason") or "Fallback audio is currently on air")
                    if fallback_active
                    else "Primary audio source is on air"
                ),
                timestamp=now,
            )
        # Moment Receipts: a home-triggered segment just started streaming.
        # Provisional (send-start, not delivery proof) — the playback loop's
        # finally records the true outcome via classify_stream_outcome. Rescue
        # and fallback fills never carry a real moment, and must never mint a
        # receipt even if their metadata leaks a stale id.
        if self.moment_store is not None and not fallback_active and not segment.metadata.get("rescue"):
            try:
                for _moment_key in ("ritual_moment_id", "gag_moment_id"):
                    _moment_id = segment.metadata.get(_moment_key)
                    if _moment_id:
                        self.moment_store.mark_airing(str(_moment_id), now=now)
            except Exception:  # pragma: no cover - receipts must never break audio
                logging.getLogger("mammamiradio.moment_receipts").debug(
                    "Moment receipt airing mark failed", exc_info=True
                )
        # Only add to studio-bleed pool once banter truly starts streaming.
        if segment.type == SegmentType.BANTER and not segment.metadata.get("canned"):
            self.recent_banter_paths.append(segment.path)
        if segment.type == SegmentType.MUSIC:
            title = str(segment.metadata.get("title_only") or segment.metadata.get("title") or "")
            artist = str(segment.metadata.get("artist") or "")
            if " – " in title and not artist:
                artist, title = title.split(" – ", 1)
            duration_ms = segment.metadata.get("duration_ms")
            if not isinstance(duration_ms, int):
                duration_ms = int(max(segment.duration_sec, 0.0) * 1000)
            title_key = title.strip().lower()
            label_key = str(label).strip().lower()
            placeholder_titles = {"", "music", "unknown", "unknown title", "untitled", "none"}
            has_real_title = title_key not in placeholder_titles and not (
                title_key == label_key and label_key in placeholder_titles
            )
            if not segment.metadata.get("error") and not fallback_active and duration_ms > 0 and has_real_title:
                self.played_track_log.append(
                    PlayedEntry(
                        track=Track(
                            title=title,
                            artist=artist,
                            duration_ms=duration_ms,
                            spotify_id=str(segment.metadata.get("spotify_id") or ""),
                            youtube_id=str(segment.metadata.get("youtube_id") or ""),
                            album_art=str(segment.metadata.get("album_art") or ""),
                            source=segment.metadata.get("source_kind") or "youtube",
                        ),
                        played_at=time.monotonic(),
                    )
                )
        self.now_streaming = {
            "type": seg_type,
            "label": label,
            "started": now,
            "epoch": self.playback_epoch,
            "duration_sec": segment.duration_sec,
            "metadata": segment.metadata,
        }
        self.last_state_change_at = now
        self.stream_log.append(
            SegmentLogEntry(
                type=seg_type,
                label=label,
                timestamp=now,
                metadata=segment.metadata,
                duration_sec=segment.duration_sec,
            )
        )

    def reserve_next_track(self) -> Track:
        """Legacy round-robin rotation — use select_next_track() for weighted shuffle."""
        if not self.playlist:
            raise RuntimeError("Playlist is empty")
        track = self.playlist.pop(0)
        self.playlist.append(track)
        return track

    def select_next_track(
        self,
        *,
        allow_explicit: bool = True,
        repeat_cooldown: int = 8,
        artist_cooldown: int = 3,
        max_artist_per_hour: int = 3,
        excluded_cache_keys: Collection[str] | None = None,
    ) -> Track:
        """Pick the next track using weighted random selection with diversity rules.

        Hard filters remove ineligible tracks, then soft weights bias toward
        tracks that haven't played recently, from under-represented artists,
        and with smooth energy transitions.  Falls back to progressively
        relaxed filters if the pool is too small.
        """
        if not self.playlist:
            raise RuntimeError("Playlist is empty")

        excluded = set(excluded_cache_keys or ())

        if self.pinned_track is not None:
            track = self.pinned_track
            self.pinned_track = None
            if track.cache_key not in excluded:
                return track
            if not any(candidate.cache_key not in excluded for candidate in self.playlist):
                raise RuntimeError("Playlist has no eligible tracks")

        pool = [track for track in self.playlist if track.cache_key not in excluded]
        if not pool:
            raise RuntimeError("Playlist has no eligible tracks")

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
            # Relax: drop hourly cap but keep repeat + artist cooldown
            candidates = [t for t in pool if t.cache_key not in recent_keys and t.artist not in recent_artist_set]
            if not allow_explicit:
                candidates = [t for t in candidates if not t.explicit]
        if not candidates:
            # Further relax: drop artist cooldown but keep repeat cooldown
            candidates = [t for t in pool if t.cache_key not in recent_keys]
            if not allow_explicit:
                candidates = [t for t in candidates if not t.explicit]
        if not candidates:
            # Final fallback: pick the track played least recently to minimise
            # audible repeats.  Never just random from the full pool — that
            # lets a song play twice in quick succession on small playlists.
            def _staleness(t: Track) -> int:
                # Higher = played longer ago (or never played)
                if t.cache_key not in last_play_pos:
                    return n_played + 1  # never played = most stale
                return n_played - last_play_pos[t.cache_key]

            candidates = [max(pool, key=_staleness)]

        # --- Soft weights (all lookups are O(1) via dicts built in the single pass above) ---
        # Pass 1: base weight per candidate (everything EXCEPT the Record Hunt lift), plus
        # the heading-match flag and the split base-weight sums the adaptive lift needs.
        heading = self.heading
        preference_scores = preference_score_map(self.song_preferences)
        base_weights: list[float] = []
        heading_flags: list[bool] = []
        sum_heading_base = 0.0
        sum_other_base = 0.0
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
                w *= 0.05  # Near-zero: effectively blocked unless pool is tiny
            elif recent_artist_count == 1:
                w *= 0.4

            # Popularity boost: slight preference for popular tracks
            if track.popularity:
                w *= 0.8 + 0.2 * (track.popularity / 100.0)

            heading_match = bool(heading is not None and heading.id and track.heading_id == heading.id)
            score = preference_scores.get(normalized_track_key(track), 0)
            # A thumbs-down never fights an active Record Hunt: clamp a negative
            # preference to neutral for heading matches (unchanged from before).
            if score < 0 and heading_match:
                score = 0
            w *= preference_weight(score)

            base_weights.append(w)
            heading_flags.append(heading_match)
            if heading_match:
                sum_heading_base += w
            else:
                sum_other_base += w

        # Record Hunt is steering, not queue control: matching records get an adaptive
        # lift sized so the hunt set reliably lands ~HEADING_TARGET_SHARE of picks
        # regardless of how big the rotation pool is (a fixed xN is inaudible in a
        # 200-track pool). Cooldowns, bans, pinned tracks, and diversity still win —
        # they run as hard filters before we ever weight, and the lift only rebalances
        # whatever survived. heading_lift is clamped to [HEADING_MIN_LIFT, HEADING_MAX_LIFT]
        # so a small pool keeps the historical x4 floor and a tiny hunt set can never make
        # one song dominate the station.
        if sum_heading_base <= 0.0 or sum_other_base <= 0.0:
            heading_lift = HEADING_MIN_LIFT
        else:
            computed = (HEADING_TARGET_SHARE / (1.0 - HEADING_TARGET_SHARE)) * (sum_other_base / sum_heading_base)
            heading_lift = min(HEADING_MAX_LIFT, max(HEADING_MIN_LIFT, computed))

        # Pass 2: apply the lift to heading matches and floor to avoid zero weights.
        weights = [
            max(w * (heading_lift if is_heading else 1.0), 0.01)
            for w, is_heading in zip(base_weights, heading_flags, strict=False)
        ]

        selected = random.choices(candidates, weights=weights, k=1)[0]
        return selected

    def after_music(self, track: Track) -> None:
        """Advance state after successfully queuing a music segment."""
        self.played_tracks.append(track)
        self.current_track = track
        heading = self.heading
        spent_heading: Heading | None = None
        if heading is not None and heading.id and track.heading_id == heading.id:
            heading.selection_spent += 1
            spent_heading = heading
        if spent_heading is not None and self.heading_persist_callback is not None:
            try:
                self.heading_persist_callback(spent_heading)
            except Exception:
                # Persistence is best-effort; audio admission already succeeded.
                pass
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
        environment: str = "",
        music_bed: str = "",
        transition_motif: str = "",
    ) -> None:
        """Record a single ad spot in history (called per-spot within a break)."""
        self.ad_history.append(
            AdHistoryEntry(
                brand=brand,
                summary=summary,
                timestamp=time.time(),
                format=format,
                sonic_signature=sonic_signature,
                environment=environment,
                music_bed=music_bed,
                transition_motif=transition_motif,
            )
        )

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
        if joke not in self.running_jokes:
            self.running_jokes.append(joke)


@dataclass(frozen=True)
class Capabilities:
    """Runtime capability flags derived from config + live state.

    Three-tier system: Demo Radio → Full AI Radio → Connected Home.
    Music source is no longer a tier gate (always available via local + yt-dlp + charts).
    """

    llm: bool = False
    """Any LLM API key available (Anthropic or OpenAI) for AI-generated banter and ads."""

    ha: bool = False
    """Home Assistant token present and integration enabled."""

    home_context_ready: bool = False
    """A prompt-safe Home Assistant context slice is available."""

    home_context_enabled: bool = False
    """Home Assistant context polling/review is enabled when HA access exists."""

    jamendo: bool = False
    """Jamendo source is configured with a client ID."""

    charts_reload: bool = False
    """Chart reloads are available because yt-dlp is enabled and charts are configured."""

    tts_degraded: bool = False
    """True when TTS was substituted at config load or during live synthesis."""

    @property
    def tier(self) -> str:
        """Derive a human-friendly tier label from capability flags."""
        if self.llm and self.home_context_ready:
            return "connected_home"
        if self.llm:
            return "full_ai"
        return "demo"

    @property
    def tier_label(self) -> str:
        """Display name for the current tier."""
        return {
            "connected_home": "Connected Home",
            "full_ai": "Full AI Radio",
            "demo": "Demo Radio",
        }[self.tier]
