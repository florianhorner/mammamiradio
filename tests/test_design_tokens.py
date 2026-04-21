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


def _primitives_in(path: Path) -> list[str]:
    text = _strip_comments(path.read_text(encoding="utf-8"))
    seen: set[str] = set()
    primitives: list[str] = []
    for block in _ROOT_BLOCK_RE.finditer(text):
        for primitive in _PRIMITIVE_DECL_RE.findall(block.group(1)):
            if primitive in seen:
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
        "--font-display",
        "--font-body",
        "--font-mono",
    }
    missing = sorted(required - defined)
    assert not missing, f"mammamiradio/static/tokens.css is missing required primitives: {missing}"
