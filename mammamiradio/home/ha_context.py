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
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TypedDict
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect as websocket_connect

from mammamiradio.core.config import DEFAULT_STATION_NAME, TimerInterruptConfig
from mammamiradio.core.models import InterruptSpec, ScoredEntityStatus
from mammamiradio.home.ha_enrichment import (
    EVENT_BUFFER_SIZE,
    HomeEvent,
    build_events_summary,
    build_events_summary_en,
    diff_states,
    prune_events,
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

# Italian-friendly labels for entity states
ENTITY_LABELS = {
    "switch.bar_kaffeemaschine_steckdose": "La macchina del caffè",
    "input_select.kaffee_dad_jokes": "Dad joke del caffè",
    "vacuum.goldstaubsucher": "Robot aspirapolvere Goldstaubsucher",
    "vacuum.matrix10_ultra": "Robot aspirapolvere Matrix10 Ultra",
    "weather.forecast_home": "Meteo al PentFLOuse",
    "person.florian_horner": "Florian",
    "person.sabrina": "Sabrina",
    "person.schnuffi": "Schnuffi",
    "lock.lock_ultra_8d3c": "Serratura porta d'ingresso",
    "input_button.foyer_fahrstuhl_fingerbot_push_button": "Ascensore (ultimo utilizzo)",
    "binary_sensor.8_stockwerk_group_sensor_wohnzimmer_esszimmer_bar": (
        "Presenza nel soggiorno/sala da pranzo/bar/cucina"
    ),
    "input_select.bedroom_occupancy_state": "Camera da letto",
    "switch.bad_gross_waschmaschine_steckdose": "Lavatrice",
    "media_player.samsung_s95ca_65": "Televisore Samsung",
    "media_player.wohnzimmer_sonos_arc_lautsprecher": "Sonos Arc soggiorno",
    "media_player.esszimmer": "Sonos sala da pranzo",
    "climate.wohnzimmer_tado_heizung": "Riscaldamento soggiorno",
    "climate.schlafzimmer": "Riscaldamento camera da letto",
    "sun.sun": "Sole",
    "fan.bad_gross_lufter_shelly": "Ventilatore bagno grande",
    "fan.bad_klein_lufter": "Ventilatore bagno piccolo",
    "fan.kuche_lufter": "Ventilatore cucina",
    "input_datetime.last_sleep_time": "Ultimo orario di sonno",
    "input_datetime.last_wake_time": "Ultimo orario di sveglia",
    "binary_sensor.buro_9_ring_intercom_klingelt": "Citofono",
    # Room-level lights
    "light.magic_areas_light_groups_wohnzimmer_all_lights": "Luci soggiorno",
    "light.magic_areas_light_groups_schlafzimmer_all_lights": "Luci camera da letto",
    "light.magic_areas_light_groups_kuche_all_lights": "Luci cucina",
    "light.magic_areas_light_groups_esszimmer_all_lights": "Luci sala da pranzo",
    # Power sensors
    "sensor.bar_bali_boot_steckdose_power": "Lavatrice (consumo)",
    "sensor.kuche_kaffeemaschine_steckdose_power": "Caffettiera (consumo)",
    # Atmosphere
    "light.schlafzimmer_sternenlicht_projektor_2": "Proiettore stelle camera",
    "light.kleiderschrank_sternenlicht_projektor": "Proiettore stelle guardaroba",
    "light.terrasse_9_outdoor_lichtschlauch": "Luci terrazza",
    # Household power
    "sensor.haushalt_stromverbrauch_gesamt": "Consumo elettrico totale",
}

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


# English entity labels for admin UI display (parallel to ENTITY_LABELS)
ENTITY_LABELS_EN: dict[str, str] = {
    "switch.bar_kaffeemaschine_steckdose": "Coffee machine",
    "input_select.kaffee_dad_jokes": "Coffee dad joke",
    "vacuum.goldstaubsucher": "Robot vacuum Goldstaubsucher",
    "vacuum.matrix10_ultra": "Robot vacuum Matrix10 Ultra",
    "weather.forecast_home": "Weather at PentFLOuse",
    "person.florian_horner": "Florian",
    "person.sabrina": "Sabrina",
    "person.schnuffi": "Schnuffi",
    "lock.lock_ultra_8d3c": "Front door lock",
    "input_button.foyer_fahrstuhl_fingerbot_push_button": "Elevator (last used)",
    "binary_sensor.8_stockwerk_group_sensor_wohnzimmer_esszimmer_bar": "Living room/dining/bar presence",
    "input_select.bedroom_occupancy_state": "Bedroom",
    "switch.bad_gross_waschmaschine_steckdose": "Washing machine",
    "media_player.samsung_s95ca_65": "Samsung TV",
    "media_player.wohnzimmer_sonos_arc_lautsprecher": "Sonos Arc living room",
    "media_player.esszimmer": "Sonos dining room",
    "climate.wohnzimmer_tado_heizung": "Heating (living room)",
    "climate.schlafzimmer": "Heating (bedroom)",
    "sun.sun": "Sun",
    "fan.bad_gross_lufter_shelly": "Large bathroom fan",
    "fan.bad_klein_lufter": "Small bathroom fan",
    "fan.kuche_lufter": "Kitchen fan",
    "input_datetime.last_sleep_time": "Last sleep time",
    "input_datetime.last_wake_time": "Last wake time",
    "binary_sensor.buro_9_ring_intercom_klingelt": "Intercom",
    "light.magic_areas_light_groups_wohnzimmer_all_lights": "Living room lights",
    "light.magic_areas_light_groups_schlafzimmer_all_lights": "Bedroom lights",
    "light.magic_areas_light_groups_kuche_all_lights": "Kitchen lights",
    "light.magic_areas_light_groups_esszimmer_all_lights": "Dining room lights",
    "sensor.bar_bali_boot_steckdose_power": "Washing machine (power)",
    "sensor.kuche_kaffeemaschine_steckdose_power": "Coffee machine (power)",
    "light.schlafzimmer_sternenlicht_projektor_2": "Star projector (bedroom)",
    "light.kleiderschrank_sternenlicht_projektor": "Star projector (wardrobe)",
    "light.terrasse_9_outdoor_lichtschlauch": "Terrace lights",
    "sensor.haushalt_stromverbrauch_gesamt": "Total household power",
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
            "summary": self.summary_line,
            "device_class": attrs.get("device_class"),
        }


@dataclass
class HomeContext:
    """Snapshot of interesting home state, formatted for scriptwriter."""

    raw_states: dict[str, dict] = field(default_factory=dict)
    summary: str = ""
    events: deque[HomeEvent] = field(default_factory=lambda: deque(maxlen=EVENT_BUFFER_SIZE))
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
    denylist_hits: dict[str, int] = field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp if self.timestamp else float("inf")


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


def _generic_label(entity_id: str, state_data: dict) -> str:
    attrs = state_data.get("attributes", {})
    friendly = attrs.get("friendly_name")
    if friendly:
        label = _sanitize_state_value(str(friendly), max_len=80)
    else:
        label = entity_id.split(".", 1)[-1].replace("_", " ").strip() or entity_id
    area = _area_from_attrs(attrs)
    if area and area.lower() not in label.lower():
        return f"{label} ({area})"
    return label


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


def _format_state(entity_id: str, state_data: dict) -> str | None:
    """Format a single entity state as a natural language line."""
    state = _sanitize_state_value(state_data.get("state", "unknown"))
    attrs = state_data.get("attributes", {})
    curated = ENTITY_LABELS.get(entity_id)
    if curated is None:
        friendly = attrs.get("friendly_name")
        if not friendly:
            # Anti-illusion guard: raw entity IDs never reach the host. Until a
            # Phase B catalog label is available, drop the entity from the slice.
            return None
        label = _sanitize_state_value(str(friendly))
    else:
        label = curated

    if state in ("unavailable", "unknown"):
        return None

    # Weather gets special treatment — include temperature and condition
    if entity_id == "weather.forecast_home":
        temp = attrs.get("temperature", "?")
        unit = attrs.get("temperature_unit", "°C")
        condition = STATE_TRANSLATIONS.get(state, state)
        return f"{label}: {condition}, {temp}{unit}"

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
) -> list[ScoredEntity]:
    """Score filtered HA entities and return the budgeted prompt slice."""
    ref_now = time.time() if now is None else now
    event_ids = event_entity_ids or set()
    scored: list[ScoredEntity] = []
    for entity_id, state_data in states.items():
        line = _format_state(entity_id, state_data)
        if not line:
            continue
        label_it = ENTITY_LABELS.get(entity_id, _generic_label(entity_id, state_data))
        label_en = ENTITY_LABELS_EN.get(entity_id, _generic_label(entity_id, state_data))
        scored.append(
            ScoredEntity(
                entity_id=entity_id,
                area=_area_from_attrs(state_data.get("attributes", {}) or {}),
                domain=_entity_domain(entity_id),
                score=_score_entity(entity_id, state_data, event_entity_ids=event_ids, now=ref_now),
                raw_state=state_data,
                label_it=label_it,
                label_en=label_en,
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


def _build_entity_label_maps(states: dict[str, dict]) -> tuple[dict[str, str], dict[str, str]]:
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
        if not state_data.get("attributes", {}).get("friendly_name"):
            continue
        labels_it.setdefault(entity_id, _generic_label(entity_id, state_data))
        labels_en.setdefault(entity_id, _generic_label(entity_id, state_data))
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
_ha_registry_area_cache: dict[str, str] | None = None
_ha_registry_fetched_at: float = 0.0
_HA_REGISTRY_TTL = 6 * 60 * 60
_HA_REGISTRY_FAILURE_TTL = 60


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
    """Fetch HA entity/device/area registries once and map entity_id -> area name."""
    global _ha_registry_area_cache, _ha_registry_fetched_at
    now = time.time()
    if _ha_registry_area_cache is not None and now - _ha_registry_fetched_at < _HA_REGISTRY_TTL:
        return _ha_registry_area_cache

    try:
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
        devices = {
            str(device.get("id")): str(device.get("area_id"))
            for device in results.get(2, [])
            if device.get("id") and device.get("area_id")
        }
        entity_areas: dict[str, str] = {}
        for entity in results.get(1, []):
            entity_id = entity.get("entity_id")
            if not entity_id:
                continue
            area_id = entity.get("area_id") or devices.get(str(entity.get("device_id")))
            area_name = areas.get(str(area_id)) if area_id else None
            if area_name:
                entity_areas[str(entity_id)] = area_name

        _ha_registry_area_cache = entity_areas
        _ha_registry_fetched_at = now
        logger.debug("Fetched HA registries: %d entity areas", len(entity_areas))
        return entity_areas
    except Exception as exc:
        logger.debug("HA registry fetch unavailable: %s", exc)
        _ha_registry_area_cache = {}
        _ha_registry_fetched_at = now - _HA_REGISTRY_TTL + _HA_REGISTRY_FAILURE_TTL
        return {}


def _apply_registry_area(entity_id: str, state_data: dict, registry_areas: dict[str, str]) -> dict:
    area = registry_areas.get(entity_id)
    if not area:
        return state_data
    attrs = dict(state_data.get("attributes", {}) or {})
    if attrs.get("area") or attrs.get("area_name") or attrs.get("area_id"):
        return state_data
    enriched = dict(state_data)
    attrs["area"] = area
    enriched["attributes"] = attrs
    return enriched


async def fetch_home_context(
    ha_url: str,
    ha_token: str,
    poll_interval: float = 60.0,
    _cache: HomeContext | None = None,
) -> HomeContext:
    """Fetch current home state from HA REST API.

    Returns cached result if fresher than poll_interval seconds.
    Uses module-level cache for persistence across calls, with the
    _cache parameter as a fallback for backward compatibility.
    """
    global _ha_cache
    # Prefer explicitly passed cache, then module-level cache
    effective_cache = _cache or _ha_cache
    if effective_cache and effective_cache.age_seconds < poll_interval:
        # Refresh event ages and prune expired entries even on cache hits, so
        # "X min fa" timestamps stay accurate and stale events are dropped.
        now = time.time()
        effective_cache.events = prune_events(effective_cache.events, now=now)
        effective_cache.events_summary = build_events_summary(effective_cache.events, now=now)
        effective_cache.events_summary_en = build_events_summary_en(
            effective_cache.events, ENTITY_LABELS_EN, STATE_TRANSLATIONS_EN, now=now
        )
        return effective_cache

    try:
        client = _get_ha_client()
        resp = await client.get(
            f"{ha_url.rstrip('/')}/api/states",
            headers={
                "Authorization": f"Bearer {ha_token}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        all_states = resp.json()

        timestamp = time.time()
        denylist_hits: dict[str, int] = {}
        registry_areas = await _fetch_ha_registry_areas(ha_url, ha_token)
        entity_map = {str(e.get("entity_id", "")): e for e in all_states if e.get("entity_id")}
        relevant = {
            entity_id: filtered
            for entity_id, state_data in entity_map.items()
            if (
                filtered := _filter_state(
                    entity_id,
                    _apply_registry_area(entity_id, state_data, registry_areas),
                    denylist_hits,
                )
            )
            is not None
        }
        old_states = effective_cache.raw_states if effective_cache else {}
        old_events = effective_cache.events if effective_cache else None
        labels_it, labels_en = _build_entity_label_maps(relevant)
        events = diff_states(
            old_states,
            relevant,
            old_events,
            entity_labels=labels_it,
            state_translations=STATE_TRANSLATIONS,
            now=timestamp,
        )
        scored = _build_scored_entities(
            relevant,
            event_entity_ids={event.entity_id for event in events},
            now=timestamp,
        )
        mood = classify_home_mood(relevant)
        mood_en = classify_home_mood_en(relevant)
        weather_arc = await fetch_weather_forecast(ha_url, ha_token)
        summary = _build_budgeted_summary(scored)
        events_summary = build_events_summary(events, now=timestamp)
        events_summary_en = build_events_summary_en(events, labels_en, STATE_TRANSLATIONS_EN, now=timestamp)
        # Determine English label of the most recent event for admin display
        last_event_label_en = ""
        if events:
            newest = max(events, key=lambda e: e.timestamp)
            last_event_label_en = labels_en.get(newest.entity_id, newest.label)
        context = HomeContext(
            raw_states=relevant,
            summary=summary,
            events=events,
            events_summary=events_summary,
            mood=mood,
            weather_arc=weather_arc,
            timestamp=timestamp,
            mood_en=mood_en,
            weather_arc_en=get_weather_arc_en(),
            events_summary_en=events_summary_en,
            last_event_label_en=last_event_label_en,
            scored=scored,
            denylist_hits=denylist_hits,
        )
        _ha_cache = context
        logger.info(
            "Fetched HA context: %d/%d entities, %d scored, %d events, mood=%r",
            len(relevant),
            len(entity_map),
            len(scored),
            len(events),
            mood or "none",
        )
        return context

    except Exception as e:
        logger.warning("Failed to fetch HA context: %s", e)
        # Return stale cache if available, otherwise empty
        if effective_cache:
            timestamp = time.time()
            effective_cache.events = prune_events(effective_cache.events, now=timestamp)
            effective_cache.events_summary = build_events_summary(effective_cache.events, now=timestamp)
            return effective_cache
        return HomeContext()


_last_ha_push: float = 0.0  # debounce: skip playing pushes < 2s apart
_last_ha_stop_push: float = 0.0  # debounce: skip consecutive stopped pushes < 2s apart
_ha_push_lock: asyncio.Lock | None = None


def _get_ha_push_lock() -> asyncio.Lock:
    """Return a process-local lock so concurrent HA pushes cannot overwrite newer state."""
    global _ha_push_lock
    if _ha_push_lock is None:
        _ha_push_lock = asyncio.Lock()
    return _ha_push_lock


async def push_state_to_ha(
    ha_url: str,
    ha_token: str,
    now_streaming: dict,
    current_track: object | None,
    listeners_active: int,
    session_stopped: bool,
    queue_depth: int = 0,
    station_name: str = DEFAULT_STATION_NAME,
) -> None:
    """Push radio state to HA as media_player + sensor entities. Fire-and-forget.

    ``station_name`` is the listener-facing name used for media_player/sensor
    friendly names and the station ``media_artist``. Entity IDs and the
    ``mammamiradio_*`` custom attributes stay unchanged for compatibility.
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
        # Only set it while actually playing, and only for an absolute http(s) cover
        # URL — never a relative/local path (HA resolves relative entity_picture
        # against its own origin, which 404s for an add-on). When absent, leave it
        # unset so HA shows its clean default icon instead of a stale or broken tile.
        album_art = str(metadata.get("album_art") or "").strip()
        if is_playing and album_art.startswith(("http://", "https://")):
            media_attrs["entity_picture"] = album_art

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
                    "attributes": {"friendly_name": f"{station_name} Segment Type"},
                },
            ),
            (
                "sensor.mammamiradio_listeners",
                {
                    "state": listeners_active,
                    "attributes": {
                        "friendly_name": f"{station_name} Listeners",
                        "unit_of_measurement": "listeners",
                    },
                },
            ),
            (
                "binary_sensor.mammamiradio_on_air",
                {
                    "state": "off" if session_stopped else "on",
                    "attributes": {"friendly_name": f"{station_name} On Air"},
                },
            ),
        ]

        async def _push_one(eid: str, p: dict) -> None:
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
                    return
                except httpx.TransportError as e:
                    last_exc = e
                    continue
                except Exception as e:
                    logger.warning("HA push failed for %s: %s: %r", eid, type(e).__name__, e)
                    return
            logger.warning(
                "HA push failed for %s after retry: %s: %r",
                eid,
                type(last_exc).__name__,
                last_exc,
            )

        await asyncio.gather(*(_push_one(eid, p) for eid, p in entities))
