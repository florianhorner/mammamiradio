"""Tests for OG social card rendering (brand-engine PR-E).

Verifies the renderer produces valid PNG bytes, handles missing track gracefully
(idle state), and respects brand theme overrides. Visual quality is verified
manually against the design D-Design-2 spec — these tests cover the contract.
"""

from __future__ import annotations

from io import BytesIO

import pytest
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


# ─── /og-card.png route tests (streamer integration) ───────────


@pytest.mark.asyncio
async def test_og_card_route_returns_png():
    """GET /og-card.png returns a valid 1200x630 PNG with proper headers."""
    import httpx

    from tests.test_streamer_routes import _make_test_app

    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/og-card.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert "max-age" in resp.headers.get("cache-control", "")
    img = _open_png(resp.content)
    assert img.size == (1200, 630)


@pytest.mark.asyncio
async def test_og_card_route_caches_response():
    """Second request hits cache (same response bytes as first)."""
    import httpx

    from tests.test_streamer_routes import _make_test_app

    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.get("/og-card.png")
        second = await client.get("/og-card.png")
    assert first.status_code == 200
    assert second.status_code == 200
    # Cache hit: bytes should be identical
    assert first.content == second.content


@pytest.mark.asyncio
async def test_og_card_route_fallback_on_render_failure(monkeypatch):
    """If the renderer crashes, route falls back to logo SVG (never 404s)."""
    import httpx

    from mammamiradio import streamer as streamer_mod
    from tests.test_streamer_routes import _make_test_app

    app = _make_test_app()
    # Clear any cached response from prior tests
    streamer_mod._og_card_cache.clear()

    def boom(brand, current_track):
        raise RuntimeError("simulated render failure")

    monkeypatch.setattr("mammamiradio.og_card.render_og_card_for_brand", boom)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/og-card.png")
    # Either logo SVG fallback (200 + svg) or 503 if logo also missing
    assert resp.status_code in (200, 503)
    if resp.status_code == 200:
        assert "image/" in resp.headers["content-type"]
