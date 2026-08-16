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

For an external recording, ``original_path`` remains an optional curator's
archive pointer; its public URL and SHA-256 are the durable ledger. A
project-authored deterministic master instead keeps that file in the pack and
pins its generator, every declared Git dependency, and toolchain metadata.

Usage:
    python scripts/validate_audio_asset_pack.py
    python scripts/validate_audio_asset_pack.py --write-attribution
    python scripts/validate_audio_asset_pack.py --pack-dir /tmp/imaging-pack
"""

from __future__ import annotations

import argparse
import array
import bisect
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from mammamiradio.audio.imaging_schema import RECIPE_MANIFEST_SCHEMA_VERSION, parse_recipe_specs

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACK_DIR = REPO_ROOT / "mammamiradio" / "assets" / "imaging"
MANIFEST_NAME = "manifest.json"
ATTRIBUTION_NAME = "ATTRIBUTION.md"
SCHEMA_VERSION = RECIPE_MANIFEST_SCHEMA_VERSION
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
GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
PROJECT_AUTHORED_PROVENANCE_TYPE = "project-authored-deterministic-master"
PROJECT_GENERATOR_KIND = "git-committed-ffmpeg-generator"
QUALITY_GUARD_KEY = "quality_guard_allowlist"
CORRELATION_THRESHOLD = 0.98
CORRELATION_WINDOW_SEC = 0.5
CORRELATION_NATIVE_SAMPLE_RATE_HZ = 48_000
CORRELATION_GRID_SEC = 0.002
CORRELATION_INNER_MARGIN_SEC = 0.002
CORRELATION_SIGNATURE_BITS = 256
CORRELATION_PROJECTION_PAIRS_PER_BIT = 8
CORRELATION_SIGNATURE_DISTANCE = 40
CORRELATION_MIN_COMMON_NONZERO_BITS = 32
CORRELATION_LANDMARK_DESCRIPTOR_BITS = 64
CORRELATION_LANDMARK_JITTER_SAMPLES = 0
CORRELATION_LANDMARK_SPARSE_PAIR_SPAN_SAMPLES = 8
CORRELATION_LANDMARK_POSITION_BITS = 20
CORRELATION_LANDMARK_POSITION_MASK = (1 << CORRELATION_LANDMARK_POSITION_BITS) - 1
CORRELATION_LANDMARK_HASH_MASK = (1 << (64 - CORRELATION_LANDMARK_POSITION_BITS)) - 1
CORRELATION_LANDMARK_SPARSE_KEY_BIT = 1 << (64 - CORRELATION_LANDMARK_POSITION_BITS - 1)
CORRELATION_MAX_REVIEW_ASSETS = 32
CORRELATION_MAX_SAMPLES_PER_ASSET = 16 * CORRELATION_NATIVE_SAMPLE_RATE_HZ
CORRELATION_MAX_TOTAL_SAMPLES = 12_000_000
CORRELATION_MAX_SIGNATURE_COORDINATES = 50_000_000
CORRELATION_MAX_SIGNATURE_WORK = 400_000_000
CORRELATION_MAX_SIGNATURE_MATCHES = 2_000_000
CORRELATION_MAX_LANDMARK_EVENTS = 50_000_000
CORRELATION_MAX_LANDMARK_POSTINGS = 1_000_000
CORRELATION_MAX_LANDMARK_JOINS = 20_000_000
CORRELATION_MAX_LANDMARK_LAGS = 1_000_000
CORRELATION_MAX_LANDMARK_REGION_PROBES = 20_000_000
CORRELATION_MAX_NATIVE_LAG_CANDIDATES = 250_000
CORRELATION_MAX_NATIVE_EXACT_WINDOWS = 100_000_000
CORRELATION_MAX_NATIVE_PRIMITIVE_WORK = 5_000_000_000
CORRELATION_MAX_NATIVE_REGIONS = 2_000_000
CORE_ASSET_KINDS = frozenset({"identity", "transition"})
CORRELATION_ASSET_KINDS = CORE_ASSET_KINDS | {"compatibility"}
LAYER_ROLES = frozenset({"foreground", "texture"})
STARTUP_SYNTH_TAGS = frozenset({"synth", "synthesizer", "electric-piano", "e-piano", "epiano", "rhodes"})
STARTUP_SYNTH_FORBIDDEN_TERMS = frozenset({"brass", "trumpet"})
STARTUP_SYNTH_SEMANTIC_RE = re.compile(
    r"(?<![a-z0-9])(?:synth|synthesizer|rhodes|e[-_]?piano|electric(?:[-_]|\s+)piano)(?![a-z0-9])",
    re.IGNORECASE,
)
STARTUP_SYNTH_FORBIDDEN_RE = re.compile(r"(?<![a-z0-9])(brass|trumpet)(?![a-z0-9])", re.IGNORECASE)
REQUIRED_LISTENING_SURFACES = frozenset({"station-id", "sweeper", "ad-in", "ad-out", "full-cadence"})
REQUIRED_LISTENING_DEVICE_CLASSES = frozenset({"mac", "target-speaker"})


class AudioAssetPackValidationError(ValueError):
    """Raised when the public-audio delivery contract is not met."""

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("\n".join(f"- {error}" for error in self.errors))


@dataclass(frozen=True)
class GeneratorDependency:
    """One Git-pinned input which can change a deterministic render."""

    path: str
    url: str
    sha256: str


@dataclass(frozen=True)
class GeneratorRuntime:
    """Human-readable toolchain versions used for a deterministic render."""

    python: str
    ffmpeg: str


@dataclass(frozen=True)
class GeneratorRecord:
    """Git-pinned program which rendered one project-authored master."""

    kind: str
    revision: str
    path: str
    url: str
    sha256: str
    dependencies: tuple[GeneratorDependency, ...]
    runtime: GeneratorRuntime


@dataclass(frozen=True)
class SourceRecord:
    """One licensed external recording or project-authored deterministic master."""

    id: str
    license: str
    source_url: str
    source_sha256: str
    creator: str
    title: str
    modification: str
    attribution: str | None
    original_path: str | None
    tags: tuple[str, ...]
    provenance_type: str | None
    generator: GeneratorRecord | None


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
class LayerRecord:
    """One reproducible source excerpt in a delivered asset."""

    source_id: str
    source_sha256: str
    source_start_sec: float
    duration_sec: float
    output_offset_sec: float
    gain_db: float
    dsp: tuple[str, ...]
    license: str
    role: str


@dataclass(frozen=True)
class AssetRecord:
    """One shipped audio file and the public recordings that informed it."""

    id: str
    path: str
    kind: str
    tags: tuple[str, ...]
    source_ids: tuple[str, ...]
    sha256: str
    format: AudioFormat
    duration_target_sec: float | None
    layers: tuple[LayerRecord, ...]


@dataclass(frozen=True)
class ValidationReport:
    """Validated records used to render deterministic attribution output."""

    pack_dir: Path
    manifest_path: Path
    attribution_path: Path
    sources: tuple[SourceRecord, ...]
    assets: tuple[AssetRecord, ...]
    recipes: int


@dataclass(frozen=True)
class QualityGuardAllowlist:
    """Narrow, reviewable exceptions to the sonic quality invariants."""

    perceptual_overlaps: frozenset[tuple[str, str]]
    source_core_roles: frozenset[str]


@dataclass(frozen=True)
class CorrelationSignature:
    """One 496 ms pair-projection signature and its disjoint start cell."""

    anchor: int
    first_start: int
    last_start: int
    signs: int
    nonzero: int


@dataclass(frozen=True)
class CorrelationLandmarkIndex:
    """Compact sorted (44-bit descriptor hash, 20-bit position) records."""

    records: array.array[int]


@dataclass(frozen=True)
class CorrelationPairPlan:
    """One fully budgeted channel-pair native confirmation plan."""

    first: AssetRecord
    second: AssetRecord
    first_label: str
    second_label: str
    first_samples: Sequence[int]
    second_samples: Sequence[int]
    regions: tuple[tuple[int, int, int], ...]


@dataclass(frozen=True)
class DecodedAudio:
    """Bounded native stereo PCM, kept separate to avoid mono cancellation."""

    left: array.array[int]
    right: array.array[int]

    @property
    def sample_count(self) -> int:
        return len(self.left)


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
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
        errors.append(f"{label} must be a finite number")
        return None
    number = float(value)
    if minimum is not None and number < minimum:
        comparator = ">" if minimum == 0 else ">="
        errors.append(f"{label} must be {comparator} {minimum:g}")
        return None
    return number


def _text_list(
    value: Any,
    label: str,
    errors: list[str],
    *,
    required: bool,
) -> tuple[str, ...] | None:
    if value is None and not required:
        return ()
    if not isinstance(value, list) or (required and not value):
        qualifier = "a non-empty list" if required else "a list"
        errors.append(f"{label} must be {qualifier}")
        return None
    resolved: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        text = _nonempty_text(item, f"{label}[{index}]", errors)
        if text is None:
            continue
        normalized = text.casefold()
        if normalized in seen:
            errors.append(f"{label}[{index}] duplicates value {text!r}")
            continue
        seen.add(normalized)
        resolved.append(text)
    if required and not resolved:
        return None
    return tuple(resolved)


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


def _safe_git_path(value: Any, label: str, errors: list[str]) -> str | None:
    path = _nonempty_text(value, label, errors)
    if path is None:
        return None
    relative = PurePosixPath(path)
    if "\\" in path or ":" in path or relative.is_absolute() or any(part in {".", ".."} for part in relative.parts):
        errors.append(f"{label} must be a safe relative POSIX path")
        return None
    return path


def _git_blob_url(value: Any, label: str, revision: str | None, path: str | None, errors: list[str]) -> str | None:
    url = _nonempty_text(value, label, errors)
    if url is None:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        errors.append(f"{label} must be an absolute https URL")
        return None
    if revision is not None and path is not None:
        expected_suffix = f"/blob/{revision}/{path}"
        if not parsed.path.endswith(expected_suffix):
            errors.append(f"{label} must pin revision and path as {expected_suffix!r}")
            return None
    return url


def _validate_generator_dependency(
    value: Any,
    label: str,
    revision: str | None,
    errors: list[str],
) -> GeneratorDependency | None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return None
    path = _safe_git_path(value.get("path"), f"{label}.path", errors)
    url = _git_blob_url(value.get("url"), f"{label}.url", revision, path, errors)
    dependency_sha256 = _sha256(value.get("sha256"), f"{label}.sha256", errors)
    if path is None or url is None or dependency_sha256 is None:
        return None
    return GeneratorDependency(path=path, url=url, sha256=dependency_sha256)


def _validate_generator_runtime(value: Any, label: str, errors: list[str]) -> GeneratorRuntime | None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return None
    python = _nonempty_text(value.get("python"), f"{label}.python", errors)
    ffmpeg = _nonempty_text(value.get("ffmpeg"), f"{label}.ffmpeg", errors)
    if python is None or ffmpeg is None:
        return None
    return GeneratorRuntime(python=python, ffmpeg=ffmpeg)


def _validate_generator(value: Any, label: str, errors: list[str]) -> GeneratorRecord | None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return None
    kind = _nonempty_text(value.get("kind"), f"{label}.kind", errors)
    if kind is not None and kind != PROJECT_GENERATOR_KIND:
        errors.append(f"{label}.kind must be {PROJECT_GENERATOR_KIND!r}")
        kind = None
    revision = _nonempty_text(value.get("revision"), f"{label}.revision", errors)
    if revision is not None and not GIT_REVISION_RE.fullmatch(revision):
        errors.append(f"{label}.revision must be a full lowercase Git SHA")
        revision = None
    path = _safe_git_path(value.get("path"), f"{label}.path", errors)
    url = _git_blob_url(value.get("url"), f"{label}.url", revision, path, errors)
    generator_sha256 = _sha256(value.get("sha256"), f"{label}.sha256", errors)

    dependencies: list[GeneratorDependency] | None = []
    raw_dependencies = value.get("dependencies")
    if not isinstance(raw_dependencies, list) or not raw_dependencies:
        errors.append(f"{label}.dependencies must be a non-empty list")
        dependencies = None
    else:
        seen_paths: set[str] = set()
        for index, raw_dependency in enumerate(raw_dependencies):
            dependency = _validate_generator_dependency(
                raw_dependency,
                f"{label}.dependencies[{index}]",
                revision,
                errors,
            )
            if dependency is None:
                dependencies = None
                continue
            if dependency.path in seen_paths:
                errors.append(f"{label}.dependencies[{index}].path duplicates {dependency.path!r}")
                dependencies = None
                continue
            seen_paths.add(dependency.path)
            if dependencies is not None:
                dependencies.append(dependency)
    runtime = _validate_generator_runtime(value.get("runtime"), f"{label}.runtime", errors)

    if (
        kind is None
        or revision is None
        or path is None
        or url is None
        or generator_sha256 is None
        or dependencies is None
        or runtime is None
    ):
        return None
    return GeneratorRecord(
        kind=kind,
        revision=revision,
        path=path,
        url=url,
        sha256=generator_sha256,
        dependencies=tuple(dependencies),
        runtime=runtime,
    )


def _validate_source_records(
    value: Any,
    errors: list[str],
    *,
    require_quality_metadata: bool,
) -> tuple[SourceRecord, ...]:
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

        tags = _text_list(raw.get("tags"), f"{label}.tags", errors, required=require_quality_metadata)
        provenance_type: str | None = None
        if raw.get("provenance_type") is not None:
            provenance_type = _nonempty_text(raw.get("provenance_type"), f"{label}.provenance_type", errors)
            if provenance_type is not None and provenance_type != PROJECT_AUTHORED_PROVENANCE_TYPE:
                errors.append(f"{label}.provenance_type must be {PROJECT_AUTHORED_PROVENANCE_TYPE!r}")
                provenance_type = None
        generator: GeneratorRecord | None = None
        if raw.get("generator") is not None:
            generator = _validate_generator(raw.get("generator"), f"{label}.generator", errors)
        if provenance_type == PROJECT_AUTHORED_PROVENANCE_TYPE:
            if original_path is None:
                errors.append(f"{label}.original_path is required for {PROJECT_AUTHORED_PROVENANCE_TYPE}")
            elif _safe_git_path(original_path, f"{label}.original_path", errors) is None:
                original_path = None
            if raw.get("generator") is None:
                errors.append(f"{label}.generator is required for {PROJECT_AUTHORED_PROVENANCE_TYPE}")
            if generator is not None and source_url is not None and source_url != generator.url:
                errors.append(f"{label}.source_url must equal {label}.generator.url")
        elif raw.get("generator") is not None:
            errors.append(f"{label}.provenance_type must declare {PROJECT_AUTHORED_PROVENANCE_TYPE!r} with generator")

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
            or tags is None
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
                tags=tags,
                provenance_type=provenance_type,
                generator=generator,
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


def _validate_layers(value: Any, label: str, errors: list[str], *, required: bool) -> tuple[LayerRecord, ...] | None:
    if value is None and not required:
        return ()
    if not isinstance(value, list) or not value:
        errors.append(f"{label} must be a non-empty list")
        return None

    layers: list[LayerRecord] = []
    for index, raw in enumerate(value):
        layer_label = f"{label}[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{layer_label} must be an object")
            continue
        source_id = _identifier(raw.get("source_id"), f"{layer_label}.source_id", errors)
        source_sha256 = _sha256(raw.get("source_sha256"), f"{layer_label}.source_sha256", errors)
        source_start_sec = _finite_number(
            raw.get("source_start_sec"), f"{layer_label}.source_start_sec", errors, minimum=0
        )
        duration_sec = _finite_number(raw.get("duration_sec"), f"{layer_label}.duration_sec", errors, minimum=0)
        if duration_sec == 0:
            errors.append(f"{layer_label}.duration_sec must be > 0")
            duration_sec = None
        output_offset_sec = _finite_number(
            raw.get("output_offset_sec"), f"{layer_label}.output_offset_sec", errors, minimum=0
        )
        gain_db = _finite_number(raw.get("gain_db"), f"{layer_label}.gain_db", errors)
        dsp = _text_list(raw.get("dsp"), f"{layer_label}.dsp", errors, required=False)
        license_name = _nonempty_text(raw.get("license"), f"{layer_label}.license", errors)
        if license_name is not None and license_name not in ALLOWED_LICENSES:
            errors.append(f"{layer_label}.license must be one of: {', '.join(sorted(ALLOWED_LICENSES))}")
            license_name = None
        role = _nonempty_text(raw.get("role"), f"{layer_label}.role", errors)
        if role is not None and role not in LAYER_ROLES:
            errors.append(f"{layer_label}.role must be one of: {', '.join(sorted(LAYER_ROLES))}")
            role = None
        if (
            source_id is None
            or source_sha256 is None
            or source_start_sec is None
            or duration_sec is None
            or output_offset_sec is None
            or gain_db is None
            or dsp is None
            or license_name is None
            or role is None
        ):
            continue
        layers.append(
            LayerRecord(
                source_id=source_id,
                source_sha256=source_sha256,
                source_start_sec=source_start_sec,
                duration_sec=duration_sec,
                output_offset_sec=output_offset_sec,
                gain_db=gain_db,
                dsp=dsp,
                license=license_name,
                role=role,
            )
        )
    return tuple(layers) if layers else None


def _validate_asset_records(
    value: Any,
    errors: list[str],
    *,
    require_quality_metadata: bool,
) -> tuple[AssetRecord, ...]:
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
        kind = _nonempty_text(raw.get("kind"), f"{label}.kind", errors)
        if "tags" not in raw:
            errors.append(f"{label}.tags must be a list")
            tags = None
        else:
            tags = _text_list(raw.get("tags"), f"{label}.tags", errors, required=False)
        if path is not None:
            relative = PurePosixPath(path)
            if "\\" in path or relative.is_absolute() or any(part in {".", ".."} for part in relative.parts):
                errors.append(f"{label}.path must be a safe relative POSIX path")
                path = None

        source_ids = _source_ids(raw, label, errors)
        asset_sha256 = _sha256(raw.get("sha256"), f"{label}.sha256", errors)
        audio_format = _validate_format(raw.get("format"), f"{label}.format", errors)
        layers = _validate_layers(raw.get("layers"), f"{label}.layers", errors, required=require_quality_metadata)
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

        if (
            identifier is None
            or path is None
            or kind is None
            or tags is None
            or source_ids is None
            or asset_sha256 is None
            or audio_format is None
            or layers is None
        ):
            continue
        records.append(
            AssetRecord(
                id=identifier,
                path=path,
                kind=kind,
                tags=tags,
                source_ids=source_ids,
                sha256=asset_sha256,
                format=audio_format,
                duration_target_sec=duration_target_sec,
                layers=layers,
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


def _read_git_blob(revision: str, path: str) -> bytes:
    """Read one committed generator input without consulting working-tree bytes."""
    try:
        completed = subprocess.run(
            ["git", "show", f"{revision}:{path}"],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            timeout=15,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git is required to validate project-authored generator provenance") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git timed out reading {revision}:{path}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"git cannot read {revision}:{path}")
    return completed.stdout


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
    pinned_blob_hashes: dict[tuple[str, str], str | RuntimeError] = {}
    reported_pins: set[tuple[str, str, str]] = set()
    for source in sources:
        if source.original_path is not None:
            try:
                original = (
                    _resolve_asset_path(pack_dir, source.original_path)
                    if source.provenance_type == PROJECT_AUTHORED_PROVENANCE_TYPE
                    else _resolve_original_path(pack_dir, source.original_path)
                )
            except ValueError as exc:
                errors.append(f"source {source.id!r} original_path is invalid: {exc}")
            else:
                if not original.is_file():
                    errors.append(f"source {source.id!r} original_path does not exist: {original}")
                else:
                    try:
                        actual = _sha256_file(original)
                    except OSError as exc:
                        errors.append(f"cannot hash source {source.id!r} original_path: {exc}")
                    else:
                        if actual != source.source_sha256:
                            errors.append(
                                f"source {source.id!r} original_path SHA-256 differs from declared source_sha256 "
                                f"({actual} != {source.source_sha256})"
                            )

        # External sources retain the URL + checksum ledger. Project-authored
        # masters additionally prove that every declared generator input exists
        # at, and hashes to, the pinned Git revision.
        if source.provenance_type != PROJECT_AUTHORED_PROVENANCE_TYPE or source.generator is None:
            continue
        generator = source.generator
        pins = [(generator.path, generator.sha256)]
        pins.extend((dependency.path, dependency.sha256) for dependency in generator.dependencies)
        for path, expected_sha256 in pins:
            key = (generator.revision, path)
            result = pinned_blob_hashes.get(key)
            if result is None:
                try:
                    blob = _read_git_blob(*key)
                except RuntimeError as exc:
                    result = exc
                else:
                    result = hashlib.sha256(blob).hexdigest()
                pinned_blob_hashes[key] = result
            report_key = (*key, expected_sha256)
            if report_key in reported_pins:
                continue
            if isinstance(result, RuntimeError):
                errors.append(f"cannot verify generated-source Git blob {generator.revision}:{path}: {result}")
                reported_pins.add(report_key)
            elif result != expected_sha256:
                errors.append(
                    f"generated-source Git blob {generator.revision}:{path} SHA-256 differs from manifest "
                    f"({result} != {expected_sha256})"
                )
                reported_pins.add(report_key)


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


def _validate_exact_duplicate_outputs(assets: tuple[AssetRecord, ...], errors: list[str]) -> None:
    by_digest: dict[str, list[str]] = {}
    by_path: dict[str, list[str]] = {}
    for asset in assets:
        by_digest.setdefault(asset.sha256, []).append(asset.id)
        by_path.setdefault(asset.path, []).append(asset.id)
    for digest, asset_ids in sorted(by_digest.items()):
        if len(asset_ids) > 1:
            errors.append(
                "exact duplicate output SHA-256 "
                f"{digest} is declared by distinct assets: {', '.join(sorted(asset_ids))}"
            )
    for path, asset_ids in sorted(by_path.items()):
        if len(asset_ids) > 1:
            errors.append(f"output path {path!r} is declared by distinct assets: {', '.join(sorted(asset_ids))}")


def _pack_digest(assets: Iterable[AssetRecord]) -> str:
    """Bind a listening receipt to paths and delivered bytes, not manifest ordering."""
    digest = hashlib.sha256()
    for asset in sorted(assets, key=lambda item: (item.path, item.id)):
        digest.update(asset.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(asset.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_quality_allowlist(
    value: Any,
    sources: tuple[SourceRecord, ...],
    assets: tuple[AssetRecord, ...],
    errors: list[str],
) -> QualityGuardAllowlist:
    if not isinstance(value, dict):
        errors.append(f"{QUALITY_GUARD_KEY} must be an object")
        return QualityGuardAllowlist(frozenset(), frozenset())
    supported = {"perceptual_overlaps", "source_core_roles"}
    for key in sorted(set(value) - supported):
        errors.append(f"{QUALITY_GUARD_KEY} contains unsupported field {key!r}")

    asset_ids = {asset.id for asset in assets}
    source_ids = {source.id for source in sources}
    overlap_pairs: set[tuple[str, str]] = set()
    raw_overlaps = value.get("perceptual_overlaps")
    if not isinstance(raw_overlaps, list):
        errors.append(f"{QUALITY_GUARD_KEY}.perceptual_overlaps must be a list")
    else:
        for index, raw in enumerate(raw_overlaps):
            label = f"{QUALITY_GUARD_KEY}.perceptual_overlaps[{index}]"
            if not isinstance(raw, dict):
                errors.append(f"{label} must be an object")
                continue
            raw_ids = raw.get("asset_ids")
            if not isinstance(raw_ids, list) or len(raw_ids) != 2:
                errors.append(f"{label}.asset_ids must contain exactly 2 asset ids")
                continue
            resolved = [
                _identifier(item, f"{label}.asset_ids[{position}]", errors) for position, item in enumerate(raw_ids)
            ]
            reason = _nonempty_text(raw.get("reason"), f"{label}.reason", errors)
            if any(asset_id is None for asset_id in resolved) or reason is None:
                continue
            first, second = resolved
            assert first is not None and second is not None
            if first == second:
                errors.append(f"{label}.asset_ids must name distinct assets")
                continue
            for asset_id in (first, second):
                if asset_id not in asset_ids:
                    errors.append(f"{label}.asset_ids references undeclared asset {asset_id!r}")
            pair = (first, second) if first < second else (second, first)
            if pair in overlap_pairs:
                errors.append(f"{label}.asset_ids duplicates allowlisted pair {pair!r}")
            overlap_pairs.add(pair)

    allowed_sources: set[str] = set()
    raw_source_roles = value.get("source_core_roles")
    if not isinstance(raw_source_roles, list):
        errors.append(f"{QUALITY_GUARD_KEY}.source_core_roles must be a list")
    else:
        for index, raw in enumerate(raw_source_roles):
            label = f"{QUALITY_GUARD_KEY}.source_core_roles[{index}]"
            if not isinstance(raw, dict):
                errors.append(f"{label} must be an object")
                continue
            source_id = _identifier(raw.get("source_id"), f"{label}.source_id", errors)
            reason = _nonempty_text(raw.get("reason"), f"{label}.reason", errors)
            if source_id is None or reason is None:
                continue
            if source_id not in source_ids:
                errors.append(f"{label}.source_id references undeclared source {source_id!r}")
            if source_id in allowed_sources:
                errors.append(f"{label}.source_id duplicates allowlisted source {source_id!r}")
            allowed_sources.add(source_id)
    return QualityGuardAllowlist(frozenset(overlap_pairs), frozenset(allowed_sources))


def _validate_layer_provenance(
    sources: tuple[SourceRecord, ...],
    assets: tuple[AssetRecord, ...],
    errors: list[str],
) -> None:
    source_map = {source.id: source for source in sources}
    for asset in assets:
        layer_source_ids: list[str] = []
        for index, layer in enumerate(asset.layers):
            label = f"asset {asset.id!r} layer[{index}]"
            source = source_map.get(layer.source_id)
            if source is None:
                errors.append(f"{label} references undeclared source {layer.source_id!r}")
                continue
            layer_source_ids.append(layer.source_id)
            if layer.source_sha256 != source.source_sha256:
                errors.append(f"{label} source_sha256 does not match source {source.id!r}")
            if layer.license != source.license:
                errors.append(f"{label} license does not match source {source.id!r}")
            if asset.duration_target_sec is not None:
                layer_end = layer.output_offset_sec + layer.duration_sec
                if layer_end > asset.duration_target_sec + 0.001:
                    errors.append(
                        f"{label} ends at {layer_end:.3f}s, beyond asset duration {asset.duration_target_sec:.3f}s"
                    )
        if set(layer_source_ids) != set(asset.source_ids):
            errors.append(
                f"asset {asset.id!r} source_ids must exactly match its layer source ids "
                f"({sorted(asset.source_ids)!r} != {sorted(set(layer_source_ids))!r})"
            )


def _validate_source_concentration(
    assets: tuple[AssetRecord, ...],
    allowlist: QualityGuardAllowlist,
    errors: list[str],
) -> None:
    roles_by_source: dict[str, set[str]] = {}
    for asset in assets:
        if asset.kind not in CORE_ASSET_KINDS:
            continue
        for layer in asset.layers:
            if layer.role == "foreground":
                roles_by_source.setdefault(layer.source_id, set()).add(asset.id)
    for source_id, asset_ids in sorted(roles_by_source.items()):
        if len(asset_ids) > 2 and source_id not in allowlist.source_core_roles:
            errors.append(
                f"foreground source {source_id!r} appears in {len(asset_ids)} core roles "
                f"({', '.join(sorted(asset_ids))}); maximum is 2 unless explicitly allowlisted"
            )


def _validate_startup_synth_semantics(
    sources: tuple[SourceRecord, ...],
    assets: tuple[AssetRecord, ...],
    errors: list[str],
) -> None:
    source_map = {source.id: source for source in sources}
    startup = next((asset for asset in assets if asset.id == "compat.startup-synth"), None)
    if startup is None:
        return
    if not _has_startup_synth_semantics(startup.tags):
        errors.append(
            f"asset 'compat.startup-synth' must include a semantic synth tag: {', '.join(sorted(STARTUP_SYNTH_TAGS))}"
        )
    asset_forbidden = _startup_synth_forbidden_terms(startup.tags)
    if asset_forbidden:
        errors.append(
            "asset 'compat.startup-synth' cannot declare brass/trumpet semantics "
            f"(forbidden tags: {', '.join(sorted(asset_forbidden))})"
        )
    has_source_semantics = False
    for layer in startup.layers:
        source = source_map.get(layer.source_id)
        if source is None:
            continue
        metadata = {
            "id": (source.id,),
            "title": (source.title,),
            "tags": source.tags,
            "modification": (source.modification,),
        }
        if any(_has_startup_synth_semantics(values) for values in metadata.values()):
            has_source_semantics = True
        forbidden_by_field = {
            field: forbidden
            for field, values in metadata.items()
            if (forbidden := _startup_synth_forbidden_terms(values))
        }
        if forbidden_by_field:
            detail = ", ".join(
                f"{field}={'+'.join(sorted(forbidden))}" for field, forbidden in forbidden_by_field.items()
            )
            errors.append(
                "asset 'compat.startup-synth' cannot use brass/trumpet source "
                f"{source.id!r} (forbidden metadata: {detail})"
            )
    if not has_source_semantics:
        errors.append(
            "asset 'compat.startup-synth' must reference at least one source with "
            "synth/e-piano/Rhodes semantics in its id, title, tags, or modification"
        )


def _has_startup_synth_semantics(values: Iterable[str]) -> bool:
    return any(STARTUP_SYNTH_SEMANTIC_RE.search(value) for value in values)


def _startup_synth_forbidden_terms(values: Iterable[str]) -> frozenset[str]:
    return frozenset(
        match.group(1).casefold()
        for value in values
        for match in STARTUP_SYNTH_FORBIDDEN_RE.finditer(value)
        if match.group(1).casefold() in STARTUP_SYNTH_FORBIDDEN_TERMS
    )


def _correlation_window_samples() -> int:
    return math.ceil(CORRELATION_WINDOW_SEC * CORRELATION_NATIVE_SAMPLE_RATE_HZ)


def _decode_audio(path: Path) -> DecodedAudio:
    """Decode bounded native stereo without discarding band or channel identity."""
    # Request one sample beyond the accepted ceiling so the caller can reject
    # oversized audio without ever asking subprocess.run to capture an
    # unbounded decoded stream in memory.
    decode_duration_sec = (CORRELATION_MAX_SAMPLES_PER_ASSET + 1) / CORRELATION_NATIVE_SAMPLE_RATE_HZ
    try:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-ac",
                "2",
                "-ar",
                str(CORRELATION_NATIVE_SAMPLE_RATE_HZ),
                "-t",
                f"{decode_duration_sec:.9f}",
                "-fs",
                str((CORRELATION_MAX_SAMPLES_PER_ASSET + 1) * 4),
                "-f",
                "s16le",
                "pipe:1",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required for perceptual-overlap validation") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffmpeg timed out while decoding {path}") from exc
    if completed.returncode != 0:
        raise RuntimeError(f"ffmpeg exited {completed.returncode}")
    interleaved = array.array("h")
    interleaved.frombytes(completed.stdout)
    if sys.byteorder != "little":
        interleaved.byteswap()
    if len(interleaved) % 2:
        raise RuntimeError("ffmpeg returned an incomplete stereo sample frame")
    return DecodedAudio(left=interleaved[0::2], right=interleaved[1::2])


def _decoded_channels(decoded: DecodedAudio | Sequence[int]) -> tuple[tuple[str, Sequence[int]], ...]:
    """Return unique channel views; plain sequences keep unit fixtures concise."""
    if not isinstance(decoded, DecodedAudio):
        return (("mono", decoded),)
    if decoded.left == decoded.right:
        return (("L/R", decoded.left),)
    return (("L", decoded.left), ("R", decoded.right))


def _decoded_sample_count(decoded: DecodedAudio | Sequence[int]) -> int:
    if isinstance(decoded, DecodedAudio):
        return decoded.sample_count
    return len(decoded)


def _native_region_workload(regions: Sequence[tuple[int, int, int]]) -> tuple[int, int, int]:
    exact_windows = sum(end - start + 1 for _lag, start, end in regions)
    primitive_work = len(regions) * 5 * _correlation_window_samples() + 32 * exact_windows
    return len(regions), exact_windows, primitive_work


def _correlation_grid_samples() -> int:
    return round(CORRELATION_GRID_SEC * CORRELATION_NATIVE_SAMPLE_RATE_HZ)


def _correlation_inner_margin_samples() -> int:
    return round(CORRELATION_INNER_MARGIN_SEC * CORRELATION_NATIVE_SAMPLE_RATE_HZ)


def _xorshift32(state: int) -> int:
    state ^= (state << 13) & 0xFFFFFFFF
    state ^= state >> 17
    state ^= (state << 5) & 0xFFFFFFFF
    return state & 0xFFFFFFFF


def _correlation_projection_pairs() -> tuple[tuple[tuple[int, int], ...], ...]:
    """Return the fixed xorshift projections used by every pack validation."""
    inner_samples = _correlation_window_samples() - 2 * _correlation_inner_margin_samples()
    state = 0x4D4E4452
    projections: list[tuple[tuple[int, int], ...]] = []
    for _ in range(CORRELATION_SIGNATURE_BITS):
        pairs: list[tuple[int, int]] = []
        for _pair in range(CORRELATION_PROJECTION_PAIRS_PER_BIT):
            state = _xorshift32(state)
            first = state % inner_samples
            state = _xorshift32(state)
            second = state % inner_samples
            if first == second:
                second = (second + 1) % inner_samples
            pairs.append((first, second))
        projections.append(tuple(pairs))
    return tuple(projections)


CORRELATION_PROJECTION_PAIRS = _correlation_projection_pairs()


def _signature_start_cells(maximum_start: int) -> tuple[tuple[int, int, int], ...]:
    """Partition every valid start into one nearest 2 ms grid anchor."""
    if maximum_start < 0:
        return ()
    hop = _correlation_grid_samples()
    anchors = list(range(0, maximum_start + 1, hop))
    if anchors[-1] != maximum_start:
        anchors.append(maximum_start)
    cells: list[tuple[int, int, int]] = []
    for index, anchor in enumerate(anchors):
        first_start = 0 if index == 0 else (anchors[index - 1] + anchor + 1) // 2
        last_start = maximum_start if index + 1 == len(anchors) else (anchor + anchors[index + 1] - 1) // 2
        cells.append((anchor, first_start, last_start))
    return tuple(cells)


def _build_pair_projection_signatures(samples: Sequence[int]) -> tuple[CorrelationSignature, ...]:
    """Build full 256-bit signatures over the grid's protected inner 496 ms."""
    maximum_start = len(samples) - _correlation_window_samples()
    margin = _correlation_inner_margin_samples()
    signatures: list[CorrelationSignature] = []
    for anchor, first_start, last_start in _signature_start_cells(maximum_start):
        inner_start = anchor + margin
        signs = 0
        nonzero = 0
        for bit, pairs in enumerate(CORRELATION_PROJECTION_PAIRS):
            difference = sum(
                samples[inner_start + first_offset] - samples[inner_start + second_offset]
                for first_offset, second_offset in pairs
            )
            if difference:
                nonzero |= 1 << bit
                if difference > 0:
                    signs |= 1 << bit
        signatures.append(
            CorrelationSignature(
                anchor=anchor,
                first_start=first_start,
                last_start=last_start,
                signs=signs,
                nonzero=nonzero,
            )
        )
    return tuple(signatures)


