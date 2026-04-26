"""OpenGraph social card renderer (brand-engine PR-E).

Per design D-Design-2: poster-style 1200x630 PNG that looks like a still-frame
from the listener page, not a generic SaaS marketing card. Italian flag tricolor
at top, brand identity dominant in Playfair italic, track info as secondary
lower-third band.

Implementation note: uses Pillow direct (cairosvg considered but adds system
cairo lib dep). Pillow renders typography well at the 1200x630 size where OG
previews are viewed — quality bar is "legible + brand-recognizable", not
marketing-hero crisp. Bundled or system Italian display font preferred.

Generation runs in a background task on track change (per design D4.1: never
on request thread). Request handler serves cached PNG. Cache miss returns
fallback (logo.png) so social previews never 404.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Volare Refined defaults (used when no brand theme override).
_DEFAULT_BG = (20, 17, 15)  # #14110F espresso
_DEFAULT_PRIMARY = (244, 208, 72)  # #F4D048 sun
_DEFAULT_TEXT = (245, 237, 216)  # #F5EDD8 cream
_TRICOLOR = [(0, 146, 70), (241, 242, 241), (206, 43, 55)]  # Italian flag

_CARD_W = 1200
_CARD_H = 630


@dataclass
class OGCardInputs:
    """Inputs for OG card render — simple value object, easy to test."""

    station_name: str
    frequency: str = ""
    city: str = ""
    founded: int = 0
    tagline: str = ""
    track_title: str = ""
    track_artist: str = ""
    primary_color_hex: str = "#F4D048"
    background_color_hex: str = "#14110F"


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    s = hex_color.strip().lstrip("#")
    if len(s) != 6:
        return _DEFAULT_BG
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return _DEFAULT_BG


def _load_font(size: int, italic: bool = False) -> "ImageFont.FreeTypeFont | ImageFont.ImageFont":
    """Best-effort font loader.

    Tries bundled fonts/Playfair-*.ttf first (cathedral path), falls back to
    macOS system fonts, then Pillow default. Default looks rough but never
    crashes — INSTANT AUDIO principle extends to social previews.
    """
    fonts_dir = Path(__file__).parent / "fonts"
    candidates: list[str | Path] = []
    if italic:
        candidates += [
            fonts_dir / "PlayfairDisplay-BoldItalic.ttf",
            fonts_dir / "PlayfairDisplay-Italic.ttf",
            "/System/Library/Fonts/Supplemental/Times New Roman Bold Italic.ttf",
            "/System/Library/Fonts/Supplemental/Times New Roman Italic.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif-BoldItalic.ttf",
        ]
    else:
        candidates += [
            fonts_dir / "Outfit-Medium.ttf",
            fonts_dir / "Outfit-Regular.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(str(candidate), size)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


def render_og_card(inputs: OGCardInputs) -> bytes:
    """Render a 1200x630 OG card PNG. Returns the PNG bytes."""
    bg = _hex_to_rgb(inputs.background_color_hex)
    primary = _hex_to_rgb(inputs.primary_color_hex)

    img = Image.new("RGB", (_CARD_W, _CARD_H), bg)
    draw = ImageDraw.Draw(img)

    # Italian flag tricolor — 4px stripe at top edge (signature element)
    stripe_h = 6
    third = _CARD_W // 3
    for i, color in enumerate(_TRICOLOR):
        x0 = i * third
        x1 = (i + 1) * third if i < 2 else _CARD_W
        draw.rectangle([x0, 0, x1, stripe_h], fill=color)

    # Soft amber glow at top-left (cheap Pillow approximation: filled circle with low alpha)
    glow_layer = Image.new("RGBA", (_CARD_W, _CARD_H), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    glow_radius = 480
    glow_alpha = 28  # ~11% opacity
    glow_color = (*primary, glow_alpha)
    glow_draw.ellipse(
        [-glow_radius // 2, -glow_radius // 2, glow_radius, glow_radius],
        fill=glow_color,
    )
    img = Image.alpha_composite(img.convert("RGBA"), glow_layer).convert("RGB")
    draw = ImageDraw.Draw(img)

    # Brand identity — Playfair italic dominant
    brand_font = _load_font(120, italic=True)
    draw.text((80, 90), inputs.station_name, font=brand_font, fill=_DEFAULT_TEXT)

    # Brand metadata line — JetBrains Mono uppercase letterspacing approximated
    meta_parts = []
    if inputs.frequency:
        meta_parts.append(inputs.frequency.upper())
    if inputs.city:
        meta_parts.append(inputs.city.upper())
    if inputs.founded:
        meta_parts.append(f"DAL {inputs.founded}")
    meta_text = "  ·  ".join(meta_parts) if meta_parts else inputs.tagline
    if meta_text:
        meta_font = _load_font(26)
        draw.text((80, 250), meta_text, font=meta_font, fill=(*_DEFAULT_TEXT, 153))

    # Lower-third divider line
    divider_y = 430
    draw.line([(80, divider_y), (_CARD_W - 80, divider_y)], fill=primary, width=1)

    # "ORA IN ONDA" eyebrow — Outfit medium uppercase
    eyebrow_font = _load_font(28)
    draw.text((80, 470), "ORA IN ONDA", font=eyebrow_font, fill=(*primary, 200))

    # Track / idle copy
    track_font = _load_font(48)
    if inputs.track_title:
        track_text = inputs.track_title
        if inputs.track_artist:
            track_text = f"{inputs.track_artist} — {track_text}"
        draw.text((80, 520), track_text[:80], font=track_font, fill=_DEFAULT_TEXT)
    elif inputs.tagline:
        idle_font = _load_font(40, italic=True)
        draw.text(
            (80, 530),
            inputs.tagline[:90],
            font=idle_font,
            fill=(*_DEFAULT_TEXT, 178),
        )

    # Encode to PNG bytes
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def render_og_card_for_brand(brand, current_track=None) -> bytes:
    """Render an OG card from BrandSection + optional Track. Helper for the route."""
    inputs = OGCardInputs(
        station_name=brand.station_name,
        frequency=brand.frequency,
        city=brand.city,
        founded=brand.founded or 0,
        tagline=brand.tagline,
        track_title=getattr(current_track, "title", "") if current_track else "",
        track_artist=getattr(current_track, "artist", "") if current_track else "",
        primary_color_hex=brand.theme.primary_color,
        background_color_hex=brand.theme.background_color,
    )
    return render_og_card(inputs)
