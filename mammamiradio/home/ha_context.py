"""Home Assistant context provider for radio scripts.

Polls HA REST API for entity states and formats them as natural language
that scriptwriter can inject into Claude prompts. The hosts reference
ambient home state ~30-50% of the time, like glancing out a window.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, TypedDict
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect as websocket_connect

from mammamiradio.core.config import DEFAULT_STATION_NAME, RadioEventRule, TimerInterruptConfig, is_absolute_http_url
from mammamiradio.core.models import InterruptSpec, ScoredEntityStatus
from mammamiradio.home.authorization import HomeAuthorization, HomeAuthorizationMode
from mammamiradio.home.catalog import ENTITY_LABELS, ENTITY_LABELS_EN, LabelResolution, resolve_label
from mammamiradio.home.entity_policy import muted_entity_ids
from mammamiradio.home.ha_enrichment import (
    EVENT_BUFFER_SIZE,
    HomeEvent,
    build_events_summary,
    build_events_summary_en,
    diff_states,
    prune_events,
)
from mammamiradio.home.radio_events import RadioEventMatch, build_radio_event_baseline, match_radio_events
from mammamiradio.home.ritual_recipes import (
    RitualRecipeMatch,
    audit_ritual_recipes,
    build_ritual_recipe_baseline,
    match_ritual_recipes,
    public_family_labels,
)
from mammamiradio.hosts.station_name_guard import strip_foreign_station_name

logger = logging.getLogger(__name__)

# Entities curated for maximum radio entertainment value
GOLD_ENTITIES = [
    # Coffee machine + dad jokes
    "switch.bar_kaffeemaschine_steckdose",
    "input_select.kaffee_dad_jokes",
    # Robot vacuums
    "vacuum.goldstaubsucher",
    "vacuum.matrix10_ultra",
    # Weather
    "weather.forecast_home",
    # Who's home
    "person.florian_horner",
    "person.sabrina",
    "person.schnuffi",
    # Door lock
    "lock.lock_ultra_8d3c",
    # Elevator fingerbot
    "input_button.foyer_fahrstuhl_fingerbot_push_button",
]

SILVER_ENTITIES = [
    # Room presence (select interesting rooms)
    "binary_sensor.8_stockwerk_group_sensor_wohnzimmer_esszimmer_bar",
    "input_select.bedroom_occupancy_state",
    # Washing machine
    "switch.bad_gross_waschmaschine_steckdose",
    # TV
    "media_player.samsung_s95ca_65",
    # Sonos speakers
    "media_player.wohnzimmer_sonos_arc_lautsprecher",
    "media_player.esszimmer",
    # Heating
    "climate.wohnzimmer_tado_heizung",
    "climate.schlafzimmer",
    # Sun
    "sun.sun",
    # Bathroom fans (someone showering?)
    "fan.bad_gross_lufter_shelly",
    "fan.bad_klein_lufter",
    # Kitchen fan (someone cooking?)
    "fan.kuche_lufter",
    # Room-level light groups (Magic Areas aggregates)
    "light.magic_areas_light_groups_wohnzimmer_all_lights",
    "light.magic_areas_light_groups_schlafzimmer_all_lights",
    "light.magic_areas_light_groups_kuche_all_lights",
    "light.magic_areas_light_groups_esszimmer_all_lights",
    # Power sensors for activity detection
    "sensor.bar_bali_boot_steckdose_power",
    "sensor.kuche_kaffeemaschine_steckdose_power",
    # Atmosphere
    "light.schlafzimmer_sternenlicht_projektor_2",
    "light.kleiderschrank_sternenlicht_projektor",
    "light.terrasse_9_outdoor_lichtschlauch",
    # Total household power
    "sensor.haushalt_stromverbrauch_gesamt",
]

BRONZE_ENTITIES = [
    # Sleep/wake times
    "input_datetime.last_sleep_time",
    "input_datetime.last_wake_time",
    # Apartment door
    "binary_sensor.buro_9_ring_intercom_klingelt",
]

ALL_ENTITIES = GOLD_ENTITIES + SILVER_ENTITIES + BRONZE_ENTITIES

DEFAULT_CONTEXT_ENTITY_LIMIT = 12
DEFAULT_CONTEXT_CHAR_LIMIT = 2000
MAX_PRESENCE_IN_SLICE = 4
DROP_DOMAINS = {
    "update",
    "button",
    "scene",
    "automation",
    "script",
    "zone",
    # Free-text helpers (input_text) and the first-class text entity domain can
    # carry plaintext secrets (e.g., input_text.guest_wifi_password) that the
    # uppercase-token regex won't catch. Drop them at the privacy layer.
    "input_text",
    "text",
}
DROP_ENTITY_CATEGORIES = {"diagnostic", "config"}
DROP_DEVICE_CLASSES = {"signal_strength", "battery", "timestamp"}
# person is intentionally NOT denied: home/away presence drives arrival greetings
# and the empty-home mood. GPS/location and identity attributes are stripped via
# SENSITIVE_ATTRIBUTE_KEYS, and person events are filtered from /public-status.
PRIVACY_DENY_DOMAINS = {"device_tracker", "camera", "alarm_control_panel"}
SENSITIVE_ATTRIBUTE_KEYS = {
    "latitude",
    "longitude",
    "gps_accuracy",
    "source_type",
    "ip_address",
    "mac_address",
    "unique_id",
    "device_id",
    "user_id",
    "device_trackers",
    "token",
    "access_token",
    "refresh_token",
}
DOMAIN_SALIENCE_WEIGHTS = {
    "media_player": 1.0,
    "vacuum": 0.9,
    "lock": 0.8,
    "weather": 0.8,
    "climate": 0.7,
    "light": 0.6,
    "fan": 0.6,
    "switch": 0.5,
    "sensor": 0.2,
    "binary_sensor": 0.2,
}
POWER_SENSOR_WEIGHT = 0.5
PRESENCE_SENSOR_WEIGHT = 0.9
PRESENCE_SENSOR_DEVICE_CLASSES = {"occupancy", "presence", "motion"}
OVERRIDE_SCORE_BOOST = 0.5
AREA_SCORE_BOOST = 0.2
RECENT_CHANGE_WINDOW_SECONDS = 15 * 60
RECENT_CHANGE_SCORE_BOOST = 0.4
EVENT_SCORE_BOOST = 0.3

_UUID_RE = re.compile(r"^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$")
_HEX_TOKEN_RE = re.compile(r"^[a-fA-F0-9]{32,}$")
_GENERIC_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_MAC_RE = re.compile(r"\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_LAT_LON_RE = re.compile(r"[-+]?\d{1,2}\.\d{4,}\s*,\s*[-+]?\d{1,3}\.\d{4,}")
_TOKEN_EXCLUSIONS = {"CONNECTED", "PENDING_UPDATE", "RESTORED", "UNAVAILABLE", "UNKNOWN"}

# Map raw HA states to natural Italian descriptions
STATE_TRANSLATIONS = {
    "home": "a casa",
    "not_home": "fuori casa",
    "on": "acceso/a",
    "off": "spento/a",
    "locked": "chiusa",
    "unlocked": "aperta",
    "docked": "alla base",
    "cleaning": "sta pulendo",
    "paused": "in pausa",
    "returning": "sta tornando alla base",
    "idle": "inattivo",
    "playing": "sta suonando",
    "above_horizon": "sopra l'orizzonte",
    "below_horizon": "sotto l'orizzonte (è notte)",
    "auto": "automatico",
    "heat": "riscaldamento attivo",
    "vacant": "vuota",
    "occupied": "occupata",
    "cloudy": "nuvoloso",
    "sunny": "soleggiato",
    "rainy": "pioggia",
    "partlycloudy": "parzialmente nuvoloso",
    "clear-night": "notte serena",
    "fog": "nebbia",
    "snowy": "neve",
    "windy": "ventoso",
    "lightning": "temporale",
}


# English state translations for admin UI display
STATE_TRANSLATIONS_EN: dict[str, str] = {
    "home": "home",
    "not_home": "away",
    "on": "on",
    "off": "off",
    "locked": "locked",
    "unlocked": "unlocked",
    "docked": "docked",
    "cleaning": "cleaning",
    "paused": "paused",
    "returning": "returning to base",
    "idle": "idle",
    "playing": "playing",
    "above_horizon": "above horizon",
    "below_horizon": "below horizon (nighttime)",
    "auto": "auto",
    "heat": "heating active",
    "vacant": "vacant",
    "occupied": "occupied",
    "cloudy": "cloudy",
    "sunny": "sunny",
    "rainy": "rainy",
    "partlycloudy": "partly cloudy",
    "clear-night": "clear night",
    "fog": "fog",
    "snowy": "snowy",
    "windy": "windy",
    "lightning": "thunderstorm",
}


# ---------------------------------------------------------------------------
# Phase 4: Reactive triggers
# ---------------------------------------------------------------------------

# (entity_id, raw_ha_trigger_state, directive_text, cooldown_seconds)
REACTIVE_TRIGGERS: list[tuple[str, str, str, int]] = [
    (
        "switch.bar_kaffeemaschine_steckdose",
        "on",
        "La macchina del caffè si è appena accesa! Profumo di espresso, notatelo brevemente —"
        " naturale, non esagerato. Se l'ora mostrata sopra calza, legate l'evento al momento"
        " ('puntuale'), solo se naturale. Mai dire con che frequenza o da quanto"
        " succede.",
        1800,
    ),
    (
        "lock.lock_ultra_8d3c",
        "unlocked",
        "La porta d'ingresso si è appena aperta. Dite 'bentornato' come se aveste"
        " sentito la porta — breve, caldo, non forzato.",
        300,
    ),
    (
        "vacuum.goldstaubsucher",
        "cleaning",
        "Il Goldstaubsucher ha iniziato ad aspirare. Lamentatevi del rumore di fondo, scherzate sul robot invadente.",
        3600,
    ),
    (
        "vacuum.matrix10_ultra",
        "cleaning",
        "Anche il Matrix10 Ultra sta aspirando! CAOS TOTALE — due robot in azione. Reazione esagerata ma divertente.",
        3600,
    ),
    (
        "person.florian_horner",
        "home",
        "Florian è appena tornato a casa. Un caloroso bentornato — come se sapeste esattamente chi è tornato.",
        3600,
    ),
    (
        "person.sabrina",
        "home",
        "Sabrina è appena tornata a casa. Un caloroso bentornata Sabrina — naturale e familiare.",
        3600,
    ),
    (
        "light.terrasse_9_outdoor_lichtschlauch",
        "on",
        "Le luci della terrazza si sono accese! Serata all'aperto — commentate il bel tempo"
        " o la voglia di aria fresca. Breve, naturale.",
        3600,
    ),
]

_reactive_cooldowns: dict[str, float] = {}


class ThresholdTrigger(TypedDict):
    """Reactive trigger based on a numeric sensor crossing a threshold."""

    entity_id: str
    threshold: float
    direction: str  # "above" or "below"
    directive: str
    cooldown: int


THRESHOLD_TRIGGERS: list[ThresholdTrigger] = [
    {
        "entity_id": "sensor.kuche_kaffeemaschine_steckdose_power",
        "threshold": 50.0,
        "direction": "above",
        "directive": (
            "La caffettiera si è appena accesa! Caffè in preparazione — "
            "commentate il momento in modo naturale. Breve e caldo."
        ),
        "cooldown": 3600,
    },
]


@dataclass
class ScoredEntity:
    """Budgeted HA entity selected for prompt context and Engine Room visibility."""

    entity_id: str
    area: str | None
    domain: str
    score: float
    raw_state: dict
    label_it: str
    label_en: str
    label_tier: str = "fallback"
    summary_line: str = ""

    def to_status_dict(self) -> ScoredEntityStatus:
        attrs = self.raw_state.get("attributes", {})
        return {
            "entity_id": self.entity_id,
            "area": self.area,
            "domain": self.domain,
            "score": round(self.score, 3),
            "state": self.raw_state.get("state"),
            "label": self.label_en,
            "label_tier": self.label_tier,
            "summary": self.summary_line,
            "device_class": attrs.get("device_class"),
        }


@dataclass
class HomeRegistrySnapshot:
    """Cached HA registry metadata used to enrich REST state snapshots."""

    entity_areas: dict[str, str] = field(default_factory=dict)
    entity_names: dict[str, str] = field(default_factory=dict)
    entity_device_names: dict[str, str] = field(default_factory=dict)
    fetched_at: float = 0.0
    source: str = "empty_fallback"


@dataclass
class HomeContext:
    """Snapshot of interesting home state, formatted for scriptwriter."""

    raw_states: dict[str, dict] = field(default_factory=dict)
    summary: str = ""
    events: deque[HomeEvent] = field(default_factory=lambda: deque(maxlen=EVENT_BUFFER_SIZE))
    radio_events: list[RadioEventMatch] = field(default_factory=list)
    ritual_recipe_matches: list[RitualRecipeMatch] = field(default_factory=list)
    ritual_public_families: list[str] = field(default_factory=list)
    ritual_recipe_audit: list[dict[str, object]] = field(default_factory=list)
    events_summary: str = ""
    timestamp: float = 0.0
    # Phase 2: mood classification
    mood: str = ""
    # Phase 3: weather narrative arc
    weather_arc: str = ""
    # English equivalents for admin UI display
    mood_en: str = ""
    weather_arc_en: str = ""
    events_summary_en: str = ""
    last_event_label_en: str = ""
    scored: list[ScoredEntity] = field(default_factory=list)
    catalog_hit_rate: float = 0.0
    label_stats: dict[str, int | float] = field(default_factory=dict)
    registry_source: str = ""
    denylist_hits: dict[str, int] = field(default_factory=dict)
    # R0 install gate. ``ambient_sources`` is private cache bookkeeping used to
    # translate a live hard mute of a real HA source into its synthetic narrow
    # projection; neither field is serialized to public/admin status.
    authorization_mode: str = HomeAuthorizationMode.NARROW.value
    ambient_sources: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp if self.timestamp else float("inf")


@dataclass(frozen=True)
class _HomeContextFetchOutcome:
    """Private fetch result for the producer's single-flight refresh mailbox.

    ``fresh`` is the only result created from a completed HA state fetch.  A
    coordinator must still compare ``snapshot_timestamp`` with the baseline it
    started from before adopting it.  ``cached`` and ``failed`` carry a
    prompt-safe context for compatibility/fallback callers but are never
    adoptable as a new snapshot.

    Candidate event baselines deliberately travel with the fresh result rather
    than updating module state in the background task.  The producer publishes
    them only when it accepts the outcome at a safe segment boundary.
    """

    kind: Literal["fresh", "cached", "failed"]
    context: HomeContext
    snapshot_timestamp: float
    attempt_started_at: float
    attempt_finished_at: float
    duration_seconds: float
    radio_event_state_baseline: dict[str, dict] = field(default_factory=dict)
    ritual_recipe_state_baseline: dict[str, dict] = field(default_factory=dict)

    def is_adoptable_from(self, baseline_timestamp: float) -> bool:
        """Whether this represents a genuinely newer fetched snapshot."""
        return self.kind == "fresh" and self.snapshot_timestamp > baseline_timestamp


def _copy_raw_state(state_data: dict) -> dict:
    copied = dict(state_data)
    attrs = copied.get("attributes")
    if isinstance(attrs, dict):
        copied["attributes"] = dict(attrs)
    return copied


def _copy_scored_entity(entity: ScoredEntity) -> ScoredEntity:
    return replace(entity, raw_state=_copy_raw_state(entity.raw_state))


def _copy_home_context(context: HomeContext) -> HomeContext:
    """Copy mutable context containers before policy/view-specific filtering."""
    return replace(
        context,
        raw_states={entity_id: _copy_raw_state(data) for entity_id, data in context.raw_states.items()},
        events=deque(context.events, maxlen=context.events.maxlen or EVENT_BUFFER_SIZE),
        radio_events=list(context.radio_events),
        ritual_recipe_matches=list(context.ritual_recipe_matches),
        ritual_public_families=list(context.ritual_public_families),
        ritual_recipe_audit=[dict(item) for item in context.ritual_recipe_audit],
        scored=[_copy_scored_entity(entity) for entity in context.scored],
        label_stats=dict(context.label_stats),
        denylist_hits=dict(context.denylist_hits),
        ambient_sources=dict(context.ambient_sources),
    )


def _sanitize_state_value(value: str, max_len: int = 100) -> str:
    """Truncate and strip instruction-like patterns from HA state values."""
    value = str(value)[:max_len]
    # Strip patterns that look like prompt injection attempts
    for pattern in ("ignore previous", "disregard", "system override", "forget your"):
        if pattern in value.lower():
            return "(filtered)"
    if _looks_sensitive_value(value):
        return "(filtered)"
    return value


def _looks_sensitive_value(value: str) -> bool:
    candidate = value.strip()
    if not candidate or candidate.upper() in _TOKEN_EXCLUSIONS:
        return False
    return bool(
        _UUID_RE.fullmatch(candidate)
        or _HEX_TOKEN_RE.fullmatch(candidate)
        or _GENERIC_TOKEN_RE.fullmatch(candidate)
        or _IP_RE.search(candidate)
        or _MAC_RE.search(candidate)
        or _EMAIL_RE.search(candidate)
        or _LAT_LON_RE.search(candidate)
    )


def _entity_domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def _area_from_attrs(attrs: dict) -> str | None:
    raw = attrs.get("area") or attrs.get("area_name") or attrs.get("area_id")
    if raw is None:
        return None
    area = _sanitize_state_value(str(raw), max_len=40).strip()
    return area or None


def _sanitize_attributes(attrs: dict) -> dict:
    sanitized: dict = {}
    for key, value in attrs.items():
        key_s = str(key)
        if key_s.lower() in SENSITIVE_ATTRIBUTE_KEYS:
            continue
        if isinstance(value, str):
            sanitized[key_s] = _sanitize_state_value(value)
        elif isinstance(value, int | float | bool) or value is None:
            sanitized[key_s] = value
        else:
            text = _sanitize_state_value(str(value), max_len=120)
            if text != "(filtered)":
                sanitized[key_s] = text
    return sanitized


def _filter_state(entity_id: str, state_data: dict, denylist_hits: dict[str, int]) -> dict | None:
    domain = _entity_domain(entity_id)
    attrs = state_data.get("attributes", {}) or {}
    state = str(state_data.get("state", "unknown"))
    # Drop the station's own pushed entities (media_player.mammamiradio,
    # sensor.mammamiradio_*, binary_sensor.mammamiradio_on_air). Otherwise the
    # high-salience media_player echoes the current segment back into the prompt,
    # producing recursive/off-topic banter instead of ambient home context.
    object_id = entity_id.split(".", 1)[-1]
    if object_id.startswith("mammamiradio"):
        denylist_hits["self:mammamiradio"] = denylist_hits.get("self:mammamiradio", 0) + 1
        return None
    if domain in DROP_DOMAINS:
        denylist_hits[f"domain:{domain}"] = denylist_hits.get(f"domain:{domain}", 0) + 1
        return None
    if attrs.get("entity_category") in DROP_ENTITY_CATEGORIES:
        key = f"entity_category:{attrs.get('entity_category')}"
        denylist_hits[key] = denylist_hits.get(key, 0) + 1
        return None
    if attrs.get("device_class") in DROP_DEVICE_CLASSES:
        key = f"device_class:{attrs.get('device_class')}"
        denylist_hits[key] = denylist_hits.get(key, 0) + 1
        return None
    if state in ("unavailable", "unknown"):
        denylist_hits[f"state:{state}"] = denylist_hits.get(f"state:{state}", 0) + 1
        return None
    if domain in PRIVACY_DENY_DOMAINS:
        denylist_hits[f"privacy:{domain}"] = denylist_hits.get(f"privacy:{domain}", 0) + 1
        return None
    sanitized = dict(state_data)
    sanitized["state"] = _sanitize_state_value(state, max_len=100)
    sanitized["attributes"] = _sanitize_attributes(attrs)
    return sanitized


def _score_entity(entity_id: str, state_data: dict, *, event_entity_ids: set[str], now: float) -> float:
    domain = _entity_domain(entity_id)
    attrs = state_data.get("attributes", {}) or {}
    device_class = attrs.get("device_class")
    score = DOMAIN_SALIENCE_WEIGHTS.get(domain, 0.1)
    if domain == "sensor" and device_class == "power":
        score = POWER_SENSOR_WEIGHT
    if domain == "binary_sensor" and device_class in PRESENCE_SENSOR_DEVICE_CLASSES:
        score = PRESENCE_SENSOR_WEIGHT
    if entity_id in ENTITY_LABELS:
        score += OVERRIDE_SCORE_BOOST
    if _area_from_attrs(attrs):
        score += AREA_SCORE_BOOST
    changed = _parse_ha_timestamp(state_data.get("last_changed"))
    if changed is not None and now - changed <= RECENT_CHANGE_WINDOW_SECONDS:
        score += RECENT_CHANGE_SCORE_BOOST
    if entity_id in event_entity_ids:
        score += EVENT_SCORE_BOOST
    return score


def _resolve_label(entity_id: str, state_data: dict, *, cache_dir: Path | None = None) -> LabelResolution | None:
    return resolve_label(entity_id, state_data, cache_dir=cache_dir)


def _format_state(
    entity_id: str,
    state_data: dict,
    *,
    cache_dir: Path | None = None,
    resolved: LabelResolution | None = None,
) -> str | None:
    """Format a single entity state as a natural language line.

    ``resolved`` lets the caller pass an already-computed label so the resolver
    (and its catalog disk read) runs once per entity per poll instead of twice.
    """
    state = _sanitize_state_value(state_data.get("state", "unknown"))
    attrs = state_data.get("attributes", {})
    resolved = resolved or _resolve_label(entity_id, state_data, cache_dir=cache_dir)
    if resolved is None:
        # Anti-illusion guard: raw entity IDs never reach the host. If no curated,
        # catalog, registry, or friendly label is available, drop the entity.
        return None
    label = resolved.label_it

    if state in ("unavailable", "unknown"):
        return None

    # Weather gets special treatment — include temperature and condition
    if entity_id.startswith("weather."):
        condition = STATE_TRANSLATIONS.get(state, state)
        temp = attrs.get("temperature")
        if temp not in (None, "") and not isinstance(temp, bool):
            unit = attrs.get("temperature_unit", "°C")
            return f"{label}: {condition}, {_sanitize_state_value(temp)}{unit}"
        return f"{label}: {condition}"

    # Climate — include current and target temperature
    if entity_id.startswith("climate."):
        current = attrs.get("current_temperature", "?")
        target = attrs.get("temperature", "?")
        mode = STATE_TRANSLATIONS.get(state, state)
        return f"{label}: {mode}, {current}°C (target: {target}°C)"

    # Media players — include what's playing
    if entity_id.startswith("media_player."):
        translated = STATE_TRANSLATIONS.get(state, state)
        title = _sanitize_state_value(attrs.get("media_title", ""))
        if title and state == "playing":
            artist = _sanitize_state_value(attrs.get("media_artist", ""))
            extra = f" — {artist}: {title}" if artist else f" — {title}"
            return f"{label}: {translated}{extra}"
        return f"{label}: {translated}"

    # Dad joke — just show the joke
    if entity_id == "input_select.kaffee_dad_jokes":
        return f'{label}: "{state}"'

    # Room-level lights — include brightness as percentage
    if entity_id.startswith("light."):
        if state == "off":
            return f"{label}: spente"
        brightness = attrs.get("brightness")
        if brightness is not None:
            try:
                pct = round(int(brightness) / 255 * 100)
            except (ValueError, TypeError):
                pct = None
            if pct is not None:
                if pct >= 90:
                    return f"{label}: accese al massimo"
                return f"{label}: luci soffuse (~{pct}%)"
        return f"{label}: accese"

    # Power sensors — translate wattage into activity description
    if entity_id.startswith("sensor.") and attrs.get("device_class") == "power":
        try:
            watts = float(state)
        except (ValueError, TypeError):
            return f"{label}: —"
        # Coffee machine: qualitative activity phases
        if entity_id == "sensor.kuche_kaffeemaschine_steckdose_power":
            if watts > 100:
                return f"{label}: in funzione"
            if watts > 5:
                return f"{label}: riscaldamento"
            return f"{label}: fredda"
        # Total household power: qualitative load context
        if entity_id == "sensor.haushalt_stromverbrauch_gesamt":
            if watts < 200:
                return f"{label}: casa tranquilla ({watts:.0f} W)"
            if watts > 2000:
                return f"{label}: tutto acceso ({watts:.0f} W)"
            return f"{label}: normale ({watts:.0f} W)"
        unit = attrs.get("unit_of_measurement", "W")
        if watts < 1:
            return f"{label}: inattivo"
        return f"{label}: {watts:.0f} {unit}"

    # Default: translate the state
    translated = STATE_TRANSLATIONS.get(state, state)
    return f"{label}: {translated}"


def _build_summary(states: dict[str, dict]) -> str:
    """Build a natural language summary from entity states."""
    lines = []
    for entity_id in ALL_ENTITIES:
        if entity_id in states:
            line = _format_state(entity_id, states[entity_id])
            if line:
                lines.append(f"- {line}")
    return "\n".join(lines) if lines else ""


def _build_scored_entities(
    states: dict[str, dict],
    *,
    event_entity_ids: set[str] | None = None,
    now: float | None = None,
    limit: int = DEFAULT_CONTEXT_ENTITY_LIMIT,
    char_limit: int = DEFAULT_CONTEXT_CHAR_LIMIT,
    cache_dir: Path | None = None,
) -> list[ScoredEntity]:
    """Score filtered HA entities and return the budgeted prompt slice."""
    ref_now = time.time() if now is None else now
    event_ids = event_entity_ids or set()
    scored: list[ScoredEntity] = []
    for entity_id, state_data in states.items():
        resolved = _resolve_label(entity_id, state_data, cache_dir=cache_dir)
        if resolved is None:
            continue
        line = _format_state(entity_id, state_data, cache_dir=cache_dir, resolved=resolved)
        if not line:
            continue
        scored.append(
            ScoredEntity(
                entity_id=entity_id,
                area=_area_from_attrs(state_data.get("attributes", {}) or {}),
                domain=_entity_domain(entity_id),
                score=_score_entity(entity_id, state_data, event_entity_ids=event_ids, now=ref_now),
                raw_state=state_data,
                label_it=resolved.label_it,
                label_en=resolved.label_en,
                label_tier=resolved.tier,
                summary_line=line,
            )
        )

    presence_keep_ids = {
        item.entity_id
        for item in sorted(
            (item for item in scored if _is_capped_presence_sensor(item) and item.area is not None),
            key=_presence_slice_rank,
            reverse=True,
        )[:MAX_PRESENCE_IN_SLICE]
    }
    selected = []
    for item in sorted(
        scored,
        key=lambda item: (item.score, item.entity_id in ENTITY_LABELS, item.entity_id),
        reverse=True,
    ):
        if _is_capped_presence_sensor(item) and (item.area is None or item.entity_id not in presence_keep_ids):
            continue
        selected.append(item)
        if len(selected) >= limit:
            break
    if char_limit <= 0:
        return selected

    budgeted: list[ScoredEntity] = []
    used = 0
    for item in selected:
        rendered = f"- {item.summary_line}"
        projected = used + len(rendered) + (1 if budgeted else 0)
        if projected > char_limit:
            continue
        budgeted.append(item)
        used = projected
    return budgeted


def _is_capped_presence_sensor(item: ScoredEntity) -> bool:
    attrs = item.raw_state.get("attributes", {}) or {}
    return (
        item.domain == "binary_sensor"
        and attrs.get("device_class") in PRESENCE_SENSOR_DEVICE_CLASSES
        and item.entity_id not in ENTITY_LABELS
    )


def _presence_slice_rank(item: ScoredEntity) -> tuple[bool, float, str]:
    # Used with sorted(..., reverse=True) to keep the most relevant uncurated
    # presence sensors: currently-`on` first, then most-recently-changed. The
    # entity_id is only a deterministic, stable tiebreak (reverse-lexicographic
    # under reverse=True) when on-state and recency are equal.
    changed = _parse_ha_timestamp(item.raw_state.get("last_changed")) or 0.0
    return (str(item.raw_state.get("state", "")).lower() == "on", changed, item.entity_id)


def _build_budgeted_summary(scored: list[ScoredEntity]) -> str:
    rendered = "\n".join(f"- {entity.summary_line}" for entity in scored)
    # The summary is concatenated between <home_state_data> tags in
    # scriptwriter.py. Strip angle brackets at the LLM boundary so HA-controlled
    # labels can't close the fence. (Done here, not in _sanitize_state_value,
    # because that sanitizer is also used by the admin UI path where esc()
    # handles HTML escaping client-side.)
    return rendered.replace("<", "").replace(">", "")


def _prune_muted_events(events: deque[HomeEvent] | None, muted_ids: set[str], *, now: float) -> deque[HomeEvent]:
    if not events:
        return deque(maxlen=EVENT_BUFFER_SIZE)
    kept = [event for event in prune_events(events, now=now) if event.entity_id not in muted_ids]
    return deque(kept, maxlen=EVENT_BUFFER_SIZE)


def _has_weather_mute(entity_ids: set[str]) -> bool:
    """Return whether an entity-specific hard mute invalidates the shared weather arc."""
    return any(entity_id.startswith("weather.") for entity_id in entity_ids)


def _apply_muted_policy_to_context(
    context: HomeContext,
    muted_ids: set[str],
    *,
    cache_dir: Path | None = None,
    now: float | None = None,
) -> HomeContext:
    """Remove muted ids from an already-built context cache."""
    if not muted_ids:
        return context
    timestamp = time.time() if now is None else now
    synthetic_muted_ids = {
        synthetic_id for synthetic_id, source_id in context.ambient_sources.items() if source_id in muted_ids
    }
    effective_muted_ids = muted_ids | synthetic_muted_ids
    affected_ids = effective_muted_ids & (
        set(context.raw_states)
        | {event.entity_id for event in context.events}
        | {entity.entity_id for entity in context.scored}
    )
    weather_muted = _has_weather_mute(effective_muted_ids)
    if not affected_ids and not weather_muted:
        return context

    context.raw_states = {
        entity_id: data for entity_id, data in context.raw_states.items() if entity_id not in effective_muted_ids
    }
    context.events = _prune_muted_events(context.events, effective_muted_ids, now=timestamp)
    context.scored = [entity for entity in context.scored if entity.entity_id not in effective_muted_ids]
    context.ambient_sources = {
        synthetic_id: source_id
        for synthetic_id, source_id in context.ambient_sources.items()
        if synthetic_id not in effective_muted_ids
    }
    _, labels_en = _build_entity_label_maps(context.raw_states, cache_dir=cache_dir)
    context.summary = _build_budgeted_summary(context.scored)
    context.events_summary = build_events_summary(context.events, now=timestamp)
    context.events_summary_en = build_events_summary_en(context.events, labels_en, STATE_TRANSLATIONS_EN, now=timestamp)
    if context.authorization_mode == HomeAuthorizationMode.NARROW.value:
        context.mood = ""
        context.mood_en = ""
    else:
        context.mood = classify_home_mood(context.raw_states)
        context.mood_en = classify_home_mood_en(context.raw_states)
    if weather_muted:
        context.weather_arc = ""
        context.weather_arc_en = ""
    context.last_event_label_en = ""
    if context.events:
        newest = max(context.events, key=lambda event: event.timestamp)
        context.last_event_label_en = labels_en.get(newest.entity_id, newest.label)
    label_stats = _label_stats(context.scored)
    context.catalog_hit_rate = float(label_stats["catalog_hit_rate"])
    context.label_stats = label_stats
    context.denylist_hits = dict(context.denylist_hits)
    context.denylist_hits["user_muted"] = max(context.denylist_hits.get("user_muted", 0), len(affected_ids))
    return context


def _serve_filtered_home_context(
    context: HomeContext,
    muted_ids: set[str],
    *,
    cache_dir: Path | None = None,
    now: float | None = None,
    update_global: bool = False,
) -> HomeContext:
    """Serve a cache/stale context through the live mute and summary policy."""
    global _ha_cache
    timestamp = time.time() if now is None else now
    served = context if cache_dir is None else _copy_home_context(context)
    served = _apply_muted_policy_to_context(served, muted_ids, cache_dir=cache_dir, now=timestamp)
    # Cache returns still need live event ages and expired-event pruning; callers
    # should not know which cached path they hit to receive prompt-safe context.
    served.events = _prune_muted_events(served.events, muted_ids, now=timestamp)
    served.events_summary = build_events_summary(served.events, now=timestamp)
    _, labels_en = _build_entity_label_maps(served.raw_states, cache_dir=cache_dir)
    served.events_summary_en = build_events_summary_en(served.events, labels_en, STATE_TRANSLATIONS_EN, now=timestamp)
    served.radio_events = []
    served.ritual_recipe_matches = []
    served.ritual_public_families = []
    served.ritual_recipe_audit = []
    if update_global:
        _ha_cache = served
    return served


def apply_entity_mute_policy(context: HomeContext, cache_dir: Path | None) -> HomeContext:
    """Re-apply the live mute policy to a context obtained outside fetch_home_context.

    ``fetch_home_context`` mute-filters on every return path, but a caller that
    falls back to a previously-held context on its own (e.g. a timed-out
    refresh reusing the caller's stale copy) bypasses that filtering. This is
    the public entry point for those callers.
    """
    if cache_dir is None:
        return context
    muted_ids = muted_entity_ids(Path(cache_dir))
    return _serve_filtered_home_context(context, muted_ids, cache_dir=cache_dir, now=time.time())


def revalidate_home_context_mutes(context: HomeContext, cache_dir: Path | None) -> HomeContext:
    """Apply live mutes to a fresh handoff without discarding unmuted one-shots.

    ``apply_entity_mute_policy`` serves a reusable cache and therefore clears
    radio-event and ritual one-shots to prevent replay.  A just-completed late
    refresh is different: its one-shots are safe to hand to exactly one next
    eligible segment, provided a mute added while the request was in flight is
    applied before that handoff.
    """
    if cache_dir is None:
        return context

    muted_ids = muted_entity_ids(Path(cache_dir))
    served = _copy_home_context(context)
    served = _apply_muted_policy_to_context(served, muted_ids, cache_dir=cache_dir, now=time.time())
    muted_radio_event_ids = {
        match.event.entity_id for match in served.radio_events if match.event.entity_id in muted_ids
    }
    muted_ritual_ids = {match.entity_id for match in served.ritual_recipe_matches if match.entity_id in muted_ids}
    served.radio_events = [match for match in served.radio_events if match.event.entity_id not in muted_ids]
    served.ritual_recipe_matches = [match for match in served.ritual_recipe_matches if match.entity_id not in muted_ids]
    served.ritual_public_families = public_family_labels(served.ritual_recipe_matches)
    muted_one_shot_ids = muted_radio_event_ids | muted_ritual_ids
    if muted_one_shot_ids:
        served.denylist_hits = dict(served.denylist_hits)
        served.denylist_hits["user_muted"] = max(
            served.denylist_hits.get("user_muted", 0),
            len(muted_one_shot_ids),
        )
    return served


def _label_stats(scored: list[ScoredEntity]) -> dict[str, int | float]:
    eligible = len(scored)
    curated = sum(1 for entity in scored if entity.label_tier == "curated")
    catalog_hits = sum(1 for entity in scored if entity.label_tier == "catalog")
    fallback = sum(1 for entity in scored if entity.label_tier == "fallback")
    denominator = max(1, eligible - curated)
    hit_rate = catalog_hits / denominator
    return {
        "eligible": eligible,
        "curated": curated,
        "catalog_hits": catalog_hits,
        "fallback": fallback,
        "catalog_hit_rate": round(hit_rate, 4),
    }


def _build_entity_label_maps(
    states: dict[str, dict],
    *,
    cache_dir: Path | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    labels_it = dict(ENTITY_LABELS)
    labels_en = dict(ENTITY_LABELS_EN)
    for entity_id, state_data in states.items():
        # Mirror the anti-illusion guard in _format_state: don't manufacture a
        # label from the entity_id when the entity has no curated label and no
        # friendly_name. Otherwise diff_states would emit a HomeEvent whose
        # humanized label ("foo bar") still reaches the prompt via
        # events_summary, bypassing the guard.
        if entity_id in labels_it and entity_id in labels_en:
            continue
        resolved = _resolve_label(entity_id, state_data, cache_dir=cache_dir)
        if resolved is None:
            continue
        labels_it.setdefault(entity_id, resolved.label_it)
        labels_en.setdefault(entity_id, resolved.label_en)
    return labels_it, labels_en


# ---------------------------------------------------------------------------
# Phase 2: Home mood classification
# ---------------------------------------------------------------------------


def classify_home_mood(states: dict[str, dict]) -> str:
    """Classify aggregate HA state into a named Italian home scene.

    Priority order — first match wins. Returns "" when no scene matches.
    """

    def _state(eid: str) -> str:
        return states.get(eid, {}).get("state", "")

    def _brightness(eid: str) -> int | None:
        """Return brightness 0-255 for a light entity, or None."""
        data = states.get(eid, {})
        if data.get("state") != "on":
            return None
        val = data.get("attributes", {}).get("brightness")
        if val is None:
            return None
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    def _power_watts(eid: str) -> float:
        """Return power consumption in watts, 0.0 if unavailable."""
        try:
            return float(states.get(eid, {}).get("state", "0"))
        except (ValueError, TypeError):
            return 0.0

    now_hour = datetime.datetime.now().hour

    if _state("vacuum.goldstaubsucher") == "cleaning" or _state("vacuum.matrix10_ultra") == "cleaning":
        return "Il robot sta pulendo"
    if _state("switch.bar_kaffeemaschine_steckdose") == "on" and 5 <= now_hour <= 10:
        return "Stanno svegliandosi"
    if _state("fan.kuche_lufter") == "on":
        return "Qualcuno sta cucinando"
    if _state("fan.bad_gross_lufter_shelly") == "on" or _state("fan.bad_klein_lufter") == "on":
        return "Qualcuno sta facendo la doccia"
    if _power_watts("sensor.bar_bali_boot_steckdose_power") > 10:
        return "Lavatrice in funzione"
    if _power_watts("sensor.kuche_kaffeemaschine_steckdose_power") > 50:
        return "Caffè in preparazione"
    if _state("media_player.samsung_s95ca_65") == "playing" and now_hour >= 18:
        return "Serata cinema"
    if (
        _state("light.schlafzimmer_sternenlicht_projektor_2") == "on"
        or _state("light.kleiderschrank_sternenlicht_projektor") == "on"
    ) and now_hour >= 18:
        return "Serata sotto le stelle"
    if (
        _state("media_player.wohnzimmer_sonos_arc_lautsprecher") == "playing"
        or _state("media_player.esszimmer") == "playing"
    ):
        return "Musica in casa"
    if _state("input_select.bedroom_occupancy_state") == "occupied" and (now_hour >= 22 or now_hour < 8):
        return "Qualcuno sta dormendo"
    # Relaxed atmosphere: living room lights on but dimmed below 40%
    wz_brightness = _brightness("light.magic_areas_light_groups_wohnzimmer_all_lights")
    if wz_brightness is not None and wz_brightness < 102 and now_hour >= 18:
        return "Atmosfera rilassata"
    # House waking up: multiple room lights turning on in the morning
    if 5 <= now_hour <= 9:
        lit_rooms = sum(
            1
            for eid in (
                "light.magic_areas_light_groups_wohnzimmer_all_lights",
                "light.magic_areas_light_groups_kuche_all_lights",
                "light.magic_areas_light_groups_esszimmer_all_lights",
            )
            if _state(eid) == "on"
        )
        if lit_rooms >= 2:
            return "La casa si sta svegliando"
    if _state("person.florian_horner") == "not_home" and _state("person.sabrina") == "not_home":
        return "Casa vuota"
    return ""


def classify_home_mood_en(states: dict[str, dict]) -> str:
    """English version of classify_home_mood for admin UI display."""

    def _state(eid: str) -> str:
        return states.get(eid, {}).get("state", "")

    def _brightness(eid: str) -> int | None:
        data = states.get(eid, {})
        if data.get("state") != "on":
            return None
        val = data.get("attributes", {}).get("brightness")
        if val is None:
            return None
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    def _power_watts(eid: str) -> float:
        try:
            return float(states.get(eid, {}).get("state", "0"))
        except (ValueError, TypeError):
            return 0.0

    now_hour = datetime.datetime.now().hour

    if _state("vacuum.goldstaubsucher") == "cleaning" or _state("vacuum.matrix10_ultra") == "cleaning":
        return "Robot vacuum running"
    if _state("switch.bar_kaffeemaschine_steckdose") == "on" and 5 <= now_hour <= 10:
        return "Morning coffee"
    if _state("fan.kuche_lufter") == "on":
        return "Someone cooking"
    if _state("fan.bad_gross_lufter_shelly") == "on" or _state("fan.bad_klein_lufter") == "on":
        return "Someone showering"
    if _power_watts("sensor.bar_bali_boot_steckdose_power") > 10:
        return "Washing machine running"
    if _power_watts("sensor.kuche_kaffeemaschine_steckdose_power") > 50:
        return "Coffee brewing"
    if _state("media_player.samsung_s95ca_65") == "playing" and now_hour >= 18:
        return "Movie night"
    if (
        _state("light.schlafzimmer_sternenlicht_projektor_2") == "on"
        or _state("light.kleiderschrank_sternenlicht_projektor") == "on"
    ) and now_hour >= 18:
        return "Evening under the stars"
    if (
        _state("media_player.wohnzimmer_sonos_arc_lautsprecher") == "playing"
        or _state("media_player.esszimmer") == "playing"
    ):
        return "Music at home"
    if _state("input_select.bedroom_occupancy_state") == "occupied" and (now_hour >= 22 or now_hour < 8):
        return "Someone sleeping"
    wz_brightness = _brightness("light.magic_areas_light_groups_wohnzimmer_all_lights")
    if wz_brightness is not None and wz_brightness < 102 and now_hour >= 18:
        return "Relaxed atmosphere"
    if 5 <= now_hour <= 9:
        lit_rooms = sum(
            1
            for eid in (
                "light.magic_areas_light_groups_wohnzimmer_all_lights",
                "light.magic_areas_light_groups_kuche_all_lights",
                "light.magic_areas_light_groups_esszimmer_all_lights",
            )
            if _state(eid) == "on"
        )
        if lit_rooms >= 2:
            return "House waking up"
    if _state("person.florian_horner") == "not_home" and _state("person.sabrina") == "not_home":
        return "Empty home"
    return ""


# ---------------------------------------------------------------------------
# Phase 3: Weather narrative arc
# ---------------------------------------------------------------------------

_weather_forecast_cache: str = ""
_weather_forecast_cache_en: str = ""
_weather_forecast_fetched_at: float = 0.0
_WEATHER_CACHE_TTL = 3600.0
_SIGNIFICANT_CONDITIONS = {"rainy", "snowy", "lightning", "windy", "fog"}


def _build_weather_arc(forecast: list[dict]) -> str:
    """Build a day-arc weather narrative from hourly forecast items."""
    if not forecast:
        return ""

    now_hour = datetime.datetime.now().hour
    current = forecast[0]
    current_cond = _sanitize_state_value(str(current.get("condition", "")), max_len=30)
    current_temp = current.get("temperature")

    # Look 6 hours ahead for upcoming significant weather
    upcoming_sig: str | None = None
    for fc in forecast[1:7]:
        cond = _sanitize_state_value(str(fc.get("condition", "")), max_len=30)
        if cond in _SIGNIFICANT_CONDITIONS:
            upcoming_sig = cond
            break

    current_is_sig = current_cond in _SIGNIFICANT_CONDITIONS
    current_italian = STATE_TRANSLATIONS.get(current_cond, current_cond)

    if upcoming_sig and now_hour < 12:
        italian = STATE_TRANSLATIONS.get(upcoming_sig, upcoming_sig)
        return f"Attenzione: {italian} in arrivo questo pomeriggio."
    if current_is_sig and 12 <= now_hour < 18:
        temp_str = f", {current_temp}°C" if current_temp is not None else ""
        return f"Fuori c'è {current_italian}{temp_str} — come previsto."
    if current_is_sig and now_hour >= 18:
        return f"Siete sopravvissuti alla {current_italian} di oggi?"
    if current_italian and current_temp is not None:
        return f"Meteo: {current_italian}, {current_temp}°C."
    return ""


def _build_weather_arc_en(forecast: list[dict]) -> str:
    """English version of _build_weather_arc for admin UI display."""
    if not forecast:
        return ""

    now_hour = datetime.datetime.now().hour
    current = forecast[0]
    current_cond = _sanitize_state_value(str(current.get("condition", "")), max_len=30)
    current_temp = current.get("temperature")

    upcoming_sig: str | None = None
    for fc in forecast[1:7]:
        cond = _sanitize_state_value(str(fc.get("condition", "")), max_len=30)
        if cond in _SIGNIFICANT_CONDITIONS:
            upcoming_sig = cond
            break

    current_is_sig = current_cond in _SIGNIFICANT_CONDITIONS
    current_en = STATE_TRANSLATIONS_EN.get(current_cond, current_cond)

    if upcoming_sig and now_hour < 12:
        en = STATE_TRANSLATIONS_EN.get(upcoming_sig, upcoming_sig)
        return f"Heads up: {en} expected this afternoon."
    if current_is_sig and 12 <= now_hour < 18:
        temp_str = f", {current_temp}°C" if current_temp is not None else ""
        return f"Outside: {current_en}{temp_str} — as forecast."
    if current_is_sig and now_hour >= 18:
        return f"Did you survive the {current_en} today?"
    if current_en and current_temp is not None:
        return f"Weather: {current_en}, {current_temp}°C."
    return ""


async def fetch_weather_forecast(ha_url: str, ha_token: str) -> str:
    """Fetch hourly weather forecast from HA and return a narrative arc string (Italian).

    Cached for 1 hour. Returns "" if HA does not support get_forecasts or on error.
    """
    global _weather_forecast_cache, _weather_forecast_cache_en, _weather_forecast_fetched_at
    if time.time() - _weather_forecast_fetched_at < _WEATHER_CACHE_TTL:
        return _weather_forecast_cache

    try:
        client = _get_ha_client()
        resp = await client.post(
            f"{ha_url.rstrip('/')}/api/services/weather/get_forecasts",
            headers={
                "Authorization": f"Bearer {ha_token}",
                "Content-Type": "application/json",
            },
            json={"entity_id": "weather.forecast_home", "type": "hourly"},
            params={"return_response": "true"},
        )
        resp.raise_for_status()
        data = resp.json()
        response_data = data.get("response", {}) or data
        first_entry: dict = next(iter(response_data.values()), {})
        forecast_list: list[dict] = first_entry.get("forecast", [])
        arc = _build_weather_arc(forecast_list)
        arc_en = _build_weather_arc_en(forecast_list)
        _weather_forecast_cache = arc
        _weather_forecast_cache_en = arc_en
        _weather_forecast_fetched_at = time.time()
        logger.debug("Weather arc: %s", arc or "(none)")
        return arc
    except Exception as e:
        logger.debug("Weather forecast unavailable: %s", e)
        _weather_forecast_cache = ""
        _weather_forecast_cache_en = ""
        _weather_forecast_fetched_at = time.time()
        return ""


def get_weather_arc_en() -> str:
    """Return the cached English weather arc string."""
    return _weather_forecast_cache_en


# ---------------------------------------------------------------------------
# Phase 4: Reactive trigger dispatch
# ---------------------------------------------------------------------------


def _parse_ha_timestamp(raw: object) -> float | None:
    """Parse an HA ISO-8601 timestamp string to an epoch float; return None on failure."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        # HA emits e.g. "2026-05-20T14:32:17.345678+00:00"; fromisoformat handles
        # the "Z" suffix from 3.11+.
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def check_reactive_triggers(
    events: deque[HomeEvent],
    current_states: dict[str, dict] | None = None,
    timer_interrupts: list[TimerInterruptConfig] | None = None,
) -> str | InterruptSpec | None:
    """Scan recent events and current sensor states for reactive triggers.

    Event-based triggers (REACTIVE_TRIGGERS) only consider events less than 2
    minutes old. Threshold triggers (THRESHOLD_TRIGGERS) check the live sensor
    value against a wattage threshold each call; the cooldown prevents re-firing.
    Timer interrupts match timer.xyz → idle transitions and return InterruptSpec.
    All checks share the module-level _reactive_cooldowns dict with namespaced keys
    to avoid collision: event keys use "entity:state", threshold keys use
    "entity:threshold:value", timer keys use "timer:entity_id".

    Returns InterruptSpec for timer matches, directive str for other triggers, or None.
    """
    now = time.time()
    age_cutoff = now - 120  # 2 minutes

    # Timer interrupt check: match timer entity idle transitions from recent events.
    # Checked first — timers take priority over ambient banter triggers.
    # Only fire on a *natural* finish: HA stamps the timer's `finished_at`
    # attribute when it runs out; cancel/reset leaves the prior value untouched.
    # So require a recent finished_at to distinguish "timer expired" from
    # "operator cancelled" — both transition state to `idle`.
    if timer_interrupts and current_states is not None:
        for cfg in timer_interrupts:
            state_data = current_states.get(cfg.entity_id, {})
            if state_data.get("state") != "idle":
                continue
            cooldown_key = f"timer:{cfg.entity_id}"
            if now - _reactive_cooldowns.get(cooldown_key, 0.0) < cfg.cooldown:
                continue
            # Confirm via events: a recent active→idle transition must exist
            fired = False
            for event in reversed(events):
                if event.timestamp < age_cutoff:
                    break
                if event.entity_id == cfg.entity_id and event.new_state == "idle":
                    fired = True
                    break
            if not fired:
                continue
            # Filter out cancel/reset: finished_at must be set and recent.
            finished_at_ts = _parse_ha_timestamp((state_data.get("attributes") or {}).get("finished_at"))
            if finished_at_ts is None or now - finished_at_ts > 30:
                continue
            _reactive_cooldowns[cooldown_key] = now
            return InterruptSpec(
                directive=cfg.directive,
                urgency=cfg.urgency,
                cooldown=cfg.cooldown,
            )

    for event in reversed(events):
        if event.timestamp < age_cutoff:
            break
        for entity_id, trigger_state, directive, cooldown in REACTIVE_TRIGGERS:
            if event.entity_id != entity_id:
                continue
            expected = STATE_TRANSLATIONS.get(trigger_state, trigger_state)
            if event.new_state != expected:
                continue
            cooldown_key = f"{entity_id}:{trigger_state}"
            if now - _reactive_cooldowns.get(cooldown_key, 0.0) < cooldown:
                continue
            _reactive_cooldowns[cooldown_key] = now
            return directive

    if current_states is not None:
        for trigger in THRESHOLD_TRIGGERS:
            eid = trigger["entity_id"]
            state_data = current_states.get(eid, {})
            try:
                val = float(state_data.get("state", "0"))
            except (ValueError, TypeError):
                continue
            threshold = trigger["threshold"]
            direction = trigger["direction"]
            crossed = (direction == "above" and val > threshold) or (direction == "below" and val < threshold)
            if not crossed:
                continue
            cooldown_key = f"{eid}:threshold:{threshold}"
            if now - _reactive_cooldowns.get(cooldown_key, 0.0) < trigger["cooldown"]:
                continue
            _reactive_cooldowns[cooldown_key] = now
            return trigger["directive"]

    return None


_ha_client: httpx.AsyncClient | None = None
_ha_cache: HomeContext | None = None
_radio_event_state_cache: dict[str, dict] = {}
_ritual_recipe_state_cache: dict[str, dict] = {}
_ha_registry_snapshot_cache: HomeRegistrySnapshot | None = None
_ha_registry_fetched_at: float = 0.0
_HA_REGISTRY_TTL = 6 * 60 * 60
_HA_REGISTRY_FAILURE_TTL = 60
_HA_REGISTRY_STALE_TTL = 7 * 24 * 60 * 60
_HA_REGISTRY_FILENAME = "ha_registry.json"
# The producer owns the outer 30-second refresh cap. The state request itself
# must not retain the client's historical 10-second default and throw away a
# reply that the late-recovery contract explicitly allows us to adopt.
_HA_CONTEXT_TOTAL_FETCH_TIMEOUT = 30.0
# Registry labels and forecast are enrichment, not a reason to discard an
# already-received `/api/states` snapshot near the total deadline.
_HA_CONTEXT_OPTIONAL_ENRICHMENT_TIMEOUT = 5.0


async def _cancel_and_await_enrichment_tasks(*tasks: asyncio.Task) -> None:
    """Cancel optional enrichment work and observe every terminal result.

    A state-fetch failure or producer shutdown must not leave registry/weather
    work running after the owned refresh has finished.  ``gather`` with
    ``return_exceptions`` both awaits cancellation and retrieves a racing
    ordinary exception, avoiding an unobserved-task warning.
    """
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _get_ha_client() -> httpx.AsyncClient:
    """Return a reusable async HTTP client for HA API calls."""
    global _ha_client
    if _ha_client is None or _ha_client.is_closed:
        _ha_client = httpx.AsyncClient(timeout=10.0)
    return _ha_client


def _ha_websocket_url(ha_url: str) -> str:
    parsed = urlsplit(ha_url.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    base_path = parsed.path.rstrip("/")
    if parsed.netloc == "supervisor":
        # Supervisor add-on proxy exposes the Core websocket at /core/websocket
        # (HA_URL is http://supervisor/core), NOT /core/api/websocket.
        ws_path = f"{base_path}/websocket" if base_path else "/core/websocket"
    else:
        # Direct Core (optionally behind a reverse-proxy subpath) uses /api/websocket.
        ws_path = f"{base_path}/api/websocket"
    return urlunsplit((scheme, parsed.netloc, ws_path, "", ""))


async def _fetch_ha_registry_areas(ha_url: str, ha_token: str) -> dict[str, str]:
    """Compatibility wrapper returning entity_id -> area name."""
    snapshot = await _fetch_ha_registry_snapshot(ha_url, ha_token)
    return snapshot.entity_areas


def _ha_registry_cache_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / _HA_REGISTRY_FILENAME


def _registry_snapshot_with_source(snapshot: HomeRegistrySnapshot, source: str) -> HomeRegistrySnapshot:
    return replace(snapshot, source=source)


def _load_registry_snapshot(cache_dir: Path, *, now: float | None = None) -> HomeRegistrySnapshot | None:
    path = _ha_registry_cache_path(cache_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    fetched_at = data.get("fetched_at")
    if not isinstance(fetched_at, int | float):
        return None
    ref_now = time.time() if now is None else now
    source = "disk_fresh" if ref_now - float(fetched_at) < _HA_REGISTRY_TTL else "disk_stale"

    def _str_map(value: object) -> dict[str, str] | None:
        # A mapping field that is ABSENT (older/partial cache) degrades to empty.
        # But a field that is PRESENT and malformed — a non-dict (e.g. []) or one
        # with nested junk like {"light.x": ["Kitchen"]} — marks the cache corrupt:
        # return None so the loader treats the whole file as a miss. The caller then
        # refetches via websocket (and rewrites the cache) instead of serving an
        # empty or garbage registry for the full TTL.
        if value is None:
            return {}
        if not isinstance(value, dict):
            return None
        cleaned: dict[str, str] = {}
        for key, val in value.items():
            if not isinstance(key, str) or not isinstance(val, str):
                return None
            cleaned[key] = val
        return cleaned

    entity_areas = _str_map(data.get("entity_areas"))
    entity_names = _str_map(data.get("entity_names"))
    entity_device_names = _str_map(data.get("entity_device_names"))
    if entity_areas is None or entity_names is None or entity_device_names is None:
        return None

    return HomeRegistrySnapshot(
        entity_areas=entity_areas,
        entity_names=entity_names,
        entity_device_names=entity_device_names,
        fetched_at=float(fetched_at),
        source=source,
    )


def _write_registry_snapshot(cache_dir: Path, snapshot: HomeRegistrySnapshot) -> None:
    path = _ha_registry_cache_path(cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    payload = {
        "schema_version": 1,
        "fetched_at": snapshot.fetched_at,
        "entity_areas": snapshot.entity_areas,
        "entity_names": snapshot.entity_names,
        "entity_device_names": snapshot.entity_device_names,
    }
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        logger.warning("Failed to write HA registry cache %s: %s", path, exc)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


async def _fetch_ha_registry_snapshot(
    ha_url: str,
    ha_token: str,
    *,
    cache_dir: Path | None = None,
) -> HomeRegistrySnapshot:
    """Fetch HA registry metadata with memory/disk cache and stale fallback."""
    global _ha_registry_snapshot_cache, _ha_registry_fetched_at
    now = time.time()
    if _ha_registry_snapshot_cache is not None and now - _ha_registry_fetched_at < _HA_REGISTRY_TTL:
        if _ha_registry_snapshot_cache.source in {"empty_fallback", "disk_stale"}:
            return _ha_registry_snapshot_cache
        return _registry_snapshot_with_source(_ha_registry_snapshot_cache, "memory")

    disk_snapshot: HomeRegistrySnapshot | None = None
    if cache_dir is not None:
        disk_snapshot = _load_registry_snapshot(Path(cache_dir), now=now)
        if disk_snapshot is not None and disk_snapshot.source == "disk_fresh":
            _ha_registry_snapshot_cache = disk_snapshot
            _ha_registry_fetched_at = disk_snapshot.fetched_at
            return disk_snapshot

    try:
        snapshot = await _fetch_ha_registry_snapshot_websocket(ha_url, ha_token, fetched_at=now)
        _ha_registry_snapshot_cache = snapshot
        _ha_registry_fetched_at = now
        if cache_dir is not None:
            _write_registry_snapshot(Path(cache_dir), snapshot)
        logger.debug(
            "Fetched HA registries: %d areas, %d entity names, %d device names",
            len(snapshot.entity_areas),
            len(snapshot.entity_names),
            len(snapshot.entity_device_names),
        )
        return snapshot
    except Exception as exc:
        logger.debug("HA registry fetch unavailable: %s", exc)
        if disk_snapshot is None and cache_dir is not None:
            disk_snapshot = _load_registry_snapshot(Path(cache_dir), now=now)
        if disk_snapshot is not None and now - disk_snapshot.fetched_at < _HA_REGISTRY_STALE_TTL:
            stale = _registry_snapshot_with_source(disk_snapshot, "disk_stale")
            _ha_registry_snapshot_cache = stale
            _ha_registry_fetched_at = now - _HA_REGISTRY_TTL + _HA_REGISTRY_FAILURE_TTL
            return stale
        fallback = HomeRegistrySnapshot(
            fetched_at=now - _HA_REGISTRY_TTL + _HA_REGISTRY_FAILURE_TTL,
            source="empty_fallback",
        )
        _ha_registry_snapshot_cache = fallback
        _ha_registry_fetched_at = fallback.fetched_at
        return fallback


async def _fetch_ha_registry_snapshot_websocket(
    ha_url: str,
    ha_token: str,
    *,
    fetched_at: float,
) -> HomeRegistrySnapshot:
    async with websocket_connect(_ha_websocket_url(ha_url), open_timeout=5, close_timeout=1) as ws:
        auth_required = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        if auth_required.get("type") != "auth_required":
            raise RuntimeError("unexpected HA websocket greeting")
        await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
        auth_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        if auth_response.get("type") != "auth_ok":
            raise RuntimeError(auth_response.get("message") or "HA websocket auth failed")

        commands = [
            (1, "config/entity_registry/list"),
            (2, "config/device_registry/list"),
            (3, "config/area_registry/list"),
        ]
        for msg_id, msg_type in commands:
            await ws.send(json.dumps({"id": msg_id, "type": msg_type}))

        results: dict[int, list[dict]] = {}
        while len(results) < len(commands):
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            msg_id = message.get("id")
            if message.get("type") != "result" or msg_id not in {1, 2, 3}:
                continue
            if not message.get("success", False):
                raise RuntimeError(f"HA registry command {msg_id} failed")
            result = message.get("result") or []
            results[int(msg_id)] = result if isinstance(result, list) else []

    areas = {
        str(area.get("area_id")): str(area.get("name") or area.get("area_id"))
        for area in results.get(3, [])
        if area.get("area_id")
    }
    device_areas: dict[str, str] = {}
    device_names: dict[str, str] = {}
    for device in results.get(2, []):
        device_id = device.get("id")
        if not device_id:
            continue
        if device.get("area_id"):
            device_areas[str(device_id)] = str(device.get("area_id"))
        name = device.get("name_by_user") or device.get("name")
        if name:
            device_names[str(device_id)] = str(name)

    entity_areas: dict[str, str] = {}
    entity_names: dict[str, str] = {}
    entity_device_names: dict[str, str] = {}
    for entity in results.get(1, []):
        entity_id = entity.get("entity_id")
        if not entity_id:
            continue
        entity_key = str(entity_id)
        device_id = str(entity.get("device_id") or "")
        area_id = entity.get("area_id") or device_areas.get(device_id)
        area_name = areas.get(str(area_id)) if area_id else None
        if area_name:
            entity_areas[entity_key] = area_name
        entity_name = entity.get("name") or entity.get("original_name")
        if entity_name:
            entity_names[entity_key] = str(entity_name)
        if device_id and device_names.get(device_id):
            entity_device_names[entity_key] = device_names[device_id]

    return HomeRegistrySnapshot(
        entity_areas=entity_areas,
        entity_names=entity_names,
        entity_device_names=entity_device_names,
        fetched_at=fetched_at,
        source="websocket",
    )


def _apply_registry_area(entity_id: str, state_data: dict, registry_areas: dict[str, str]) -> dict:
    return _apply_registry_snapshot(entity_id, state_data, HomeRegistrySnapshot(entity_areas=registry_areas))


def _apply_registry_snapshot(entity_id: str, state_data: dict, snapshot: HomeRegistrySnapshot) -> dict:
    area = snapshot.entity_areas.get(entity_id)
    entity_name = snapshot.entity_names.get(entity_id)
    device_name = snapshot.entity_device_names.get(entity_id)
    if not area and not entity_name and not device_name:
        return state_data
    attrs = dict(state_data.get("attributes", {}) or {})
    changed = False
    if area and not (attrs.get("area") or attrs.get("area_name") or attrs.get("area_id")):
        attrs["area"] = area
        changed = True
    if entity_name and not attrs.get("registry_entity_name"):
        attrs["registry_entity_name"] = entity_name
        changed = True
    if device_name and not attrs.get("registry_device_name"):
        attrs["registry_device_name"] = device_name
        changed = True
    if not changed:
        return state_data
    enriched = dict(state_data)
    enriched["attributes"] = attrs
    return enriched


async def _fetch_home_context_outcome(
    ha_url: str,
    ha_token: str,
    poll_interval: float = 60.0,
    _cache: HomeContext | None = None,
    cache_dir: Path | None = None,
    radio_event_rules: list[RadioEventRule] | None = None,
    authorization: HomeAuthorization | None = None,
    observed_entity_ids_callback: Callable[[frozenset[str]], None] | None = None,
) -> _HomeContextFetchOutcome:
    """Fetch HA context without publishing it to module-level state.

    The producer can retain this coroutine after its foreground budget expires.
    Keeping publication outside the task prevents a late result from changing
    event baselines or the visible snapshot before the next safe segment
    boundary accepts it.
    """
    active_authorization = authorization or HomeAuthorization.narrow()
    attempt_started_at = time.time()
    started_monotonic = time.monotonic()

    def outcome(
        kind: Literal["fresh", "cached", "failed"],
        context: HomeContext,
        *,
        radio_event_state_baseline: dict[str, dict] | None = None,
        ritual_recipe_state_baseline: dict[str, dict] | None = None,
    ) -> _HomeContextFetchOutcome:
        attempt_finished_at = time.time()
        return _HomeContextFetchOutcome(
            kind=kind,
            context=context,
            snapshot_timestamp=context.timestamp,
            attempt_started_at=attempt_started_at,
            attempt_finished_at=attempt_finished_at,
            duration_seconds=max(0.0, time.monotonic() - started_monotonic),
            radio_event_state_baseline=radio_event_state_baseline or {},
            ritual_recipe_state_baseline=ritual_recipe_state_baseline or {},
        )

    # Prefer explicitly passed cache, then module-level cache. A context built
    # under the legacy bridge is never a valid stale fallback for a cold/narrow
    # install (and vice versa), including in process-reuse tests.
    effective_cache = _cache or _ha_cache
    if effective_cache and effective_cache.authorization_mode != active_authorization.mode.value:
        effective_cache = None
    muted_ids = muted_entity_ids(Path(cache_dir)) if cache_dir is not None else set()
    if effective_cache and effective_cache.age_seconds < poll_interval:
        return outcome(
            "cached",
            _serve_filtered_home_context(
                effective_cache,
                muted_ids,
                cache_dir=cache_dir,
                now=time.time(),
                update_global=False,
            ),
        )

    enrichment_tasks: list[asyncio.Task] = []
    try:

        async def _optional_registry_snapshot() -> HomeRegistrySnapshot:
            try:
                return await asyncio.wait_for(
                    _fetch_ha_registry_snapshot(ha_url, ha_token, cache_dir=cache_dir),
                    timeout=_HA_CONTEXT_OPTIONAL_ENRICHMENT_TIMEOUT,
                )
            except TimeoutError:
                logger.debug("HA registry enrichment exceeded %.1fs", _HA_CONTEXT_OPTIONAL_ENRICHMENT_TIMEOUT)
                return HomeRegistrySnapshot(fetched_at=time.time(), source="empty_fallback")
            except Exception as exc:
                logger.debug("HA registry enrichment unavailable: %s", exc)
                return HomeRegistrySnapshot(fetched_at=time.time(), source="empty_fallback")

        async def _optional_weather_arc() -> str:
            try:
                return await asyncio.wait_for(
                    fetch_weather_forecast(ha_url, ha_token),
                    timeout=_HA_CONTEXT_OPTIONAL_ENRICHMENT_TIMEOUT,
                )
            except TimeoutError:
                logger.debug("HA weather enrichment exceeded %.1fs", _HA_CONTEXT_OPTIONAL_ENRICHMENT_TIMEOUT)
                return ""
            except Exception as exc:
                logger.debug("HA weather enrichment unavailable: %s", exc)
                return ""

        # Start enrichment before awaiting `/api/states`, so its independent
        # five-second caps overlap the full state request instead of consuming
        # the tail of the producer-owned 30-second refresh budget.
        # Narrow (cold-install) mode never loads registry names or the weather
        # forecast arc — the projection would discard both — so skip the network
        # work entirely and mark the registry as deliberately not loaded.
        registry_task: asyncio.Task | None = None
        weather_task: asyncio.Task | None = None
        if active_authorization.mode is not HomeAuthorizationMode.NARROW:
            registry_task = asyncio.create_task(
                _optional_registry_snapshot(),
                name="ha-context-registry-enrichment",
            )
            enrichment_tasks.append(registry_task)
            # Any weather.* hard mute invalidates the shared forecast arc, so an
            # operator muting a single weather source skips the forecast fetch
            # entirely (privacy) — not just a mute of weather.forecast_home.
            if not _has_weather_mute(muted_ids):
                weather_task = asyncio.create_task(
                    _optional_weather_arc(),
                    name="ha-context-weather-enrichment",
                )
                enrichment_tasks.append(weather_task)

        client = _get_ha_client()
        resp = await client.get(
            f"{ha_url.rstrip('/')}/api/states",
            headers={
                "Authorization": f"Bearer {ha_token}",
                "Content-Type": "application/json",
            },
            timeout=_HA_CONTEXT_TOTAL_FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        all_states = resp.json()

        # Optional enrichment errors and timeouts have already degraded to
        # fallback values.  Await their tasks only after the state reply so a
        # slow but valid `/api/states` response is never serialized behind
        # registry/weather work.
        registry_snapshot = (
            await registry_task if registry_task is not None else HomeRegistrySnapshot(source="narrow_not_loaded")
        )
        fetched_weather_arc = await weather_task if weather_task is not None else ""

        timestamp = time.time()
        denylist_hits: dict[str, int] = {}
        # Re-read the mute policy after the awaited HA/registry calls above so an
        # operator mute applied mid-refresh isn't served by a stale pre-await
        # snapshot (TOCTOU — codex adversarial review).
        if cache_dir is not None:
            muted_ids = muted_entity_ids(Path(cache_dir))
        all_entity_map = {str(e.get("entity_id", "")): e for e in all_states if e.get("entity_id")}
        if observed_entity_ids_callback is not None:
            try:
                observed_entity_ids_callback(frozenset(all_entity_map))
            except Exception:
                # Bridge provenance is diagnostic/recovery metadata. It must be
                # loud in logs but can never block the context fetch or audio.
                logger.warning("Legacy-home observation persistence failed", exc_info=True)
        muted_present = set(all_entity_map) & muted_ids
        # Muted ids are dropped from entity_map itself — not just the ambient
        # "relevant" slice — so a configured radio_event rule can never fire a
        # directive for an entity the operator explicitly excluded.
        entity_map = (
            {eid: data for eid, data in all_entity_map.items() if eid not in muted_ids}
            if muted_present
            else all_entity_map
        )
        enriched_entity_map = {
            entity_id: _apply_registry_snapshot(entity_id, state_data, registry_snapshot)
            for entity_id, state_data in entity_map.items()
        }
        projection = active_authorization.project(enriched_entity_map)
        projected_muted = set(projection.states) & muted_ids
        authorized_entity_map = {
            entity_id: state_data
            for entity_id, state_data in projection.states.items()
            if entity_id not in projected_muted
        }
        ambient_sources = {
            synthetic_id: source_id
            for synthetic_id, source_id in projection.ambient_sources.items()
            if synthetic_id not in projected_muted
        }
        radio_events: list[RadioEventMatch] = []
        if active_authorization.allows_household_moments and radio_event_rules:
            try:
                radio_events = match_radio_events(
                    radio_event_rules,
                    _radio_event_state_cache,
                    authorized_entity_map,
                    now=timestamp,
                )
            except Exception as exc:  # pragma: no cover - defensive continuity guard
                logger.warning("Failed to match configured HA radio events: %s", exc)
                radio_events = []
            try:
                radio_event_state_baseline = build_radio_event_baseline(authorized_entity_map, radio_event_rules)
            except Exception as exc:  # pragma: no cover - defensive continuity guard
                logger.warning("Failed to update configured HA radio-event baseline: %s", exc)
                radio_event_state_baseline = {}
        else:
            radio_event_state_baseline = {}
        ritual_recipe_matches: list[RitualRecipeMatch] = []
        if active_authorization.allows_household_moments:
            try:
                ritual_recipe_matches = match_ritual_recipes(
                    None,
                    _ritual_recipe_state_cache,
                    authorized_entity_map,
                    now=timestamp,
                )
            except Exception as exc:  # pragma: no cover - defensive continuity guard
                logger.warning("Failed to match HA ritual recipes: %s", exc)
                ritual_recipe_matches = []
            try:
                ritual_recipe_state_baseline = build_ritual_recipe_baseline(authorized_entity_map)
            except Exception as exc:  # pragma: no cover - defensive continuity guard
                logger.warning("Failed to update HA ritual recipe baseline: %s", exc)
                ritual_recipe_state_baseline = {}
        else:
            ritual_recipe_state_baseline = {}
        relevant = {
            entity_id: filtered
            for entity_id, state_data in authorized_entity_map.items()
            if (
                filtered := _filter_state(
                    entity_id,
                    state_data,
                    denylist_hits,
                )
            )
            is not None
        }
        if muted_present or projected_muted:
            denylist_hits["user_muted"] = denylist_hits.get("user_muted", 0) + len(muted_present | projected_muted)
        old_states = {
            entity_id: state_data
            for entity_id, state_data in (effective_cache.raw_states if effective_cache else {}).items()
            if entity_id not in muted_ids
        }
        old_events = _prune_muted_events(effective_cache.events, muted_ids, now=timestamp) if effective_cache else None
        labels_it, labels_en = _build_entity_label_maps(relevant, cache_dir=cache_dir)
        events = (
            diff_states(
                old_states,
                relevant,
                old_events,
                entity_labels=labels_it,
                state_translations=STATE_TRANSLATIONS,
                now=timestamp,
            )
            if active_authorization.allows_household_moments
            else deque(maxlen=EVENT_BUFFER_SIZE)
        )
        scored = _build_scored_entities(
            relevant,
            event_entity_ids={event.entity_id for event in events},
            now=timestamp,
            cache_dir=cache_dir,
        )
        label_stats = _label_stats(scored)
        mood = classify_home_mood(relevant) if active_authorization.allows_derived_mood else ""
        mood_en = classify_home_mood_en(relevant) if active_authorization.allows_derived_mood else ""
        weather_muted = _has_weather_mute(muted_ids)
        weather_arc = (
            "" if weather_muted or active_authorization.mode is HomeAuthorizationMode.NARROW else fetched_weather_arc
        )
        summary = _build_budgeted_summary(scored)
        events_summary = build_events_summary(events, now=timestamp)
        events_summary_en = build_events_summary_en(events, labels_en, STATE_TRANSLATIONS_EN, now=timestamp)
        ritual_public_families = public_family_labels(ritual_recipe_matches)
        ritual_recipe_audit = (
            audit_ritual_recipes(states=authorized_entity_map) if active_authorization.allows_household_moments else []
        )
        # Determine English label of the most recent event for admin display
        last_event_label_en = ""
        if events:
            newest = max(events, key=lambda e: e.timestamp)
            last_event_label_en = labels_en.get(newest.entity_id, newest.label)
        context = HomeContext(
            raw_states=relevant,
            summary=summary,
            events=events,
            radio_events=radio_events,
            ritual_recipe_matches=ritual_recipe_matches,
            ritual_public_families=ritual_public_families,
            ritual_recipe_audit=ritual_recipe_audit,
            events_summary=events_summary,
            mood=mood,
            weather_arc=weather_arc,
            timestamp=timestamp,
            mood_en=mood_en,
            weather_arc_en=(
                ""
                if weather_muted or active_authorization.mode is HomeAuthorizationMode.NARROW
                else get_weather_arc_en()
            ),
            events_summary_en=events_summary_en,
            last_event_label_en=last_event_label_en,
            scored=scored,
            catalog_hit_rate=float(label_stats["catalog_hit_rate"]),
            label_stats=label_stats,
            registry_source=registry_snapshot.source,
            denylist_hits=denylist_hits,
            authorization_mode=active_authorization.mode.value,
            ambient_sources=ambient_sources,
        )
        logger.info(
            "Fetched HA context: %d/%d entities, %d scored, %d events, %d ritual matches, mood=%r",
            len(relevant),
            len(authorized_entity_map),
            len(scored),
            len(events),
            len(ritual_recipe_matches),
            mood or "none",
        )
        return outcome(
            "fresh",
            context,
            radio_event_state_baseline=radio_event_state_baseline,
            ritual_recipe_state_baseline=ritual_recipe_state_baseline,
        )

    except asyncio.CancelledError:
        await _cancel_and_await_enrichment_tasks(*enrichment_tasks)
        raise
    except Exception as e:
        await _cancel_and_await_enrichment_tasks(*enrichment_tasks)
        logger.warning("Failed to fetch HA context: %s", e)
        # Return stale cache if available, otherwise empty
        if effective_cache:
            timestamp = time.time()
            return outcome(
                "failed",
                _serve_filtered_home_context(
                    effective_cache,
                    muted_ids,
                    cache_dir=cache_dir,
                    now=timestamp,
                    update_global=False,
                ),
            )
        return outcome("failed", HomeContext(authorization_mode=active_authorization.mode.value))


def _publish_home_context_outcome(outcome: _HomeContextFetchOutcome) -> bool:
    """Publish a producer-accepted fresh result and its candidate baselines.

    Cached and failed outcomes intentionally leave all module caches untouched.
    Returning whether publication occurred lets a coordinator reject stale or
    otherwise ineligible fresh outcomes without a second state mutation path.
    """
    global _ha_cache, _radio_event_state_cache, _ritual_recipe_state_cache
    if outcome.kind != "fresh":
        return False
    _ha_cache = outcome.context
    _radio_event_state_cache = outcome.radio_event_state_baseline
    _ritual_recipe_state_cache = outcome.ritual_recipe_state_baseline
    return True


async def fetch_home_context(
    ha_url: str,
    ha_token: str,
    poll_interval: float = 60.0,
    _cache: HomeContext | None = None,
    cache_dir: Path | None = None,
    radio_event_rules: list[RadioEventRule] | None = None,
    authorization: HomeAuthorization | None = None,
    observed_entity_ids_callback: Callable[[frozenset[str]], None] | None = None,
) -> HomeContext:
    """Fetch current home state from HA REST API (legacy context-only surface).

    Existing callers receive the same ``HomeContext`` fallback contract.  New
    producer code should use ``_fetch_home_context_outcome`` and publish only a
    safe-boundary-adopted fresh result.
    """
    global _ha_cache
    result = await _fetch_home_context_outcome(
        ha_url,
        ha_token,
        poll_interval=poll_interval,
        _cache=_cache,
        cache_dir=cache_dir,
        radio_event_rules=radio_event_rules,
        authorization=authorization,
        observed_entity_ids_callback=observed_entity_ids_callback,
    )
    if result.kind == "fresh":
        _publish_home_context_outcome(result)
    elif result.context.timestamp and cache_dir is None:
        # Preserve the legacy cache-only/failure fallback behaviour for direct
        # callers without allowing the background outcome helper to publish.
        _ha_cache = result.context
    return result.context


def get_cached_home_context(
    cache_dir: Path | None = None,
    *,
    authorization: HomeAuthorization | None = None,
) -> HomeContext | None:
    """Return the module-level HA context cache for admin-triggered refreshes.

    The module cache is only refreshed by fetch_home_context()'s own poll
    cycle — a caller reading it directly between polls could otherwise see a
    just-muted entity that hasn't been purged from it yet (adversarial
    review). Pass ``cache_dir`` to receive a live-policy-filtered copy; omit it
    for callers that don't consume entity content.
    """
    active_authorization = authorization or HomeAuthorization.narrow()
    if _ha_cache is None or _ha_cache.authorization_mode != active_authorization.mode.value:
        return None
    if cache_dir is not None:
        return apply_entity_mute_policy(_ha_cache, cache_dir)
    return _ha_cache


_last_ha_push: float = 0.0  # debounce: skip playing pushes < 2s apart
_last_ha_stop_push: float = 0.0  # debounce: skip consecutive stopped pushes < 2s apart
_ha_push_lock: asyncio.Lock | None = None
_HA_ENTITY_RECOVERY_REPUBLISH_SECONDS = 300.0
_HA_DEDUPED_ENTITY_IDS = {
    "sensor.mammamiradio_segment_type",
    "sensor.mammamiradio_listeners",
    "binary_sensor.mammamiradio_on_air",
}
_ha_entity_payload_fingerprints: dict[str, str] = {}
_ha_entity_last_push_at: dict[str, float] = {}

# Absolute fallback logo for the HA media_player entity_picture. HA's
# media-control card does NOT clear a removed entity_picture — it keeps the last
# cover — so a voice/ad/idle segment must actively push an image or the previous
# track's art lingers. Must be absolute (HA resolves it against its own origin).
# Overridable per station via [brand] artwork_url in radio.toml.
_DEFAULT_STATION_ARTWORK_URL = (
    "https://raw.githubusercontent.com/florianhorner/mammamiradio/main/ha-addon/mammamiradio/logo.png"
)
_HA_SEGMENT_TYPE_ICONS = {
    "music": "mdi:music-note",
    "banter": "mdi:microphone",
    "ad": "mdi:bullhorn",
    "news_flash": "mdi:newspaper",
    "station_id": "mdi:radio-tower",
    "sweeper": "mdi:waveform",
    "time_check": "mdi:clock-outline",
    "off": "mdi:power-standby",
}
_HA_SEGMENT_TYPE_FALLBACK_ICON = "mdi:radio"


def _segment_type_icon(segment_type: object) -> str:
    """Return the HA icon for a pushed segment-type sensor state."""
    key = str(segment_type or "").strip().lower()
    return _HA_SEGMENT_TYPE_ICONS.get(key, _HA_SEGMENT_TYPE_FALLBACK_ICON)


def _get_ha_push_lock() -> asyncio.Lock:
    """Return a process-local lock so concurrent HA pushes cannot overwrite newer state."""
    global _ha_push_lock
    if _ha_push_lock is None:
        _ha_push_lock = asyncio.Lock()
    return _ha_push_lock


_GHOST_MEDIA_PLAYER_EID = "media_player.mammamiradio"
_media_player_ghost_purged = False


def _ha_payload_fingerprint(payload: dict) -> str:
    """Stable payload fingerprint for unchanged HA sensor writes."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _ha_entity_write_due(eid: str, payload: dict, now: float) -> bool:
    """Return whether this entity should be written to HA this cycle.

    The media player is intentionally not deduped: its playback position changes
    over time and add-on-only installs expect a regular card refresh. The
    auxiliary sensors are much cheaper to recover from a slower forced heartbeat,
    so unchanged payloads wait for the recovery interval instead of writing every
    30 seconds forever.
    """
    if eid not in _HA_DEDUPED_ENTITY_IDS:
        return True
    fingerprint = _ha_payload_fingerprint(payload)
    if _ha_entity_payload_fingerprints.get(eid) != fingerprint:
        return True
    last_push = _ha_entity_last_push_at.get(eid, 0.0)
    return now - last_push >= _HA_ENTITY_RECOVERY_REPUBLISH_SECONDS


def _remember_ha_entity_write(eid: str, payload: dict, now: float) -> None:
    if eid not in _HA_DEDUPED_ENTITY_IDS:
        return
    _ha_entity_payload_fingerprints[eid] = _ha_payload_fingerprint(payload)
    _ha_entity_last_push_at[eid] = now


def _media_player_push_enabled() -> bool:
    """Whether to push ``media_player.mammamiradio`` (default on).

    Operators who install the HACS ``mammamiradio`` integration set the add-on's
    ``ha_media_player_push`` option to false (-> ``MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH``).
    The registered ``MediaPlayerEntity`` then owns the id; a 30s REST push to the
    same id would clobber it (the HA state machine is last-writer-wins) and flap
    the card between real and ghost state. The three sensor/binary_sensor pushes
    have no registered backing and keep flowing regardless.
    """
    val = os.getenv("MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH", "").strip().lower()
    return val not in ("0", "false", "no", "off")


async def _purge_ghost_media_player(base_url: str, headers: dict, client: httpx.AsyncClient) -> None:
    """Delete the stale ghost ``media_player.mammamiradio`` once.

    REST ``/api/states`` entries never expire, so when the push is turned off the
    last-pushed ghost would linger forever. Deleting it once frees the id so the
    registered integration entity claims it cleanly (no ``_2`` suffix) and no dead
    ghost is left on the prime id. Idempotent (a 404 = already gone is success);
    best-effort — a failed delete is retried on the next push.
    """
    global _media_player_ghost_purged
    if _media_player_ghost_purged:
        return
    _media_player_ghost_purged = True
    try:
        await client.delete(
            f"{base_url}/api/states/{_GHOST_MEDIA_PLAYER_EID}",
            headers=headers,
            timeout=5.0,
        )
    except Exception as e:
        _media_player_ghost_purged = False  # allow a retry on the next push
        logger.warning("HA ghost media_player purge failed: %s: %r", type(e).__name__, e)


async def push_state_to_ha(
    ha_url: str,
    ha_token: str,
    now_streaming: dict,
    current_track: object | None,
    listeners_active: int,
    session_stopped: bool,
    queue_depth: int = 0,
    station_name: str = DEFAULT_STATION_NAME,
    artwork_url: str = "",
) -> None:
    """Push radio state to HA as media_player + sensor entities. Fire-and-forget.

    ``station_name`` is the listener-facing name used for media_player/sensor
    friendly names and the station ``media_artist``. Entity IDs and the
    ``mammamiradio_*`` custom attributes stay unchanged for compatibility.

    ``artwork_url`` is the absolute station-logo URL used for ``entity_picture``
    whenever the current segment has no real cover (voice/ad/idle); blank falls
    back to ``_DEFAULT_STATION_ARTWORK_URL``.
    """
    global _last_ha_push, _last_ha_stop_push

    # Floor to the canonical name so a blank value can never reach an HA label.
    station_name = station_name or DEFAULT_STATION_NAME

    async with _get_ha_push_lock():
        now = time.time()
        if not session_stopped and now - _last_ha_push < 2.0:
            return
        if session_stopped and now - _last_ha_stop_push < 2.0:
            return
        if session_stopped:
            _last_ha_stop_push = now
        else:
            _last_ha_push = now

        headers = {"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"}
        base_url = ha_url.rstrip("/")
        client = _get_ha_client()

        segment_type = "off" if session_stopped else (now_streaming.get("type", "off") if now_streaming else "off")
        metadata = now_streaming.get("metadata", {}) if now_streaming else {}
        if not isinstance(metadata, dict):
            metadata = {}
        is_playing = not session_stopped and bool(now_streaming)
        mp_state = "playing" if is_playing else "idle"

        if segment_type == "music":
            media_title = (
                metadata.get("title_only")
                or metadata.get("title")
                or getattr(current_track, "title", None)
                or now_streaming.get("label", "")
            )
            # Illusion guard: never let a foreign "Radio X" station name reach the
            # HA card as a song's artist/title (e.g. a rescue segment built from a
            # poisoned norm-cache sidecar). Strip it and fall back; our own name is
            # the right last resort for the artist, but not a competitor's.
            media_artist = (
                strip_foreign_station_name(metadata.get("artist"), station_name)
                or strip_foreign_station_name(getattr(current_track, "artist", None), station_name)
                or station_name
            )
            # Title uses prefix-only mode: strip a rescue display prefix
            # ("Radio X - Song" -> "Song") but never blank a real song that is
            # genuinely titled "Radio Ga Ga" / "Radio Free Europe".
            media_title = strip_foreign_station_name(media_title, station_name, prefix_only=True)
        else:
            media_title = metadata.get("title") or (now_streaming.get("label", "") if now_streaming else "")
            media_artist = station_name

        started = (now_streaming.get("started", now) if now_streaming else now) or now
        media_position = max(0.0, now - started)

        media_attrs: dict = {
            "friendly_name": station_name,
            "icon": "mdi:radio",
            "supported_features": 0,
            "media_title": media_title,
            "media_artist": media_artist,
            "media_content_type": "music" if segment_type == "music" else "channel",
            "mammamiradio_segment_type": segment_type,
            "mammamiradio_queue_depth": queue_depth,
            "mammamiradio_listeners": listeners_active,
        }
        if is_playing:
            media_attrs["media_position"] = media_position
            media_attrs["media_position_updated_at"] = datetime.datetime.now(datetime.UTC).isoformat()

        # Secondary artwork surface: the HA frontend reads entity_picture directly.
        # ALWAYS set an absolute http(s) image — the real cover for a music track,
        # the station logo for everything else (voice/ad/idle, or music with no
        # cover). HA's media-control card does NOT clear a removed entity_picture;
        # it keeps the last cover, so omitting it leaves the previous track's art
        # on screen during a news flash. A relative/local path is never used (HA
        # resolves it against its own origin, which 404s for an add-on).
        album_art = str(metadata.get("album_art") or "").strip()
        cover = album_art if (is_playing and is_absolute_http_url(album_art)) else ""
        media_attrs["entity_picture"] = cover or artwork_url or _DEFAULT_STATION_ARTWORK_URL

        entities: list[tuple[str, dict]] = [
            (
                "media_player.mammamiradio",
                {
                    "state": mp_state,
                    "attributes": media_attrs,
                },
            ),
            (
                "sensor.mammamiradio_segment_type",
                {
                    "state": segment_type,
                    "attributes": {
                        "friendly_name": f"{station_name} Segment Type",
                        "icon": _segment_type_icon(segment_type),
                    },
                },
            ),
            (
                "sensor.mammamiradio_listeners",
                {
                    "state": listeners_active,
                    "attributes": {
                        "friendly_name": f"{station_name} Listeners",
                        "icon": "mdi:account-group",
                        "unit_of_measurement": "listeners",
                    },
                },
            ),
            (
                "binary_sensor.mammamiradio_on_air",
                {
                    "state": "off" if session_stopped else "on",
                    "attributes": {
                        "friendly_name": f"{station_name} On Air",
                        "icon": "mdi:broadcast",
                    },
                },
            ),
        ]

        # When the HACS integration owns media_player.mammamiradio, stop pushing
        # it (last-writer-wins would clobber the real entity) and purge the stale
        # ghost once. The sensors/binary_sensor keep flowing — no registered
        # backing, no collision, and the integration doesn't provide them.
        if not _media_player_push_enabled():
            entities = [e for e in entities if e[0] != _GHOST_MEDIA_PLAYER_EID]
            await _purge_ghost_media_player(base_url, headers, client)

        async def _push_one(eid: str, p: dict) -> bool:
            # Always log the exception TYPE + repr. A bare str() on a timeout or
            # cancellation-style exception is empty, which is what produced the
            # unreadable "HA push failed for <eid>: " lines in production. Include
            # the HTTP body on a 4xx/5xx so the operator can see *why* it failed.
            #
            # One bounded retry, transient network errors only. The whole push
            # runs inside _get_ha_push_lock(), so a newer push cannot interleave
            # and replay stale state behind this one. HTTP errors are not retried
            # (they will not fix themselves within 5s); the 30s heartbeat re-pushes.
            last_exc: httpx.TransportError | None = None
            for _attempt in range(2):
                try:
                    resp = await client.post(
                        f"{base_url}/api/states/{eid}",
                        headers=headers,
                        json=p,
                        timeout=5.0,
                    )
                    if resp.status_code >= 400:
                        raw = getattr(resp, "text", "")
                        body = raw.strip().replace("\n", " ")[:200] if isinstance(raw, str) else ""
                        logger.warning(
                            "HA push failed for %s: HTTP %d%s",
                            eid,
                            resp.status_code,
                            f" — {body}" if body else "",
                        )
                        return False
                    return True
                except httpx.TransportError as e:
                    last_exc = e
                    continue
                except Exception as e:
                    logger.warning("HA push failed for %s: %s: %r", eid, type(e).__name__, e)
                    return False
            logger.warning(
                "HA push failed for %s after retry: %s: %r",
                eid,
                type(last_exc).__name__,
                last_exc,
            )
            return False

        # Keep state writes ordered. Supervisor's API proxy can report noisy
        # request-body errors when all entity updates hit it at once during HA
        # slowness; the outer push lock already serializes push cycles, so this
        # preserves freshness while smoothing each cycle.
        for eid, payload in entities:
            if not _ha_entity_write_due(eid, payload, now):
                continue
            if await _push_one(eid, payload):
                _remember_ha_entity_write(eid, payload, now)
