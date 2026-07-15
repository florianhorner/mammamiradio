"""Fail-closed manifest for packaged audio that can enter speech lanes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from mammamiradio.core.listener_truth import contains_unsafe_listener_claims
from mammamiradio.core.packaged_assets import DEMO_ASSETS_DIR

MANIFEST_FILENAME = "spoken_assets.json"
DISCOVERABLE_AUDIO_SUBDIRS = ("recovery", "banter", "welcome")


@dataclass(frozen=True, slots=True)
class SpokenAssetEntry:
    """One content-addressed packaged-audio declaration."""

    relative_path: str
    sha256: str
    kind: str
    language: str
    transcript: str


def validate_spoken_asset_manifest(*, assets_root: Path = DEMO_ASSETS_DIR) -> list[str]:
    """Return all schema, inventory, hash, and listener-truth errors."""

    root = Path(assets_root)
    data, errors = _read_manifest(root)
    if data is None:
        return errors
    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    raw_assets = data.get("assets")
    if not isinstance(raw_assets, list):
        errors.append("assets must be a list")
        return errors

    declared: set[str] = set()
    for index, raw in enumerate(raw_assets):
        prefix = f"assets[{index}]"
        entry, entry_errors = _parse_entry(raw, root=root, prefix=prefix)
        errors.extend(entry_errors)
        if entry is None:
            continue
        if entry.relative_path in declared:
            errors.append(f"{prefix}.path duplicates {entry.relative_path}")
            continue
        declared.add(entry.relative_path)
        asset_path = root / entry.relative_path
        if not asset_path.is_file():
            errors.append(f"{entry.relative_path} is missing")
            continue
        try:
            actual_sha256 = _sha256(asset_path)
        except OSError as exc:
            errors.append(f"{entry.relative_path} is unreadable: {exc}")
        else:
            if actual_sha256 != entry.sha256:
                errors.append(f"{entry.relative_path} sha256 does not match")
        if entry.kind == "speech":
            if entry.language not in {"en", "it"}:
                errors.append(f"{entry.relative_path} speech language must be en or it")
            if not entry.transcript.strip():
                errors.append(f"{entry.relative_path} speech transcript is empty")
            elif contains_unsafe_listener_claims(entry.transcript):
                errors.append(f"{entry.relative_path} transcript contains listener arrival/return copy")
        elif entry.kind == "tone":
            if entry.language != "none" or entry.transcript:
                errors.append(f"{entry.relative_path} tone must use language=none and an empty transcript")
        else:
            errors.append(f"{entry.relative_path} kind must be speech or tone")

    discoverable = {
        path.relative_to(root).as_posix()
        for subdir in DISCOVERABLE_AUDIO_SUBDIRS
        for path in (root / subdir).glob("*.mp3")
        if path.is_file()
    }
    for relative_path in sorted(discoverable - declared):
        errors.append(f"{relative_path} is unlisted packaged audio")
    return errors


def approved_spoken_assets(subdir: str, *, assets_root: Path = DEMO_ASSETS_DIR) -> list[Path]:
    """Return hash-valid, truth-safe speech entries in one runtime subdirectory."""

    if subdir not in DISCOVERABLE_AUDIO_SUBDIRS:
        return []
    root = Path(assets_root)
    if validate_spoken_asset_manifest(assets_root=root):
        return []
    data, _errors = _read_manifest(root)
    if data is None:
        return []
    raw_assets = data.get("assets")
    if not isinstance(raw_assets, list):
        return []
    approved: list[Path] = []
    for index, raw in enumerate(raw_assets):
        entry, entry_errors = _parse_entry(raw, root=root, prefix=f"assets[{index}]")
        if entry is None or entry_errors or entry.kind != "speech":
            continue
        path = root / entry.relative_path
        if Path(entry.relative_path).parent.as_posix() != subdir:
            continue
        try:
            if path.is_file() and _sha256(path) == entry.sha256:
                approved.append(path)
        except OSError:
            continue
    return approved


def is_approved_spoken_asset(path: Path, *, assets_root: Path = DEMO_ASSETS_DIR) -> bool:
    """Revalidate one cached path so a changed asset fails closed immediately."""

    candidate = Path(path)
    try:
        relative = candidate.resolve().relative_to(Path(assets_root).resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return candidate in approved_spoken_assets(relative.parent.as_posix(), assets_root=assets_root)


def _read_manifest(root: Path) -> tuple[dict[str, object] | None, list[str]]:
    manifest_path = root / MANIFEST_FILENAME
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, [f"{MANIFEST_FILENAME} is missing"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"{MANIFEST_FILENAME} is unreadable: {exc}"]
    if not isinstance(raw, dict):
        return None, [f"{MANIFEST_FILENAME} root must be an object"]
    return raw, []


def _parse_entry(raw: object, *, root: Path, prefix: str) -> tuple[SpokenAssetEntry | None, list[str]]:
    if not isinstance(raw, dict):
        return None, [f"{prefix} must be an object"]
    values = {key: raw.get(key) for key in ("path", "sha256", "kind", "language", "transcript")}
    if not all(isinstance(value, str) for value in values.values()):
        return None, [f"{prefix} fields must all be strings"]
    relative_path = str(values["path"])
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() != ".mp3":
        return None, [f"{prefix}.path must be a safe relative mp3 path"]
    if relative.parts[:1] not in {(name,) for name in DISCOVERABLE_AUDIO_SUBDIRS}:
        return None, [f"{prefix}.path is outside the packaged speech inventory"]
    digest = str(values["sha256"]).lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return None, [f"{prefix}.sha256 must be 64 lowercase hex characters"]
    try:
        (root / relative).resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return None, [f"{prefix}.path escapes the asset root"]
    return (
        SpokenAssetEntry(
            relative_path=relative.as_posix(),
            sha256=digest,
            kind=str(values["kind"]),
            language=str(values["language"]),
            transcript=str(values["transcript"]),
        ),
        [],
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
