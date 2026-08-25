"""Guards for the repository's Dependabot grouping policy."""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPENDABOT_CONFIG = REPO_ROOT / ".github" / "dependabot.yml"


def test_every_pip_group_excludes_major_updates() -> None:
    document = yaml.safe_load(DEPENDABOT_CONFIG.read_text(encoding="utf-8"))
    pip_updates = [update for update in document["updates"] if update.get("package-ecosystem") == "pip"]

    assert len(pip_updates) == 1, "Dependabot must have one authoritative pip update rule"
    groups = pip_updates[0].get("groups")
    assert groups, "The pip update rule must retain explicit dependency groups"

    for group_name, group in groups.items():
        update_types = group.get("update-types")
        assert isinstance(update_types, list), f"{group_name} must declare update-types"
        assert len(update_types) == 2 and set(update_types) == {"minor", "patch"}, (
            f"{group_name} must group only minor and patch updates; majors stay standalone"
        )
