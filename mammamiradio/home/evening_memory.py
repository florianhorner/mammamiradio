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

Candidacy is operator-portable: gag-worthiness is decided by device DOMAIN
(`switch`/`fan`/`lock`/`vacuum`/`binary_sensor` by default), so the feature works
on any home, and operators tune it via `[home.running_gags]` in radio.toml
(domain_allowlist / entity_allowlist / entity_denylist). The salience weights,
cooldown, inject probability, and render phrasing remain provisional and may be
tuned further from real listening.
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

from mammamiradio.home.gag_select import weighted_offer
from mammamiradio.home.ha_context import BRONZE_ENTITIES, GOLD_ENTITIES, SILVER_ENTITIES
from mammamiradio.home.ha_enrichment import HomeEvent

logger = logging.getLogger(__name__)

LEDGER_FILENAME = "evening_ledger.json"
_SCHEMA_VERSION = 1

# --- gag candidacy (operator-portable) ---------------------------------------
# Gag-candidacy is decided by DEVICE DOMAIN, not hardcoded entity_ids, so the
# feature fires on any operator's home out of the box (a coffee machine is a
# `switch.*` everywhere, even though the entity_id differs per home). The default
# set is the discrete, recurring domains whose on/off / locked / docked toggles
# read as "Nth time tonight" gags. Deliberately excludes `sensor` (numeric drift),
# `climate`, `media_player`, `weather`, `light` (flaps constantly → gag noise),
# and `person` (privacy + tracker noise). Operators override via
# `[home.running_gags]` in radio.toml (domain_allowlist / entity_allowlist /
# entity_denylist) — see core/config.EveningGagsSection.
_DEFAULT_GAG_DOMAINS: frozenset[str] = frozenset({"switch", "fan", "lock", "vacuum", "binary_sensor"})
# NOTE: input_button.* and other timestamp-state entities never form a repeat
# (their state is the last-press time, a unique transition each time) — they are
# kept out of the default domain set and bloat-guarded by the numeric exclusion.

_TIER_WEIGHTS = {3.0: GOLD_ENTITIES, 2.0: SILVER_ENTITIES, 1.0: BRONZE_ENTITIES}
_RECENCY_HALFLIFE_SECONDS = 7200.0  # salience halves ~every 2 hours
_RECENCY_LAMBDA = math.log(2) / _RECENCY_HALFLIFE_SECONDS

MIN_COUNT_FOR_GAG = 2  # a "running" gag needs at least one repeat
GAG_COOLDOWN_SECONDS = 900.0  # 15 min before the same gag is eligible again
GAG_INJECT_PROBABILITY = 0.55  # silence chance — gags are discovered, not announced

EVENING_GAP_SECONDS = 3.5 * 3600  # this long without activity ends the evening
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


# HA emits these when a device drops off or reconnects (e.g. on an addon/HA
# restart). A `unavailable -> on` transition is infrastructure noise, not a human
# action: it must neither form a running gag ("the doorbell, di nuovo stasera"
# every time HA reboots) nor count as evening activity that keeps a quiet session
# from rolling over.
_HA_SENTINEL_STATES: frozenset[str] = frozenset({"unavailable", "unknown", "none"})


def _is_sentinel_transition(event: HomeEvent) -> bool:
    return event.raw_old_state.lower() in _HA_SENTINEL_STATES or event.raw_new_state.lower() in _HA_SENTINEL_STATES


def _domain(entity_id: str) -> str:
    """The HA domain of an entity_id — the part before the first dot."""
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


# Domains whose non-numeric state changes happen on their own — weather conditions
# flipping (cloudy -> sunny), the sun crossing the horizon. They are NOT a human
# doing something in the home, so they must not advance the evening's activity
# clock; otherwise a genuinely empty home with passive weather/sun changes would
# never hit the advertised inactivity reset and would carry stale gags forward.
_PASSIVE_ACTIVITY_DOMAINS: frozenset[str] = frozenset({"weather", "sun"})


