"""Tests for Home Assistant temperature normalization."""

from __future__ import annotations

import pytest

from mammamiradio.home.temperature import (
    format_celsius,
    is_plausible_celsius,
    normalize_temperature,
    temperature_unit_of,
)


def test_normalize_temperature_converts_fahrenheit_to_celsius():
    assert normalize_temperature(41, "°F") == 5.0
    assert normalize_temperature("59", "F") == 15.0


def test_normalize_temperature_converts_kelvin_to_celsius():
    # Home Assistant's sensor contract allows K alongside °C and °F.
    assert normalize_temperature(294.15, "K") == pytest.approx(21.0)
    assert normalize_temperature("273.15", "K") == pytest.approx(0.0)


def test_normalize_temperature_preserves_celsius_and_defaults_legacy_missing_unit_to_celsius():
    assert normalize_temperature(5, "°C") == 5.0
    assert normalize_temperature(5) == 5.0


def test_normalize_temperature_can_require_an_explicit_unit():
    assert normalize_temperature(5, None, require_unit=True) is None
    assert normalize_temperature(5, "   ", require_unit=True) is None
    assert normalize_temperature(5, "%") is None


@pytest.mark.parametrize(
    "value",
    [True, False, None, [], {}, object(), "abc", "", float("nan"), float("inf"), float("-inf")],
)
def test_normalize_temperature_rejects_non_numeric_and_non_finite(value):
    assert normalize_temperature(value, "°C") is None


def test_normalize_temperature_does_not_round_the_conversion():
    # The helper stays exact; rounding is the formatter's job.
    assert normalize_temperature(70, "°F") == pytest.approx(21.111111, abs=1e-6)


@pytest.mark.parametrize(
    ("attrs", "expected"),
    [
        ({"temperature_unit": "°F"}, "°F"),
        ({"unit_of_measurement": "°F"}, "°F"),
        # temperature_unit wins: weather entities carry both meaningfully.
        ({"temperature_unit": "°C", "unit_of_measurement": "°F"}, "°C"),
        ({}, None),
        ({"temperature_unit": ""}, None),
        (None, None),
        ("not-a-mapping", None),
    ],
)
def test_temperature_unit_of_reads_both_home_assistant_conventions(attrs, expected):
    assert temperature_unit_of(attrs) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (21.111111111, "21.1"),
        (20.0, "20"),
        (5.0, "5"),
        (-0.0, "0"),
        # Every Fahrenheit reading just under freezing used to render "-0.0".
        (normalize_temperature(31.98, "°F"), "0"),
        (-3.25, "-3.2"),
    ],
)
def test_format_celsius_never_exposes_float_noise(value, expected):
    assert format_celsius(value) == expected


def test_format_celsius_appends_an_optional_suffix():
    assert format_celsius(21.111111111, suffix=" °C") == "21.1 °C"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, True),
        (21, True),  # ints are real temperatures too
        # A Pi's own CPU sensor and a boiler flow are legitimately device_class
        # temperature; this band filters physical nonsense, not warm devices.
        (78.0, True),
        (-273.15, True),
        (1000.0, True),
        (-273.2, False),
        (1000.1, False),
        (1e308, False),
        (float("nan"), False),
        (float("inf"), False),
        (True, False),
        (None, False),
        ("21", False),
    ],
)
def test_is_plausible_celsius_rejects_only_physical_nonsense(value, expected):
    assert is_plausible_celsius(value) is expected
