"""Fail-closed manifest for packaged audio that can enter speech lanes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from mammamiradio.core.listener_truth import contains_unsafe_listener_claims
from mammamiradio.core.packaged_assets import DEMO_ASSETS_DIR
from mammamiradio.core.path_safety import safe_path_within

MANIFEST_FILENAME = "spoken_assets.json"
DISCOVERABLE_AUDIO_SUBDIRS = ("recovery", "banter", "first_listen")
PACKAGED_BANTER_PREDECESSOR_STARTER_ID_KEY = "_packaged_banter_predecessor_starter_id"


@dataclass(frozen=True, slots=True)
class SpokenAssetEntry:
    """One content-addressed packaged-audio declaration."""

    relative_path: str
    sha256: str
    kind: str
    language: str
    transcript: str
    mode: str = ""
    required_previous_starter_id: str = ""
    special: bool = False


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
        errors.extend(_entry_policy_errors(entry))

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

    root = Path(assets_root)
    return [root / entry.relative_path for entry in approved_spoken_asset_entries(subdir, assets_root=root)]


def approved_spoken_asset_entries(
    subdir: str,
    *,
    assets_root: Path = DEMO_ASSETS_DIR,
) -> list[SpokenAssetEntry]:
    """Return validated declarations while preserving runtime selection metadata."""

    root = Path(assets_root)
    return [
        entry
        for entry in declared_spoken_asset_entries(subdir, assets_root=root)
        if _approved_manifest_entry(root / entry.relative_path, assets_root=root) == entry
    ]


def declared_spoken_asset_entries(
    subdir: str,
    *,
    assets_root: Path = DEMO_ASSETS_DIR,
) -> list[SpokenAssetEntry]:
    """Return policy-valid declarations without reading every packaged audio file.

    Runtime selectors cache these small manifest records, then hash only the
    selected file at admission. Whole-inventory hash and discovery checks stay
    in the release validator, away from first-byte and recovery paths.
    """

    if subdir not in DISCOVERABLE_AUDIO_SUBDIRS:
        return []
    root = Path(assets_root)
    data, _errors = _read_manifest(root)
    if data is None or data.get("schema_version") != 1:
        return []
    raw_assets = data.get("assets")
    if not isinstance(raw_assets, list):
        return []
    approved: list[SpokenAssetEntry] = []
    for index, raw in enumerate(raw_assets):
        entry, entry_errors = _parse_entry(raw, root=root, prefix=f"assets[{index}]")
        if entry is None or entry_errors or _entry_policy_errors(entry) or entry.kind != "speech":
            continue
        if Path(entry.relative_path).parent.as_posix() != subdir:
            continue
        approved.append(entry)
    return approved


def is_approved_spoken_asset(path: Path, *, assets_root: Path = DEMO_ASSETS_DIR) -> bool:
    """Revalidate one cached path so a changed asset fails closed immediately."""

    entry = _approved_manifest_entry(path, assets_root=assets_root)
    return entry is not None and entry.kind == "speech"


def _stays_inside_root(candidate: Path, root: Path) -> bool:
    """Whether ``candidate`` is still inside ``root`` once symlinks are followed.

    Thin wrapper over the shared containment helper so this module cannot drift
    from the cache and handoff paths that ask the same question. The symlink
    cycle handling that Python 3.13 made necessary lives there, in one place.
    """

    return safe_path_within(candidate, root) is not None


def is_approved_packaged_audio_asset(path: Path, *, assets_root: Path = DEMO_ASSETS_DIR) -> bool:
    """Return whether a speech or tone asset is declared, intact, and safe."""

    return _approved_manifest_entry(path, assets_root=assets_root) is not None


def _approved_manifest_entry(path: Path, *, assets_root: Path) -> SpokenAssetEntry | None:
    """Validate only the selected manifest entry and its content-addressed file."""

    candidate = Path(path)
    root = Path(assets_root)
    if not _stays_inside_root(candidate, root):
        return None
    try:
        relative = candidate.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    data, _errors = _read_manifest(root)
    if data is None or data.get("schema_version") != 1:
        return None
    raw_assets = data.get("assets") if data is not None else None
    if not isinstance(raw_assets, list):
        return None
    matching = [
        (index, raw)
        for index, raw in enumerate(raw_assets)
        if isinstance(raw, dict) and raw.get("path") == relative.as_posix()
    ]
    if len(matching) != 1:
        return None
    index, raw = matching[0]
    entry, entry_errors = _parse_entry(raw, root=root, prefix=f"assets[{index}]")
    if entry is None or entry_errors or _entry_policy_errors(entry):
        return None
    try:
        if candidate.is_file() and _sha256(candidate) == entry.sha256:
            return entry
    except OSError:
        return None
    return None


def _entry_policy_errors(entry: SpokenAssetEntry) -> list[str]:
    """Return listener-truth and lane-policy errors for one parsed entry."""

    errors: list[str] = []
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

    subdir = Path(entry.relative_path).parent.as_posix()
    if subdir == "banter":
        if entry.mode not in {"normal", "super_italian"}:
            errors.append(f"{entry.relative_path} banter mode must be normal or super_italian")
        expected_language = {"normal": "en", "super_italian": "it"}.get(entry.mode)
        if expected_language is not None and entry.language != expected_language:
            errors.append(f"{entry.relative_path} banter language does not match its mode")
        starter_id = entry.required_previous_starter_id
        if starter_id and (len(starter_id) > 80 or any(not (char.isalnum() or char in "._-") for char in starter_id)):
            errors.append(f"{entry.relative_path} required starter id is invalid")
        if entry.special and (entry.mode != "normal" or starter_id):
            errors.append(f"{entry.relative_path} special banter must be evergreen Normal Mode copy")
    elif entry.mode or entry.required_previous_starter_id or entry.special:
        errors.append(f"{entry.relative_path} non-banter asset has banter metadata")
    return errors


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
    mode = raw.get("mode", "")
    required_previous_starter_id = raw.get("required_previous_starter_id", "")
    special = raw.get("special", False)
    if not isinstance(mode, str) or not isinstance(required_previous_starter_id, str) or not isinstance(special, bool):
        return None, [f"{prefix} banter metadata has invalid types"]
    relative_path = str(values["path"])
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() != ".mp3":
        return None, [f"{prefix}.path must be a safe relative mp3 path"]
    if relative.parts[:1] not in {(name,) for name in DISCOVERABLE_AUDIO_SUBDIRS}:
        return None, [f"{prefix}.path is outside the packaged speech inventory"]
    digest = str(values["sha256"]).lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return None, [f"{prefix}.sha256 must be 64 lowercase hex characters"]
    if not _stays_inside_root(root / relative, root):
        return None, [f"{prefix}.path escapes the asset root"]
    return (
        SpokenAssetEntry(
            relative_path=relative.as_posix(),
            sha256=digest,
            kind=str(values["kind"]),
            language=str(values["language"]),
            transcript=str(values["transcript"]),
            mode=mode,
            required_previous_starter_id=required_previous_starter_id,
            special=special,
        ),
        [],
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
