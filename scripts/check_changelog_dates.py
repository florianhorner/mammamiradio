#!/usr/bin/env python3
"""Check both changelogs against GitHub release publication dates (Europe/Berlin).

Requires Python 3.11+ and IANA timezone data; live checks also need authenticated gh.
Live data always comes from florianhorner/mammamiradio on github.com.
--repo-root changes local inputs only. --releases-json accepts a release array or
the page arrays produced by gh api --paginate --slurp, without network access.

Exit 0: both files audited and matching; 1: wrong/missing dates; 2: audit unavailable
or invalid. Drafts, Unreleased and headings without a published release are skipped.
This checks existing headings, not release-history completeness.

After publication, run this check and correct only the reported heading dates in
a corrective PR. Cut-time dates are provisional until publication. Scheduled checks
are best-effort; the maintainer responds to failed jobs (no automatic repair).

Offline self-test:
  python3.11 tests/scripts/test_check_changelog_dates.py -v
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CHANGELOGS = ("CHANGELOG.md", "ha-addon/mammamiradio/CHANGELOG.md")
TIMEZONE = "Europe/Berlin"
RELEASES_ENDPOINT = "repos/florianhorner/mammamiradio/releases?per_page=100"
VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
HEADING = re.compile(rf"^(?:\[({VERSION})\]|({VERSION}))(?=\s|$)(.*)$")


class AuditError(Exception):
    """The inputs do not permit a complete publication-date audit."""


def load_releases(fixture: Path | None) -> object:
    if fixture is not None:
        return json.loads(fixture.read_text(encoding="utf-8"))
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--hostname",
                "github.com",
                "--method",
                "GET",
                "--paginate",
                "--slurp",
                RELEASES_ENDPOINT,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise AuditError("gh is missing; install GitHub CLI or use --releases-json FILE") from exc
    except subprocess.TimeoutExpired as exc:
        raise AuditError("GitHub did not answer within 30 seconds; retry or use --releases-json FILE") from exc
    except subprocess.CalledProcessError as exc:
        # gh stderr can contain environment-specific details; do not echo it.
        raise AuditError(
            "GitHub release retrieval failed; check gh auth status, retry after service/rate-limit "
            "recovery, or use --releases-json FILE"
        ) from exc
    return json.loads(result.stdout)


def release_dates(data: object) -> dict[str, date]:
    if not isinstance(data, list):
        raise AuditError("release JSON must be an array of releases or page arrays")
    if data and all(isinstance(page, list) for page in data):
        data = [release for page in data for release in page]
    zone = ZoneInfo(TIMEZONE)
    dates: dict[str, date] = {}
    for release in data:
        if not isinstance(release, dict):
            raise AuditError("release JSON contains a non-object release or mixed page shapes")
        tag = release.get("tag_name")
        draft = release.get("draft")
        if not isinstance(tag, str) or not tag or not isinstance(draft, bool):
            raise AuditError("each release needs a nonempty tag_name and a boolean draft field")
        if draft:
            continue
        version = tag.removeprefix("v")
        if not re.fullmatch(VERSION, version):
            continue  # Edge/non-version tags do not describe these release headings.
        if version in dates:
            raise AuditError(f"multiple published releases normalize to version {version}")
        published = release.get("published_at")
        if not isinstance(published, str):
            raise AuditError(f"release {version} needs a published_at timestamp")
        try:
            instant = datetime.fromisoformat(published)
            if instant.utcoffset() is None:
                raise ValueError("timestamp has no timezone")
            dates[version] = instant.astimezone(zone).date()
        except (ValueError, OverflowError) as exc:
            raise AuditError(f"release {version} has an invalid or timezone-free published_at") from exc
    if not dates:
        raise AuditError("no usable published version releases; cannot certify an empty audit")
    return dates


def audit_changelog(root: Path, name: str, dates: dict[str, date]) -> tuple[list[str], int, int]:
    findings: list[str] = []
    checked = skipped = 0
    fence = ""
    for line_number, line in enumerate((root / name).read_text(encoding="utf-8").splitlines(), 1):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*", line):
                fence = ""
            continue
        if marker:
            fence = marker[1]
            continue
        heading = re.match(r"^ {0,3}##[ \t]+(.*?)\s*$", line)
        if not heading:
            continue
        if heading[1] in ("Unreleased", "[Unreleased]"):
            skipped += 1
            continue
        versioned = HEADING.fullmatch(heading[1])
        if not versioned:
            raise AuditError(f"{name}:{line_number}: unrecognized release heading; restore the version format")
        version = versioned[1] or versioned[2]
        if version not in dates:
            skipped += 1
            continue
        checked += 1
        suffix = versioned[3].strip()
        actual = suffix.removeprefix("-").strip() or "<missing>"
        expected = dates[version].isoformat()
        if suffix != f"- {expected}":
            findings.append(
                f"{name}:{line_number}: {version}: date {actual}; expected {expected}; heading suffix {suffix!r}"
            )
    if not checked:
        raise AuditError(f"{name}: no headings match published releases; cannot certify an empty audit")
    return findings, checked, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--releases-json", type=Path, metavar="FILE", help="audit offline release JSON")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        metavar="PATH",
        help="directory containing both canonical changelogs (default: this checkout)",
    )
    args = parser.parse_args(argv)
    try:
        dates = release_dates(load_releases(args.releases_json))
        audits = [(name, audit_changelog(args.repo_root, name, dates)) for name in CHANGELOGS]
    except ZoneInfoNotFoundError:
        print(f"ERROR: {TIMEZONE} timezone data is missing; install system tzdata. See --help.", file=sys.stderr)
        return 2
    except (AuditError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}. Fix the input/source and retry; see --help.", file=sys.stderr)
        return 2

    print(f"Publication calendar: {TIMEZONE} (GitHub published_at)")
    findings = []
    for name, (problems, checked, skipped) in audits:
        print(f"{name}: {checked} checked, {skipped} skipped")
        findings.extend(problems)
    if findings:
        print("\n".join(findings))
        print(f"Found {len(findings)} date finding(s); correct only the reported heading dates.")
        return 1
    print("Changelog publication dates match.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
