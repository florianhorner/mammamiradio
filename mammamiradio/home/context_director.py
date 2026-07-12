"""Session-scoped, privacy-safe rotation for ambient Home Assistant facts.

The director deliberately owns only selection and queue lifecycle bookkeeping.
It never polls Home Assistant, persists policy, builds a full host prompt, or
serializes listener payloads.  Callers project a filtered HA snapshot into
``DirectorObservation`` instances, then pass the selected ``PromptFact`` to
the scriptwriter.  Only the fact's opaque id and controlled prompt text should
cross that boundary.

Facts are reserved after a segment has been admitted to the queue and start
their cooldown only once the segment starts streaming.  This keeps lookahead
from producing the same ambient topic twice without claiming that a discarded
segment was heard.
"""

from __future__ import annotations

import hashlib
import math
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

COOLDOWN_SECONDS = 30 * 60
TEMPERATURE_REOPEN_DELTA_C = 2.0

# This is intentionally a small, explicit allowlist.  A source being present
# in HomeContext.scored does not make it appropriate routine on-air material.
CURATED_COFFEE_ENTITY_IDS = frozenset(
    {
        "switch.bar_kaffeemaschine_steckdose",
        "input_select.kaffee_dad_jokes",
    }
)
PRESENCE_DEVICE_CLASSES = frozenset({"occupancy", "presence", "motion"})

_WEATHER_STATES = {
    "clear-night": "sereno di notte",
    "cloudy": "nuvoloso",
    "exceptional": "insolito",
    "fog": "nebbioso",
    "hail": "con grandine",
    "lightning": "temporalesco",
    "lightning-rainy": "con temporali e pioggia",
    "partlycloudy": "parzialmente nuvoloso",
    "pouring": "con pioggia intensa",
    "rainy": "piovoso",
    "snowy": "nevoso",
    "snowy-rainy": "con neve e pioggia",
    "sunny": "soleggiato",
    "windy": "ventoso",
    "windy-variant": "ventoso",
}
_CLIMATE_STATES = {
    "auto": "in automatico",
    "cool": "in raffrescamento",
    "dry": "in deumidificazione",
    "fan_only": "con la ventilazione attiva",
    "heat": "con il riscaldamento attivo",
    "heat_cool": "in automatico",
    "off": "spento",
}
_VACUUM_STATES = {
    "cleaning": "sta pulendo",
    "docked": "è alla base",
    "idle": "è in attesa",
    "paused": "è in pausa",
    "returning": "sta tornando alla base",
}
_SUN_STATES = {
    "above_horizon": "il sole è sopra l'orizzonte",
    "below_horizon": "è già notte",
}
_MAX_COUNTER = 9_999
_MAX_SETTLED_QUEUE_IDS = 256
_MAX_ISSUED_FACTS = 512


