"""Listener mobile invariants.

Two regressions shipped to production by skipping mobile-specific CSS:

1. PR #235 (Volare Refined) renamed `.nav` → `.mmr-nav`. The pre-Volare
   `@media (max-width: 640px) { .nav-links { display: none; } }` rule was
   left for the old class name and never ported. On a 375 px phone the
   brand + 4 anchor links + the In Onda pill exceeded the viewport width,
   the body became wider than the viewport, and vertical scroll broke —
   ~80 % of sections were rendered to the right of the visible area.

2. The dedica form `<input>` and `<textarea>` shipped at `font-size: 14px`.
   iOS Safari auto-zooms any form field below 16 px on focus, which knocks
   the layout sideways and is one of the most-reported mobile UX bugs.

Both classes of bug are CSS-only and can be caught with a static parse.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LISTENER_CSS = REPO_ROOT / "mammamiradio" / "web" / "static" / "listener.css"

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
# Greedy capture inside a top-level @media block. Listener CSS does not
# nest @media, so a single non-greedy outer scan with brace matching is
# overkill — we just take everything up to the next `@media` or EOF.
_MEDIA_BLOCK_RE = re.compile(
    r"@media\s*\(\s*max-width\s*:\s*(\d+)px\s*\)\s*\{(.*?)(?=@media|\Z)",
    re.DOTALL,
)


def _read_listener_css() -> str:
    return _COMMENT_RE.sub("", LISTENER_CSS.read_text(encoding="utf-8"))


def _rule_bodies_for_selector(text: str, selector: str) -> list[str]:
    rule_re = re.compile(r"([^{}]+)\{([^}]*)\}", re.DOTALL)
    bodies: list[str] = []
    for selector_block, body in rule_re.findall(text):
        selectors = [s.strip() for s in selector_block.split(",")]
        if selector in selectors:
            bodies.append(body)
    return bodies


def test_phone_breakpoint_collapses_or_hides_nav_anchor_links() -> None:
    """The <=600px @media block must hide or wrap the listener nav anchor links.

    Anchor links are the four `<a href="#…">` items inside `.mmr-nav nav`.
    On a 375 px phone they push the In Onda pill off-screen and force a
    horizontal overflow. Acceptable mitigations:
      - `.mmr-nav nav { display: none; }` (current fix), or
      - `.mmr-nav-inner { flex-wrap: wrap; }` (wrap onto a second row).
    """
    text = _read_listener_css()
    phone_blocks = [body for width, body in _MEDIA_BLOCK_RE.findall(text) if int(width) <= 600]
    assert phone_blocks, "listener.css has no @media (max-width: 600px) block — phones get desktop layout."
    combined = "\n".join(phone_blocks)
    hides_links = re.search(r"\.mmr-nav\s+nav\s*\{[^}]*display\s*:\s*none", combined)
    wraps_inner = re.search(r"\.mmr-nav-inner\s*\{[^}]*flex-wrap\s*:\s*wrap", combined)
    assert hides_links or wraps_inner, (
        "Phone breakpoint (<=600px) must either hide `.mmr-nav nav` or set "
        "`flex-wrap: wrap` on `.mmr-nav-inner`. Without one of these the "
        "header overflows the viewport and breaks vertical scroll on phones."
    )


def test_form_inputs_avoid_ios_auto_zoom() -> None:
    """All Volare-namespace input/textarea rules in listener.css must declare
    font-size >= 16px.

    iOS Safari auto-zooms any focused form field below 16 px. The dedica
    form lives on the listener page and shipped at 14 px, which broke the
    mobile UX on every iPhone tap.

    Scoped to the `.mmr-*` namespace (Volare Refined). Pre-Volare class
    selectors like `.form-input` are dead code — none of them appear in the
    rendered HTML — and are removed in #270. Catching them here would
    create a false-positive on this branch.
    """
    text = _read_listener_css()
    rule_re = re.compile(r"([^{}]+)\{([^}]*)\}", re.DOTALL)
    font_size_re = re.compile(r"font-size\s*:\s*(\d+(?:\.\d+)?)px")
    offenders: list[tuple[str, float]] = []
    for selector_block, body in rule_re.findall(text):
        selectors = [s.strip() for s in selector_block.split(",")]
        # Targets a form field iff at least one selector in the group is in
        # the live Volare namespace AND mentions input or textarea (either
        # as an element selector or as a class-name suffix like
        # `.mmr-dedica-form-input`).
        targets_form = any(".mmr-" in s and ("input" in s or "textarea" in s) for s in selectors)
        if not targets_form:
            continue
        # Last font-size declaration wins inside a single rule block.
        sizes = font_size_re.findall(body)
        if not sizes:
            continue
        size_px = float(sizes[-1])
        if size_px < 16:
            first_selector = selectors[0].splitlines()[0][:80]
            offenders.append((first_selector, size_px))
    assert not offenders, (
        "Listener form fields below 16 px font-size will trigger iOS auto-zoom "
        "on focus and break the mobile layout. Bump to 16 px:\n"
        + "\n".join(f"  {sel}: {size}px" for sel, size in offenders)
    )


def test_body_uses_modern_viewport_units_for_ios() -> None:
    """html/body must declare a `100svh`/`100dvh` min-height to avoid iOS
    address-bar cutoff (falling back to `100vh` is fine, but the dynamic unit
    must be present), AND must keep `overscroll-behavior-x: contain` to
    prevent horizontal rubber-band snap when content overflows.
    """
    text = _read_listener_css()
    # Order-insensitive: accept both `html, body` and `body, html`.
    html_body_re = re.compile(
        r"(?:html\s*,\s*body|body\s*,\s*html)\s*\{([^}]*)\}",
        re.DOTALL,
    )
    block = html_body_re.search(text)
    assert block, "listener.css must declare an `html, body { … }` block."
    body = block.group(1)
    has_dynamic = re.search(r"min-height\s*:\s*100(svh|dvh)", body)
    assert has_dynamic, (
        "html/body must declare `min-height: 100svh` (or 100dvh) in addition "
        "to `100vh`. Without it iOS Safari hides ~100 px of content under "
        "the collapsing address bar."
    )
    has_overscroll_guard = re.search(r"overscroll-behavior-x\s*:\s*contain", body)
    assert has_overscroll_guard, (
        "html/body must keep `overscroll-behavior-x: contain` to prevent the "
        "horizontal rubber-band snap that triggered the original `In Onda` "
        "tap regression on phones."
    )


def test_listener_uses_no_fixed_body_overlay() -> None:
    """The listener background must not be a fixed body pseudo-element.

    Real Safari viewport compositing can keep fixed pseudo-elements in a
    separate layer that hides scrolled content. Full-page screenshots flatten
    the document and miss this class of bug, so pin the CSS shape directly.
    """
    text = _read_listener_css()
    fixed_overlay = re.search(r"body::before\s*\{[^}]*position\s*:\s*fixed", text, re.DOTALL)
    assert not fixed_overlay, (
        "listener.css must not use `body::before { position: fixed; ... }` "
        "for page atmosphere. Put the glow/grain on `html, body` instead so "
        "real viewport compositing cannot cover scrolled elements."
    )


def test_listener_anchor_targets_clear_sticky_nav() -> None:
    """Listener anchors must land below the sticky nav in real viewports."""
    text = _read_listener_css()
    scroll_margin_re = re.compile(r"scroll-margin-top\s*:\s*(?!0(?:px|rem|em|%)?\s*;)[^;]+;")
    missing = [
        selector
        for selector in (".mmr-stage", ".mmr-section")
        if not any(scroll_margin_re.search(body) for body in _rule_bodies_for_selector(text, selector))
    ]
    assert not missing, (
        "Listener anchor targets need non-zero `scroll-margin-top` so sticky "
        "navigation does not hide the section when users jump or scroll to it: " + ", ".join(missing)
    )
