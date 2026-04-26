"""Design-token guard.

mammamiradio/static/tokens.css is the sole source of truth for CSS
palette primitives. This test fails if any other file reintroduces a
`:root { --foo: ...; }` primitive, which would re-open the drift the
Phase A consolidation closed.

Non-`:root` custom-property scopes (component-scoped vars inside a class)
are allowed and ignored.

See DESIGN.md § "Listener site composition — canonical" and the
Decisions Log entry dated 2026-04-21.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKENS_CSS = REPO_ROOT / "mammamiradio" / "static" / "tokens.css"
_HTML_FILES = sorted((REPO_ROOT / "mammamiradio").glob("*.html"))
_CSS_FILES = sorted((REPO_ROOT / "mammamiradio" / "static").glob("*.css"))
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
        f"mammamiradio/static/tokens.css. See DESIGN.md § "
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
    assert not missing, f"mammamiradio/static/tokens.css is missing required primitives: {missing}"


_VAR_REF_NO_FALLBACK_RE = re.compile(r"var\(\s*(--[a-z0-9-]+)\s*\)")


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
        if path.suffix != ".css":
            continue
        text = _strip_comments(path.read_text(encoding="utf-8"))
        for ref in _VAR_REF_NO_FALLBACK_RE.findall(text):
            if ref in defined:
                continue
            if _BRAND_NAMESPACE_RE.match(ref):
                continue
            missing.setdefault(path.name, set()).add(ref)
    assert not missing, (
        "CSS files reference undefined tokens with no fallback. Add them to tokens.css or use var(--foo, fallback):\n"
        + "\n".join(f"  {f}: {sorted(refs)}" for f, refs in sorted(missing.items()))
    )