def _is_home_activity(event: HomeEvent) -> bool:
    """A real, discrete home action — what should keep an evening session alive.

    Broader than gag candidacy: any non-numeric, non-person state change means a
    human did something in the home. Numeric drift (power meters tick every poll),
    person trackers, device-availability flaps (HA restart artefacts), and passive
    environmental domains (weather/sun, which change on their own) are excluded so
    nothing but real activity can fake an evening into staying alive.
    """
    if event.entity_id.startswith("person."):
        return False
    if _domain(event.entity_id) in _PASSIVE_ACTIVITY_DOMAINS:
        return False
    if _is_sentinel_transition(event):
        return False
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
    cooldown_seconds: float = GAG_COOLDOWN_SECONDS
    # Ritual-recipe provenance (empty for plain home events): lets a Moment
    # Receipt built at offer time name its ritual family — the recipe match is
    # long gone by the time offer_gag() picks this bucket.
    ritual_family: str = ""

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
            "cooldown_seconds": self.cooldown_seconds,
            "ritual_family": self.ritual_family,
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
            cooldown_seconds=float(data.get("cooldown_seconds", GAG_COOLDOWN_SECONDS) or GAG_COOLDOWN_SECONDS),
            ritual_family=str(data.get("ritual_family", "") or ""),
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
    # --- candidacy policy (from config, NOT persisted to disk) ---------------
    # domain_allowlist gates which device domains form gags; entity_allowlist (if
    # non-empty) restricts to specific entity_ids; entity_denylist always wins.
    domain_allowlist: frozenset[str] = field(default_factory=lambda: _DEFAULT_GAG_DOMAINS)
    entity_allowlist: frozenset[str] = field(default_factory=frozenset)
    entity_denylist: frozenset[str] = field(default_factory=frozenset)
    _dirty: bool = field(default=False, repr=False)

    # --- candidacy -----------------------------------------------------------

    def _is_gag_candidate(self, event: HomeEvent) -> bool:
        """Discrete (non-numeric), non-person, policy-allowed events become gags."""
        if event.entity_id.startswith("person."):
            return False
        if event.entity_id in self.entity_denylist:
            return False
        # Availability flaps remain infrastructure noise even for explicit
        # radio-event rules.
        if _is_sentinel_transition(event):
            return False
        if getattr(event, "force_gag_candidate", False):
            return True
        # Defense in depth: a candidate must never emit numeric drift...
        if _is_numeric(event.raw_new_state) or _is_numeric(event.raw_old_state):
            return False
        if self.entity_allowlist:
            return event.entity_id in self.entity_allowlist
        return _domain(event.entity_id) in self.domain_allowlist

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
            # Reset the activity clock to the roll point. last_active now advances
            # only on real home activity (not every poll), so without this a rolled
            # session whose home stays quiet would re-roll on every subsequent poll.
            self.last_active = now
            self.buckets.clear()
            self._dirty = True

    # --- ingest --------------------------------------------------------------

    def observe(self, events: Iterable[HomeEvent], *, now: float) -> bool:
        """Fold NEW events (newer than the watermark) into the tally buckets.

        Returns True if the ledger changed (and should be persisted).
        """
        prev_session = self.session_id
        self._maybe_roll_session(now)
        # A session start (0→1) or roll mutates persisted state, so it counts as
        # a change even on an otherwise-quiet poll (honest return for the caller).
        changed = self.session_id != prev_session
        max_ts = self.watermark
        activity = False
        for event in events:
            if event.timestamp <= self.watermark:
                continue  # already consumed on a prior poll (dedupe)
            max_ts = max(max_ts, event.timestamp)
            if _is_home_activity(event):
                activity = True
            if not self._is_gag_candidate(event):
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
                    cooldown_seconds=(
                        event.gag_cooldown_seconds
                        if getattr(event, "gag_cooldown_seconds", 0.0) > 0
                        else GAG_COOLDOWN_SECONDS
                    ),
                    ritual_family=getattr(event, "ritual_family", "") or "",
                )
                self.buckets[key] = bucket
            elif getattr(event, "gag_cooldown_seconds", 0.0) > 0:
                bucket.cooldown_seconds = event.gag_cooldown_seconds
            if getattr(event, "ritual_family", "") and not bucket.ritual_family:
                # A ritual event landing in a bucket first created by a plain
                # home event upgrades it with provenance (never downgrades).
                # The label upgrades WITH it: a plain bucket's label is a real
                # device label, and a ritual-sourced receipt shows its label on
                # the unauthenticated listener strip — only the generic
                # public_family_label (this event's label) may travel there.
                bucket.ritual_family = event.ritual_family
                bucket.label = event.label
            bucket.count += 1
            bucket.last_ts = event.timestamp
            changed = True
        if max_ts > self.watermark:
            self.watermark = max_ts
            changed = True
        # last_active advances ONLY on real home activity (not every poll), so a
        # quiet evening genuinely rolls over after EVENING_GAP_SECONDS instead of
        # radio-cadence polling keeping the session alive forever.
        if activity:
            self.last_active = now
            changed = True
        if changed:
            self._dirty = True
        return changed

    # --- read / render -------------------------------------------------------

    def offer_gag(self, *, now: float, rng: random.Random | None = None) -> tuple[str, str] | None:
        """Pick one eligible gag without spending its cooldown.

        Returns (bucket_key, rendered_gag), or None when nothing fires. The
        producer calls `mark_spoken()` only after generated banter successfully
        queues, so failed/canned fallback paths do not burn a callback.
        """
        eligible = [
            (key, bucket)
            for key, bucket in self.buckets.items()
            if bucket.count >= MIN_COUNT_FOR_GAG
            and (now - bucket.last_spoken_ts) >= bucket.cooldown_seconds
            and bucket.entity_id not in self.entity_denylist
        ]
        # Weighted-random pick (not strict top, so a hot gag can't starve the rest)
        # + silence chance ("discovered, not announced"). Shared with the verbal
        # running-gag ledger via gag_select.weighted_offer.
        chosen = weighted_offer(
            eligible,
            now=now,
            inject_probability=GAG_INJECT_PROBABILITY,
            weight=lambda bucket, n: bucket.salience(now=n),
            rng=rng,
        )
        if chosen is None:
            return None
        chosen_key, chosen_bucket = chosen
        return chosen_key, _render_gag(chosen_bucket)

    def mark_spoken(self, bucket_key: str, *, now: float) -> None:
        """Spend a bucket's cooldown after the rendered gag is queued."""
        bucket = self.buckets.get(bucket_key)
        if bucket is None:
            return
        bucket.last_spoken_ts = now
        self._dirty = True

    def purge_entity(self, entity_id: str) -> bool:
        """Drop any bucket already tallied for ``entity_id``.

        `entity_denylist` only stops NEW events from becoming buckets — it does
        nothing about a bucket built before an operator mutes the entity, and
        `offer_gag()` does not re-check the denylist at read time. Called when
        an operator mutes an entity so an already-observed moment about it
        cannot still be offered as a running gag after the mute.
        """
        to_drop = [key for key, bucket in self.buckets.items() if bucket.entity_id == entity_id]
        for key in to_drop:
            del self.buckets[key]
        if to_drop:
            self._dirty = True
        return bool(to_drop)

    def purge_denied_entities(self) -> bool:
        """Drop persisted buckets that current config/policy now denies."""
        if not self.entity_denylist:
            return False
        to_drop = [key for key, bucket in self.buckets.items() if bucket.entity_id in self.entity_denylist]
        for key in to_drop:
            del self.buckets[key]
        if to_drop:
            self._dirty = True
        return bool(to_drop)

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
    def _load_raw(cls, cache_dir: Path) -> EveningLedger:
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

    @classmethod
    def load(
        cls,
        cache_dir: Path,
        *,
        domain_allowlist: Iterable[str] | None = None,
        entity_allowlist: Iterable[str] | None = None,
        entity_denylist: Iterable[str] | None = None,
    ) -> EveningLedger:
        """Reload the ledger from disk; start fresh on missing/corrupt files.

        A corrupt ledger must never crash boot (that would cause dead air —
        leadership principle #1). Missing → silent fresh ledger; corrupt → warn
        and fresh ledger.

        Candidacy policy comes from config, NOT from disk: pass the operator's
        `[home.running_gags]` overrides here. A `None` argument keeps the built-in
        default (domain-based candidacy). The persisted tally is independent of the
        policy, so changing the allowlist between runs takes effect immediately.
        """
        led = cls._load_raw(cache_dir)
        if domain_allowlist is not None:
            led.domain_allowlist = frozenset(domain_allowlist)
        if entity_allowlist is not None:
            led.entity_allowlist = frozenset(entity_allowlist)
        if entity_denylist is not None:
            led.entity_denylist = frozenset(entity_denylist)
            led.purge_denied_entities()
        return led

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
