#!/usr/bin/env python3
"""Validate public provenance for the bundled station-imaging audio pack.

The imaging pack is part of the public add-on, so every recorded source must
be independently redistributable.  This validator deliberately works offline:
it validates the checked-in source URL and SHA-256 ledger, rather than trying
to download a mutable third-party file during a build.

Schema version 2 keeps three linked inventories in ``manifest.json``:

* ``sources``: public source recordings and their licence/provenance;
* ``assets``: rendered files, their output SHA-256 and source references; and
* ``recipes``: named mixes which refer only to declared assets.

The optional ``original_path`` on a source is useful for a local curator's
archive.  It is not required for the public repository: source URL and source
SHA-256 are the durable provenance record.  When an original is present, its
hash is verified as well.

Usage:
    python scripts/validate_audio_asset_pack.py
    python scripts/validate_audio_asset_pack.py --write-attribution
    python scripts/validate_audio_asset_pack.py --pack-dir /tmp/imaging-pack
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACK_DIR = REPO_ROOT / "mammamiradio" / "assets" / "imaging"
MANIFEST_NAME = "manifest.json"
ATTRIBUTION_NAME = "ATTRIBUTION.md"
SCHEMA_VERSION = 2
ALLOWED_LICENSES = frozenset({"CC0-1.0", "CC-BY-4.0"})
LICENSE_LABELS = {
    "CC0-1.0": "CC0 1.0 Universal",
    "CC-BY-4.0": "Creative Commons Attribution 4.0 International",
}
LICENSE_URLS = {
    "CC0-1.0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "CC-BY-4.0": "https://creativecommons.org/licenses/by/4.0/",
}
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_GAIN_DB = 12.0
MIN_GAIN_DB = -60.0


class AudioAssetPackValidationError(ValueError):
    """Raised when the public-audio delivery contract is not met."""

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("\n".join(f"- {error}" for error in self.errors))


@dataclass(frozen=True)
class SourceRecord:
    """One independently licensed recording used by one or more render assets."""

    id: str
    license: str
    source_url: str
    source_sha256: str
    creator: str
    title: str
    modification: str
    attribution: str | None
    original_path: str | None


@dataclass(frozen=True)
class AudioFormat:
    """Encoded delivery format recorded in the pack manifest."""

    codec: str
    sample_rate_hz: int
    channels: int
    bitrate_kbps: int


@dataclass(frozen=True)
class AudioProbe:
    """Audio facts returned by FFprobe for a delivered pack member."""

    codec: str
    sample_rate_hz: int
    channels: int
    bitrate_kbps: float
    duration_sec: float


@dataclass(frozen=True)
class AssetRecord:
    """One shipped audio file and the public recordings that informed it."""

    id: str
    path: str
    source_ids: tuple[str, ...]
    sha256: str
    format: AudioFormat
    duration_target_sec: float | None


@dataclass(frozen=True)
class ValidationReport:
    """Validated records used to render deterministic attribution output."""

    pack_dir: Path
    manifest_path: Path
    attribution_path: Path
    sources: tuple[SourceRecord, ...]
    assets: tuple[AssetRecord, ...]
    recipes: int


def _nonempty_text(value: Any, label: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be a non-empty string")
        return None
    text = value.strip()
    if "\n" in text or "\r" in text:
        errors.append(f"{label} must be one line")
        return None
    return text


def _identifier(value: Any, label: str, errors: list[str]) -> str | None:
    identifier = _nonempty_text(value, label, errors)
    if identifier is None:
        return None
    if not IDENTIFIER_RE.fullmatch(identifier):
        errors.append(f"{label} must use only letters, numbers, dot, dash, and underscore")
        return None
    return identifier


def _sha256(value: Any, label: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        errors.append(f"{label} must be a lowercase 64-character SHA-256 hex digest")
        return None
    return value


def _finite_number(value: Any, label: str, errors: list[str], *, minimum: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        errors.append(f"{label} must be a finite number")
        return None
    number = float(value)
    if minimum is not None and number < minimum:
        comparator = ">" if minimum == 0 else ">="
        errors.append(f"{label} must be {comparator} {minimum:g}")
        return None
    return number


def _read_manifest(path: Path, errors: list[str]) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"missing manifest: {path}")
        return None
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read manifest {path}: {exc}")
        return None
    if not isinstance(payload, dict):
        errors.append("manifest root must be an object")
        return None
    return payload


def _validate_format(value: Any, label: str, errors: list[str]) -> AudioFormat | None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return None

    codec = _nonempty_text(value.get("codec"), f"{label}.codec", errors)
    sample_rate = value.get("sample_rate_hz")
    channels = value.get("channels")
    bitrate = value.get("bitrate_kbps")
    valid = codec is not None

    for field, raw, minimum, maximum in (
        ("sample_rate_hz", sample_rate, 8_000, 384_000),
        ("channels", channels, 1, 16),
        ("bitrate_kbps", bitrate, 8, 2_000),
    ):
        if isinstance(raw, bool) or not isinstance(raw, int) or not minimum <= raw <= maximum:
            errors.append(f"{label}.{field} must be an integer between {minimum} and {maximum}")
            valid = False

    if not valid:
        return None
    assert codec is not None
    assert isinstance(sample_rate, int)
    assert isinstance(channels, int)
    assert isinstance(bitrate, int)
    return AudioFormat(codec=codec.lower(), sample_rate_hz=sample_rate, channels=channels, bitrate_kbps=bitrate)


def _validate_source_records(value: Any, errors: list[str]) -> tuple[SourceRecord, ...]:
    if not isinstance(value, list) or not value:
        errors.append("sources must be a non-empty list")
        return ()

    records: list[SourceRecord] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(value):
        label = f"sources[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{label} must be an object")
            continue

        identifier = _identifier(raw.get("id"), f"{label}.id", errors)
        license_name = _nonempty_text(raw.get("license"), f"{label}.license", errors)
        if license_name is not None and license_name not in ALLOWED_LICENSES:
            errors.append(f"{label}.license must be one of: {', '.join(sorted(ALLOWED_LICENSES))}")
            license_name = None

        source_url = _nonempty_text(raw.get("source_url"), f"{label}.source_url", errors)
        if source_url is not None:
            parsed = urlparse(source_url)
            if parsed.scheme != "https" or not parsed.netloc:
                errors.append(f"{label}.source_url must be an absolute https URL")
                source_url = None

        source_sha256 = _sha256(raw.get("source_sha256"), f"{label}.source_sha256", errors)
        creator = _nonempty_text(raw.get("creator"), f"{label}.creator", errors)
        title = _nonempty_text(raw.get("title"), f"{label}.title", errors)
        modification = _nonempty_text(raw.get("modification"), f"{label}.modification", errors)

        attribution: str | None = None
        raw_attribution = raw.get("attribution")
        if raw_attribution is not None:
            attribution = _nonempty_text(raw_attribution, f"{label}.attribution", errors)
        if license_name == "CC-BY-4.0" and attribution is None:
            errors.append(f"{label}.attribution is required for CC-BY-4.0 sources")

        original_path: str | None = None
        raw_original_path = raw.get("original_path")
        if raw_original_path is not None:
            original_path = _nonempty_text(raw_original_path, f"{label}.original_path", errors)

        if identifier is not None:
            if identifier in seen_ids:
                errors.append(f"{label}.id duplicates source {identifier!r}")
                identifier = None
            else:
                seen_ids.add(identifier)

        if (
            identifier is None
            or license_name is None
            or source_url is None
            or source_sha256 is None
            or creator is None
            or title is None
            or modification is None
        ):
            continue
        records.append(
            SourceRecord(
                id=identifier,
                license=license_name,
                source_url=source_url,
                source_sha256=source_sha256,
                creator=creator,
                title=title,
                modification=modification,
                attribution=attribution,
                original_path=original_path,
            )
        )
    return tuple(records)


def _source_ids(raw: dict[str, Any], label: str, errors: list[str]) -> tuple[str, ...] | None:
    canonical = raw.get("source_ids")
    alias = raw.get("sources")
    if canonical is None and alias is None:
        errors.append(f"{label}.source_ids must list one or more source ids")
        return None
    if canonical is not None and alias is not None and canonical != alias:
        errors.append(f"{label}.source_ids and {label}.sources disagree")
        return None
    value = canonical if canonical is not None else alias
    field = "source_ids" if canonical is not None else "sources"
    if not isinstance(value, list) or not value:
        errors.append(f"{label}.{field} must be a non-empty list of source ids")
        return None

    resolved: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        identifier = _identifier(item, f"{label}.{field}[{index}]", errors)
        if identifier is None:
            continue
        if identifier in seen:
            errors.append(f"{label}.{field}[{index}] duplicates source {identifier!r}")
            continue
        seen.add(identifier)
        resolved.append(identifier)
    return tuple(resolved) if resolved else None


def _validate_asset_records(value: Any, errors: list[str]) -> tuple[AssetRecord, ...]:
    if not isinstance(value, list) or not value:
        errors.append("assets must be a non-empty list")
        return ()

    records: list[AssetRecord] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(value):
        label = f"assets[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{label} must be an object")
            continue

        identifier = _identifier(raw.get("id"), f"{label}.id", errors)
        path = _nonempty_text(raw.get("path"), f"{label}.path", errors)
        if path is not None:
            relative = PurePosixPath(path)
            if "\\" in path or relative.is_absolute() or any(part in {".", ".."} for part in relative.parts):
                errors.append(f"{label}.path must be a safe relative POSIX path")
                path = None

        source_ids = _source_ids(raw, label, errors)
        asset_sha256 = _sha256(raw.get("sha256"), f"{label}.sha256", errors)
        audio_format = _validate_format(raw.get("format"), f"{label}.format", errors)
        duration_target_sec: float | None = None
        if "duration_target_sec" in raw:
            duration_target_sec = _finite_number(
                raw.get("duration_target_sec"), f"{label}.duration_target_sec", errors, minimum=0
            )
            if duration_target_sec == 0:
                errors.append(f"{label}.duration_target_sec must be > 0")
                duration_target_sec = None

        if identifier is not None:
            if identifier in seen_ids:
                errors.append(f"{label}.id duplicates asset {identifier!r}")
                identifier = None
            else:
                seen_ids.add(identifier)

        if identifier is None or path is None or source_ids is None or asset_sha256 is None or audio_format is None:
            continue
        records.append(
            AssetRecord(
                id=identifier,
                path=path,
                source_ids=source_ids,
                sha256=asset_sha256,
                format=audio_format,
                duration_target_sec=duration_target_sec,
            )
        )
    return tuple(records)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_asset_path(pack_dir: Path, relative_path: str) -> Path:
    """Resolve a declared asset without allowing a manifest path to escape its pack."""
    candidate = pack_dir.joinpath(*PurePosixPath(relative_path).parts)
    root = pack_dir.resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"asset path escapes pack directory: {relative_path}") from exc
    return resolved


def _resolve_original_path(pack_dir: Path, original_path: str) -> Path:
    path = Path(original_path).expanduser()
    return path if path.is_absolute() else pack_dir / path


def _probe_audio(path: Path) -> AudioProbe:
    """Inspect one output member with FFprobe; no network or decoding rewrite occurs."""
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,sample_rate,channels,bit_rate",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffprobe is required to validate delivered audio format") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffprobe timed out for {path}") from exc

    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"ffprobe exited {completed.returncode}")
    try:
        payload = json.loads(completed.stdout)
        stream = payload["streams"][0]
        codec = str(stream["codec_name"]).lower()
        sample_rate_hz = int(stream["sample_rate"])
        channels = int(stream["channels"])
        bitrate_bps = int(stream["bit_rate"])
        duration_sec = float(payload["format"]["duration"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"ffprobe returned incomplete audio metadata for {path}") from exc
    if not math.isfinite(duration_sec) or duration_sec <= 0:
        raise RuntimeError(f"ffprobe returned invalid duration for {path}")
    return AudioProbe(
        codec=codec,
        sample_rate_hz=sample_rate_hz,
        channels=channels,
        bitrate_kbps=bitrate_bps / 1_000,
        duration_sec=duration_sec,
    )


def _validate_source_originals(pack_dir: Path, sources: tuple[SourceRecord, ...], errors: list[str]) -> None:
    for source in sources:
        # A public URL plus source checksum is enough for the public ledger. The
        # unredistributable local master is intentionally optional.
        if source.original_path is None:
            continue
        original = _resolve_original_path(pack_dir, source.original_path)
        if not original.is_file():
            errors.append(f"source {source.id!r} original_path does not exist: {original}")
            continue
        try:
            actual = _sha256_file(original)
        except OSError as exc:
            errors.append(f"cannot hash source {source.id!r} original_path: {exc}")
            continue
        if actual != source.source_sha256:
            errors.append(
                f"source {source.id!r} original_path SHA-256 differs from declared source_sha256 "
                f"({actual} != {source.source_sha256})"
            )


def _validate_assets(
    pack_dir: Path,
    sources: tuple[SourceRecord, ...],
    assets: tuple[AssetRecord, ...],
    errors: list[str],
) -> dict[str, float]:
    source_ids = {source.id for source in sources}
    durations: dict[str, float] = {}
    for asset in assets:
        for source_id in asset.source_ids:
            if source_id not in source_ids:
                errors.append(f"asset {asset.id!r} references undeclared source {source_id!r}")

        try:
            path = _resolve_asset_path(pack_dir, asset.path)
        except ValueError as exc:
            errors.append(f"asset {asset.id!r}: {exc}")
            continue
        if not path.is_file():
            errors.append(f"asset {asset.id!r} is missing: {asset.path}")
            continue
        try:
            actual_sha256 = _sha256_file(path)
        except OSError as exc:
            errors.append(f"cannot hash asset {asset.id!r}: {exc}")
            continue
        if actual_sha256 != asset.sha256:
            errors.append(f"asset {asset.id!r} SHA-256 differs from manifest ({actual_sha256} != {asset.sha256})")

        try:
            probe = _probe_audio(path)
        except RuntimeError as exc:
            errors.append(f"asset {asset.id!r} is not probeable: {exc}")
            continue
        durations[asset.id] = probe.duration_sec
        expected = asset.format
        if probe.codec != expected.codec:
            errors.append(f"asset {asset.id!r} codec is {probe.codec!r}, expected {expected.codec!r}")
        if probe.sample_rate_hz != expected.sample_rate_hz:
            errors.append(
                f"asset {asset.id!r} sample rate is {probe.sample_rate_hz}Hz, expected {expected.sample_rate_hz}Hz"
            )
        if probe.channels != expected.channels:
            errors.append(f"asset {asset.id!r} has {probe.channels} channels, expected {expected.channels}")
        # Encoders may vary by a few frame headers, but a 2 kbps window still
        # catches the wrong delivery preset without false-failing normal CBR MP3.
        if abs(probe.bitrate_kbps - expected.bitrate_kbps) > 2.0:
            errors.append(
                f"asset {asset.id!r} bitrate is {probe.bitrate_kbps:.1f}kbps, expected {expected.bitrate_kbps}kbps"
            )
        if asset.duration_target_sec is not None:
            tolerance = max(0.12, asset.duration_target_sec * 0.10)
            if abs(probe.duration_sec - asset.duration_target_sec) > tolerance:
                errors.append(
                    f"asset {asset.id!r} duration is {probe.duration_sec:.3f}s, expected "
                    f"{asset.duration_target_sec:.3f}s (±{tolerance:.3f}s)"
                )
    return durations


def _validate_gain(value: Any, label: str, errors: list[str]) -> None:
    gain = _finite_number(value, label, errors)
    if gain is not None and not MIN_GAIN_DB <= gain <= MAX_GAIN_DB:
        errors.append(f"{label} must be between {MIN_GAIN_DB:g} and {MAX_GAIN_DB:g} dB")


def _validate_recipe_asset_ref(
    value: Any,
    label: str,
    asset_ids: set[str],
    errors: list[str],
) -> str | None:
    identifier = _identifier(value, label, errors)
    if identifier is not None and identifier not in asset_ids:
        errors.append(f"{label} references undeclared asset {identifier!r}")
        return None
    return identifier


def _validate_recipe_duration_bound(
    value: Any,
    label: str,
    asset_id: str | None,
    assets: dict[str, AssetRecord],
    measured_durations: dict[str, float],
    errors: list[str],
) -> None:
    duration = _finite_number(value, label, errors, minimum=0)
    if duration == 0:
        errors.append(f"{label} must be > 0")
        return
    if duration is None or asset_id is None:
        return
    # Prefer an intentionally declared target, then fall back to the delivered
    # file's measured duration. This prevents a recipe from asking the runtime
    # to play farther into an asset than the reviewed source actually contains.
    bound = assets[asset_id].duration_target_sec or measured_durations.get(asset_id)
    if bound is not None and duration > bound + 0.001:
        errors.append(f"{label} ({duration:.3f}s) exceeds asset {asset_id!r} duration bound ({bound:.3f}s)")


def _validate_recipes(
    value: Any,
    assets: tuple[AssetRecord, ...],
    measured_durations: dict[str, float],
    errors: list[str],
) -> int:
    if not isinstance(value, list) or not value:
        errors.append("recipes must be a non-empty list")
        return 0

    asset_map = {asset.id: asset for asset in assets}
    asset_ids = set(asset_map)
    seen_ids: set[str] = set()
    valid_count = 0
    for index, raw in enumerate(value):
        label = f"recipes[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{label} must be an object")
            continue
        identifier = _identifier(raw.get("id"), f"{label}.id", errors)
        if identifier is not None:
            if identifier in seen_ids:
                errors.append(f"{label}.id duplicates recipe {identifier!r}")
                identifier = None
            else:
                seen_ids.add(identifier)

        bed = raw.get("bed")
        cues = raw.get("cues")
        if bed is None and cues is None:
            errors.append(f"{label} must declare a bed, cues, or both")
        if bed is not None:
            if not isinstance(bed, dict):
                errors.append(f"{label}.bed must be an object")
            else:
                bed_asset_id = _validate_recipe_asset_ref(
                    bed.get("asset_id"), f"{label}.bed.asset_id", asset_ids, errors
                )
                _validate_gain(bed.get("gain_db"), f"{label}.bed.gain_db", errors)
                if "max_duration_sec" in bed:
                    _validate_recipe_duration_bound(
                        bed.get("max_duration_sec"),
                        f"{label}.bed.max_duration_sec",
                        bed_asset_id,
                        asset_map,
                        measured_durations,
                        errors,
                    )

        if cues is not None:
            if not isinstance(cues, list):
                errors.append(f"{label}.cues must be a list")
            elif not cues and bed is None:
                errors.append(f"{label}.cues cannot be empty when no bed is declared")
            else:
                for cue_index, cue in enumerate(cues):
                    cue_label = f"{label}.cues[{cue_index}]"
                    if not isinstance(cue, dict):
                        errors.append(f"{cue_label} must be an object")
                        continue
                    _nonempty_text(cue.get("anchor"), f"{cue_label}.anchor", errors)
                    cue_asset_id = _validate_recipe_asset_ref(
                        cue.get("asset_id"), f"{cue_label}.asset_id", asset_ids, errors
                    )
                    _validate_gain(cue.get("gain_db"), f"{cue_label}.gain_db", errors)
                    _validate_recipe_duration_bound(
                        cue.get("max_duration_sec"),
                        f"{cue_label}.max_duration_sec",
                        cue_asset_id,
                        asset_map,
                        measured_durations,
                        errors,
                    )
        if identifier is not None:
            valid_count += 1
    return valid_count


def render_attribution(report: ValidationReport) -> str:
    """Return a stable, human-readable public provenance ledger.

    Sorting by source id keeps generated output invariant under harmless
    manifest reordering and makes a stale attribution file a useful review
    signal rather than formatting churn.
    """
    used_by: dict[str, list[str]] = {source.id: [] for source in report.sources}
    for asset in report.assets:
        for source_id in asset.source_ids:
            if source_id in used_by:
                used_by[source_id].append(asset.id)

    lines = [
        "# Audio asset attribution",
        "",
        "This file is generated from `manifest.json` by `scripts/validate_audio_asset_pack.py`.",
        "Do not edit it by hand; run `python scripts/validate_audio_asset_pack.py --write-attribution`.",
        "",
        "All source recordings listed here are approved for public redistribution in this add-on.",
        "",
        "## Source recordings",
        "",
    ]
    for source in sorted(report.sources, key=lambda item: (item.id.casefold(), item.id)):
        lines.extend(
            [
                f"### `{source.id}` — {source.title}",
                "",
                f"- Creator: {source.creator}",
                f"- License: [{LICENSE_LABELS[source.license]}]({LICENSE_URLS[source.license]})",
                f"- Source: {source.source_url}",
                f"- Source SHA-256: `{source.source_sha256}`",
                f"- Modification: {source.modification}",
            ]
        )
        if source.license == "CC-BY-4.0":
            assert source.attribution is not None
            lines.append(f"- Required attribution: {source.attribution}")
        assets = ", ".join(f"`{asset_id}`" for asset_id in sorted(used_by[source.id])) or "(none)"
        lines.extend([f"- Used by: {assets}", ""])
    return "\n".join(lines).rstrip() + "\n"


def _write_text_atomically(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_attribution(report: ValidationReport) -> None:
    """Publish the deterministic provenance ledger after a successful validation."""
    _write_text_atomically(report.attribution_path, render_attribution(report))


def check_attribution(report: ValidationReport) -> None:
    """Fail when the committed ledger no longer matches reviewed manifest facts."""
    expected = render_attribution(report)
    try:
        actual = report.attribution_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AudioAssetPackValidationError(
            [f"missing attribution file: {report.attribution_path}; run with --write-attribution"]
        ) from exc
    except OSError as exc:
        raise AudioAssetPackValidationError([f"cannot read attribution file {report.attribution_path}: {exc}"]) from exc
    if actual != expected:
        raise AudioAssetPackValidationError(
            [f"attribution file is stale: {report.attribution_path}; run with --write-attribution"]
        )


def validate_audio_asset_pack(
    pack_dir: Path = DEFAULT_PACK_DIR,
    *,
    manifest_path: Path | None = None,
    attribution_path: Path | None = None,
) -> ValidationReport:
    """Validate schema-v2 provenance, output integrity, format, and mix refs.

    This function never writes. Call :func:`write_attribution` after it returns
    successfully, or use the command-line ``--write-attribution`` switch.
    """
    pack_dir = pack_dir.resolve()
    manifest_path = (manifest_path or pack_dir / MANIFEST_NAME).resolve()
    attribution_path = (attribution_path or pack_dir / ATTRIBUTION_NAME).resolve()
    errors: list[str] = []
    manifest = _read_manifest(manifest_path, errors)
    if manifest is None:
        raise AudioAssetPackValidationError(errors)

    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    sources = _validate_source_records(manifest.get("sources"), errors)
    assets = _validate_asset_records(manifest.get("assets"), errors)
    _validate_source_originals(pack_dir, sources, errors)
    measured_durations = _validate_assets(pack_dir, sources, assets, errors)
    recipe_count = _validate_recipes(manifest.get("recipes"), assets, measured_durations, errors)
    if errors:
        raise AudioAssetPackValidationError(errors)
    return ValidationReport(
        pack_dir=pack_dir,
        manifest_path=manifest_path,
        attribution_path=attribution_path,
        sources=sources,
        assets=assets,
        recipes=recipe_count,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pack-dir",
        type=Path,
        default=None,
        help=f"Directory containing the pack (default: {DEFAULT_PACK_DIR.relative_to(REPO_ROOT)})",
    )
    parser.add_argument("--manifest", type=Path, help="Override the manifest path (assets stay relative to --pack-dir)")
    parser.add_argument("--attribution", type=Path, help="Override the generated attribution path")
    parser.add_argument(
        "--write-attribution",
        action="store_true",
        help="Write ATTRIBUTION.md from validated manifest records instead of checking it",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.pack_dir is not None:
        pack_dir = args.pack_dir
    elif args.manifest is not None:
        pack_dir = args.manifest.parent
    else:
        pack_dir = DEFAULT_PACK_DIR
    try:
        report = validate_audio_asset_pack(
            pack_dir,
            manifest_path=args.manifest,
            attribution_path=args.attribution,
        )
        if args.write_attribution:
            write_attribution(report)
            print(f"Wrote attribution: {report.attribution_path}")
        else:
            check_attribution(report)
    except AudioAssetPackValidationError as exc:
        print("ERROR: public audio pack validation failed:", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 2

    print(f"Audio asset pack OK: {len(report.sources)} sources, {len(report.assets)} assets, {report.recipes} recipes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
