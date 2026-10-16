"""Keep the root agent guide small, its cited headings present, and its doc paths real."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDE = REPO_ROOT / "CLAUDE.md"

# The guide loads into every agent session, so reference material lives in docs/.
# Raising this ceiling is the deliberate edit that says a new rule needs the room;
# moving prose out is the default.
MAX_BYTES = 40_000

# Headings and labels that scripts, tests, and docs cite by name.
REQUIRED_HEADINGS = (
    "## Quality gates",
    "**Landing contract**",
    "**Model registry watch**",
    "## Changelog editorial boundary",
    "## Audio delivery test coverage rule",
    "## Protected UI elements",
    "## Commands",
    "## Doc sync",
    "## No-touch zones",
    "## Scope discipline",
    "<!-- BEGIN: commit-message-standards",
    "<!-- END: commit-message-standards -->",
)

_BACKTICKED_REPO_PATH = re.compile(r"`((?:docs|scripts|tests|\.github)/[A-Za-z0-9_./-]+)`")
# Placeholders in cited paths, plus the backlog file names the guide forbids rather
# than cites (scripts/check-no-backlog-files.sh relies on that sentence).
_SKIP_MARKERS = ("*", "<", "{", "YYYY", "NNN")
_FORBIDDEN_NAMES = frozenset({"docs/todos.md", "docs/backlog.md"})


def _guide_text() -> str:
    return GUIDE.read_text(encoding="utf-8")


def test_guide_stays_within_budget() -> None:
    size = GUIDE.stat().st_size
    assert size <= MAX_BYTES, (
        f"CLAUDE.md is {size} bytes, above the {MAX_BYTES} byte budget. Move reference "
        "material to docs/operations.md, docs/architecture.md, or CONTRIBUTING.md and "
        "leave a pointer; raise MAX_BYTES only for a new rule that needs the room."
    )


def test_required_headings_survive() -> None:
    text = _guide_text()
    for heading in REQUIRED_HEADINGS:
        count = text.count(heading)
        assert count == 1, f"{heading!r} must appear exactly once in CLAUDE.md, found {count}"


def test_backticked_repo_paths_exist() -> None:
    text = _guide_text()
    checked = 0
    for match in _BACKTICKED_REPO_PATH.finditer(text):
        token = match.group(1)
        if token in _FORBIDDEN_NAMES or any(marker in token for marker in _SKIP_MARKERS):
            continue
        checked += 1
        assert (REPO_ROOT / token).exists(), f"CLAUDE.md cites `{token}`, which does not exist"
    assert checked > 0, "no backticked docs/scripts/tests/.github paths found; the regex stopped matching"
