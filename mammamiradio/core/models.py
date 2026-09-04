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
from collections.abc import Callable, Collection, Iterator, Mapping
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, NotRequired, TypedDict
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from mammamiradio.core.listener_session import ListenerSession
from mammamiradio.core.segment_status import is_fallback_active
from mammamiradio.core.song_identity import (
    normalize_song_identity_key,
    song_identity_keys_match,
)
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

_RUNTIME_PROVIDER_OBSERVATION_TOKEN: ContextVar[str] = ContextVar(
    "mammamiradio_runtime_provider_observation_token",
    default="",
)

# Internal render-scoped identity for the playlist source that produced a
# segment.  The active station source may change while already-rendered audio
# remains queued, so listener-audible provider truth must travel with the audio
# rather than being reconstructed from mutable global state.
SEGMENT_PLAYLIST_SOURCE_KIND_KEY = "_playlist_source_kind"


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
    LISTENER_REQUEST_RESERVED = "listener_request_reserved"
    OPERATOR_STOP = "operator_stop"
    OPERATOR_PANIC = "operator_panic"
    OPERATOR_PURGE = "operator_purge"
    SOURCE_SWITCH = "source_switch"
    OPERATOR_BAN = "operator_ban"
    OPERATOR_QUEUE_REMOVE = "operator_queue_remove"
    PLAYBACK_FILE_ERROR = "playback_file_error"
    PLAYBACK_ADMISSION_DENIED = "playback_admission_denied"
    STALE_PLAYED_TRACK_REF = "stale_played_track_ref"
    LISTENER_SESSION_STALE = "listener_session_stale"


class StarterCycleReservationPendingError(RuntimeError):
    """The current starter cycle is fully reserved by queued segments.

    This is a transient scheduling condition, not an unavailable-media error:
    the producer must wait for one queued starter to begin playback or be
    released instead of filling the next cycle early.
    """


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


@dataclass(frozen=True)
class MediaAttribution:
    """Immutable, listener-safe music provenance facts.

    This describes what a source or committed manifest reports; it is not a
    legal-clearance verdict. Provider-private stream URLs and credentials never
    belong in this object.
    """

    provider: str
    license_id: str
    license_url: str
    source_url: str
    credit: str
    modified: bool
    basis: Literal["bundled_manifest", "provider_reported"]

    def to_dict(self) -> dict[str, str | bool]:
        """Return the stable additive shape used by public serializers."""
        return asdict(self)


def safe_media_attribution_dict(value: MediaAttribution | Mapping | None) -> dict[str, str | bool] | None:
    """Validate and bound one attribution object before it crosses a public boundary.

    Attribution links are clickable on unauthenticated surfaces.  Keep the
    currently supported provider contracts explicit here so a malformed
    segment cannot turn an audio download URL, token-bearing link, or arbitrary
    HTTPS host into a listener-facing link.
    """
    raw = value.to_dict() if isinstance(value, MediaAttribution) else value
    if not isinstance(raw, Mapping):
        return None
    basis = raw.get("basis")
    if basis not in {"bundled_manifest", "provider_reported"}:
        return None

    def _text(name: str, limit: int) -> str | None:
        candidate = raw.get(name)
        if not isinstance(candidate, str):
            return None
        candidate = candidate.strip()
        return candidate if 0 < len(candidate) <= limit else None

    def _url(name: str) -> tuple[str, object] | None:
        candidate = _text(name, 512)
        if candidate is None:
            return None
        try:
            parsed = urlsplit(candidate)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
                or parsed.port not in (None, 443)
                or "\\" in candidate
                or any(ord(char) < 32 for char in candidate)
                or "%2e" in parsed.path.lower()
                or any(part == ".." for part in parsed.path.split("/"))
            ):
                return None
        except ValueError:
            return None
        return candidate, parsed

    provider = _text("provider", 64)
    license_id = _text("license_id", 64)
    credit = _text("credit", 512)
    license_result = _url("license_url")
    source_result = _url("source_url")
    modified = raw.get("modified")
    if (
        None in {provider, license_id, credit}
        or license_result is None
        or source_result is None
        or not isinstance(modified, bool)
    ):
        return None
    assert provider is not None
    assert license_id is not None
    assert credit is not None
    license_url, _license_parts = license_result
    source_url, source_parts = source_result

    allowed_licenses = {
        "https://creativecommons.org/licenses/by/3.0/": "CC-BY-3.0",
        "https://creativecommons.org/licenses/by/4.0/": "CC-BY-4.0",
    }
    if allowed_licenses.get(license_url) != license_id:
        return None

    source_host = str(getattr(source_parts, "hostname", "") or "").lower().rstrip(".")
    source_path = str(getattr(source_parts, "path", "") or "")
    source_query = str(getattr(source_parts, "query", "") or "")
    if provider == "incompetech" and basis == "bundled_manifest":
        if (
            source_host not in {"incompetech.com", "www.incompetech.com"}
            or not source_path.startswith("/music/royalty-free/")
            or (source_query and re.fullmatch(r"isrc=[A-Z0-9]{12}", source_query) is None)
            or license_id != "CC-BY-4.0"
        ):
            return None
    elif provider == "jamendo" and basis in {"provider_reported", "bundled_manifest"}:
        # `bundled_manifest` is the packaged crate, `provider_reported` the
        # transient runtime source. Both point at a jamendo.com track page; the
        # difference is who vouched for it, not what it looks like.
        #
        # Bundled Jamendo tracks are CC BY 3.0 — Jamendo publishes no 4.0 at all
        # — so this branch must not inherit the 4.0-only rule the Incompetech
        # branch applies. Dropping them here is what silently strips the credit
        # from half the crate, and CC BY requires that credit to accompany the
        # work.
        if (
            source_host not in {"jamendo.com", "www.jamendo.com"}
            or not source_path.startswith("/track/")
            or source_query
        ):
            return None
    else:
        return None

    return {
        "provider": provider,
        "license_id": license_id,
        "license_url": license_url,
        "source_url": source_url,
        "credit": credit,
        "modified": modified,
        "basis": basis,
    }


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
    source: Literal["youtube", "jamendo", "local", "demo", "classic", "starter"] = "youtube"
    heading_id: str = ""
    provider_track_id: str = ""
    attribution: MediaAttribution | None = None

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
            jamendo_id = self.provider_track_id.strip()
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
        """Stored literal key used by preferences, dedupe, and persisted bans."""
        return song_identity_key(self.artist, self.title)


def song_identity_key(artist: object, title: object) -> tuple[str, str]:
    """Build the stored literal song key from a raw artist/title pair.

    The single key definition for callers that hold two fields rather than a
    :class:`Track` — listener-request blocklist aliases, sidecars, and segment
    metadata all have to mean the same thing by "the same song", and every
    hand-rolled ``strip().lower()`` copy is a place that meaning can drift.
    """
    return (str(artist or "").strip().lower(), str(title or "").strip().lower())


def normalized_track_key(track: Track) -> tuple[str, str]:
    """Return the stored literal key used by preferences, dedupe, and bans."""
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


SOURCE_READINESS_KINDS: tuple[str, ...] = ("charts", "jamendo", "local", "demo", "recovery")
_SOURCE_READINESS_ALIASES = {
    "youtube": "charts",
    "chart": "charts",
    "charts": "charts",
    "jamendo": "jamendo",
    "local": "local",
    "demo": "demo",
    "recovery": "recovery",
    "continuity": "recovery",
    "canned": "recovery",
    "norm_cache": "recovery",
    "emergency_tone": "recovery",
}
_SOURCE_EVIDENCE_LIMIT = 10_000
_SOURCE_FAILURE_LIMIT = 160


def canonical_source_readiness_kind(kind: object) -> str:
    """Map runtime source labels onto the bounded first-listen source set."""
    return _SOURCE_READINESS_ALIASES.get(str(kind or "").strip().lower(), "")


def _bounded_source_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(parsed, _SOURCE_EVIDENCE_LIMIT))


def _safe_source_failure(value: object) -> str:
    return " ".join(str(value or "").split())[:_SOURCE_FAILURE_LIMIT]


@dataclass
class SourceReadinessEntry:
    """Bounded event evidence for one human-facing music source."""

    kind: str
    label: str = ""
    configured: bool = False
    attempted: bool = False
    candidates: int = 0
    playable: int = 0
    on_air: bool = False
    exhausted: bool = False
    failure: str = ""
    bundled: bool | None = None

    def clone(self) -> SourceReadinessEntry:
        return SourceReadinessEntry(**asdict(self))


def _empty_source_entries() -> dict[str, SourceReadinessEntry]:
    return {kind: SourceReadinessEntry(kind=kind) for kind in SOURCE_READINESS_KINDS}


