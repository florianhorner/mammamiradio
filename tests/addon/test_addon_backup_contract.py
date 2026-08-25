"""Home Assistant add-on hot-backup contract for stable and Edge."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
STABLE_CONFIG = ROOT / "ha-addon" / "mammamiradio" / "config.yaml"
EDGE_CONFIG = ROOT / "ha-addon" / "mammamiradio-edge" / "config.yaml"

EXPECTED_BACKUP_EXCLUDES = [
    "tmp",
    "cache/.ytdlp_tmp",
    "cache/restart_handoff",
    "cache/clips",
    "cache/*.mp3",
    "cache/*.mp3.json",
    "cache/*.m4a",
    "cache/*.webm",
    "*.part",
    "*.ytdl",
    "*.tmp",
]


def _backup_contract(path: Path) -> tuple[str, list[str]]:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    return config["backup"], config["backup_exclude"]


# Match behavior is pinned to Supervisor 41528557138cecd321686af16a83dd0af9d8cea8
# and securetar 64434c787e4e1486ab547ecc0f9d3b466b0b7e6b.
def _is_excluded(relative_path: str, patterns: list[str]) -> bool:
    """Model Path.match checks and directory pruning during archive traversal."""
    path = PurePosixPath(relative_path)
    assert not path.is_absolute()
    assert ".." not in path.parts

    for depth in range(1, len(path.parts) + 1):
        traversed_path = PurePosixPath(*path.parts[:depth])
        if any(traversed_path.match(pattern) for pattern in patterns):
            return True
    return False


def test_stable_and_edge_share_the_exact_hot_backup_contract() -> None:
    stable_contract = _backup_contract(STABLE_CONFIG)
    edge_contract = _backup_contract(EDGE_CONFIG)

    assert stable_contract == ("hot", EXPECTED_BACKUP_EXCLUDES)
    assert edge_contract == stable_contract


@pytest.mark.parametrize(
    "relative_path",
    [
        "tmp/segment_with_sting_80173a6f.mp3",
        "cache/.ytdlp_tmp/download.webm",
        "cache/restart_handoff/current.json",
        "cache/clips/last-hour.mp3",
        "cache/current.mp3",
        "cache/current.mp3.json",
        "cache/current.m4a",
        "cache/current.webm",
        "cache/downloads/current.part",
        "cache/downloads/current.ytdl",
        "cache/ledger/provenance-2026-07-25.jsonl.tmp",
        # Single-segment patterns ("tmp", "*.part", "*.ytdl", "*.tmp") match a
        # path's last component at ANY depth, not only at the tree root — this
        # mirrors PurePath.match() semantics for a bare pattern with no slash.
        "music/tmp/orphan_render.mp3",
        "cache/library/downloads/current.part",
    ],
)
def test_generated_or_incomplete_artifacts_are_excluded(relative_path: str) -> None:
    assert _is_excluded(relative_path, EXPECTED_BACKUP_EXCLUDES)


@pytest.mark.parametrize(
    "relative_path",
    [
        "options.json",
        "secrets.env",
        "music",
        "music/local-library.mp3",
        "cache/mammamiradio.db",
        "cache/mammamiradio.db-journal",
        "cache/mammamiradio.db-shm",
        "cache/mammamiradio.db-wal",
        "cache/blocklist.json",
        "cache/evening_ledger.json",
        "cache/moments.json",
        "cache/playlist_source.json",
        "cache/release_campaign_ledger.json",
        "cache/session_stopped.flag",
        "cache/state/home_context.json",
        "cache/ledger/provenance-2026-07-24.jsonl",
        "cache/ledger/provenance-2026-07-23.jsonl.gz",
        "tmp-retained/segment.mp3",
        "cache/.ytdlp_tmp-retained/download.webm",
        "cache/restart_handoff-retained/current.json",
        "cache/clips-retained/last-hour.mp3",
        "cache/archive/current.mp3",
        "cache/archive/current.m4a",
        "cache/archive/current.webm",
        "cache/current.mp3.json.bak",
        "cache/downloads/current.part.ready",
        "cache/downloads/current.ytdl.lock",
        "cache/ledger/provenance-2026-07-25.jsonl.tmp.ready",
        # A kept moment. "Keep this" promises a link that never expires, so
        # the file behind it has to survive a restore. `cache/*.mp3` does not
        # match a nested path, so this is included today; pinning it here
        # keeps the product's strongest durability promise from resting on an
        # incidental glob. The sidecar carries the title the restored share
        # page renders, so it is pinned alongside the audio.
        "cache/keepsakes/abc123def456.mp3",
        "cache/keepsakes/abc123def456.json",
    ],
)
def test_durable_state_and_near_misses_are_included(relative_path: str) -> None:
    assert not _is_excluded(relative_path, EXPECTED_BACKUP_EXCLUDES)
