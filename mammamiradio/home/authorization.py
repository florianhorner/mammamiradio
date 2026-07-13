"""Install-scoped Home Assistant authorization for the R0 privacy bridge.

R0 has two deliberately coarse modes:

    pre-existing station database -> legacy context (continuity bridge)
    cold-created station database -> narrow ambient context

The later Home Profile release replaces this coarse install gate with durable
per-capability grants.  Until then, the narrow projection is the one choke point
that keeps household facts away from every downstream matcher, prompt, label
task, receipt, and status surface.  Hard mutes are applied by the caller before
this projection and remain the independent subtractive authority.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

NARROW_WEATHER_ENTITY_ID = "weather.ambient"
NARROW_DAYLIGHT_ENTITY_ID = "sun.ambient"

_WEATHER_FAMILIES = {
    "clear-night": "clear-night",
    "sunny": "sunny",
    "cloudy": "cloudy",
    "partlycloudy": "cloudy",
    "fog": "cloudy",
    "rainy": "rainy",
    "pouring": "rainy",
    "snowy-rainy": "rainy",
    "snowy": "snowy",
    "lightning": "lightning",
    "lightning-rainy": "lightning",
    "hail": "lightning",
    "exceptional": "lightning",
    "windy": "windy",
    "windy-variant": "windy",
}
_DAYLIGHT_STATES = frozenset({"above_horizon", "below_horizon"})


class HomeAuthorizationMode(StrEnum):
    """R0 runtime modes; intentionally not a user-facing setting."""

    LEGACY = "legacy"
    NARROW = "narrow"


@dataclass(frozen=True)
class AuthorizedHomeProjection:
    """Prompt-safe state projection plus private source-to-synthetic mapping."""

    states: dict[str, dict]
    # Synthetic id -> real source id.  This never reaches prompts or status; it
    # exists only so a hard mute applied while a cached snapshot is live can
    # invalidate the matching ambient basic immediately.
    ambient_sources: dict[str, str] = field(repr=False)


def expand_muted_with_ambient_sources(muted_ids: set[str], ambient_sources: Mapping[str, str]) -> set[str]:
    """Expand real-source hard mutes to their synthetic ambient projection ids.

    In narrow mode a downstream break is tagged with the synthetic ambient id
    (``weather.ambient`` / ``sun.ambient``) whose real HA source (e.g.
    ``weather.forecast_home``) an operator may mute directly.  The fetch layer
    already honors a real-source mute via ``ambient_sources``; this lets every
    other consumer of the muted set (director observation, queue purge, segment
    admission) do the same without holding the projection itself.  Returns a new
    set; never mutates the input.
    """
    expanded = set(muted_ids)
    if not muted_ids or not ambient_sources:
        return expanded
    for synthetic_id, source_id in ambient_sources.items():
        if source_id in muted_ids:
            expanded.add(synthetic_id)
    return expanded


@dataclass(frozen=True)
class HomeAuthorization:
    """Coarse R0 authorization selected from immutable install provenance."""

    mode: HomeAuthorizationMode

    @classmethod
    def legacy(cls) -> HomeAuthorization:
        return cls(HomeAuthorizationMode.LEGACY)

    @classmethod
    def narrow(cls) -> HomeAuthorization:
        return cls(HomeAuthorizationMode.NARROW)

    @property
    def allows_household_moments(self) -> bool:
        return self.mode is HomeAuthorizationMode.LEGACY

    @property
    def allows_derived_mood(self) -> bool:
        return self.mode is HomeAuthorizationMode.LEGACY

    @property
    def allows_label_generation(self) -> bool:
        return self.mode is HomeAuthorizationMode.LEGACY

    def project(self, states: Mapping[str, dict]) -> AuthorizedHomeProjection:
        """Return the only state map downstream home consumers may inspect."""
        if self.mode is HomeAuthorizationMode.LEGACY:
            return AuthorizedHomeProjection(states=dict(states), ambient_sources={})

        projected: dict[str, dict] = {}
        sources: dict[str, str] = {}

        daylight = states.get("sun.sun")
        normalized_daylight = _normalize_daylight(daylight)
        if normalized_daylight is not None:
            projected[NARROW_DAYLIGHT_ENTITY_ID] = normalized_daylight
            sources[NARROW_DAYLIGHT_ENTITY_ID] = "sun.sun"

        weather_sources = [
            (entity_id, state_data) for entity_id, state_data in states.items() if entity_id.startswith("weather.")
        ]
        # Authorization is not permission to guess: an ambiguous source set
        # yields no ambient weather until a later explicit profile selection.
        # Count source entities before validation: one usable source plus one
        # unavailable/unsupported source is still an ambiguous home.
        if len(weather_sources) == 1:
            source_id, state_data = weather_sources[0]
            normalized = _normalize_weather(state_data)
            if normalized is not None:
                projected[NARROW_WEATHER_ENTITY_ID] = normalized
                sources[NARROW_WEATHER_ENTITY_ID] = source_id

        return AuthorizedHomeProjection(states=projected, ambient_sources=sources)


def _normalize_daylight(state_data: object) -> dict | None:
    if not isinstance(state_data, dict):
        return None
    state = str(state_data.get("state") or "").strip().lower()
    if state not in _DAYLIGHT_STATES:
        return None
    return {
        "entity_id": NARROW_DAYLIGHT_ENTITY_ID,
        "state": state,
        "attributes": {},
    }


def _normalize_weather(state_data: object) -> dict | None:
    if not isinstance(state_data, dict):
        return None
    raw_state = str(state_data.get("state") or "").strip().lower()
    state = _WEATHER_FAMILIES.get(raw_state)
    if state is None:
        return None
    attrs = state_data.get("attributes")
    attrs = attrs if isinstance(attrs, dict) else {}
    temperature_c = _temperature_c(attrs.get("temperature"), attrs.get("temperature_unit"))
    if temperature_c is None:
        return None
    return {
        "entity_id": NARROW_WEATHER_ENTITY_ID,
        "state": state,
        "attributes": {"temperature": temperature_c, "temperature_unit": "°C"},
    }


def _temperature_c(value: object, unit: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        temperature = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(temperature):
        return None
    normalized_unit = str(unit or "").strip().upper().replace("°", "")
    if normalized_unit not in {"C", "F"}:
        return None
    if normalized_unit == "F":
        temperature = (temperature - 32.0) * 5.0 / 9.0
    if temperature < -80.0 or temperature > 60.0:
        return None
    # Coarse five-degree buckets preserve weather usefulness without retaining
    # a household's precise sensor reading.
    return int(round(temperature / 5.0) * 5)
