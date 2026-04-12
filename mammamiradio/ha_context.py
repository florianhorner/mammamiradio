"""Home Assistant context provider for radio scripts.

Polls HA REST API for entity states and formats them as natural language
that scriptwriter can inject into Claude prompts. The hosts reference
ambient home state ~30-50% of the time, like glancing out a window.
"""

from __future__ import annotations

import datetime
import logging
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

from mammamiradio.ha_enrichment import (
    EVENT_BUFFER_SIZE,
    HomeEvent,
    build_events_summary,
    diff_states,
    prune_events,
)

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


# ---------------------------------------------------------------------------
# Phase 4: Reactive triggers
# ---------------------------------------------------------------------------

# (entity_id, raw_ha_trigger_state, directive_text, cooldown_seconds)
REACTIVE_TRIGGERS: list[tuple[str, str, str, int]] = [
    (
        "switch.bar_kaffeemaschine_steckdose",
        "on",
        "La macchina del caffè si è appena accesa! I conduttori sentono il profumo di espresso"
        " e lo notano brevemente — naturale, non esagerato.",
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
]

_reactive_cooldowns: dict[str, float] = {}


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

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp if self.timestamp else float("inf")


def _sanitize_state_value(value: str, max_len: int = 100) -> str:
    """Truncate and strip instruction-like patterns from HA state values."""
    value = value[:max_len]
    # Strip patterns that look like prompt injection attempts
    for pattern in ("ignore previous", "disregard", "system override", "forget your"):
        if pattern in value.lower():
            return "(filtered)"
    return value


def _format_state(entity_id: str, state_data: dict) -> str | None:
    """Format a single entity state as a natural language line."""
    state = _sanitize_state_value(state_data.get("state", "unknown"))
    attrs = state_data.get("attributes", {})
    label = ENTITY_LABELS.get(entity_id, _sanitize_state_value(attrs.get("friendly_name", entity_id)))

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


# ---------------------------------------------------------------------------
# Phase 1 helpers: event diffing
# ---------------------------------------------------------------------------


def _diff_states(
    old_states: dict[str, dict],
    new_states: dict[str, dict],
    events: deque[HomeEvent],
) -> None:
    """Detect state changes between polls and append as HomeEvent objects.

    Mutates the events deque in place. Prunes events older than 30 minutes.
    """
    now = time.time()
    for entity_id, new_data in new_states.items():
        new_state = new_data.get("state", "unknown")
        if new_state in ("unavailable", "unknown"):
            continue
        old_data = old_states.get(entity_id, {})
        old_state = old_data.get("state", "")
        if not old_state or old_state == new_state:
            continue
        if old_state in ("unavailable", "unknown"):
            continue
        label = ENTITY_LABELS.get(entity_id, entity_id)
        events.append(
            HomeEvent(
                entity_id=entity_id,
                label=label,
                old_state=STATE_TRANSLATIONS.get(old_state, old_state),
                new_state=STATE_TRANSLATIONS.get(new_state, new_state),
                timestamp=now,
            )
        )
    # Prune events older than 30 minutes (from the left — oldest first)
    cutoff = now - 1800
    while events and events[0].timestamp < cutoff:
        events.popleft()


def _build_events_summary(events: deque[HomeEvent]) -> str:
    """Build a most-recent-first summary of home events, capped at 5 lines."""
    if not events:
        return ""
    lines = [e.describe() for e in reversed(events)][:5]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase 2: Home mood classification
# ---------------------------------------------------------------------------


def classify_home_mood(states: dict[str, dict]) -> str:
    """Classify aggregate HA state into a named Italian home scene.

    Priority order — first match wins. Returns "" when no scene matches.
    """

    def _state(eid: str) -> str:
        return states.get(eid, {}).get("state", "")

    now_hour = datetime.datetime.now().hour

    if _state("vacuum.goldstaubsucher") == "cleaning" or _state("vacuum.matrix10_ultra") == "cleaning":
        return "Il robot sta pulendo"
    if _state("switch.bar_kaffeemaschine_steckdose") == "on" and 5 <= now_hour <= 10:
        return "Stanno svegliandosi"
    if _state("fan.kuche_lufter") == "on":
        return "Qualcuno sta cucinando"
    if _state("fan.bad_gross_lufter_shelly") == "on" or _state("fan.bad_klein_lufter") == "on":
        return "Qualcuno sta facendo la doccia"
    if _state("media_player.samsung_s95ca_65") == "playing" and now_hour >= 18:
        return "Serata cinema"
    if (
        _state("media_player.wohnzimmer_sonos_arc_lautsprecher") == "playing"
        or _state("media_player.esszimmer") == "playing"
    ):
        return "Musica in casa"
    if _state("input_select.bedroom_occupancy_state") == "occupied" and (now_hour >= 22 or now_hour < 8):
        return "Qualcuno sta dormendo"
    if _state("person.florian_horner") == "not_home" and _state("person.sabrina") == "not_home":
        return "Casa vuota"
    return ""


# ---------------------------------------------------------------------------
# Phase 3: Weather narrative arc
# ---------------------------------------------------------------------------

_weather_forecast_cache: str = ""
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


async def fetch_weather_forecast(ha_url: str, ha_token: str) -> str:
    """Fetch hourly weather forecast from HA and return a narrative arc string.

    Cached for 1 hour. Returns "" if HA does not support get_forecasts or on error.
    """
    global _weather_forecast_cache, _weather_forecast_fetched_at
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
        forecast_list: list[dict] = data.get("weather.forecast_home", {}).get("forecast", [])
        arc = _build_weather_arc(forecast_list)
        _weather_forecast_cache = arc
        _weather_forecast_fetched_at = time.time()
        logger.debug("Weather arc: %s", arc or "(none)")
        return arc
    except Exception as e:
        logger.debug("Weather forecast unavailable: %s", e)
        _weather_forecast_cache = ""
        _weather_forecast_fetched_at = time.time()
        return ""


# ---------------------------------------------------------------------------
# Phase 4: Reactive trigger dispatch
# ---------------------------------------------------------------------------


def check_reactive_triggers(events: deque[HomeEvent]) -> str | None:
    """Scan recent events for reactive triggers.

    Only considers events less than 2 minutes old. Respects per-trigger cooldowns.
    Returns the first matching directive text, or None.
    """
    now = time.time()
    age_cutoff = now - 120  # 2 minutes
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
    return None


_ha_client: httpx.AsyncClient | None = None
_ha_cache: HomeContext | None = None


def _get_ha_client() -> httpx.AsyncClient:
    """Return a reusable async HTTP client for HA API calls."""
    global _ha_client
    if _ha_client is None or _ha_client.is_closed:
        _ha_client = httpx.AsyncClient(timeout=10.0)
    return _ha_client


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

        # Filter to our curated entities
        entity_map = {e["entity_id"]: e for e in all_states}
        relevant = {eid: entity_map[eid] for eid in ALL_ENTITIES if eid in entity_map}

        timestamp = time.time()
        old_states = effective_cache.raw_states if effective_cache else {}
        old_events = effective_cache.events if effective_cache else None
        events = diff_states(
            old_states,
            relevant,
            old_events,
            entity_labels=ENTITY_LABELS,
            state_translations=STATE_TRANSLATIONS,
            now=timestamp,
        )
        mood = classify_home_mood(relevant)
        weather_arc = await fetch_weather_forecast(ha_url, ha_token)
        summary = _build_summary(relevant)
        events_summary = build_events_summary(events, now=timestamp)
        context = HomeContext(
            raw_states=relevant,
            summary=summary,
            events=events,
            events_summary=events_summary,
            mood=mood,
            weather_arc=weather_arc,
            timestamp=timestamp,
        )
        _ha_cache = context
        logger.info("Fetched HA context: %d entities, %d events, mood=%r", len(relevant), len(events), mood or "none")
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