def _pair_projection_distance(first: CorrelationSignature, second: CorrelationSignature) -> int | None:
    """Return polarity-aware sign/nonzero distance, or no evidence for silence."""
    common_nonzero = first.nonzero & second.nonzero
    if common_nonzero.bit_count() < CORRELATION_MIN_COMMON_NONZERO_BITS:
        return None
    zero_disagreement = (first.nonzero ^ second.nonzero).bit_count()
    sign_disagreement = ((first.signs ^ second.signs) & common_nonzero).bit_count()
    inverted_disagreement = ((first.signs ^ ~second.signs) & common_nonzero).bit_count()
    return zero_disagreement + min(sign_disagreement, inverted_disagreement)


def _iter_pair_projection_matches(
    first: Sequence[CorrelationSignature],
    second: Sequence[CorrelationSignature],
) -> Iterator[tuple[int, int, int, int]]:
    """Yield every matching pair of disjoint native-start cells."""
    for first_signature in first:
        for second_signature in second:
            distance = _pair_projection_distance(first_signature, second_signature)
            if distance is not None and distance <= CORRELATION_SIGNATURE_DISTANCE:
                yield (
                    first_signature.first_start,
                    first_signature.last_start,
                    second_signature.first_start,
                    second_signature.last_start,
                )


def _landmark_descriptor(kind: str, view: str, values: Sequence[int], center: int) -> tuple[str, int, int] | None:
    """Return a polarity-canonical 64-derivative local sign descriptor."""
    if view == "centered":
        first = center - CORRELATION_LANDMARK_DESCRIPTOR_BITS // 2
    elif view == "forward":
        first = center
    elif view == "backward":
        first = center - CORRELATION_LANDMARK_DESCRIPTOR_BITS + 1
    else:
        raise ValueError(f"unknown landmark descriptor view: {view}")
    last = first + CORRELATION_LANDMARK_DESCRIPTOR_BITS
    signs = 0
    nonzero = 0
    for bit, index in enumerate(range(first, last)):
        value = values[index] if 0 <= index < len(values) else 0
        if value:
            nonzero |= 1 << bit
            if value > 0:
                signs |= 1 << bit
    if not nonzero:
        return None
    inverted = (~signs) & nonzero
    return f"{kind}:{view}", nonzero, min(signs, inverted)