@dataclass(frozen=True, slots=True)
class DirectorObservation:
    """Minimal, typed projection of one potentially usable HA state.

    This model intentionally has no raw attributes, labels, summaries, or
    event text.  It makes the allowlist decision before any state can become
    prompt material and keeps raw entity labels out of generated copy.
    """

    entity_id: str
    domain: str
    state: str
    score: float = 0.0
    temperature_c: float | None = None
    target_temperature_c: float | None = None
    device_class: str | None = None
    area: str | None = None

    @classmethod
    def from_home_assistant_state(
        cls,
        entity_id: object,
        state_data: Mapping[str, object],
        *,
        score: object = 0.0,
        area: object = None,
    ) -> DirectorObservation | None:
        """Create a strict projection from one Home Assistant state payload.

        The full payload is consumed at this trust boundary and is never kept
        on the resulting object.  Unsupported, unavailable, malformed, or
        non-finite values return ``None`` rather than reaching a host prompt.
        """

        if not isinstance(entity_id, str) or "." not in entity_id:
            return None
        # Fail closed on a non-mapping payload (None, list, scalar): the docstring
        # promises malformed input returns None, and state_data.get(...) below
        # would otherwise raise into the producer's HA projection loop.
        if not isinstance(state_data, Mapping):
            return None
        domain, object_id = entity_id.split(".", 1)
        if not _safe_identifier_piece(domain) or not _safe_identifier_piece(object_id):
            return None
        raw_state = state_data.get("state")
        if not isinstance(raw_state, str):
            return None
        state = raw_state.strip().lower()
        if not state or state in {"unknown", "unavailable", "none"}:
            return None
        attrs_raw = state_data.get("attributes", {})
        attrs = attrs_raw if isinstance(attrs_raw, Mapping) else {}
        parsed_score = _finite_number(score)
        if parsed_score is None:
            return None
        device_class = _safe_token(attrs.get("device_class"))
        observed_area = _safe_area(area if area is not None else attrs.get("area") or attrs.get("area_name"))

        temperature_c: float | None = None
        target_temperature_c: float | None = None
        if domain == "weather":
            temperature_c = _temperature(attrs.get("temperature"))
            if temperature_c is None:
                return None
        elif domain == "climate":
            temperature_c = _temperature(attrs.get("current_temperature"))
            target_temperature_c = _temperature(attrs.get("temperature"))
            if temperature_c is None and target_temperature_c is None:
                return None
        elif domain == "sensor" and device_class == "temperature":
            # A numeric temperature sensor is allowed only when Home Assistant
            # has classified it explicitly.  Other ``sensor.*`` values remain
            # deny-by-default, and the raw state never crosses this boundary.
            temperature_c = _temperature(raw_state)
            if temperature_c is None:
                return None

        return cls(
            entity_id=entity_id,
            domain=domain,
            state=state,
            score=parsed_score,
            temperature_c=temperature_c,
            target_temperature_c=target_temperature_c,
            device_class=device_class,
            area=observed_area,
        )


@dataclass(frozen=True, slots=True)
class PromptFact:
    """Immutable, safe prompt input selected for exactly one casual break."""

    fact_id: str
    entity_id: str
    topic_key: str
    fingerprint: str
    prompt: str
    policy_revision: int

    def segment_metadata(self) -> dict[str, str | int]:
        """Return internal lifecycle metadata for a queued segment.

        Callers must add these keys to their shared public-metadata exclusion
        list.  This helper is intentionally explicit to avoid retyping keys at
        each queue lifecycle seam.
        """

        return {
            "home_fact_id": self.fact_id,
            "home_fact_entity_id": self.entity_id,
            "home_fact_topic_key": self.topic_key,
            "home_fact_fingerprint": self.fingerprint,
            "home_fact_policy_revision": self.policy_revision,
        }


@dataclass(frozen=True, slots=True)
class _Candidate:
    entity_id: str
    topic_key: str
    source: Literal["weather", "climate", "temperature", "vacuum", "sun", "coffee", "coffee_joke", "presence"]
    score: float
    fingerprint: str
    prompt: str
    discrete_state: str
    temperature_c: float | None
    target_temperature_c: float | None


@dataclass(slots=True)
class _IssuedFact:
    fact: PromptFact
    candidate: _Candidate
    state: Literal["selected", "reserved", "activated", "released"] = "selected"


@dataclass(frozen=True, slots=True)
class _Reservation:
    queue_id: str
    fact_id: str
    entity_id: str
    topic_key: str
    fingerprint: str
    policy_revision: int
    candidate: _Candidate


@dataclass(frozen=True, slots=True)
class _SettledQueue:
    """Terminal queue lifecycle state used to make callbacks idempotent."""

    fact_id: str
    terminal_state: Literal["activated", "released"]
    revision_current: bool


@dataclass(frozen=True, slots=True)
class _Cooldown:
    activated_at: float
    source: str
    fingerprint: str
    discrete_state: str
    temperature_c: float | None
    target_temperature_c: float | None


