"""User-facing Home Assistant catalog claims for stable and Edge."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
STABLE_CONFIG = ROOT / "ha-addon" / "mammamiradio" / "config.yaml"
EDGE_CONFIG = ROOT / "ha-addon" / "mammamiradio-edge" / "config.yaml"


def _description(path: Path) -> str:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    return str(config["description"])


def test_stable_catalog_lists_only_packaged_music_sources() -> None:
    description = _description(STABLE_CONFIG).casefold()
    assert "jamendo" in description
    assert "live italian charts" in description
    assert "local file" not in description


def test_edge_catalog_describes_the_deliberate_release_pin() -> None:
    description = _description(EDGE_CONFIG).casefold()
    assert "deliberately cut" in description
    assert "may trail main" in description
    assert "always the latest" not in description
