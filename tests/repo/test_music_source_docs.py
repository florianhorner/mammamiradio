"""Keep current operator docs aligned with the B-transient media boundary."""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def _bundled_titles_in_guide(guide: str) -> list[str]:
    """Track titles the guide claims are bundled, read back out of its own prose.

    Deliberately parses the same two sentences the guide uses to list them, so a
    title added or left behind in either sentence is visible to the test.
    """
    titles: list[str] = []
    for match in re.finditer(r"Six from [^:]+:(.+?)\.\n", guide, re.S):
        for item in match.group(1).split(","):
            item = re.sub(r"\s+", " ", item).strip()
            item = re.sub(r"\s*\([^)]*\)$", "", item).strip()
            if item:
                titles.append(item)
    return sorted(titles)


def test_canonical_music_source_guide_records_the_rights_boundaries() -> None:
    guide = _read("docs/music-sources.md")
    flat_guide = " ".join(guide.split())

    # Derived from the manifest rather than hardcoded. This guide is the
    # project's public rights statement, and a hardcoded list let it keep
    # naming twelve tracks that had been removed from the bundle — the doc was
    # wrong in every particular while its own guard stayed green. Reading the
    # catalog means the two cannot drift again.
    catalog = json.loads(_read("mammamiradio/assets/starter/catalog.json"))
    rows = catalog["tracks"]
    titles = [row["title"].strip() for row in rows]
    assert len(titles) == 12, "the guide describes a twelve-track bundle"

    # Both directions. Checking only that bundled titles appear would let a
    # retired one linger in the guide indefinitely, which is how the guide came
    # to describe twelve tracks that had already been replaced.
    for track in titles:
        assert track in flat_guide, f"{track!r} is bundled but absent from the rights guide"
        assert flat_guide.count(track) == 1, f"{track!r} appears more than once in the rights guide"

    guide_titles = _bundled_titles_in_guide(guide)
    assert guide_titles == sorted(titles), (
        "the rights guide lists tracks that are not bundled, or omits ones that are: "
        f"{sorted(set(guide_titles) ^ set(titles))}"
    )

    # Provider and licence must be paired. Naming both tiers somewhere is not
    # enough: a reader complying with the wrong one is no better off than a
    # reader with no statement at all.
    for provider, license_id in sorted({(r.get("provider", "incompetech"), r["license"]["id"]) for r in rows}):
        version = license_id.rsplit("-", 1)[-1]
        label = "Incompetech" if provider == "incompetech" else provider.capitalize()
        window = re.search(rf"Six from {label}[^.]*?CC BY {re.escape(version)}", flat_guide)
        assert window, f"the guide does not pair {label} with CC BY {version}"
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
        # The gate is opt-in; the guide must name the switch, not just the
        # measurement, or a reader cannot tell whether a release produced it.
        "MMR_REQUIRE_HA_RECEIPTS",
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


def test_home_assistant_music_docs_name_each_backup_home() -> None:
    for relative in (
        "ha-addon/mammamiradio/DOCS.md",
        "docs/runbooks/ha-addon.md",
        "docs/music-sources.md",
    ):
        guide = _read(relative)
        flat_guide = " ".join(guide.split())
        assert "backup includes Media" in flat_guide, relative
        assert "NAS library separately" in flat_guide, relative
        assert "`/data/music`" in guide, relative
        assert re.search(r"`/tmp/mammamiradio-data/music`[^.]{0,100}not persisted", flat_guide), relative
    assert "| `music_folder` | `str?` | `MAMMAMIRADIO_MUSIC_FOLDER`" in _read("docs/runbooks/ha-addon.md")
    assert "+-- /media/ (Home Assistant Media storage; outside the app backup)" in _read(
        "ha-addon/mammamiradio/DOCS.md"
    )


def test_v3_release_changelogs_share_the_media_boundary() -> None:
    notes = []
    for relative in ("CHANGELOG.md", "ha-addon/mammamiradio/CHANGELOG.md"):
        section = re.search(
            r"^## \[?3\.0\.0\]?(?: - [^\n]+)?\n(.*?)(?=^## |\Z)",
            _read(relative),
            re.MULTILINE | re.DOTALL,
        )
        assert section, f"{relative} is missing the 3.0.0 release entry"
        notes.append(" ".join(section.group(1).split()))
    root, addon = notes
    assert root == addon, "The two 3.0.0 release entries must describe the same boundaries"
    for statement in (
        "twelve credited tracks, about 47 minutes of offline music",
        "Other stock speech can still use online Edge voices",
        "optional expansion",
        "off until you enable it and acknowledge non-commercial API use",
        "provider confirmation for this station model remains pending",
        "Songs and breaks containing third-party music cannot be kept",
        "Home Assistant app no longer downloads YouTube tracks or live charts",
        "Standalone users who installed `external-media` should reinstall that extra",
        "Old `/data/music` songs stay where they are but do not play in this mode",
    ):
        assert statement in root
    assert "Release remains intentionally blocked" not in root
