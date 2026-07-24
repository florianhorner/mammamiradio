"""Identity-free station listening epochs and companionship-cue lifecycle.

The stream hub owns connection membership.  This module turns its aggregate
membership count into a coarser, in-memory station epoch.  It never identifies
listeners and deliberately resets on process restart.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

LISTENER_SESSION_GAP_SECONDS = 600.0
COMPANIONSHIP_MIN_ACTIVE_SECONDS = 1800.0


class ListenerSessionTransitionKind(StrEnum):
    """Membership edge observed by the station session state machine."""

    STARTED = "started"
    RESUMED = "resumed"
    BECAME_EMPTY = "became_empty"
    ACTIVE_COUNT_CHANGED = "active_count_changed"


class ListenerSessionCueState(StrEnum):
    """One-shot companionship cue state for the current station epoch."""

    UNAVAILABLE = "unavailable"
    AVAILABLE = "available"
    ATTEMPTED = "attempted"
    QUEUED = "queued"
    CONSUMED = "consumed"
    ABANDONED = "abandoned"


class CompanionshipDurationBucket(StrEnum):
    """Coarse active-listening duration shared with the scriptwriter."""

    MINUTES_30_TO_44 = "30-44_minutes"
    MINUTES_45_TO_59 = "45-59_minutes"
    MINUTES_60_TO_89 = "60-89_minutes"
    MINUTES_90_PLUS = "90_plus_minutes"

    @property
    def spoken_label(self) -> str:
        """Natural-language duration that does not expose exact timing."""

        return {
            self.MINUTES_30_TO_44: "roughly half an hour",
            self.MINUTES_45_TO_59: "the better part of an hour",
            self.MINUTES_60_TO_89: "more than an hour",
            self.MINUTES_90_PLUS: "well over an hour",
        }[self]


@dataclass(frozen=True, slots=True)
class CompanionshipPromptContext:
    """The complete identity-free session context allowed into an LLM prompt."""

    duration_bucket: CompanionshipDurationBucket

    def to_prompt_context(self) -> str:
        """Return the bounded companionship instruction for one generation."""

        return (
            "COMPANIONSHIP CUE: The station has had company for "
            f"{self.duration_bucket.spoken_label}. Acknowledge the ongoing shared "
            "listening moment once, warmly and in aggregate. Make both the shared-company "
            "idea and this coarse duration clear in the spoken lines. Do not imply that "
            "anyone just arrived, returned, connected, or is individually known."
        )

    def is_used_by(self, texts: str | Iterable[str]) -> bool:
        """Verify that final copy visibly used this bounded cue context.

        Model-returned proof fields are useful shape checks, but they are not
        evidence by themselves.  The application also requires one aggregate
        companionship marker and one phrase from the claimed coarse duration
        bucket before a segment can carry the cue stamp.
        """

        if isinstance(texts, str):
            texts = (texts,)
        normalized = " ".join(str(text) for text in texts).casefold().replace("’", "'")
        shared = any(pattern.search(normalized) for pattern in _COMPANIONSHIP_SHARED_PATTERNS)
        matched_buckets = {
            bucket
            for bucket, patterns in _COMPANIONSHIP_DURATION_PATTERNS.items()
            if any(pattern.search(normalized) for pattern in patterns)
        }
        return shared and matched_buckets == {self.duration_bucket}


_COMPANIONSHIP_SHARED_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bcompany\b",
        r"\btogether\b",
        r"\bshared\s+(?:moment|listening)\b",
        r"\bwith\s+us\b",
        r"\bcompagnia\b",
        r"\binsieme\b",
        r"\bcon\s+noi\b",
        r"\b(?:momento|ascolto)\s+condiviso\b",
    )
)

_COMPANIONSHIP_DURATION_PATTERNS: dict[CompanionshipDurationBucket, tuple[re.Pattern[str], ...]] = {
    CompanionshipDurationBucket.MINUTES_30_TO_44: tuple(
        re.compile(pattern)
        for pattern in (
            r"\bhalf\s+an\s+hour\b",
            r"\bmezz[' ]ora\b",
        )
    ),
    CompanionshipDurationBucket.MINUTES_45_TO_59: tuple(
        re.compile(pattern)
        for pattern in (
            r"\bbetter\s+part\s+of\s+an\s+hour\b",
            r"\balmost\s+an\s+hour(?!\s+and\s+a\s+half)\b",
            r"\bquasi\s+un'ora(?!\s+e\s+mezza)\b",
            r"\bbuona\s+parte\s+di\s+un'ora\b",
            r"\btre\s+quarti\s+d'ora\b",
        )
    ),
    CompanionshipDurationBucket.MINUTES_60_TO_89: tuple(
        re.compile(pattern)
        for pattern in (
            r"\bmore\s+than\s+an\s+hour(?!\s+and\s+a\s+half)\b",
            r"(?<!well\s)\bover\s+an\s+hour(?!\s+and\s+a\s+half)\b",
            r"\bpiù\s+di\s+un'ora(?!\s+e\s+mezza)\b",
            r"\boltre\s+un'ora(?!\s+e\s+mezza)\b",
        )
    ),
    CompanionshipDurationBucket.MINUTES_90_PLUS: tuple(
        re.compile(pattern)
        for pattern in (
            r"\bwell\s+over\s+an\s+hour\b",
            r"\b(?:more\s+than|over)\s+an\s+hour\s+and\s+a\s+half\b",
            r"\b(?:più\s+di|oltre)\s+un'ora\s+e\s+mezza\b",
        )
    ),
}


@dataclass(frozen=True, slots=True)
class ListenerSessionCueClaim:
    """Producer-owned claim; only ``prompt_context`` may cross the LLM boundary."""

    epoch: int
    prompt_context: CompanionshipPromptContext


@dataclass(frozen=True, slots=True)
class ListenerSessionTransition:
    """Result of one active-listener membership mutation."""

    kind: ListenerSessionTransitionKind
    epoch: int
    active_count: int
    monotonic_at: float
    accumulated_active_seconds: float

    @property
    def started_new_epoch(self) -> bool:
        """Whether this edge opened a new station listening epoch."""

        return self.kind is ListenerSessionTransitionKind.STARTED


@dataclass(frozen=True, slots=True)
class ListenerSessionSnapshot:
    """Immutable, anonymous admin diagnostic for the current station epoch."""

    epoch: int
    phase: str
    active_count: int
    accumulated_active_seconds: float
    empty_for_seconds: float
    persona_pending_count: int
    companionship_cue_state: ListenerSessionCueState
    companionship_duration_bucket: CompanionshipDurationBucket | None
    companionship_eligible: bool

    @property
    def persona_pending(self) -> bool:
        """Whether at least one epoch still needs its persona commit."""

        return self.persona_pending_count > 0

    def to_dict(self) -> dict[str, object]:
        """Return the admin-safe representation without receipt identifiers."""

        return {
            "epoch": self.epoch,
            "phase": self.phase,
            "active_count": self.active_count,
            "accumulated_active_seconds": round(self.accumulated_active_seconds, 3),
            "empty_for_seconds": round(self.empty_for_seconds, 3),
            "persona_pending": self.persona_pending,
            "persona_pending_count": self.persona_pending_count,
            "companionship_cue_state": self.companionship_cue_state.value,
            "companionship_duration_bucket": (
                self.companionship_duration_bucket.value if self.companionship_duration_bucket else None
            ),
            "companionship_eligible": self.companionship_eligible,
        }


class ListenerSession:
    """In-memory station session state driven by active hub membership."""

    COMPANIONSHIP_MIN_ACTIVE_SECONDS = COMPANIONSHIP_MIN_ACTIVE_SECONDS

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        gap_seconds: float = LISTENER_SESSION_GAP_SECONDS,
    ) -> None:
        self._monotonic = monotonic
        self._gap_seconds = max(0.0, float(gap_seconds))
        self._epoch = 0
        self._active_count = 0
        self._active_since: float | None = None
        self._empty_since: float | None = None
        self._accumulated_active_seconds = 0.0
        self._pending_persona_epochs: set[int] = set()
        self._companionship_cue_state = ListenerSessionCueState.UNAVAILABLE

    @property
    def epoch(self) -> int:
        """Current in-process station epoch, or zero before first activity."""

        return self._epoch

    @property
    def active_count(self) -> int:
        """Current active stream membership count."""

        return self._active_count

    @property
    def companionship_cue_state(self) -> ListenerSessionCueState:
        """Stored lifecycle state for the current epoch."""

        return self._companionship_cue_state

    @property
    def pending_persona_epochs(self) -> tuple[int, ...]:
        """Internal receipt worklist, oldest epoch first."""

        return tuple(sorted(self._pending_persona_epochs))

    @property
    def oldest_pending_persona_epoch(self) -> int | None:
        """Return the next receipt epoch without sorting the whole backlog."""

        return min(self._pending_persona_epochs, default=None)

    def monotonic_now(self) -> float:
        """Read the injected clock used by this state machine."""

        return float(self._monotonic())

    def observe_active_count(
        self,
        active_count: int,
        *,
        now: float | None = None,
    ) -> ListenerSessionTransition | None:
        """Apply one authoritative hub membership count transition.

        A ``0 -> positive`` edge starts a new epoch only after the room has
        remained empty for at least ``gap_seconds``.  Churn while membership is
        positive and reconnects inside the grace window retain the epoch.
        """

        if isinstance(active_count, bool) or not isinstance(active_count, int) or active_count < 0:
            raise ValueError(f"active_count must be a non-negative integer, got {active_count!r}")
        if active_count == self._active_count:
            return None

        current_time = self._resolve_now(now)
        previous_count = self._active_count
        transition_kind = ListenerSessionTransitionKind.ACTIVE_COUNT_CHANGED

        if previous_count == 0 and active_count > 0:
            empty_for = current_time - self._empty_since if self._empty_since is not None else self._gap_seconds
            starts_new_epoch = self._epoch == 0 or self._empty_since is None or empty_for >= self._gap_seconds
            if starts_new_epoch:
                self._epoch += 1
                self._accumulated_active_seconds = 0.0
                self._companionship_cue_state = ListenerSessionCueState.UNAVAILABLE
                transition_kind = ListenerSessionTransitionKind.STARTED
                self._pending_persona_epochs.add(self._epoch)
            else:
                transition_kind = ListenerSessionTransitionKind.RESUMED
            self._active_since = current_time
            self._empty_since = None

        elif previous_count > 0 and active_count == 0:
            self._accumulated_active_seconds += self._active_duration_until(current_time)
            self._active_since = None
            self._empty_since = current_time
            transition_kind = ListenerSessionTransitionKind.BECAME_EMPTY
            if self._companionship_cue_state is ListenerSessionCueState.AVAILABLE:
                self._companionship_cue_state = ListenerSessionCueState.UNAVAILABLE

        self._active_count = active_count
        self.refresh_companionship_availability(now=current_time)
        return ListenerSessionTransition(
            kind=transition_kind,
            epoch=self._epoch,
            active_count=active_count,
            monotonic_at=current_time,
            accumulated_active_seconds=self._accumulated_active_seconds,
        )

    def refresh_companionship_availability(self, *, now: float | None = None) -> ListenerSessionCueState:
        """Refresh the reversible pre-claim availability state."""

        if self._companionship_cue_state not in {
            ListenerSessionCueState.UNAVAILABLE,
            ListenerSessionCueState.AVAILABLE,
        }:
            return self._companionship_cue_state
        current_time = self._resolve_now(now)
        eligible = self._companionship_is_eligible(current_time)
        self._companionship_cue_state = (
            ListenerSessionCueState.AVAILABLE if eligible else ListenerSessionCueState.UNAVAILABLE
        )
        return self._companionship_cue_state

    def claim_companionship(self, *, now: float | None = None) -> ListenerSessionCueClaim | None:
        """Atomically claim the current epoch's one allowed companionship attempt."""

        current_time = self._resolve_now(now)
        if self.refresh_companionship_availability(now=current_time) is not ListenerSessionCueState.AVAILABLE:
            return None
        bucket = companionship_duration_bucket(self._active_seconds_at(current_time))
        if bucket is None:
            return None
        self._companionship_cue_state = ListenerSessionCueState.ATTEMPTED
        return ListenerSessionCueClaim(
            epoch=self._epoch,
            prompt_context=CompanionshipPromptContext(duration_bucket=bucket),
        )

    def mark_companionship_queued(self, epoch: int) -> bool:
        """Transfer a claimed cue to an admitted queue segment."""

        if not self._matches_epoch(epoch) or self._active_count <= 0:
            return False
        if self._companionship_cue_state is not ListenerSessionCueState.ATTEMPTED:
            return False
        self._companionship_cue_state = ListenerSessionCueState.QUEUED
        return True

    def mark_companionship_consumed(self, epoch: int) -> bool:
        """Settle a queued cue after at least one listener accepted audio."""

        if not self._matches_epoch(epoch):
            return False
        if self._companionship_cue_state is not ListenerSessionCueState.QUEUED:
            return False
        self._companionship_cue_state = ListenerSessionCueState.CONSUMED
        return True

    def abandon_companionship(self, epoch: int) -> bool:
        """Permanently settle a failed claimed or queued cue for its epoch."""

        if not self._matches_epoch(epoch):
            return False
        if self._companionship_cue_state not in {
            ListenerSessionCueState.ATTEMPTED,
            ListenerSessionCueState.QUEUED,
        }:
            return False
        self._companionship_cue_state = ListenerSessionCueState.ABANDONED
        return True

    def mark_persona_recorded(self, epoch: int) -> bool:
        """Acknowledge a committed epoch exactly once."""

        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
            return False
        if epoch not in self._pending_persona_epochs:
            return False
        self._pending_persona_epochs.remove(epoch)
        return True

    def snapshot(self, *, now: float | None = None) -> ListenerSessionSnapshot:
        """Take a stable snapshot without exposing receipt keys or identity."""

        current_time = self._resolve_now(now)
        if self._active_count > 0:
            accumulated = self._active_seconds_at(current_time)
            empty_for = 0.0
            phase = "active"
        elif self._epoch > 0 and self._empty_since is not None:
            accumulated = self._accumulated_active_seconds
            empty_for = max(0.0, current_time - self._empty_since)
            phase = "grace"
        else:
            accumulated = self._accumulated_active_seconds
            empty_for = 0.0
            phase = "empty"

        cue_state = self._companionship_cue_state
        eligible = self._companionship_is_eligible(current_time)
        if cue_state in {ListenerSessionCueState.UNAVAILABLE, ListenerSessionCueState.AVAILABLE}:
            cue_state = ListenerSessionCueState.AVAILABLE if eligible else ListenerSessionCueState.UNAVAILABLE
        return ListenerSessionSnapshot(
            epoch=self._epoch,
            phase=phase,
            active_count=self._active_count,
            accumulated_active_seconds=max(0.0, accumulated),
            empty_for_seconds=empty_for,
            persona_pending_count=len(self._pending_persona_epochs),
            companionship_cue_state=cue_state,
            companionship_duration_bucket=companionship_duration_bucket(accumulated),
            companionship_eligible=eligible,
        )

    def _companionship_is_eligible(self, current_time: float) -> bool:
        return (
            self._epoch > 0
            and self._active_count > 0
            and self._active_seconds_at(current_time) >= self.COMPANIONSHIP_MIN_ACTIVE_SECONDS
        )

    def _matches_epoch(self, epoch: int) -> bool:
        return not isinstance(epoch, bool) and isinstance(epoch, int) and epoch > 0 and epoch == self._epoch

    def _resolve_now(self, now: float | None) -> float:
        return self.monotonic_now() if now is None else float(now)

    def _active_seconds_at(self, current_time: float) -> float:
        return max(0.0, self._accumulated_active_seconds + self._active_duration_until(current_time))

    def _active_duration_until(self, current_time: float) -> float:
        if self._active_since is None:
            return 0.0
        return max(0.0, current_time - self._active_since)


def companionship_duration_bucket(active_seconds: float) -> CompanionshipDurationBucket | None:
    """Return the bounded prompt bucket for accumulated active-listening time."""

    seconds = max(0.0, float(active_seconds))
    if seconds < COMPANIONSHIP_MIN_ACTIVE_SECONDS:
        return None
    if seconds < 45 * 60:
        return CompanionshipDurationBucket.MINUTES_30_TO_44
    if seconds < 60 * 60:
        return CompanionshipDurationBucket.MINUTES_45_TO_59
    if seconds < 90 * 60:
        return CompanionshipDurationBucket.MINUTES_60_TO_89
    return CompanionshipDurationBucket.MINUTES_90_PLUS


def persona_session_id(epoch: int) -> str:
    """Return the logical epoch key; PersonaStore adds a process receipt token."""

    return f"listener-epoch-{int(epoch)}"
