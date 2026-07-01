#!/usr/bin/env python3
"""Validate the source release-beat manifest before PRs and releases."""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on Python < 3.11
    try:
        import tomli as tomllib
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise SystemExit("ERROR: Python 3.11+ or the tomli package is required") from exc

DEFAULT_MANIFEST = Path("mammamiradio/assets/release/release_beat.toml")
DEFAULT_PYPROJECT = Path("pyproject.toml")

VALID_CHANNELS = {"edge", "stable"}
VALID_PRIORITIES = {"low", "normal", "high"}

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{5,120}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

# These are release-engineering words, not on-air words. A manifest may opt into
# one explicitly through listener_safe_terms, but the default is radio-safe copy.
UNSAFE_TERMS = {
    "version": re.compile(r"\bversions?\b", re.IGNORECASE),
    "commit": re.compile(r"\bcommits?\b", re.IGNORECASE),
    "dependency": re.compile(r"\bdependencies?\b|\bdependency\b", re.IGNORECASE),
    "github": re.compile(r"\bGitHub\b", re.IGNORECASE),
    "pull request": re.compile(r"\bpull requests?\b", re.IGNORECASE),
    "pr": re.compile(r"\bPRs?\b"),
    "ci": re.compile(r"\bCI\b"),
    "sha": re.compile(r"\bSHAs?\b", re.IGNORECASE),
    "semver": re.compile(r"\bsemver\b", re.IGNORECASE),
    "docker": re.compile(r"\bDocker\b", re.IGNORECASE),
}

ALLOWED_KEYS = {
    "enabled",
    "schema",
    "id",
    "channel",
    "build_sha",
    "semver",
    "priority",
    "facts",
    "props",
    "avoid",
    "copy",
    "listener_safe_terms",
}


class ManifestState:
    MISSING = "missing"
    DISABLED = "disabled"
    ENABLED = "enabled"


def _read_toml(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")), None
    except tomllib.TOMLDecodeError as exc:
        return None, f"{path}: invalid TOML: {exc}"
    except OSError as exc:
        return None, f"{path}: could not read file: {exc}"


def _manifest_package_relative_path(manifest_path: Path) -> str:
    parts = manifest_path.as_posix().split("/")
    if "mammamiradio" in parts:
        package_index = parts.index("mammamiradio")
        return "/".join(parts[package_index + 1 :])
    return manifest_path.as_posix()


def _validate_package_data(pyproject_path: Path, manifest_path: Path, errors: list[str]) -> None:
    pyproject, error = _read_toml(pyproject_path)
    if error:
        errors.append(error)
        return
    assert pyproject is not None

    package_data = pyproject.get("tool", {}).get("setuptools", {}).get("package-data", {}).get("mammamiradio")
    if not isinstance(package_data, list) or not all(isinstance(item, str) for item in package_data):
        errors.append("pyproject.toml must define [tool.setuptools.package-data].mammamiradio")
        return

    relative_manifest = _manifest_package_relative_path(manifest_path)
    if not any(fnmatch.fnmatchcase(relative_manifest, pattern) for pattern in package_data):
        errors.append(
            "pyproject.toml package-data for mammamiradio must include "
            f"{relative_manifest!r} (current patterns: {package_data!r})"
        )


def _validate_string_field(
    release_beat: dict[str, Any],
    field: str,
    errors: list[str],
    *,
    required: bool = True,
) -> str | None:
    value = release_beat.get(field)
    if value is None:
        if required:
            errors.append(f"release_beat.{field} is required")
        return None
    if not isinstance(value, str) or not value.strip():
        errors.append(f"release_beat.{field} must be a non-empty string")
        return None
    return value.strip()


def _validate_safe_terms(release_beat: dict[str, Any], errors: list[str]) -> set[str]:
    raw_terms = release_beat.get("listener_safe_terms", [])
    if not isinstance(raw_terms, list) or not all(isinstance(item, str) and item.strip() for item in raw_terms):
        errors.append("release_beat.listener_safe_terms must be a list of non-empty strings")
        return set()

    safe_terms = {item.strip().lower() for item in raw_terms}
    unknown = safe_terms - set(UNSAFE_TERMS)
    if unknown:
        errors.append("release_beat.listener_safe_terms contains unknown term(s): " + ", ".join(sorted(unknown)))
    return safe_terms


def _unsafe_terms(text: str, safe_terms: set[str]) -> list[str]:
    return [term for term, pattern in UNSAFE_TERMS.items() if term not in safe_terms and pattern.search(text)]


def _validate_text_list(
    release_beat: dict[str, Any],
    field: str,
    errors: list[str],
    *,
    required: bool,
    min_items: int,
    max_items: int,
    max_chars: int,
    scan_listener_safe: bool,
    safe_terms: set[str],
) -> None:
    value = release_beat.get(field)
    if value is None:
        if required:
            errors.append(f"release_beat.{field} is required")
        return
    if not isinstance(value, list):
        errors.append(f"release_beat.{field} must be a list of strings")
        return
    if not min_items <= len(value) <= max_items:
        errors.append(f"release_beat.{field} must contain {min_items}-{max_items} item(s)")
        return

    seen: set[str] = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str):
            errors.append(f"release_beat.{field}[{index}] must be a string")
            continue
        text = item.strip()
        if not text:
            errors.append(f"release_beat.{field}[{index}] must not be blank")
            continue
        if "\n" in text or "\r" in text:
            errors.append(f"release_beat.{field}[{index}] must be one line")
        if len(text) > max_chars:
            errors.append(f"release_beat.{field}[{index}] must be <= {max_chars} characters")
        if re.search(r"\b(TODO|TBD|FIXME|placeholder|lorem ipsum)\b", text, re.IGNORECASE):
            errors.append(f"release_beat.{field}[{index}] contains placeholder copy")
        normalized = re.sub(r"\s+", " ", text).casefold()
        if normalized in seen:
            errors.append(f"release_beat.{field}[{index}] duplicates another {field} item")
        seen.add(normalized)
        if scan_listener_safe:
            terms = _unsafe_terms(text, safe_terms)
            if terms:
                errors.append(f"release_beat.{field}[{index}] contains listener-unsafe term(s): " + ", ".join(terms))


