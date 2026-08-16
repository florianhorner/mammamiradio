#!/usr/bin/env python3
"""Prove the bundled and transient music boundaries across release artifacts.

``--quick`` is the offline source-tree gate behind ``make media-check``.  The
default full proof also builds a wheel and sdist in an isolated temporary
source copy, validates two already-built add-on images, and runs the focused
Jamendo transience tests.  This command never builds, pulls, pushes, or
publishes a container image.
"""

from __future__ import annotations

import argparse
import ast
import functools
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "mammamiradio" / "assets" / "starter" / "catalog.json"
STARTER_ROOT = CATALOG_PATH.parent
STARTER_PREFIX = "mammamiradio/assets/starter/"
GROUPS_QUICK = ("MEDIA-EVIDENCE", "MEDIA-BYTES", "MEDIA-AUDIO", "MEDIA-PACKAGE")
GROUPS_FULL = (*GROUPS_QUICK, "MEDIA-EXTRACTOR", "MEDIA-TRANSIENT")
STATUS_ORDER = {"PASS": 0, "FAIL": 1, "ERROR": 2}
IMAGE_ENV = {
    "amd64": "MEDIA_PROOF_AMD64_IMAGE",
    "aarch64": "MEDIA_PROOF_AARCH64_IMAGE",
}
IMAGE_PLATFORM = {"amd64": "linux/amd64", "aarch64": "linux/arm64"}
IMAGE_ARCH = {"amd64": "amd64", "aarch64": "arm64"}
TRANSIENT_TESTS = (
    "tests/playlist/test_jamendo_transient.py",
    "tests/playlist/test_legacy_media.py",
    "tests/scheduling/test_queue_mutations.py::test_drop_releases_provider_resource_exactly_once",
    "tests/web/test_streamer_routes_extended.py::test_clip_rejects_nonstarter_music[jamendo]",
)


class ProofToolError(RuntimeError):
    """A usage or missing-tool failure (exit 2)."""


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_catalog() -> dict[str, Any]:
    try:
        value = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        # The strict starter validator owns manifest validity and will turn this
        # into a MEDIA-EVIDENCE validation failure (exit 1), not a tooling error.
        return {}
    if not isinstance(value, dict):
        return {}
    return value


def _manifest_identity(catalog: Mapping[str, Any] | None = None) -> dict[str, str]:
    value = catalog or {}
    return {
        "catalog_id": str(value.get("catalog_id") or "unknown"),
        "path": CATALOG_PATH.relative_to(ROOT).as_posix(),
    }


def _new_report(*, mode: str, catalog: Mapping[str, Any] | None = None) -> dict[str, Any]:
    group_names = GROUPS_QUICK if mode == "quick" else GROUPS_FULL
    return {
        "schema_version": "1",
        "mode": mode,
        "generated_at": _utc_now(),
        "manifest": _manifest_identity(catalog),
        "groups": {name: {"status": "PASS", "checks": []} for name in group_names},
        "ok": False,
        "exit_code": 1,
    }


def _record(
    report: dict[str, Any],
    group: str,
    status: str,
    code: str,
    message: str,
    *,
    actual: object | None = None,
    expected: object | None = None,
    next_command: str,
) -> None:
    if status not in STATUS_ORDER:
        raise ValueError(f"invalid proof status: {status}")
    manifest = report["manifest"]
    check = {
        "status": status,
        "code": code,
        "message": message,
        "manifest": {"catalog_id": manifest["catalog_id"], "path": manifest["path"]},
        "actual": actual,
        "expected": expected,
        "next_command": next_command,
    }
    target = report["groups"][group]
    target["checks"].append(check)
    if STATUS_ORDER[status] > STATUS_ORDER[target["status"]]:
        target["status"] = status


def _finish(report: dict[str, Any]) -> dict[str, Any]:
    statuses = [group["status"] for group in report["groups"].values()]
    if "ERROR" in statuses:
        exit_code = 2
    elif "FAIL" in statuses:
        exit_code = 1
    else:
        exit_code = 0
    report["exit_code"] = exit_code
    report["ok"] = exit_code == 0
    return report


def _first_pending_command(catalog: Mapping[str, Any]) -> str:
    tracks = catalog.get("tracks")
    if isinstance(tracks, list):
        for row in tracks:
            if not isinstance(row, dict):
                continue
            approval = row.get("approval")
            if not isinstance(approval, dict) or approval.get("status") != "approved":
                isrc = row.get("isrc")
                if isinstance(isrc, str) and isrc:
                    return f"python scripts/starter-catalog.py acquire --isrc {isrc}"
    return "make media-check"


