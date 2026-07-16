#!/usr/bin/env python3
"""Validate physical Home Assistant Green cold-launch release receipts.

The per-PR Pi smoke proves one cold launch. Stable publication additionally
requires at least twenty receipts recorded on Home Assistant Green hardware.
Receipts bind to the exact code commit that was measured. They may be added in
one or more later commits only when the complete diff from the tested commit is
limited to the receipt directory; this avoids the impossible requirement that
a committed receipt name the commit which contains itself.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECEIPT_DIR = REPO_ROOT / "proof" / "media" / "ha-green-release-evidence"
DEFAULT_EXAMPLE = REPO_ROOT / "proof" / "media" / "ha-green-release-receipt.example.json"
SCHEMA_VERSION = 1
MINIMUM_RUNS = 20
P95_LIMIT_MS = 2_000.0
MAX_RECEIPT_BYTES = 64 * 1024
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_RECEIPT_NAME = re.compile(r"^run-([0-9a-f-]{36})\.json$")
_METRIC = "listener_connection_to_first_accepted_non_silent_manifest_starter_byte"
_ALLOWED_TOP_LEVEL = {
    "$schema",
    "schema_version",
    "evidence_kind",
    "release_version",
    "source_commit",
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


@dataclass(frozen=True, slots=True)
class ValidatedReceipt:
    path: Path
    run_id: str
    release_version: str
    source_commit: str
    recorded_at: str
    first_byte_ms: float


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _read_json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("symlinks are not accepted")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"cannot stat receipt: {exc}") from exc
    if size > MAX_RECEIPT_BYTES:
        raise ValueError(f"receipt is {size} bytes; maximum is {MAX_RECEIPT_BYTES}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
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
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("recorded_at must be UTC")
    return value


def _validate_receipt(path: Path, *, allow_example: bool = False) -> ValidatedReceipt:
    payload = _read_json_object(path)
    unexpected = sorted(set(payload) - _ALLOWED_TOP_LEVEL)
    missing = sorted(_ALLOWED_TOP_LEVEL - {"$schema"} - set(payload))
    if unexpected:
        raise ValueError(f"unexpected fields: {', '.join(unexpected)}")
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}")

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
        if not _is_number(value) or not math.isfinite(float(value)) or not 0 <= float(value) <= upper_bound:
            raise ValueError(f"timing.{field} must be finite and between 0 and {upper_bound:g}")

    assertions = payload.get("assertions")
    if not isinstance(assertions, dict) or assertions != _REQUIRED_ASSERTIONS:
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
        recorded_at=recorded_at,
        first_byte_ms=float(timing["connection_to_first_byte_ms"]),
    )


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _current_commit(repo_root: Path) -> str:
    result = _git("rev-parse", "HEAD^{commit}", cwd=repo_root)
    value = result.stdout.strip().casefold()
    if result.returncode != 0 or not _COMMIT.fullmatch(value):
        raise ValueError("cannot resolve the current git commit; run from a committed release checkout")
    return value


def _validate_git_binding(*, repo_root: Path, tested_commit: str, current_commit: str, receipt_dir: Path) -> list[str]:
    errors: list[str] = []
    ancestor = _git("merge-base", "--is-ancestor", tested_commit, current_commit, cwd=repo_root)
    if ancestor.returncode != 0:
        return [f"tested source commit {tested_commit} is not an ancestor of release commit {current_commit}"]

    diff = _git("diff", "--name-only", f"{tested_commit}..{current_commit}", cwd=repo_root)
    if diff.returncode != 0:
        return [f"cannot compare tested source commit to release commit: {diff.stderr.strip() or 'git diff failed'}"]
    try:
        allowed_prefix = receipt_dir.resolve().relative_to(repo_root.resolve()).as_posix().rstrip("/") + "/"
    except ValueError:
        return ["receipt directory must be inside the release repository for commit binding"]
    changed = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
    disallowed = [path for path in changed if not path.startswith(allowed_prefix) or not path.endswith(".json")]
    if disallowed:
        errors.append(
            "release commit changed files after the measured source commit outside the receipt set: "
            + ", ".join(disallowed)
        )
    return errors


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
    if not _SEMVER.fullmatch(release_version):
        errors.append(f"release version {release_version!r} is not exact X.Y.Z semver")
    if not receipt_dir.exists():
        errors.append(f"receipt directory is missing: {receipt_dir}")
    elif receipt_dir.is_symlink() or not receipt_dir.is_dir():
        errors.append(f"receipt path must be a real directory, not a symlink: {receipt_dir}")
    else:
        entries = sorted(receipt_dir.iterdir(), key=lambda item: item.name)
        unexpected = [item.name for item in entries if not item.is_file() or item.suffix != ".json"]
        if unexpected:
            errors.append("receipt directory contains unexpected entries: " + ", ".join(unexpected))
        for path in (item for item in entries if item.is_file() and item.suffix == ".json"):
            try:
                receipts.append(_validate_receipt(path))
            except ValueError as exc:
                errors.append(f"{path.name}: {exc}")

    if len(receipts) < MINIMUM_RUNS:
        errors.append(f"found {len(receipts)} valid cold runs; at least {MINIMUM_RUNS} are required")

    run_ids = [receipt.run_id for receipt in receipts]
    duplicate_ids = sorted({run_id for run_id in run_ids if run_ids.count(run_id) > 1})
    if duplicate_ids:
        errors.append("duplicate run_id values: " + ", ".join(duplicate_ids))
    versions = sorted({receipt.release_version for receipt in receipts})
    if versions and versions != [release_version]:
        errors.append(f"receipt release versions {versions} do not exactly match {release_version}")
    source_commits = sorted({receipt.source_commit for receipt in receipts})
    if len(source_commits) > 1:
        errors.append("all receipts must bind to one tested source commit: " + ", ".join(source_commits))

    p95_ms: float | None = None
    if receipts:
        p95_ms = _nearest_rank_p95([receipt.first_byte_ms for receipt in receipts])
        if p95_ms > P95_LIMIT_MS:
            errors.append(f"first-byte p95 is {p95_ms:.3f}ms; release limit is {P95_LIMIT_MS:.3f}ms")

    resolved_current: str | None = current_commit
    tested_commit = source_commits[0] if len(source_commits) == 1 else None
    if verify_git_binding and tested_commit is not None:
        try:
            resolved_current = resolved_current or _current_commit(repo_root)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if not _COMMIT.fullmatch(resolved_current):
                errors.append("current release commit must be a lowercase 40-character git SHA")
            else:
                errors.extend(
                    _validate_git_binding(
                        repo_root=repo_root,
                        tested_commit=tested_commit,
                        current_commit=resolved_current,
                        receipt_dir=receipt_dir,
                    )
                )

    return {
        "schema_version": SCHEMA_VERSION,
        "proof_kind": "ha_green_release_evidence",
        "ok": not errors,
        "release_version": release_version,
        "tested_source_commit": tested_commit,
        "release_commit": resolved_current,
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
        for line in config.read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                value = line.split(":", 1)[1].strip().strip('"')
                if _SEMVER.fullmatch(value):
                    return value
    except OSError as exc:
        raise ValueError(f"cannot read release version from {config}: {exc}") from exc
    raise ValueError(f"cannot read exact X.Y.Z release version from {config}")


def _print_report(report: dict[str, Any]) -> None:
    status = "PASS" if report["ok"] else "FAIL"
    p95 = "n/a" if report["p95_ms"] is None else f"{report['p95_ms']:.3f}ms"
    print(
        f"[{status}] HA Green release evidence: {report['receipt_count']}/{report['minimum_receipts']} runs, "
        f"p95={p95} (limit {report['p95_limit_ms']:.3f}ms)"
    )
    if report["tested_source_commit"]:
        print(f"  tested source: {report['tested_source_commit']}")
    for error in report["errors"]:
        print(f"  - {error}", file=sys.stderr)
    if not report["ok"]:
        print(
            "  Next: on the tested Home Assistant Green checkout, run\n"
            "    python scripts/ha-green-launch-smoke.py --record-release-receipt "
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
