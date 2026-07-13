"""R0 install-scoped Home Assistant authorization contract."""

from __future__ import annotations

from mammamiradio.home.authorization import (
    NARROW_DAYLIGHT_ENTITY_ID,
    NARROW_WEATHER_ENTITY_ID,
    HomeAuthorization,
    HomeAuthorizationMode,
)


def _weather(entity_id: str, *, state: str = "sunny", temperature=22.4, unit: str = "°C") -> tuple[str, dict]:
    return entity_id, {
        "entity_id": entity_id,
        "state": state,
        "attributes": {
            "temperature": temperature,
            "temperature_unit": unit,
            "friendly_name": "Florian's exact weather station",
            "attribution": "private location",
            "forecast": [{"temperature": 99}],
        },
    }


def test_legacy_projection_preserves_existing_state_shape() -> None:
    states = dict(
        [
            _weather("weather.forecast_home"),
            ("person.florian", {"entity_id": "person.florian", "state": "home", "attributes": {}}),
        ]
    )

    authorization = HomeAuthorization.legacy()
    projection = authorization.project(states)

    assert authorization.mode is HomeAuthorizationMode.LEGACY
    assert projection.states == states
    assert projection.ambient_sources == {}
    assert authorization.allows_household_moments is True
    assert authorization.allows_derived_mood is True
    assert authorization.allows_label_generation is True


def test_narrow_projection_keeps_only_synthetic_normalized_ambient_basics() -> None:
    states = dict(
        [
            _weather("weather.local", temperature=72, unit="°F"),
            ("sun.sun", {"entity_id": "sun.sun", "state": "above_horizon", "attributes": {"azimuth": 187}}),
            ("person.florian", {"entity_id": "person.florian", "state": "home", "attributes": {}}),
            ("climate.bedroom", {"entity_id": "climate.bedroom", "state": "heat", "attributes": {}}),
            ("vacuum.secret", {"entity_id": "vacuum.secret", "state": "cleaning", "attributes": {}}),
        ]
    )

    authorization = HomeAuthorization.narrow()
    projection = authorization.project(states)

    assert authorization.mode is HomeAuthorizationMode.NARROW
    assert set(projection.states) == {NARROW_WEATHER_ENTITY_ID, NARROW_DAYLIGHT_ENTITY_ID}
    assert projection.states[NARROW_WEATHER_ENTITY_ID] == {
        "entity_id": NARROW_WEATHER_ENTITY_ID,
        "state": "sunny",
        "attributes": {"temperature": 20, "temperature_unit": "°C"},
    }
    assert projection.states[NARROW_DAYLIGHT_ENTITY_ID] == {
        "entity_id": NARROW_DAYLIGHT_ENTITY_ID,
        "state": "above_horizon",
        "attributes": {},
    }
    assert projection.ambient_sources == {
        NARROW_WEATHER_ENTITY_ID: "weather.local",
        NARROW_DAYLIGHT_ENTITY_ID: "sun.sun",
    }
    assert "Florian" not in repr(projection)
    assert "private location" not in repr(projection)
    assert "weather.local" not in repr(projection)
    assert authorization.allows_household_moments is False
    assert authorization.allows_derived_mood is False
    assert authorization.allows_label_generation is False


def test_narrow_projection_refuses_to_guess_between_weather_sources() -> None:
    states = dict([_weather("weather.one"), _weather("weather.two")])

    projection = HomeAuthorization.narrow().project(states)

    assert projection.states == {}
    assert projection.ambient_sources == {}


def test_narrow_projection_treats_valid_plus_invalid_weather_as_ambiguous() -> None:
    valid_id, valid = _weather("weather.good")
    projection = HomeAuthorization.narrow().project(
        {
            valid_id: valid,
            "weather.unavailable": {
                "entity_id": "weather.unavailable",
                "state": "unavailable",
                "attributes": {},
            },
        }
    )

    assert projection.states == {}
    assert projection.ambient_sources == {}


def test_narrow_projection_drops_unavailable_or_malformed_ambient_inputs() -> None:
    states = {
        "weather.local": {
            "entity_id": "weather.local",
            "state": "unavailable",
            "attributes": {"temperature": 21},
        },
        "sun.sun": {"entity_id": "sun.sun", "state": "unknown", "attributes": {}},
    }

    assert HomeAuthorization.narrow().project(states).states == {}


def test_narrow_projection_drops_weather_when_temperature_is_not_safe() -> None:
    entity_id, state = _weather("weather.local", temperature="nan")

    projection = HomeAuthorization.narrow().project({entity_id: state})

    assert projection.states == {}


def test_narrow_projection_coarsens_condition_family_and_requires_explicit_unit() -> None:
    rainy_id, rainy = _weather("weather.local", state="pouring", temperature=17.0)
    no_unit_id, no_unit = _weather("weather.no_unit", temperature=17.0, unit="")

    rainy_projection = HomeAuthorization.narrow().project({rainy_id: rainy})
    no_unit_projection = HomeAuthorization.narrow().project({no_unit_id: no_unit})

    assert rainy_projection.states[NARROW_WEATHER_ENTITY_ID]["state"] == "rainy"
    assert rainy_projection.states[NARROW_WEATHER_ENTITY_ID]["attributes"]["temperature"] == 15
    assert no_unit_projection.states == {}
