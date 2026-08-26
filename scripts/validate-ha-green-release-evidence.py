#!/usr/bin/env python3
"""Validate physical Home Assistant Green cold-launch release receipts.

The per-PR Pi smoke proves one cold launch. Stable publication additionally
requires at least twenty receipts recorded on Home Assistant Green hardware.
Receipts bind to the complete tracked release content rather than commit
ancestry. The canonical digest excludes only immutable HA Green run receipts,
so an equivalent squash commit remains valid while every other path, mode, and
blob-byte change fails closed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECEIPT_DIR = REPO_ROOT / "proof" / "media" / "ha-green-release-evidence"
DEFAULT_EXAMPLE = REPO_ROOT / "proof" / "media" / "ha-green-release-receipt.example.json"
SCHEMA_VERSION = 2
MINIMUM_RUNS = 20
P95_LIMIT_MS = 2_000.0
MAX_RECEIPT_BYTES = 64 * 1024
MAX_RECEIPTS = 1_000
_RECEIPT_ROOT_BYTES = b"proof/media/ha-green-release-evidence/"
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_VERSION_VALUE = re.compile(r"^(?:([0-9]+\.[0-9]+\.[0-9]+)|\"([0-9]+\.[0-9]+\.[0-9]+)\"|'([0-9]+\.[0-9]+\.[0-9]+)')$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_RECEIPT_NAME = re.compile(r"^run-([0-9a-f-]{36})\.json$")
_METRIC = "listener_connection_to_first_accepted_non_silent_manifest_starter_byte"
_ALLOWED_TOP_LEVEL = {
    "$schema",
    "schema_version",
    "evidence_kind",
    "release_version",
    "source_commit",
    "content_profile",
    "content_sha256",
    "run_id",
    "recorded_at",
    "hardware",
    "timing",
    "assertions",
}
_REQUIRED_ASSERTIONS = {
    "cache_empty": True,
    "outbound_network_blocked": True,
    "manifest_attributed_starter": True,
    "non_silent": True,
    "provider": "incompetech",
    "basis": "bundled_manifest",
}
_REQUIRED_ASSERTIONS_JSON = json.dumps(_REQUIRED_ASSERTIONS, sort_keys=True)


def _is_excluded_receipt_path(path: bytes) -> bool:
    if not path.startswith(_RECEIPT_ROOT_BYTES):
        return False
    leaf = path.removeprefix(_RECEIPT_ROOT_BYTES)
    return b"/" not in leaf and leaf.startswith(b"run-") and leaf.endswith(b".json")


def _exclude_release_entry(entry: tuple[bytes, bytes, bytes]) -> bool:
    path, mode, _oid = entry
    if not path.startswith(_RECEIPT_ROOT_BYTES):
        return False
    if not _is_excluded_receipt_path(path):
        raise ValueError(f"unexpected entry in HA Green receipt directory: {os.fsdecode(path)!r}")
    if mode != b"100644":
        raise ValueError(f"HA Green receipt {os.fsdecode(path)!r} must be a non-executable regular blob")
    return True


_RELEASE_CONTENT_PATH = REPO_ROOT / "scripts" / "release_content.py"
_RELEASE_CONTENT = ModuleType("_mammamiradio_release_content")
_RELEASE_CONTENT.__file__ = str(_RELEASE_CONTENT_PATH)
sys.modules[_RELEASE_CONTENT.__name__] = _RELEASE_CONTENT
exec(compile(_RELEASE_CONTENT_PATH.read_bytes(), str(_RELEASE_CONTENT_PATH), "exec"), _RELEASE_CONTENT.__dict__)
CONTENT_PROFILE = "mammamiradio-release-content-v1"
_git = _RELEASE_CONTENT.git
_tracked_entries = partial(_RELEASE_CONTENT.partitioned_entries, exclude_entry=_exclude_release_entry)
_tracked_content_sha256 = partial(_RELEASE_CONTENT.tracked_content_sha256, exclude_entry=_exclude_release_entry)
_worktree_content_sha256 = partial(_RELEASE_CONTENT.worktree_content_sha256, exclude_entry=_exclude_release_entry)


@dataclass(frozen=True, slots=True)
class ValidatedReceipt:
    path: Path
    run_id: str
    release_version: str
    source_commit: str
    content_sha256: str
    recorded_at: str
    first_byte_ms: float


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _read_json_object(path: Path, *, raw: bytes | None = None) -> dict[str, Any]:
    if raw is None:
        if path.is_symlink():
            raise ValueError("symlinks are not accepted")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"cannot read receipt: {exc}") from exc
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ValueError(f"receipt is {len(raw)} bytes; maximum is {MAX_RECEIPT_BYTES}")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"cannot read JSON object: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("top level must be a JSON object")
    return payload


def _validate_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("recorded_at must be an RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("recorded_at must be an RFC 3339 UTC timestamp") from exc
    if (offset := parsed.utcoffset()) is None or offset.total_seconds() != 0:
        raise ValueError("recorded_at must be UTC")
    return value


def _validate_receipt(path: Path, *, raw: bytes | None = None, allow_example: bool = False) -> ValidatedReceipt:
    payload = _read_json_object(path, raw=raw)
    unexpected = sorted(set(payload) - _ALLOWED_TOP_LEVEL)
    missing = sorted(_ALLOWED_TOP_LEVEL - {"$schema"} - set(payload))
    if unexpected:
        raise ValueError(f"unexpected fields: {', '.join(unexpected)}")
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
    if "$schema" in payload and not isinstance(payload["$schema"], str):
        raise ValueError("$schema must be a string when present")

    kind = payload.get("evidence_kind")
    if kind == "example" and allow_example:
        pass
    elif kind != "ha_green_cold_launch":
        raise ValueError("evidence_kind must be ha_green_cold_launch")

    release_version = payload.get("release_version")
    if not isinstance(release_version, str) or not _SEMVER.fullmatch(release_version):
        raise ValueError("release_version must be exact X.Y.Z semver")
    source_commit = payload.get("source_commit")
    if not isinstance(source_commit, str) or not _COMMIT.fullmatch(source_commit):
        raise ValueError("source_commit must be a lowercase 40-character git SHA")
    if payload.get("content_profile") != CONTENT_PROFILE:
        raise ValueError(f"content_profile must be {CONTENT_PROFILE}")
    content_sha256 = payload.get("content_sha256")
    if not isinstance(content_sha256, str) or not _DIGEST.fullmatch(content_sha256):
        raise ValueError("content_sha256 must be a lowercase 64-character SHA-256")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not _UUID.fullmatch(run_id):
        raise ValueError("run_id must be a lowercase UUIDv4")
    recorded_at = _validate_timestamp(payload.get("recorded_at"))

    hardware = payload.get("hardware")
    if not isinstance(hardware, dict) or set(hardware) != {"model", "machine", "detected_from"}:
        raise ValueError("hardware must contain exactly model, machine, and detected_from")
    model = hardware.get("model")
    if not isinstance(model, str) or "home assistant green" not in model.casefold():
        raise ValueError("hardware.model must identify Home Assistant Green")
    if hardware.get("machine") not in {"aarch64", "arm64"}:
        raise ValueError("hardware.machine must be aarch64 or arm64")
    detected_from = hardware.get("detected_from")
    if detected_from not in {"/proc/device-tree/model", "/sys/firmware/devicetree/base/model"}:
        raise ValueError("hardware.detected_from must be a supported device-tree model path")

    timing = payload.get("timing")
    if not isinstance(timing, dict) or set(timing) != {
        "metric",
        "boot_to_tcp_ms",
        "connection_to_first_byte_ms",
    }:
        raise ValueError("timing must contain exactly metric, boot_to_tcp_ms, and connection_to_first_byte_ms")
    if timing.get("metric") != _METRIC:
        raise ValueError(f"timing.metric must be {_METRIC}")
    for field, upper_bound in (("boot_to_tcp_ms", 60_000.0), ("connection_to_first_byte_ms", 10_000.0)):
        value = timing.get(field)
        if not isinstance(value, int | float) or not _is_number(value) or not 0 <= value <= upper_bound:
            raise ValueError(f"timing.{field} must be finite and between 0 and {upper_bound:g}")

    assertions = payload.get("assertions")
    if not isinstance(assertions, dict) or json.dumps(assertions, sort_keys=True) != _REQUIRED_ASSERTIONS_JSON:
        raise ValueError("assertions must exactly prove empty/offline/non-silent Incompetech starter playback")

    if kind == "ha_green_cold_launch":
        name_match = _RECEIPT_NAME.fullmatch(path.name)
        if not name_match or name_match.group(1) != run_id:
            raise ValueError("receipt filename must be run-<run_id>.json")

    return ValidatedReceipt(
        path=path,
        run_id=run_id,
        release_version=release_version,
        source_commit=source_commit,
        content_sha256=content_sha256,
        recorded_at=recorded_at,
        first_byte_ms=float(timing["connection_to_first_byte_ms"]),
    )


def _committed_receipt_inputs(repo_root: Path, commit: str) -> list[tuple[Path, bytes]]:
    _resolved, _ordinary, entries = _tracked_entries(repo_root, commit)
    if len(entries) > MAX_RECEIPTS:
        raise ValueError(f"release commit contains more than {MAX_RECEIPTS} HA Green receipts")
    inputs: list[tuple[Path, bytes]] = []
    for path, _mode, oid in entries:
        size = _git("cat-file", "-s", oid.decode("ascii"), cwd=repo_root)
        if size.returncode != 0 or not size.stdout.strip().isdigit() or int(size.stdout) > MAX_RECEIPT_BYTES:
            raise ValueError(f"HA Green receipt {os.fsdecode(path)!r} is unreadable or oversized")
        blob = _git("cat-file", "blob", oid.decode("ascii"), cwd=repo_root)
        if blob.returncode != 0:
            raise ValueError(f"cannot read HA Green receipt {os.fsdecode(path)!r}")
        inputs.append((repo_root / os.fsdecode(path), blob.stdout))
    return inputs


def _parse_release_version(raw: bytes, label: str) -> str:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise ValueError(f"cannot parse release version from {label}: {exc}") from exc
    versions: list[str] = []
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#") or line[0].isspace():
            continue
        match = re.fullmatch(r"([a-z_][a-z0-9_]*):(?:[ \t]*(.*))?", line)
        if match is None:
            raise ValueError(f"cannot read exactly one strict release version from {label}")
        if match.group(1) == "version":
            versions.append(match.group(2) or "")
    match = _VERSION_VALUE.fullmatch(versions[0]) if len(versions) == 1 else None
    if match is None:
        raise ValueError(f"cannot read exactly one strict release version from {label}")
    return next(value for value in match.groups() if value is not None)


def _release_version_from_commit(repo_root: Path, commit: str) -> str:
    path = "ha-addon/mammamiradio/config.yaml"
    result = _git("cat-file", "blob", f"{commit}:{path}", cwd=repo_root)
    if result.returncode != 0:
        raise ValueError(f"cannot read release version from {path} at {commit}")
    return _parse_release_version(result.stdout, f"{path} at {commit}")


def _nearest_rank_p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def validate_release_evidence(
    *,
    receipt_dir: Path,
    release_version: str,
    repo_root: Path = REPO_ROOT,
    current_commit: str | None = None,
    verify_git_binding: bool = True,
) -> dict[str, Any]:
    errors: list[str] = []
    receipts: list[ValidatedReceipt] = []
    receipt_inputs: list[tuple[Path, bytes | None]] = []
    resolved_current: str | None = None
    release_content_sha256: str | None = None
    committed_release_version: str | None = None
    if not _SEMVER.fullmatch(release_version):
        errors.append(f"release version {release_version!r} is not exact X.Y.Z semver")
    if verify_git_binding:
        canonical_receipt_dir = repo_root.resolve() / "proof" / "media" / "ha-green-release-evidence"
        if receipt_dir.resolve() != canonical_receipt_dir:
            errors.append(f"receipt directory must be the canonical release path: {canonical_receipt_dir}")
        try:
            resolved_current, release_content_sha256 = _tracked_content_sha256(repo_root, current_commit or "HEAD")
            committed_release_version = _release_version_from_commit(repo_root, resolved_current)
            receipt_inputs.extend(_committed_receipt_inputs(repo_root, resolved_current))
        except ValueError as exc:
            errors.append(str(exc))
    elif not receipt_dir.exists():
        errors.append(f"receipt directory is missing: {receipt_dir}")
    elif receipt_dir.is_symlink() or not receipt_dir.is_dir():
        errors.append(f"receipt path must be a real directory, not a symlink: {receipt_dir}")
    else:
        entries = sorted(receipt_dir.iterdir(), key=lambda item: item.name)
        if len(entries) > MAX_RECEIPTS:
            errors.append(f"receipt directory contains more than {MAX_RECEIPTS} entries")
            entries = entries[:MAX_RECEIPTS]
        unexpected = [item.name for item in entries if not item.is_file() or item.suffix != ".json"]
        if unexpected:
            errors.append("receipt directory contains unexpected entries: " + ", ".join(unexpected))
        receipt_inputs = [(item, None) for item in entries if item.is_file() and item.suffix == ".json"]
    for path, raw in receipt_inputs:
        try:
            receipts.append(_validate_receipt(path, raw=raw))
        except ValueError as exc:
            errors.append(f"{path.name}: {exc}")

    if len(receipts) < MINIMUM_RUNS:
        errors.append(f"found {len(receipts)} valid cold runs; at least {MINIMUM_RUNS} are required")

    duplicate_ids = {run_id for run_id, count in Counter(receipt.run_id for receipt in receipts).items() if count > 1}
    if duplicate_ids:
        errors.append("duplicate run_id values: " + ", ".join(sorted(duplicate_ids)))
    versions = sorted({receipt.release_version for receipt in receipts})
    if versions and versions != [release_version]:
        errors.append(f"receipt release versions {versions} do not exactly match {release_version}")
    source_commits = sorted({receipt.source_commit for receipt in receipts})
    content_digests = sorted({receipt.content_sha256 for receipt in receipts})
    if len(content_digests) > 1:
        errors.append("receipts contain mixed content_sha256 values: " + ", ".join(content_digests))

    p95_ms: float | None = None
    if receipts:
        p95_ms = _nearest_rank_p95([receipt.first_byte_ms for receipt in receipts])
        if p95_ms > P95_LIMIT_MS:
            errors.append(f"first-byte p95 is {p95_ms:.3f}ms; release limit is {P95_LIMIT_MS:.3f}ms")

    tested_commit = source_commits[0] if len(source_commits) == 1 else None
    receipt_content_sha256 = content_digests[0] if len(content_digests) == 1 else None
    if verify_git_binding:
        if committed_release_version is not None and committed_release_version != release_version:
            errors.append(
                f"committed release version {committed_release_version} does not exactly match {release_version}"
            )
        if (
            receipt_content_sha256 is not None
            and release_content_sha256 is not None
            and receipt_content_sha256 != release_content_sha256
        ):
            errors.append(
                f"receipt content digest {receipt_content_sha256} does not match "
                f"release content digest {release_content_sha256}"
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "proof_kind": "ha_green_release_evidence",
        "ok": not errors,
        "release_version": release_version,
        "committed_release_version": committed_release_version,
        "tested_source_commit": tested_commit,
        "tested_source_commits": source_commits,
        "release_commit": resolved_current,
        "content_profile": CONTENT_PROFILE,
        "receipt_content_sha256": receipt_content_sha256,
        "release_content_sha256": release_content_sha256,
        "receipt_count": len(receipts),
        "minimum_receipts": MINIMUM_RUNS,
        "p95_ms": p95_ms,
        "p95_limit_ms": P95_LIMIT_MS,
        "metric": _METRIC,
        "errors": errors,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            import os

            os.fsync(handle.fileno())
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _release_version_from_repo(repo_root: Path) -> str:
    config = repo_root / "ha-addon" / "mammamiradio" / "config.yaml"
    try:
        raw = config.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read release version from {config}: {exc}") from exc
    return _parse_release_version(raw, str(config))


def _print_report(report: dict[str, Any]) -> None:
    status = "PASS" if report["ok"] else "FAIL"
    p95 = "n/a" if report["p95_ms"] is None else f"{report['p95_ms']:.3f}ms"
    print(
        f"[{status}] HA Green release evidence: {report['receipt_count']}/{report['minimum_receipts']} runs, "
        f"p95={p95} (limit {report['p95_limit_ms']:.3f}ms)"
    )
    if report["tested_source_commits"]:
        print(f"  informational source commits: {', '.join(report['tested_source_commits'])}")
    if report["release_content_sha256"]:
        print(f"  release content: {report['release_content_sha256']}")
    for error in report["errors"]:
        print(f"  - {error}", file=sys.stderr)
    if not report["ok"]:
        print(
            "  Next: on the tested Home Assistant Green checkout, run\n"
            "    python -P scripts/ha-green-launch-smoke.py --record-release-receipt "
            "proof/media/ha-green-release-evidence\n"
            "  until at least 20 receipts exist, then commit only those receipt JSON files.",
            file=sys.stderr,
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt-dir", type=Path, default=DEFAULT_RECEIPT_DIR)
    parser.add_argument("--release-version", help="exact X.Y.Z release version (defaults to add-on config)")
    parser.add_argument("--current-commit", help="release commit to verify (defaults to git HEAD)")
    parser.add_argument("--output", type=Path, help="atomically write the validation report as JSON")
    parser.add_argument(
        "--validate-example",
        type=Path,
        nargs="?",
        const=DEFAULT_EXAMPLE,
        help="validate the non-evidence example shape and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.validate_example is not None:
        try:
            _validate_receipt(args.validate_example, allow_example=True)
        except ValueError as exc:
            print(f"[FAIL] example receipt: {exc}", file=sys.stderr)
            return 1
        print(f"[PASS] example receipt shape: {args.validate_example}")
        return 0
    try:
        release_version = args.release_version or _release_version_from_repo(REPO_ROOT)
    except ValueError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    report = validate_release_evidence(
        receipt_dir=args.receipt_dir,
        release_version=release_version,
        current_commit=args.current_commit,
    )
    if args.output is not None:
        _write_json_atomic(args.output, report)
    _print_report(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