@functools.cache
def _load_starter_tool() -> Any:
    path = ROOT / "scripts" / "starter-catalog.py"
    spec = importlib.util.spec_from_file_location("mammamiradio_starter_catalog_tool", path)
    if spec is None or spec.loader is None:
        raise ProofToolError(f"cannot load starter validator at {path.relative_to(ROOT)}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (ImportError, OSError, SyntaxError) as exc:
        raise ProofToolError(f"cannot import starter validator: {exc}") from exc
    return module


def _minimum_measured_duration_seconds(catalog: Mapping[str, Any]) -> int:
    tool = _load_starter_tool()
    locked = int(tool.STARTER_MINIMUM_DURATION_SECONDS)
    requirements = catalog.get("release_requirements")
    configured = requirements.get("minimum_duration_seconds") if isinstance(requirements, dict) else None
    return max(locked, configured if isinstance(configured, int) else 0)


def _measure_packaged_lufs(path: Path) -> float:
    tool = _load_starter_tool()
    try:
        return float(tool._measure_lufs(path))
    except tool.ToolError as exc:
        raise RuntimeError(str(exc)) from exc


def _counts_from_message(message: str) -> tuple[int | None, int | None]:
    match = re.search(r"(?:derivatives|tracks) (\d+)/(\d+)", message)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None, None


def _starter_groups(report: dict[str, Any], catalog: Mapping[str, Any]) -> None:
    tool = _load_starter_tool()
    raw = tool.check(allow_incomplete=False)
    pending_command = _first_pending_command(catalog)
    tracks = catalog.get("tracks")
    requirements = catalog.get("release_requirements")
    if isinstance(tracks, list) and isinstance(requirements, dict):
        approved_count = sum(
            1
            for row in tracks
            if isinstance(row, dict)
            and isinstance(row.get("approval"), dict)
            and row["approval"].get("status") == "approved"
        )
        exact_count = requirements.get("exact_track_count")
        if isinstance(exact_count, int) and approved_count != exact_count:
            _record(
                report,
                "MEDIA-EVIDENCE",
                "FAIL",
                "human_audition_count",
                "complete human audition evidence is missing",
                actual=approved_count,
                expected=exact_count,
                next_command=pending_command,
            )
    for group_name in GROUPS_QUICK:
        group = raw.get("groups", {}).get(group_name)
        if not isinstance(group, dict):
            _record(
                report,
                group_name,
                "ERROR",
                "validator_contract_missing",
                "starter validator did not return this diagnostic group",
                actual=None,
                expected=group_name,
                next_command="python scripts/validate-starter-media.py --help",
            )
            continue
        raw_status = str(group.get("status") or "FAIL")
        status = "PASS" if raw_status == "PASS" else "FAIL"
        messages = group.get("messages")
        if not isinstance(messages, list) or not messages:
            messages = ["strict source-tree checks passed"]
        for index, raw_message in enumerate(messages):
            message = str(raw_message)
            actual, expected = _counts_from_message(message)
            code = "source_tree_pass" if status == "PASS" else f"source_tree_{index + 1}"
            _record(
                report,
                group_name,
                status,
                code,
                message,
                actual=actual,
                expected=expected,
                next_command=pending_command if status == "FAIL" else "make media-proof",
            )
    try:
        requirements_extractor_entries = _requirements_extractor_entries(ROOT / "requirements.txt")
    except (OSError, UnicodeError) as exc:
        _record(
            report,
            "MEDIA-PACKAGE",
            "ERROR",
            "requirements_lock_unreadable",
            f"cannot validate the default requirements lock: {exc}",
            actual="unreadable",
            expected="readable requirements.txt without yt-dlp",
            next_command="regenerate requirements.txt without the external-media extra",
        )
    else:
        _record(
            report,
            "MEDIA-PACKAGE",
            "FAIL" if requirements_extractor_entries else "PASS",
            "requirements_extractor_absent",
            (
                "requirements.txt installs yt-dlp in the default environment"
                if requirements_extractor_entries
                else "requirements.txt omits yt-dlp"
            ),
            actual=requirements_extractor_entries,
            expected=[],
            next_command="regenerate requirements.txt without the external-media extra",
        )


def run_quick() -> dict[str, Any]:
    catalog = _read_catalog()
    report = _new_report(mode="quick", catalog=catalog)
    approved = [
        row
        for row in catalog.get("tracks", [])
        if isinstance(row, dict)
        and isinstance(row.get("approval"), dict)
        and row["approval"].get("status") == "approved"
    ]
    if approved and shutil.which("ffprobe") is None:
        _record(
            report,
            "MEDIA-AUDIO",
            "ERROR",
            "ffprobe_missing",
            "ffprobe is required to validate approved starter derivatives",
            actual="not found",
            expected="ffprobe on PATH",
            next_command="install FFmpeg, then run make media-check",
        )
    _starter_groups(report, catalog)
    return _finish(report)


def _tracked_snapshot(destination: Path) -> None:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProofToolError(f"cannot enumerate the source snapshot with git: {exc}") from exc
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or ".." in relative.parts or ".context" in relative.parts:
            raise ProofToolError(f"git returned an unsafe proof input path: {relative}")
        source = ROOT / relative
        target = destination / relative
        if source.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(os.readlink(source))
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _build_archives(work_dir: Path) -> tuple[Path, Path]:
    uv = shutil.which("uv")
    if uv is None:
        raise ProofToolError("uv is required to build the wheel and sdist; install uv and retry")
    source_copy = work_dir / "source"
    artifacts = work_dir / "artifacts"
    source_copy.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    _tracked_snapshot(source_copy)
    try:
        result = subprocess.run(
            [uv, "build", "--wheel", "--sdist", "--out-dir", os.fspath(artifacts)],
            cwd=source_copy,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProofToolError(f"package builder could not run: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError("uv could not build the wheel and sdist from the isolated source snapshot")
    wheels = sorted(artifacts.glob("*.whl"))
    sdists = sorted(artifacts.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError(f"package build produced {len(wheels)} wheel(s) and {len(sdists)} sdist(s)")
    return wheels[0], sdists[0]


def _source_starter_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    if STARTER_ROOT.is_symlink():
        raise RuntimeError("source starter directory may not be a symlink")
    for path in STARTER_ROOT.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"source starter path may not be a symlink: {path.relative_to(ROOT)}")
        if path.is_file():
            relative = path.relative_to(ROOT).as_posix()
            files[relative] = path.read_bytes()
    return files


def _wheel_starter_files(path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            name = PurePosixPath(info.filename).as_posix()
            if not name.startswith(STARTER_PREFIX) or name.endswith("/"):
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise RuntimeError(f"wheel starter member is a symlink: {name}")
            files[name] = archive.read(info)
    return files


def _sdist_starter_files(path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with tarfile.open(path, mode="r:gz") as archive:
        for member in archive.getmembers():
            parts = PurePosixPath(member.name).parts
            try:
                package_index = parts.index("mammamiradio")
            except ValueError:
                continue
            normalized = PurePosixPath(*parts[package_index:]).as_posix()
            if not normalized.startswith(STARTER_PREFIX) or normalized.endswith("/"):
                continue
            if not member.isfile():
                raise RuntimeError(f"sdist starter member is not a regular file: {member.name}")
            handle = archive.extractfile(member)
            if handle is None:
                raise RuntimeError(f"sdist starter member is unreadable: {member.name}")
            files[normalized] = handle.read()
    return files


def _parity_summary(expected: Mapping[str, bytes], actual: Mapping[str, bytes]) -> tuple[bool, str]:
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(name for name in set(expected) & set(actual) if expected[name] != actual[name])
    if not missing and not extra and not changed:
        return True, f"{len(expected)} starter package file(s) match byte-for-byte"
    parts = []
    if missing:
        parts.append("missing=" + ",".join(missing))
    if extra:
        parts.append("extra=" + ",".join(extra))
    if changed:
        parts.append("changed=" + ",".join(changed))
    return False, "; ".join(parts)


def _probe_bytes(payload: bytes, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,sample_rate,channels:format=duration,bit_rate",
        "-of",
        "json",
        os.fspath(destination),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        raw = json.loads(result.stdout)
        stream = raw["streams"][0]
        media_format = raw["format"]
        return {
            "codec": stream["codec_name"],
            "sample_rate_hz": int(stream["sample_rate"]),
            "channels": int(stream["channels"]),
            "duration_seconds": round(float(media_format["duration"]), 3),
            "bitrate_kbps": round(int(media_format["bit_rate"]) / 1000),
        }
    except (OSError, subprocess.SubprocessError, KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"ffprobe rejected packaged derivative {destination.name}: {type(exc).__name__}") from exc


def _derivative_rows(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for row in catalog.get("tracks", []):
        if (
            isinstance(row, dict)
            and isinstance(row.get("approval"), dict)
            and row["approval"].get("status") == "approved"
            and isinstance(row.get("derivative"), dict)
        ):
            rows.append(row)
    return rows


def _probe_archive_derivatives(
    report: dict[str, Any],
    catalog: Mapping[str, Any],
    artifact_name: str,
    files: Mapping[str, bytes],
    work_dir: Path,
) -> None:
    rows = _derivative_rows(catalog)
    expected_count = int(catalog.get("release_requirements", {}).get("exact_track_count", 0))
    if len(rows) != expected_count:
        _record(
            report,
            "MEDIA-AUDIO",
            "FAIL",
            f"{artifact_name}_derivative_count",
            f"{artifact_name} cannot contain a complete ffprobe-verified starter catalog",
            actual=len(rows),
            expected=expected_count,
            next_command=_first_pending_command(catalog),
        )
    tool = _load_starter_tool()
    minimum_lufs = float(tool.STARTER_MINIMUM_INTEGRATED_LOUDNESS_LUFS)
    maximum_lufs = float(tool.STARTER_MAXIMUM_INTEGRATED_LOUDNESS_LUFS)
    minimum_duration_seconds = _minimum_measured_duration_seconds(catalog)
    measured_duration_seconds = 0.0
    validated_count = 0
    failures: list[str] = []
    for row in rows:
        failures_before = len(failures)
        derivative = row["derivative"]
        package_path = STARTER_PREFIX + str(derivative.get("path", ""))
        payload = files.get(package_path)
        if payload is None:
            failures.append(f"{row.get('isrc', 'unknown')}:missing")
            continue
        probe_path = work_dir / "probe" / artifact_name / Path(package_path).name
        try:
            facts = _probe_bytes(payload, probe_path)
        except RuntimeError:
            failures.append(f"{row.get('isrc', 'unknown')}:unplayable")
        else:
            measured_duration_seconds += float(facts["duration_seconds"])
            if (
                facts["codec"] != derivative.get("codec")
                or facts["sample_rate_hz"] != derivative.get("sample_rate_hz")
                or facts["channels"] != derivative.get("channels")
                or abs(facts["bitrate_kbps"] - int(derivative.get("bitrate_kbps", 0))) > 2
                or abs(facts["duration_seconds"] - float(derivative.get("duration_seconds", 0))) > 0.1
            ):
                failures.append(f"{row.get('isrc', 'unknown')}:facts")
        try:
            measured_lufs = _measure_packaged_lufs(probe_path)
        except RuntimeError:
            failures.append(f"{row.get('isrc', 'unknown')}:loudness_unavailable")
        else:
            declared_lufs = derivative.get("integrated_loudness_lufs")
            if not minimum_lufs <= measured_lufs <= maximum_lufs:
                failures.append(f"{row.get('isrc', 'unknown')}:loudness={measured_lufs:.1f}")
            if not isinstance(declared_lufs, int | float) or abs(measured_lufs - float(declared_lufs)) > 0.1:
                failures.append(f"{row.get('isrc', 'unknown')}:loudness_facts")
        if len(failures) == failures_before:
            validated_count += 1
    if measured_duration_seconds < minimum_duration_seconds:
        failures.append(f"catalog_duration={measured_duration_seconds:.3f}s<{minimum_duration_seconds}s")
    status = "FAIL" if failures else "PASS"
    _record(
        report,
        "MEDIA-AUDIO",
        status,
        f"{artifact_name}_ffprobe",
        (
            f"{artifact_name} packaged derivative failures: {', '.join(failures)}"
            if failures
            else (
                f"ffprobe and ebur128 remeasured all {len(rows)} {artifact_name} derivatives; "
                f"catalog duration is {measured_duration_seconds:.3f}s"
            )
        ),
        actual={"validated_tracks": validated_count, "measured_duration_seconds": measured_duration_seconds},
        expected={
            "tracks": len(rows),
            "minimum_duration_seconds": minimum_duration_seconds,
            "integrated_loudness_lufs": [minimum_lufs, maximum_lufs],
        },
        next_command="make media-check" if failures else "make media-proof",
    )


def _wheel_requires(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise RuntimeError("wheel does not contain exactly one METADATA file")
        message = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
    return message.get_all("Requires-Dist", [])


def _looks_like_extractor_file(name: str) -> bool:
    value = name.casefold()
    return bool(
        value == "yt_dlp"
        or value.startswith("yt_dlp.")
        or value.startswith("yt_dlp-")
        or value == "yt-dlp"
        or value.startswith("yt-dlp.")
        or value.startswith("yt-dlp-")
    )


def _archive_extractor_files(path: Path) -> list[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    else:
        with tarfile.open(path, mode="r:gz") as archive:
            names = archive.getnames()
    return sorted(name for name in names if any(_looks_like_extractor_file(part) for part in PurePosixPath(name).parts))


def _yt_dlp_metadata_is_optional(requirements: Iterable[str]) -> bool:
    matches = [value for value in requirements if re.match(r"(?i)^yt-dlp(?:\W|$)", value)]
    return len(matches) == 1 and "external-media" in matches[0] and ";" in matches[0]


def _requirements_extractor_entries(path: Path) -> list[str]:
    """Return lockfile entries that would install yt-dlp in the default environment."""

    entries: list[str] = []
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith(("#", "--hash=", "\\")):
            continue
        if re.search(r"(?i)(?<![A-Za-z0-9])yt[-_.]+dlp(?![A-Za-z0-9])", line):
            entries.append(f"{path.name}:{number}:{line}")
    return entries


def _pyproject_extractor_boundary(path: Path) -> tuple[bool, str]:
    try:
        import tomllib

        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return False, f"cannot parse pyproject: {exc}"
    project = raw.get("project", {})
    base = project.get("dependencies", []) if isinstance(project, dict) else []
    extras = project.get("optional-dependencies", {}) if isinstance(project, dict) else {}
    external = extras.get("external-media", []) if isinstance(extras, dict) else []
    base_yt = [value for value in base if re.match(r"(?i)^yt-dlp(?:\W|$)", str(value))]
    extra_yt = [value for value in external if re.match(r"(?i)^yt-dlp(?:\W|$)", str(value))]
    ok = not base_yt and len(extra_yt) == 1
    return ok, "yt-dlp is declared once under external-media and never in base dependencies"


def _extract_sdist_pyproject(path: Path, destination: Path) -> Path:
    with tarfile.open(path, mode="r:gz") as archive:
        members = [member for member in archive.getmembers() if PurePosixPath(member.name).name == "pyproject.toml"]
        root_members = [member for member in members if len(PurePosixPath(member.name).parts) == 2 and member.isfile()]
        if len(root_members) != 1:
            raise RuntimeError("sdist does not contain exactly one root pyproject.toml")
        handle = archive.extractfile(root_members[0])
        if handle is None:
            raise RuntimeError("sdist pyproject.toml is unreadable")
        destination.write_bytes(handle.read())
    return destination


def _direct_extractor_imports(root: Path = ROOT) -> list[str]:
    failures: list[str] = []
    gateway = PurePosixPath("mammamiradio/playlist/downloader.py")
    for path in sorted((root / "mammamiradio").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=os.fspath(path))
        except (OSError, UnicodeError, SyntaxError):
            failures.append(path.relative_to(root).as_posix() + ":unparseable")
            continue
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if not any(name == "yt_dlp" or name.startswith("yt_dlp.") for name in names):
                continue
            relative = PurePosixPath(path.relative_to(root).as_posix())
            ancestor = parents.get(node)
            inside_function = False
            while ancestor is not None:
                if isinstance(ancestor, ast.FunctionDef | ast.AsyncFunctionDef):
                    inside_function = True
                    break
                ancestor = parents.get(ancestor)
            if relative != gateway or not inside_function:
                failures.append(f"{relative}:{getattr(node, 'lineno', 0)}")
    return failures


def _shared_addon_boundary() -> tuple[bool, str]:
    stable = ROOT / "ha-addon" / "mammamiradio" / "config.yaml"
    edge = ROOT / "ha-addon" / "mammamiradio-edge" / "config.yaml"

    def image_value(path: Path) -> str:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("image:"):
                return line.partition(":")[2].strip().strip('"')
        return ""

    try:
        stable_image = image_value(stable)
        edge_image = image_value(edge)
        stable_config = stable.read_text(encoding="utf-8")
        edge_config = edge.read_text(encoding="utf-8")
        run_script = (ROOT / "ha-addon" / "mammamiradio" / "rootfs" / "run.sh").read_text(encoding="utf-8")
        dockerfile = (ROOT / "ha-addon" / "mammamiradio" / "Dockerfile").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return False, f"cannot read shared add-on boundary: {exc}"
    ok = bool(
        stable_image
        and stable_image == edge_image
        and "{arch}" in stable_image
        and "allow_ytdlp" not in stable_config.casefold()
        and "allow_ytdlp" not in edge_config.casefold()
        and 'export MAMMAMIRADIO_ALLOW_YTDLP="false"' in run_script
        and 'export MAMMAMIRADIO_ALLOW_YTDLP="true"' not in run_script
        and ".[external-media]" not in dockerfile
    )
    return ok, "Stable and Edge share one image family that hard-disables and omits external-media"


def _transient_static_boundary() -> tuple[bool, str]:
    path = ROOT / "mammamiradio" / "playlist" / "jamendo_transient.py"
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=os.fspath(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        return False, f"cannot parse Jamendo provider: {exc}"
    forbidden_imports = {
        "aiosqlite",
        "sqlite3",
        "mammamiradio.restart_handoff",
        "mammamiradio.audio.norm_cache",
        "mammamiradio.audio.synth_cache",
        "mammamiradio.playlist.downloader",
        "mammamiradio.scheduling.clip",
    }
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    boundary_ok = not (forbidden_imports & imported)
    protocol_ok = (
        source.count('"audioformat": "mp32"') >= 2
        and 'item.get("audio")' in source
        and "audiodownload" not in source
        and "follow_redirects=False" in source
        and 'operation_dir / "normalized.part"' in source
        and 'operation_dir / "normalized.mp3"' in source
    )
    return boundary_ok and protocol_ok, "provider uses audio/mp32 and one provider-owned partial/final pair only"


IMAGE_PROBE = r"""
import hashlib
import importlib.metadata
import importlib.util
import json
import re
import shutil
import site
import subprocess
from pathlib import Path

import mammamiradio
from mammamiradio.media.starter import (
    STARTER_MAXIMUM_INTEGRATED_LOUDNESS_LUFS,
    STARTER_MINIMUM_DURATION_SECONDS,
    STARTER_MINIMUM_INTEGRATED_LOUDNESS_LUFS,
)

root = Path(mammamiradio.__file__).resolve().parent / "assets" / "starter"
result = {
    "files": {},
    "audio_checked": 0,
    "audio_duration_seconds": 0.0,
    "audio_loudness_lufs": {},
    "audio_failures": [],
    "extractor_failures": [],
}
if not root.is_dir() or root.is_symlink():
    raise SystemExit("installed starter root is missing or unsafe")
for path in root.rglob("*"):
    if path.is_symlink():
        raise SystemExit("installed starter package contains a symlink")
    if path.is_file():
        key = "mammamiradio/assets/starter/" + path.relative_to(root).as_posix()
        result["files"][key] = hashlib.sha256(path.read_bytes()).hexdigest()
catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
for row in catalog.get("tracks", []):
    approval = row.get("approval")
    derivative = row.get("derivative")
    if not isinstance(approval, dict) or approval.get("status") != "approved" or not isinstance(derivative, dict):
        continue
    isrc = str(row.get("isrc", "unknown"))
    declared_path = Path(str(derivative.get("path", "")))
    if declared_path.is_absolute() or ".." in declared_path.parts:
        result["audio_failures"].append(f"{isrc}:unsafe_path")
        continue
    path = root / declared_path
    track_failures = []
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels:format=duration,bit_rate",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if probe.returncode != 0:
        track_failures.append("ffprobe")
    else:
        try:
            raw = json.loads(probe.stdout)
            stream = raw["streams"][0]
            media_format = raw["format"]
            duration_seconds = float(media_format["duration"])
            bitrate_kbps = round(int(media_format["bit_rate"]) / 1000)
            result["audio_duration_seconds"] += duration_seconds
            if (
                stream["codec_name"] != derivative.get("codec")
                or int(stream["sample_rate"]) != derivative.get("sample_rate_hz")
                or int(stream["channels"]) != derivative.get("channels")
                or abs(bitrate_kbps - int(derivative.get("bitrate_kbps", 0))) > 2
                or abs(duration_seconds - float(derivative.get("duration_seconds", 0))) > 0.1
            ):
                track_failures.append("facts")
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            track_failures.append("ffprobe_facts")
    loudness = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-filter_complex",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    values = re.findall(r"I:\s*(-?\d+(?:\.\d+)?) LUFS", loudness.stderr)
    if loudness.returncode != 0 or not values:
        track_failures.append("loudness_unavailable")
    else:
        measured_lufs = float(values[-1])
        result["audio_loudness_lufs"][isrc] = measured_lufs
        declared_lufs = derivative.get("integrated_loudness_lufs")
        if not STARTER_MINIMUM_INTEGRATED_LOUDNESS_LUFS <= measured_lufs <= STARTER_MAXIMUM_INTEGRATED_LOUDNESS_LUFS:
            track_failures.append(f"loudness={measured_lufs:.1f}")
        if not isinstance(declared_lufs, (int, float)) or abs(measured_lufs - float(declared_lufs)) > 0.1:
            track_failures.append("loudness_facts")
    if track_failures:
        result["audio_failures"].extend(f"{isrc}:{failure}" for failure in track_failures)
    else:
        result["audio_checked"] += 1
requirements = catalog.get("release_requirements")
configured_minimum = requirements.get("minimum_duration_seconds") if isinstance(requirements, dict) else None
if not isinstance(configured_minimum, int) or configured_minimum < STARTER_MINIMUM_DURATION_SECONDS:
    result["audio_failures"].append("catalog:duration_requirement")
minimum_duration_seconds = max(
    configured_minimum if isinstance(configured_minimum, int) else 0,
    STARTER_MINIMUM_DURATION_SECONDS,
)
result["audio_minimum_duration_seconds"] = minimum_duration_seconds
if result["audio_duration_seconds"] < minimum_duration_seconds:
    result["audio_failures"].append(
        f"catalog:duration={result['audio_duration_seconds']:.3f}s<{minimum_duration_seconds}s"
    )
if importlib.util.find_spec("yt_dlp") is not None:
    result["extractor_failures"].append("module")
try:
    importlib.metadata.version("yt-dlp")
except importlib.metadata.PackageNotFoundError:
    pass
else:
    result["extractor_failures"].append("distribution")
if shutil.which("yt-dlp") is not None:
    result["extractor_failures"].append("executable")
for site_root in site.getsitepackages():
    root_path = Path(site_root)
    if not root_path.is_dir():
        continue
    for candidate in root_path.iterdir():
        name = candidate.name.casefold()
        if name == "yt_dlp" or name.startswith(("yt_dlp.", "yt_dlp-", "yt-dlp.", "yt-dlp-")):
            result["extractor_failures"].append("bundled_file")
            break
print(json.dumps(result, sort_keys=True))
"""


def _image_audio_is_complete(result: Mapping[str, Any], catalog: Mapping[str, Any]) -> bool:
    tool = _load_starter_tool()
    expected_count = int(catalog.get("release_requirements", {}).get("exact_track_count", 0))
    audio_failures = result.get("audio_failures")
    checked = result.get("audio_checked")
    measured_duration = result.get("audio_duration_seconds")
    measured_loudness = result.get("audio_loudness_lufs")
    minimum_duration = _minimum_measured_duration_seconds(catalog)
    return bool(
        isinstance(audio_failures, list)
        and not audio_failures
        and checked == expected_count
        and len(_derivative_rows(catalog)) == expected_count
        and isinstance(measured_duration, int | float)
        and float(measured_duration) >= minimum_duration
        and isinstance(measured_loudness, dict)
        and len(measured_loudness) == expected_count
        and all(
            isinstance(value, int | float)
            and float(tool.STARTER_MINIMUM_INTEGRATED_LOUDNESS_LUFS)
            <= float(value)
            <= float(tool.STARTER_MAXIMUM_INTEGRATED_LOUDNESS_LUFS)
            for value in measured_loudness.values()
        )
    )


def _inspect_image(image: str, arch: str) -> dict[str, Any]:
    docker = shutil.which("docker")
    if docker is None:
        raise ProofToolError("docker is required to inspect the supplied add-on images")
    try:
        inspect = subprocess.run(
            [docker, "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProofToolError(f"docker could not inspect the {arch} image input: {exc}") from exc
    if inspect.returncode != 0:
        raise ProofToolError(
            f"{IMAGE_ENV[arch]} names no local image; load or build it first (media-proof never pulls images)"
        )
    actual_platform = inspect.stdout.strip()
    expected_platform = f"linux/{IMAGE_ARCH[arch]}"
    if actual_platform != expected_platform:
        raise RuntimeError(f"{arch} image reports platform {actual_platform!r}, expected {expected_platform!r}")
    try:
        run = subprocess.run(
            [
                docker,
                "run",
                "--rm",
                "--network",
                "none",
                "--platform",
                IMAGE_PLATFORM[arch],
                "-i",
                image,
                "python3",
                "-",
            ],
            input=IMAGE_PROBE,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProofToolError(f"docker could not run the {arch} installed-image probe: {exc}") from exc
    if run.returncode != 0:
        raise RuntimeError(f"{arch} installed-image probe rejected the package")
    try:
        payload = json.loads(run.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{arch} installed-image probe did not return JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{arch} installed-image probe returned a non-object")
    return payload


def _safe_output_path(raw: str | None, *, label: str) -> Path | None:
    if raw is None:
        return None
    path = Path(raw).expanduser()
    path = path if path.is_absolute() else ROOT / path
    context = (ROOT / ".context").resolve(strict=False)
    resolved = path.resolve(strict=False)
    if resolved == context or context in resolved.parents:
        raise ProofToolError(f"{label} may not be written under .context")
    if path.is_symlink():
        raise ProofToolError(f"{label} may not overwrite a symlink")
    return path


def _safe_work_base(raw: str | None) -> Path | None:
    path = _safe_output_path(raw, label="proof work directory")
    if path is None:
        return None
    resolved = path.resolve(strict=False)
    repository = ROOT.resolve(strict=False)
    ignored_work = (ROOT / "tmp" / "media-proof").resolve(strict=False)
    if (repository == resolved or repository in resolved.parents) and (
        resolved != ignored_work and ignored_work not in resolved.parents
    ):
        raise ProofToolError("proof work directory inside the repository must be under ignored tmp/media-proof")
    return path


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _run_transient_tests(report: dict[str, Any]) -> None:
    if importlib.util.find_spec("pytest") is None:
        _record(
            report,
            "MEDIA-TRANSIENT",
            "ERROR",
            "pytest_missing",
            "pytest is required for the focused Jamendo lifecycle proof",
            actual="not importable",
            expected="pytest",
            next_command="install requirements-dev.txt, then run make media-proof",
        )
        return
    missing = [path for path in TRANSIENT_TESTS if "::" not in path and not (ROOT / path).is_file()]
    if missing:
        _record(
            report,
            "MEDIA-TRANSIENT",
            "FAIL",
            "focused_tests_missing",
            "focused transience proof files are missing",
            actual=missing,
            expected=list(TRANSIENT_TESTS),
            next_command="restore the focused Jamendo transience tests, then run make media-proof",
        )
        return
    command = [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", *TRANSIENT_TESTS]
    try:
        result = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _record(
            report,
            "MEDIA-TRANSIENT",
            "ERROR",
            "focused_tests_unavailable",
            f"focused transience tests could not run: {type(exc).__name__}",
            actual="not completed",
            expected="completed test process",
            next_command="run the focused pytest command shown in docs/music-sources.md",
        )
        return
    _record(
        report,
        "MEDIA-TRANSIENT",
        "PASS" if result.returncode == 0 else "FAIL",
        "focused_runtime_tests",
        "focused lifecycle, race, cleanup, restart, and clip-containment tests passed"
        if result.returncode == 0
        else "focused Jamendo transience tests failed; output is withheld to avoid leaking provider inputs",
        actual=result.returncode,
        expected=0,
        next_command="python -m pytest -q -o addopts= " + " ".join(TRANSIENT_TESTS),
    )


def run_full(*, work_dir: Path, images: Mapping[str, str | None]) -> dict[str, Any]:
    catalog = _read_catalog()
    report = _new_report(mode="full", catalog=catalog)
    _starter_groups(report, catalog)

    if shutil.which("ffprobe") is None:
        _record(
            report,
            "MEDIA-AUDIO",
            "ERROR",
            "ffprobe_missing",
            "ffprobe is required for source, archive, and image audio validation",
            actual="not found",
            expected="ffprobe on PATH",
            next_command="install FFmpeg, then run make media-proof",
        )

    source_files: dict[str, bytes] = {}
    wheel_files: dict[str, bytes] = {}
    sdist_files: dict[str, bytes] = {}
    wheel: Path | None = None
    sdist: Path | None = None
    try:
        source_files = _source_starter_files()
        wheel, sdist = _build_archives(work_dir)
        wheel_files = _wheel_starter_files(wheel)
        sdist_files = _sdist_starter_files(sdist)
        for label, files in (("wheel", wheel_files), ("sdist", sdist_files)):
            ok, summary = _parity_summary(source_files, files)
            _record(
                report,
                "MEDIA-PACKAGE",
                "PASS" if ok else "FAIL",
                f"{label}_parity",
                f"{label}: {summary}",
                actual=len(files),
                expected=len(source_files),
                next_command="make media-check" if not ok else "make media-proof",
            )
            if shutil.which("ffprobe") is not None:
                _probe_archive_derivatives(report, catalog, label, files, work_dir)
    except ProofToolError as exc:
        _record(
            report,
            "MEDIA-PACKAGE",
            "ERROR",
            "archive_tooling_unavailable",
            str(exc),
            actual="unavailable",
            expected="isolated wheel and sdist build",
            next_command="install uv and retry make media-proof",
        )
    except (OSError, RuntimeError, tarfile.TarError, zipfile.BadZipFile) as exc:
        _record(
            report,
            "MEDIA-PACKAGE",
            "FAIL",
            "archive_validation_failed",
            str(exc),
            actual="invalid",
            expected="byte-identical source, wheel, and sdist starter package",
            next_command="make media-check",
        )

    source_boundary_ok, source_boundary_message = _pyproject_extractor_boundary(ROOT / "pyproject.toml")
    _record(
        report,
        "MEDIA-EXTRACTOR",
        "PASS" if source_boundary_ok else "FAIL",
        "source_dependency_boundary",
        source_boundary_message,
        actual=source_boundary_ok,
        expected=True,
        next_command="move yt-dlp exclusively under project.optional-dependencies.external-media",
    )
    try:
        requirements_extractor_entries = _requirements_extractor_entries(ROOT / "requirements.txt")
    except (OSError, UnicodeError) as exc:
        _record(
            report,
            "MEDIA-EXTRACTOR",
            "ERROR",
            "requirements_lock_unreadable",
            f"cannot validate the default requirements lock: {exc}",
            actual="unreadable",
            expected="readable requirements.txt without yt-dlp",
            next_command="regenerate requirements.txt without the external-media extra",
        )
    else:
        _record(
            report,
            "MEDIA-EXTRACTOR",
            "FAIL" if requirements_extractor_entries else "PASS",
            "requirements_extractor_absent",
            (
                "requirements.txt installs yt-dlp in the default environment"
                if requirements_extractor_entries
                else "requirements.txt omits yt-dlp"
            ),
            actual=requirements_extractor_entries,
            expected=[],
            next_command="regenerate requirements.txt without the external-media extra",
        )
    direct_imports = _direct_extractor_imports()
    _record(
        report,
        "MEDIA-EXTRACTOR",
        "FAIL" if direct_imports else "PASS",
        "lazy_import_boundary",
        (
            "direct extractor imports escaped the lazy gateway"
            if direct_imports
            else "yt-dlp imports exist only inside the lazy gateway"
        ),
        actual=direct_imports,
        expected=[],
        next_command="keep the yt_dlp import inside mammamiradio/playlist/downloader.py",
    )
    addon_ok, addon_message = _shared_addon_boundary()
    _record(
        report,
        "MEDIA-EXTRACTOR",
        "PASS" if addon_ok else "FAIL",
        "shared_addon_boundary",
        addon_message,
        actual=addon_ok,
        expected=True,
        next_command="restore the shared add-on image and hard-disabled external-media entrypoint",
    )
    if wheel is not None:
        try:
            requirements = _wheel_requires(wheel)
            wheel_optional = _yt_dlp_metadata_is_optional(requirements)
            wheel_extractor_files = _archive_extractor_files(wheel)
        except (OSError, RuntimeError, UnicodeError, zipfile.BadZipFile):
            wheel_optional = False
            wheel_extractor_files = ["unreadable"]
        _record(
            report,
            "MEDIA-EXTRACTOR",
            "PASS" if wheel_optional else "FAIL",
            "wheel_dependency_boundary",
            "wheel metadata keeps yt-dlp optional under external-media"
            if wheel_optional
            else "wheel metadata does not preserve the external-media-only yt-dlp boundary",
            actual=wheel_optional,
            expected=True,
            next_command="repair pyproject package metadata, then run make media-proof",
        )
        _record(
            report,
            "MEDIA-EXTRACTOR",
            "FAIL" if wheel_extractor_files else "PASS",
            "wheel_extractor_files_absent",
            "wheel bundles extractor files" if wheel_extractor_files else "wheel bundles no yt-dlp files",
            actual=wheel_extractor_files,
            expected=[],
            next_command="remove vendored yt-dlp files from the default wheel, then run make media-proof",
        )
    if sdist is not None:
        try:
            extracted = _extract_sdist_pyproject(sdist, work_dir / "sdist-pyproject.toml")
            sdist_optional, _ = _pyproject_extractor_boundary(extracted)
            sdist_extractor_files = _archive_extractor_files(sdist)
        except (OSError, RuntimeError, tarfile.TarError):
            sdist_optional = False
            sdist_extractor_files = ["unreadable"]
        _record(
            report,
            "MEDIA-EXTRACTOR",
            "PASS" if sdist_optional else "FAIL",
            "sdist_dependency_boundary",
            "sdist metadata keeps yt-dlp optional under external-media"
            if sdist_optional
            else "sdist metadata does not preserve the external-media-only yt-dlp boundary",
            actual=sdist_optional,
            expected=True,
            next_command="repair pyproject package metadata, then run make media-proof",
        )
        _record(
            report,
            "MEDIA-EXTRACTOR",
            "FAIL" if sdist_extractor_files else "PASS",
            "sdist_extractor_files_absent",
            "sdist bundles extractor files" if sdist_extractor_files else "sdist bundles no yt-dlp files",
            actual=sdist_extractor_files,
            expected=[],
            next_command="remove vendored yt-dlp files from the default sdist, then run make media-proof",
        )

    source_hashes = {name: _sha256_bytes(payload) for name, payload in source_files.items()}
    for arch in ("amd64", "aarch64"):
        image = images.get(arch)
        if not image:
            message = f"{IMAGE_ENV[arch]} is required and must name an already-built local {arch} image"
            for group in ("MEDIA-PACKAGE", "MEDIA-EXTRACTOR"):
                _record(
                    report,
                    group,
                    "ERROR",
                    f"{arch}_image_input_missing",
                    message,
                    actual="unset",
                    expected=IMAGE_ENV[arch],
                    next_command=f"set {IMAGE_ENV[arch]} to a local image reference, then run make media-proof",
                )
            continue
        try:
            result = _inspect_image(image, arch)
            image_files = result.get("files")
            image_hashes = image_files if isinstance(image_files, dict) else {}
            parity = image_hashes == source_hashes
            _record(
                report,
                "MEDIA-PACKAGE",
                "PASS" if parity else "FAIL",
                f"{arch}_image_parity",
                (
                    f"{arch} installed starter bytes match source"
                    if parity
                    else f"{arch} installed starter bytes differ from source"
                ),
                actual=len(image_hashes),
                expected=len(source_hashes),
                next_command="rebuild the local add-on image from this exact source tree, then rerun media-proof",
            )
            audio_failures = result.get("audio_failures")
            audio_ok = _image_audio_is_complete(result, catalog)
            measured_image_duration = result.get("audio_duration_seconds")
            _record(
                report,
                "MEDIA-AUDIO",
                "PASS" if audio_ok else "FAIL",
                f"{arch}_image_ffprobe",
                f"{arch} image ffprobe and ebur128 remeasured every approved derivative"
                if audio_ok
                else f"{arch} image lacks a complete measured starter catalog",
                actual={
                    "validated_tracks": result.get("audio_checked"),
                    "measured_duration_seconds": measured_image_duration,
                    "measured_loudness_lufs": result.get("audio_loudness_lufs"),
                    "failures": audio_failures,
                },
                expected={
                    "tracks": int(catalog.get("release_requirements", {}).get("exact_track_count", 0)),
                    "minimum_duration_seconds": _minimum_measured_duration_seconds(catalog),
                    "integrated_loudness_lufs": [
                        float(_load_starter_tool().STARTER_MINIMUM_INTEGRATED_LOUDNESS_LUFS),
                        float(_load_starter_tool().STARTER_MAXIMUM_INTEGRATED_LOUDNESS_LUFS),
                    ],
                },
                next_command="make media-check",
            )
            extractor_failures = result.get("extractor_failures")
            extractor_ok = isinstance(extractor_failures, list) and not extractor_failures
            _record(
                report,
                "MEDIA-EXTRACTOR",
                "PASS" if extractor_ok else "FAIL",
                f"{arch}_image_extractor_absent",
                f"{arch} image omits the yt-dlp distribution, module, and executable"
                if extractor_ok
                else f"{arch} image contains extractor authority",
                actual=extractor_failures,
                expected=[],
                next_command="remove external-media from the add-on image and rebuild it locally",
            )
        except ProofToolError as exc:
            for group in ("MEDIA-PACKAGE", "MEDIA-EXTRACTOR"):
                _record(
                    report,
                    group,
                    "ERROR",
                    f"{arch}_image_tooling_unavailable",
                    str(exc),
                    actual="not inspected",
                    expected=f"local {arch} installed image",
                    next_command=f"load the image named by {IMAGE_ENV[arch]}, then run make media-proof",
                )
        except RuntimeError as exc:
            for group in ("MEDIA-PACKAGE", "MEDIA-AUDIO", "MEDIA-EXTRACTOR"):
                _record(
                    report,
                    group,
                    "FAIL",
                    f"{arch}_image_validation_failed",
                    str(exc),
                    actual="invalid",
                    expected=f"valid local {arch} add-on image",
                    next_command="rebuild the local add-on image from this source tree, then run make media-proof",
                )

    static_ok, static_message = _transient_static_boundary()
    _record(
        report,
        "MEDIA-TRANSIENT",
        "PASS" if static_ok else "FAIL",
        "provider_static_boundary",
        static_message,
        actual=static_ok,
        expected=True,
        next_command="restore the one-file Jamendo stream-to-FFmpeg lifecycle, then run make media-proof",
    )
    _run_transient_tests(report)
    return _finish(report)


def _print_human(report: Mapping[str, Any]) -> None:
    manifest = report["manifest"]
    print(
        "MEDIA-PROOF "
        f"schema={report['schema_version']} mode={report['mode']} generated_at={report['generated_at']} "
        f"catalog_id={manifest['catalog_id']} manifest={manifest['path']}"
    )
    for name, group in report["groups"].items():
        print(f"{name} {group['status']}")
        for check in group["checks"]:
            scope = check["manifest"]
            detail = f"actual={check['actual']!r}, expected={check['expected']!r}"
            print(
                f"  {check['status']} {check['code']} "
                f"[{scope['catalog_id']} {scope['path']}]: {check['message']} ({detail})"
            )
            print(f"    Next: {check['next_command']}")
    output = report.get("output")
    if output:
        print(f"PROOF-JSON {output}")
    work_dir = report.get("work_dir")
    if work_dir:
        print(f"PROOF-WORK-DIR {work_dir}")
    print(f"MEDIA-PROOF-RESULT ok={str(report['ok']).lower()} exit_code={report['exit_code']}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="run only the fast strict source-tree gate")
    parser.add_argument("--json", action="store_true", dest="json_stdout", help="print the complete report as JSON")
    parser.add_argument("--output", help="also atomically write the JSON report here (never under .context)")
    parser.add_argument(
        "--work-dir",
        help="optional ignored/temp base for retained build artifacts (never under .context)",
    )
    parser.add_argument("--amd64-image", default=os.environ.get(IMAGE_ENV["amd64"]))
    parser.add_argument("--aarch64-image", default=os.environ.get(IMAGE_ENV["aarch64"]))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        output = _safe_output_path(args.output, label="proof output")
        work_base = _safe_work_base(args.work_dir)
        if args.quick:
            report = run_quick()
        elif work_base is not None:
            work_base.mkdir(parents=True, exist_ok=True)
            work_dir = Path(tempfile.mkdtemp(prefix="media-proof-", dir=work_base))
            report = run_full(
                work_dir=work_dir,
                images={"amd64": args.amd64_image, "aarch64": args.aarch64_image},
            )
            report["work_dir"] = os.fspath(work_dir)
        else:
            with tempfile.TemporaryDirectory(prefix="mammamiradio-media-proof-") as temporary:
                report = run_full(
                    work_dir=Path(temporary),
                    images={"amd64": args.amd64_image, "aarch64": args.aarch64_image},
                )
        if output is not None:
            report["output"] = os.fspath(output)
            _write_report(output, report)
        if args.json_stdout:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            _print_human(report)
        return int(report["exit_code"])
    except (OSError, ProofToolError) as exc:
        print(f"media-proof: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