def _sha_matches(manifest_sha: str, target_sha: str) -> bool:
    manifest = manifest_sha.lower()
    target = target_sha.lower()
    return manifest == target or manifest.startswith(target) or target.startswith(manifest)


def _validate_enabled_manifest(
    release_beat: dict[str, Any],
    manifest_path: Path,
    pyproject_path: Path,
    *,
    target_channel: str | None,
    target_sha: str | None,
    target_semver: str | None,
) -> list[str]:
    errors: list[str] = []

    unknown_keys = sorted(set(release_beat) - ALLOWED_KEYS)
    if unknown_keys:
        errors.append("release_beat contains unknown key(s): " + ", ".join(unknown_keys))

    schema = release_beat.get("schema")
    if schema is not None and schema != 1:
        errors.append("release_beat.schema must be 1 when present")

    beat_id = _validate_string_field(release_beat, "id", errors)
    if beat_id is not None and not ID_RE.fullmatch(beat_id):
        errors.append("release_beat.id must use 6-121 lowercase letters, digits, dots, underscores, or hyphens")

    channel = _validate_string_field(release_beat, "channel", errors)
    if channel is not None and channel not in VALID_CHANNELS:
        errors.append('release_beat.channel must be "edge" or "stable"')

    build_sha = _validate_string_field(release_beat, "build_sha", errors, required=channel == "edge")
    if build_sha is not None and not SHA_RE.fullmatch(build_sha):
        errors.append("release_beat.build_sha must be a 7-40 character hexadecimal git SHA")

    semver = _validate_string_field(release_beat, "semver", errors, required=channel == "stable")
    if semver is not None and not SEMVER_RE.fullmatch(semver):
        errors.append("release_beat.semver must be a semantic version like 2.15.0")

    if beat_id is not None:
        for target_name, target_value in (("build_sha", build_sha), ("semver", semver)):
            if target_value is not None and beat_id == target_value:
                errors.append(f"release_beat.id must be globally unique, not just the {target_name}")

    priority = release_beat.get("priority", "normal")
    if not isinstance(priority, str) or priority not in VALID_PRIORITIES:
        errors.append('release_beat.priority must be one of "low", "normal", or "high"')

    safe_terms = _validate_safe_terms(release_beat, errors)
    _validate_text_list(
        release_beat,
        "facts",
        errors,
        required=True,
        min_items=1,
        max_items=5,
        max_chars=180,
        scan_listener_safe=True,
        safe_terms=safe_terms,
    )
    _validate_text_list(
        release_beat,
        "props",
        errors,
        required=True,
        min_items=1,
        max_items=5,
        max_chars=160,
        scan_listener_safe=True,
        safe_terms=safe_terms,
    )
    _validate_text_list(
        release_beat,
        "copy",
        errors,
        required=False,
        min_items=1,
        max_items=4,
        max_chars=220,
        scan_listener_safe=True,
        safe_terms=safe_terms,
    )
    _validate_text_list(
        release_beat,
        "avoid",
        errors,
        required=False,
        min_items=0,
        max_items=12,
        max_chars=160,
        scan_listener_safe=False,
        safe_terms=safe_terms,
    )

    if target_channel is not None and channel != target_channel:
        errors.append(f"release_beat.channel must be {target_channel!r} for this release gate (got {channel!r})")
    if (
        target_channel == "edge"
        and build_sha is not None
        and target_sha is not None
        and not _sha_matches(build_sha, target_sha)
    ):
        errors.append(f"release_beat.build_sha {build_sha!r} does not match selected edge target {target_sha!r}")
    if target_channel == "stable" and semver is not None and target_semver is not None and semver != target_semver:
        errors.append(f"release_beat.semver {semver!r} does not match stable release {target_semver!r}")

    _validate_package_data(pyproject_path, manifest_path, errors)
    return errors


