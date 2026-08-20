"""Keep current operator docs aligned with the B-transient media boundary."""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_canonical_music_source_guide_records_the_rights_boundaries() -> None:
    guide = _read("docs/music-sources.md")
    flat_guide = " ".join(guide.split())

    # Derived from the manifest rather than hardcoded. This guide is the
    # project's public rights statement, and a hardcoded list let it keep
    # naming twelve tracks that had been removed from the bundle — the doc was
    # wrong in every particular while its own guard stayed green. Reading the
    # catalog means the two cannot drift again.
    catalog = json.loads(_read("mammamiradio/assets/starter/catalog.json"))
    titles = [row["title"].strip() for row in catalog["tracks"]]
    assert len(titles) == 12, "the guide describes a twelve-track bundle"
    for track in titles:
        assert track in flat_guide, f"{track!r} is bundled but absent from the rights guide"

    # Both licence tiers must be stated: they differ by provider, and a reader
    # cannot comply with a licence the doc does not name.
    for license_id in {row["license"]["id"] for row in catalog["tracks"]}:
        version = license_id.rsplit("-", 1)[-1]
        assert f"CC BY {version}" in flat_guide, f"{license_id} is bundled but unstated in the rights guide"
    for boundary in (
        "default-off",
        "provider confirmation",
        "`audiodownload` is never used",
        "never enter the persistent cache, SQLite",
        "Both current Home Assistant add-ons omit yt-dlp entirely",
        "`403 music_share_unavailable`",
        "exactly 12 approved derivatives",
        "at least 45 minutes",
        "75 MiB",
        "20 cold Home Assistant Green runs",
        "p95 no slower than two seconds",
    ):
        assert boundary in flat_guide


def test_current_addon_guides_do_not_reintroduce_the_old_chart_boot_contract() -> None:
    current = "\n".join(
        _read(path)
        for path in (
            "ha-addon/README.md",
            "ha-addon/mammamiradio/DOCS.md",
            "README.md",
            "docs/operations.md",
            "docs/troubleshooting.md",
            "docs/architecture.md",
            "docs/conductor.md",
            "docs/runbooks/ha-addon.md",
            "CLAUDE.md",
        )
    )
    stale_claims = (
        "First boot can take 30-90 seconds while chart tracks are downloaded",
        "Demo Mode does not bundle a song library",
        "run.sh enables yt-dlp",
        "full music rotation still needs live-chart access",
        "reachable charts or Jamendo still provide the music",
        "charts → Jamendo → local `music/` → bundled demo assets",
        "MAMMAMIRADIO_ALLOW_YTDLP=true` | run.sh",
        "sets `MAMMAMIRADIO_ALLOW_YTDLP=true` by default",
        "`jamendo_client_id` | `password?`",
    )
    for claim in stale_claims:
        assert claim not in current
    assert "docs/music-sources.md" in current
    assert "Both add-ons" in current and "Neither image" in current
    assert "Jamendo transient provider" in current
    assert "MAMMAMIRADIO_ALLOW_YTDLP=false" in current


def test_unreleased_changelogs_share_the_media_boundary() -> None:
    root = _read("CHANGELOG.md").split("## 2.", 1)[0]
    addon = _read("ha-addon/mammamiradio/CHANGELOG.md").split("## 2.", 1)[0]
    for statement in (
        "rights-aware offline starter-catalog contract",
        "Release remains intentionally blocked",
        "Jamendo is available as an explicit transient music source",
        "External extraction is now a standalone opt-in capability",
        "Music sharing now fails closed around the eligible bundled window",
    ):
        assert statement in root
        assert statement in addon