def _landmark_magnitude_order(view: str, values: Sequence[int], center: int) -> int:
    """Return gain/polarity-invariant local magnitude-order bits."""
    if view == "forward":
        first = center
    elif view == "backward":
        first = center - CORRELATION_LANDMARK_DESCRIPTOR_BITS + 1
    else:
        first = center - CORRELATION_LANDMARK_DESCRIPTOR_BITS // 2
    magnitudes = [
        abs(values[index]) if 0 <= index < len(values) else 0
        for index in range(first, first + CORRELATION_LANDMARK_DESCRIPTOR_BITS)
    ]
    ordering = 0
    for bit, magnitude in enumerate(magnitudes):
        peer = magnitudes[(bit * 17 + 13) % CORRELATION_LANDMARK_DESCRIPTOR_BITS]
        if magnitude > peer:
            ordering |= 1 << bit
    return ordering


def _build_landmark_postings(
    samples: Sequence[int],
    *,
    maximum_events: int = CORRELATION_MAX_LANDMARK_EVENTS,
) -> tuple[CorrelationLandmarkIndex, int]:
    """Build one compact sorted index for every intrinsic derivative event."""
    if len(samples) < CORRELATION_LANDMARK_DESCRIPTOR_BITS + 4:
        return CorrelationLandmarkIndex(array.array("Q")), 0
    first_difference = array.array("i", (samples[index + 1] - samples[index] for index in range(len(samples) - 1)))
    second_difference = array.array(
        "i", (first_difference[index + 1] - first_difference[index] for index in range(len(first_difference) - 1))
    )
    event_kinds = bytearray(len(samples))
    for index in range(len(first_difference)):
        magnitude = abs(first_difference[index])
        previous_magnitude = abs(first_difference[index - 1]) if index else 0
        next_magnitude = abs(first_difference[index + 1]) if index + 1 < len(first_difference) else 0
        if magnitude > previous_magnitude and magnitude >= next_magnitude:
            event_kinds[index + 1] |= 1
        if index and (
            first_difference[index - 1]
            and first_difference[index]
            and (first_difference[index - 1] > 0) != (first_difference[index] > 0)
        ):
            event_kinds[index] |= 2
    for index in range(len(second_difference)):
        magnitude = abs(second_difference[index])
        previous_magnitude = abs(second_difference[index - 1]) if index else 0
        next_magnitude = abs(second_difference[index + 1]) if index + 1 < len(second_difference) else 0
        if magnitude > previous_magnitude and magnitude >= next_magnitude:
            event_kinds[index + 1] |= 4

    records: list[int] = []
    view_ids = {"forward": 1, "backward": 2, "centered": 3}
    for position, kind_mask in enumerate(event_kinds):
        if not kind_mask:
            continue
        center = min(position, len(first_difference) - 1)
        for view, view_id in view_ids.items():
            descriptor = _landmark_descriptor(str(kind_mask), view, first_difference, center)
            if descriptor is None:
                continue
            _kind, nonzero, signs = descriptor
            if view == "centered" and nonzero.bit_count() > 8:
                continue
            if len(records) >= maximum_events:
                raise OverflowError("perceptual-overlap landmark-event budget exceeded")
            magnitude_order = _landmark_magnitude_order(view, first_difference, center)
            key_hash = 0xCBF29CE484222325
            for value in (kind_mask, view_id, nonzero, signs, magnitude_order):
                key_hash ^= value
                key_hash = (key_hash * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
            descriptor_hash = key_hash & (CORRELATION_LANDMARK_SPARSE_KEY_BIT - 1)
            if nonzero.bit_count() <= 8:
                descriptor_hash |= CORRELATION_LANDMARK_SPARSE_KEY_BIT
            packed = (descriptor_hash << CORRELATION_LANDMARK_POSITION_BITS) | position
            records.append(packed)
    return CorrelationLandmarkIndex(array.array("Q", sorted(records))), len(records)


def _landmark_join_workload(
    first: CorrelationLandmarkIndex,
    second: CorrelationLandmarkIndex,
) -> int:
    first_records = first.records
    second_records = second.records
    first_index = 0
    second_index = 0
    joins = 0
    while first_index < len(first_records) and second_index < len(second_records):
        first_key = first_records[first_index] >> CORRELATION_LANDMARK_POSITION_BITS
        second_key = second_records[second_index] >> CORRELATION_LANDMARK_POSITION_BITS
        if first_key < second_key:
            first_index += 1
            continue
        if second_key < first_key:
            second_index += 1
            continue
        first_end = first_index + 1
        while (
            first_end < len(first_records)
            and first_records[first_end] >> CORRELATION_LANDMARK_POSITION_BITS == first_key
        ):
            first_end += 1
        second_end = second_index + 1
        while (
            second_end < len(second_records)
            and second_records[second_end] >> CORRELATION_LANDMARK_POSITION_BITS == second_key
        ):
            second_end += 1
        joins += (first_end - first_index) * (second_end - second_index)
        first_index = first_end
        second_index = second_end
    return joins


def _matched_landmark_regions(
    first: CorrelationLandmarkIndex,
    second: CorrelationLandmarkIndex,
    first_sample_count: int,
    second_sample_count: int,
    *,
    maximum_segments: int,
) -> tuple[tuple[tuple[int, int, int], ...], int]:
    """Nominate jittered lags only where a window contains both events."""
    evidence_by_center: dict[int, list[tuple[int, int]]] = {}
    dense_evidence_by_center: dict[int, list[tuple[int, int]]] = {}
    support_by_center: dict[int, int] = {}
    sparse_support_by_center: dict[int, int] = {}
    sparse_first_key_positions: dict[int, list[int]] = {}
    clustered_sparse_centers: set[int] = set()
    dense_qualified_centers: set[int] = set()
    jitter = CORRELATION_LANDMARK_JITTER_SAMPLES
    window_samples = _correlation_window_samples()
    maximum_first_start = first_sample_count - window_samples
    maximum_second_start = second_sample_count - window_samples
    segment_count = 0

    def add_interval(mapping: dict[int, list[tuple[int, int]]], key: int, first_start: int, last_start: int) -> None:
        intervals = mapping.setdefault(key, [])
        insertion = bisect.bisect_left(intervals, (first_start, last_start))
        if insertion and intervals[insertion - 1][1] + 1 >= first_start:
            insertion -= 1
            first_start = min(first_start, intervals[insertion][0])
            last_start = max(last_start, intervals[insertion][1])
            intervals.pop(insertion)
        while insertion < len(intervals) and intervals[insertion][0] <= last_start + 1:
            first_start = min(first_start, intervals[insertion][0])
            last_start = max(last_start, intervals[insertion][1])
            intervals.pop(insertion)
        intervals.insert(insertion, (first_start, last_start))

    first_records = first.records
    second_records = second.records
    first_index = 0
    second_index = 0
    while first_index < len(first_records) and second_index < len(second_records):
        first_key = first_records[first_index] >> CORRELATION_LANDMARK_POSITION_BITS
        second_key = second_records[second_index] >> CORRELATION_LANDMARK_POSITION_BITS
        if first_key < second_key:
            first_index += 1
            continue
        if second_key < first_key:
            second_index += 1
            continue
        first_end = first_index + 1
        while (
            first_end < len(first_records)
            and first_records[first_end] >> CORRELATION_LANDMARK_POSITION_BITS == first_key
        ):
            first_end += 1
        second_end = second_index + 1
        while (
            second_end < len(second_records)
            and second_records[second_end] >> CORRELATION_LANDMARK_POSITION_BITS == second_key
        ):
            second_end += 1
        is_sparse_key = bool(first_key & CORRELATION_LANDMARK_SPARSE_KEY_BIT)
        # Matching descriptor keys are contiguous in both packed indexes. Keep
        # one sorted position list only until a center receives its second
        # sparse key, then decide the narrow two-key exception in one linear
        # merge. This avoids retaining and sorting every sparse join in a pack.
        current_sparse_positions: dict[int, list[int]] = {}
        for first_record_index in range(first_index, first_end):
            first_position = first_records[first_record_index] & CORRELATION_LANDMARK_POSITION_MASK
            for second_record_index in range(second_index, second_end):
                second_position = second_records[second_record_index] & CORRELATION_LANDMARK_POSITION_MASK
                center = second_position - first_position
                support_by_center[center] = support_by_center.get(center, 0) | (1 << (first_key & 63))
                if is_sparse_key:
                    current_sparse_positions.setdefault(center, []).append(first_position)
                first_start = max(0, -center, first_position - window_samples + 1)
                last_start = min(maximum_first_start, maximum_second_start - center, first_position)
                if first_start <= last_start:
                    if is_sparse_key:
                        add_interval(evidence_by_center, center, first_start, last_start)
                    else:
                        dense_evidence_by_center.setdefault(center, []).append((first_position, first_key))
        if is_sparse_key:
            for center, positions in current_sparse_positions.items():
                support = sparse_support_by_center.get(center, 0) + 1
                sparse_support_by_center[center] = support
                if support == 1:
                    sparse_first_key_positions[center] = positions
                    continue
                if support == 2:
                    previous_positions = sparse_first_key_positions.pop(center)
                    previous_index = 0
                    current_index = 0
                    while previous_index < len(previous_positions) and current_index < len(positions):
                        previous_position = previous_positions[previous_index]
                        current_position = positions[current_index]
                        if abs(previous_position - current_position) <= CORRELATION_LANDMARK_SPARSE_PAIR_SPAN_SAMPLES:
                            clustered_sparse_centers.add(center)
                            break
                        if previous_position < current_position:
                            previous_index += 1
                        else:
                            current_index += 1
                    continue
                sparse_first_key_positions.pop(center, None)
        first_index = first_end
        second_index = second_end
    for center, evidence in dense_evidence_by_center.items():
        if support_by_center[center].bit_count() < 40:
            continue
        ordered = sorted(evidence)
        key_counts: dict[int, int] = {}
        left = 0
        for right_position, right_key in ordered:
            key_counts[right_key] = key_counts.get(right_key, 0) + 1
            while right_position - ordered[left][0] >= window_samples:
                left_key = ordered[left][1]
                key_counts[left_key] -= 1
                if key_counts[left_key] == 0:
                    del key_counts[left_key]
                left += 1
            if len(key_counts) < 40:
                continue
            first_start = max(0, -center, right_position - window_samples + 1)
            last_start = min(maximum_first_start, maximum_second_start - center, ordered[left][0])
            if first_start <= last_start:
                add_interval(evidence_by_center, center, first_start, last_start)
                dense_qualified_centers.add(center)
    intervals_by_lag: dict[int, list[tuple[int, int]]] = {}
    for center, evidence_intervals in evidence_by_center.items():
        sparse_support = sparse_support_by_center.get(center, 0)
        if sparse_support < 3 and center not in clustered_sparse_centers and center not in dense_qualified_centers:
            continue
        for lag in range(center - jitter, center + jitter + 1):
            if lag not in intervals_by_lag and len(intervals_by_lag) >= CORRELATION_MAX_LANDMARK_LAGS:
                raise OverflowError("perceptual-overlap landmark-lag budget exceeded")
            for evidence_start, evidence_end in evidence_intervals:
                if segment_count >= maximum_segments:
                    raise OverflowError("perceptual-overlap landmark-region budget exceeded")
                first_start = max(0, -lag, evidence_start - jitter)
                last_start = min(maximum_first_start, maximum_second_start - lag, evidence_end + jitter)
                if first_start <= last_start:
                    add_interval(intervals_by_lag, lag, first_start, last_start)
                    segment_count += 1
    regions: list[tuple[int, int, int]] = []
    for lag, intervals in intervals_by_lag.items():
        regions.extend((lag, first_start, last_start) for first_start, last_start in intervals)
    return tuple(regions), len(intervals_by_lag)


def _add_rectangle_regions(
    intervals_by_lag: dict[int, list[tuple[int, int]]],
    rectangle: tuple[int, int, int, int],
) -> int:
    first_min, first_max, second_min, second_max = rectangle
    added = 0
    for lag in range(second_min - first_max, second_max - first_min + 1):
        start = max(first_min, second_min - lag)
        end = min(first_max, second_max - lag)
        if start <= end:
            intervals_by_lag.setdefault(lag, []).append((start, end))
            added += 1
    return added


def _full_lag_region(first_sample_count: int, second_sample_count: int, lag: int) -> tuple[int, int, int] | None:
    window_count = _native_window_count(first_sample_count, second_sample_count, lag)
    if window_count <= 0:
        return None
    first_start = max(0, -lag)
    return lag, first_start, first_start + window_count - 1


def _merge_native_regions(
    intervals_by_lag: dict[int, list[tuple[int, int]]],
    full_lags: Iterable[int],
    first_sample_count: int,
    second_sample_count: int,
) -> tuple[tuple[int, int, int], ...]:
    """Union signature cells and full landmark lags without duplicate work."""
    for lag in full_lags:
        region = _full_lag_region(first_sample_count, second_sample_count, lag)
        if region is not None:
            intervals_by_lag[lag] = [(region[1], region[2])]
    regions: list[tuple[int, int, int]] = []
    for lag in sorted(intervals_by_lag):
        merged: list[tuple[int, int]] = []
        for start, end in sorted(intervals_by_lag[lag]):
            if not merged or start > merged[-1][1] + 1:
                merged.append((start, end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        regions.extend((lag, start, end) for start, end in merged)
    return tuple(regions)


def _native_window_count(first_sample_count: int, second_sample_count: int, lag: int) -> int:
    first_start = max(0, -lag)
    second_start = first_start + lag
    overlap = min(first_sample_count - first_start, second_sample_count - second_start)
    return max(0, overlap - _correlation_window_samples() + 1)


def _maximum_native_correlation_at_lag(
    first: Sequence[int],
    second: Sequence[int],
    lag: int,
) -> float:
    """Exact rolling Pearson correlation for every native window at one lag."""
    window_count = _native_window_count(len(first), len(second), lag)
    if window_count <= 0:
        return 0.0
    first_start = max(0, -lag)
    return _maximum_native_correlation_in_region(
        first,
        second,
        lag,
        first_start,
        first_start + window_count - 1,
    )


def _maximum_native_correlation_in_region(
    first: Sequence[int],
    second: Sequence[int],
    lag: int,
    first_start: int,
    last_start: int,
) -> float:
    """Exact rolling Pearson over one bounded native diagonal interval."""
    window_samples = _correlation_window_samples()
    window_count = last_start - first_start + 1
    if window_count <= 0:
        return 0.0
    second_start = first_start + lag
    first_window = first[first_start : first_start + window_samples]
    second_window = second[second_start : second_start + window_samples]
    first_sum = sum(first_window)
    second_sum = sum(second_window)
    first_sum_squares = sum(value * value for value in first_window)
    second_sum_squares = sum(value * value for value in second_window)
    product_sum = sum(a * b for a, b in zip(first_window, second_window, strict=True))
    maximum = 0.0
    for offset in range(window_count):
        numerator = window_samples * product_sum - first_sum * second_sum
        first_energy = window_samples * first_sum_squares - first_sum * first_sum
        second_energy = window_samples * second_sum_squares - second_sum * second_sum
        if first_energy > 0 and second_energy > 0:
            maximum = max(maximum, abs(numerator / math.sqrt(first_energy * second_energy)))
            if maximum >= CORRELATION_THRESHOLD:
                return maximum
        if offset + 1 < window_count:
            first_outgoing = first[first_start + offset]
            second_outgoing = second[second_start + offset]
            first_incoming = first[first_start + offset + window_samples]
            second_incoming = second[second_start + offset + window_samples]
            first_sum += first_incoming - first_outgoing
            second_sum += second_incoming - second_outgoing
            first_sum_squares += first_incoming * first_incoming - first_outgoing * first_outgoing
            second_sum_squares += second_incoming * second_incoming - second_outgoing * second_outgoing
            product_sum += first_incoming * second_incoming - first_outgoing * second_outgoing
    return maximum


def _plan_correlation_channel_pair(
    first: AssetRecord,
    second: AssetRecord,
    first_label: str,
    second_label: str,
    first_samples: Sequence[int],
    second_samples: Sequence[int],
    first_signatures: Sequence[CorrelationSignature],
    second_signatures: Sequence[CorrelationSignature],
    first_landmarks: CorrelationLandmarkIndex,
    second_landmarks: CorrelationLandmarkIndex,
    totals: dict[str, int],
    errors: list[str],
    *,
    use_signatures: bool,
    use_landmarks: bool,
) -> CorrelationPairPlan | None:
    """Materialize one plan while charging every retained item globally."""
    signature_matches: list[tuple[int, int, int, int]] = []
    if use_signatures:
        for match in _iter_pair_projection_matches(first_signatures, second_signatures):
            if totals["signature_matches"] >= CORRELATION_MAX_SIGNATURE_MATCHES:
                errors.append(
                    f"perceptual-overlap signature-match budget exceeded (> {CORRELATION_MAX_SIGNATURE_MATCHES})"
                )
                return None
            signature_matches.append(match)
            totals["signature_matches"] += 1

    landmark_regions: tuple[tuple[int, int, int], ...] = ()
    if use_landmarks:
        landmark_joins = _landmark_join_workload(first_landmarks, second_landmarks)
        totals["landmark_joins"] += landmark_joins
        if totals["landmark_joins"] > CORRELATION_MAX_LANDMARK_JOINS:
            errors.append(
                "perceptual-overlap landmark-join budget exceeded "
                f"({totals['landmark_joins']} > {CORRELATION_MAX_LANDMARK_JOINS})"
            )
            return None
        try:
            landmark_regions, landmark_lag_count = _matched_landmark_regions(
                first_landmarks,
                second_landmarks,
                len(first_samples),
                len(second_samples),
                maximum_segments=CORRELATION_MAX_LANDMARK_REGION_PROBES,
            )
        except OverflowError as exc:
            errors.append(str(exc))
            return None
        totals["landmark_lags"] += landmark_lag_count
        if totals["landmark_lags"] > CORRELATION_MAX_LANDMARK_LAGS:
            errors.append(
                "perceptual-overlap landmark-lag budget exceeded "
                f"({totals['landmark_lags']} > {CORRELATION_MAX_LANDMARK_LAGS})"
            )
            return None

    intervals_by_lag: dict[int, list[tuple[int, int]]] = {}
    for rectangle in signature_matches:
        rectangle_regions = (rectangle[1] - rectangle[0]) + (rectangle[3] - rectangle[2]) + 1
        totals["projected_regions"] += rectangle_regions
        if totals["projected_regions"] > CORRELATION_MAX_NATIVE_REGIONS:
            errors.append(
                "perceptual-overlap native-region preallocation budget exceeded "
                f"({totals['projected_regions']} > {CORRELATION_MAX_NATIVE_REGIONS})"
            )
            return None
        _add_rectangle_regions(intervals_by_lag, rectangle)
    totals["projected_regions"] += len(landmark_regions)
    if totals["projected_regions"] > CORRELATION_MAX_NATIVE_REGIONS:
        errors.append(
            "perceptual-overlap native-region preallocation budget exceeded "
            f"({totals['projected_regions']} > {CORRELATION_MAX_NATIVE_REGIONS})"
        )
        return None
    if totals["native_regions"] + len(landmark_regions) > CORRELATION_MAX_NATIVE_REGIONS:
        errors.append(
            "perceptual-overlap native-region preallocation budget exceeded "
            f"({totals['native_regions'] + len(landmark_regions)} > {CORRELATION_MAX_NATIVE_REGIONS})"
        )
        return None
    for lag, first_start, last_start in landmark_regions:
        intervals_by_lag.setdefault(lag, []).append((first_start, last_start))
    regions = _merge_native_regions(
        intervals_by_lag,
        (),
        len(first_samples),
        len(second_samples),
    )
    if not regions:
        return None
    totals["native_regions"] += len(regions)
    if totals["native_regions"] > CORRELATION_MAX_NATIVE_REGIONS:
        errors.append(
            "perceptual-overlap native-region budget exceeded "
            f"({totals['native_regions']} > {CORRELATION_MAX_NATIVE_REGIONS})"
        )
        return None

    native_lags, native_windows, native_work = _native_region_workload(regions)
    totals["native_lags"] += native_lags
    totals["native_windows"] += native_windows
    totals["native_work"] += native_work
    if totals["native_lags"] > CORRELATION_MAX_NATIVE_LAG_CANDIDATES:
        errors.append(
            "perceptual-overlap native-lag budget exceeded "
            f"({totals['native_lags']} > {CORRELATION_MAX_NATIVE_LAG_CANDIDATES})"
        )
        return None
    if totals["native_windows"] > CORRELATION_MAX_NATIVE_EXACT_WINDOWS:
        errors.append(
            "perceptual-overlap native-window budget exceeded "
            f"({totals['native_windows']} > {CORRELATION_MAX_NATIVE_EXACT_WINDOWS})"
        )
        return None
    if totals["native_work"] > CORRELATION_MAX_NATIVE_PRIMITIVE_WORK:
        errors.append(
            "perceptual-overlap native-work budget exceeded "
            f"({totals['native_work']} > {CORRELATION_MAX_NATIVE_PRIMITIVE_WORK})"
        )
        return None
    return CorrelationPairPlan(
        first=first,
        second=second,
        first_label=first_label,
        second_label=second_label,
        first_samples=first_samples,
        second_samples=second_samples,
        regions=regions,
    )


def _confirm_correlation_plans(plans: Sequence[CorrelationPairPlan], errors: list[str]) -> bool:
    """Run exact Pearson only after the complete phase has passed preflight."""
    rejected_asset_pairs: set[tuple[str, str]] = set()
    for plan in plans:
        asset_pair = (plan.first.id, plan.second.id)
        if asset_pair in rejected_asset_pairs:
            continue
        maximum = 0.0
        matching_lag: int | None = None
        for lag, first_start, last_start in plan.regions:
            maximum = max(
                maximum,
                _maximum_native_correlation_in_region(
                    plan.first_samples,
                    plan.second_samples,
                    lag,
                    first_start,
                    last_start,
                ),
            )
            if maximum >= CORRELATION_THRESHOLD:
                matching_lag = lag
                break
        if matching_lag is not None:
            rejected_asset_pairs.add(asset_pair)
            errors.append(
                f"assets {plan.first.id!r} and {plan.second.id!r} have undocumented decoded-audio correlation "
                f"{maximum:.3f} >= {CORRELATION_THRESHOLD:.2f} over {CORRELATION_WINDOW_SEC:.3f}s "
                f"at native {CORRELATION_NATIVE_SAMPLE_RATE_HZ}Hz "
                f"({plan.first_label}-{plan.second_label}, lag {matching_lag} samples)"
            )
    return bool(rejected_asset_pairs)


def _validate_perceptual_overlaps(
    pack_dir: Path,
    assets: tuple[AssetRecord, ...],
    allowlist: QualityGuardAllowlist,
    errors: list[str],
) -> None:
    """Reject only exact Pearson matches nominated by bounded independent screens."""
    review_assets = tuple(asset for asset in assets if asset.kind in CORRELATION_ASSET_KINDS)
    if len(review_assets) > CORRELATION_MAX_REVIEW_ASSETS:
        errors.append(
            "perceptual-overlap reviewed-asset budget exceeded "
            f"({len(review_assets)} > {CORRELATION_MAX_REVIEW_ASSETS})"
        )
        return
    unchecked_pairs = []
    for first, second in combinations(review_assets, 2):
        pair = tuple(sorted((first.id, second.id)))
        if pair not in allowlist.perceptual_overlaps:
            unchecked_pairs.append((first, second))

    compared_asset_ids = {asset.id for pair in unchecked_pairs for asset in pair}
    decoded_by_asset: dict[str, DecodedAudio | Sequence[int]] = {}
    total_samples = 0
    for asset in review_assets:
        if asset.id not in compared_asset_ids:
            continue
        try:
            decoded = _decode_audio(_resolve_asset_path(pack_dir, asset.path))
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(
                f"asset {asset.id!r} cannot be decoded at native 48kHz for perceptual-overlap validation: {exc}"
            )
            continue
        sample_count = _decoded_sample_count(decoded)
        if sample_count > CORRELATION_MAX_SAMPLES_PER_ASSET:
            errors.append(
                f"asset {asset.id!r} exceeds perceptual-overlap decoded-sample budget "
                f"({sample_count} > {CORRELATION_MAX_SAMPLES_PER_ASSET})"
            )
            return
        total_samples += sample_count
        if total_samples > CORRELATION_MAX_TOTAL_SAMPLES:
            errors.append(
                "perceptual-overlap total decoded-sample budget exceeded "
                f"({total_samples} > {CORRELATION_MAX_TOTAL_SAMPLES})"
            )
            return
        decoded_by_asset[asset.id] = decoded

    channels_by_asset = {asset_id: _decoded_channels(decoded) for asset_id, decoded in decoded_by_asset.items()}
    channel_pairs: list[tuple[AssetRecord, AssetRecord, str, str, Sequence[int], Sequence[int]]] = []
    for first, second in unchecked_pairs:
        first_channels = channels_by_asset.get(first.id)
        second_channels = channels_by_asset.get(second.id)
        if first_channels is None or second_channels is None:
            continue
        for first_label, first_samples in first_channels:
            for second_label, second_samples in second_channels:
                channel_pairs.append((first, second, first_label, second_label, first_samples, second_samples))

    signature_counts = {
        (asset_id, label): len(_signature_start_cells(len(samples) - _correlation_window_samples()))
        for asset_id, channels in channels_by_asset.items()
        for label, samples in channels
    }
    signature_count = sum(signature_counts.values())
    signature_coordinates = sum(
        signature_counts[(first.id, first_label)] * signature_counts[(second.id, second_label)]
        for first, second, first_label, second_label, _first_samples, _second_samples in channel_pairs
    )
    signature_work = (
        signature_count * CORRELATION_SIGNATURE_BITS * CORRELATION_PROJECTION_PAIRS_PER_BIT + 4 * signature_coordinates
    )
    if signature_coordinates > CORRELATION_MAX_SIGNATURE_COORDINATES:
        errors.append(
            "perceptual-overlap signature-coordinate budget exceeded "
            f"({signature_coordinates} > {CORRELATION_MAX_SIGNATURE_COORDINATES})"
        )
        return
    if signature_work > CORRELATION_MAX_SIGNATURE_WORK:
        errors.append(
            f"perceptual-overlap signature-work budget exceeded ({signature_work} > {CORRELATION_MAX_SIGNATURE_WORK})"
        )
        return

    signatures: dict[tuple[str, str], tuple[CorrelationSignature, ...]] = {}
    for asset_id, channels in channels_by_asset.items():
        for label, samples in channels:
            key = (asset_id, label)
            signatures[key] = _build_pair_projection_signatures(samples)

    totals = {
        "signature_matches": 0,
        "landmark_joins": 0,
        "landmark_lags": 0,
        "projected_regions": 0,
        "native_regions": 0,
        "native_lags": 0,
        "native_windows": 0,
        "native_work": 0,
    }
    signature_plans: list[CorrelationPairPlan] = []
    empty_landmarks = CorrelationLandmarkIndex(array.array("Q"))
    for first, second, first_label, second_label, first_samples, second_samples in channel_pairs:
        plan = _plan_correlation_channel_pair(
            first,
            second,
            first_label,
            second_label,
            first_samples,
            second_samples,
            signatures[(first.id, first_label)],
            signatures[(second.id, second_label)],
            empty_landmarks,
            empty_landmarks,
            totals,
            errors,
            use_signatures=True,
            use_landmarks=False,
        )
        if plan is None:
            if errors:
                return
            continue
        signature_plans.append(plan)
    if _confirm_correlation_plans(signature_plans, errors):
        return

    landmark_events = 0
    landmark_postings = 0
    landmark_indexes: dict[tuple[str, str], CorrelationLandmarkIndex] = {}
    for asset_id, channels in channels_by_asset.items():
        for label, samples in channels:
            remaining = min(
                CORRELATION_MAX_LANDMARK_EVENTS - landmark_events,
                CORRELATION_MAX_LANDMARK_POSTINGS - landmark_postings,
            )
            try:
                index, event_count = _build_landmark_postings(samples, maximum_events=remaining)
            except OverflowError:
                errors.append("perceptual-overlap landmark-event/posting budget exceeded")
                return
            landmark_events += event_count
            landmark_postings += len(index.records)
            landmark_indexes[(asset_id, label)] = index

    landmark_plans: list[CorrelationPairPlan] = []
    for first, second, first_label, second_label, first_samples, second_samples in channel_pairs:
        plan = _plan_correlation_channel_pair(
            first,
            second,
            first_label,
            second_label,
            first_samples,
            second_samples,
            (),
            (),
            landmark_indexes[(first.id, first_label)],
            landmark_indexes[(second.id, second_label)],
            totals,
            errors,
            use_signatures=False,
            use_landmarks=True,
        )
        if plan is None:
            if errors:
                return
            continue
        landmark_plans.append(plan)
    if _confirm_correlation_plans(landmark_plans, errors):
        return


def _validate_listening_receipt(
    manifest: dict[str, Any],
    assets: tuple[AssetRecord, ...],
    errors: list[str],
    *,
    require_listening_approval: bool,
) -> None:
    expected_digest = _pack_digest(assets)
    declared_digest = _sha256(manifest.get("pack_digest"), "pack_digest", errors)
    if declared_digest is not None and declared_digest != expected_digest:
        errors.append(f"pack_digest differs from delivered asset ledger ({declared_digest} != {expected_digest})")

    release_ready = manifest.get("release_ready")
    if not isinstance(release_ready, bool):
        errors.append("release_ready must be a boolean")
        release_ready = False
    receipt = manifest.get("listening_receipt")
    if not isinstance(receipt, dict):
        errors.append("listening_receipt must be an object")
        return
    status = _nonempty_text(receipt.get("status"), "listening_receipt.status", errors)
    if status is not None and status not in {"pending", "approved", "rejected"}:
        errors.append("listening_receipt.status must be 'pending', 'approved', or 'rejected'")
        status = None
    receipt_digest: str | None = None
    if receipt.get("pack_digest") is not None:
        receipt_digest = _sha256(receipt.get("pack_digest"), "listening_receipt.pack_digest", errors)
    candidate_id: str | None = None
    if receipt.get("approved_candidate_id") is not None:
        candidate_id = _identifier(
            receipt.get("approved_candidate_id"), "listening_receipt.approved_candidate_id", errors
        )
    surfaces = _text_list(receipt.get("surfaces"), "listening_receipt.surfaces", errors, required=False)
    device_classes = _text_list(
        receipt.get("device_classes"), "listening_receipt.device_classes", errors, required=False
    )
    reviewed_at: str | None = None
    if receipt.get("reviewed_at") is not None:
        reviewed_at = _nonempty_text(receipt.get("reviewed_at"), "listening_receipt.reviewed_at", errors)
        if reviewed_at is not None:
            try:
                parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
            except ValueError:
                errors.append("listening_receipt.reviewed_at must be an ISO-8601 timestamp")
                reviewed_at = None
            else:
                if parsed.tzinfo is None:
                    errors.append("listening_receipt.reviewed_at must include a timezone")
                    reviewed_at = None
    notes: str | None = None
    if receipt.get("notes") is not None:
        notes = _nonempty_text(receipt.get("notes"), "listening_receipt.notes", errors)

    approval_required = require_listening_approval or release_ready
    if require_listening_approval and not release_ready:
        errors.append("release_ready must be true when listening approval is required")
    if not approval_required and status == "pending":
        return
    if status == "rejected":
        if release_ready:
            errors.append("release_ready must be false for a rejected listening receipt")
        if require_listening_approval:
            errors.append("a rejected listening receipt cannot satisfy required listening approval")
        if receipt_digest != expected_digest:
            errors.append("listening_receipt.pack_digest must match the delivered pack_digest")
        if reviewed_at is None:
            errors.append("listening_receipt.reviewed_at is required for a rejected pack")
        if notes is None:
            errors.append("listening_receipt.notes is required for a rejected pack")
        return
    if status != "approved":
        errors.append("listening_receipt.status must be 'approved' for a release-ready pack")
    if receipt_digest != expected_digest:
        errors.append("listening_receipt.pack_digest must match the delivered pack_digest")
    if candidate_id is None:
        errors.append("listening_receipt.approved_candidate_id is required for a release-ready pack")
    surface_set = {item.casefold() for item in surfaces or ()}
    missing_surfaces = sorted(REQUIRED_LISTENING_SURFACES - surface_set)
    if missing_surfaces:
        errors.append(f"listening_receipt.surfaces is missing: {', '.join(missing_surfaces)}")
    device_set = {item.casefold() for item in device_classes or ()}
    missing_devices = sorted(REQUIRED_LISTENING_DEVICE_CLASSES - device_set)
    if missing_devices:
        errors.append(f"listening_receipt.device_classes is missing: {', '.join(missing_devices)}")
    if reviewed_at is None:
        errors.append("listening_receipt.reviewed_at is required for a release-ready pack")


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
    asset_map = {asset.id: asset for asset in assets}
    recipes, recipe_errors = parse_recipe_specs(value, asset_map.keys())
    errors.extend(recipe_errors)
    for recipe in recipes:
        for cue_index, cue in enumerate(recipe.cues):
            _validate_recipe_duration_bound(
                cue.max_duration_sec,
                f"recipes[{recipe.index}].cues[{cue_index}].max_duration_sec",
                cue.asset_id,
                asset_map,
                measured_durations,
                errors,
            )
    return len(recipes)


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

    includes_project_masters = any(
        source.provenance_type == PROJECT_AUTHORED_PROVENANCE_TYPE for source in report.sources
    )
    redistribution_line = (
        "All external recordings and project-authored masters listed here are approved for public redistribution."
        if includes_project_masters
        else "All source recordings listed here are approved for public redistribution in this add-on."
    )
    source_heading = "## Source recordings and masters" if includes_project_masters else "## Source recordings"
    lines = [
        "# Audio asset attribution",
        "",
        "This file is generated from `manifest.json` by `scripts/validate_audio_asset_pack.py`.",
        "Do not edit it by hand; run `python scripts/validate_audio_asset_pack.py --write-attribution`.",
        "",
        redistribution_line,
        "",
        source_heading,
        "",
    ]
    for source in sorted(report.sources, key=lambda item: (item.id.casefold(), item.id)):
        lines.extend(
            [
                f"### `{source.id}` — {source.title}",
                "",
                f"- Creator: {source.creator}",
                f"- License: [{LICENSE_LABELS[source.license]}]({LICENSE_URLS[source.license]})",
            ]
        )
        if source.provenance_type == PROJECT_AUTHORED_PROVENANCE_TYPE:
            assert source.generator is not None
            generator = source.generator
            lines.extend(
                [
                    f"- Generator: [`{generator.path}`]({generator.url})",
                    f"- Generator revision: `{generator.revision}`",
                    f"- Generator SHA-256: `{generator.sha256}`",
                ]
            )
            for dependency in generator.dependencies:
                lines.append(f"- Generator dependency: [`{dependency.path}`]({dependency.url}) — `{dependency.sha256}`")
            lines.extend(
                [
                    f"- Generator runtime: Python `{generator.runtime.python}`; FFmpeg `{generator.runtime.ffmpeg}`",
                    f"- Master SHA-256: `{source.source_sha256}`",
                ]
            )
        else:
            lines.extend(
                [
                    f"- Source: {source.source_url}",
                    f"- Source SHA-256: `{source.source_sha256}`",
                ]
            )
        lines.append(f"- Modification: {source.modification}")
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
    require_listening_approval: bool = False,
) -> ValidationReport:
    """Validate schema-v2 provenance, output integrity, format, and mix refs.

    This function never writes. Call :func:`write_attribution` after it returns
    successfully, or use the command-line ``--write-attribution`` switch.
    Legacy packs remain auditionable without the opt-in quality metadata. A
    release gate passes ``require_listening_approval=True`` and therefore
    requires that metadata plus a receipt bound to the delivered pack digest.
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
    quality_enabled = QUALITY_GUARD_KEY in manifest
    companion_fields = {"pack_digest", "release_ready", "listening_receipt"}
    if not quality_enabled and companion_fields & manifest.keys():
        errors.append(
            f"{QUALITY_GUARD_KEY} is required when pack_digest, release_ready, or listening_receipt is declared"
        )
    if require_listening_approval and not quality_enabled:
        errors.append(f"{QUALITY_GUARD_KEY} is required when listening approval is required")
    sources = _validate_source_records(manifest.get("sources"), errors, require_quality_metadata=quality_enabled)
    assets = _validate_asset_records(manifest.get("assets"), errors, require_quality_metadata=quality_enabled)
    _validate_source_originals(pack_dir, sources, errors)
    measured_durations = _validate_assets(pack_dir, sources, assets, errors)
    _validate_exact_duplicate_outputs(assets, errors)
    if quality_enabled:
        allowlist = _validate_quality_allowlist(manifest.get(QUALITY_GUARD_KEY), sources, assets, errors)
        _validate_layer_provenance(sources, assets, errors)
        _validate_source_concentration(assets, allowlist, errors)
        _validate_startup_synth_semantics(sources, assets, errors)
        _validate_perceptual_overlaps(pack_dir, assets, allowlist, errors)
        _validate_listening_receipt(
            manifest,
            assets,
            errors,
            require_listening_approval=require_listening_approval,
        )
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
    parser.add_argument(
        "--require-listening-approval",
        action="store_true",
        help="Require a release-ready listening receipt bound to the delivered pack digest",
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
            require_listening_approval=args.require_listening_approval,
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
