from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_PATH = ROOT / "scripts" / "validate-ha-green-release-evidence.py"
SMOKE_PATH = ROOT / "scripts" / "ha-green-launch-smoke.py"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load(VALIDATOR_PATH, "validate_ha_green_release_evidence_tests")


def _receipt(*, index: int, source_commit: str, first_byte_ms: float = 1_000.0) -> dict[str, object]:
    run_id = str(uuid.UUID(int=index + 1, version=4))
    return {
        "$schema": "../ha-green-release-receipt.schema.json",
        "schema_version": 1,
        "evidence_kind": "ha_green_cold_launch",
        "release_version": "3.4.5",
        "source_commit": source_commit,
        "run_id": run_id,
        "recorded_at": (datetime(2026, 7, 1, tzinfo=UTC) + timedelta(seconds=index)).isoformat().replace("+00:00", "Z"),
        "hardware": {
            "model": "Home Assistant Green",
            "machine": "aarch64",
            "detected_from": "/proc/device-tree/model",
        },
        "timing": {
            "metric": "listener_connection_to_first_accepted_non_silent_manifest_starter_byte",
            "boot_to_tcp_ms": 5_000.0,
            "connection_to_first_byte_ms": first_byte_ms,
        },
        "assertions": {
            "cache_empty": True,
            "outbound_network_blocked": True,
            "manifest_attributed_starter": True,
            "non_silent": True,
            "provider": "incompetech",
            "basis": "bundled_manifest",
        },
    }


