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

REPO_ROOT = Path(__file__).resolve().parent.parent
LISTENER_CSS = REPO_ROOT / "mammamiradio" / "static" / "listener.css"

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
    """All <input>/<textarea> rules in listener.css must declare font-size >= 16px.

    iOS Safari auto-zooms any focused form field below 16 px. The dedica
    form lives on the listener page and shipped at 14 px, which broke the
    mobile UX on every iPhone tap.
    """
    text = _read_listener_css()
    rule_re = re.compile(
        r"(\.mmr-dedica-form[^{]*\b(?:input|textarea)[^{]*|\.mmr-dedica-form-input[^{]*)\{([^}]*)\}",
        re.DOTALL,
    )
    font_size_re = re.compile(r"font-size\s*:\s*(\d+(?:\.\d+)?)px")
    offenders: list[tuple[str, float]] = []
    for selector, body in rule_re.findall(text):
        match = font_size_re.search(body)
        if not match:
            continue
        size_px = float(match.group(1))
        if size_px < 16:
            offenders.append((selector.strip().splitlines()[0], size_px))
    assert not offenders, (
        "Listener form fields below 16 px font-size will trigger iOS auto-zoom "
        "on focus and break the mobile layout. Bump to 16 px:\n"
        + "\n".join(f"  {sel}: {size}px" for sel, size in offenders)
    )


def test_body_uses_modern_viewport_units_for_ios() -> None:
    """html/body must declare a `100svh`/`100dvh` min-height to avoid iOS
    address-bar cutoff. Falling back to `100vh` is fine, but the dynamic
    unit must be present too.
    """
    text = _read_listener_css()
    html_body_re = re.compile(r"html\s*,\s*body\s*\{([^}]*)\}", re.DOTALL)
    block = html_body_re.search(text)
    assert block, "listener.css must declare an `html, body { … }` block."
    body = block.group(1)
    has_dynamic = re.search(r"min-height\s*:\s*100(svh|dvh)", body)
    assert has_dynamic, (
        "html/body must declare `min-height: 100svh` (or 100dvh) in addition "
        "to `100vh`. Without it iOS Safari hides ~100 px of content under "
        "the collapsing address bar."
    )
