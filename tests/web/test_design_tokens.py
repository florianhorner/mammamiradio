"""Design-token guard.

mammamiradio/web/static/tokens.css is the sole source of truth for CSS
palette primitives. This test fails if any other file reintroduces a
`:root { --foo: ...; }` primitive, which would re-open the drift the
Phase A consolidation closed.

Non-`:root` custom-property scopes (component-scoped vars inside a class)
are allowed and ignored.

See docs/design/system.md § "Listener site composition — canonical" and the
Decisions Log entry dated 2026-04-21.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKENS_CSS = REPO_ROOT / "mammamiradio" / "web" / "static" / "tokens.css"
ADMIN_HTML = REPO_ROOT / "mammamiradio" / "web" / "templates" / "admin.html"
_ADMIN_HTML_TEXT = ADMIN_HTML.read_text(encoding="utf-8")
_HTML_FILES = sorted((REPO_ROOT / "mammamiradio" / "web" / "templates").rglob("*.html"))
_CSS_FILES = sorted((REPO_ROOT / "mammamiradio" / "web" / "static").glob("*.css"))
GUARDED_FILES = [path for path in (_HTML_FILES + _CSS_FILES) if path != TOKENS_CSS]

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_ROOT_BLOCK_RE = re.compile(r":root\s*\{([^}]*)\}", re.DOTALL)
_PRIMITIVE_DECL_RE = re.compile(r"--[a-z0-9-]+(?=\s*:)")


def _strip_comments(text: str) -> str:
    return _COMMENT_RE.sub("", text)


# --brand-* tokens are intentionally per-station dynamic (set in listener.html
# from radio.toml [brand.theme]). They are NOT Volare Refined palette primitives —
# they are the brand-engine overlay layer (PR-C, 2026-04-26). listener.css references
# them via fallback: var(--brand-primary, var(--sun)).
_BRAND_NAMESPACE_RE = re.compile(r"--brand-")


def _primitives_in(path: Path) -> list[str]:
    text = _strip_comments(path.read_text(encoding="utf-8"))
    seen: set[str] = set()
    primitives: list[str] = []
    for block in _ROOT_BLOCK_RE.finditer(text):
        for primitive in _PRIMITIVE_DECL_RE.findall(block.group(1)):
            if primitive in seen:
                continue
            if _BRAND_NAMESPACE_RE.match(primitive):
                # Brand-engine namespace exempt: per-station dynamic tokens.
                continue
            seen.add(primitive)
            primitives.append(primitive)
    return primitives


@pytest.mark.parametrize("path", GUARDED_FILES, ids=lambda path: path.name)
def test_no_palette_primitives_outside_tokens_css(path: Path) -> None:
    """Every file except tokens.css must have zero :root palette primitives."""
    primitives = _primitives_in(path)
    rel = path.relative_to(REPO_ROOT).as_posix()
    preview = ", ".join(primitives[:5])
    if len(primitives) > 5:
        preview += " ..."
    assert not primitives, (
        f"{rel} re-introduces :root primitives: {preview}\n"
        f"Palette / typography / spacing tokens must ONLY be defined in "
        f"mammamiradio/web/static/tokens.css. See docs/design/system.md § "
        f"'Listener site composition — canonical' and the Decisions Log "
        f"entry dated 2026-04-21."
    )


def test_tokens_css_has_core_primitives() -> None:
    """Smoke check: tokens.css must define at least the core primitives."""
    defined = set(_primitives_in(TOKENS_CSS))
    required = {
        "--bg",
        "--surface",
        "--sun",
        "--sun2",
        "--cream",
        "--lancia",
        "--ok",
        "--error",
        "--warning",
        "--flag-green",
        "--flag-white",
        "--flag-red",
        "--font-display",
        "--font-body",
        "--font-mono",
    }
    missing = sorted(required - defined)
    assert not missing, f"mammamiradio/web/static/tokens.css is missing required primitives: {missing}"


_VAR_REF_NO_FALLBACK_RE = re.compile(r"var\(\s*(--[a-z0-9-]+)\s*\)")
_CSS_DECL_RE = re.compile(r"(?P<name>[\w-]+)\s*:\s*(?P<value>[^;]+);")


def _css_block(text: str, selector: str) -> str:
    escaped = re.escape(selector)
    match = re.search(rf"{escaped}\s*\{{([^}}]*)\}}", _strip_comments(text), re.DOTALL)
    assert match, f"CSS selector not found: {selector}"
    return match.group(1)


def _css_declarations(block: str) -> dict[str, str]:
    return {match.group("name"): match.group("value").strip() for match in _CSS_DECL_RE.finditer(block)}


def test_every_var_ref_resolves_to_a_defined_token() -> None:
    """Every var(--foo) used outside tokens.css with no fallback must resolve to a defined token.

    Catches the class of bug where a CSS author writes `background: var(--flag-green)`
    but never declares `--flag-green` in tokens.css. The browser silently renders
    transparent and the visual breaks (PR #235 shipped this regression for both
    --flag-* and --terracotta/--sage/--ink).

    Exempt:
    - --brand-* (per-station dynamic, injected at request time from [brand.theme]).
    - var(--foo, fallback) — any fallback (literal or nested var) is safe because
      the browser falls through cleanly when the token is missing.
    """
    defined = set(_primitives_in(TOKENS_CSS))
    missing: dict[str, set[str]] = {}
    for path in GUARDED_FILES:
        text = _strip_comments(path.read_text(encoding="utf-8"))
        for ref in _VAR_REF_NO_FALLBACK_RE.findall(text):
            if ref in defined:
                continue
            if _BRAND_NAMESPACE_RE.match(ref):
                continue
            missing.setdefault(path.name, set()).add(ref)
    assert not missing, (
        "CSS/template files reference undefined tokens with no fallback. "
        "Add them to tokens.css or use var(--foo, fallback):\n"
        + "\n".join(f"  {f}: {sorted(refs)}" for f, refs in sorted(missing.items()))
    )


@pytest.mark.parametrize(
    ("control_selector", "switch_selector", "slider_selector", "input_id", "aria_label"),
    [
        (".chaos-control", ".chaos-switch", ".chaos-slider", "chaosToggle", "Toggle Chaos Mode"),
        (".festival-control", ".festival-switch", ".festival-slider", "festivalToggle", "Toggle Festival Mode"),
    ],
)
def test_admin_mode_controls_use_tokens_and_accessible_switches(
    control_selector: str,
    switch_selector: str,
    slider_selector: str,
    input_id: str,
    aria_label: str,
) -> None:
    """Admin mode switches must keep tokenized layout and keyboard accessibility."""
    html = _ADMIN_HTML_TEXT

    control = _css_declarations(_css_block(html, control_selector))
    tokenized_layout = {
        "gap": "var(--space-3)",
        "margin-bottom": "var(--space-3)",
        "padding": "var(--space-2) var(--space-3)",
        "border-radius": "var(--radius-md)",
    }
    for property_name, expected_value in tokenized_layout.items():
        actual = control.get(property_name)
        assert actual == expected_value, f"{control_selector} {property_name} must use {expected_value}, got {actual!r}"

    switch = _css_declarations(_css_block(html, switch_selector))
    assert switch.get("height") == "44px", f"{switch_selector} must keep a 44px touch target."

    assert re.search(
        rf'<input\b(?=[^>]*\bid="{re.escape(input_id)}")(?=[^>]*\baria-label="{re.escape(aria_label)}")[^>]*>',
        html,
        re.DOTALL,
    ), f'input#{input_id} must keep aria-label="{aria_label}".'

    focus_block = _css_block(html, f"{switch_selector} input:focus-visible + {slider_selector}")
    assert "box-shadow" in focus_block or "outline" in focus_block, (
        f"{switch_selector} must show a visible keyboard focus indicator."
    )


def test_ad_segment_color_is_distinct_from_warning() -> None:
    """Ad segments must not share --warning with the degraded-status color.

    --warning (amber) is the degraded / on-fallback status color. Coloring ad
    segments with the same token left an operator unable to tell an "ad" badge
    from a "system degraded" status by color alone. Ads use the dedicated
    --seg-ad token instead. Regression guard: --seg-ad exists, never aliases
    --warning, and no ad-segment rule in admin.html colors with --warning.
    """
    assert "--seg-ad" in _primitives_in(TOKENS_CSS), (
        "tokens.css must define --seg-ad (the ad-segment color, distinct from "
        "--warning / degraded status)."
    )
    tokens_text = _strip_comments(TOKENS_CSS.read_text(encoding="utf-8"))
    assert not re.search(r"--seg-ad\s*:\s*var\(\s*--warning\s*\)", tokens_text), (
        "--seg-ad must not alias --warning — that re-creates the ad/degraded "
        "color collision."
    )

    text = _strip_comments(_ADMIN_HTML_TEXT)
    ad_rule_re = re.compile(
        r'[^\n{}]*(?:\[data-t="ad"\]|\[data-type="ad"\]|\.segment-ad)[^\n{}]*\{[^}]*\}'
    )
    offenders = [
        m.group(0).strip()
        for m in ad_rule_re.finditer(text)
        if "var(--warning)" in m.group(0)
    ]
    assert not offenders, (
        "ad-segment rules must use var(--seg-ad), not var(--warning) "
        "(degraded-status color):\n" + "\n".join(offenders)
    )