def _load_manifest(manifest_path: Path) -> tuple[str, dict[str, Any] | None, list[str]]:
    if not manifest_path.exists():
        return ManifestState.MISSING, None, []

    manifest, error = _read_toml(manifest_path)
    if error:
        return ManifestState.ENABLED, None, [error]
    assert manifest is not None

    release_beat = manifest.get("release_beat")
    if not isinstance(release_beat, dict):
        return ManifestState.ENABLED, None, ["manifest must contain a [release_beat] table"]

    enabled = release_beat.get("enabled", True)
    if not isinstance(enabled, bool):
        return ManifestState.ENABLED, release_beat, ["release_beat.enabled must be true or false"]
    if not enabled:
        return ManifestState.DISABLED, release_beat, []
    return ManifestState.ENABLED, release_beat, []


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate mammamiradio/assets/release/release_beat.toml in generic, edge-target, or stable-target mode."
        )
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help=f"default: {DEFAULT_MANIFEST}")
    parser.add_argument("--pyproject", type=Path, default=DEFAULT_PYPROJECT, help=f"default: {DEFAULT_PYPROJECT}")
    parser.add_argument("--channel", choices=sorted(VALID_CHANNELS), help="release target to validate against")
    parser.add_argument("--target-sha", help="selected edge build SHA; implies --channel edge")
    parser.add_argument("--semver", help="stable semantic version; implies --channel stable")
    args = parser.parse_args(argv)

    if args.target_sha and args.semver:
        parser.error("--target-sha and --semver are mutually exclusive")
    if args.target_sha and not SHA_RE.fullmatch(args.target_sha):
        parser.error("--target-sha must be a 7-40 character hexadecimal git SHA")
    if args.semver and not SEMVER_RE.fullmatch(args.semver):
        parser.error("--semver must be a semantic version like 2.15.0")

    implied_channel = "edge" if args.target_sha else "stable" if args.semver else None
    if args.channel and implied_channel and args.channel != implied_channel:
        implied_flag = "target-sha" if implied_channel == "edge" else "semver"
        parser.error(f"--{implied_flag} implies --channel {implied_channel}")
    if not args.channel:
        args.channel = implied_channel
    if args.channel == "edge" and not args.target_sha:
        parser.error("--channel edge requires --target-sha")
    if args.channel == "stable" and not args.semver:
        parser.error("--channel stable requires --semver")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    state, release_beat, errors = _load_manifest(args.manifest)
    if errors:
        print("ERROR: release beat manifest invalid:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    if state == ManifestState.MISSING:
        print(f"release beat: no manifest at {args.manifest}; no-op")
        return 0
    if state == ManifestState.DISABLED:
        print(f"release beat: disabled in {args.manifest}; no-op")
        return 0

    assert release_beat is not None
    errors = _validate_enabled_manifest(
        release_beat,
        args.manifest,
        args.pyproject,
        target_channel=args.channel,
        target_sha=args.target_sha,
        target_semver=args.semver,
    )
    if errors:
        print("ERROR: release beat manifest invalid:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    channel = release_beat["channel"]
    if args.channel == "edge":
        print(f"release beat: {channel} manifest OK for target {args.target_sha}")
    elif args.channel == "stable":
        print(f"release beat: {channel} manifest OK for release {args.semver}")
    else:
        print(f"release beat: {channel} manifest OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
