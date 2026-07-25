"""Home Assistant temperature normalization helpers.

Home Assistant publishes temperatures in whatever unit the household configured,
so every value that crosses into a host prompt, the admin panel, or the narrow
privacy projection has to be converted to Celsius first.  This module is the one
place that knows how to find the unit, how to convert it, and how to render the
result as something a radio host can say out loud.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

# Home Assistant's sensor contract allows °C, °F and K for a temperature
# device class.  Anything else is a unit we cannot reason about.
_KELVIN_OFFSET = 273.15

# Display guard for text that reaches a listener or an operator.  This is a
# physical-nonsense filter, not a household-ambient filter: a Pi's CPU sensor at
# 78 °C, a boiler flow at 85 °C and an oven probe are all legitimately
# ``device_class: temperature``, and withholding them would delete the entity
# from the projection entirely.  The narrower "is this a room" judgement belongs
# to the subtractive authorities, which keep their own tighter bounds
# (``context_director`` -90..70, ``authorization`` -80..60).
PLAUSIBLE_CELSIUS_MIN = -273.15
PLAUSIBLE_CELSIUS_MAX = 1000.0


def temperature_unit_of(attrs: object) -> object | None:
    """Return the temperature unit an entity's attributes advertise.

    ``weather.*`` entities publish ``temperature_unit``; ``sensor.*`` entities
    publish ``unit_of_measurement``.  Checking both in that order is the rule
    every caller needs, so it lives here instead of being copied per module.
    """

    if not isinstance(attrs, Mapping):
        return None
    return attrs.get("temperature_unit") or attrs.get("unit_of_measurement")


def normalize_temperature(
    value: object,
    unit: object = None,
    *,
    require_unit: bool = False,
) -> float | None:
    """Return a finite Home Assistant temperature in degrees Celsius.

    Home Assistant may expose temperatures as Celsius, Fahrenheit or Kelvin,
    using values such as ``"°C"``/``"C"``, ``"°F"``/``"F"`` and ``"K"``.  Legacy
    payloads sometimes omit the unit, so callers can retain the historical
    Celsius assumption unless they explicitly require a unit.

    Pass ``require_unit=True`` wherever guessing wrong would put a wrong number
    in front of a human: a Fahrenheit reading narrated as Celsius is a twenty
    degree lie stated as fact.
    """

    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        temperature = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(temperature):
        return None

    # Only an absent or blank unit earns the legacy Celsius default. An
    # explicitly supplied non-string (False, 0, a dict) is malformed input, not
    # a missing field, and must not be silently read as Celsius.
    if unit is None:
        normalized_unit = ""
    elif isinstance(unit, str):
        normalized_unit = unit.strip().upper().replace("°", "")
    else:
        return None
    if not normalized_unit:
        if require_unit:
            return None
        normalized_unit = "C"
    if normalized_unit not in {"C", "F", "K"}:
        return None
    if normalized_unit == "F":
        temperature = (temperature - 32.0) * 5.0 / 9.0
    elif normalized_unit == "K":
        temperature = temperature - _KELVIN_OFFSET
    return temperature


def is_plausible_celsius(value: object) -> bool:
    """Whether a normalized Celsius value is safe to show to a human."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    if not math.isfinite(value):
        return False
    return PLAUSIBLE_CELSIUS_MIN <= value <= PLAUSIBLE_CELSIUS_MAX


def format_celsius(value: object, *, suffix: str = "") -> str:
    """Render a normalized Celsius value without exposing float noise.

    Unit conversion produces repeating decimals (70 °F is 21.111… °C), and a
    host reading that aloud breaks the illusion.  Round to one decimal, drop a
    pointless ``.0``, and never emit ``-0.0``.  A non-finite value renders as
    empty rather than as the words ``inf`` or ``nan``: machine words must never
    reach a listener, even from a caller that skipped its own guard.
    """

    try:
        numeric = round(float(value), 1) + 0.0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(numeric):
        return ""
    text = f"{numeric:.0f}" if numeric.is_integer() else f"{numeric:.1f}"
    return f"{text}{suffix}"