@dataclass
class SourceReadinessEvidence:
    """Single in-memory owner for source truth, scoped to a source revision."""

    source_revision: int = 0
    entries: dict[str, SourceReadinessEntry] = field(default_factory=_empty_source_entries)
    current_rotation_kind: str = ""
    current_rotation_label: str = ""
    advanced: SourceReadinessEntry | None = None

    def clone_for_revision(self, source_revision: int) -> SourceReadinessEvidence:
        clone = SourceReadinessEvidence(source_revision=max(0, int(source_revision)))
        clone.entries = {
            kind: self.entries.get(kind, SourceReadinessEntry(kind=kind)).clone() for kind in SOURCE_READINESS_KINDS
        }
        clone.current_rotation_kind = self.current_rotation_kind
        clone.current_rotation_label = self.current_rotation_label
        clone.advanced = self.advanced.clone() if self.advanced is not None else None
        clone.clear_on_air()
        return clone

    def has_signal(self) -> bool:
        return bool(
            self.current_rotation_kind
            or self.advanced is not None
            or any(
                entry.configured
                or entry.attempted
                or entry.candidates
                or entry.playable
                or entry.on_air
                or entry.exhausted
                or entry.failure
                or entry.bundled is not None
                for entry in self.entries.values()
            )
        )

    def configure(self, kind: object, configured: bool = True, *, bundled: bool | None = None) -> None:
        canonical = canonical_source_readiness_kind(kind)
        if not canonical:
            return
        entry = self.entries[canonical]
        entry.configured = bool(configured)
        if bundled is not None:
            entry.bundled = bool(bundled)

    def mark_attempted(self, kind: object, *, failure: object = "") -> None:
        canonical = canonical_source_readiness_kind(kind)
        if not canonical:
            raw_kind = str(kind or "").strip().lower()
            if self.advanced is not None and raw_kind in {self.advanced.kind, self.current_rotation_kind}:
                self.advanced.configured = True
                self.advanced.attempted = True
                if failure:
                    self.advanced.failure = _safe_source_failure(failure)
            return
        entry = self.entries[canonical]
        entry.configured = True
        entry.attempted = True
        if failure:
            entry.failure = _safe_source_failure(failure)

    def mark_candidates(self, kind: object, count: object) -> None:
        canonical = canonical_source_readiness_kind(kind)
        if not canonical:
            return
        entry = self.entries[canonical]
        entry.configured = True
        entry.attempted = True
        entry.candidates = max(entry.candidates, _bounded_source_count(count))
        if entry.candidates:
            entry.exhausted = False
            entry.failure = ""

    def observe_tracks(self, tracks: Collection[Track] | None) -> None:
        if not tracks:
            return
        counts = {kind: 0 for kind in SOURCE_READINESS_KINDS}
        for track in tracks:
            canonical = canonical_source_readiness_kind(track.source)
            if canonical and canonical != "recovery":
                counts[canonical] += 1
        for kind, count in counts.items():
            if count:
                self.mark_candidates(kind, count)

    def reconcile_active_tracks(
        self,
        tracks: Collection[Track] | None,
        *,
        removed_tracks: Collection[Track] | None = None,
    ) -> None:
        """Replace loader candidate counts with the policy-filtered rotation.

        Loader evidence is captured before the operator blocklist is applied.
        Without this reconciliation, a source whose every fetched track was
        removed by local policy would remain ``candidates_only`` forever because
        the producer never sees a track from that source to mark exhausted.
        """
        counts = {kind: 0 for kind in SOURCE_READINESS_KINDS}
        advanced_count = 0
        for track in tracks or ():
            canonical = canonical_source_readiness_kind(track.source)
            if canonical and canonical != "recovery":
                counts[canonical] += 1
            elif self.advanced is not None and str(track.source or "").strip().lower() in {
                self.advanced.kind,
                self.current_rotation_kind,
            }:
                advanced_count += 1

        # Playable is aggregate evidence, not a per-track identity map. When a
        # source loses tracks, conservatively require one of its survivors to
        # pass preparation again instead of attributing a removed track's proof
        # to a different candidate.
        removed_kinds: set[str] = set()
        advanced_removed = False
        for track in removed_tracks or ():
            canonical = canonical_source_readiness_kind(track.source)
            if canonical and canonical != "recovery":
                removed_kinds.add(canonical)
            elif self.advanced is not None and str(track.source or "").strip().lower() in {
                self.advanced.kind,
                self.current_rotation_kind,
            }:
                advanced_removed = True

        empty_reason = "No found track remains in the active rotation after local policy filters."
        for kind, entry in self.entries.items():
            if kind == "recovery":
                continue
            previous_candidates = entry.candidates
            previous_playable = entry.playable
            if kind in removed_kinds:
                entry.playable = 0
            active_candidates = _bounded_source_count(counts[kind])
            if active_candidates:
                entry.candidates = active_candidates
                entry.exhausted = False
                entry.failure = ""
            elif previous_candidates or previous_playable:
                entry.candidates = 0
                self.mark_exhausted(kind, empty_reason)

        if self.advanced is not None:
            previous_candidates = self.advanced.candidates
            previous_playable = self.advanced.playable
            if advanced_removed:
                self.advanced.playable = 0
            if advanced_count:
                self.advanced.candidates = _bounded_source_count(advanced_count)
                self.advanced.exhausted = False
                self.advanced.failure = ""
            elif previous_candidates or previous_playable:
                self.advanced.candidates = 0
                self.mark_exhausted(self.current_rotation_kind or self.advanced.kind, empty_reason)

    def mark_playable(self, kind: object, count: object = 1) -> None:
        canonical = canonical_source_readiness_kind(kind)
        if not canonical:
            raw_kind = str(kind or "").strip().lower()
            if self.advanced is not None and raw_kind in {self.advanced.kind, self.current_rotation_kind}:
                self.advanced.playable = max(self.advanced.playable, _bounded_source_count(count), 1)
                self.advanced.exhausted = False
                self.advanced.failure = ""
            return
        if canonical == "recovery":
            return
        entry = self.entries[canonical]
        entry.configured = True
        entry.attempted = True
        entry.playable = max(entry.playable, _bounded_source_count(count), 1)
        entry.exhausted = False
        entry.failure = ""

    def mark_failure(self, kind: object, reason: object) -> None:
        canonical = canonical_source_readiness_kind(kind)
        if not canonical:
            raw_kind = str(kind or "").strip().lower()
            if self.advanced is not None and raw_kind in {self.advanced.kind, self.current_rotation_kind}:
                self.advanced.failure = _safe_source_failure(reason)
            return
        entry = self.entries[canonical]
        entry.configured = True
        entry.attempted = True
        entry.failure = _safe_source_failure(reason)

    def mark_exhausted(self, kind: object, reason: object) -> None:
        """Record that no active candidate for a source remains selectable."""
        canonical = canonical_source_readiness_kind(kind)
        if not canonical:
            raw_kind = str(kind or "").strip().lower()
            if self.advanced is not None and raw_kind in {self.advanced.kind, self.current_rotation_kind}:
                self.advanced.attempted = True
                self.advanced.playable = 0
                self.advanced.exhausted = True
                self.advanced.failure = _safe_source_failure(reason)
            return
        if canonical == "recovery":
            return
        entry = self.entries[canonical]
        entry.configured = True
        entry.attempted = True
        entry.playable = 0
        entry.exhausted = True
        entry.failure = _safe_source_failure(reason)

    def clear_exhausted(self, kind: object) -> None:
        """Re-open a source after a selectable candidate or concrete recovery appears."""
        canonical = canonical_source_readiness_kind(kind)
        if not canonical:
            raw_kind = str(kind or "").strip().lower()
            if self.advanced is not None and raw_kind in {self.advanced.kind, self.current_rotation_kind}:
                self.advanced.exhausted = False
            return
        self.entries[canonical].exhausted = False

    def set_current_rotation(self, kind: object, label: object = "") -> None:
        raw_kind = str(kind or "").strip().lower()
        self.current_rotation_kind = raw_kind
        self.current_rotation_label = _safe_source_failure(label)
        canonical = canonical_source_readiness_kind(raw_kind)
        if raw_kind and not canonical:
            advanced_kind = "classic" if raw_kind == "classic" else "custom"
            self.advanced = SourceReadinessEntry(
                kind=advanced_kind,
                label=self.current_rotation_label or advanced_kind.replace("_", " ").title(),
                configured=True,
                attempted=True,
            )
        else:
            self.advanced = None

    def mark_advanced_candidates(self, count: object) -> None:
        if self.advanced is None:
            return
        self.advanced.candidates = max(self.advanced.candidates, _bounded_source_count(count))
        if self.advanced.candidates:
            self.advanced.exhausted = False
            self.advanced.failure = ""

    def clear_on_air(self) -> None:
        for entry in self.entries.values():
            entry.on_air = False
        if self.advanced is not None:
            self.advanced.on_air = False

    def mark_on_air(self, kind: object, *, recovery: bool = False) -> None:
        self.clear_on_air()
        canonical = "recovery" if recovery else canonical_source_readiness_kind(kind)
        if canonical:
            entry = self.entries[canonical]
            entry.configured = True
            entry.attempted = True
            entry.on_air = True
            if canonical != "recovery":
                entry.playable = max(1, entry.playable)
                entry.exhausted = False
                entry.failure = ""
            return
        raw_kind = str(kind or "").strip().lower()
        if self.advanced is not None and raw_kind in {self.advanced.kind, self.current_rotation_kind}:
            self.advanced.on_air = True
            self.advanced.playable = max(1, self.advanced.playable)
            self.advanced.exhausted = False
            self.advanced.failure = ""


@dataclass(frozen=True)
class RuntimeProviderObservation:
    """Provider route observed while preparing one future audio segment."""

    current_provider: str
    primary_provider: str
    fallback_active: bool
    current_reason: str
    observation_token: str = ""


@dataclass
class PlaylistSource:
    """The user-visible source backing the currently loaded playlist."""

    kind: str
    source_id: str = ""
    url: str = ""
    label: str = ""
    track_count: int = 0
    selected_at: float = 0.0
    # Transient load evidence. StationState adopts a revision-scoped clone;
    # persistence and public source serialization deliberately omit it.
    readiness_evidence: SourceReadinessEvidence | None = field(default=None, repr=False, compare=False)


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
    runtime_provider_observations: dict[str, RuntimeProviderObservation] = field(
        default_factory=dict,
        repr=False,
    )
    # Private queue-lifecycle marker for a music-head / speech-tail handoff.
    # It deliberately lives outside ``metadata`` because metadata is projected
    # through public/admin status payloads.
    handoff_id: str | None = field(default=None, repr=False, compare=False)
    # Provider-owned single-use resources are released only through this hook;
    # queue mutation and playback finalizers call ``release()`` exactly once.
    playback_start_callback: Callable[[], bool] | None = field(default=None, repr=False, compare=False)
    release_callback: Callable[[], None] | None = field(default=None, repr=False, compare=False)
    _playback_started: bool = field(default=False, init=False, repr=False, compare=False)
    _released: bool = field(default=False, init=False, repr=False, compare=False)

    def mark_playback_started(self) -> bool:
        """Synchronously admit a segment and notify its provider before airing.

        Ordinary segments have no admission callback and remain admitted by
        default. Provider-owned segments fail closed when their callback denies
        admission or raises; their release hook is run immediately.
        """
        if self._released:
            return False
        if self._playback_started:
            return True
        callback = self.playback_start_callback
        if callback is None:
            self._playback_started = True
            return True
        try:
            admitted = callback()
        except Exception:
            logger.debug("Segment playback admission callback failed", exc_info=True)
            admitted = False
        if admitted is not True:
            self.release()
            return False
        self._playback_started = True
        self.playback_start_callback = None
        return True

    def release(self) -> None:
        """Idempotently release any provider-owned resource carried by this segment."""
        if self._released:
            return
        self._released = True
        self.playback_start_callback = None
        callback = self.release_callback
        self.release_callback = None
        if callback is None:
            return
        try:
            callback()
        except Exception:
            # Cleanup must never interrupt queue restoration or playback.
            logger.debug("Segment release callback failed for %s", self.path, exc_info=True)


