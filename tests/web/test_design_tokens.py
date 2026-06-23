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
BASE_CSS = REPO_ROOT / "mammamiradio" / "web" / "static" / "base.css"
ADMIN_HTML = REPO_ROOT / "mammamiradio" / "web" / "templates" / "admin.html"
_ADMIN_HTML_TEXT = ADMIN_HTML.read_text(encoding="utf-8")
LISTENER_CSS = REPO_ROOT / "mammamiradio" / "web" / "static" / "listener.css"
SYSTEM_MD = REPO_ROOT / "docs" / "design" / "system.md"
CLIP_HTML = REPO_ROOT / "mammamiradio" / "web" / "templates" / "clip.html"
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
        "--ok-text",
        "--error",
        "--warning",
        "--flag-green",
        "--flag-white",
        "--flag-red",
        "--font-display",
        "--font-body",
        "--font-mono",
        "--seg-music-text",
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


def _css_root_declarations(text: str) -> dict[str, str]:
    match = _ROOT_BLOCK_RE.search(_strip_comments(text))
    assert match, "CSS text must contain a :root { ... } block."
    return _css_declarations(match.group(1))


def _normalized_css_value(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


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
        "tokens.css must define --seg-ad (the ad-segment color, distinct from --warning / degraded status)."
    )
    tokens_text = _strip_comments(TOKENS_CSS.read_text(encoding="utf-8"))
    assert not re.search(r"--seg-ad\s*:\s*var\(\s*--warning\s*\)", tokens_text), (
        "--seg-ad must not alias --warning — that re-creates the ad/degraded color collision."
    )

    text = _strip_comments(_ADMIN_HTML_TEXT)
    ad_rule_re = re.compile(r'[^\n{}]*(?:\[data-t="ad"\]|\[data-type="ad"\]|\.segment-ad)[^\n{}]*\{[^}]*\}')
    offenders = [m.group(0).strip() for m in ad_rule_re.finditer(text) if "var(--warning)" in m.group(0)]
    assert not offenders, (
        "ad-segment rules must use var(--seg-ad), not var(--warning) (degraded-status color):\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    ("segment", "token", "forbidden", "forbidden_label"),
    [
        ("music", "--seg-music", ("--ok",), "--ok (OK/connected/playing status blue)"),
        ("banter", "--seg-banter", ("--sun", "--sun2"), "--sun / --sun2 (accent golds)"),
    ],
)
def test_segment_colors_are_decoupled_from_semantic_and_accent_tokens(
    segment: str, token: str, forbidden: tuple[str, ...], forbidden_label: str
) -> None:
    """music/banter must own dedicated --seg-* tokens, never semantic/accent ones.

    Reusing --ok for music meant the OK/connected status blue also meant
    "music segment"; reusing --sun/--sun2 for banter meant the accent gold also
    meant "banter" (and banter flip-flopped between the two golds). A scrub that
    later retuned --ok or --sun for status/accent reasons would silently shift
    segment colours. Decouple via dedicated tokens and forbid re-aliasing —
    mirrors the --seg-ad ≠ --warning guard. The colorblind-safe pairing still
    holds: every segment also carries its type label text.
    """
    assert token in _primitives_in(TOKENS_CSS), f"tokens.css must define {token} (the {segment}-segment color)."

    tokens_text = _strip_comments(TOKENS_CSS.read_text(encoding="utf-8"))
    for bad in forbidden:
        assert not re.search(rf"{re.escape(token)}\s*:\s*var\(\s*{re.escape(bad)}\s*\)", tokens_text), (
            f"{token} must not alias var({bad}) — that re-couples {segment} to {forbidden_label}."
        )

    text = _strip_comments(_ADMIN_HTML_TEXT)
    rule_re = re.compile(
        rf'[^\n{{}}]*(?:\[data-t="{segment}"\]|\[data-type="{segment}"\]|\.segment-{segment})[^\n{{}}]*\{{[^}}]*\}}'
    )
    bad_refs = {f"var({bad})" for bad in forbidden}
    offenders = [m.group(0).strip() for m in rule_re.finditer(text) if any(ref in m.group(0) for ref in bad_refs)]
    assert not offenders, f"{segment}-segment rules must use var({token}), not {forbidden_label}:\n" + "\n".join(
        offenders
    )


