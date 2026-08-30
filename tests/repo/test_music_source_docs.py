"""Keep operator docs and release metadata aligned with the media boundary."""

from __future__ import annotations

import json
import re
import tomllib
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


def _latest_versioned_changelog_section(relative: str) -> str:
    content = _read(relative)
    match = re.search(
        r"^## \[?\d+\.\d+\.\d+\]?[^\n]*\n.*?(?=^## \[?\d+\.\d+\.\d+\]?|\Z)",
        content,
        re.M | re.S,
    )
    assert match, f"{relative} has no versioned release section"
    return match.group(0)


def test_editable_lock_version_matches_project_version() -> None:
    project_version = tomllib.loads(_read("pyproject.toml"))["project"]["version"]
    packages = tomllib.loads(_read("uv.lock"))["package"]
    editable_project = [
        package
        for package in packages
        if package["name"] == "mammamiradio" and package.get("source") == {"editable": "."}
    ]

    assert len(editable_project) == 1
    assert editable_project[0]["version"] == project_version


def test_changelogs_reopen_unreleased_before_the_latest_release() -> None:
    root_headings = re.findall(r"^## .+$", _read("CHANGELOG.md"), re.M)
    addon_headings = re.findall(r"^## .+$", _read("ha-addon/mammamiradio/CHANGELOG.md"), re.M)

    assert root_headings[0] == "## [Unreleased]"
    assert re.fullmatch(r"## \[\d+\.\d+\.\d+\] - \d{4}-\d{2}-\d{2}", root_headings[1])
    assert addon_headings[0] == "## Unreleased"
    assert re.fullmatch(r"## \d+\.\d+\.\d+ - \d{4}-\d{2}-\d{2}", addon_headings[1])


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


def test_latest_changelogs_share_the_release_media_boundary() -> None:
    root = _latest_versioned_changelog_section("CHANGELOG.md")
    addon = _latest_versioned_changelog_section("ha-addon/mammamiradio/CHANGELOG.md")
    for statement in (
        "The add-on no longer downloads music from the internet",
        "searching the music you already have still works",
        "song request the station cannot fetch becomes a shout-out on air",
        "Twelve tracks come with the station",
        "attribution-only",
        "six from Incompetech",
        "six from Jamendo",
        "music folder is picked up without a restart",
    ):
        assert statement in root
        assert statement in addon

    # The root notes carry the standalone and sharing detail that the concise
    # add-on notes intentionally omit.
    assert "optional `external-media` package" in root
    assert "Only a complete bundled track can be shared" in root