class HomeContextDirector:
    """Choose safe ambient facts and track their queue-to-stream lifecycle.

    The instance is intentionally session-scoped.  Constructing a new one
    resets reservations and cooldowns; no state is written to disk.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._clock = clock or time.time
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._policy_revision = 0
        self._candidates: dict[str, _Candidate] = {}
        self._personal_moment_eligible_ids: set[str] = set()
        self._issued_facts: OrderedDict[str, _IssuedFact] = OrderedDict()
        self._reservations: dict[str, _Reservation] = {}
        self._reserved_topics: dict[str, str] = {}
        self._cooldowns: dict[str, _Cooldown] = {}
        self._settled_queue_ids: OrderedDict[str, _SettledQueue] = OrderedDict()
        self._rotation_topic: str | None = None
        self._counters = {
            "selected": 0,
            "reserved": 0,
            "activated": 0,
            "released": 0,
            "policy_rejected": 0,
            "repaired": 0,
            "fact_free_fallback": 0,
        }
        self._last_outcome = "waiting"
        self._last_changed_at = self._now()

    @property
    def policy_revision(self) -> int:
        """Return the latest policy revision known to this session."""

        return self._policy_revision

    def observe(
        self,
        observations: Iterable[DirectorObservation],
        *,
        policy_revision: int,
        muted_entity_ids: Iterable[str] = (),
        personal_moment_opt_ins: Iterable[str] = (),
    ) -> None:
        """Replace the current projected snapshot with safe eligible topics.

        A lower policy revision is ignored rather than allowing a stale HA
        refresh to undo a newer mute or consent decision.
        """

        if not _valid_revision(policy_revision):
            raise ValueError("policy_revision must be a non-negative integer")
        if policy_revision < self._policy_revision:
            self._record("stale_policy_observation")
            return
        if policy_revision > self._policy_revision:
            self._policy_revision = policy_revision
            self._record("policy_updated")

        muted = {entity_id for entity_id in muted_entity_ids if isinstance(entity_id, str)}
        opt_ins = {entity_id for entity_id in personal_moment_opt_ins if isinstance(entity_id, str)}
        by_topic: dict[str, _Candidate] = {}
        eligible_presence_ids: set[str] = set()
        for observation in observations:
            if not isinstance(observation, DirectorObservation) or observation.entity_id in muted:
                continue
            if _is_personal_moment_eligible(observation):
                eligible_presence_ids.add(observation.entity_id)
            candidate = _candidate_for(observation, allow_presence=observation.entity_id in opt_ins)
            if candidate is None:
                continue
            current = by_topic.get(candidate.topic_key)
            if current is None or _candidate_rank(candidate) < _candidate_rank(current):
                by_topic[candidate.topic_key] = candidate
        self._candidates = by_topic
        self._personal_moment_eligible_ids = eligible_presence_ids
        if not by_topic and not self._reservations and not self._cooldowns:
            self._record("waiting")

    def set_policy_revision(self, policy_revision: int) -> bool:
        """Advance the monotonic policy revision without replacing a snapshot."""

        if not _valid_revision(policy_revision):
            raise ValueError("policy_revision must be a non-negative integer")
        if policy_revision < self._policy_revision:
            self._record("stale_policy_observation")
            return False
        if policy_revision > self._policy_revision:
            self._policy_revision = policy_revision
            self._record("policy_updated")
        return True

    def personal_moment_eligible(self, entity_id: str) -> bool:
        """Return whether the current typed snapshot permits presence consent."""

        return entity_id in self._personal_moment_eligible_ids

    def pending_queue_ids_for_entity(self, entity_id: str) -> tuple[str, ...]:
        """Return unstarted queues that must be purged when an entity is muted."""

        return tuple(
            queue_id for queue_id, reservation in self._reservations.items() if reservation.entity_id == entity_id
        )

    def invalidate_entity(self, entity_id: str, *, policy_revision: int) -> tuple[str, ...]:
        """Invalidate an entity and report its unstarted reservations.

        The caller deliberately performs the actual queue removal and then
        calls :meth:`release`; this keeps all queue discard paths central.
        """

        if not self.set_policy_revision(policy_revision):
            return ()
        self._candidates = {
            topic_key: candidate
            for topic_key, candidate in self._candidates.items()
            if candidate.entity_id != entity_id
        }
        self._personal_moment_eligible_ids.discard(entity_id)
        queue_ids = self.pending_queue_ids_for_entity(entity_id)
        self._record("entity_invalidated")
        return queue_ids

    def select(self, *, lane: str = "casual") -> PromptFact | None:
        """Select one safe fact for a casual host break, or ``None``.

        Deliberate weather flashes, ritual moments, and reactive directives
        bypass this selector.  Passing their lane explicitly keeps accidental
        reuse visible in unit tests while preserving a fact-free result.
        """

        if lane != "casual":
            self._record("lane_bypass")
            return None
        now = self._now()
        eligible = [
            candidate
            for candidate in self._candidates.values()
            if candidate.topic_key not in self._reserved_topics and not self._cooldown_blocks(candidate, now)
        ]
        if not eligible:
            self._record("no_eligible_fact")
            return None

        ordered = sorted(eligible, key=_candidate_rank)
        candidate = self._rotated_choice(ordered)
        fact_id = self._new_fact_id()
        fact = PromptFact(
            fact_id=fact_id,
            entity_id=candidate.entity_id,
            topic_key=candidate.topic_key,
            fingerprint=candidate.fingerprint,
            prompt=candidate.prompt,
            policy_revision=self._policy_revision,
        )
        self._issued_facts[fact_id] = _IssuedFact(fact=fact, candidate=candidate)
        self._trim_issued_facts()
        self._rotation_topic = candidate.topic_key
        self._increment("selected")
        self._record("selected")
        return fact

    def is_policy_current(self, fact: PromptFact) -> bool:
        """Return whether a selected fact still belongs to the current policy."""

        issued = self._issued_facts.get(fact.fact_id)
        return issued is not None and issued.fact == fact and fact.policy_revision == self._policy_revision

    def reserve(self, queue_id: str, fact: PromptFact) -> bool:
        """Claim a selected fact after successful queue admission.

        Queue-id retries are idempotent.  A stale policy, unknown fact, or an
        existing reservation for the topic is rejected without altering state.
        """

        if not _safe_queue_id(queue_id):
            self._record("reservation_rejected")
            return False
        existing = self._reservations.get(queue_id)
        if existing is not None:
            return existing.fact_id == fact.fact_id
        settled = self._settled_queue_ids.get(queue_id)
        if settled is not None and settled.fact_id == fact.fact_id:
            return False
        issued = self._issued_facts.get(fact.fact_id)
        if issued is None or issued.fact != fact or issued.state != "selected" or not self.is_policy_current(fact):
            self._increment("policy_rejected")
            self._record("reservation_rejected")
            return False
        if fact.topic_key in self._reserved_topics or self._cooldown_blocks(issued.candidate, self._now()):
            self._record("reservation_rejected")
            return False
        reservation = _Reservation(
            queue_id=queue_id,
            fact_id=fact.fact_id,
            entity_id=fact.entity_id,
            topic_key=fact.topic_key,
            fingerprint=fact.fingerprint,
            policy_revision=fact.policy_revision,
            candidate=issued.candidate,
        )
        self._reservations[queue_id] = reservation
        self._reserved_topics[fact.topic_key] = queue_id
        issued.state = "reserved"
        self._increment("reserved")
        self._record("reserved")
        return True

    def reserve_by_id(self, queue_id: str, fact_id: str) -> bool:
        """Reserve a still-selected fact by its opaque id.

        The producer's shared queue-admission path only carries segment metadata
        (a fact id, not the ``PromptFact`` object). It looks the issued fact up
        here so the reservation can gate admission before the segment enters the
        queue. An unknown or already-consumed id is rejected like any other
        stale reservation.
        """

        if not isinstance(fact_id, str) or not fact_id:
            return False
        issued = self._issued_facts.get(fact_id)
        if issued is None:
            self._record("reservation_rejected")
            return False
        return self.reserve(queue_id, issued.fact)

    def activate(self, queue_id: str, *, fact_id: str | None = None) -> bool:
        """Start listener-visible cooldown when a matching segment streams."""

        # Pop the reservation and topic lock BEFORE evaluating the rejection
        # branches. activate() runs once at stream start and its return value is
        # discarded by the caller (best-effort bookkeeping, never an audio gate),
        # and an aired segment never reaches a release() path — so returning
        # early here would leak the reservation and permanently exclude the topic
        # family for the rest of the session. Mirror release()'s pop-then-account.
        reservation = self._reservations.pop(queue_id, None)
        if reservation is None:
            settled = self._settled_queue_ids.get(queue_id)
            return bool(
                fact_id
                and settled is not None
                and settled.fact_id == fact_id
                and settled.terminal_state == "activated"
                and settled.revision_current
            )
        self._reserved_topics.pop(reservation.topic_key, None)
        if fact_id is not None and reservation.fact_id != fact_id:
            issued = self._issued_facts.get(reservation.fact_id)
            if issued is not None:
                issued.state = "released"
            self._remember_settled(
                queue_id,
                reservation.fact_id,
                terminal_state="released",
                revision_current=False,
            )
            self._increment("released")
            self._record("activation_rejected")
            return False

        revision_current = reservation.policy_revision == self._policy_revision
        self._cooldowns[reservation.topic_key] = _Cooldown(
            activated_at=self._now(),
            source=reservation.candidate.source,
            fingerprint=reservation.fingerprint,
            discrete_state=reservation.candidate.discrete_state,
            temperature_c=reservation.candidate.temperature_c,
            target_temperature_c=reservation.candidate.target_temperature_c,
        )
        issued = self._issued_facts.get(reservation.fact_id)
        if issued is not None:
            issued.state = "activated"
        self._remember_settled(
            queue_id,
            reservation.fact_id,
            terminal_state="activated",
            revision_current=revision_current,
        )
        self._increment("activated")
        if not revision_current:
            self._increment("policy_rejected")
            self._record("activation_rejected")
            return False
        self._record("activated")
        return True

    def release(self, queue_id: str, *, fact_id: str | None = None) -> bool:
        """Release one matching unstarted reservation after queue discard.

        An activated segment is deliberately not releasable: its cooldown has
        already become listener-visible and must remain in force.
        """

        reservation = self._reservations.get(queue_id)
        if reservation is None:
            return False
        if fact_id is not None and reservation.fact_id != fact_id:
            self._record("release_rejected")
            return False
        self._reservations.pop(queue_id, None)
        self._reserved_topics.pop(reservation.topic_key, None)
        issued = self._issued_facts.get(reservation.fact_id)
        if issued is not None:
            issued.state = "released"
        self._remember_settled(
            queue_id,
            reservation.fact_id,
            terminal_state="released",
            revision_current=False,
        )
        self._increment("released")
        self._record("released")
        return True

    def note_repaired(self) -> None:
        """Record one bounded successful fact-attribution repair."""

        self._increment("repaired")
        self._record("repaired")

    def note_fact_free_fallback(self) -> None:
        """Record terminal fact-free generation fallback without exposing data."""

        self._increment("fact_free_fallback")
        self._record("fact_free_fallback")

    def admin_status(self) -> dict[str, object]:
        """Return count-only operator diagnostics with no household fact data."""

        now = self._now()
        eligible = 0
        cooling = 0
        for candidate in self._candidates.values():
            if candidate.topic_key in self._reserved_topics:
                continue
            if self._cooldown_blocks(candidate, now):
                cooling += 1
            else:
                eligible += 1
        reserved = len(self._reservations)
        mode, message = _status_message(eligible=eligible, cooling=cooling, reserved=reserved)
        return {
            "mode": mode,
            "eligible_count": eligible,
            "cooling_count": cooling,
            "reserved_count": reserved,
            "session_counters": dict(self._counters),
            "last_outcome": self._last_outcome,
            "last_changed_at": self._last_changed_at,
            "operator_message": message,
        }

    def _rotated_choice(self, ordered: list[_Candidate]) -> _Candidate:
        if self._rotation_topic is None:
            return ordered[0]
        for index, candidate in enumerate(ordered):
            if candidate.topic_key == self._rotation_topic:
                return ordered[(index + 1) % len(ordered)]
        return ordered[0]

    def _cooldown_blocks(self, candidate: _Candidate, now: float) -> bool:
        cooldown = self._cooldowns.get(candidate.topic_key)
        if cooldown is None:
            return False
        if now - cooldown.activated_at >= COOLDOWN_SECONDS:
            self._cooldowns.pop(candidate.topic_key, None)
            return False
        return not _reopens_early(candidate, cooldown)

    def _new_fact_id(self) -> str:
        for _ in range(3):
            fact_id = self._id_factory()
            if isinstance(fact_id, str) and _safe_opaque_id(fact_id) and fact_id not in self._issued_facts:
                return fact_id
        raise RuntimeError("could not create a unique opaque home fact id")

    def _trim_issued_facts(self) -> None:
        while len(self._issued_facts) > _MAX_ISSUED_FACTS:
            fact_id, issued = next(iter(self._issued_facts.items()))
            if issued.state == "reserved":
                # Reservations are few and must stay resolvable. Move it to the
                # end and continue looking for a completed/unused issue.
                self._issued_facts.move_to_end(fact_id)
                if all(entry.state == "reserved" for entry in self._issued_facts.values()):
                    return
                continue
            self._issued_facts.popitem(last=False)

    def _remember_settled(
        self,
        queue_id: str,
        fact_id: str,
        *,
        terminal_state: Literal["activated", "released"],
        revision_current: bool,
    ) -> None:
        self._settled_queue_ids[queue_id] = _SettledQueue(
            fact_id=fact_id,
            terminal_state=terminal_state,
            revision_current=revision_current,
        )
        self._settled_queue_ids.move_to_end(queue_id)
        while len(self._settled_queue_ids) > _MAX_SETTLED_QUEUE_IDS:
            self._settled_queue_ids.popitem(last=False)

    def _increment(self, key: str) -> None:
        self._counters[key] = min(_MAX_COUNTER, self._counters[key] + 1)

    def _record(self, outcome: str) -> None:
        self._last_outcome = outcome
        self._last_changed_at = self._now()

    def _now(self) -> float:
        now = self._clock()
        if not isinstance(now, int | float) or not math.isfinite(float(now)):
            raise ValueError("clock must return a finite timestamp")
        return float(now)


def _candidate_for(observation: DirectorObservation, *, allow_presence: bool) -> _Candidate | None:
    """Return a deny-by-default candidate with controlled Italian prompt copy."""

    if not _valid_observation(observation):
        return None
    if observation.domain == "weather":
        condition = _WEATHER_STATES.get(observation.state)
        if condition is None or observation.temperature_c is None:
            return None
        return _candidate(
            observation,
            topic_key="ambient.temperature",
            source="weather",
            discrete_state=observation.state,
            fingerprint_parts=("weather", observation.state, _temperature_fingerprint(observation.temperature_c)),
            prompt=(
                "Usa al massimo una nota di casa, solo se naturale: oggi il meteo è "
                f"{condition}, con {_format_temperature(observation.temperature_c)}. "
                "Non citare fonti tecniche e non aggiungere dettagli non forniti."
            ),
        )
    if observation.domain == "climate":
        mode = _CLIMATE_STATES.get(observation.state)
        if mode is None or (observation.temperature_c is None and observation.target_temperature_c is None):
            return None
        temperature = (
            observation.temperature_c if observation.temperature_c is not None else observation.target_temperature_c
        )
        assert temperature is not None
        target = ""
        if observation.target_temperature_c is not None and observation.target_temperature_c != temperature:
            target = f" (obiettivo {_format_temperature(observation.target_temperature_c)})"
        return _candidate(
            observation,
            topic_key="ambient.temperature",
            source="climate",
            discrete_state=observation.state,
            fingerprint_parts=(
                "climate",
                observation.state,
                _temperature_fingerprint(observation.temperature_c),
                _temperature_fingerprint(observation.target_temperature_c),
            ),
            prompt=(
                "Usa al massimo una nota di casa, solo se naturale: il clima di casa è "
                f"{mode}, intorno a {_format_temperature(temperature)}{target}. "
                "Non citare fonti tecniche e non aggiungere dettagli non forniti."
            ),
        )
    if (
        observation.domain == "sensor"
        and observation.device_class == "temperature"
        and observation.temperature_c is not None
    ):
        return _candidate(
            observation,
            topic_key="ambient.temperature",
            source="temperature",
            discrete_state="temperature",
            fingerprint_parts=("temperature", _temperature_fingerprint(observation.temperature_c)),
            prompt=(
                "Usa al massimo una nota di casa, solo se naturale: in casa ci sono "
                f"circa {_format_temperature(observation.temperature_c)}. "
                "Non citare fonti tecniche e non aggiungere dettagli non forniti."
            ),
        )
    if observation.domain == "vacuum" and observation.state in _VACUUM_STATES:
        return _candidate(
            observation,
            topic_key=f"ambient.vacuum.{observation.entity_id}",
            source="vacuum",
            discrete_state=observation.state,
            fingerprint_parts=("vacuum", observation.state),
            prompt=(
                "Usa al massimo una nota di casa, solo se naturale: "
                f"il robot aspirapolvere {_VACUUM_STATES[observation.state]}. "
                "Non citare fonti tecniche e non aggiungere dettagli non forniti."
            ),
        )
    if observation.domain == "sun" and observation.state in _SUN_STATES:
        return _candidate(
            observation,
            topic_key="ambient.sun",
            source="sun",
            discrete_state=observation.state,
            fingerprint_parts=("sun", observation.state),
            prompt=(
                "Usa al massimo una nota di casa, solo se naturale: "
                f"{_SUN_STATES[observation.state]}. "
                "Non citare fonti tecniche e non aggiungere dettagli non forniti."
            ),
        )
    if observation.entity_id == "switch.bar_kaffeemaschine_steckdose" and observation.state == "on":
        return _candidate(
            observation,
            topic_key="ambient.coffee.machine",
            source="coffee",
            discrete_state="on",
            fingerprint_parts=("coffee", "machine_on"),
            prompt=(
                "Usa al massimo una nota di casa, solo se naturale: "
                "la macchina del caffè è accesa. "
                "Non citare fonti tecniche e non aggiungere dettagli non forniti."
            ),
        )
    if observation.entity_id == "input_select.kaffee_dad_jokes":
        # The entity's arbitrary selected joke is deliberately not retained or
        # rendered.  The safe projection only carries the fact that a curated
        # coffee-joke source is available.
        return _candidate(
            observation,
            topic_key="ambient.coffee.joke",
            source="coffee_joke",
            discrete_state="available",
            fingerprint_parts=("coffee", "joke_available"),
            prompt=(
                "Usa al massimo una nota di casa, solo se naturale: "
                "puoi accennare con leggerezza a una piccola battuta sul caffè. "
                "Non citare fonti tecniche e non riportare testo da Home Assistant."
            ),
        )
    if (
        allow_presence
        and observation.domain == "binary_sensor"
        and observation.device_class in PRESENCE_DEVICE_CLASSES
        and observation.area is not None
        and observation.state in {"on", "occupied"}
    ):
        return _candidate(
            observation,
            topic_key=f"ambient.presence.{observation.entity_id}",
            source="presence",
            discrete_state=observation.state,
            fingerprint_parts=("presence", observation.state),
            prompt=(
                "Usa al massimo una nota di casa, solo se naturale: "
                "c'è un po' di attività in una stanza. "
                "Non nominare persone, stanze, dispositivi o fonti tecniche."
            ),
        )
    return None


def _is_personal_moment_eligible(observation: DirectorObservation) -> bool:
    """Return whether an observation can be offered for explicit consent.

    The entity can be consented while currently quiet (``off``/``vacant``),
    but only an active observation becomes a selected on-air fact.
    """

    return (
        _valid_observation(observation)
        and observation.domain == "binary_sensor"
        and observation.device_class in PRESENCE_DEVICE_CLASSES
        and observation.area is not None
        and observation.state in {"on", "off", "occupied", "vacant"}
    )


def _candidate(
    observation: DirectorObservation,
    *,
    topic_key: str,
    source: Literal["weather", "climate", "temperature", "vacuum", "sun", "coffee", "coffee_joke", "presence"],
    discrete_state: str,
    fingerprint_parts: tuple[str, ...],
    prompt: str,
) -> _Candidate:
    fingerprint = hashlib.sha256("\x1f".join(fingerprint_parts).encode("utf-8")).hexdigest()
    return _Candidate(
        entity_id=observation.entity_id,
        topic_key=topic_key,
        source=source,
        score=observation.score,
        fingerprint=fingerprint,
        prompt=prompt,
        discrete_state=discrete_state,
        temperature_c=observation.temperature_c,
        target_temperature_c=observation.target_temperature_c,
    )


def _candidate_rank(candidate: _Candidate) -> tuple[float, int, str]:
    # Lower is preferred.  Salience wins first; source and entity ties are
    # stable so repeated inputs lead to deterministic rotation.
    source_rank = {"weather": 0, "climate": 1}.get(candidate.source, 2)
    return (-candidate.score, source_rank, candidate.topic_key)


def _reopens_early(candidate: _Candidate, cooldown: _Cooldown) -> bool:
    if candidate.topic_key == "ambient.temperature":
        # Only a same-source move is comparable: weather (outdoor), climate,
        # and a room sensor share this cooldown but measure different things,
        # so an outdoor→indoor swap would otherwise reopen on the absolute
        # gap and defeat the shared temperature-family cooldown.
        if candidate.source != cooldown.source:
            return False
        if _temperature_changed(candidate.temperature_c, cooldown.temperature_c):
            return True
        if _temperature_changed(candidate.target_temperature_c, cooldown.target_temperature_c):
            return True
        # A weather condition or climate mode changed in place is a new
        # listener-relevant condition.
        return candidate.discrete_state != cooldown.discrete_state
    return candidate.fingerprint != cooldown.fingerprint


def _temperature_changed(current: float | None, previous: float | None) -> bool:
    return current is not None and previous is not None and abs(current - previous) >= TEMPERATURE_REOPEN_DELTA_C


def _valid_observation(observation: DirectorObservation) -> bool:
    return (
        _safe_entity_id(observation.entity_id)
        and observation.domain == observation.entity_id.split(".", 1)[0]
        and _safe_token(observation.state) is not None
        and _finite_number(observation.score) is not None
        and (observation.temperature_c is None or _temperature(observation.temperature_c) is not None)
        and (observation.target_temperature_c is None or _temperature(observation.target_temperature_c) is not None)
    )


def _safe_identifier_piece(value: str) -> bool:
    return bool(value) and all(character.islower() or character.isdigit() or character == "_" for character in value)


def _safe_entity_id(value: str) -> bool:
    return value.count(".") == 1 and all(_safe_identifier_piece(piece) for piece in value.split(".", 1))


def _safe_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    if not cleaned or len(cleaned) > 64:
        return None
    if not all(character.islower() or character.isdigit() or character in {"_", "-"} for character in cleaned):
        return None
    return cleaned


def _safe_area(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 80 or any(character in "\r\n\x00" for character in cleaned):
        return None
    return cleaned


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _temperature(value: object) -> float | None:
    temperature = _finite_number(value)
    if temperature is None or not -90.0 <= temperature <= 70.0:
        return None
    return temperature


def _temperature_fingerprint(value: float | None) -> str:
    return "none" if value is None else f"{value:.1f}"


def _format_temperature(value: float) -> str:
    numeric = float(value)
    return f"{numeric:.0f} °C" if numeric.is_integer() else f"{numeric:.1f} °C"


def _safe_queue_id(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 200 and "\x00" not in value


def _safe_opaque_id(value: str) -> bool:
    return (
        bool(value) and len(value) <= 128 and all(character.isalnum() or character in {"-", "_"} for character in value)
    )


def _valid_revision(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _status_message(*, eligible: int, cooling: int, reserved: int) -> tuple[str, str]:
    if eligible:
        return "rotating", "A safe home cue is ready for a future host break."
    if reserved:
        return "queued", "A safe home cue is queued for a future host break."
    if cooling:
        return "resting", "Recently used home cues are resting; hosts will keep breaks ordinary."
    return "waiting", "Waiting for a safe home-context update."