def segment_track_key(segment: Segment) -> tuple[str, str]:
    """Return the stored literal song key carried by a rendered segment.

    The segment-side mirror of :func:`normalized_track_key`: producer music
    stamps ``title_only`` (the bare title) alongside ``artist``, while norm-cache
    bridges and rescue fills stamp only ``title``. Hard gates pass this key
    through the shared exact-equivalence normalizer; preferences and rotation
    dedupe intentionally retain literal stored-key behavior.
    """
    metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
    return (
        str(metadata.get("artist") or "").strip().lower(),
        str(metadata.get("title_only") or metadata.get("title") or "").strip().lower(),
    )


@dataclass(frozen=True)
class ListenerTrackReservations:
    """Matched listener songs that must wait for their dedication handoff.

    Pending requests own ordinary reservations whether or not they already own
    ``pinned_track``. After a successful dedication acknowledgement archives a
    request, its exact promised source remains reserved through handoff,
    admission, and the first emitted byte. Cache-key and canonical song
    identities cover both the downloaded object and pre-existing segments for
    the same recording.
    """

    cache_keys: frozenset[str] = frozenset()
    track_keys: frozenset[tuple[str, str]] = frozenset()

    @classmethod
    def from_pending_requests(cls, requests: Collection[dict]) -> ListenerTrackReservations:
        tracks = [
            track
            for request in requests
            if request.get("song_found") and isinstance((track := request.get("song_track_obj")), Track)
        ]
        return cls(
            cache_keys=frozenset(track.cache_key for track in tracks),
            track_keys=frozenset(normalize_song_identity_key(normalized_track_key(track)) for track in tracks),
        )

    def including_tracks(self, tracks: Collection[Track]) -> ListenerTrackReservations:
        """Return this set plus equivalent lifecycle-owned recordings."""
        return ListenerTrackReservations(
            cache_keys=self.cache_keys | frozenset(track.cache_key for track in tracks),
            track_keys=self.track_keys
            | frozenset(normalize_song_identity_key(normalized_track_key(track)) for track in tracks),
        )

    def reserves_cache_key(self, cache_key: object) -> bool:
        return bool(cache_key) and str(cache_key) in self.cache_keys

    def reserves_track_key(self, track_key: tuple[str, str]) -> bool:
        # Nothing reserved is the overwhelmingly common state, and the answer is
        # then False whatever the key normalizes to. Check that first so the
        # producer's per-pick sweep of the whole playlist does not pay full
        # identity normalization per track for a foregone answer.
        if not self.track_keys:
            return False
        equivalence_key = normalize_song_identity_key(track_key)
        return bool(equivalence_key[0] and equivalence_key[1]) and equivalence_key in self.track_keys

    def reserves_track(self, track: Track) -> bool:
        return self.reserves_cache_key(track.cache_key) or self.reserves_track_key(normalized_track_key(track))

    def reserves_segment(self, segment: Segment) -> bool:
        if self.reserves_track_key(segment_track_key(segment)):
            return True
        metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
        if self.reserves_cache_key(metadata.get("cache_key")):
            return True
        youtube_id = str(metadata.get("youtube_id") or "").strip()
        if youtube_id:
            source_key = Track(title="", artist="", duration_ms=0, youtube_id=youtube_id).cache_key
            return self.reserves_cache_key(source_key)
        return False


# Frozen, empty, and immutable — safe to hand to every caller when the listener
# lifecycle owns nothing, which is the state the station is in most of the time.
_NO_LISTENER_TRACK_RESERVATIONS = ListenerTrackReservations()

LISTENER_REQUEST_HANDOFF_TOKEN_KEY = "listener_request_handoff_id"
LISTENER_REQUEST_HANDOFF_ADMITTED_KEY = "listener_request_handoff_admitted"
LISTENER_REQUEST_DEDICATION_QUEUE_ID_KEY = "listener_request_dedication_queue_id"
LISTENER_REQUEST_HANDOFF_EXCLUSIVE_KEY = "listener_request_handoff_exclusive"
LISTENER_REQUEST_FORCE_REVISION_KEY = "song_force_next_revision"
LISTENER_REQUEST_PIN_REVISION_KEY = "song_pinned_track_revision"
LISTENER_REQUEST_INTERNAL_METADATA_KEYS = frozenset(
    {
        LISTENER_REQUEST_HANDOFF_TOKEN_KEY,
        LISTENER_REQUEST_HANDOFF_ADMITTED_KEY,
        LISTENER_REQUEST_DEDICATION_QUEUE_ID_KEY,
        LISTENER_REQUEST_HANDOFF_EXCLUSIVE_KEY,
    }
)
URGENT_INTERRUPT_PRIORITY_KEY = "urgent_interrupt_priority"


def _positive_revision(request: dict, key: str) -> int | None:
    """Return a request-carried ownership revision, or ``None`` if not a real one.

    ``bool`` is excluded deliberately: it is an ``int`` subclass, so a stray
    ``True`` would otherwise read as revision 1 and let a request clear state it
    never owned. That subtlety lives here once, for every revision field.
    """
    revision = request.get(key)
    if isinstance(revision, int) and not isinstance(revision, bool) and revision > 0:
        return revision
    return None


def listener_request_force_revision(request: dict) -> int | None:
    """Return the owned MUSIC-force revision carried by a request, if valid."""
    return _positive_revision(request, LISTENER_REQUEST_FORCE_REVISION_KEY)


def listener_request_pin_revision(request: dict) -> int | None:
    """Return the pinned-track ownership revision carried by a request."""
    return _positive_revision(request, LISTENER_REQUEST_PIN_REVISION_KEY)


@dataclass(frozen=True)
class ListenerRequestHandoff:
    """Single request-owned exception to later same-recording reservations."""

    token: str
    request_id: str
    track: Track
    dedication_queue_id: str = ""
    force_next_revision: int | None = None
    pin_revision: int | None = None
    music_selection_exclusive: bool = False
    borrowed_pin_clear_revision: int | None = None
    borrowed_force_clear_revision: int | None = None

    def matches_track(self, track: Track) -> bool:
        """Match the exact media source, never a live/remix textual alias."""
        return self.track.cache_key == track.cache_key

    def authorizes_segment(self, segment: Segment) -> bool:
        metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
        return str(metadata.get(LISTENER_REQUEST_HANDOFF_TOKEN_KEY) or "") == self.token and song_identity_keys_match(
            normalized_track_key(self.track), segment_track_key(segment)
        )


def segment_has_admitted_listener_request_handoff(segment: Segment) -> bool:
    """Return whether producer admission marked this exact promised segment."""
    metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
    return bool(
        metadata.get(LISTENER_REQUEST_HANDOFF_ADMITTED_KEY)
        and str(metadata.get(LISTENER_REQUEST_HANDOFF_TOKEN_KEY) or "")
    )


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
# StreamPacer's send-ahead cushion, and what stream_delivery_snapshot reports.
# 4s absorbs a render pause (station ID, ad, banter, HA projection) before a
# direct MP3 client hears it; worst measured on HA Green was 1.781s. Costs 32
# of a listener queue's 128 packet slots at 192 kbps.
STREAM_TARGET_LEAD_SECONDS = 4.0
STREAM_LATE_THRESHOLD_SECONDS = 0.05
HA_REFRESH_STAGES = ("states_request", "enrichment_wait", "projection", "idle")


