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


def _receipt(index, source_commit, content_sha256="0" * 64, release_version="3.4.5", first_byte_ms=1_000.0):
    run_id = str(uuid.UUID(int=index + 1, version=4))
    return {
        "$schema": "../ha-green-release-receipt.schema.json",
        "schema_version": 2,
        "evidence_kind": "ha_green_cold_launch",
        "release_version": release_version,
        "source_commit": source_commit,
        "content_profile": VALIDATOR.CONTENT_PROFILE,
        "content_sha256": content_sha256,
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
    content_sha256: str = "0" * 64,
    release_version: str = "3.4.5",
    timings: list[float] | None = None,
) -> None:
    directory.mkdir(parents=True)
    values = timings or [1_000.0] * 20
    for index, timing in enumerate(values):
        payload = _receipt(index, source_commit, content_sha256, release_version, timing)
        (directory / f"run-{payload['run_id']}.json").write_text(json.dumps(payload), encoding="utf-8")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _init_repo(path: Path, *, include_release_config: bool = True) -> None:
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "tests@example.com")
    _git(path, "config", "user.name", "Tests")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "config", "core.hooksPath", "/dev/null")
    _git(path, "config", "core.filemode", "true")
    (path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    if include_release_config:
        config = path / "ha-addon" / "mammamiradio" / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text('version: "3.4.5"\n', encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")


def _measured_repo(path: Path, release_version: str = "3.4.5"):
    _init_repo(path)
    source, digest = VALIDATOR._tracked_content_sha256(path, "HEAD")
    receipt_dir = path / "proof" / "media" / "ha-green-release-evidence"
    _write_receipts(receipt_dir, source_commit=source, content_sha256=digest, release_version=release_version)
    return source, digest, receipt_dir


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


def test_validator_rejects_wrong_hardware(tmp_path: Path) -> None:
    receipt_dir = tmp_path / "receipts"
    _write_receipts(receipt_dir, source_commit="c" * 40)
    first = next(receipt_dir.iterdir())
    payload = json.loads(first.read_text(encoding="utf-8"))
    payload["hardware"]["model"] = "generic arm runner"
    first.write_text(json.dumps(payload), encoding="utf-8")

    report = VALIDATOR.validate_release_evidence(
        receipt_dir=receipt_dir,
        release_version="3.4.5",
        verify_git_binding=False,
    )

    assert report["ok"] is False
    assert any("Home Assistant Green" in error for error in report["errors"])


def test_squash_equivalent_tree_and_mixed_informational_source_commits_pass(tmp_path: Path) -> None:
    tested_source, content_sha256, receipt_dir = _measured_repo(tmp_path)
    second = sorted(receipt_dir.iterdir())[1]
    payload = json.loads(second.read_text(encoding="utf-8"))
    payload["source_commit"] = "d" * 40
    second.write_text(json.dumps(payload), encoding="utf-8")
    _git(tmp_path, "add", "proof/media/ha-green-release-evidence")
    _git(tmp_path, "commit", "-qm", "evidence only")
    tree = _git(tmp_path, "rev-parse", "HEAD^{tree}")
    squash_commit = _git(tmp_path, "commit-tree", tree, "-m", "squash-equivalent release")
    assert _git(tmp_path, "rev-list", "--parents", "-n", "1", squash_commit) == squash_commit
    _git(tmp_path, "checkout", "-q", "--detach", squash_commit)
    report = VALIDATOR.validate_release_evidence(receipt_dir=receipt_dir, release_version="3.4.5", repo_root=tmp_path)
    assert report["ok"] is True
    assert set(report["tested_source_commits"]) == {tested_source, "d" * 40}
    assert (report["receipt_content_sha256"], report["release_content_sha256"]) == (content_sha256,) * 2


@pytest.mark.parametrize("mutation", ["byte", "path", "add", "remove", "mode"])
def test_validator_rejects_any_non_receipt_content_drift(tmp_path: Path, mutation: str) -> None:
    _tested_source, _content_sha256, receipt_dir = _measured_repo(tmp_path)
    _git(tmp_path, "add", "proof/media/ha-green-release-evidence")
    _git(tmp_path, "commit", "-qm", "evidence only")
    if mutation in {"byte", "add"}:
        name = "app.py" if mutation == "byte" else "added.py"
        (tmp_path / name).write_text("VALUE = 2\n" if mutation == "byte" else "ADDED = 1\n", encoding="utf-8")
        _git(tmp_path, "add", name)
    elif mutation in {"path", "remove"}:
        _git(tmp_path, *(("mv", "app.py", "renamed.py") if mutation == "path" else ("rm", "app.py")))
    else:
        _git(tmp_path, "update-index", "--chmod=+x", "app.py")
    _git(tmp_path, "commit", "-qm", f"{mutation} changed after measurement")
    changed = VALIDATOR.validate_release_evidence(receipt_dir=receipt_dir, release_version="3.4.5", repo_root=tmp_path)
    assert changed["ok"] is False
    assert any("does not match release content digest" in error for error in changed["errors"])


@pytest.mark.parametrize("mutation", ["missing", "mixed", "uppercase"])
def test_validator_rejects_missing_or_mixed_content_digests(tmp_path: Path, mutation: str) -> None:
    receipt_dir = tmp_path / "receipts"
    _write_receipts(receipt_dir, source_commit="a" * 40, content_sha256="1" * 64)
    first = sorted(receipt_dir.iterdir())[0]
    payload = json.loads(first.read_text(encoding="utf-8"))
    if mutation == "missing":
        del payload["content_sha256"]
    else:
        payload["content_sha256"] = ("2" if mutation == "mixed" else "A") * 64
    first.write_text(json.dumps(payload), encoding="utf-8")
    report = VALIDATOR.validate_release_evidence(
        receipt_dir=receipt_dir, release_version="3.4.5", verify_git_binding=False
    )
    assert report["ok"] is False
    assert any("content_sha256" in error for error in report["errors"])


@pytest.mark.parametrize("mutation", ["syntax", "duplicate-key", "recursion", "overflow", "bool"])
def test_validator_rejects_malformed_receipt_json(mutation: str) -> None:
    payload = _receipt(0, "a" * 40)
    path = Path(f"run-{payload['run_id']}.json")
    raw = json.dumps(payload).encode()
    if mutation == "syntax":
        raw = b"{not-json\n"
    elif mutation == "recursion":
        raw = b"[" * 1_100 + b"0" + b"]" * 1_100
    elif mutation == "duplicate-key":
        raw = raw.replace(b'"release_version": "3.4.5"', b'"release_version": "0.0.0", "release_version": "3.4.5"')
    else:
        section = payload["timing" if mutation == "overflow" else "assertions"]
        assert isinstance(section, dict)
        field = "boot_to_tcp_ms" if mutation == "overflow" else "cache_empty"
        section[field] = 10**400 if mutation == "overflow" else 1
        raw = json.dumps(payload).encode()
    expected = {"overflow": "timing.boot_to_tcp_ms", "bool": "assertions"}.get(mutation, "duplicate JSON key")
    if mutation in {"syntax", "recursion"}:
        expected = "cannot read JSON object"
    with pytest.raises(ValueError, match=expected):
        VALIDATOR._validate_receipt(path, raw=raw)


@pytest.mark.parametrize("case", ["untracked", "substituted"])
def test_validator_only_counts_receipts_from_the_target_commit(tmp_path: Path, case: str) -> None:
    _source, _digest, receipt_dir = _measured_repo(tmp_path)
    if case == "substituted":
        first = sorted(receipt_dir.iterdir())[0]
        valid = first.read_bytes()
        first.write_text("{broken\n", encoding="utf-8")
        _git(tmp_path, "add", "proof/media/ha-green-release-evidence")
        _git(tmp_path, "commit", "-qm", "malformed target receipt")
        _git(tmp_path, "update-index", "--assume-unchanged", str(first.relative_to(tmp_path)))
        first.write_bytes(valid)
        assert _git(tmp_path, "status", "--porcelain") == ""
    report = VALIDATOR.validate_release_evidence(receipt_dir=receipt_dir, release_version="3.4.5", repo_root=tmp_path)
    assert report["ok"] is False
    expected = "cannot read JSON object" if case == "substituted" else "found 0 valid cold runs"
    assert any(expected in error for error in report["errors"])


def test_requested_version_must_match_the_release_commit(tmp_path: Path) -> None:
    _source, _digest, receipt_dir = _measured_repo(tmp_path, "3.4.4")
    wrong_receipt = VALIDATOR.validate_release_evidence(
        receipt_dir=receipt_dir, release_version="3.4.5", verify_git_binding=False
    )
    assert any("do not exactly match 3.4.5" in error for error in wrong_receipt["errors"])
    _git(tmp_path, "add", "proof/media/ha-green-release-evidence")
    _git(tmp_path, "commit", "-qm", "evidence")
    report = VALIDATOR.validate_release_evidence(receipt_dir=receipt_dir, release_version="3.4.4", repo_root=tmp_path)
    assert report["ok"] is False
    assert any("committed release version 3.4.5" in error for error in report["errors"])
    with pytest.raises(ValueError, match="exactly one strict release version"):
        VALIDATOR._parse_release_version(b'version: "3.4.5"\n"\\x76ersion": 9.9.9\n', "test config")


def test_canonical_content_digest_has_a_fixed_vector_and_writer_validator_parity(tmp_path: Path) -> None:
    _init_repo(tmp_path, include_release_config=False)
    smoke = _load(SMOKE_PATH, "ha_green_launch_digest_parity_tests")
    validator_snapshot = VALIDATOR._tracked_content_sha256(tmp_path, "HEAD")
    smoke_validator = smoke._validator_module()
    assert validator_snapshot == smoke_validator._tracked_content_sha256(tmp_path, "HEAD")
    assert validator_snapshot[1] == "1465a28c7a67aab4d4ab193263e554ed427abcba010e3ba877f435e15c96a484"
    assert VALIDATOR.CONTENT_PROFILE == smoke_validator.CONTENT_PROFILE == "mammamiradio-release-content-v1"
    owner = ROOT / "scripts" / "release_content.py"
    assert Path(VALIDATOR._tracked_content_sha256.func.__code__.co_filename) == owner
    assert Path(VALIDATOR._worktree_content_sha256.func.__code__.co_filename) == owner
    accepted = b"proof/media/ha-green-release-evidence/run-12345678-1234-4234-8234-123456789abc.json"
    assert VALIDATOR._is_excluded_receipt_path(accepted)
    invalid = VALIDATOR._RECEIPT_ROOT_BYTES + b"notes.json"
    for path, mode in ((invalid, b"100644"), (accepted, b"100755")):
        with pytest.raises(ValueError):
            VALIDATOR._exclude_release_entry((path, mode, b"0" * 40))


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
        "content_sha256": "1" * 64,
        "boot_to_tcp_s": 4.25,
        "connection_to_first_byte_s": 1.25,
        "run_id": receipt_id,
        "recorded_at": datetime(2026, 7, 16, tzinfo=UTC),
    }

    path = smoke._write_release_receipt(**kwargs)

    payload = json.loads(path.read_text(encoding="utf-8"))
    validated = VALIDATOR._validate_receipt(path)
    assert payload["timing"]["boot_to_tcp_ms"] == 4_250.0
    assert payload["timing"]["connection_to_first_byte_ms"] == 1_250.0
    assert (payload["content_profile"], validated.content_sha256) == (VALIDATOR.CONTENT_PROFILE, "1" * 64)
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


@pytest.mark.parametrize("conceal", [None, "--assume-unchanged", "--skip-worktree", "filter", "receipt"])
def test_smoke_receipt_source_rejects_dirty_code(tmp_path, conceal):
    smoke = _load(SMOKE_PATH, "ha_green_launch_clean_checkout_tests")
    _init_repo(tmp_path)
    receipt_dir = tmp_path / "proof" / "media" / "ha-green-release-evidence"
    receipt_dir.mkdir(parents=True)
    (receipt_dir / "run-12345678-1234-4234-8234-123456789abc.json").write_text("{}\n", encoding="utf-8")
    assert smoke._source_snapshot(receipt_dir, repo_root=tmp_path)[0] == _git(tmp_path, "rev-parse", "HEAD")

    if conceal == "receipt":
        receipt = next(receipt_dir.iterdir())
        _git(tmp_path, "add", str(receipt.relative_to(tmp_path)))
        _git(tmp_path, "commit", "-qm", "track prior receipt")
        _git(tmp_path, "update-index", "--assume-unchanged", str(receipt.relative_to(tmp_path)))
        receipt.write_text("{changed}\n", encoding="utf-8")
    elif conceal == "filter":
        (tmp_path / ".gitattributes").write_text("app.py filter=hide\n")
        _git(tmp_path, "add", ".gitattributes")
        _git(tmp_path, "commit", "-qm", "add clean filter")
        _git(tmp_path, "config", "filter.hide.clean", "sed 's/VALUE = 2/VALUE = 1/'")
    elif conceal is not None:
        _git(tmp_path, "update-index", conceal, "app.py")
    if conceal != "receipt":
        (tmp_path / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    if conceal is None:
        (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (tmp_path / ".git/info").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".git/info/exclude").write_text("*.pyc\n", encoding="utf-8")
        (tmp_path / "shadow.pyc").write_bytes(b"ignored legacy bytecode")
    with pytest.raises(RuntimeError, match=r"release receipts require|tracked path differs"):
        smoke._source_snapshot(receipt_dir, repo_root=tmp_path)


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
            content_sha256="1" * 64,
            boot_to_tcp_s=1.0,
            connection_to_first_byte_s=1.0,
        )
