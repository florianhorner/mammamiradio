"""Evening Memory — the running-gag ledger (Impossible Moments v2, Approach A).

Accumulates *notable, discrete* home events across an evening into a small
persisted ledger, scores them for salience, and surfaces deferred "running gag"
callbacks for the scriptwriter to weave into banter — e.g. "the coffee machine,
on again tonight." The deferral is the charm; latency is a feature here.

Pipeline
--------

    ha_enrichment.diff_states ──► deque[HomeEvent]   (30-min window, re-served
                                        │             every poll — see watermark)
                                        ▼
    EveningLedger.observe(events, now) ─┤  fold NEW events (timestamp > watermark)
                                        │  into aggregated tally buckets
                                        ▼
              buckets: { (entity, transition): {count, first_ts,
                          last_ts, last_spoken_ts, label, states} }
                                        │
    EveningLedger.select_and_render(now)┤  pick one eligible (count>=MIN, off
                                        │  cooldown) gag, weighted by salience,
                                        ▼  gated by an inject probability
                              STASERA gag string (data) ──► scriptwriter fence

Why a watermark: `fetch_home_context()` returns the *same* retained 30-minute
events deque on every poll (ha_context.py). Folding it blindly would count one
event once per poll for 30 minutes. `observe()` only consumes events strictly
newer than the high-water timestamp it has already seen. diff_states stamps a
whole poll-batch with one timestamp, so a `>` comparison consumes each batch
exactly once.

Why aggregation: storing every raw event is unbounded (numeric sensors emit
`227.4 -> 231.8 -> 229.1 ...`). Buckets keyed by (entity, transition) are
bounded by the number of discrete states an allowlisted entity has. Numeric /
continuous states are excluded outright.

PROVISIONAL config: the allowlist, salience weights, cooldown, inject
probability, and render phrasing below are v0 placeholders. They are tuned in
Phase 1 from the design's "Assignment" (hand-collected real evening events) and
the renderer output is golden-snapshotted then. Do not treat them as final.
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from mammamiradio.home.ha_context import BRONZE_ENTITIES, GOLD_ENTITIES, SILVER_ENTITIES
from mammamiradio.home.ha_enrichment import HomeEvent

logger = logging.getLogger(__name__)

LEDGER_FILENAME = "evening_ledger.json"
_SCHEMA_VERSION = 1

# --- PROVISIONAL tuning (Phase 1 finalizes from the Assignment data) ---------
# Discrete-toggle entities whose state changes read as "Nth time tonight" gags.
# Deliberately NOT "GOLD+SILVER": tiers are curation priority, not gag-worthiness
# (codex review). Weather, climate temps, media titles, power, brightness do not
# make running-gag callbacks. Phase 1 tunes this set from real collected events.
_GAG_ENTITY_ALLOWLIST: frozenset[str] = frozenset(
    {
        "switch.bar_kaffeemaschine_steckdose",  # coffee machine on/off
        "vacuum.goldstaubsucher",  # robot vacuum docked/cleaning
        "vacuum.matrix10_ultra",
        "lock.lock_ultra_8d3c",  # door lock locked/unlocked
        # NOTE: input_button.* and other timestamp-state entities are deliberately
        # excluded — their state is the last-press time, so every press is a unique
        # transition that never forms a repeat (no gag) while bloating the ledger.
        "switch.bad_gross_waschmaschine_steckdose",  # washing machine on/off
        "fan.bad_gross_lufter_shelly",  # bathroom / kitchen fans on/off
        "fan.bad_klein_lufter",
        "fan.kuche_lufter",
        "binary_sensor.buro_9_ring_intercom_klingelt",  # doorbell
        "binary_sensor.8_stockwerk_group_sensor_wohnzimmer_esszimmer_bar",  # living-room presence
    }
)

_TIER_WEIGHTS = {3.0: GOLD_ENTITIES, 2.0: SILVER_ENTITIES, 1.0: BRONZE_ENTITIES}
_RECENCY_HALFLIFE_SECONDS = 7200.0  # salience halves ~every 2 hours
_RECENCY_LAMBDA = math.log(2) / _RECENCY_HALFLIFE_SECONDS

MIN_COUNT_FOR_GAG = 2  # a "running" gag needs at least one repeat
GAG_COOLDOWN_SECONDS = 900.0  # 15 min before the same gag is eligible again
GAG_INJECT_PROBABILITY = 0.55  # silence chance — gags are discovered, not announced

EVENING_GAP_SECONDS = 3.5 * 3600  # this int without activity ends the evening
_DAY_ROLLOVER_HOUR = 4  # an "evening" belongs to the day it started; 4am rolls over
# -----------------------------------------------------------------------------


def _tier_weight(entity_id: str) -> float:
    for weight, members in _TIER_WEIGHTS.items():
        if entity_id in members:
            return weight
    return 1.0


def _is_numeric(value: str) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _is_gag_candidate(event: HomeEvent) -> bool:
    """Only allowlisted, discrete (non-numeric), non-person events become gags."""
    if event.entity_id.startswith("person."):
        return False
    if event.entity_id not in _GAG_ENTITY_ALLOWLIST:
        return False
    # Defense in depth: even an allowlisted entity must not emit numeric drift.
    return not (_is_numeric(event.raw_new_state) or _is_numeric(event.raw_old_state))


def _evening_day(ts: float) -> datetime.date:
    """The calendar day an evening belongs to (anything before 4am → prior day)."""
    return (datetime.datetime.fromtimestamp(ts) - datetime.timedelta(hours=_DAY_ROLLOVER_HOUR)).date()


@dataclass
class GagBucket:
    """An aggregated (entity, transition) tally — the unit of a running gag."""

    entity_id: str
    label: str
    old_state: str
    new_state: str
    count: int = 0
    first_ts: float = 0.0
    last_ts: float = 0.0
    last_spoken_ts: float = 0.0

    def salience(self, *, now: float) -> float:
        """tier_weight x log(count+1) x recency_decay(now - last_ts)."""
        age = max(0.0, now - self.last_ts)
        recency = math.exp(-_RECENCY_LAMBDA * age)
        return _tier_weight(self.entity_id) * math.log(self.count + 1) * recency

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "label": self.label,
            "old_state": self.old_state,
            "new_state": self.new_state,
            "count": self.count,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "last_spoken_ts": self.last_spoken_ts,
        }

    @classmethod
    def from_dict(cls, key: str, data: dict) -> GagBucket:
        return cls(
            entity_id=str(data.get("entity_id", key.split("|", 1)[0])),
            label=str(data.get("label", "")),
            old_state=str(data.get("old_state", "")),
            new_state=str(data.get("new_state", "")),
            count=int(data.get("count", 0) or 0),
            first_ts=float(data.get("first_ts", 0.0) or 0.0),
            last_ts=float(data.get("last_ts", 0.0) or 0.0),
            last_spoken_ts=float(data.get("last_spoken_ts", 0.0) or 0.0),
        )


def _bucket_key(event: HomeEvent) -> str:
    return f"{event.entity_id}|{event.raw_old_state}->{event.raw_new_state}"


def _render_gag(bucket: GagBucket) -> str:
    """Render a bucket as approximate Italian gag DATA for inside the fence.

    Approximate ("di nuovo"/"non si ferma"), NOT an exact count: BANTER/AD-cadence
    HA sampling can miss short toggles, so an exact "10th time" is not dependable
    (codex review). PROVISIONAL phrasing — Phase 1 finalizes and golden-snapshots.
    """
    cadence = "praticamente non si ferma stasera" if bucket.count >= 4 else "di nuovo stasera"
    # No "old -> new" arrow: _sanitize_prompt_data strips '>' before the gag
    # reaches the prompt, which would mangle it. State the new state in prose.
    return f"{bucket.label}: {bucket.new_state}, {cadence}."


@dataclass
class EveningLedger:
    """Persisted, evening-scoped tally of discrete home events for running gags.

    Owns its OWN session identity (PersonaStore's session id is in-memory and
    does not survive the addon's frequent restarts — codex review). Cooldown
    state lives in each bucket's `last_spoken_ts` so a restart cannot re-fire a
    gag that just aired.
    """

    session_id: int = 0
    started_at: float = 0.0
    last_active: float = 0.0
    watermark: float = 0.0
    buckets: dict[str, GagBucket] = field(default_factory=dict)
    _dirty: bool = field(default=False, repr=False)

    # --- session lifecycle ---------------------------------------------------

    def _maybe_roll_session(self, now: float) -> None:
        """Start, continue, or reset the evening based on inactivity / day rollover."""
        if self.session_id == 0:
            self.session_id = 1
            self.started_at = now
            self.last_active = now
            self._dirty = True
            return
        gap_too_long = (now - self.last_active) > EVENING_GAP_SECONDS
        day_rolled = _evening_day(now) != _evening_day(self.started_at)
        if gap_too_long or day_rolled:
            self.session_id += 1
            self.started_at = now
            self.buckets.clear()
            self._dirty = True

    # --- ingest --------------------------------------------------------------

    def observe(self, events: Iterable[HomeEvent], *, now: float) -> bool:
        """Fold NEW events (newer than the watermark) into the tally buckets.

        Returns True if the ledger changed (and should be persisted).
        """
        self._maybe_roll_session(now)
        max_ts = self.watermark
        changed = False
        for event in events:
            if event.timestamp <= self.watermark:
                continue  # already consumed on a prior poll (dedupe)
            max_ts = max(max_ts, event.timestamp)
            if not _is_gag_candidate(event):
                continue
            key = _bucket_key(event)
            bucket = self.buckets.get(key)
            if bucket is None:
                bucket = GagBucket(
                    entity_id=event.entity_id,
                    label=event.label,
                    old_state=event.old_state,
                    new_state=event.new_state,
                    first_ts=event.timestamp,
                )
                self.buckets[key] = bucket
            bucket.count += 1
            bucket.last_ts = event.timestamp
            changed = True
        if max_ts > self.watermark:
            self.watermark = max_ts
            changed = True
        self.last_active = now
        # Persist on every observe: last_active always advances, and it must
        # survive a restart so the inactivity-gap math is correct (otherwise a
        # quiet-but-active stretch would look like an evening-ending gap).
        self._dirty = True
        return changed

    # --- read / render -------------------------------------------------------

    def offer_gag(self, *, now: float, rng: random.Random | None = None) -> tuple[str, str] | None:
        """Pick one eligible gag without spending its cooldown.

        Returns (bucket_key, rendered_gag), or None when nothing fires. The
        producer calls `mark_spoken()` only after generated banter successfully
        queues, so failed/canned fallback paths do not burn a callback.
        """
        roll = rng or random
        eligible = [
            (key, bucket)
            for key, bucket in self.buckets.items()
            if bucket.count >= MIN_COUNT_FOR_GAG and (now - bucket.last_spoken_ts) >= GAG_COOLDOWN_SECONDS
        ]
        if not eligible:
            return None
        weights = [bucket.salience(now=now) for _, bucket in eligible]
        if sum(weights) <= 0:
            return None
        # Weighted-random pick (not strict top) so a hot gag can't starve the rest.
        chosen_key, chosen_bucket = roll.choices(eligible, weights=weights, k=1)[0]
        # Silence chance: a callback is "discovered, not announced".
        if roll.random() >= GAG_INJECT_PROBABILITY:
            return None  # stayed silent; no cooldown spent
        return chosen_key, _render_gag(chosen_bucket)

    def mark_spoken(self, bucket_key: str, *, now: float) -> None:
        """Spend a bucket's cooldown after the rendered gag is queued."""
        bucket = self.buckets.get(bucket_key)
        if bucket is None:
            return
        bucket.last_spoken_ts = now
        self._dirty = True

    def select_and_render(self, *, now: float, rng: random.Random | None = None) -> str:
        """Pick one eligible gag and immediately spend its cooldown."""
        offer = self.offer_gag(now=now, rng=rng)
        if offer is None:
            return ""
        bucket_key, rendered = offer
        self.mark_spoken(bucket_key, now=now)
        return rendered

    # --- persistence ---------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "version": _SCHEMA_VERSION,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "last_active": self.last_active,
            "watermark": self.watermark,
            "buckets": {key: bucket.to_dict() for key, bucket in self.buckets.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> EveningLedger:
        buckets_raw = data.get("buckets", {})
        if not isinstance(buckets_raw, dict):
            raise ValueError("buckets must be an object")
        buckets = {key: GagBucket.from_dict(key, val) for key, val in buckets_raw.items()}
        return cls(
            session_id=int(data.get("session_id", 0) or 0),
            started_at=float(data.get("started_at", 0.0) or 0.0),
            last_active=float(data.get("last_active", 0.0) or 0.0),
            watermark=float(data.get("watermark", 0.0) or 0.0),
            buckets=buckets,
        )

    @classmethod
    def load(cls, cache_dir: Path) -> EveningLedger:
        """Reload the ledger from disk; start fresh on missing/corrupt files.

        A corrupt ledger must never crash boot (that would cause dead air —
        leadership principle #1). Missing → silent fresh ledger; corrupt → warn
        and fresh ledger.
        """
        path = cache_dir / LEDGER_FILENAME
        try:
            payload = json.loads(path.read_text())
        except FileNotFoundError:
            return cls()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Evening ledger is unreadable, starting fresh: %s", path)
            return cls()
        if not isinstance(payload, dict):
            logger.warning("Evening ledger has unexpected shape, starting fresh: %s", path)
            return cls()
        try:
            return cls.from_dict(payload)
        except (AttributeError, TypeError, ValueError):
            logger.warning("Evening ledger has invalid fields, starting fresh: %s", path)
            return cls()

    def save_if_dirty(self, cache_dir: Path) -> None:
        """Persist atomically (temp + rename) only when state changed."""
        if not self._dirty:
            return
        path = cache_dir / LEDGER_FILENAME
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))
            tmp.replace(path)
            self._dirty = False
        except OSError as exc:  # disk full / permissions — never crash the producer
            logger.warning("Could not persist evening ledger to %s: %s", path, exc)
