"""Tests for OG social card rendering (brand-engine PR-E).

Verifies the renderer produces valid PNG bytes, handles missing track gracefully
(idle state), and respects brand theme overrides. Visual quality is verified
manually against the design D-Design-2 spec — these tests cover the contract.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image

from mammamiradio.og_card import OGCardInputs, render_og_card


def _open_png(png_bytes: bytes) -> Image.Image:
    return Image.open(BytesIO(png_bytes))


def test_render_og_card_basic():
    """Renders a 1200x630 PNG with brand + track text."""
    inputs = OGCardInputs(
        station_name="Mamma Mi Radio",
        frequency="96,7 FM",
        city="Napoli",
        founded=2024,
        track_title="Albachiara",
        track_artist="Vasco Rossi",
    )
    png = render_og_card(inputs)
    assert isinstance(png, bytes)
    assert len(png) > 1000  # not empty
    img = _open_png(png)
    assert img.size == (1200, 630), "OG cards must be 1200x630 (Twitter/Facebook spec)"
    assert img.format == "PNG"


def test_render_og_card_idle_state():
    """No current track: tagline used instead of empty track line."""
    inputs = OGCardInputs(
        station_name="Mamma Mi Radio",
        tagline="La radio che ascolta la tua casa",
    )
    png = render_og_card(inputs)
    img = _open_png(png)
    assert img.size == (1200, 630)


def test_render_og_card_minimal():
    """Only station_name set: still renders without crashing."""
    png = render_og_card(OGCardInputs(station_name="Test Radio"))
    img = _open_png(png)
    assert img.size == (1200, 630)


def test_render_og_card_brand_theme_override():
    """Brand-supplied colors are honored in output (different from default).

    We can't easily assert pixel values without flakiness, but we CAN verify
    the renderer accepts the inputs and produces a valid PNG.
    """
    inputs = OGCardInputs(
        station_name="Berlin Radio",
        primary_color_hex="#FF6B35",  # orange — different from --sun
        background_color_hex="#0A0A0F",  # near-black, different from espresso
    )
    png = render_og_card(inputs)
    img = _open_png(png)
    assert img.size == (1200, 630)


def test_render_og_card_long_track_truncated():
    """Track title over 80 chars must not break layout."""
    long_title = "A" * 200
    inputs = OGCardInputs(
        station_name="Mamma Mi Radio",
        track_title=long_title,
        track_artist="Long Artist Name",
    )
    png = render_og_card(inputs)
    img = _open_png(png)
    assert img.size == (1200, 630)


def test_render_og_card_invalid_hex_falls_back():
    """Bad hex colors don't crash; renderer falls back to defaults."""
    inputs = OGCardInputs(
        station_name="Test",
        primary_color_hex="not-a-hex",
        background_color_hex="also-bad",
    )
    png = render_og_card(inputs)
    img = _open_png(png)
    assert img.size == (1200, 630)


def test_render_og_card_unicode_station_name():
    """Italian and other unicode characters render without errors."""
    inputs = OGCardInputs(station_name="Mamma Mì Radio · 96,7 FM")
    png = render_og_card(inputs)
    img = _open_png(png)
    assert img.size == (1200, 630)


def test_render_og_card_for_brand_helper():
    """The render_og_card_for_brand helper accepts BrandSection + Track shapes."""
    from mammamiradio.config import BrandSection, BrandTheme
    from mammamiradio.og_card import render_og_card_for_brand

    brand = BrandSection(
        station_name="Test Station",
        frequency="100 FM",
        city="Milano",
        founded=2024,
        tagline="Test tagline",
        theme=BrandTheme(),
    )
    png = render_og_card_for_brand(brand, current_track=None)
    img = _open_png(png)
    assert img.size == (1200, 630)