# Music is the DEFAULT segment, so its progress fills are colored by the
# unqualified base `.progress-fill` rule (no [data-t="music"] selector). The
# parametrized guard above keys on explicit `music` selectors, so it cannot see
# these: a revert of one base rule to var(--ok) would slip past it. Guard the
# base fills explicitly so the decoupling holds end to end.
_MUSIC_DEFAULT_FILL_SELECTORS = [
    ".a-now-compact .progress-fill",
    ".on-air-progress-track .progress-fill",
]


def _base_rule_block(text: str, selector: str) -> str:
    """Return the body of the rule whose selector is EXACTLY `selector`.

    Anchored at a rule boundary (start-of-line or after a `}`) so a longer
    compound selector that ends in the same string (e.g.
    `body[data-fader-down="true"] .on-air-progress-track .progress-fill`) cannot
    shadow the base rule via a substring match.
    """
    match = re.search(
        rf"(?:^|\}})\s*{re.escape(selector)}\s*\{{([^}}]*)\}}",
        _strip_comments(text),
        re.MULTILINE,
    )
    assert match, f"base rule not found for selector: {selector}"
    return match.group(1)


@pytest.mark.parametrize("selector", _MUSIC_DEFAULT_FILL_SELECTORS)
def test_default_music_fill_uses_seg_music(selector: str) -> None:
    """The default (music) progress-fill base rules must use var(--seg-music)."""
    background = _css_declarations(_base_rule_block(_ADMIN_HTML_TEXT, selector)).get("background")
    assert background == "var(--seg-music)", (
        f"{selector} colors the default (music) segment and must use var(--seg-music), "
        f"not a semantic/accent token. Got {background!r}. (--ok is the status blue; "
        f"reusing it re-couples music to OK/connected status.)"
    )


def test_listener_now_playing_fill_uses_seg_music() -> None:
    """The listener now-playing progress fill is music-semantic.

    Mirrors the admin default-fill guard on the listener surface: .mmr-np-fill
    must use var(--seg-music), never var(--ok) (the OK/connected status blue).
    Reusing --ok here re-couples "music is playing" to "system is OK" — the same
    drift the admin guard closes, previously unguarded on the listener side.
    """
    background = _css_declarations(_css_block(LISTENER_CSS.read_text(encoding="utf-8"), ".mmr-np-fill")).get(
        "background"
    )
    assert background == "var(--seg-music)", (
        f".mmr-np-fill colors the now-playing (music) progress and must use var(--seg-music), got {background!r}."
    )


# Eyebrow floor from docs/design/system.md, Typography (9-10px). Below this,
# uppercase labels stop rendering legibly on the dense admin surface (the
# regression caught after the design-review scrub: 5.5px preset axis initials,
# 8px section labels).
_FONT_SIZE_PX_RE = re.compile(r"font-size:\s*([0-9]+(?:\.[0-9]+)?)px")
_EYEBROW_FLOOR_PX = 9.0


def test_admin_inline_style_has_no_sub_floor_font_size() -> None:
    """No font-size in admin.html may fall below the 9px legibility floor."""
    text = _strip_comments(_ADMIN_HTML_TEXT)
    offenders = sorted({m.group(1) for m in _FONT_SIZE_PX_RE.finditer(text) if float(m.group(1)) < _EYEBROW_FLOOR_PX})
    assert not offenders, (
        "admin.html declares font-size below the 9px eyebrow floor "
        f"(docs/design/system.md § Typography): {', '.join(f'{v}px' for v in offenders)}. "
        "Raise to >=9px or remove the label."
    )


# test_rotation_sort_is_mono_typeface removed: the "order: current rotation"
# readout it guarded was a dead, non-interactive label (no sort control behind it)
# and was removed in the Concept B producer-desk redesign.


