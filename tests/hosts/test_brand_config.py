"""Tests for brand-fiction config layer (separate from operator-truth engine config).

Covers: schema parsing, theme guardrails (hex/contrast/lightness), font allowlist,
brand-host FK validation, backward compatibility (missing [brand] block).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from mammamiradio.core.config import (
    _BRAND_DEFAULT_BG,
    _BRAND_DEFAULT_PRIMARY,
    _contrast_ratio,
    _hex_lightness,
    _hex_to_rgb,
    _parse_brand,
)
from mammamiradio.core.models import HostPersonality, PersonalityAxes


def _make_hosts():
    return [
        HostPersonality(
            name="Marco",
            voice="onyx",
            style="manic energy, conspiracy theories",
            personality=PersonalityAxes.from_dict({}),
            engine="openai",
        ),
        HostPersonality(
            name="Giulia",
            voice="it-IT-IsabellaNeural",
            style="razor-sharp sarcasm",
            personality=PersonalityAxes.from_dict({}),
            engine="edge",
        ),
    ]


# ─── color helpers ───────────────────────────────────────────────


def test_hex_to_rgb_valid():
    assert _hex_to_rgb("#F4D048") == (244, 208, 72)
    assert _hex_to_rgb("F4D048") == (244, 208, 72)


def test_hex_to_rgb_invalid_returns_none():
    for bad in ["", "#GGG", "#12345", "not-a-color", None]:
        assert _hex_to_rgb(bad) is None


def test_hex_lightness_dark_canvas():
    assert _hex_lightness("#14110F") < 10  # almost-black


def test_hex_lightness_light_color():
    assert _hex_lightness("#F5EDD8") > 80  # cream is very light


def test_contrast_ratio_cream_on_espresso():
    # WCAG AAA tier; matches docs/design/system.md claim of 12.4:1
    ratio = _contrast_ratio("#F5EDD8", "#14110F")
    assert ratio is not None
    assert ratio > 12.0


def test_contrast_ratio_invalid_inputs():
    assert _contrast_ratio("#fff", "not-a-color") is None


# ─── brand parse: empty / missing block ─────────────────────────


def test_parse_brand_missing_block_uses_defaults():
    raw = {"station": {"name": "Test Station"}}
    brand, warnings = _parse_brand(raw, _make_hosts())
    assert brand.station_name == "Test Station"
    assert brand.frequency == ""
    assert len(brand.hosts) == 2
    assert brand.hosts[0].display_name == "Marco"
    assert warnings == []


def test_parse_brand_empty_dict_falls_back():
    raw = {}
    brand, warnings = _parse_brand(raw, _make_hosts())
    assert brand.station_name == "mammamiradio"
    assert warnings == []


# ─── brand parse: valid full block ──────────────────────────────


def test_parse_brand_full_valid_block():
    raw = {
        "station": {"name": "Mamma Mi Radio"},
        "brand": {
            "station_name": "Mamma Mi Radio",
            "frequency": "96,7 FM",
            "city": "Napoli",
            "founded": 2024,
            "tagline": "La radio che ascolta la tua casa",
            "theme": {
                "primary_color": "#F4D048",
                "background_color": "#14110F",
                "display_font": "Playfair Display",
            },
            "hosts": [
                {"engine_host": "Marco", "display_name": "Marco del bar"},
                {"engine_host": "Giulia", "display_name": "Nonna Giulia"},
            ],
        },
    }
    brand, warnings = _parse_brand(raw, _make_hosts())
    assert brand.station_name == "Mamma Mi Radio"
    assert brand.frequency == "96,7 FM"
    assert brand.founded == 2024
    assert brand.theme.primary_color == "#F4D048"
    assert brand.theme.display_font == "Playfair Display"
    assert {h.display_name for h in brand.hosts} == {"Marco del bar", "Nonna Giulia"}
    assert warnings == []


# ─── theme guardrails (design D1) ───────────────────────────────


def test_theme_invalid_hex_falls_back_with_warning():
    raw = {
        "brand": {
            "theme": {"primary_color": "not-a-hex"},
            "hosts": [],
        }
    }
    brand, warnings = _parse_brand(raw, _make_hosts())
    assert brand.theme.primary_color == _BRAND_DEFAULT_PRIMARY
    assert any("not a valid hex" in w for w in warnings)


def test_theme_light_background_rejected():
    """Volare Refined invariant: dark canvas only. Lightness > 25% rejected."""
    raw = {
        "brand": {
            "theme": {"background_color": "#FFFFFF"},  # white
            "hosts": [],
        }
    }
    brand, warnings = _parse_brand(raw, _make_hosts())
    assert brand.theme.background_color == _BRAND_DEFAULT_BG
    assert any("too light" in w for w in warnings)


def test_theme_decorative_colors_no_contrast_check():
    """primary_color and accent_color are decorative (not body text) — any valid hex passes,
    even low-contrast values that would fail body-text rules. Background is the only field
    with contrast/lightness enforcement (covered by test_theme_light_background_rejected)."""
    raw = {
        "brand": {
            "theme": {"primary_color": "#F4D048", "accent_color": "#F4D048"},  # gold, low contrast vs cream
            "hosts": [],
        }
    }
    brand, warnings = _parse_brand(raw, _make_hosts())
    # Decorative colors pass through unchanged
    assert brand.theme.primary_color == "#F4D048"
    assert brand.theme.accent_color == "#F4D048"
    # No warning about primary or accent
    assert not any("primary_color" in w for w in warnings)
    assert not any("accent_color" in w for w in warnings)


def test_theme_unapproved_font_rejected():
    raw = {
        "brand": {
            "theme": {"display_font": "Comic Sans MS"},
            "hosts": [],
        }
    }
    brand, warnings = _parse_brand(raw, _make_hosts())
    assert brand.theme.display_font == "Playfair Display"  # default
    assert any("not in the approved list" in w for w in warnings)


def test_theme_approved_alt_display_font():
    raw = {
        "brand": {
            "theme": {"display_font": "Cormorant Garamond"},
            "hosts": [],
        }
    }
    brand, warnings = _parse_brand(raw, _make_hosts())
    assert brand.theme.display_font == "Cormorant Garamond"
    assert warnings == []


# ─── brand-host FK validation ───────────────────────────────────


def test_brand_host_unknown_engine_host_dropped():
    raw = {
        "brand": {
            "hosts": [
                {"engine_host": "Marco", "display_name": "Marco del bar"},
                {"engine_host": "Francesca", "display_name": "Francesca"},  # not in [[hosts]]
            ]
        }
    }
    brand, warnings = _parse_brand(raw, _make_hosts())
    # Marco kept + Giulia auto-added (covered=Marco, missing=Giulia); Francesca dropped
    names = {h.engine_host for h in brand.hosts}
    assert "Marco" in names
    assert "Giulia" in names
    assert "Francesca" not in names
    assert any("does not match" in w for w in warnings)


def test_brand_host_auto_fill_uncovered_engine_hosts():
    """If [[brand.hosts]] only declares Marco, Giulia is auto-filled with defaults."""
    raw = {
        "brand": {
            "hosts": [{"engine_host": "Marco", "display_name": "Marco del bar"}],
        }
    }
    brand, warnings = _parse_brand(raw, _make_hosts())
    assert {h.engine_host for h in brand.hosts} == {"Marco", "Giulia"}
    auto = next(h for h in brand.hosts if h.engine_host == "Giulia")
    assert auto.display_name == "Giulia"  # default to engine name
    # Auto-fill is silent (not a warning)
    assert warnings == []


# ─── founded year guardrails ────────────────────────────────────


def test_founded_invalid_year_dropped():
    raw = {"brand": {"founded": 1800, "hosts": []}}
    brand, warnings = _parse_brand(raw, _make_hosts())
    assert brand.founded == 0
    assert any("outside" in w for w in warnings)


def test_founded_non_int_dropped():
    raw = {"brand": {"founded": "yesterday", "hosts": []}}
    brand, warnings = _parse_brand(raw, _make_hosts())
    assert brand.founded == 0
    assert any("not a valid year" in w for w in warnings)


# ─── backward compat: real production-shape radio.toml ──────────


def test_load_pre_brand_fixture_clean():
    """Existing radio.toml without [brand] must load cleanly with derived defaults."""
    fixture = Path(__file__).parent / "fixtures" / "radio_pre_brand.toml"
    if not fixture.exists():
        pytest.skip("radio_pre_brand.toml fixture not present")
    with open(fixture, "rb") as f:
        raw = tomllib.load(f)
    brand, warnings = _parse_brand(raw, _make_hosts())
    assert brand.station_name  # falls back to [station].name or "mammamiradio"
    assert len(brand.hosts) == 2
    assert warnings == []
