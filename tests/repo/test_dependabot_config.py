"""Guards for the repository's Dependabot grouping policy."""

from fnmatch import fnmatch
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPENDABOT_CONFIG = REPO_ROOT / ".github" / "dependabot.yml"


def test_every_pip_group_excludes_major_updates() -> None:
    document = yaml.safe_load(DEPENDABOT_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "Dependabot configuration must be a mapping"

    updates = document.get("updates")
    assert isinstance(updates, list), "Dependabot configuration must define an updates list"
    assert all(isinstance(update, dict) for update in updates), "Every Dependabot update rule must be a mapping"

    pip_updates = [update for update in updates if update.get("package-ecosystem") == "pip"]

    assert len(pip_updates) == 1, "Dependabot must have one authoritative pip update rule"
    groups = pip_updates[0].get("groups")
    assert isinstance(groups, dict) and groups, "The pip update rule must retain explicit dependency groups"

    for group_name, group in groups.items():
        assert isinstance(group, dict), f"{group_name} must be a dependency-group mapping"
        update_types = group.get("update-types")
        assert isinstance(update_types, list), f"{group_name} must declare update-types"
        assert len(update_types) == 2 and set(update_types) == {"minor", "patch"}, (
            f"{group_name} must group only minor and patch updates; majors stay standalone"
        )


def _pip_update() -> dict:
    document = yaml.safe_load(DEPENDABOT_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "Dependabot configuration must be a mapping"
    updates = document.get("updates")
    assert isinstance(updates, list), "Dependabot configuration must define an updates list"
    pip_updates = [
        update for update in updates if isinstance(update, dict) and update.get("package-ecosystem") == "pip"
    ]
    assert len(pip_updates) == 1, "Dependabot must have one authoritative pip update rule"
    return pip_updates[0]


def test_pydantic_group_carries_pydantic_and_its_core() -> None:
    groups = _pip_update().get("groups")
    assert isinstance(groups, dict), "The pip update rule must define dependency groups"
    pydantic_group = groups.get("pydantic")
    assert isinstance(pydantic_group, dict), "The pip update rule must keep a pydantic group"
    patterns = pydantic_group.get("patterns")
    assert isinstance(patterns, list), "The pydantic group must declare patterns"
    assert "pydantic" in patterns, "pydantic must be grouped so its core pin moves with it"
    assert "pydantic-core" in patterns, (
        "pydantic-core must ride in the pydantic group; pydantic pins an exact core and a lone bump "
        "leaves the hash-locked requirements.txt unsatisfiable"
    )


def _patterns_overlap(ignore_pattern: str, group_pattern: str) -> bool:
    """True when either pattern, read as a literal name, matches the other.

    Dependabot accepts wildcards on both sides, so a one-way ``fnmatch`` misses an
    ignore such as ``pydantic*`` against the literal group pattern ``pydantic``. Two
    wildcards that overlap only through a third name (``pyd*`` and ``*core``) are
    not detected.
    """
    return fnmatch(ignore_pattern, group_pattern) or fnmatch(group_pattern, ignore_pattern)


def test_wildcard_ignore_overlaps_literal_group_pattern() -> None:
    assert _patterns_overlap("pydantic*", "pydantic")
    assert _patterns_overlap("pydantic", "pydantic*")
    assert _patterns_overlap("pydantic*", "pyd*")
    assert not _patterns_overlap("ruff", "pydantic*")


def test_ignored_dependencies_are_never_also_grouped() -> None:
    pip_update = _pip_update()
    ignored = pip_update.get("ignore") or []
    assert isinstance(ignored, list), "ignore must be a list when present"
    groups = pip_update.get("groups") or {}
    assert isinstance(groups, dict), "groups must be a mapping when present"

    for entry in ignored:
        assert isinstance(entry, dict), "Every ignore entry must be a mapping"
        dependency_name = entry.get("dependency-name")
        assert isinstance(dependency_name, str) and dependency_name, "Every ignore entry must name a dependency"
        for group_name, group in groups.items():
            assert isinstance(group, dict), f"{group_name} must be a dependency-group mapping"
            for pattern in group.get("patterns") or []:
                assert not _patterns_overlap(dependency_name, pattern), (
                    f"{dependency_name} is ignored but also matches group {group_name!r} pattern {pattern!r}; "
                    "an ignored dependency can never move with its group, so its grouped siblings ship a stale pin"
                )