def test_listener_card_surfaces_use_card_tokens_not_magic_literals() -> None:
    """Listener card surfaces must flow through --card* tokens, not inline literals.

    The schedule / dedica / about-card / now-playing surfaces were inlined as
    magic hex (#54453A, #6E5B49) plus an rgba(...,0.32) border, guarded only by
    a "do not raise --surface*" comment. They now use the dedicated --card /
    --card-strong / --card-line tokens. Guard: those literals never return to
    listener.css, or the next editor re-opens the drift.
    """
    text = _strip_comments(LISTENER_CSS.read_text(encoding="utf-8")).upper()
    offenders = [hex_ for hex_ in ("#54453A", "#6E5B49") if hex_ in text]
    assert not offenders, (
        f"listener.css reintroduces magic card-surface hex {offenders}; "
        "use var(--card) / var(--card-strong) (defined in tokens.css) instead."
    )
    compact = re.sub(r"\s+", "", text)
    assert "RGBA(245,237,216,0.32)" not in compact, (
        "listener.css reintroduces the magic card-border rgba literal; use var(--card-line) instead."
    )
    assert {"--card", "--card-strong", "--card-line"} <= set(_primitives_in(TOKENS_CSS)), (
        "tokens.css must define --card / --card-strong / --card-line."
    )
    expected_declarations = {
        ".mmr-stage": {"border": "1px solid var(--card-line)"},
        ".mmr-np-bar": {"background": "var(--card-strong)"},
        ".btn-ghost": {"border": "1px solid var(--card-line)"},
        ".mmr-schedule": {"background": "var(--card)", "border": "1px solid var(--card-line)"},
        ".mmr-dedica": {"background": "var(--card-strong)", "border": "1px solid var(--card-line)"},
        ".mmr-about-card": {"background": "var(--card-strong)", "border": "1px solid var(--card-line)"},
    }
    listener_css = LISTENER_CSS.read_text(encoding="utf-8")
    for selector, declarations in expected_declarations.items():
        actual = _css_declarations(_css_block(listener_css, selector))
        for property_name, expected_value in declarations.items():
            assert actual.get(property_name) == expected_value, (
                f"{selector} {property_name} must use {expected_value}, got {actual.get(property_name)!r}"
            )


def _system_md_root_primitives() -> list[str]:
    """Token names declared in the first system.md :root code block (the mirror)."""
    text = _strip_comments(SYSTEM_MD.read_text(encoding="utf-8"))
    match = _ROOT_BLOCK_RE.search(text)
    assert match, "system.md must document a :root { ... } block."
    seen: set[str] = set()
    out: list[str] = []
    for name in _PRIMITIVE_DECL_RE.findall(match.group(1)):
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def test_system_md_root_tokens_exist_in_tokens_css() -> None:
    """Every token documented in system.md's :root must exist in tokens.css.

    system.md mirrors tokens.css; the file is the source of truth. This catches
    the drift where a token is deleted or renamed in tokens.css but left
    documented in the design spec, so the doc quietly describes a token the
    browser never sees. Name parity only (values may carry doc-side annotation).
    """
    defined = set(_primitives_in(TOKENS_CSS))
    missing = [
        name for name in _system_md_root_primitives() if name not in defined and not _BRAND_NAMESPACE_RE.match(name)
    ]
    assert not missing, (
        "system.md :root documents tokens absent from tokens.css: "
        f"{missing}. Update docs/design/system.md or tokens.css — the file wins."
    )


def test_system_md_root_tokens_match_tokens_css_values() -> None:
    """Documented root-token values must mirror tokens.css for tokens listed there."""
    defined = _css_root_declarations(TOKENS_CSS.read_text(encoding="utf-8"))
    documented = _css_root_declarations(SYSTEM_MD.read_text(encoding="utf-8"))
    mismatched = {
        name: {"system.md": documented[name], "tokens.css": defined[name]}
        for name in documented
        if name in defined
        and not _BRAND_NAMESPACE_RE.match(name)
        and _normalized_css_value(documented[name]) != _normalized_css_value(defined[name])
    }
    assert not mismatched, (
        f"system.md :root token values drifted from tokens.css (tokens.css is authoritative): {mismatched}"
    )


def test_tricolor_stripes_use_flag_tokens() -> None:
    """Brand tricolor stripes must use --flag-* tokens instead of hard-coded flag colors."""
    tokenized_gradients = [
        ("base.css .tricolor-stripe", BASE_CSS.read_text(encoding="utf-8"), ".tricolor-stripe"),
        ("admin.html .tricolor-stripe", _ADMIN_HTML_TEXT, ".tricolor-stripe"),
    ]
    for label, text, selector in tokenized_gradients:
        background = _css_declarations(_css_block(text, selector)).get("background", "")
        for token in ("--flag-green", "--flag-white", "--flag-red"):
            assert f"var({token})" in background, f"{label} must use var({token}), got {background!r}"

    exact_backgrounds = [
        (BASE_CSS, ".tricolor-band > div:nth-child(1)", "var(--flag-green)"),
        (BASE_CSS, ".tricolor-band > div:nth-child(2)", "var(--flag-white)"),
        (BASE_CSS, ".tricolor-band > div:nth-child(3)", "var(--flag-red)"),
        (LISTENER_CSS, ".mmr-tricolor .g", "var(--flag-green)"),
        (LISTENER_CSS, ".mmr-tricolor .w", "var(--flag-white)"),
        (LISTENER_CSS, ".mmr-tricolor .r", "var(--flag-red)"),
        (CLIP_HTML, ".tricolor-stripe > div:nth-child(1)", "var(--flag-green)"),
        (CLIP_HTML, ".tricolor-stripe > div:nth-child(2)", "var(--flag-white)"),
        (CLIP_HTML, ".tricolor-stripe > div:nth-child(3)", "var(--flag-red)"),
    ]
    for path, selector, expected in exact_backgrounds:
        block = _css_block(path.read_text(encoding="utf-8"), selector)
        actual = _css_declarations(block).get("background")
        assert actual == expected, f"{path.name} {selector} background must be {expected}, got {actual!r}"