class ConsumedListenerRequest(TypedDict):
    """A terminal listener receipt retained briefly for admin and public views."""

    id: str
    name: str | None
    message: str | None
    song_track: str | None
    type: str | None
    status: str  # "sent_to_hosts" | "song_not_found" | "source_changed" | "dismissed"
    song_error_reason: str
    consumed_at: float
    public_token: NotRequired[str | None]
    song_found: NotRequired[bool]
    song_error: NotRequired[bool]


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
    # Automatic starter rotation consumes every manifest track once before any
    # starter repeat. Queue admission reserves an identity; only playback start
    # consumes it from the cycle. Local/operator tracks may interleave without
    # resetting the cycle.
    starter_cycle_remaining: set[str] = field(default_factory=set, repr=False)
    starter_cycle_catalog: set[str] = field(default_factory=set, repr=False)
    starter_cycle_reserved: set[str] = field(default_factory=set, repr=False)
    music_admission_reservations: dict[str, Track] = field(default_factory=dict, repr=False)
    music_admission_changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    # Jamendo is eligible only after two starter/local tracks actually begin
    # playback. Queued reservations never advance this counter.
    jamendo_base_music_since_last: int = 0
    songs_since_banter: int = 0
    songs_since_ad: int = 0
    songs_since_news: int = 0
    segments_since_station_id: int = 0
    segments_since_time_check: int = 0
    guest_host_banter_cooldown_remaining: int = 0
    running_jokes: deque[str] = field(default_factory=lambda: deque(maxlen=5))
    recent_shapes: deque[str] = field(default_factory=lambda: deque(maxlen=5))
    recent_lore: deque[str] = field(default_factory=lambda: deque(maxlen=6))
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
    # Session-only ad receipts for completed breaks. Stores aggregate counts
    # in memory and resets with the process.
    ad_experiment_completed_breaks: int = 0
    ad_experiment_brand_airings: dict[str, int] = field(default_factory=dict)
    session_stopped: bool = False
    # True only after an explicit assetless force-resume, until a listener
    # accepts the first rebuilt segment. Readiness stays "starting" meanwhile.
    force_recovery_active: bool = False
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
    source_readiness: SourceReadinessEvidence = field(default_factory=SourceReadinessEvidence)
    heading: Heading | None = None
    heading_revision: int = 0
    heading_persist_callback: Callable[[Heading], None] | None = None
    heading_pending_announcement: str = ""
    heading_pending_narration_kind: str = ""
    heading_announced_id: str = ""
    # What the listener is hearing RIGHT NOW
    now_streaming: dict = field(default_factory=dict)
    # Selection and delivery are deliberately separate commits. A readable
    # segment becomes ``now_streaming`` before broadcast, but it is not
    # listener-audible until one listener queue accepts a chunk.
    current_stream_audible: bool = False
    audible_playback_epoch: int = 0
    _last_audible_stream: dict = field(default_factory=dict, repr=False)
    # Pre-produced segments waiting to play (shadow of asyncio.Queue for UI display)
    queued_segments: list[dict] = field(default_factory=list)
    # Private exact-once music→speech handoffs.  Entries are owned by
    # ``scheduling.handoff`` and are never serialized into status payloads or
    # restart-handoff manifests.
    handoff_reservations: dict[str, object] = field(default_factory=dict, repr=False, compare=False)
    # The playback loop owns the actual Segment while ``now_streaming`` remains
    # a public projection.  Keeping this private pointer lets a Skip/Panic
    # cancel the matching tail-bearing successor without leaking internal
    # handoff state into the wire contract.
    active_playback_segment: Segment | None = field(default=None, repr=False, compare=False)
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
    # Last continuity reservation whose audio actually reached a listener. One
    # live control can reserve several segments under one id and they air
    # consecutively, so remembering the last one reports ONE bridge fire per
    # control action instead of one per reserved track.
    last_continuity_air_reservation_id: str = ""
    # Stream-side log (when segments actually play, not when produced)
    stream_log: deque[SegmentLogEntry] = field(default_factory=lambda: deque(maxlen=50))
    # Recent generated banter clips that have actually started streaming.
    # Producer may mix these under future music for "studio bleed".
    recent_banter_paths: deque[Path] = field(default_factory=lambda: deque(maxlen=5))
    # Home Assistant context (natural language summary of home state)
    # Privacy cutover fence for generated host segments. Any global Home-context
    # disable increments this value before purging queued work; producer renders
    # capture it and may only enter the live queue while it still matches.
    home_context_policy_generation: int = 0
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
    # Per-line loss accounting for the banter the scriptwriter just returned
    # (same lifetime as last_banter_script). The producer copies it onto the
    # Tier-2 provenance row so a debrief can tell a full exchange from a short
    # one without re-parsing the raw model output. None when nothing was lost.
    last_banter_line_loss: dict[str, int] | None = None
    # Community-inspired Impossible Moments recipe telemetry. Public surfaces
    # may expose only the coarse family labels; recipe internals stay admin-only.
    ha_ritual_context: str = ""
    ha_ritual_public_families: list[str] = field(default_factory=list)
    ha_ritual_matches: list[dict[str, object]] = field(default_factory=list)
    ha_ritual_recipe_audit: list[dict[str, object]] = field(default_factory=list)
    # Force-trigger: producer will use this type instead of scheduler for the next segment
    force_next: SegmentType | None = None
    # Monotonic ownership generation for ``force_next``. Every semantic writer,
    # including a same-valued replacement, advances this counter so cleanup for
    # an older render cannot erase a newer operator/internal directive merely
    # because both happen to request the same SegmentType.
    force_next_revision: int = 0
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
    # Provenance for the out-of-band bridge. Home/timer interrupts carry the
    # privacy generation so a later global cutover can retire them before air;
    # operator/non-Home bridges remain untagged and are not clobbered.
    interrupt_slot_source: str = ""
    interrupt_slot_home_context_generation: int | None = None
    # Timestamp of last fired interrupt (for cooldown enforcement)
    last_interrupt_ts: float = 0.0
    # Chaos Mode: station-wide host-chaos toggle plus first-strike handoff.
    chaos_mode_active: bool = False
    chaos_pending: ChaosSubtype | None = None
    # Lifecycle owner for the urgent-interrupt BANTER safety belt. It remains
    # set after force claim and across failed renders, then is cleared only by
    # admission or an explicit stronger control. A later same-valued operator
    # force must survive cleanup for the older interrupt.
    urgent_interrupt_force_next_revision: int | None = None
    # Per-event ownership for drain recovery. Every successful interrupt
    # overwrites this with whether that interrupt actually purged queued audio;
    # cumulative discard telemetry is intentionally not a scheduling signal.
    urgent_interrupt_drained_audio: bool = False
    chaos_cutover_epoch: int = 0
    chaos_script_fallbacks: int = 0
    chaos_audio_failures: int = 0
    chaos_last_degraded_reason: str = ""
    # Pinned track: select_next_track returns this immediately then clears it
    pinned_track: Track | None = None
    # Monotonic ownership generation for the play-next slot. Listener cleanup
    # must not clear a later operator pin merely because it names the same Track.
    pinned_track_revision: int = 0
    # Transport ownership for an operator Skip. The now-playing "skipping"
    # sentinel can be replaced as soon as playback selects the next segment,
    # while the originating request is still settling history. Keep request
    # ownership separate so a rapid second Skip cannot cut two songs.
    skip_in_flight: bool = False
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
    # One admitted dedication may carry its promised recording past equivalent
    # reservations owned by later requests. The token is transferred to exactly
    # one producer-built music segment and cleared at queue admission.
    listener_request_handoff: ListenerRequestHandoff | None = None
    # Queue admission clears the mutable handoff so the next request may plan,
    # but the promised recording must remain reserved until its marked segment
    # emits audio. Keep the full handoff so a pre-byte file failure can restore
    # the request-owned promise. Multiple lookahead promises may coexist here.
    listener_request_admitted_reservations: dict[str, ListenerRequestHandoff] = field(default_factory=dict)
    # A failed admitted promise waits here only when another request already
    # owns the single active handoff slot. The producer promotes retries before
    # planning more request music.
    listener_request_retry_handoffs: deque[ListenerRequestHandoff] = field(default_factory=deque)
    # Short-lived terminal receipts used by admin history and public-token lookup.
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
    # What the live StreamPacer runs at, recorded when the playback loop builds
    # it. Defaults to the shipped constants so a state object with no loop
    # attached still reports honest numbers.
    stream_pacing_target_lead_seconds: float = STREAM_TARGET_LEAD_SECONDS
    stream_pacing_late_threshold_seconds: float = STREAM_LATE_THRESHOLD_SECONDS
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
    # Monotonic stamp of the last segment whose chunk was accepted by at least
    # one listener — including continuity clips and rescue fills. The
    # /healthz - /readyz silence gate needs "is anything reaching listeners",
    # not "did a file open": queue_empty_since keeps running across clip serves
    # (so the rescue ladder can escalate), but listener-audible bridge clips
    # must not trip the watchdog.
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
    bridge_fires_by_type: dict[str, int] = field(
        default_factory=lambda: {"drain": 0, "resume": 0, "idle": 0, "continuity": 0}
    )
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
    _runtime_provider_observations_by_token: dict[str, dict[str, RuntimeProviderObservation]] = field(
        default_factory=dict,
        repr=False,
    )
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

    def __post_init__(self) -> None:
        self._reset_source_readiness()

    def _reset_source_readiness(self) -> None:
        """Adopt load-time evidence and clear stale facts on a source revision."""
        source = self.playlist_source
        playlist = self.playlist if isinstance(self.playlist, list | tuple) else []
        seed = source.readiness_evidence if source is not None else None
        seeded_from_loader = seed is not None
        if seed is not None:
            evidence = seed.clone_for_revision(self.source_revision)
        elif self.source_readiness.has_signal() and self.source_readiness.source_revision == self.source_revision:
            evidence = self.source_readiness.clone_for_revision(self.source_revision)
        else:
            evidence = SourceReadinessEvidence(source_revision=self.source_revision)

        if source is not None:
            if not evidence.current_rotation_kind:
                evidence.set_current_rotation(source.kind, source.label)
            if not seeded_from_loader:
                canonical = canonical_source_readiness_kind(source.kind)
                if canonical:
                    evidence.mark_attempted(canonical)
                    evidence.mark_candidates(canonical, source.track_count or len(playlist))
                elif evidence.advanced is not None:
                    evidence.mark_advanced_candidates(source.track_count or len(playlist))
            if seeded_from_loader:
                evidence.reconcile_active_tracks(playlist)
            source.track_count = len(playlist)
        evidence.observe_tracks(playlist)
        self.source_readiness = evidence

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
        accepted_listener_count: int = 0,
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
                "accepted_listener_count": max(0, int(accepted_listener_count)),
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
            "target_lead_ms": round(self.stream_pacing_target_lead_seconds * 1000),
            "late_threshold_ms": round(self.stream_pacing_late_threshold_seconds * 1000),
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

            bridge_type ∈ {"drain", "resume", "idle", "continuity"}
                          (which rescue site fired; "continuity" is a live
                          control reserving safety audio, not the producer)
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
        self.release_listener_request_admitted_reservation(segment)
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

    def observe_runtime_provider(
        self,
        provider_class: str,
        *,
        current_provider: str,
        primary_provider: str,
        fallback_active: bool,
        reason: str,
        timestamp: float | None = None,
        observation_token: str | None = None,
    ) -> RuntimeProviderObservation:
        """Update current provider truth without claiming that audio was heard.

        A producer render binds a task-local observation token before it starts
        script and voice work. Observations made by child tasks inherit that
        token, while unrelated tasks retain their own context. Keeping the
        token-owned observations separately from the process-wide latest state
        prevents a background LLM call from being attached to whichever segment
        happens to finish next.
        """
        now = time.time() if timestamp is None else timestamp
        owner_token = (
            _RUNTIME_PROVIDER_OBSERVATION_TOKEN.get() if observation_token is None else str(observation_token).strip()
        )
        previous = self.runtime_provider_state.get(provider_class, {})
        previous_switch_timestamp = previous.get("last_switch_timestamp")
        previous_switch_reason = previous.get("last_switch_reason")
        if previous_switch_reason is None and previous_switch_timestamp is not None:
            # In-memory compatibility for state populated before the split
            # between current observation and historical transition reason.
            previous_switch_reason = previous.get("reason")
        last_audible_provider = previous.get("last_audible_provider")
        last_audible_primary = previous.get("last_audible_primary_provider")
        last_audible_fallback = previous.get("last_audible_fallback_active")
        last_audible_reason = previous.get("last_audible_reason")
        if last_audible_provider is None and previous_switch_timestamp is not None:
            # State created before the two-phase provider boundary had only
            # current-provider fields. Its timestamp proves that provider was
            # already committed, so preserve it as the audible baseline instead
            # of fabricating a duplicate switch on the next observation.
            last_audible_provider = previous.get("current_provider")
            last_audible_primary = previous.get("primary_provider")
            last_audible_fallback = previous.get("fallback_active")
            last_audible_reason = previous_switch_reason or previous.get("reason")
        try:
            observation_revision = int(previous.get("observation_revision") or 0) + 1
        except (TypeError, ValueError):
            observation_revision = 1
        self.runtime_provider_state[provider_class] = {
            "current_provider": current_provider,
            "primary_provider": primary_provider,
            "fallback_active": fallback_active,
            # ``reason`` remains the compatibility name for consumers that
            # need the latest observation. Transition history has its own
            # immutable-until-switch fields below.
            "reason": reason,
            "current_reason": reason,
            "last_observed": now,
            "observation_revision": observation_revision,
            "last_audible_provider": last_audible_provider,
            "last_audible_primary_provider": last_audible_primary,
            "last_audible_fallback_active": last_audible_fallback,
            "last_audible_reason": last_audible_reason,
            "last_switch_timestamp": previous_switch_timestamp,
            "last_switch_reason": previous_switch_reason,
        }
        observation = RuntimeProviderObservation(
            current_provider=current_provider,
            primary_provider=primary_provider,
            fallback_active=fallback_active,
            current_reason=reason,
            observation_token=owner_token,
        )
        if owner_token:
            self._runtime_provider_observations_by_token.setdefault(owner_token, {})[provider_class] = observation
        return observation

    def bind_runtime_provider_observation_scope(self, observation_token: str) -> Token[str]:
        """Bind provider observations made by this async render and its children."""
        owner_token = str(observation_token).strip()
        if not owner_token:
            raise ValueError("provider observation scope token must not be empty")
        return _RUNTIME_PROVIDER_OBSERVATION_TOKEN.set(owner_token)

    def reset_runtime_provider_observation_scope(self, scope: Token[str]) -> None:
        """Restore the caller's previous provider-observation ownership."""
        _RUNTIME_PROVIDER_OBSERVATION_TOKEN.reset(scope)

    def snapshot_runtime_provider_observations(
        self,
        observation_token: str,
    ) -> dict[str, RuntimeProviderObservation]:
        """Inspect one render's observations without transferring ownership."""
        owner_token = str(observation_token).strip()
        if not owner_token:
            return {}
        return dict(self._runtime_provider_observations_by_token.get(owner_token, {}))

    def take_runtime_provider_observations(
        self,
        observation_token: str,
    ) -> dict[str, RuntimeProviderObservation]:
        """Transfer one render's provider observations to its future segment."""
        owner_token = str(observation_token).strip()
        if not owner_token:
            return {}
        return self._runtime_provider_observations_by_token.pop(owner_token, {})

    def commit_runtime_provider_audible(
        self,
        provider_class: str,
        observation: RuntimeProviderObservation,
        *,
        event: str = "provider_switch_event",
        timestamp: float | None = None,
    ) -> RuntimeProviderEvent | None:
        """Commit provider switch history only when its segment reaches a listener."""
        now = time.time() if timestamp is None else timestamp
        previous = self.runtime_provider_state.get(provider_class, {})
        if not previous:
            self.observe_runtime_provider(
                provider_class,
                current_provider=observation.current_provider,
                primary_provider=observation.primary_provider,
                fallback_active=observation.fallback_active,
                reason=observation.current_reason,
                timestamp=now,
            )
            previous = self.runtime_provider_state[provider_class]

        previous_provider = str(previous.get("last_audible_provider") or "")
        previous_fallback_value = previous.get("last_audible_fallback_active")
        previous_fallback = bool(previous_fallback_value) if previous_fallback_value is not None else False
        changed = (
            previous_provider != observation.current_provider or previous_fallback != observation.fallback_active
            if previous_provider
            else observation.fallback_active or observation.current_provider != observation.primary_provider
        )

        updated = dict(previous)
        updated["last_audible_provider"] = observation.current_provider
        updated["last_audible_primary_provider"] = observation.primary_provider
        updated["last_audible_fallback_active"] = observation.fallback_active
        updated["last_audible_reason"] = observation.current_reason
        if changed:
            updated["last_switch_timestamp"] = now
            updated["last_switch_reason"] = observation.current_reason
        self.runtime_provider_state[provider_class] = updated
        if not changed:
            return None

        entry = RuntimeProviderEvent(
            event=event,
            provider_class=provider_class,
            from_provider=previous_provider or observation.primary_provider,
            to_provider=observation.current_provider,
            reason=observation.current_reason,
            fallback_active=observation.fallback_active,
            timestamp=now,
        )
        self.runtime_events.append(entry)
        return entry

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
        """Compatibility helper for boundaries that are already listener-audible."""
        observation = self.observe_runtime_provider(
            provider_class,
            current_provider=current_provider,
            primary_provider=primary_provider,
            fallback_active=fallback_active,
            reason=reason,
            timestamp=timestamp,
        )
        return self.commit_runtime_provider_audible(
            provider_class,
            observation,
            event=event,
            timestamp=timestamp,
        )

    def _apply_playlist_context(self, tracks: list[Track], source: PlaylistSource | None) -> None:
        """Swap the crate and everything scoped to the crate, and nothing else.

        This is the half of a source change that belongs to the *playlist*:
        which records exist, how they rotate, what steering they inherited. It
        deliberately touches no transport intent (pin, force, listener
        ownership, pending work) so a caller that must not disturb the live
        timeline can reuse it. ``switch_playlist`` adds the revocation half on
        top; ``apply_source_metadata_only`` does not.

        Music admission reservations and the starter cycle live in the
        revocation half for the same reason. A queued music segment holds its
        reservation until ``commit_music_admission`` runs from
        ``Segment.mark_playback_started``; clearing the map under a queue that
        survives makes every one of those segments fail admission and get
        skipped at the moment it should air. ``_sync_starter_cycle`` rebuilds
        ``starter_cycle_reserved`` from the live reservations the next time the
        cycle is consulted and finds the catalogue changed, so leaving the cycle
        alone here is self-healing rather than stale, in both directions of a
        swap.
        """
        self.playlist_revision += 1
        self.source_revision += 1
        self.playlist = tracks
        self.playlist_source = source
        self.startup_source_error = ""
        self._reset_source_readiness()
        self.songs_since_banter = 0
        self.songs_since_ad = 0
        self.songs_since_news = 0
        self.music_admission_changed.set()
        self.jamendo_base_music_since_last = 0
        # Clear play history so diversity filters start fresh for the new
        # playlist context.  Without this, a 20-track playlist loops after
        # ~30-40 min because the deque fills and recency weights flatten.
        self.played_tracks.clear()
        self.played_track_log.clear()
        self.heading = None
        self.heading_revision += 1
        self.heading_pending_announcement = ""
        self.heading_pending_narration_kind = ""
        self.heading_announced_id = ""

    def apply_source_metadata_only(self, tracks: list[Track], source: PlaylistSource | None = None) -> None:
        """Commit a stale source load as metadata, leaving the live timeline whole.

        A source load whose captured continuity epoch has moved (a Stop, or a
        Stop plus a fast Resume) owns the crate, not the controls. Every piece
        of transport intent visible on the current epoch — the pinned track, the
        force slot, listener request ownership, queued operator work — belongs to
        a newer timeline than this request and must survive untouched, revision
        counters included. Restoring those fields *after* ``switch_playlist``
        revoked them cannot do that: the guarded clears bump
        ``pinned_track_revision`` / ``force_next_revision``, orphaning every
        handoff and pending request that recorded the revision it owns. So the
        revocation half never runs here.
        """
        self._apply_playlist_context(tracks, source)

    def switch_playlist(
        self,
        tracks: list[Track],
        source: PlaylistSource | None = None,
        *,
        preserve_reservation_ids: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        """Replace the active playlist and bump revision counter.

        In-flight producer segments are discarded on next commit check.

        ``preserve_reservation_ids`` names queue ids the caller deliberately kept
        in the queue across the switch: the on-air dedication's promised song,
        and the assetless branch's preserved runway head. A queued music segment
        cannot start without its ``music_admission_reservations`` entry
        (``commit_music_admission`` runs from ``Segment.mark_playback_started``),
        so revoking a survivor's reservation is the same as deleting the segment,
        only later and silently. Everything not named here is genuinely gone and
        its reservation goes with it.
        """
        self._apply_playlist_context(tracks, source)
        retained_reservations = {
            reservation_id: track
            for reservation_id, track in self.music_admission_reservations.items()
            if reservation_id in preserve_reservation_ids
        }
        self.starter_cycle_remaining.clear()
        self.starter_cycle_catalog.clear()
        self.starter_cycle_reserved.clear()
        self.music_admission_reservations.clear()
        self.music_admission_reservations.update(retained_reservations)
        # A retained starter reservation keeps holding its cycle slot, or the
        # rebuilt cycle would hand the same recording out a second time.
        self.starter_cycle_reserved.update(
            track.cache_key for track in retained_reservations.values() if track.source == "starter"
        )
        # Clear listener requests and pinned track so in-flight background
        # download tasks from the old source can't zombie-pin a track into
        # the new playlist context. Keep an admin-visible trail so accepted
        # listener requests never disappear without an outcome.
        self._mark_pending_requests_source_changed()
        self.pending_actions.clear()
        self._listener_request_rl.clear()
        self.set_pinned_track(None)
        self.listener_request_handoff = None
        self.listener_request_admitted_reservations.clear()
        self.listener_request_retry_handoffs.clear()
        self.clear_force_next()
        self.operator_force_pending = None

    def restore_playlist_if_still_empty(self, tracks: list[Track], source: PlaylistSource | None = None) -> bool:
        """Repopulate an empty crate without the full source-switch reset.

        Unlike ``switch_playlist`` (an operator-initiated source override),
        this is a mid-session recovery from an empty rotation. It preserves
        heading (Record Hunt steering), pending listener requests, the pinned
        track, force_next/operator_force_pending, and play history — none of
        that operator intent should be wiped just because the crate briefly
        went empty and refilled. It does not purge the queue either, so music
        admission reservations stay with the segments still holding them; a
        queued song that outlived the empty crate must remain startable.

        Returns False and mutates nothing if the playlist is no longer empty
        (e.g. an admin source switch landed while the caller's directory scan
        was still in flight) — the caller must not clobber it.
        """
        if self.playlist:
            return False
        self.playlist = tracks
        self.playlist_source = source
        self.playlist_revision += 1
        self.startup_source_error = ""
        self._reset_source_readiness()
        self.music_admission_changed.set()
        self.jamendo_base_music_since_last = 0
        return True

    def set_force_next(self, value: SegmentType | None) -> int:
        """Replace the next-segment directive and return its ownership revision.

        A same-valued write is still a new directive. Advancing the revision on
        every call lets deferred cleanup distinguish its own force from a newer
        Panic Cut, move-to-next, or other writer that selected the same type.
        """
        self.force_next = value
        self.force_next_revision += 1
        return self.force_next_revision

    def clear_force_next(
        self,
        *,
        expected_revision: int | None = None,
        expected_type: SegmentType | None = None,
    ) -> bool:
        """Clear ``force_next`` only when optional ownership checks still match."""
        if expected_revision is not None and self.force_next_revision != expected_revision:
            return False
        if expected_type is not None and self.force_next is not expected_type:
            return False
        self.set_force_next(None)
        return True

    def set_pinned_track(self, track: Track | None) -> int:
        """Replace the play-next pin and return its ownership revision."""
        self.pinned_track = track
        self.pinned_track_revision += 1
        return self.pinned_track_revision

    def clear_pinned_track(
        self,
        *,
        expected_revision: int | None = None,
        expected_track: Track | None = None,
    ) -> bool:
        """Clear the play-next pin only while optional ownership checks match."""
        if expected_revision is not None and self.pinned_track_revision != expected_revision:
            return False
        if expected_track is not None and self.pinned_track is not expected_track:
            return False
        if self.pinned_track is None:
            return False
        self.set_pinned_track(None)
        return True

    def _mark_pending_requests_source_changed(self) -> None:
        if not self.pending_requests:
            return
        now = time.time()
        for request in list(self.pending_requests):
            self.archive_listener_request(
                request,
                status="source_changed",
                song_error_reason="source_changed" if request.get("type") == "song_request" else "",
                now=now,
            )

    def archive_listener_request(
        self,
        request: dict,
        *,
        status: str,
        song_error_reason: str | None = None,
        now: float | None = None,
    ) -> ConsumedListenerRequest:
        """Move one request to the short-lived receipt trail without losing its public token."""
        consumed_at = time.time() if now is None else now
        reason = str(song_error_reason if song_error_reason is not None else request.get("song_error_reason") or "")
        song_error = bool(request.get("song_error")) or bool(reason)
        receipt: ConsumedListenerRequest = {
            "id": request.get("request_id") or str(request.get("ts", "")),
            "name": request.get("name"),
            "message": request.get("message") or request.get("text"),
            "song_track": request.get("song_track"),
            "type": request.get("type"),
            "status": status,
            "song_error_reason": reason,
            "song_found": bool(request.get("song_found")) and not song_error,
            "song_error": song_error,
            "public_token": request.get("public_token"),
            "consumed_at": consumed_at,
        }
        self.recently_consumed_requests.append(receipt)
        self.prune_recent_listener_requests(consumed_at)
        if request in self.pending_requests:
            self.pending_requests.remove(request)
        return receipt

    def prune_recent_listener_requests(self, now: float | None = None) -> None:
        """Drop terminal listener receipts after their public retention window."""
        cutoff = (time.time() if now is None else now) - RECENTLY_CONSUMED_RETENTION_SECONDS
        records = self.recently_consumed_requests
        # Every listener receipt poll and every admin poll lands here, and almost
        # none of them find an expired record. Only rebuild the trail when one
        # actually fell outside the window.
        if all(record.get("consumed_at", 0) >= cutoff for record in records):
            return
        self.recently_consumed_requests = [record for record in records if record.get("consumed_at", 0) >= cutoff]

    def listener_track_reservations(self) -> ListenerTrackReservations:
        """Return song identities owned anywhere in the listener-request lifecycle."""
        lifecycle_tracks = [handoff.track for handoff in self.listener_request_admitted_reservations.values()]
        lifecycle_tracks.extend(handoff.track for handoff in self.listener_request_retry_handoffs)
        if self.listener_request_handoff is not None:
            lifecycle_tracks.append(self.listener_request_handoff.track)
        if not self.pending_requests and not lifecycle_tracks:
            # No request owns anything: hand back the shared empty answer instead
            # of normalizing identities into two throwaway frozensets on every
            # music pick and every status poll.
            return _NO_LISTENER_TRACK_RESERVATIONS
        reservations = ListenerTrackReservations.from_pending_requests(self.pending_requests)
        return reservations.including_tracks(lifecycle_tracks)

    def release_listener_request_admitted_reservation(self, segment: Segment) -> bool:
        """Release the promise carried by one admitted music segment."""
        if not segment_has_admitted_listener_request_handoff(segment):
            return False
        metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
        token = str(metadata.get(LISTENER_REQUEST_HANDOFF_TOKEN_KEY) or "")
        if not token or token not in self.listener_request_admitted_reservations:
            return False
        del self.listener_request_admitted_reservations[token]
        return True

    @staticmethod
    def _retry_listener_request_handoff(handoff: ListenerRequestHandoff) -> ListenerRequestHandoff:
        """Return a clean, request-owned retry after its admitted file failed."""
        return replace(
            handoff,
            force_next_revision=None,
            pin_revision=None,
            music_selection_exclusive=True,
            borrowed_pin_clear_revision=None,
            borrowed_force_clear_revision=None,
        )

    def restore_listener_request_handoff_before_first_byte(self, segment: Segment) -> bool:
        """Restore an admitted promise whose file failed before emitting audio."""
        if not segment_has_admitted_listener_request_handoff(segment):
            return False
        metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
        token = str(metadata.get(LISTENER_REQUEST_HANDOFF_TOKEN_KEY) or "")
        handoff = self.listener_request_admitted_reservations.pop(token, None)
        if handoff is None:
            return False
        retry = self._retry_listener_request_handoff(handoff)
        if self.listener_request_handoff is None:
            self.listener_request_handoff = retry
        else:
            self.listener_request_retry_handoffs.append(retry)
        return True

    def restore_listener_request_handoff_after_source_switch(self, handoff: ListenerRequestHandoff) -> None:
        """Restore an audible promise after a source switch cleared ownership."""
        if self.listener_request_handoff is not None:
            raise RuntimeError("source-switch retry restore requires an empty active handoff slot")
        self.listener_request_handoff = self._retry_listener_request_handoff(handoff)
        self.force_listener_request_handoff_music()

    def active_listener_request_handoff_on_air(self) -> ListenerRequestHandoff | None:
        """Return the active handoff only when its dedication is currently airing."""
        handoff = self.listener_request_handoff
        now_streaming = self.now_streaming if isinstance(self.now_streaming, dict) else {}
        metadata = now_streaming.get("metadata")
        if handoff is None or now_streaming.get("type") != SegmentType.BANTER.value or not isinstance(metadata, dict):
            return None
        queue_id = str(metadata.get("queue_id") or "")
        return handoff if queue_id and queue_id == handoff.dedication_queue_id else None

    def promote_listener_request_retry_handoff(self) -> bool:
        """Promote the oldest failed promise when the active slot is free."""
        if self.listener_request_handoff is not None or not self.listener_request_retry_handoffs:
            return False
        self.listener_request_handoff = self.listener_request_retry_handoffs.popleft()
        return True

    def arm_listener_request_handoff(
        self,
        request: dict,
        track: Track,
        *,
        dedication_queue_id: str = "",
    ) -> bool:
        """Give one queued dedication scoped ownership of its music handoff."""
        request_id = str(request.get("request_id") or request.get("ts") or "").strip() or uuid4().hex
        current = self.listener_request_handoff
        if current is not None:
            return current.request_id == request_id and current.matches_track(track)
        pin_revision = listener_request_pin_revision(request)
        self.listener_request_handoff = ListenerRequestHandoff(
            token=uuid4().hex,
            request_id=request_id,
            track=track,
            dedication_queue_id=dedication_queue_id,
            force_next_revision=listener_request_force_revision(request),
            pin_revision=pin_revision,
            music_selection_exclusive=pin_revision is not None,
        )
        return True

    def force_listener_request_handoff_music(self) -> None:
        """Arm and revision-own the next retry for an admitted dedication."""
        handoff = self.listener_request_handoff
        if handoff is None or self.force_next is not None:
            return
        revision = self.set_force_next(SegmentType.MUSIC)
        self.listener_request_handoff = replace(handoff, force_next_revision=revision)

    def revoke_listener_request_handoff_for_discarded_dedication(self, segment: Segment) -> bool:
        """Revoke a promise only when its still-queued dedication is discarded."""
        handoff = self.listener_request_handoff
        metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
        queue_id = str(metadata.get("queue_id") or "")
        if handoff is None or not handoff.dedication_queue_id or queue_id != handoff.dedication_queue_id:
            return False
        if (
            handoff.pin_revision is not None
            and self.pinned_track is not None
            and handoff.matches_track(self.pinned_track)
        ):
            self.clear_pinned_track(
                expected_revision=handoff.pin_revision,
                expected_track=self.pinned_track,
            )
        restore_borrowed_pin = bool(
            not handoff.music_selection_exclusive
            and handoff.borrowed_pin_clear_revision is not None
            and self.pinned_track is None
            and self.pinned_track_revision == handoff.borrowed_pin_clear_revision
        )
        restore_borrowed_force = bool(
            restore_borrowed_pin
            and handoff.borrowed_force_clear_revision is not None
            and self.force_next is None
            and self.force_next_revision == handoff.borrowed_force_clear_revision
        )
        self.clear_listener_request_handoff()
        if restore_borrowed_pin:
            # The handoff borrowed, rather than owned, an operator's same-track
            # pin. Its in-flight render is fenced below, so put that independent
            # action back only if neither ownership slot changed meanwhile.
            self.set_pinned_track(handoff.track)
            if restore_borrowed_force:
                self.set_force_next(SegmentType.MUSIC)
        # Selection consumes the pin before its slow render. Fence any such
        # in-flight music attempt so removing the still-queued dedication cannot
        # let the now-unannounced song cross render, egress, or capacity waits.
        self.continuity_epoch += 1
        return True

    def listener_request_handoff_metadata(self, track: Track) -> dict[str, str | bool]:
        """Return admission and dedication-link metadata for the promised recording."""
        handoff = self.listener_request_handoff
        if handoff is None or not handoff.matches_track(track):
            return {}
        metadata: dict[str, str | bool] = {
            LISTENER_REQUEST_HANDOFF_TOKEN_KEY: handoff.token,
            LISTENER_REQUEST_HANDOFF_EXCLUSIVE_KEY: handoff.music_selection_exclusive,
        }
        if handoff.dedication_queue_id:
            metadata[LISTENER_REQUEST_DEDICATION_QUEUE_ID_KEY] = handoff.dedication_queue_id
        return metadata

    def listener_request_handoff_authorizes(self, segment: Segment) -> bool:
        """Validate a promised segment while it crosses producer admission."""
        handoff = self.listener_request_handoff
        return handoff is not None and handoff.authorizes_segment(segment)

    def admit_listener_request_handoff(self, segment: Segment) -> None:
        """Mark the promised segment and release the single in-memory handoff slot."""
        if not self.listener_request_handoff_authorizes(segment):
            raise RuntimeError("listener request handoff ownership changed before admission")
        handoff = self.listener_request_handoff
        assert handoff is not None
        segment.metadata[LISTENER_REQUEST_HANDOFF_ADMITTED_KEY] = True
        self.listener_request_admitted_reservations[handoff.token] = handoff
        self.listener_request_handoff = None

    def clear_listener_request_handoff(self, track: Track | None = None) -> None:
        """Revoke a pending handoff, optionally only when it owns ``track``."""
        handoff = self.listener_request_handoff
        if handoff is not None and (track is None or handoff.matches_track(track)):
            if handoff.force_next_revision is not None:
                self.clear_force_next(
                    expected_revision=handoff.force_next_revision,
                    expected_type=SegmentType.MUSIC,
                )
            self.listener_request_handoff = None

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

    def on_stream_segment_selected(self, segment: Segment) -> int:
        """Commit a readable segment as the current playback selection.

        This boundary contains no claims about listener delivery. The playback
        loop calls it only after opening the file and reading a non-empty chunk.
        """
        now = time.time()
        self.playback_epoch += 1
        seg_type = segment.type.value
        label = segment.metadata.get("title", segment.metadata.get("brand", seg_type))
        self.current_stream_audible = False
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
        return self.playback_epoch

    def on_stream_segment_audible(self, segment: Segment) -> bool:
        """Commit listener-facing state once for the selected segment.

        Returns ``True`` only for the first accepted-listener commit in the
        current playback epoch. All work here is bounded and in-memory.
        """
        selected_epoch = self.now_streaming.get("epoch") if isinstance(self.now_streaming, dict) else None
        if selected_epoch != self.playback_epoch or self.audible_playback_epoch == self.playback_epoch:
            return False

        self.audible_playback_epoch = self.playback_epoch
        self.current_stream_audible = True
        self.force_recovery_active = False
        now = time.time()
        metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
        try:
            director = self.home_context_director
            home_fact_id = str(metadata.get("home_fact_id") or "")
            # Only a home-fact segment holds a reservation; gate on its id so an
            # ordinary segment can never activate an unrelated fact's cooldown.
            if director is not None and home_fact_id:
                director.activate(str(metadata.get("queue_id") or ""), fact_id=home_fact_id)
        except Exception:
            logging.getLogger("mammamiradio.home_context_director").debug(
                "Home context director activation failed", exc_info=True
            )
        seg_type = segment.type.value
        label = metadata.get("title", metadata.get("brand", seg_type))
        # Record only the previous listener-audible music segment as completed.
        # Readable selections that never reached a listener are deliberately
        # absent from this history.
        prev = self._last_audible_stream
        if prev.get("type") == "music" and prev.get("started"):
            self.listener.record_outcome(
                skipped=False,
                listen_sec=now - prev["started"],
                track_display=prev.get("label", ""),
            )
            self.listener.segments_since_taste_mirror += 1
        # Track only ordinary canned banter at stream time. Packaged recovery
        # speech is operational safety audio, never shareware trial content.
        if segment.type == SegmentType.BANTER and metadata.get("canned") and not metadata.get("rescue"):
            self.canned_clips_streamed += 1
        for provider_class, observation in segment.runtime_provider_observations.items():
            self.commit_runtime_provider_audible(
                provider_class,
                observation,
                timestamp=now,
            )
        raw_audio_source = str(metadata.get("audio_source") or "")
        if raw_audio_source == "fallback_norm_cache":
            raw_audio_source = "norm_cache"
        fallback_active = is_fallback_active(metadata)
        bound_playlist_source = str(metadata.get(SEGMENT_PLAYLIST_SOURCE_KIND_KEY) or "")
        active_playlist_source = (
            bound_playlist_source or (self.playlist_source.kind if self.playlist_source is not None else "") or "stream"
        )
        # Source readiness is listener-audible truth. A readable selection that
        # never reaches a listener must not claim either recovery or music is on
        # air, and a later source swap must not relabel the rendered segment.
        self.source_readiness.clear_on_air()
        if fallback_active or metadata.get("rescue"):
            self.source_readiness.mark_on_air("recovery", recovery=True)
        elif segment.type == SegmentType.MUSIC:
            source_kind = metadata.get("source_kind") or bound_playlist_source or active_playlist_source
            self.source_readiness.mark_on_air(source_kind)
        if raw_audio_source or metadata.get("fallback") or fallback_active or segment.type == SegmentType.MUSIC:
            audio_source = raw_audio_source
            if not audio_source and fallback_active:
                audio_source = "canned"
            elif segment.type == SegmentType.MUSIC and (
                not audio_source or (not fallback_active and audio_source in {"download", "prewarm"})
            ):
                audio_source = active_playlist_source
            self.update_runtime_provider(
                "audio_source",
                current_provider=audio_source or "stream",
                primary_provider=active_playlist_source,
                fallback_active=fallback_active,
                reason=(
                    str(metadata.get("fallback_reason") or "Fallback audio is currently on air")
                    if fallback_active
                    else "Primary audio source is on air"
                ),
                timestamp=now,
            )
        # Moment Receipts: a home-triggered segment reached a listener. The
        # playback loop's final result still records its complete outcome.
        # Rescue and fallback fills never carry a real moment, and must never
        # mint a receipt even if their metadata leaks a stale id.
        if self.moment_store is not None and not fallback_active and not metadata.get("rescue"):
            try:
                for _moment_key in ("ritual_moment_id", "gag_moment_id"):
                    _moment_id = metadata.get(_moment_key)
                    if _moment_id:
                        self.moment_store.mark_airing(str(_moment_id), now=now)
            except Exception:  # pragma: no cover - receipts must never break audio
                logging.getLogger("mammamiradio.moment_receipts").debug(
                    "Moment receipt airing mark failed", exc_info=True
                )
        # Only add to studio-bleed pool once banter truly starts streaming.
        if segment.type == SegmentType.BANTER and not metadata.get("canned"):
            self.recent_banter_paths.append(segment.path)
        if segment.type == SegmentType.MUSIC:
            title = str(metadata.get("title_only") or metadata.get("title") or "")
            artist = str(metadata.get("artist") or "")
            if " – " in title and not artist:
                artist, title = title.split(" – ", 1)
            duration_ms = metadata.get("duration_ms")
            if not isinstance(duration_ms, int):
                duration_ms = int(max(segment.duration_sec, 0.0) * 1000)
            title_key = title.strip().lower()
            label_key = str(label).strip().lower()
            placeholder_titles = {"", "music", "unknown", "unknown title", "untitled", "none"}
            has_real_title = title_key not in placeholder_titles and not (
                title_key == label_key and label_key in placeholder_titles
            )
            if not metadata.get("error") and not fallback_active and duration_ms > 0 and has_real_title:
                self.played_track_log.append(
                    PlayedEntry(
                        track=Track(
                            title=title,
                            artist=artist,
                            duration_ms=duration_ms,
                            spotify_id=str(metadata.get("spotify_id") or ""),
                            youtube_id=str(metadata.get("youtube_id") or ""),
                            album_art=str(metadata.get("album_art") or ""),
                            source=metadata.get("source_kind") or "youtube",
                        ),
                        played_at=time.monotonic(),
                    )
                )
        self._last_audible_stream = dict(self.now_streaming)
        self.last_state_change_at = now
        return True

    def on_stream_segment(self, segment: Segment) -> None:
        """Compatibility helper that commits both selection and audibility.

        Runtime playback uses the explicit two-stage methods above. Direct
        callers retain the historical one-call behavior.
        """
        self.on_stream_segment_selected(segment)
        self.on_stream_segment_audible(segment)

    def reserve_next_track(self) -> Track:
        """Legacy round-robin rotation — use select_next_track() for weighted shuffle."""
        if not self.playlist:
            raise RuntimeError("Playlist is empty")
        track = self.playlist.pop(0)
        self.playlist.append(track)
        return track

    def _sync_starter_cycle(self) -> set[str]:
        """Refresh the manifest identity bag without crossing a queued boundary."""
        catalog = {track.cache_key for track in self.playlist if track.source == "starter"}
        if catalog != self.starter_cycle_catalog:
            self.starter_cycle_catalog = catalog
            self.starter_cycle_remaining = set(catalog)
            # Rebuild the reserved set from the reservations that actually exist
            # rather than trimming the old one. Trimming is lossy in one
            # direction: a crate that drops a starter track forgets a live
            # reservation, and if that track returns the cycle offers it again
            # while the first copy is still queued.
            self.starter_cycle_reserved = {
                reserved.cache_key
                for reserved in self.music_admission_reservations.values()
                if reserved.source == "starter" and reserved.cache_key in catalog
            }
        elif catalog and not self.starter_cycle_remaining and not self.starter_cycle_reserved:
            # A new cycle may begin only after the last reservation in the old
            # cycle actually started (commit) or was removed (rollback).
            self.starter_cycle_remaining = set(catalog)
        return catalog

    def reserve_music_admission(self, reservation_id: str, track: Track) -> bool:
        """Reserve queue ownership without counting the track as aired.

        Reservations are idempotent by queue identity and fail closed for a
        duplicate starter identity or a second queued Jamendo lease.
        """
        token = reservation_id.strip()
        if not token:
            return False
        existing = self.music_admission_reservations.get(token)
        if existing is not None:
            return existing is track

        if track.source == "starter":
            self._sync_starter_cycle()
            key = track.cache_key
            if key not in self.starter_cycle_remaining or key in self.starter_cycle_reserved:
                return False
            self.starter_cycle_reserved.add(key)
        elif track.source == "jamendo" and any(
            reserved.source == "jamendo" for reserved in self.music_admission_reservations.values()
        ):
            return False

        self.music_admission_reservations[token] = track
        return True

    def commit_music_admission(self, reservation_id: str) -> bool:
        """Count one reserved track exactly when it is admitted to playback."""
        track = self.music_admission_reservations.pop(reservation_id, None)
        if track is None:
            return False
        if track.source == "starter":
            self.starter_cycle_reserved.discard(track.cache_key)
            self.starter_cycle_remaining.discard(track.cache_key)
            self.jamendo_base_music_since_last += 1
        elif track.source == "local":
            self.jamendo_base_music_since_last += 1
        elif track.source == "jamendo":
            self.jamendo_base_music_since_last = 0
        self.music_admission_changed.set()
        return True

    def rollback_music_admission(self, reservation_id: str) -> bool:
        """Release a queued reservation without advancing cycle or cadence."""
        track = self.music_admission_reservations.pop(reservation_id, None)
        if track is None:
            return False
        if track.source == "starter":
            self.starter_cycle_reserved.discard(track.cache_key)
        self.music_admission_changed.set()
        return True

    def jamendo_insert_eligible(self) -> bool:
        """Return whether cadence permits exactly one new transient insert."""
        if any(track.source == "jamendo" for track in self.music_admission_reservations.values()):
            return False
        if self.jamendo_base_music_since_last >= 2:
            return True
        # No starter/local crate exists to satisfy the two-track gate.
        # Jamendo is then the only remaining legal music path.
        return not self.playlist

    async def wait_for_music_admission_change(self, *, timeout: float = 1.0) -> None:
        """Wait without spinning while a full starter lookahead owns the cycle."""
        self.music_admission_changed.clear()
        self._sync_starter_cycle()
        if self.starter_cycle_remaining - self.starter_cycle_reserved:
            return
        try:
            await asyncio.wait_for(self.music_admission_changed.wait(), timeout=timeout)
        except TimeoutError:
            pass

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
        starter_catalog = self._sync_starter_cycle()

        if self.pinned_track is not None:
            track = self.pinned_track
            starter_blocked = track.source == "starter" and (
                track.cache_key not in self.starter_cycle_remaining or track.cache_key in self.starter_cycle_reserved
            )
            if not starter_blocked:
                # Consuming the pin is a semantic write: go through the setter so
                # the revision advances and a listener/operator pin owner can
                # still tell its own pin apart from a newer one.
                self.set_pinned_track(None)
                if track.cache_key not in excluded:
                    return track
                if not any(candidate.cache_key not in excluded for candidate in self.playlist):
                    raise RuntimeError("Playlist has no eligible tracks")

        pool = [track for track in self.playlist if track.cache_key not in excluded]
        if not pool:
            raise RuntimeError("Playlist has no eligible tracks")

        # Fail closed on automatic starter repeats: a queued starter identity is
        # reserved, then removed from the cycle only when playback begins. A
        # failed render remains selectable and a queued identity cannot be
        # selected twice merely to fill lookahead.
        if starter_catalog:
            pool = [
                track
                for track in pool
                if track.source != "starter"
                or (
                    track.cache_key in self.starter_cycle_remaining
                    and track.cache_key not in self.starter_cycle_reserved
                )
            ]
            if not pool:
                if self.starter_cycle_remaining and self.starter_cycle_remaining <= self.starter_cycle_reserved:
                    raise StarterCycleReservationPendingError(
                        "Starter cycle is waiting for queued tracks to begin playback"
                    )
                raise RuntimeError("Playlist has no eligible tracks in the current starter cycle")
            if self.playlist_source is not None and self.playlist_source.kind == "starter":
                # Startup supplied one manifest-digest-pinned bag cycle in
                # playlist order. Honor that order; reserve after queue
                # admission and consume only at playback start, so a render
                # failure retries rather than skipping an entry.
                for starter_track in self.playlist:
                    if starter_track in pool and starter_track.source == "starter":
                        return starter_track

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

        # An active course keeps lifting its matches for as long as it is set. That
        # is deliberate: steering is durable and does not retire when a budget runs out.
        # The cost is that a small found set gets picked from over and over: at the
        # target share, a set of H tracks brings a given one back roughly every
        # H/share picks, which on a five-track set is inside the plain repeat
        # cooldown's blind spot and reads to a listener as the same song again.
        #
        # So cool a course track down against the other course tracks rather than
        # against the last few plays: it cannot return until the rest of the set has
        # had its turn. The set still takes its full share of the show, it just
        # cycles instead of repeating. This is a strict filter and relaxes with the
        # others below, so a course can never starve selection.
        heading_recent_keys: set[str] = set()
        active_heading = self.heading
        if active_heading is not None and active_heading.id:
            # Size the set from what could actually be picked. An explicit course track
            # under allow_explicit=False is not selectable, so counting it would let the
            # cooldown exclude every track that is.
            course_keys = {
                track.cache_key
                for track in pool
                if track.heading_id == active_heading.id and (allow_explicit or not track.explicit)
            }
            if len(course_keys) > 1:
                # Match history against course_keys, not the heading id: a course track
                # that has since been banned or dropped from the pool is not something
                # the set can cycle back to, so it must not consume a cooldown slot and
                # let a current track return early.
                for played in reversed(self.played_tracks):
                    if played.cache_key not in course_keys:
                        continue
                    heading_recent_keys.add(played.cache_key)
                    if len(heading_recent_keys) >= len(course_keys) - 1:
                        break

        # --- Hard filters (progressively relaxed) ---
        def _apply_filters(candidates: list[Track], *, strict: bool = True) -> list[Track]:
            result = candidates
            if not allow_explicit:
                result = [t for t in result if not t.explicit]
            if strict and heading_recent_keys:
                result = [t for t in result if t.cache_key not in heading_recent_keys]
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
        heading = active_heading
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
        """Advance queue-scheduling state after a music segment is admitted.

        Starter-cycle and Jamendo cadence accounting deliberately live in
        ``commit_music_admission`` because those rights-sensitive facts advance
        only when playback begins. The remaining counters preserve the
        producer's established lookahead and pacing semantics.
        """
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

    def record_completed_ad_break(self, brands: Collection[str]) -> None:
        """Increment process-local counts for one credited ad break."""
        normalized = [brand.strip() for brand in brands if isinstance(brand, str) and brand.strip()]
        if not normalized:
            return
        self.ad_experiment_completed_breaks += 1
        for brand in normalized:
            self.ad_experiment_brand_airings[brand] = self.ad_experiment_brand_airings.get(brand, 0) + 1

    def ad_experiment_snapshot(self) -> dict[str, object]:
        """Return the public process-local receipt payload."""
        brands = [
            {"brand": brand, "completed_airings": count}
            for brand, count in sorted(
                self.ad_experiment_brand_airings.items(),
                key=lambda item: (-item[1], item[0].casefold(), item[0]),
            )
        ]
        return {
            "scope": "runtime",
            "completed_breaks": self.ad_experiment_completed_breaks,
            "completed_spots": sum(self.ad_experiment_brand_airings.values()),
            "brands": brands,
        }

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
    Music source is not an AI tier gate: starter/local music is independent of
    the optional standalone external-media resolver.
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
