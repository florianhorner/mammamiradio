"""Home Assistant context provider for radio scripts.

Polls HA REST API for entity states and formats them as natural language
that scriptwriter can inject into Claude prompts. The hosts reference
ambient home state ~30-50% of the time, like glancing out a window.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx

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
]

BRONZE_ENTITIES = [
    # Sleep/wake times
    "input_datetime.last_sleep_time",
    "input_datetime.last_wake_time",
    # Apartment door
    "binary_sensor.buro_9_ring_intercom_klingelt",
]

ALL_ENTITIES = GOLD_ENTITIES + SILVER_ENTITIES + BRONZE_ENTITIES

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


@dataclass
class HomeContext:
    """Snapshot of interesting home state, formatted for scriptwriter."""
    raw_states: dict[str, dict] = field(default_factory=dict)
    summary: str = ""
    timestamp: float = 0.0

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp if self.timestamp else float("inf")


def _format_state(entity_id: str, state_data: dict) -> str | None:
    """Format a single entity state as a natural language line."""
    state = state_data.get("state", "unknown")
    attrs = state_data.get("attributes", {})
    label = ENTITY_LABELS.get(entity_id, attrs.get("friendly_name", entity_id))

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
        title = attrs.get("media_title", "")
        if title and state == "playing":
            artist = attrs.get("media_artist", "")
            extra = f" — {artist}: {title}" if artist else f" — {title}"
            return f"{label}: {translated}{extra}"
        return f"{label}: {translated}"

    # Dad joke — just show the joke
    if entity_id == "input_select.kaffee_dad_jokes":
        return f"{label}: \"{state}\""

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


async def fetch_home_context(
    ha_url: str,
    ha_token: str,
    poll_interval: float = 60.0,
    _cache: HomeContext | None = None,
) -> HomeContext:
    """Fetch current home state from HA REST API.

    Returns cached result if fresher than poll_interval seconds.
    """
    if _cache and _cache.age_seconds < poll_interval:
        return _cache

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{ha_url.rstrip('/')}/api/states",
                headers={
                    "Authorization": f"Bearer {ha_token}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            all_states = resp.json()

        # Filter to our curated entities
        entity_map = {e["entity_id"]: e for e in all_states}
        relevant = {
            eid: entity_map[eid]
            for eid in ALL_ENTITIES
            if eid in entity_map
        }

        summary = _build_summary(relevant)
        context = HomeContext(
            raw_states=relevant,
            summary=summary,
            timestamp=time.time(),
        )
        logger.info("Fetched HA context: %d entities", len(relevant))
        return context

    except Exception as e:
        logger.warning("Failed to fetch HA context: %s", e)
        # Return stale cache if available, otherwise empty
        if _cache:
            return _cache
        return HomeContext()