def test_accessible_blue_text_tokens_are_documented_and_defined() -> None:
    defined = set(_primitives_in(TOKENS_CSS))
    for token in ("--ok-text", "--seg-music-text"):
        assert token in defined, f"tokens.css must define {token} for readable blue labels on dark surfaces."
    documented = _system_md_root_primitives()
    assert "--ok-text" in documented and "--seg-music-text" in documented


def test_admin_small_blue_labels_use_accessible_text_tokens() -> None:
    """Tiny admin labels need lighter blue text tokens on the dark producer desk."""
    expected_colors = {
        '.le-type[data-t="music"]': "var(--seg-music-text)",
        '.setup-steps li[data-s="done"] .setup-step-shape': "var(--ok-text)",
        ".setup-ready-badge": "var(--ok-text)",
        ".lr-badge-ok": "var(--ok-text)",
    }
    for selector, expected in expected_colors.items():
        color = _css_declarations(_css_block(_ADMIN_HTML_TEXT, selector)).get("color")
        assert color == expected, f"{selector} must use {expected}, got {color!r}."

    keys_box = re.search(r'<div\b(?=[^>]*\bid="setupKeysConfigured")[^>]*>', _ADMIN_HTML_TEXT)
    assert keys_box, "setupKeysConfigured banner must exist."
    assert "rgba(37,99,235" not in keys_box.group(0)
    assert "var(--ok)" in keys_box.group(0)

    keys_label = re.search(r'<span\b(?=[^>]*\bid="setupKeysLabel")[^>]*>', _ADMIN_HTML_TEXT)
    assert keys_label, "setupKeysLabel must exist."
    assert "color:var(--ok-text)" in keys_label.group(0).replace(" ", "")


def test_design_system_status_ready_docs_match_runtime_text_tokens() -> None:
    """Docs must keep ready label color separate from ready status-dot fills."""
    text = SYSTEM_MD.read_text(encoding="utf-8")
    ready_block = text[text.index("/* State: ready */") : text.index("/* State: working */")]
    assert ".status-chip.ready" in ready_block
    assert ".status-inline.ready" in ready_block
    assert ".status-dot.ready" in ready_block
    assert "var(--ok-text)" in ready_block
    assert ".status-dot.ready          { color: var(--ok); }" in ready_block


def test_admin_section_labels_keep_quiet_hierarchy() -> None:
    """Nested admin labels must not all use the same loud gold eyebrow treatment."""
    global_eyebrow = _css_declarations(_css_block(_ADMIN_HTML_TEXT, ".eyebrow"))
    assert global_eyebrow.get("color") == "var(--muted)"
    assert global_eyebrow.get("letter-spacing") == "0.12em"
    assert global_eyebrow.get("line-height") == "1.35"

    card_label = _css_declarations(_css_block(_ADMIN_HTML_TEXT, ".card-label"))
    assert card_label.get("color") == "var(--muted)"
    assert card_label.get("letter-spacing") == "0.12em"
    assert card_label.get("line-height") == "1.35"
    assert "rgba(245, 237, 216, 0.10)" in card_label.get("border-bottom", "")

    group_label = _css_declarations(_css_block(_ADMIN_HTML_TEXT, ".drawer-section .ttl-eyebrow"))
    assert group_label.get("color") == "var(--muted)"
    assert group_label.get("letter-spacing") == "0.12em"
    assert group_label.get("line-height") == "1.35"

    panel_heading = _css_declarations(_css_block(_ADMIN_HTML_TEXT, ".mmr-panel-head h2"))
    assert panel_heading.get("line-height") == "1.22"
    assert panel_heading.get("padding-top") == "2px"
