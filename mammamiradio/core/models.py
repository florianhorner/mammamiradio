"""Core data models shared across playback, scripting, and streaming."""

from __future__ import annotations

import asyncio
import math
import random
import re
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, TypedDict
from urllib.parse import urlsplit, urlunsplit

from mammamiradio.core.segment_status import is_fallback_active

if TYPE_CHECKING:
    from mammamiradio.home.evening_memory import EveningLedger
    from mammamiradio.hosts.persona import PersonaStore


PartyMode = Literal["festival"]


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
    summary: str
    device_class: object


class ExternalAddNotice(TypedDict):
    """A failed/dropped background queue-from-search outcome surfaced in /status."""

    display: str
    ok: bool
    reason: str
    ts: float


RECENTLY_CONSUMED_RETENTION_SECONDS = 300


class ConsumedListenerRequest(TypedDict):
    """A consumed listener request retained for 5-minute admin visibility."""

    id: str
    name: str | None
    message: str | None
    song_track: str | None
    type: str | None
    status: str  # "sent_to_hosts" | "song_not_found"
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
    playlist_source: PlaylistSource | None = None
    startup_source_error: str = ""
    # What the listener is hearing RIGHT NOW
    now_streaming: dict = field(default_factory=dict)
    # Pre-produced segments waiting to play (shadow of asyncio.Queue for UI display)
    queued_segments: list[dict] = field(default_factory=list)
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
    # Impossible Moments v2 (A): one rendered evening running-gag for the next
    # banter (consumed after one use); populated by the producer from the ledger.
    ha_running_gag: str = ""
    # Ledger bucket key for the offered gag, so the producer can spend its
    # cooldown (mark_spoken) only after generated banter actually airs.
    ha_running_gag_key: str = ""
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
    ha_context_last_updated: float = 0.0
    ha_context_entity_count: int = 0
    ha_context_char_count: int = 0
    ha_first_home_context_moment_fired: bool = False
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
    # Consumption metrics
    api_calls: int = 0
    api_input_tokens: int = 0
    api_output_tokens: int = 0
    # Per-model token tallies (model_id → {"input": n, "output": n}) so the cost
    # counter prices each model it actually used, not a flat single rate. Dynamic
    # routing means different segments run different models within one session.
    api_tokens_by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    tts_characters: int = 0
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
    # Listener connection tracking for "impossible moments"
    listeners_active: int = 0
    listeners_peak: int = 0
    listeners_total: int = 0
    new_listeners_pending: int = 0
    queue_empty_since: float | None = None
    # Runtime integrity counters for long-lived sessions
    runtime_sync_events: int = 0
    shadow_queue_corrections: int = 0
    playback_epoch: int = 0
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

    def set_gen(self, phase: str, kind: str, label: str) -> None:
        """Mark the producer as actively building a segment (drives 'In produzione').

        Best-effort display state only — never gates the audio path.
        """
        self.gen_phase, self.gen_kind, self.gen_label = phase, kind, label
        self.gen_started = time.monotonic()

    def end_gen(self, ok: bool = True) -> None:
        """Clear the current production phase, pushing it onto the recent trail.

        ok=False records a blocked (✗) outcome for operator honesty. A crash that
        skips end_gen does not wedge anything: the next set_gen overwrites state.
        """
        if self.gen_phase:
            self.gen_recent.appendleft(
                {"phase": self.gen_phase, "kind": self.gen_kind, "label": self.gen_label, "ok": ok}
            )
        self.gen_phase = self.gen_kind = self.gen_label = ""
        self.gen_started = 0.0

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
        # the new playlist context.
        self.pending_requests.clear()
        self.pending_actions.clear()
        self._listener_request_rl.clear()
        self.pinned_track = None
        self.force_next = None
        self.operator_force_pending = None

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
        # Track canned banter clips at stream time (shareware trial)
        if segment.metadata.get("canned"):
            self.canned_clips_streamed += 1
        raw_audio_source = str(segment.metadata.get("audio_source") or "")
        if raw_audio_source or segment.metadata.get("fallback") or segment.type == SegmentType.MUSIC:
            fallback_active = is_fallback_active(segment.metadata)
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
            if not segment.metadata.get("error") and duration_ms > 0 and has_real_title:
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
    ) -> Track:
        """Pick the next track using weighted random selection with diversity rules.

        Hard filters remove ineligible tracks, then soft weights bias toward
        tracks that haven't played recently, from under-represented artists,
        and with smooth energy transitions.  Falls back to progressively
        relaxed filters if the pool is too small.
        """
        if not self.playlist:
            raise RuntimeError("Playlist is empty")

        if self.pinned_track is not None:
            track = self.pinned_track
            self.pinned_track = None
            return track

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
                w *= 0.05  # Near-zero: effectively blocked unless pool is tiny
            elif recent_artist_count == 1:
                w *= 0.4

            # Popularity boost: slight preference for popular tracks
            if track.popularity:
                w *= 0.8 + 0.2 * (track.popularity / 100.0)

            weights.append(max(w, 0.01))  # Floor to avoid zero weights

        return random.choices(candidates, weights=weights, k=1)[0]

    def after_music(self, track: Track) -> None:
        """Advance state after successfully queuing a music segment."""
        self.played_tracks.append(track)
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

    jamendo: bool = False
    """Jamendo source is configured with a client ID."""

    charts_reload: bool = False
    """Chart reloads are available because yt-dlp is enabled and charts are configured."""

    tts_degraded: bool = False
    """True when one or more configured TTS voices were replaced with a fallback at config load."""

    @property
    def tier(self) -> str:
        """Derive a human-friendly tier label from capability flags."""
        if self.llm and self.ha:
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