def _write_receipts(
    directory: Path,
    *,
    source_commit: str,
    timings: list[float] | None = None,
) -> None:
    directory.mkdir(parents=True)
    values = timings or [1_000.0] * 20
    for index, timing in enumerate(values):
        payload = _receipt(index=index, source_commit=source_commit, first_byte_ms=timing)
        (directory / f"run-{payload['run_id']}.json").write_text(json.dumps(payload), encoding="utf-8")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "tests@example.com")
    _git(path, "config", "user.name", "Tests")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "config", "core.hooksPath", "/dev/null")
    (path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(path, "add", "app.py")
    _git(path, "commit", "-qm", "base")


def test_validator_requires_twenty_runs_and_nearest_rank_p95(tmp_path: Path) -> None:
    source = "a" * 40
    receipt_dir = tmp_path / "receipts"
    _write_receipts(receipt_dir, source_commit=source, timings=[1_000.0] * 19 + [2_500.0])

    report = VALIDATOR.validate_release_evidence(
        receipt_dir=receipt_dir,
        release_version="3.4.5",
        verify_git_binding=False,
    )

    assert report["ok"] is True
    assert report["receipt_count"] == 20
    assert report["p95_ms"] == 1_000.0


def test_validator_fails_when_p95_exceeds_two_seconds(tmp_path: Path) -> None:
    receipt_dir = tmp_path / "receipts"
    _write_receipts(receipt_dir, source_commit="b" * 40, timings=[1_000.0] * 18 + [2_500.0, 2_600.0])

    report = VALIDATOR.validate_release_evidence(
        receipt_dir=receipt_dir,
        release_version="3.4.5",
        verify_git_binding=False,
    )

    assert report["ok"] is False
    assert report["p95_ms"] == 2_500.0
    assert any("release limit" in error for error in report["errors"])


def test_validator_rejects_wrong_hardware_and_mixed_source_commits(tmp_path: Path) -> None:
    receipt_dir = tmp_path / "receipts"
    _write_receipts(receipt_dir, source_commit="c" * 40)
    first = next(receipt_dir.iterdir())
    payload = json.loads(first.read_text(encoding="utf-8"))
    payload["hardware"]["model"] = "generic arm runner"
    first.write_text(json.dumps(payload), encoding="utf-8")
    second = sorted(receipt_dir.iterdir())[1]
    payload = json.loads(second.read_text(encoding="utf-8"))
    payload["source_commit"] = "d" * 40
    second.write_text(json.dumps(payload), encoding="utf-8")

    report = VALIDATOR.validate_release_evidence(
        receipt_dir=receipt_dir,
        release_version="3.4.5",
        verify_git_binding=False,
    )

    assert report["ok"] is False
    assert any("Home Assistant Green" in error for error in report["errors"])
    assert any("one tested source commit" in error for error in report["errors"])


def test_receipt_only_commit_avoids_sha_self_reference_but_code_change_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    tested_source = _git(tmp_path, "rev-parse", "HEAD")
    receipt_dir = tmp_path / "proof" / "media" / "ha-green-release-evidence"
    _write_receipts(receipt_dir, source_commit=tested_source)
    _git(tmp_path, "add", "proof/media/ha-green-release-evidence")
    _git(tmp_path, "commit", "-qm", "evidence only")
    evidence_commit = _git(tmp_path, "rev-parse", "HEAD")

    report = VALIDATOR.validate_release_evidence(
        receipt_dir=receipt_dir,
        release_version="3.4.5",
        repo_root=tmp_path,
        current_commit=evidence_commit,
    )

    assert report["ok"] is True
    assert report["tested_source_commit"] == tested_source
    assert report["release_commit"] == evidence_commit

    (tmp_path / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-qm", "code changed after measurement")
    changed_commit = _git(tmp_path, "rev-parse", "HEAD")
    changed = VALIDATOR.validate_release_evidence(
        receipt_dir=receipt_dir,
        release_version="3.4.5",
        repo_root=tmp_path,
        current_commit=changed_commit,
    )

    assert changed["ok"] is False
    assert any("outside the receipt set: app.py" in error for error in changed["errors"])


def test_validator_example_is_explicitly_non_evidence() -> None:
    example = ROOT / "proof" / "media" / "ha-green-release-receipt.example.json"

    validated = VALIDATOR._validate_receipt(example, allow_example=True)

    assert validated.release_version == "0.0.0"
    with pytest.raises(ValueError, match="evidence_kind"):
        VALIDATOR._validate_receipt(example)


def test_validator_missing_receipts_is_actionable(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR_PATH),
            "--receipt-dir",
            str(tmp_path / "missing"),
            "--release-version",
            "3.4.5",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "at least 20" in result.stderr
    assert "--record-release-receipt" in result.stderr


def test_smoke_receipt_writer_is_atomic_and_never_overwrites(tmp_path: Path) -> None:
    smoke = _load(SMOKE_PATH, "ha_green_launch_receipt_writer_tests")
    receipt_id = uuid.UUID("12345678-1234-4234-8234-123456789abc")
    directory = tmp_path / "receipts"
    kwargs = {
        "directory": directory,
        "hardware": {
            "model": "Home Assistant Green",
            "machine": "aarch64",
            "detected_from": "/proc/device-tree/model",
        },
        "release_version": "3.4.5",
        "source_commit": "e" * 40,
        "boot_to_tcp_s": 4.25,
        "connection_to_first_byte_s": 1.25,
        "run_id": receipt_id,
        "recorded_at": datetime(2026, 7, 16, tzinfo=UTC),
    }

    path = smoke._write_release_receipt(**kwargs)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["timing"]["boot_to_tcp_ms"] == 4_250.0
    assert payload["timing"]["connection_to_first_byte_ms"] == 1_250.0
    assert list(directory.glob("*.tmp")) == []
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        smoke._write_release_receipt(**kwargs)


def test_smoke_receipt_mode_requires_detected_ha_green(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    smoke = _load(SMOKE_PATH, "ha_green_launch_hardware_detection_tests")
    model = tmp_path / "model"
    model.write_bytes(b"Home Assistant Green\x00")
    monkeypatch.setattr(smoke.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(smoke, "_DEVICE_MODEL_PATHS", (model,))

    detected = smoke._detect_ha_green()

    assert detected["model"] == "Home Assistant Green"
    assert detected["machine"] == "aarch64"
    monkeypatch.setattr(smoke.platform, "machine", lambda: "x86_64")
    with pytest.raises(RuntimeError, match="physical Home Assistant Green"):
        smoke._detect_ha_green()


def test_smoke_receipt_source_rejects_dirty_code_but_allows_prior_untracked_receipts(tmp_path: Path) -> None:
    smoke = _load(SMOKE_PATH, "ha_green_launch_clean_checkout_tests")
    _init_repo(tmp_path)
    receipt_dir = tmp_path / "proof" / "media" / "ha-green-release-evidence"
    receipt_dir.mkdir(parents=True)
    (receipt_dir / "run-12345678-1234-4234-8234-123456789abc.json").write_text("{}\n", encoding="utf-8")

    assert smoke._source_commit(receipt_dir, repo_root=tmp_path) == _git(tmp_path, "rev-parse", "HEAD")

    (tmp_path / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="clean tested checkout"):
        smoke._source_commit(receipt_dir, repo_root=tmp_path)


def test_smoke_refuses_context_receipt_path(tmp_path: Path) -> None:
    smoke = _load(SMOKE_PATH, "ha_green_launch_context_guard_tests")
    with pytest.raises(RuntimeError, match=r"must not be written under \.context"):
        smoke._write_release_receipt(
            directory=tmp_path / ".context" / "receipts",
            hardware={
                "model": "Home Assistant Green",
                "machine": "aarch64",
                "detected_from": "/proc/device-tree/model",
            },
            release_version="3.4.5",
            source_commit="f" * 40,
            boot_to_tcp_s=1.0,
            connection_to_first_byte_s=1.0,
        )
