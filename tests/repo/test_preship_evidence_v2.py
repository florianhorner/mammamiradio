from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

import scripts.landing.__main__ as landing_cli
import scripts.landing.evidence as evidence_module
import scripts.landing.gitops as gitops_module
from scripts.landing.errors import EvidenceError, GitError
from scripts.landing.evidence import (
    CONTENT_PROFILE,
    EXPECTED_REPOSITORY,
    HA_RECEIPT_ROOT,
    LEGACY_CONTENT_PROFILE,
    MAX_LEDGER_LINE_BYTES,
    MAX_NEW_RECEIPTS,
    MAX_RECEIPT_BATCH_BYTES,
    MAX_RECEIPT_BYTES,
    MAX_TREE_RECORD_BYTES,
    RECEIPT_KIND,
    RECEIPT_READ_BATCH_SIZE,
    RECEIPT_ROOT,
    SCHEMA_VERSION,
    VerificationResult,
    _iter_ledger_records,
    _ledger_files,
    _ledger_record,
    _parse_aware_timestamp,
    _read_existing_at,
    _read_ledger,
    _validate_receipt,
    _write_all,
    _write_exclusive,
    canonical_json_bytes,
    emit_v2,
    repository_identity,
    select_review_record,
    snapshot_tree,
    verify_v2,
)
from scripts.landing.gitops import GitRepository, TreeEntry

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "scripts" / "check-preship-evidence.sh"
EMIT = ROOT / "scripts" / "emit-review-evidence.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "preship-evidence.yml"
QUALITY_WORKFLOW = ROOT / ".github" / "workflows" / "quality.yml"
ORIGIN = "https://github.com/florianhorner/mammamiradio.git"


def _completed(
    *,
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(["git"], returncode, stdout, stderr)


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        stderr_after_wait: bytes = b"",
        returncode: int = 0,
        stdout_stream: Any | None = None,
    ) -> None:
        self.stdout = stdout_stream or io.BytesIO(stdout)
        self.stderr_bytes = stderr
        self.stderr_after_wait = stderr_after_wait
        self.stderr_stream: Any | None = None
        self.returncode = returncode
        self.killed = False
        self._status: int | None = None

    def kill(self) -> None:
        self.killed = True
        self._status = -9

    def wait(self) -> int:
        if self._status is None:
            if self.stderr_stream is not None and self.stderr_after_wait:
                self.stderr_stream.write(self.stderr_after_wait)
                self.stderr_stream.flush()
            self._status = self.returncode
        return self._status

    def poll(self) -> int | None:
        return self._status


def _popen_factory(process: _FakeProcess) -> Any:
    def start(*args: Any, stderr: Any, **kwargs: Any) -> _FakeProcess:
        assert stderr is not subprocess.PIPE
        process.stderr_stream = stderr
        stderr.write(process.stderr_bytes)
        stderr.flush()
        return process

    return start


def _git(
    path: Path,
    *args: str,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    result = subprocess.run(
        ["git", "--no-replace-objects", *args],
        cwd=path,
        input=input_bytes,
        env=env,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{result.stderr.decode('utf-8', errors='replace')}")
    return result


def _commit(path: Path, message: str = "test") -> str:
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "--allow-empty", "-m", message)
    return _git(path, "rev-parse", "HEAD").stdout.decode().strip()


def _init_repo(tmp_path: Path) -> GitRepository:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "tests@example.com")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "config", "core.hooksPath", "/dev/null")
    _git(path, "config", "core.filemode", "true")
    _git(path, "remote", "add", "origin", ORIGIN)
    (path / "seed.txt").write_text("seed\n")
    _commit(path, "seed")
    return GitRepository(path)


@pytest.fixture
def repo(tmp_path: Path) -> GitRepository:
    return _init_repo(tmp_path)


def _review_line(
    commit: str,
    *,
    timestamp: str = "2026-08-23T10:00:00Z",
    status: object = "clean",
    skill: str = "review",
    findings: object = None,
) -> bytes:
    if findings is None:
        findings = []
    return (
        json.dumps(
            {
                "skill": skill,
                "timestamp": timestamp,
                "status": status,
                "findings": findings,
                "commit": commit,
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def _write_ledger(
    tmp_path: Path,
    *lines: bytes,
    nested: bool = False,
) -> Path:
    gstack = tmp_path / "gstack"
    project = gstack / "projects" / "florianhorner-mammamiradio"
    if nested:
        project /= "florianhorner/feature"
    project.mkdir(parents=True, exist_ok=True)
    (project / "branch-reviews.jsonl").write_bytes(b"".join(lines))
    return gstack


def _receipt_bytes(
    repo: GitRepository,
    *,
    content_digest: str,
    reviewed_commit: str,
    timestamp: str = "2026-08-23T10:00:00Z",
    skill: str = "review",
    source_digest: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> tuple[Path, bytes]:
    payload: dict[str, Any] = {
        "content_profile": CONTENT_PROFILE,
        "kind": RECEIPT_KIND,
        "repository": EXPECTED_REPOSITORY,
        "review": {"skill": skill, "status": "clean", "timestamp": timestamp},
        "reviewed_commit": reviewed_commit,
        "reviewed_content_sha256": content_digest,
        "schema_version": SCHEMA_VERSION,
        "source_record_sha256": source_digest or hashlib.sha256(b"source\n").hexdigest(),
    }
    if overrides:
        payload.update(overrides)
    raw = canonical_json_bytes(payload)
    receipt_digest = hashlib.sha256(raw).hexdigest()
    path = Path(RECEIPT_ROOT) / content_digest / f"{receipt_digest}.json"
    return path, raw


def _store_receipt_raw(
    repo: GitRepository,
    raw: bytes,
    *,
    content_directory: str,
    filename_digest: str | None = None,
) -> Path:
    digest = filename_digest or hashlib.sha256(raw).hexdigest()
    path = Path(RECEIPT_ROOT) / content_directory / f"{digest}.json"
    destination = repo.root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)
    _commit(repo.root, "store raw receipt")
    return path


def _ha_receipt_bytes(*, run_id: str, content_digest: str) -> bytes:
    payload = json.loads((ROOT / "proof/media/ha-green-release-receipt.example.json").read_text())
    payload.update(evidence_kind="ha_green_cold_launch", release_version="3.4.5", source_commit="a" * 40)
    payload.update(content_sha256=content_digest, run_id=run_id)
    payload["hardware"]["model"] = "Home Assistant Green"
    return (json.dumps(payload, sort_keys=True) + "\n").encode()


def _add_receipt(
    repo: GitRepository,
    *,
    reviewed_commit: str | None = None,
    content_digest: str | None = None,
    timestamp: str = "2026-08-23T10:00:00Z",
    source_digest: str | None = None,
    commit: bool = True,
) -> tuple[Path, bytes]:
    reviewed_commit = reviewed_commit or repo.head()
    content_digest = content_digest or snapshot_tree(repo, "HEAD").content_sha256
    path, raw = _receipt_bytes(
        repo,
        content_digest=content_digest,
        reviewed_commit=reviewed_commit,
        timestamp=timestamp,
        source_digest=source_digest,
    )
    destination = repo.root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)
    if commit:
        _commit(repo.root, "add receipt")
    return path, raw


def _emit_and_commit(repo: GitRepository, tmp_path: Path) -> tuple[str, Path]:
    reviewed = repo.head()
    ledger = _write_ledger(tmp_path, _review_line(reviewed[:8]), nested=True)
    path, created = emit_v2(repo, ledger_root=ledger)
    assert created
    target = _commit(repo.root, "add v2 receipt")
    return target, path


def test_raw_recursive_tree_digest_survives_receipt_commit_and_squash(repo: GitRepository) -> None:
    reviewed = repo.head()
    before = snapshot_tree(repo, reviewed)
    raw_tree = repo.run(("ls-tree", "-r", "-z", "--full-tree", reviewed))
    assert before.content_sha256 == hashlib.sha256(raw_tree).hexdigest()

    receipt_path, _ = _add_receipt(repo, reviewed_commit=reviewed)
    after = snapshot_tree(repo, "HEAD", retain_all_receipts=True)
    assert after.content_sha256 == before.content_sha256
    assert os.fsencode(receipt_path.as_posix()) in after.receipts

    tree = _git(repo.root, "rev-parse", "HEAD^{tree}").stdout.strip()
    squash = _git(repo.root, "commit-tree", tree.decode(), input_bytes=b"squash\n").stdout.decode().strip()
    assert snapshot_tree(repo, squash).content_sha256 == before.content_sha256


def test_v2_and_ha_stay_valid_after_parentless_squash(repo: GitRepository, tmp_path: Path, monkeypatch) -> None:
    code = "from scripts.landing.evidence import _ha_release_validator as load;load()"
    subprocess.run([sys.executable, "-S", "-P", "-c", code], cwd=ROOT, env={"PYTHONPATH": str(ROOT)}, check=True)
    config = repo.root / "ha-addon/mammamiradio/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("version: 3.4.5\n")
    _commit(repo.root, "add release config")
    _v2_commit, v2_path = _emit_and_commit(repo, tmp_path)
    validator = evidence_module._ha_release_validator()
    _, ha_digest = validator._tracked_content_sha256(repo.root, "HEAD")
    receipt_root = repo.root / HA_RECEIPT_ROOT
    receipt_root.mkdir(parents=True)
    for run_id in (f"{index:08x}-0000-4000-8000-{index:012x}" for index in range(20)):
        (receipt_root / f"run-{run_id}.json").write_bytes(_ha_receipt_bytes(run_id=run_id, content_digest=ha_digest))
    _commit(repo.root, "add HA Green receipts")
    monkeypatch.setattr(evidence_module, "RECEIPT_READ_BATCH_SIZE", 20)
    tree = _git(repo.root, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
    squash = _git(repo.root, "commit-tree", tree, input_bytes=b"squash\n").stdout.decode().strip()
    assert verify_v2(repo, target=squash, base=None, mode="main").matching_receipts == (v2_path.as_posix(),)
    report = validator.validate_release_evidence(
        receipt_dir=receipt_root, release_version="3.4.5", repo_root=repo.root, current_commit=squash
    )
    assert report["ok"] and verify_v2(repo, target="HEAD", base=f"{_v2_commit}^", mode="pr").matching_receipts


def test_legacy_v2_profile_is_readable_but_cannot_be_emitted_for_new_content(repo: GitRepository) -> None:
    content_digest = snapshot_tree(repo, "HEAD").content_sha256
    _, raw = _receipt_bytes(
        repo,
        content_digest=content_digest,
        reviewed_commit=repo.head(),
        overrides={"content_profile": LEGACY_CONTENT_PROFILE},
    )
    path = _store_receipt_raw(repo, raw, content_directory=content_digest)
    snapshot = snapshot_tree(repo, "HEAD", retain_all_receipts=True)
    assert snapshot.receipts[os.fsencode(path.as_posix())].content_profile == LEGACY_CONTENT_PROFILE
    with pytest.raises(EvidenceError, match="must use content profile"):
        verify_v2(repo, target="HEAD", base="HEAD^", mode="pr")


@pytest.mark.parametrize("mutation", ["mode", "json", "run-id", "count", "overflow"])
def test_malformed_ha_receipts_cannot_hide_from_v2_review(repo, monkeypatch, mutation):
    run_id = "12345678-1234-4234-8234-123456789abc"
    path = repo.root / HA_RECEIPT_ROOT / f"run-{run_id}.json"
    path.parent.mkdir(parents=True)
    raw = _ha_receipt_bytes(run_id=run_id, content_digest="1" * 64)
    if mutation == "json":
        raw = b"{not-json\n"
    elif mutation == "run-id":
        payload = json.loads(raw)
        payload["run_id"] = "22345678-1234-4234-8234-123456789abc"
        raw = (json.dumps(payload) + "\n").encode()
    elif mutation == "overflow":
        payload = json.loads(raw)
        payload["timing"]["boot_to_tcp_ms"] = 10**400
        raw = (json.dumps(payload) + "\n").encode()
    path.write_bytes(raw)
    if mutation == "mode":
        path.chmod(0o755)
    _commit(repo.root, f"malformed HA receipt {mutation}")
    if mutation == "count":
        monkeypatch.setattr(evidence_module, "MAX_HA_RECEIPTS", 0)
    with pytest.raises((EvidenceError, GitError)):
        snapshot_tree(repo, "HEAD")


def test_modes_symlinks_and_gitlinks_affect_content_identity(repo: GitRepository) -> None:
    executable = repo.root / "tool.sh"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o644)
    _commit(repo.root, "plain file")
    plain_digest = snapshot_tree(repo, "HEAD").content_sha256

    executable.chmod(0o755)
    _commit(repo.root, "executable file")
    executable_digest = snapshot_tree(repo, "HEAD").content_sha256
    assert executable_digest != plain_digest

    link = repo.root / "current"
    link.symlink_to("seed.txt")
    _commit(repo.root, "symlink")
    first_link_digest = snapshot_tree(repo, "HEAD").content_sha256
    link.unlink()
    link.symlink_to("tool.sh")
    _commit(repo.root, "retarget symlink")
    second_link_digest = snapshot_tree(repo, "HEAD").content_sha256
    assert second_link_digest != first_link_digest

    tree = _git(repo.root, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
    first_gitlink = _git(repo.root, "commit-tree", tree, input_bytes=b"submodule one\n").stdout.decode().strip()
    second_gitlink = (
        _git(
            repo.root,
            "commit-tree",
            tree,
            "-p",
            first_gitlink,
            input_bytes=b"submodule two\n",
        )
        .stdout.decode()
        .strip()
    )
    _git(repo.root, "update-index", "--add", "--cacheinfo", f"160000,{first_gitlink},vendor/dependency")
    _git(repo.root, "commit", "-q", "-m", "first gitlink")
    first_gitlink_digest = snapshot_tree(repo, "HEAD").content_sha256
    _git(repo.root, "update-index", "--cacheinfo", f"160000,{second_gitlink},vendor/dependency")
    _git(repo.root, "commit", "-q", "-m", "second gitlink")
    assert snapshot_tree(repo, "HEAD").content_sha256 != first_gitlink_digest


def test_path_bytes_with_tabs_newlines_unicode_and_non_utf8_are_preserved(repo: GitRepository) -> None:
    names = (b"tab\tname", b"line\nname", "caffè".encode(), b"raw-\xff")
    blob = _git(repo.root, "rev-parse", "HEAD:seed.txt").stdout.strip()
    index_records = b"".join(b"100644 blob " + blob + b"\t" + name + b"\0" for name in names)
    _git(repo.root, "update-index", "-z", "--index-info", input_bytes=index_records)
    _git(repo.root, "commit", "-q", "-m", "odd paths")

    snapshot = snapshot_tree(repo, "HEAD")
    raw_tree = repo.run(("ls-tree", "-r", "-z", "--full-tree", snapshot.commit))
    assert snapshot.content_sha256 == hashlib.sha256(raw_tree).hexdigest()
    entries = tuple(
        repo.tree_entries(
            snapshot.commit,
            max_record_bytes=MAX_TREE_RECORD_BYTES,
        )
    )
    for name in names:
        assert any(entry.path == name for entry in entries)


def test_git_replacement_refs_do_not_change_content_identity(repo: GitRepository) -> None:
    original = repo.head()
    original_digest = snapshot_tree(repo, original).content_sha256
    (repo.root / "seed.txt").write_text("replacement\n")
    replacement = _commit(repo.root, "replacement")
    assert snapshot_tree(repo, replacement).content_sha256 != original_digest

    _git(repo.root, "replace", original, replacement)
    assert snapshot_tree(repo, original).content_sha256 == original_digest


def test_git_repository_discovery_and_command_failures(repo: GitRepository, tmp_path: Path) -> None:
    assert GitRepository.discover(repo.root).root == repo.root
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    with pytest.raises(GitError, match="not a Git repository"):
        GitRepository.discover(non_repo)
    with pytest.raises(GitError, match="failed with exit"):
        repo.run(("definitely-not-a-git-subcommand",))


def test_git_repository_rejects_empty_discovery_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GitRepository, "_invoke", Mock(return_value=_completed(stdout=b"\n")))
    with pytest.raises(GitError, match="empty repository root"):
        GitRepository.discover()


def test_git_repository_wraps_process_start_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitops_module.subprocess, "run", Mock(side_effect=OSError("git missing")))
    with pytest.raises(GitError, match="cannot run git: git missing"):
        GitRepository.discover()


@pytest.mark.parametrize(
    "object_format, expected_length",
    [(b"sha1\n", 40), (b"sha256\n", 64)],
)
def test_git_repository_accepts_supported_object_formats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    object_format: bytes,
    expected_length: int,
) -> None:
    monkeypatch.setattr(GitRepository, "run", Mock(return_value=object_format))
    assert GitRepository(tmp_path).oid_length == expected_length


def test_git_repository_rejects_unknown_object_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(GitRepository, "run", Mock(return_value=b"future\n"))
    with pytest.raises(GitError, match="unsupported Git object format"):
        GitRepository(tmp_path)


def test_git_repository_ignores_inherited_git_environment(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = _init_repo(other_root)
    (other.root / "other.txt").write_text("other\n")
    _commit(other.root, "other")
    monkeypatch.setenv("GIT_DIR", str(other.root / ".git"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "remote.origin.url")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/attacker/example.git")
    monkeypatch.setenv("GIT_TRACE", "1")

    assert repo.head() != other.head()
    assert repo.origin_url() == ORIGIN
    env = repo._environment()
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert env["LC_ALL"] == "C"
    assert "GIT_DIR" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert "GIT_TRACE" not in env


def test_streaming_git_wraps_temporary_file_errors(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gitops_module.tempfile, "TemporaryFile", Mock(side_effect=OSError("no temp space")))
    with pytest.raises(GitError, match="cannot create bounded stderr buffer"):
        repo.count_added_paths(
            "HEAD",
            "HEAD",
            pathspec=RECEIPT_ROOT,
            max_paths=1,
            max_path_bytes=16,
        )


@pytest.mark.parametrize(
    "ref, result, expected",
    [
        ("", _completed(), "empty commit reference"),
        ("missing", _completed(returncode=1), "does not resolve uniquely"),
        ("many", _completed(stdout=(b"a" * 40) + b"\n" + (b"b" * 40) + b"\n"), "exactly one"),
        ("unicode", _completed(stdout=(b"a" * 39) + b"\xff\n"), "non-ASCII"),
        ("malformed", _completed(stdout=(b"g" * 40) + b"\n"), "malformed object ID"),
    ],
)
def test_resolve_commit_rejects_bad_plumbing(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
    ref: str,
    result: subprocess.CompletedProcess[bytes],
    expected: str,
) -> None:
    monkeypatch.setattr(repo, "run_result", Mock(return_value=result))
    with pytest.raises(GitError, match=expected):
        repo.resolve_commit(ref)


@pytest.mark.parametrize("value", [None, "abc", "g" * 7, "a" * 41])
def test_resolve_abbreviated_commit_rejects_invalid_values(repo: GitRepository, value: object) -> None:
    with pytest.raises(GitError, match="ledger commit"):
        repo.resolve_abbreviated_commit(value)


@pytest.mark.parametrize("value", ["A" * 40, "a" * 39, "g" * 40])
def test_resolve_full_commit_requires_lowercase_exact_oid(repo: GitRepository, value: str) -> None:
    with pytest.raises(GitError, match="not a lowercase object ID"):
        repo.resolve_full_commit(value)


def test_resolve_full_commit_rejects_ref_resolving_to_different_object(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = "a" * repo.oid_length
    monkeypatch.setattr(repo, "resolve_commit", Mock(return_value="b" * repo.oid_length))
    with pytest.raises(GitError, match="resolves to different object"):
        repo.resolve_full_commit(claimed)


@pytest.mark.parametrize(
    "output, expected",
    [
        (b"record-without-nul", "non-NUL-terminated"),
        (b"record-without-tab\0", "without a path separator"),
        (b"100644 blob extra " + (b"a" * 40) + b"\tpath\0", "malformed entry metadata"),
        (b"100644 blob " + (b"a" * 39) + b"\xff\tpath\0", "non-ASCII object ID"),
        (b"100644 blob " + (b"g" * 40) + b"\tpath\0", "malformed object ID"),
        (b"100644 blob " + (b"a" * 40) + b"\t\0", "empty path"),
    ],
)
def test_tree_entries_rejects_malformed_git_output(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
    expected: str,
) -> None:
    process = _FakeProcess(stdout=output)
    monkeypatch.setattr(repo, "resolve_commit", Mock(return_value="a" * repo.oid_length))
    monkeypatch.setattr(gitops_module.subprocess, "Popen", Mock(side_effect=_popen_factory(process)))
    with pytest.raises(GitError, match=expected):
        tuple(
            repo.tree_entries(
                "HEAD",
                max_record_bytes=256,
            )
        )


def test_tree_entries_accepts_empty_tree(repo: GitRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess()
    monkeypatch.setattr(repo, "resolve_commit", Mock(return_value="a" * repo.oid_length))
    monkeypatch.setattr(gitops_module.subprocess, "Popen", Mock(side_effect=_popen_factory(process)))
    assert (
        tuple(
            repo.tree_entries(
                "HEAD",
                max_record_bytes=256,
            )
        )
        == ()
    )


def test_tree_entries_rejects_invalid_limits(
    repo: GitRepository,
) -> None:
    with pytest.raises(GitError, match="tree record limit"):
        tuple(
            repo.tree_entries(
                "HEAD",
                max_record_bytes=0,
            )
        )


@pytest.mark.parametrize(
    "output,returncode,max_record_bytes,expected",
    [
        (b"too-long\0", 0, 3, "record longer"),
        (b"\0", 0, 10, "empty record"),
        (b"unterminated", 0, 3, "record longer"),
        (b"unterminated", 0, 32, "non-NUL-terminated"),
        (b"", 2, 32, "exit 2: boom"),
    ],
)
def test_tree_entries_enforces_stream_bounds_and_process_result(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
    returncode: int,
    max_record_bytes: int,
    expected: str,
) -> None:
    process = _FakeProcess(stdout=output, stderr=b"boom", returncode=returncode)
    monkeypatch.setattr(repo, "resolve_commit", Mock(return_value="a" * repo.oid_length))
    monkeypatch.setattr(gitops_module.subprocess, "Popen", Mock(side_effect=_popen_factory(process)))

    with pytest.raises(GitError, match=expected):
        tuple(
            repo.tree_entries(
                "HEAD",
                max_record_bytes=max_record_bytes,
            )
        )

    assert process.poll() is not None


def test_tree_entries_preserves_records_split_across_chunks(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oid = b"a" * repo.oid_length
    stdout = Mock()
    stdout.read.side_effect = [b"100644 blob " + oid + b"\todd", b" path\0", b""]
    stdout.close = Mock()
    process = _FakeProcess(stdout_stream=stdout)
    monkeypatch.setattr(repo, "resolve_commit", Mock(return_value="a" * repo.oid_length))
    monkeypatch.setattr(gitops_module.subprocess, "Popen", Mock(side_effect=_popen_factory(process)))

    entries = tuple(
        repo.tree_entries(
            "HEAD",
            max_record_bytes=128,
        )
    )

    assert [entry.path for entry in entries] == [b"odd path"]
    assert all(call.args == (gitops_module._GIT_READ_CHUNK_BYTES,) for call in stdout.read.call_args_list)
    stdout.close.assert_called_once()


def test_tree_entries_wraps_stream_read_errors_and_reaps_process(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken_stdout = Mock()
    broken_stdout.read.side_effect = OSError("read failed")
    process = _FakeProcess(stdout_stream=broken_stdout)
    monkeypatch.setattr(repo, "resolve_commit", Mock(return_value="a" * repo.oid_length))
    monkeypatch.setattr(gitops_module.subprocess, "Popen", Mock(side_effect=_popen_factory(process)))

    with pytest.raises(GitError, match="cannot read git ls-tree"):
        tuple(
            repo.tree_entries(
                "HEAD",
                max_record_bytes=128,
            )
        )
    assert process.killed


def test_added_path_preflight_counts_and_stops_after_limit(repo: GitRepository) -> None:
    base = repo.head()
    first = repo.root / RECEIPT_ROOT / "one"
    second = repo.root / RECEIPT_ROOT / "two"
    first.parent.mkdir(parents=True)
    first.write_text("one\n")
    second.write_text("two\n")
    target = _commit(repo.root, "added reserved paths")

    assert (
        repo.count_added_paths(
            base,
            target,
            pathspec=RECEIPT_ROOT,
            max_paths=2,
            max_path_bytes=1024,
        )
        == 2
    )
    assert (
        repo.count_added_paths(
            base,
            target,
            pathspec=RECEIPT_ROOT,
            max_paths=1,
            max_path_bytes=1024,
        )
        == 2
    )


@pytest.mark.parametrize("max_paths,max_path_bytes", [(-1, 1), (0, 0)])
def test_added_path_preflight_rejects_invalid_limits(
    repo: GitRepository,
    max_paths: int,
    max_path_bytes: int,
) -> None:
    with pytest.raises(GitError, match="diff-path limits"):
        repo.count_added_paths(
            "HEAD",
            "HEAD",
            pathspec=RECEIPT_ROOT,
            max_paths=max_paths,
            max_path_bytes=max_path_bytes,
        )


def test_diff_path_preflight_rejects_unknown_filter(repo: GitRepository) -> None:
    with pytest.raises(GitError, match="unsupported diff filter"):
        repo.diff_paths(
            "HEAD",
            "HEAD",
            pathspec=RECEIPT_ROOT,
            diff_filter="M",
            max_paths=1,
            max_path_bytes=16,
        )


def test_added_path_preflight_wraps_process_start_errors(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = repo.head()
    monkeypatch.setattr(repo, "resolve_commit", Mock(return_value=head))
    monkeypatch.setattr(gitops_module.subprocess, "Popen", Mock(side_effect=OSError("cannot fork")))
    with pytest.raises(GitError, match="cannot start git diff"):
        repo.count_added_paths(
            "HEAD",
            "HEAD",
            pathspec=RECEIPT_ROOT,
            max_paths=1,
            max_path_bytes=16,
        )


@pytest.mark.parametrize(
    "stdout,stderr,returncode,max_path_bytes,expected",
    [
        (b"\0", b"", 0, 16, "empty added path"),
        (b"too-long\0", b"", 0, 3, "path longer"),
        (b"too-long", b"", 0, 3, "path longer"),
        (b"unterminated", b"", 0, 32, "non-NUL-terminated"),
        (b"", b"boom", 2, 32, "exit 2: boom"),
        (b"", b"", 2, 32, "exit 2"),
    ],
)
def test_added_path_preflight_rejects_malformed_or_failed_streams(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
    stdout: bytes,
    stderr: bytes,
    returncode: int,
    max_path_bytes: int,
    expected: str,
) -> None:
    process = _FakeProcess(stdout=stdout, stderr=stderr, returncode=returncode)
    head = repo.head()
    monkeypatch.setattr(repo, "resolve_commit", Mock(return_value=head))
    monkeypatch.setattr(gitops_module.subprocess, "Popen", Mock(side_effect=_popen_factory(process)))
    with pytest.raises(GitError, match=expected):
        repo.count_added_paths(
            "HEAD",
            "HEAD",
            pathspec=RECEIPT_ROOT,
            max_paths=4,
            max_path_bytes=max_path_bytes,
        )


def test_added_path_preflight_redirects_large_stderr_without_deadlock(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(stderr=b"boom\n" + (b"x" * (256 * 1024)), returncode=2)
    head = repo.head()
    monkeypatch.setattr(repo, "resolve_commit", Mock(return_value=head))
    popen = Mock(side_effect=_popen_factory(process))
    monkeypatch.setattr(gitops_module.subprocess, "Popen", popen)

    with pytest.raises(GitError, match="exit 2: boom"):
        repo.count_added_paths(
            "HEAD",
            "HEAD",
            pathspec=RECEIPT_ROOT,
            max_paths=4,
            max_path_bytes=32,
        )

    assert popen.call_args.kwargs["stderr"] is not subprocess.PIPE


def test_streaming_git_reads_diagnostics_after_process_exit(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(stderr_after_wait=b"late diagnostic", returncode=2)
    head = repo.head()
    monkeypatch.setattr(repo, "resolve_commit", Mock(return_value=head))
    monkeypatch.setattr(gitops_module.subprocess, "Popen", Mock(side_effect=_popen_factory(process)))

    with pytest.raises(GitError, match="exit 2: late diagnostic"):
        repo.count_added_paths(
            "HEAD",
            "HEAD",
            pathspec=RECEIPT_ROOT,
            max_paths=4,
            max_path_bytes=32,
        )


def test_added_path_preflight_wraps_stream_read_errors_and_reaps_process(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken_stdout = Mock()
    broken_stdout.read.side_effect = OSError("read failed")
    process = _FakeProcess(stdout_stream=broken_stdout)
    head = repo.head()
    monkeypatch.setattr(repo, "resolve_commit", Mock(return_value=head))
    monkeypatch.setattr(gitops_module.subprocess, "Popen", Mock(side_effect=_popen_factory(process)))

    with pytest.raises(GitError, match="cannot read git diff"):
        repo.count_added_paths(
            "HEAD",
            "HEAD",
            pathspec=RECEIPT_ROOT,
            max_paths=4,
            max_path_bytes=32,
        )
    assert process.killed


def test_snapshot_rejects_duplicate_tree_path(repo: GitRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = TreeEntry(
        raw=b"100644 blob " + repo.head().encode() + b"\tduplicate",
        mode=b"100644",
        kind=b"blob",
        oid=repo.head(),
        path=b"duplicate",
    )
    monkeypatch.setattr(repo, "tree_entries", Mock(return_value=(entry, entry)))
    with pytest.raises(GitError, match="duplicate path"):
        snapshot_tree(repo, "HEAD", repository=EXPECTED_REPOSITORY)


@pytest.mark.parametrize(
    "constant,expected",
    [("MAX_TREE_ENTRIES", "ordinary entries"), ("MAX_TREE_BYTES", "ordinary bytes")],
)
def test_snapshot_bounds_ordinary_tree_without_retaining_entries(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    expected: str,
) -> None:
    assert snapshot_tree(repo, "HEAD").receipts == {}
    monkeypatch.setattr(evidence_module, constant, 0)
    with pytest.raises(GitError, match=expected):
        snapshot_tree(repo, "HEAD")


def test_snapshot_validates_history_but_retains_only_requested_receipts(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_path, _ = _add_receipt(repo, timestamp="2026-08-23T09:00:00Z")
    second_path, _ = _add_receipt(
        repo,
        reviewed_commit=repo.head(),
        timestamp="2026-08-23T10:00:00Z",
        source_digest=hashlib.sha256(b"second source").hexdigest(),
    )
    first = os.fsencode(first_path.as_posix())
    second = os.fsencode(second_path.as_posix())
    monkeypatch.setattr(evidence_module, "MAX_TREE_ENTRIES", 1)

    assert snapshot_tree(repo, "HEAD").receipts == {}
    assert set(snapshot_tree(repo, "HEAD", receipt_paths={first}).receipts) == {first}
    assert set(snapshot_tree(repo, "HEAD", retain_all_receipts=True).receipts) == {first, second}
    assert set(snapshot_tree(repo, "HEAD", retain_matching_receipts=True).receipts) == {first, second}


def test_snapshot_rejects_conflicting_receipt_retention_modes(repo: GitRepository) -> None:
    with pytest.raises(EvidenceError, match="mutually exclusive"):
        snapshot_tree(repo, "HEAD", retain_all_receipts=True, retain_matching_receipts=True)


@pytest.mark.parametrize(
    "check_output, expected",
    [
        (b"", "wrong number"),
        ((b"a" * 40) + b" missing\n", "missing object"),
        (b"malformed\n", "malformed metadata"),
        ((b"a" * 39) + b"\xff blob 1\n", "malformed object metadata"),
        ((b"a" * 40) + b" blob nope\n", "malformed object metadata"),
        ((b"b" * 40) + b" blob 1\n", "not the requested blob"),
        ((b"a" * 40) + b" tree 1\n", "not the requested blob"),
        ((b"a" * 40) + b" blob -1\n", "maximum"),
        ((b"a" * 40) + b" blob 2\n", "maximum"),
    ],
)
def test_read_blobs_rejects_bad_batch_check_metadata(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
    check_output: bytes,
    expected: str,
) -> None:
    monkeypatch.setattr(repo, "run", Mock(return_value=check_output))
    with pytest.raises(GitError, match=expected):
        repo.read_blobs(("a" * 40,), max_bytes=1, max_total_bytes=1)


@pytest.mark.parametrize(
    "batch_output, expected",
    [
        (b"truncated", "truncated header"),
        ((b"a" * 40) + b" missing\n", "missing object"),
        (b"malformed\n", "malformed batch metadata"),
        ((b"a" * 39) + b"\xff blob 1\nx\n", "malformed object metadata"),
        ((b"a" * 40) + b" blob nope\nx\n", "malformed object metadata"),
        ((b"b" * 40) + b" blob 1\nx\n", "not the requested blob"),
        ((b"a" * 40) + b" tree 1\nx\n", "not the requested blob"),
        ((b"a" * 40) + b" blob 2\nxx\n", "maximum"),
        ((b"a" * 40) + b" blob 1\n", "truncated bytes"),
        ((b"a" * 40) + b" blob 1\nx\ntrailing", "trailing output"),
    ],
)
def test_read_blobs_rejects_bad_batch_body(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
    batch_output: bytes,
    expected: str,
) -> None:
    oid = "a" * 40
    check_output = oid.encode() + b" blob 1\n"
    monkeypatch.setattr(repo, "run", Mock(side_effect=[check_output, batch_output]))
    with pytest.raises(GitError, match=expected):
        repo.read_blobs((oid,), max_bytes=1, max_total_bytes=1)


def test_read_blobs_handles_empty_and_duplicate_object_lists(repo: GitRepository) -> None:
    assert repo.read_blobs((), max_bytes=1, max_total_bytes=1) == {}
    blob = _git(repo.root, "rev-parse", "HEAD:seed.txt").stdout.decode().strip()
    assert repo.read_blobs((blob, blob), max_bytes=10, max_total_bytes=10) == {blob: b"seed\n"}


def test_read_blobs_rejects_malformed_requested_oid(repo: GitRepository) -> None:
    with pytest.raises(GitError, match="malformed blob object ID"):
        repo.read_blobs(("not-an-oid",), max_bytes=1, max_total_bytes=1)


def test_git_ancestor_and_origin_errors_are_typed(repo: GitRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    head = repo.head()
    monkeypatch.setattr(repo, "resolve_commit", Mock(return_value=head))
    monkeypatch.setattr(repo, "run_result", Mock(return_value=_completed(returncode=2, stderr=b"boom\n")))
    with pytest.raises(GitError, match="exit 2: boom"):
        repo.is_ancestor(head, head)

    monkeypatch.setattr(repo, "run", Mock(return_value=b"\n"))
    with pytest.raises(GitError, match="origin remote has no URL"):
        repo.origin_url()
    monkeypatch.setattr(repo, "run", Mock(return_value=b"https://github.com/\xff\n"))
    with pytest.raises(GitError, match="not valid UTF-8"):
        repo.origin_url()


@pytest.mark.parametrize(
    "reserved_path",
    [
        "proof/preship-reviews/v2",
        "proof/preship-reviews/v2/readme.txt",
        f"proof/preship-reviews/v2/{'A' * 64}/{'0' * 64}.json",
        f"proof/preship-reviews/v2/{'0' * 64}/{'1' * 64}/extra.json",
    ],
)
def test_unknown_reserved_namespace_entries_fail(repo: GitRepository, reserved_path: str) -> None:
    path = repo.root / reserved_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("reserved\n")
    _commit(repo.root, "reserved entry")
    with pytest.raises(EvidenceError, match="unknown entry in reserved v2 namespace"):
        snapshot_tree(repo, "HEAD")


@pytest.mark.parametrize("entry_kind", ["executable", "symlink"])
def test_receipt_shaped_non_regular_entries_fail(repo: GitRepository, entry_kind: str) -> None:
    reviewed = repo.head()
    path, raw = _receipt_bytes(
        repo,
        content_digest=snapshot_tree(repo, reviewed).content_sha256,
        reviewed_commit=reviewed,
    )
    destination = repo.root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    if entry_kind == "executable":
        destination.write_bytes(raw)
        destination.chmod(0o755)
    else:
        destination.symlink_to("../../../../../../seed.txt")
    _commit(repo.root, f"{entry_kind} receipt")
    with pytest.raises(EvidenceError, match="non-executable regular blob"):
        snapshot_tree(repo, "HEAD")


@pytest.mark.parametrize("mutation", ["pretty", "unknown-key", "wrong-repository", "duplicate-key"])
def test_malformed_or_noncanonical_receipts_fail(repo: GitRepository, mutation: str) -> None:
    reviewed = repo.head()
    content_digest = snapshot_tree(repo, reviewed).content_sha256
    _, canonical = _receipt_bytes(
        repo,
        content_digest=content_digest,
        reviewed_commit=reviewed,
    )
    payload = json.loads(canonical)
    if mutation == "pretty":
        raw = (json.dumps(payload, indent=2) + "\n").encode()
    elif mutation == "unknown-key":
        payload["surprise"] = True
        raw = canonical_json_bytes(payload)
    elif mutation == "wrong-repository":
        payload["repository"] = "someone/else"
        raw = canonical_json_bytes(payload)
    else:
        raw = canonical[:-2] + b',"schema_version":"2.0.0"}\n'

    receipt_digest = hashlib.sha256(raw).hexdigest()
    path = Path(RECEIPT_ROOT) / content_digest / f"{receipt_digest}.json"
    destination = repo.root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)
    _commit(repo.root, f"{mutation} receipt")
    with pytest.raises(EvidenceError):
        snapshot_tree(repo, "HEAD")


@pytest.mark.parametrize(
    "case, expected",
    [
        ("invalid-utf8", "UTF-8"),
        ("non-object", "one JSON object"),
        ("deep-json", "single-document JSON"),
        ("huge-integer-json", "single-document JSON"),
        ("review-not-object", "invalid review object"),
        ("review-keys", "invalid review object"),
        ("schema", "schema_version"),
        ("kind", "kind"),
        ("profile", "content_profile"),
        ("skill", "unsupported review skill"),
        ("status", "status='clean'"),
        ("timestamp", "has no timezone"),
        ("timestamp-underflow", "supported range"),
        ("timestamp-overflow", "supported range"),
        ("content-digest", "invalid content digest"),
        ("source-digest", "invalid source-record digest"),
        ("reviewed-commit", "invalid full reviewed commit ID"),
        ("content-directory", "content directory"),
        ("filename", "filename digest"),
    ],
)
def test_receipt_schema_fails_closed(repo: GitRepository, case: str, expected: str) -> None:
    reviewed = repo.head()
    content_digest = snapshot_tree(repo, reviewed).content_sha256
    _, valid_raw = _receipt_bytes(
        repo,
        content_digest=content_digest,
        reviewed_commit=reviewed,
    )
    payload = json.loads(valid_raw)
    raw = valid_raw
    content_directory = content_digest
    filename_digest: str | None = None

    if case == "invalid-utf8":
        raw = b"\xff\n"
    elif case == "non-object":
        raw = b"[]\n"
    elif case == "deep-json":
        raw = b'{"x":' + (b"[" * 1200) + b"0" + (b"]" * 1200) + b"}\n"
    elif case == "huge-integer-json":
        raw = b'{"x":' + (b"1" * 5000) + b"}\n"
    elif case == "review-not-object":
        payload["review"] = []
        raw = canonical_json_bytes(payload)
    elif case == "review-keys":
        payload["review"]["extra"] = True
        raw = canonical_json_bytes(payload)
    elif case == "schema":
        payload["schema_version"] = "3.0.0"
        raw = canonical_json_bytes(payload)
    elif case == "kind":
        payload["kind"] = "other"
        raw = canonical_json_bytes(payload)
    elif case == "profile":
        payload["content_profile"] = "other"
        raw = canonical_json_bytes(payload)
    elif case == "skill":
        payload["review"]["skill"] = "not-a-review"
        raw = canonical_json_bytes(payload)
    elif case == "status":
        payload["review"]["status"] = "issues_found"
        raw = canonical_json_bytes(payload)
    elif case == "timestamp":
        payload["review"]["timestamp"] = "2026-08-23T10:00:00"
        raw = canonical_json_bytes(payload)
    elif case == "timestamp-underflow":
        payload["review"]["timestamp"] = "0001-01-01T00:00:00+23:59"
        raw = canonical_json_bytes(payload)
    elif case == "timestamp-overflow":
        payload["review"]["timestamp"] = "9999-12-31T23:59:59-23:59"
        raw = canonical_json_bytes(payload)
    elif case == "content-digest":
        payload["reviewed_content_sha256"] = "invalid"
        raw = canonical_json_bytes(payload)
    elif case == "source-digest":
        payload["source_record_sha256"] = "invalid"
        raw = canonical_json_bytes(payload)
    elif case == "reviewed-commit":
        payload["reviewed_commit"] = reviewed.upper()
        raw = canonical_json_bytes(payload)
    elif case == "content-directory":
        content_directory = "f" * 64
    elif case == "filename":
        filename_digest = "f" * 64

    _store_receipt_raw(
        repo,
        raw,
        content_directory=content_directory,
        filename_digest=filename_digest,
    )
    with pytest.raises(EvidenceError, match=expected):
        snapshot_tree(repo, "HEAD")


def test_canonical_json_wraps_unencodable_unicode() -> None:
    with pytest.raises(EvidenceError, match="serialized canonically"):
        canonical_json_bytes({"bad": "\ud800"})


def test_timestamp_parser_rejects_empty_invalid_and_internal_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(EvidenceError, match="non-empty string"):
        _parse_aware_timestamp(None)
    with pytest.raises(EvidenceError, match="ISO-8601"):
        _parse_aware_timestamp("not-a-time")

    parsed = Mock()
    parsed.utcoffset.side_effect = OverflowError
    fake_datetime = Mock()
    fake_datetime.fromisoformat.return_value = parsed
    monkeypatch.setattr(evidence_module, "datetime", fake_datetime)
    with pytest.raises(EvidenceError, match="supported range"):
        _parse_aware_timestamp("2026-08-23T10:00:00Z")


def test_validate_receipt_defends_against_direct_bad_entry(repo: GitRepository) -> None:
    bad_path = TreeEntry(raw=b"", mode=b"100644", kind=b"blob", oid="0" * 40, path=b"not-a-receipt")
    with pytest.raises(EvidenceError, match="unknown entry"):
        _validate_receipt(repo=repo, repository=EXPECTED_REPOSITORY, entry=bad_path, raw=b"{}\n")

    content_digest = snapshot_tree(repo, "HEAD").content_sha256
    path, raw = _receipt_bytes(repo, content_digest=content_digest, reviewed_commit=repo.head())
    bad_mode = TreeEntry(
        raw=b"",
        mode=b"100755",
        kind=b"blob",
        oid="0" * 40,
        path=os.fsencode(path.as_posix()),
    )
    with pytest.raises(EvidenceError, match="non-executable regular blob"):
        _validate_receipt(repo=repo, repository=EXPECTED_REPOSITORY, entry=bad_mode, raw=raw)


def test_oversized_receipt_blob_is_rejected_before_parsing(repo: GitRepository) -> None:
    content_digest = snapshot_tree(repo, "HEAD").content_sha256
    raw = b"x" * (MAX_RECEIPT_BYTES + 1)
    receipt_digest = hashlib.sha256(raw).hexdigest()
    path = repo.root / RECEIPT_ROOT / content_digest / f"{receipt_digest}.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    _commit(repo.root, "oversized receipt")

    with pytest.raises(GitError, match="maximum"):
        snapshot_tree(repo, "HEAD")


def test_receipt_batch_byte_cap_is_checked_before_reading(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_receipt(repo)
    monkeypatch.setattr(evidence_module, "MAX_RECEIPT_BATCH_BYTES", 1)
    with pytest.raises(GitError, match="total more than"):
        snapshot_tree(repo, "HEAD")


def test_receipt_namespace_is_validated_in_bounded_batches(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_receipt(repo)
    _add_receipt(
        repo,
        reviewed_commit=repo.head(),
        timestamp="2026-08-23T10:01:00Z",
        source_digest=hashlib.sha256(b"second").hexdigest(),
    )
    read_blobs = Mock(wraps=repo.read_blobs)
    monkeypatch.setattr(repo, "read_blobs", read_blobs)
    monkeypatch.setattr(evidence_module, "RECEIPT_READ_BATCH_SIZE", 1)

    snapshot = snapshot_tree(repo, "HEAD", retain_all_receipts=True)

    assert len(snapshot.receipts) == 2
    assert read_blobs.call_count == 2
    assert RECEIPT_READ_BATCH_SIZE >= 1
    assert MAX_RECEIPT_BATCH_BYTES >= MAX_RECEIPT_BYTES


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/florianhorner/mammamiradio.git",
        "https://github.com/florianhorner/mammamiradio.git/",
        "https://github.com/florianhorner/mammamiradio",
        "git@github.com:florianhorner/mammamiradio.git",
        "ssh://git@github.com/florianhorner/mammamiradio.git",
    ],
)
def test_repository_identity_accepts_exact_https_and_ssh_origins(repo: GitRepository, url: str) -> None:
    _git(repo.root, "remote", "set-url", "origin", url)
    assert repository_identity(repo) == EXPECTED_REPOSITORY


def test_repository_identity_rejects_wrong_or_missing_origin(repo: GitRepository) -> None:
    _git(repo.root, "remote", "set-url", "origin", "https://github.com/other/mammamiradio.git")
    with pytest.raises(EvidenceError, match="scoped"):
        repository_identity(repo)
    _git(repo.root, "remote", "remove", "origin")
    with pytest.raises(GitError, match="remote get-url origin"):
        repository_identity(repo)


def test_repository_identity_rejects_unapproved_url_scheme(repo: GitRepository) -> None:
    _git(repo.root, "remote", "set-url", "origin", "file://github.com/florianhorner/mammamiradio.git")
    with pytest.raises(EvidenceError, match=r"github\.com repository URL"):
        repository_identity(repo)

    _git(repo.root, "remote", "set-url", "origin", "https://github.com/too/many/parts")
    with pytest.raises(EvidenceError, match="unrecognized"):
        repository_identity(repo)


@pytest.mark.parametrize("dirty_kind", ["index", "worktree", "untracked"])
def test_emitter_refuses_all_dirty_states(repo: GitRepository, tmp_path: Path, dirty_kind: str) -> None:
    reviewed = repo.head()
    ledger = _write_ledger(tmp_path, _review_line(reviewed))
    if dirty_kind == "index":
        (repo.root / "staged.txt").write_text("staged\n")
        _git(repo.root, "add", "staged.txt")
    elif dirty_kind == "worktree":
        (repo.root / "seed.txt").write_text("modified\n")
    else:
        (repo.root / "untracked.txt").write_text("untracked\n")

    with pytest.raises(EvidenceError, match="dirty"):
        emit_v2(repo, ledger_root=ledger)


def test_emitter_uses_exact_nested_ledger_and_hashes_raw_line(repo: GitRepository, tmp_path: Path) -> None:
    raw_line = _review_line(repo.head()[:8], timestamp="2026-08-23T12:00:00.123+02:00")
    ledger = _write_ledger(tmp_path, raw_line, nested=True)
    path, created = emit_v2(repo, ledger_root=ledger)
    assert created
    payload = json.loads((repo.root / path).read_bytes())
    assert payload["reviewed_commit"] == repo.head()
    assert payload["review"]["timestamp"] == "2026-08-23T12:00:00.123+02:00"
    assert payload["source_record_sha256"] == hashlib.sha256(raw_line).hexdigest()


def test_emitter_rejects_suffix_lookalike_ledger_directories(repo: GitRepository, tmp_path: Path) -> None:
    gstack = tmp_path / "gstack"
    exact = gstack / "projects" / "florianhorner-mammamiradio"
    exact.mkdir(parents=True)
    for dirname in ("mammamiradio", "someone-mammamiradio"):
        lookalike = gstack / "projects" / dirname
        lookalike.mkdir(parents=True)
        (lookalike / "branch-reviews.jsonl").write_bytes(_review_line(repo.head()))
    with pytest.raises(EvidenceError, match="no review records"):
        emit_v2(repo, ledger_root=gstack)


def test_emitter_does_not_follow_ledger_symlinks(repo: GitRepository, tmp_path: Path) -> None:
    gstack = tmp_path / "gstack"
    project = gstack / "projects" / "florianhorner-mammamiradio"
    outside = tmp_path / "outside"
    project.mkdir(parents=True)
    outside.mkdir()
    (outside / "escaped-reviews.jsonl").write_bytes(_review_line(repo.head()))
    (project / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(EvidenceError, match="no review records"):
        emit_v2(repo, ledger_root=gstack)


@pytest.mark.parametrize("symlink_at", ["proof", "proof/preship-reviews"])
def test_emitter_does_not_follow_receipt_directory_symlinks(
    repo: GitRepository,
    tmp_path: Path,
    symlink_at: str,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = repo.root / symlink_at
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    reviewed = _commit(repo.root, "tracked receipt ancestor symlink")
    ledger = _write_ledger(tmp_path, _review_line(reviewed))

    with pytest.raises(EvidenceError, match="cannot create receipt"):
        emit_v2(repo, ledger_root=ledger)
    assert list(outside.iterdir()) == []


def test_newer_nonclean_review_supersedes_older_clean(repo: GitRepository, tmp_path: Path) -> None:
    commit = repo.head()
    ledger = _write_ledger(
        tmp_path,
        _review_line(commit, timestamp="2026-08-23T10:00:00Z"),
        _review_line(commit, timestamp="2026-08-23T10:01:00Z", status="issues_found"),
    )
    with pytest.raises(EvidenceError, match="issues_found"):
        emit_v2(repo, ledger_root=ledger)


def test_newer_clean_review_supersedes_older_nonclean(repo: GitRepository, tmp_path: Path) -> None:
    commit = repo.head()
    ledger = _write_ledger(
        tmp_path,
        _review_line(commit, timestamp="2026-08-23T10:00:00Z", status="issues_open"),
        _review_line(commit, timestamp="2026-08-23T10:01:00Z"),
    )
    _, created = emit_v2(repo, ledger_root=ledger)
    assert created


def test_same_time_aggregate_review_is_authoritative(repo: GitRepository, tmp_path: Path) -> None:
    commit = repo.head()
    clean_aggregate = _write_ledger(
        tmp_path,
        _review_line(commit, status="issues_found", skill="adversarial-review"),
        _review_line(commit, status="clean", skill="review"),
    )
    _, created = emit_v2(repo, ledger_root=clean_aggregate)
    assert created


def test_same_time_nonclean_aggregate_blocks_clean_adversarial(repo: GitRepository, tmp_path: Path) -> None:
    commit = repo.head()
    ledger = _write_ledger(
        tmp_path,
        _review_line(commit, status="clean", skill="adversarial-review"),
        _review_line(commit, status="issues_open", skill="review"),
    )
    with pytest.raises(EvidenceError, match="issues_open"):
        emit_v2(repo, ledger_root=ledger)


def test_clean_adversarial_review_is_accepted_when_no_aggregate_exists(
    repo: GitRepository,
    tmp_path: Path,
) -> None:
    ledger = _write_ledger(tmp_path, _review_line(repo.head(), skill="adversarial-review"))
    _, created = emit_v2(repo, ledger_root=ledger)
    assert created


def test_same_skill_tie_with_conflicting_status_fails(repo: GitRepository, tmp_path: Path) -> None:
    commit = repo.head()
    ledger = _write_ledger(
        tmp_path,
        _review_line(commit, status="clean"),
        _review_line(commit, status="issues_found"),
    )
    with pytest.raises(EvidenceError, match="disagree"):
        emit_v2(repo, ledger_root=ledger)


def test_same_skill_clean_tie_selects_lowest_source_digest(
    repo: GitRepository,
    tmp_path: Path,
) -> None:
    commit = repo.head()
    ordinary = _review_line(commit)
    whitespace_variant = b" " + ordinary
    lines = sorted((ordinary, whitespace_variant), key=lambda raw: hashlib.sha256(raw).hexdigest())
    ledger = _write_ledger(tmp_path, *lines)

    path, created = emit_v2(repo, ledger_root=ledger)

    assert created
    payload = json.loads((repo.root / path).read_bytes())
    assert payload["source_record_sha256"] == hashlib.sha256(lines[0]).hexdigest()


def test_timestamp_order_uses_utc_instant(repo: GitRepository, tmp_path: Path) -> None:
    commit = repo.head()
    ledger = _write_ledger(
        tmp_path,
        _review_line(commit, timestamp="2026-08-23T10:00:00+02:00", status="clean"),
        _review_line(commit, timestamp="2026-08-23T08:30:00Z", status="issues_found"),
    )
    with pytest.raises(EvidenceError, match="issues_found"):
        emit_v2(repo, ledger_root=ledger)


def test_older_matching_record_after_newer_record_is_ignored(repo: GitRepository, tmp_path: Path) -> None:
    commit = repo.head()
    ledger = _write_ledger(
        tmp_path,
        _review_line(commit, timestamp="2026-08-23T11:00:00Z", status="clean"),
        _review_line(commit, timestamp="2026-08-23T10:00:00Z", status="issues_found"),
    )
    _, created = emit_v2(repo, ledger_root=ledger)
    assert created


def test_matching_naive_timestamp_fails_closed(repo: GitRepository, tmp_path: Path) -> None:
    ledger = _write_ledger(tmp_path, _review_line(repo.head(), timestamp="2026-08-23T10:00:00"))
    with pytest.raises(EvidenceError, match="invalid timestamp"):
        emit_v2(repo, ledger_root=ledger)


def test_malformed_matching_ledger_record_cannot_override_schema(repo: GitRepository, tmp_path: Path) -> None:
    ledger = _write_ledger(tmp_path, _review_line(repo.head(), findings=0))
    with pytest.raises(EvidenceError, match="malformed"):
        emit_v2(repo, ledger_root=ledger)


def test_ledger_parser_ignores_unrelated_and_non_object_records(repo: GitRepository) -> None:
    assert _ledger_record(b"[]\n") is None
    unrelated = json.dumps(
        {
            "skill": "plan-eng-review",
            "commit": repo.head(),
            "timestamp": "2026-08-23T10:00:00Z",
            "status": "clean",
            "findings": [],
        }
    ).encode()
    assert _ledger_record(unrelated) is None


@pytest.mark.parametrize(
    "updates, expected",
    [
        ({"timestamp": 123}, "timestamp"),
        (
            {
                "findings": [
                    {
                        "fingerprint": "scripts/x.py:1:bad",
                        "severity": "INFORMATIONAL",
                        "action": "skipped",
                        "reason": "",
                    }
                ]
            },
            "invalid reason",
        ),
        ({"status": None}, "status"),
    ],
)
def test_ledger_parser_marks_schema_errors(
    repo: GitRepository,
    updates: dict[str, Any],
    expected: str,
) -> None:
    payload: dict[str, Any] = {
        "skill": "review",
        "commit": repo.head(),
        "timestamp": "2026-08-23T10:00:00Z",
        "status": "clean",
        "findings": [],
    }
    payload.update(updates)
    record = _ledger_record(json.dumps(payload).encode() + b"\n")
    assert record is not None
    assert record.schema_error is not None
    assert expected in record.schema_error


def test_ledger_reader_skips_noise_and_symlink_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    valid = project / "valid-reviews.jsonl"
    valid.write_bytes(b"\n--- divider\n[]\n")
    outside = tmp_path / "outside-reviews.jsonl"
    outside.write_bytes(b"{}\n")
    (project / "linked-reviews.jsonl").symlink_to(outside)
    (project / "ignore.txt").write_text("ignored\n")

    assert list(_ledger_files(project)) == [valid]
    assert _read_ledger(project) == []


def test_ledger_reader_rejects_oversized_line_before_json_parsing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "oversized-reviews.jsonl").write_bytes(b"x" * (MAX_LEDGER_LINE_BYTES + 1) + b"\n")

    with pytest.raises(EvidenceError, match=r"ledger line.*exceeds"):
        list(_iter_ledger_records(project))


def test_ledger_walk_and_read_errors_are_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_walk(_project: Path, *, followlinks: bool, onerror: Any) -> list[Any]:
        assert not followlinks
        onerror(OSError("walk failed"))
        return []

    monkeypatch.setattr(evidence_module.os, "walk", failed_walk)
    with pytest.raises(EvidenceError, match="walk failed"):
        list(_ledger_files(tmp_path))

    fake_path = Mock()
    fake_path.open.side_effect = OSError("read failed")
    monkeypatch.setattr(evidence_module, "_ledger_files", Mock(return_value=iter((fake_path,))))
    with pytest.raises(EvidenceError, match="read failed"):
        _read_ledger(tmp_path)


def test_select_review_requires_exact_ledger_directory(repo: GitRepository, tmp_path: Path) -> None:
    with pytest.raises(EvidenceError, match="no exact review ledger directory"):
        select_review_record(
            repo,
            target_content_sha256=snapshot_tree(repo, "HEAD").content_sha256,
            repository=EXPECTED_REPOSITORY,
            ledger_root=tmp_path,
        )


def test_invalid_timestamp_for_unresolvable_record_does_not_block_valid_review(
    repo: GitRepository,
    tmp_path: Path,
) -> None:
    ledger = _write_ledger(
        tmp_path,
        _review_line("f" * 40, timestamp="bad"),
        _review_line(repo.head(), timestamp="2026-08-23T10:00:00Z"),
    )
    _, created = emit_v2(repo, ledger_root=ledger)
    assert created


def test_malformed_namespace_on_unrelated_reviewed_commit_does_not_poison_target(
    repo: GitRepository,
    tmp_path: Path,
) -> None:
    target = repo.head()
    _git(repo.root, "checkout", "-q", "-b", "malformed-side")
    junk = repo.root / RECEIPT_ROOT / "junk"
    junk.parent.mkdir(parents=True)
    junk.write_text("not a receipt\n")
    side_commit = _commit(repo.root, "malformed side receipt")
    _git(repo.root, "checkout", "-q", "main")
    ledger = _write_ledger(
        tmp_path,
        _review_line(side_commit, timestamp="2026-08-23T11:00:00Z"),
        _review_line(target, timestamp="2026-08-23T10:00:00Z"),
    )

    _, created = emit_v2(repo, ledger_root=ledger)
    assert created


@pytest.mark.parametrize(
    "findings",
    [
        ["garbage"],
        [{"fingerprint": "scripts/x.py:1:bad", "severity": "critical", "action": "fixed"}],
        [{"fingerprint": "", "severity": "CRITICAL", "action": "fixed"}],
        [{"fingerprint": "../x.py:1:parent-path", "severity": "CRITICAL", "action": "fixed"}],
        [{"fingerprint": "scripts/x.py:0:zero-line", "severity": "CRITICAL", "action": "fixed"}],
        [{"fingerprint": "scripts/x.py:1:Bad_Slug", "severity": "CRITICAL", "action": "fixed"}],
        [{"fingerprint": "scripts/x.py:1:bad", "severity": "CRITICAL", "action": ""}],
        [{"fingerprint": "scripts/x.py:1:bad", "severity": "CRITICAL", "action": "planned"}],
        [{"fingerprint": "scripts/x.py:1:bad", "severity": "CRITICAL", "action": "skipped "}],
        [{"fingerprint": "scripts/x.py:1:bad", "severity": "CRITICAL", "action": "fixed", "extra": True}],
        [{"fingerprint": "scripts/x.py:1:bad", "severity": "CRITICAL", "action": "skipped"}],
        [
            {
                "fingerprint": "scripts/x.py:1:bad",
                "severity": "CRITICAL",
                "action": "skipped",
                "reason": "accepted residual",
            }
        ],
        [
            {
                "fingerprint": "scripts/x.py:1:bad",
                "severity": "INFORMATIONAL",
                "action": "skipped",
            }
        ],
    ],
)
def test_malformed_or_inconsistent_findings_cannot_emit_clean_receipt(
    repo: GitRepository,
    tmp_path: Path,
    findings: object,
) -> None:
    ledger = _write_ledger(tmp_path, _review_line(repo.head(), findings=findings))
    with pytest.raises(EvidenceError, match="malformed"):
        emit_v2(repo, ledger_root=ledger)


def test_well_formed_resolved_findings_can_emit_clean_receipt(repo: GitRepository, tmp_path: Path) -> None:
    findings = [
        {
            "fingerprint": "scripts/landing/evidence.py:1:resolved",
            "severity": "CRITICAL",
            "action": "fixed",
        },
        {
            "fingerprint": "tests/repo/test_preship_evidence_v2.py:1:auto-resolved",
            "severity": "INFORMATIONAL",
            "action": "auto-fixed",
        },
        {
            "fingerprint": "CLAUDE.md:1:root-file-resolved",
            "severity": "INFORMATIONAL",
            "action": "fixed",
        },
    ]
    ledger = _write_ledger(tmp_path, _review_line(repo.head(), findings=findings))
    _, created = emit_v2(repo, ledger_root=ledger)
    assert created


def test_duplicate_key_ledger_record_is_rejected(repo: GitRepository, tmp_path: Path) -> None:
    commit = repo.head()
    raw = (
        b'{"skill":"review","timestamp":"2026-08-23T10:00:00Z",'
        b'"status":"clean","status":"issues_found","findings":[],"commit":"' + commit.encode() + b'"}\n'
    )
    ledger = _write_ledger(tmp_path, raw)
    with pytest.raises(EvidenceError, match="malformed"):
        emit_v2(repo, ledger_root=ledger)


def test_deeply_nested_ledger_record_is_ignored_without_escaping_policy_errors() -> None:
    raw = b'{"skill":"review","nested":' + (b"[" * 1200) + b"0" + (b"]" * 1200) + b"}\n"
    assert _ledger_record(raw) is None


def test_invalid_utf8_ledger_record_cannot_produce_evidence(repo: GitRepository, tmp_path: Path) -> None:
    ledger = _write_ledger(tmp_path, b'{"skill":"review","commit":"' + repo.head().encode() + b'\xff"}\n')
    with pytest.raises(EvidenceError, match="no review records"):
        emit_v2(repo, ledger_root=ledger)


def test_rewritten_commit_with_identical_content_qualifies(repo: GitRepository, tmp_path: Path) -> None:
    tree = _git(repo.root, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
    rewritten = _git(repo.root, "commit-tree", tree, input_bytes=b"rewritten\n").stdout.decode().strip()
    assert rewritten != repo.head()
    ledger = _write_ledger(tmp_path, _review_line(rewritten))
    _, created = emit_v2(repo, ledger_root=ledger)
    assert created


def test_ancestor_review_with_different_content_does_not_qualify(repo: GitRepository, tmp_path: Path) -> None:
    ancestor = repo.head()
    (repo.root / "feature.txt").write_text("feature\n")
    _commit(repo.root, "feature")
    ledger = _write_ledger(tmp_path, _review_line(ancestor))
    with pytest.raises(EvidenceError, match="no review ledger record matches"):
        emit_v2(repo, ledger_root=ledger)


@pytest.mark.parametrize("bad_commit", ["HEAD", "0" * 40])
def test_symbolic_or_missing_ledger_commit_never_qualifies(
    repo: GitRepository,
    tmp_path: Path,
    bad_commit: str,
) -> None:
    ledger = _write_ledger(tmp_path, _review_line(bad_commit))
    with pytest.raises(EvidenceError, match="no review"):
        emit_v2(repo, ledger_root=ledger)


def test_noncommit_ledger_object_never_qualifies(repo: GitRepository, tmp_path: Path) -> None:
    blob = _git(repo.root, "hash-object", "-w", "--stdin", input_bytes=b"not a commit\n").stdout.decode().strip()
    ledger = _write_ledger(tmp_path, _review_line(blob))
    with pytest.raises(EvidenceError, match="no review ledger record matches"):
        emit_v2(repo, ledger_root=ledger)


def test_hex_named_ref_cannot_masquerade_as_object_abbreviation(
    repo: GitRepository,
    tmp_path: Path,
) -> None:
    fake_prefix = "deadbee"
    assert not repo.head().startswith(fake_prefix)
    _git(repo.root, "branch", fake_prefix, repo.head())
    ledger = _write_ledger(tmp_path, _review_line(fake_prefix))
    with pytest.raises(EvidenceError, match="no review ledger record matches"):
        emit_v2(repo, ledger_root=ledger)


def test_review_selection_deterministically_hashes_physical_line_ending(
    repo: GitRepository,
    tmp_path: Path,
) -> None:
    commit = repo.head()
    line = _review_line(commit).replace(b"\n", b"\r\n")
    ledger = _write_ledger(tmp_path, line)
    snapshot = snapshot_tree(repo, "HEAD")
    record, resolved = select_review_record(
        repo,
        target_content_sha256=snapshot.content_sha256,
        repository=EXPECTED_REPOSITORY,
        ledger_root=ledger,
    )
    assert resolved == commit
    assert record.raw_sha256 == hashlib.sha256(line).hexdigest()


def test_emission_is_idempotent_after_receipt_only_commit(repo: GitRepository, tmp_path: Path) -> None:
    reviewed = repo.head()
    raw_line = _review_line(reviewed[:7])
    ledger = _write_ledger(tmp_path, raw_line)
    first_path, created = emit_v2(repo, ledger_root=ledger)
    assert created
    first_bytes = (repo.root / first_path).read_bytes()
    _commit(repo.root, "receipt only")

    second_path, created = emit_v2(repo, ledger_root=ledger)
    assert not created
    assert second_path == first_path
    assert (repo.root / second_path).read_bytes() == first_bytes


def test_emission_retry_is_idempotent_before_receipt_commit(repo: GitRepository, tmp_path: Path) -> None:
    ledger = _write_ledger(tmp_path, _review_line(repo.head()))
    first_path, created = emit_v2(repo, ledger_root=ledger)
    assert created

    second_path, created = emit_v2(repo, ledger_root=ledger)
    assert not created
    assert second_path == first_path


def test_emission_refuses_changed_existing_receipt(repo: GitRepository, tmp_path: Path) -> None:
    ledger = _write_ledger(tmp_path, _review_line(repo.head()))
    path, created = emit_v2(repo, ledger_root=ledger)
    assert created
    (repo.root / path).write_text("changed\n")

    with pytest.raises(EvidenceError, match="non-identical receipt"):
        emit_v2(repo, ledger_root=ledger)


@pytest.mark.parametrize(
    "relative",
    [Path("leaf.json"), Path("/absolute/leaf.json"), Path("proof/../escape.json")],
)
def test_atomic_writer_rejects_unsafe_paths(tmp_path: Path, relative: Path) -> None:
    with pytest.raises(EvidenceError, match="safe relative path"):
        _write_exclusive(tmp_path, relative, b"receipt\n")


def test_write_all_handles_partial_writes_and_rejects_zero_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write = Mock(side_effect=[2, 3])
    monkeypatch.setattr(evidence_module.os, "write", write)
    _write_all(123, b"hello")
    assert write.call_args_list[0].args == (123, b"hello")
    assert write.call_args_list[1].args == (123, b"llo")

    monkeypatch.setattr(evidence_module.os, "write", Mock(return_value=0))
    with pytest.raises(EvidenceError, match="made no progress"):
        _write_all(123, b"x")


def test_existing_receipt_reader_detects_growth_after_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "receipt.json").write_bytes(b"x")
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(evidence_module.os, "read", Mock(return_value=b"xx"))
        assert _read_existing_at(parent_fd, "receipt.json", expected_length=1) == b"xx"
    finally:
        os.close(parent_fd)


def test_atomic_writer_requires_no_follow_platform_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(evidence_module.os, "O_NOFOLLOW")
    with pytest.raises(EvidenceError, match="POSIX no-follow"):
        _write_exclusive(tmp_path, Path("proof/receipt.json"), b"receipt\n")


def test_atomic_writer_bounds_temporary_name_collisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "proof"
    parent.mkdir()
    candidate = f".receipt-{os.getpid()}-fixed"
    (parent / candidate).write_text("collision\n")
    monkeypatch.setattr(evidence_module.secrets, "token_hex", Mock(return_value="fixed"))

    with pytest.raises(EvidenceError, match="cannot allocate a temporary receipt"):
        _write_exclusive(tmp_path, Path("proof/receipt.json"), b"receipt\n")


def test_atomic_writer_wraps_body_write_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(evidence_module, "_write_all", Mock(side_effect=OSError("disk failed")))
    with pytest.raises(EvidenceError, match="disk failed"):
        _write_exclusive(tmp_path, Path("proof/receipt.json"), b"receipt\n")


@pytest.mark.parametrize("raced_bytes, expected_created", [(b"receipt\n", False), (b"other\n", None)])
def test_atomic_writer_handles_concurrent_destination_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raced_bytes: bytes,
    expected_created: bool | None,
) -> None:
    destination = tmp_path / "proof/receipt.json"

    def racing_link(*_args: Any, **_kwargs: Any) -> None:
        destination.write_bytes(raced_bytes)
        raise FileExistsError

    monkeypatch.setattr(evidence_module.os, "link", racing_link)
    if expected_created is None:
        with pytest.raises(EvidenceError, match="concurrent process created non-identical"):
            _write_exclusive(tmp_path, Path("proof/receipt.json"), b"receipt\n")
    else:
        assert _write_exclusive(tmp_path, Path("proof/receipt.json"), b"receipt\n") is expected_created


@pytest.mark.parametrize("cleanup_error", [FileNotFoundError(), PermissionError("denied")])
def test_atomic_writer_cleanup_errors_do_not_mask_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_error: OSError,
) -> None:
    monkeypatch.setattr(evidence_module.os, "unlink", Mock(side_effect=cleanup_error))
    assert _write_exclusive(tmp_path, Path("proof/receipt.json"), b"receipt\n")


@pytest.mark.parametrize("leaf_kind", ["fifo", "directory", "symlink"])
def test_emission_refuses_non_regular_existing_leaf_without_following_it(
    repo: GitRepository,
    tmp_path: Path,
    leaf_kind: str,
) -> None:
    ledger = _write_ledger(tmp_path, _review_line(repo.head()))
    path, created = emit_v2(repo, ledger_root=ledger)
    assert created
    leaf = repo.root / path
    leaf.unlink()
    outside = tmp_path / "outside"
    outside.write_text("outside\n")
    if leaf_kind == "fifo":
        os.mkfifo(leaf)
    elif leaf_kind == "directory":
        leaf.mkdir()
    else:
        leaf.symlink_to(outside)

    with pytest.raises(EvidenceError, match=r"cannot create receipt|not a regular file"):
        emit_v2(repo, ledger_root=ledger)
    assert outside.read_text() == "outside\n"


def test_emitter_detects_head_change_before_write(
    repo: GitRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed = repo.head()
    ledger = _write_ledger(tmp_path, _review_line(reviewed))
    monkeypatch.setattr(repo, "head", Mock(side_effect=[reviewed, "f" * repo.oid_length]))

    with pytest.raises(EvidenceError, match="HEAD changed"):
        emit_v2(repo, ledger_root=ledger)
    assert not (repo.root / RECEIPT_ROOT).exists()


def test_emitter_detects_worktree_change_before_write(
    repo: GitRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _write_ledger(tmp_path, _review_line(repo.head()))
    monkeypatch.setattr(repo, "status_bytes", Mock(side_effect=[b"", b"? raced\0"]))

    with pytest.raises(EvidenceError, match="working tree changed"):
        emit_v2(repo, ledger_root=ledger)
    assert not (repo.root / RECEIPT_ROOT).exists()


def test_emitter_refuses_non_head_target(repo: GitRepository, tmp_path: Path) -> None:
    old = repo.head()
    (repo.root / "later.txt").write_text("later\n")
    _commit(repo.root, "later")
    ledger = _write_ledger(tmp_path, _review_line(old))
    with pytest.raises(EvidenceError, match="not the checked-out HEAD"):
        emit_v2(repo, target=old, ledger_root=ledger)


def test_pr_and_main_verification_accept_fresh_receipt(repo: GitRepository, tmp_path: Path) -> None:
    base = repo.head()
    (repo.root / "feature.txt").write_text("feature\n")
    reviewed = _commit(repo.root, "feature")
    target, path = _emit_and_commit(repo, tmp_path)

    pr_result = verify_v2(repo, target=target, base=base, mode="pr")
    main_result = verify_v2(repo, target=target, base=None, mode="main")
    assert pr_result.matching_receipts == (path.as_posix(),)
    assert main_result.content_sha256 == snapshot_tree(repo, reviewed).content_sha256


def test_receipt_is_process_evidence_not_cryptographic_ledger_attestation(repo: GitRepository) -> None:
    base = repo.head()
    forged_path, _ = _add_receipt(
        repo,
        reviewed_commit=base,
        source_digest="f" * 64,
    )

    result = verify_v2(repo, target="HEAD", base=base, mode="pr")
    assert result.matching_receipts == (forged_path.as_posix(),)


def test_committed_receipt_is_not_revoked_by_later_local_ledger_outcome(
    repo: GitRepository,
    tmp_path: Path,
) -> None:
    base = repo.head()
    clean_line = _review_line(base, timestamp="2026-08-23T10:00:00Z")
    ledger = _write_ledger(tmp_path, clean_line)
    path, created = emit_v2(repo, ledger_root=ledger)
    assert created
    target = _commit(repo.root, "commit clean receipt")
    project_file = ledger / "projects/florianhorner-mammamiradio/branch-reviews.jsonl"
    project_file.write_bytes(
        clean_line
        + _review_line(
            target,
            timestamp="2026-08-23T10:01:00Z",
            status="issues_found",
        )
    )

    with pytest.raises(EvidenceError, match="issues_found"):
        select_review_record(
            repo,
            target_content_sha256=snapshot_tree(repo, target).content_sha256,
            repository=EXPECTED_REPOSITORY,
            ledger_root=ledger,
        )
    assert verify_v2(repo, target=target, base=base, mode="pr").matching_receipts == (path.as_posix(),)


def test_main_verification_survives_real_parentless_squash(repo: GitRepository, tmp_path: Path) -> None:
    (repo.root / "feature.txt").write_text("feature\n")
    reviewed = _commit(repo.root, "reviewed feature")
    receipt_commit, path = _emit_and_commit(repo, tmp_path)
    tree = _git(repo.root, "rev-parse", f"{receipt_commit}^{{tree}}").stdout.decode().strip()
    squash = _git(repo.root, "commit-tree", tree, input_bytes=b"squash\n").stdout.decode().strip()

    assert not repo.is_ancestor(reviewed, squash)
    result = verify_v2(repo, target=squash, base=None, mode="main")
    assert result.matching_receipts == (path.as_posix(),)


@pytest.mark.parametrize(
    "mode, base, expected",
    [
        ("other", None, "unsupported verification mode"),
        ("pr", None, "explicit base commit"),
        ("main", None, "no surviving v2 receipt"),
    ],
)
def test_verification_rejects_invalid_api_or_missing_evidence(
    repo: GitRepository,
    mode: str,
    base: str | None,
    expected: str,
) -> None:
    with pytest.raises(EvidenceError, match=expected):
        verify_v2(repo, target="HEAD", base=base, mode=mode)


def test_pr_verification_rejects_code_changed_after_review(repo: GitRepository, tmp_path: Path) -> None:
    base = repo.head()
    (repo.root / "feature.txt").write_text("reviewed\n")
    _commit(repo.root, "reviewed")
    _emit_and_commit(repo, tmp_path)
    (repo.root / "feature.txt").write_text("changed later\n")
    target = _commit(repo.root, "unreviewed change")

    with pytest.raises(EvidenceError, match="does not match the target content"):
        verify_v2(repo, target=target, base=base, mode="pr")


def test_pr_verification_requires_current_base(repo: GitRepository) -> None:
    old_base = repo.head()
    _git(repo.root, "checkout", "-q", "-b", "feature")
    (repo.root / "feature.txt").write_text("feature\n")
    target = _commit(repo.root, "feature")
    _git(repo.root, "checkout", "-q", "main")
    (repo.root / "main.txt").write_text("main\n")
    current_base = _commit(repo.root, "main moved")
    assert old_base != current_base

    with pytest.raises(EvidenceError, match="not an ancestor"):
        verify_v2(repo, target=target, base=current_base, mode="pr")


def test_pr_rejects_deleted_base_receipt(repo: GitRepository) -> None:
    _add_receipt(repo)
    base = repo.head()
    base_receipt = next(iter(snapshot_tree(repo, base, retain_all_receipts=True).receipts))
    (repo.root / os.fsdecode(base_receipt)).unlink()
    reviewed = _commit(repo.root, "delete base receipt")
    _add_receipt(
        repo,
        reviewed_commit=reviewed,
        source_digest=hashlib.sha256(b"new source").hexdigest(),
    )
    target = repo.head()

    with pytest.raises(EvidenceError, match="was deleted or modified"):
        verify_v2(repo, target=target, base=base, mode="pr")


def test_pr_rejects_modified_base_receipt_mode(repo: GitRepository) -> None:
    _add_receipt(repo)
    base = repo.head()
    base_receipt = next(iter(snapshot_tree(repo, base, retain_all_receipts=True).receipts))
    (repo.root / os.fsdecode(base_receipt)).chmod(0o755)
    target = _commit(repo.root, "modify base receipt mode")

    with pytest.raises(EvidenceError, match="was deleted or modified"):
        verify_v2(repo, target=target, base=base, mode="pr")


def test_pr_verifier_defensively_rejects_non_additive_namespace_diff(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = b"proof/preship-reviews/v2/" + (b"a" * 64) + b"/" + (b"b" * 64) + b".json"
    monkeypatch.setattr(repo, "diff_paths", Mock(side_effect=[(), (path,)]))

    with pytest.raises(EvidenceError, match="was deleted or modified"):
        verify_v2(repo, target="HEAD", base="HEAD", mode="pr")


def test_pr_accepts_historical_base_receipts_plus_new_final_receipt(repo: GitRepository) -> None:
    _add_receipt(repo, timestamp="2026-08-23T09:00:00Z")
    base = repo.head()
    (repo.root / "feature.txt").write_text("feature\n")
    reviewed = _commit(repo.root, "feature")
    final_path, _ = _add_receipt(
        repo,
        reviewed_commit=reviewed,
        timestamp="2026-08-23T10:00:00Z",
        source_digest=hashlib.sha256(b"final").hexdigest(),
    )

    result = verify_v2(repo, target="HEAD", base=base, mode="pr")
    assert result.matching_receipts == (final_path.as_posix(),)


def test_pr_requires_a_new_matching_receipt(repo: GitRepository) -> None:
    _add_receipt(repo)
    base = repo.head()
    (repo.root / "feature.txt").write_text("unreviewed\n")
    target = _commit(repo.root, "feature without receipt")

    with pytest.raises(EvidenceError, match="adds no new v2 review receipt"):
        verify_v2(repo, target=target, base=base, mode="pr")


def test_pr_rejects_new_receipt_count_above_bound(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = repo.head()
    _add_receipt(repo)
    monkeypatch.setattr(evidence_module, "MAX_NEW_RECEIPTS", 0)
    with pytest.raises(EvidenceError, match="more than 0 entries"):
        verify_v2(repo, target="HEAD", base=base, mode="pr")

    assert MAX_NEW_RECEIPTS >= 1


def test_append_only_history_stays_landable_across_read_batches(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_receipt(repo, timestamp="2026-08-23T09:00:00Z")
    _add_receipt(
        repo,
        reviewed_commit=repo.head(),
        timestamp="2026-08-23T09:30:00Z",
        source_digest=hashlib.sha256(b"historical second").hexdigest(),
    )
    base = repo.head()
    _add_receipt(
        repo,
        reviewed_commit=base,
        timestamp="2026-08-23T10:00:00Z",
        source_digest=hashlib.sha256(b"new receipt").hexdigest(),
    )
    monkeypatch.setattr(evidence_module, "RECEIPT_READ_BATCH_SIZE", 1)

    pr_result = verify_v2(repo, target="HEAD", base=base, mode="pr")
    main_result = verify_v2(repo, target="HEAD", base=None, mode="main")

    assert len(pr_result.matching_receipts) == 1
    assert len(main_result.matching_receipts) == 3


def test_pr_rejects_unavailable_reviewed_commit_while_main_accepts(repo: GitRepository) -> None:
    base = repo.head()
    fake_commit = "f" * repo.oid_length
    content_digest = snapshot_tree(repo, "HEAD").content_sha256
    _add_receipt(repo, reviewed_commit=fake_commit, content_digest=content_digest)
    target = repo.head()

    with pytest.raises(EvidenceError, match="unavailable reviewed commit"):
        verify_v2(repo, target=target, base=base, mode="pr")
    assert verify_v2(repo, target=target, base=None, mode="main").content_sha256 == content_digest


@pytest.mark.parametrize("descends_from_base", [False, True])
def test_pr_rejects_reviewed_commit_outside_base_to_target_history(
    repo: GitRepository,
    descends_from_base: bool,
) -> None:
    base = repo.head()
    content_digest = snapshot_tree(repo, base).content_sha256
    tree = _git(repo.root, "rev-parse", f"{base}^{{tree}}").stdout.decode().strip()
    commit_args = ["commit-tree", tree]
    if descends_from_base:
        commit_args.extend(["-p", base])
    side_commit = _git(repo.root, *commit_args, input_bytes=b"side\n").stdout.decode().strip()
    assert repo.is_ancestor(base, side_commit) is descends_from_base
    assert not repo.is_ancestor(side_commit, base)
    _add_receipt(repo, reviewed_commit=side_commit, content_digest=content_digest)

    with pytest.raises(EvidenceError, match="outside base-to-target history"):
        verify_v2(repo, target="HEAD", base=base, mode="pr")


def test_pr_bounds_transient_receipts_before_scanning_reviewed_commit(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = repo.head()
    content_digest = snapshot_tree(repo, base).content_sha256
    first, _ = _add_receipt(repo, reviewed_commit=base, content_digest=content_digest)
    second, _ = _add_receipt(
        repo,
        reviewed_commit=base,
        content_digest=content_digest,
        timestamp="2026-08-23T10:01:00Z",
        source_digest=hashlib.sha256(b"transient second").hexdigest(),
    )
    reviewed = repo.head()
    (repo.root / first).unlink()
    (repo.root / second).unlink()
    _commit(repo.root, "remove transient receipts")
    _add_receipt(
        repo,
        reviewed_commit=reviewed,
        content_digest=content_digest,
        timestamp="2026-08-23T10:02:00Z",
        source_digest=hashlib.sha256(b"final").hexdigest(),
    )
    monkeypatch.setattr(evidence_module, "MAX_NEW_RECEIPTS", 1)
    real_snapshot_tree = evidence_module.snapshot_tree
    snapshot_spy = Mock(wraps=real_snapshot_tree)
    monkeypatch.setattr(evidence_module, "snapshot_tree", snapshot_spy)

    with pytest.raises(EvidenceError, match=r"reviewed commit .* adds more than 1 entries"):
        verify_v2(repo, target="HEAD", base=base, mode="pr")

    assert all(call.args[1] != reviewed for call in snapshot_spy.call_args_list)


def test_pr_rejects_reviewed_commit_that_temporarily_modifies_base_receipt(repo: GitRepository) -> None:
    base_path, _ = _add_receipt(repo)
    base = repo.head()
    content_digest = snapshot_tree(repo, base).content_sha256
    receipt_file = repo.root / base_path
    receipt_file.chmod(0o755)
    reviewed = _commit(repo.root, "temporarily modify base receipt")
    receipt_file.chmod(0o644)
    _commit(repo.root, "restore base receipt")
    _add_receipt(
        repo,
        reviewed_commit=reviewed,
        content_digest=content_digest,
        timestamp="2026-08-23T10:01:00Z",
        source_digest=hashlib.sha256(b"final after restore").hexdigest(),
    )

    with pytest.raises(EvidenceError, match="deletes or modifies base v2 receipt"):
        verify_v2(repo, target="HEAD", base=base, mode="pr")


def test_main_verifies_surviving_digest_without_reopening_reviewed_commits(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong_reviewed_commit = repo.head()
    (repo.root / "new.txt").write_text("new content\n")
    _commit(repo.root, "new content")
    content_digest = snapshot_tree(repo, "HEAD").content_sha256
    first_path, _ = _add_receipt(
        repo,
        reviewed_commit=wrong_reviewed_commit,
        content_digest=content_digest,
        source_digest=hashlib.sha256(b"first").hexdigest(),
    )
    second_path, _ = _add_receipt(
        repo,
        reviewed_commit=repo.head(),
        content_digest=content_digest,
        timestamp="2026-08-23T10:01:00Z",
        source_digest=hashlib.sha256(b"second").hexdigest(),
    )
    real_snapshot_tree = evidence_module.snapshot_tree
    snapshot_spy = Mock(wraps=real_snapshot_tree)
    monkeypatch.setattr(evidence_module, "snapshot_tree", snapshot_spy)
    monkeypatch.setattr(
        repo,
        "resolve_full_commit",
        Mock(side_effect=AssertionError("main mode must not reopen reviewed commits")),
    )

    result = verify_v2(repo, target="HEAD", base=None, mode="main")

    assert result.matching_receipts == tuple(sorted((first_path.as_posix(), second_path.as_posix())))
    assert snapshot_spy.call_count == 1


def test_main_validates_but_does_not_retain_stale_receipts(repo: GitRepository) -> None:
    _add_receipt(repo)
    (repo.root / "seed.txt").write_text("changed after review\n")
    _commit(repo.root, "change reviewed content")

    assert snapshot_tree(repo, "HEAD", retain_matching_receipts=True).receipts == {}
    with pytest.raises(EvidenceError, match="no surviving v2 receipt"):
        verify_v2(repo, target="HEAD", base=None, mode="main")


def test_pr_rejects_receipt_whose_full_hex_name_is_only_a_ref(repo: GitRepository) -> None:
    base = repo.head()
    fake_full_id = "d" * repo.oid_length
    assert repo.head() != fake_full_id
    _git(repo.root, "update-ref", f"refs/heads/{fake_full_id}", repo.head())
    content_digest = snapshot_tree(repo, "HEAD").content_sha256
    _add_receipt(repo, reviewed_commit=fake_full_id, content_digest=content_digest)

    with pytest.raises(EvidenceError, match="unavailable reviewed commit"):
        verify_v2(repo, target="HEAD", base=base, mode="pr")


def test_pr_rejects_receipt_that_pins_different_resolvable_content(repo: GitRepository) -> None:
    base = repo.head()
    wrong_reviewed_commit = base
    (repo.root / "feature.txt").write_text("feature\n")
    _commit(repo.root, "feature")
    content_digest = snapshot_tree(repo, "HEAD").content_sha256
    _add_receipt(repo, reviewed_commit=wrong_reviewed_commit, content_digest=content_digest)

    with pytest.raises(EvidenceError, match="does not match its reviewed commit"):
        verify_v2(repo, target="HEAD", base=base, mode="pr")


def test_pr_rejects_self_consistent_historical_receipt_added_beside_final_receipt(
    repo: GitRepository,
) -> None:
    base = repo.head()
    historical_digest = snapshot_tree(repo, base).content_sha256
    (repo.root / "feature.txt").write_text("feature\n")
    reviewed = _commit(repo.root, "reviewed feature")
    _add_receipt(repo, reviewed_commit=reviewed)
    _add_receipt(
        repo,
        reviewed_commit=base,
        content_digest=historical_digest,
        source_digest=hashlib.sha256(b"historical import").hexdigest(),
    )

    with pytest.raises(EvidenceError, match="does not match the target content"):
        verify_v2(repo, target="HEAD", base=base, mode="pr")


def test_pr_returns_multiple_matching_receipts_in_sorted_order(repo: GitRepository) -> None:
    base = repo.head()
    content_digest = snapshot_tree(repo, "HEAD").content_sha256
    first, _ = _add_receipt(
        repo,
        reviewed_commit=base,
        content_digest=content_digest,
        timestamp="2026-08-23T10:00:00Z",
        source_digest=hashlib.sha256(b"first").hexdigest(),
        commit=False,
    )
    second, _ = _add_receipt(
        repo,
        reviewed_commit=base,
        content_digest=content_digest,
        timestamp="2026-08-23T10:01:00Z",
        source_digest=hashlib.sha256(b"second").hexdigest(),
    )

    result = verify_v2(repo, target="HEAD", base=base, mode="pr")
    assert result.matching_receipts == tuple(sorted((first.as_posix(), second.as_posix())))


def test_ordinary_proof_files_are_part_of_v2_content_identity(repo: GitRepository) -> None:
    # The retired fixed-name v1 artifact doubles as the example: any ordinary
    # proof/ file is reviewed content, only the v2 receipt namespace is excluded.
    before = snapshot_tree(repo, "HEAD").content_sha256
    legacy = repo.root / "proof/preship-review.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"schema_version":"1.0.0"}\n')
    _commit(repo.root, "legacy evidence")
    after = snapshot_tree(repo, "HEAD").content_sha256
    assert after != before


def test_parallel_branches_use_distinct_receipt_paths_without_merge_conflict(repo: GitRepository) -> None:
    base = repo.head()
    _git(repo.root, "checkout", "-q", "-b", "branch-a")
    (repo.root / "a.txt").write_text("a\n")
    reviewed_a = _commit(repo.root, "feature a")
    path_a, _ = _add_receipt(repo, reviewed_commit=reviewed_a)

    _git(repo.root, "checkout", "-q", "-b", "branch-b", base)
    (repo.root / "b.txt").write_text("b\n")
    reviewed_b = _commit(repo.root, "feature b")
    path_b, _ = _add_receipt(repo, reviewed_commit=reviewed_b)
    assert path_a != path_b

    merge = _git(repo.root, "merge", "--no-edit", "branch-a", check=False)
    assert merge.returncode == 0, merge.stderr.decode()
    assert (repo.root / path_a).is_file()
    assert (repo.root / path_b).is_file()


def test_v2_shell_checker_dispatches_to_python_package(repo: GitRepository) -> None:
    env = os.environ.copy()
    env["MAMMAMIRADIO_PYTHON"] = sys.executable
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [
            "bash",
            str(CHECK),
            "--v2",
            "--target",
            repo.head(),
            "--base",
            repo.head(),
            "--mode",
            "pr",
        ],
        cwd=repo.root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "PR adds no new v2 review receipt" in result.stderr


def test_v2_shell_checker_succeeds_for_fresh_receipt(repo: GitRepository, tmp_path: Path) -> None:
    base = repo.head()
    target, _ = _emit_and_commit(repo, tmp_path)
    env = os.environ.copy()
    env["MAMMAMIRADIO_PYTHON"] = sys.executable
    result = subprocess.run(
        [
            "bash",
            str(CHECK),
            "--v2",
            "--target",
            target,
            "--base",
            base,
            "--mode",
            "pr",
        ],
        cwd=repo.root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "landing-evidence: OK" in result.stdout


@pytest.mark.parametrize(
    "wrapper,args",
    [
        (CHECK, ["--v2", "--target", "HEAD", "--mode", "main"]),
        (EMIT, ["--v2"]),
    ],
)
def test_v2_wrappers_cannot_import_hostile_cwd_or_pythonpath(
    tmp_path: Path,
    wrapper: Path,
    args: list[str],
) -> None:
    hostile = tmp_path / "hostile"
    trap = hostile / "scripts/landing"
    trap.mkdir(parents=True)
    (hostile / "scripts/__init__.py").write_text("")
    marker = tmp_path / "executed"
    (trap / "__init__.py").write_text("")
    (trap / "__main__.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n")
    env = os.environ.copy()
    env["MAMMAMIRADIO_PYTHON"] = sys.executable
    env["PYTHONPATH"] = str(hostile)

    result = subprocess.run(
        ["bash", str(wrapper), *args],
        cwd=hostile,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert not marker.exists()
    assert "not a Git repository" in result.stderr


def test_bare_cli_prefers_repository_package_over_hostile_pythonpath(tmp_path: Path) -> None:
    hostile = tmp_path / "hostile"
    trap = hostile / "scripts/landing"
    trap.mkdir(parents=True)
    (hostile / "scripts/__init__.py").write_text("")
    marker = tmp_path / "executed"
    (trap / "__init__.py").write_text("")
    (trap / "__main__.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(hostile)

    result = subprocess.run(
        [sys.executable, "-m", "scripts.landing", "--help"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert "emit or verify pre-ship review evidence" in result.stdout


@pytest.mark.parametrize("created, verb", [(True, "wrote"), (False, "already matches")])
def test_cli_emit_reports_create_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    created: bool,
    verb: str,
) -> None:
    fake_repo = Mock()
    monkeypatch.setattr(landing_cli.GitRepository, "discover", Mock(return_value=fake_repo))
    monkeypatch.setattr(landing_cli, "emit_v2", Mock(return_value=(Path("proof/receipt.json"), created)))
    monkeypatch.setattr(landing_cli, "retire_superseded_receipts", Mock(return_value=((), ())))

    assert landing_cli.main(["evidence", "emit", "--target", "HEAD"]) == 0
    assert f"landing-evidence: {verb} proof/receipt.json" in capsys.readouterr().out


def test_cli_verify_reports_matching_receipts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_repo = Mock()
    result = VerificationResult(
        mode="main",
        target="a" * 40,
        content_sha256="b" * 64,
        matching_receipts=("one", "two"),
    )
    monkeypatch.setattr(landing_cli.GitRepository, "discover", Mock(return_value=fake_repo))
    monkeypatch.setattr(landing_cli, "verify_v2", Mock(return_value=result))

    assert landing_cli.main(["evidence", "verify", "--target", "HEAD", "--mode", "main"]) == 0
    output = capsys.readouterr().out
    assert "main content" in output
    assert "2 matching v2 receipt(s)" in output


def test_cli_converts_landing_errors_to_exit_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        landing_cli.GitRepository,
        "discover",
        Mock(side_effect=GitError("synthetic failure")),
    )

    assert landing_cli.main(["evidence", "verify", "--target", "HEAD", "--mode", "main"]) == 1
    assert "landing-evidence: FAIL — synthetic failure" in capsys.readouterr().err


def test_workflow_verifies_v2_only_from_trusted_base() -> None:
    text = WORKFLOW.read_text()
    assert "name: pre-ship evidence (report-only)" in text
    assert "ref: ${{ github.event.pull_request.base.sha }}" in text
    assert text.count('git fetch --no-tags origin "$HEAD_SHA"') == 1
    assert 'actual_base="$(git rev-parse HEAD)"' in text
    assert 'if [ "$actual_base" != "$BASE_SHA" ]' in text
    assert "name: Check v2 pre-ship review evidence" in text
    assert "python3 -S -P -m scripts.landing evidence verify" in text
    assert '--target "$HEAD_SHA" --base "$BASE_SHA" --mode pr' in text
    assert "base predates the v2 verifier" in text
    assert "workflow definition itself is still PR-controlled" in text
    assert "base-owned control plane" in text
    assert "reopened" in text
    assert "github.event.pull_request.user.login != 'dependabot[bot]'" in text
    assert "git checkout" not in text
    assert "ref: ${{ github.event.pull_request.head.sha }}" not in text
    # The fixed-name v1 artifact is retired; the workflow neither reads nor
    # mentions a v1 checking step anymore, and says why the file is gone.
    assert "Check v1 pre-ship review evidence" not in text
    assert "proof/preship-review.json" not in text
    assert "legacy fixed-name v1 file is retired" in text
    # Report-only, not blocking: the v2 check annotates and never fails the job.
    # These guard the report-only -> blocking flip, which is a separate approval.
    assert "(report-only)" in text
    assert "::warning::" in text
    v2_step = text[text.index("name: Check v2 pre-ship review evidence") :]
    assert "::error::" not in v2_step
    assert "exit 1" not in v2_step

    quality = QUALITY_WORKFLOW.read_text()
    assert "Landing policy full branch coverage" in quality
    assert "--cov=scripts.landing --cov-branch --cov-fail-under=100" in quality


def _set_origin_main(repo: GitRepository, commit: str) -> None:
    """Point the local remote-tracking main at ``commit`` (the landed truth)."""

    _git(repo.root, "update-ref", "refs/remotes/origin/main", commit)


def _integrated_branch(repo: GitRepository, tmp_path: Path) -> tuple[str, str, Path, dict[str, Any]]:
    """Reviewed feature branch, receipt committed, base advanced, base cleanly merged.

    Sets ``origin/main`` to the merged base, since retirement anchors to it.
    Returns (base_commit, merge_commit, old_receipt_path, old_receipt_payload).
    """

    fork_point = repo.head()
    (repo.root / "feature.txt").write_text("feature\n")
    _commit(repo.root, "feature work")
    _, old_path = _emit_and_commit(repo, tmp_path)
    old_payload = json.loads((repo.root / old_path).read_bytes())
    _git(repo.root, "checkout", "-q", "-b", "base-branch", fork_point)
    (repo.root / "base-only.txt").write_text("base\n")
    base = _commit(repo.root, "base advances")
    _git(repo.root, "checkout", "-q", "main")
    merge = _git(repo.root, "merge", "--no-edit", base, check=False)
    assert merge.returncode == 0, merge.stderr.decode()
    _set_origin_main(repo, base)
    return base, repo.head(), old_path, old_payload


def test_reattest_derives_receipt_after_clean_base_integration(repo: GitRepository, tmp_path: Path) -> None:
    base, merge_commit, old_path, old_payload = _integrated_branch(repo, tmp_path)

    path, created, superseded = evidence_module.reattest_v2(repo, base=base)
    assert created
    assert superseded == (old_path,)
    assert not (repo.root / old_path).exists()

    payload = json.loads((repo.root / path).read_bytes())
    assert payload["reviewed_commit"] == merge_commit
    assert payload["reviewed_content_sha256"] == snapshot_tree(repo, merge_commit).content_sha256
    assert payload["review"] == old_payload["review"]
    assert payload["source_record_sha256"] == old_payload["source_record_sha256"]

    target = _commit(repo.root, "reattest receipt swap")
    result = verify_v2(repo, target=target, base=base, mode="pr")
    assert result.matching_receipts == (path.as_posix(),)


def test_reattest_is_idempotent_after_the_receipt_swap_lands(repo: GitRepository, tmp_path: Path) -> None:
    base, _, _, _ = _integrated_branch(repo, tmp_path)
    first_path, created, superseded = evidence_module.reattest_v2(repo, base=base)
    assert created and superseded
    _commit(repo.root, "reattest receipt swap")

    again_path, created_again, superseded_again = evidence_module.reattest_v2(repo, base=base)
    assert again_path == first_path
    assert not created_again
    assert superseded_again == ()


def test_reattest_chains_across_two_base_integrations(repo: GitRepository, tmp_path: Path) -> None:
    base, _, _, old_payload = _integrated_branch(repo, tmp_path)
    first_path, _, _ = evidence_module.reattest_v2(repo, base=base)
    _commit(repo.root, "first reattestation")

    _git(repo.root, "checkout", "-q", "base-branch")
    (repo.root / "base-second.txt").write_text("more base\n")
    second_base = _commit(repo.root, "base advances again")
    _git(repo.root, "checkout", "-q", "main")
    merge = _git(repo.root, "merge", "--no-edit", second_base, check=False)
    assert merge.returncode == 0, merge.stderr.decode()
    _set_origin_main(repo, second_base)

    second_path, created, superseded = evidence_module.reattest_v2(repo, base=second_base)
    assert created
    assert superseded == (first_path,)
    payload = json.loads((repo.root / second_path).read_bytes())
    assert payload["review"] == old_payload["review"]

    target = _commit(repo.root, "second reattestation")
    result = verify_v2(repo, target=target, base=second_base, mode="pr")
    assert result.matching_receipts == (second_path.as_posix(),)


def test_reattest_refuses_post_review_content_change(repo: GitRepository, tmp_path: Path) -> None:
    base, _, _, _ = _integrated_branch(repo, tmp_path)
    (repo.root / "sneak.txt").write_text("unreviewed\n")
    _commit(repo.root, "unreviewed change after the merge")

    with pytest.raises(EvidenceError, match="no existing receipt derives this content"):
        evidence_module.reattest_v2(repo, base=base)


def test_reattest_refuses_hand_resolved_conflicted_integration(repo: GitRepository, tmp_path: Path) -> None:
    fork_point = repo.head()
    (repo.root / "feature.txt").write_text("feature\n")
    _commit(repo.root, "feature work")
    _emit_and_commit(repo, tmp_path)
    _git(repo.root, "checkout", "-q", "-b", "base-branch", fork_point)
    (repo.root / "feature.txt").write_text("conflicting\n")
    base = _commit(repo.root, "base edits the same file")
    _git(repo.root, "checkout", "-q", "main")
    merge = _git(repo.root, "merge", "--no-edit", base, check=False)
    assert merge.returncode != 0
    (repo.root / "feature.txt").write_text("hand resolution\n")
    _git(repo.root, "add", "feature.txt")
    _git(repo.root, "commit", "-q", "--no-edit")
    _set_origin_main(repo, base)

    with pytest.raises(EvidenceError, match="conflicts"):
        evidence_module.reattest_v2(repo, base=base)


def test_reattest_requires_an_integrated_base_and_a_source_receipt(
    repo: GitRepository,
    tmp_path: Path,
) -> None:
    fork_point = repo.head()
    _git(repo.root, "checkout", "-q", "-b", "base-branch", fork_point)
    (repo.root / "base-only.txt").write_text("base\n")
    unmerged_base = _commit(repo.root, "unintegrated base")
    _git(repo.root, "checkout", "-q", "main")
    _set_origin_main(repo, fork_point)

    with pytest.raises(EvidenceError, match="integrate the base first"):
        evidence_module.reattest_v2(repo, base=unmerged_base)

    with pytest.raises(EvidenceError, match="no v2 receipt exists on this branch to derive from"):
        evidence_module.reattest_v2(repo, base=fork_point)


def test_reattest_refuses_dirty_state_and_non_head_target(repo: GitRepository, tmp_path: Path) -> None:
    base, _, _, _ = _integrated_branch(repo, tmp_path)
    (repo.root / "scratch.txt").write_text("dirty\n")
    with pytest.raises(EvidenceError, match="dirty"):
        evidence_module.reattest_v2(repo, base=base)
    (repo.root / "scratch.txt").unlink()

    with pytest.raises(EvidenceError, match="not the checked-out HEAD"):
        evidence_module.reattest_v2(repo, base=base, target=base)


def test_reattest_never_touches_base_receipts(repo: GitRepository, tmp_path: Path) -> None:
    fork_point = repo.head()
    _git(repo.root, "checkout", "-q", "-b", "base-branch", fork_point)
    (repo.root / "base-only.txt").write_text("base\n")
    base_reviewed = _commit(repo.root, "base advances")
    base_receipt_path, _ = _add_receipt(repo, reviewed_commit=base_reviewed)
    base = repo.head()
    _git(repo.root, "checkout", "-q", "main")

    (repo.root / "feature.txt").write_text("feature\n")
    _commit(repo.root, "feature work")
    _, old_path = _emit_and_commit(repo, tmp_path)
    merge = _git(repo.root, "merge", "--no-edit", base, check=False)
    assert merge.returncode == 0, merge.stderr.decode()
    _set_origin_main(repo, base)

    path, created, superseded = evidence_module.reattest_v2(repo, base=base)
    assert created
    assert superseded == (old_path,)
    # The base's own receipt is present on origin/main, so retirement leaves it —
    # only the branch's pre-integration receipt is retired.
    assert (repo.root / base_receipt_path).is_file()

    target = _commit(repo.root, "reattest receipt swap")
    result = verify_v2(repo, target=target, base=base, mode="pr")
    assert result.matching_receipts == (path.as_posix(),)


def test_reattest_refuses_an_untrusted_base_not_landed_on_origin_main(repo: GitRepository, tmp_path: Path) -> None:
    # The witness base must be landed content. An unmerged/untrusted base could
    # smuggle unreviewed content through the sound-looking three-way merge; both
    # the original --base HEAD exploit and a divergent feature-branch base are
    # refused because neither is contained in origin/main.
    fork_point = repo.head()
    (repo.root / "feature.txt").write_text("feature\n")
    reviewed = _commit(repo.root, "reviewed feature")
    _emit_and_commit(repo, tmp_path)
    _git(repo.root, "checkout", "-q", "-b", "evil", fork_point)
    (repo.root / "backdoor.txt").write_text("backdoor\n")
    evil = _commit(repo.root, "unreviewed backdoor")
    _git(repo.root, "checkout", "-q", "main")
    merge = _git(repo.root, "merge", "--no-edit", evil, check=False)
    assert merge.returncode == 0, merge.stderr.decode()
    _set_origin_main(repo, fork_point)  # honest main: no backdoor, evil not on it

    for bad_base in (repo.head(), reviewed, evil):
        with pytest.raises(EvidenceError, match="is not landed content in 'origin/main'"):
            evidence_module.reattest_v2(repo, base=bad_base)


def test_reattest_refuses_when_origin_main_is_unresolvable_up_front(repo: GitRepository, tmp_path: Path) -> None:
    base, _, _, _ = _integrated_branch(repo, tmp_path)
    _git(repo.root, "update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(EvidenceError, match="'origin/main' does not resolve"):
        evidence_module.reattest_v2(repo, base=base)


def test_reattest_deletion_is_anchored_to_origin_main_not_the_base_arg(
    repo: GitRepository,
    tmp_path: Path,
) -> None:
    # A stale/wrong --base must never delete a receipt that has landed on main.
    # main advances B0 -> B1 where B0..B1 only touches the receipt namespace, so a
    # witness against the stale B0 still passes; retirement must still spare B1's
    # landed receipt because it is anchored to origin/main, not to --base.
    fork_point = repo.head()
    _git(repo.root, "checkout", "-q", "-b", "base-branch", fork_point)
    (repo.root / "base-only.txt").write_text("base\n")
    b0 = _commit(repo.root, "base B0")
    landed_receipt, _ = _add_receipt(repo, reviewed_commit=b0)  # lands on B1
    b1 = repo.head()
    _git(repo.root, "checkout", "-q", "main")

    (repo.root / "feature.txt").write_text("feature\n")
    _commit(repo.root, "feature work")
    _, old_path = _emit_and_commit(repo, tmp_path)
    merge = _git(repo.root, "merge", "--no-edit", b1, check=False)
    assert merge.returncode == 0, merge.stderr.decode()
    _set_origin_main(repo, b1)

    # Reattest against the stale B0 (as if origin/main had not been fetched to B1).
    _path, created, superseded = evidence_module.reattest_v2(repo, base=b0)
    assert created
    assert superseded == (old_path,)
    assert (repo.root / landed_receipt).is_file(), "a landed receipt was wrongly retired via a stale --base"


def test_reattest_cli_reports_receipt_and_superseded_paths(
    repo: GitRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base, _, old_path, _ = _integrated_branch(repo, tmp_path)
    monkeypatch.chdir(repo.root)

    assert landing_cli.main(["evidence", "reattest", "--base", base]) == 0
    output = capsys.readouterr().out
    assert "landing-evidence: wrote proof/preship-reviews/v2/" in output
    assert f"superseded {old_path.as_posix()}" in output
    assert "commit the proof/preship-reviews/v2/ changes" in output


def test_tree_content_digest_matches_snapshot_profile(repo: GitRepository) -> None:
    (repo.root / "nested").mkdir()
    (repo.root / "nested" / "päth münz.txt").write_text("odd\n")
    _commit(repo.root, "odd paths")
    _add_receipt(repo)
    # HA Green receipts are excluded from the content profile; the witness digest
    # must ignore them exactly like snapshot_tree, or every real-repo reattest
    # would fail the moment an HA receipt lands in the tree.
    _add_valid_ha_receipt(repo, run_index=2)

    tree_oid = _git(repo.root, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
    snapshot = snapshot_tree(repo, "HEAD")
    assert snapshot.has_ha_green_receipts
    assert evidence_module._tree_content_digest(repo, tree_oid) == snapshot.content_sha256


def _add_valid_ha_receipt(repo: GitRepository, *, run_index: int) -> None:
    """Write a validator-clean HA Green receipt for the current tree state."""

    config = repo.root / "ha-addon/mammamiradio/config.yaml"
    if not config.exists():
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("version: 3.4.5\n")
        _commit(repo.root, "release config")
    validator = evidence_module._ha_release_validator()
    _, ha_digest = validator._tracked_content_sha256(repo.root, "HEAD")
    receipt_root = repo.root / HA_RECEIPT_ROOT
    receipt_root.mkdir(parents=True, exist_ok=True)
    run_id = f"{run_index:08x}-0000-4000-8000-{run_index:012x}"
    (receipt_root / f"run-{run_id}.json").write_bytes(_ha_receipt_bytes(run_id=run_id, content_digest=ha_digest))
    _commit(repo.root, "HA Green receipt")


def test_reattest_survives_ha_green_receipts_arriving_with_the_base(
    repo: GitRepository,
    tmp_path: Path,
) -> None:
    fork_point = repo.head()
    (repo.root / "feature.txt").write_text("feature\n")
    _commit(repo.root, "feature work")
    _, old_path = _emit_and_commit(repo, tmp_path)

    _git(repo.root, "checkout", "-q", "-b", "base-branch", fork_point)
    _add_valid_ha_receipt(repo, run_index=1)
    base = repo.head()
    _git(repo.root, "checkout", "-q", "main")
    merge = _git(repo.root, "merge", "--no-edit", base, check=False)
    assert merge.returncode == 0, merge.stderr.decode()
    _set_origin_main(repo, base)

    path, created, superseded = evidence_module.reattest_v2(repo, base=base)
    assert created
    assert superseded == (old_path,)

    target = _commit(repo.root, "reattest with HA receipts from base")
    result = verify_v2(repo, target=target, base=base, mode="pr")
    assert result.matching_receipts == (path.as_posix(),)


def test_merge_tree_commits_returns_tree_or_none(repo: GitRepository) -> None:
    fork_point = repo.head()
    (repo.root / "a.txt").write_text("a\n")
    ours = _commit(repo.root, "ours")
    _git(repo.root, "checkout", "-q", "-b", "theirs-branch", fork_point)
    (repo.root / "b.txt").write_text("b\n")
    theirs = _commit(repo.root, "theirs")

    clean = repo.merge_tree_commits(ours, theirs)
    assert clean is not None and len(clean) == repo.oid_length
    merged_paths = {entry.path for entry in repo.tree_entries_for_tree(clean, max_record_bytes=MAX_TREE_RECORD_BYTES)}
    assert b"a.txt" in merged_paths and b"b.txt" in merged_paths

    _git(repo.root, "checkout", "-q", "-b", "conflict-branch", fork_point)
    (repo.root / "a.txt").write_text("different\n")
    conflicting = _commit(repo.root, "conflicting")
    assert repo.merge_tree_commits(ours, conflicting) is None


def test_resolve_tree_and_tree_entries_for_tree_reject_malformed_input(repo: GitRepository) -> None:
    resolved = repo.resolve_tree("HEAD")
    assert len(resolved) == repo.oid_length

    with pytest.raises(GitError, match="does not resolve"):
        repo.resolve_tree("no-such-ref")
    with pytest.raises(GitError, match="empty tree reference"):
        repo.resolve_tree("")
    with pytest.raises(GitError, match="malformed tree object ID"):
        list(repo.tree_entries_for_tree("zz", max_record_bytes=MAX_TREE_RECORD_BYTES))


def test_reattest_reports_each_disqualified_candidate(repo: GitRepository) -> None:
    fork_point = repo.head()
    stale_digest = snapshot_tree(repo, fork_point).content_sha256
    # A divergent base so the content-mismatch candidate reaches the content check
    # instead of being pre-empted by the ancestral-base (vacuous-witness) guard.
    _git(repo.root, "checkout", "-q", "-b", "side-branch", fork_point)
    (repo.root / "side.txt").write_text("side\n")
    side_commit = _commit(repo.root, "side work")
    _git(repo.root, "checkout", "-q", "-b", "diverged-base", fork_point)
    (repo.root / "base-only.txt").write_text("base\n")
    diverged_base = _commit(repo.root, "diverged base")
    _git(repo.root, "checkout", "-q", "main")
    (repo.root / "feature.txt").write_text("feature\n")
    feature_head = _commit(repo.root, "feature work")
    merge = _git(repo.root, "merge", "--no-edit", diverged_base, check=False)
    assert merge.returncode == 0, merge.stderr.decode()
    _set_origin_main(repo, diverged_base)

    _add_receipt(
        repo,
        reviewed_commit="f" * 40,
        content_digest=stale_digest,
        source_digest=hashlib.sha256(b"unavailable").hexdigest(),
        commit=False,
    )
    _add_receipt(
        repo,
        reviewed_commit=side_commit,
        content_digest=stale_digest,
        source_digest=hashlib.sha256(b"foreign").hexdigest(),
        commit=False,
    )
    _add_receipt(
        repo,
        reviewed_commit=feature_head,
        content_digest=stale_digest,
        source_digest=hashlib.sha256(b"mismatch").hexdigest(),
    )

    with pytest.raises(EvidenceError) as failure:
        evidence_module.reattest_v2(repo, base=diverged_base)
    message = str(failure.value)
    assert "pins an unavailable reviewed commit" in message
    assert "pins a reviewed commit outside this branch's history" in message
    assert "does not match its reviewed commit's content" in message


def test_reattest_retires_multiple_superseded_receipts_sharing_a_directory(
    repo: GitRepository,
) -> None:
    fork_point = repo.head()
    (repo.root / "feature.txt").write_text("feature\n")
    feature_head = _commit(repo.root, "feature work")
    reviewed_digest = snapshot_tree(repo, feature_head).content_sha256
    first_old, _ = _add_receipt(
        repo,
        reviewed_commit=feature_head,
        content_digest=reviewed_digest,
        source_digest=hashlib.sha256(b"first").hexdigest(),
        commit=False,
    )
    second_old, _ = _add_receipt(
        repo,
        reviewed_commit=feature_head,
        content_digest=reviewed_digest,
        source_digest=hashlib.sha256(b"second").hexdigest(),
    )

    _git(repo.root, "checkout", "-q", "-b", "base-branch", fork_point)
    (repo.root / "base-only.txt").write_text("base\n")
    base = _commit(repo.root, "base advances")
    _git(repo.root, "checkout", "-q", "main")
    merge = _git(repo.root, "merge", "--no-edit", base, check=False)
    assert merge.returncode == 0, merge.stderr.decode()
    _set_origin_main(repo, base)

    path, created, superseded = evidence_module.reattest_v2(repo, base=base)
    assert created
    assert set(superseded) == {first_old, second_old}
    assert not (repo.root / first_old).exists()
    assert not (repo.root / first_old).parent.exists()
    assert (repo.root / path).is_file()


def test_reattest_raises_when_superseded_removal_fails(
    repo: GitRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, _, _, _ = _integrated_branch(repo, tmp_path)

    def refuse_unlink(self: Path, missing_ok: bool = False) -> None:
        raise OSError("synthetic unlink failure")

    monkeypatch.setattr(Path, "unlink", refuse_unlink)
    with pytest.raises(EvidenceError, match="could not be removed"):
        evidence_module.reattest_v2(repo, base=base)


def test_reattest_reuses_a_pre_created_identical_receipt(repo: GitRepository, tmp_path: Path) -> None:
    base, _, old_path, _ = _integrated_branch(repo, tmp_path)
    first_path, created, first_superseded = evidence_module.reattest_v2(repo, base=base)
    assert created and first_superseded == (old_path,)
    # Restore the retired receipt but keep the derived one untracked: the rerun
    # must accept its own byte-identical output and retire the stale copy again.
    _git(repo.root, "checkout", "--", old_path.as_posix())

    again_path, created_again, again_superseded = evidence_module.reattest_v2(repo, base=base)
    assert again_path == first_path
    assert not created_again
    assert again_superseded == (old_path,)


def test_reattest_detects_head_and_worktree_races(
    repo: GitRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, _, _, _ = _integrated_branch(repo, tmp_path)

    real_head = repo.head
    head_calls = {"count": 0}

    def racing_head() -> str:
        head_calls["count"] += 1
        if head_calls["count"] > 1:
            return "0" * repo.oid_length
        return real_head()

    monkeypatch.setattr(repo, "head", racing_head)
    with pytest.raises(EvidenceError, match="HEAD changed while evidence was being computed"):
        evidence_module.reattest_v2(repo, base=base)
    monkeypatch.undo()

    real_status = repo.status_bytes
    status_calls = {"count": 0}

    def racing_status() -> bytes:
        status_calls["count"] += 1
        if status_calls["count"] > 1:
            return b"1 .M N... 100644 100644 100644 0 0 raced.txt\0"
        return real_status()

    monkeypatch.setattr(repo, "status_bytes", racing_status)
    with pytest.raises(EvidenceError, match="working tree changed while evidence was being computed"):
        evidence_module.reattest_v2(repo, base=base)


def test_reattest_cli_is_quiet_when_already_matching(
    repo: GitRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base, _, _, _ = _integrated_branch(repo, tmp_path)
    evidence_module.reattest_v2(repo, base=base)
    _commit(repo.root, "reattest receipt swap")
    monkeypatch.chdir(repo.root)

    assert landing_cli.main(["evidence", "reattest", "--base", base]) == 0
    output = capsys.readouterr().out
    assert "already matches" in output
    assert "commit the proof/preship-reviews/v2/ changes" not in output


def test_reattest_already_matching_retires_leftover_stale_receipt(repo: GitRepository, tmp_path: Path) -> None:
    # A partial commit can land the derived receipt while leaving the stale one
    # tracked; a re-run's already-matching short-circuit must still retire it.
    base, _, old_path, _ = _integrated_branch(repo, tmp_path)
    derived_path, created, _ = evidence_module.reattest_v2(repo, base=base)
    assert created
    # Commit ONLY the new receipt by name, leaving old_path tracked (git add <file>).
    _git(repo.root, "add", derived_path.as_posix())
    _git(repo.root, "checkout", "--", old_path.as_posix())
    _git(repo.root, "commit", "-q", "-m", "partial receipt commit")
    assert (repo.root / old_path).is_file()

    chosen, created_again, superseded = evidence_module.reattest_v2(repo, base=base)
    assert not created_again
    assert chosen == derived_path
    assert superseded == (old_path,)
    assert not (repo.root / old_path).exists()


def test_reattest_already_matching_refuses_a_dirty_tree(repo: GitRepository, tmp_path: Path) -> None:
    base, _, _, _ = _integrated_branch(repo, tmp_path)
    evidence_module.reattest_v2(repo, base=base)
    _commit(repo.root, "reattest receipt swap")
    (repo.root / "scratch.txt").write_text("dirty\n")
    with pytest.raises(EvidenceError, match="dirty"):
        evidence_module.reattest_v2(repo, base=base)


def test_reattest_fails_loud_when_origin_main_is_unresolvable(repo: GitRepository, tmp_path: Path) -> None:
    # Same integration, but no origin/main ref: retirement cannot prove the stale
    # source receipt is unlanded, so reattest refuses rather than silently leaving
    # a branch CI will reject.
    fork_point = repo.head()
    (repo.root / "feature.txt").write_text("feature\n")
    _commit(repo.root, "feature work")
    _emit_and_commit(repo, tmp_path)
    _git(repo.root, "checkout", "-q", "-b", "base-branch", fork_point)
    (repo.root / "base-only.txt").write_text("base\n")
    base = _commit(repo.root, "base advances")
    _git(repo.root, "checkout", "-q", "main")
    merge = _git(repo.root, "merge", "--no-edit", base, check=False)
    assert merge.returncode == 0, merge.stderr.decode()

    with pytest.raises(EvidenceError, match="'origin/main' does not resolve"):
        evidence_module.reattest_v2(repo, base=base)


def test_reattest_already_matching_fails_loud_without_origin_main(repo: GitRepository, tmp_path: Path) -> None:
    base, _, old_path, _ = _integrated_branch(repo, tmp_path)
    derived_path, _, _ = evidence_module.reattest_v2(repo, base=base)
    _git(repo.root, "add", derived_path.as_posix())
    _git(repo.root, "checkout", "--", old_path.as_posix())
    _git(repo.root, "commit", "-q", "-m", "partial receipt commit")
    _git(repo.root, "update-ref", "-d", "refs/remotes/origin/main")

    with pytest.raises(EvidenceError, match="does not resolve"):
        evidence_module.reattest_v2(repo, base=base)


def test_retire_public_wrapper_snapshots_head_and_retires(repo: GitRepository, tmp_path: Path) -> None:
    base, _, old_path, _ = _integrated_branch(repo, tmp_path)
    derived_path, _, _ = evidence_module.reattest_v2(repo, base=base)
    _git(repo.root, "add", derived_path.as_posix())
    _git(repo.root, "checkout", "--", old_path.as_posix())
    _git(repo.root, "commit", "-q", "-m", "partial receipt commit")

    removed, blocked = evidence_module.retire_superseded_receipts(repo)
    assert removed == (old_path,)
    assert blocked == ()
    assert not (repo.root / old_path).exists()

    # Nothing stale left: a second call is a clean no-op.
    _git(repo.root, "commit", "-aqm", "record retirement")
    assert evidence_module.retire_superseded_receipts(repo) == ((), ())


def test_emit_cli_retires_stale_receipt_after_a_re_review(
    repo: GitRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The reattest-refusal recovery path: content changed, so a fresh review +
    # emit is required; the emit CLI must retire the pre-change receipt so the
    # branch verifies, instead of leaving it for a manual retire commit.
    fork_point = repo.head()
    (repo.root / "feature.txt").write_text("feature\n")
    reviewed = _commit(repo.root, "feature work")
    ledger = _write_ledger(tmp_path, _review_line(reviewed))
    old_path, _ = emit_v2(repo, ledger_root=ledger)
    _commit(repo.root, "first receipt")
    _set_origin_main(repo, fork_point)

    # Content changes; re-review the new head and emit again through the CLI.
    (repo.root / "feature.txt").write_text("revised feature\n")
    revised = _commit(repo.root, "revised feature")
    ledger.joinpath("projects/florianhorner-mammamiradio/branch-reviews.jsonl").write_bytes(_review_line(revised))
    monkeypatch.chdir(repo.root)
    monkeypatch.setenv("GSTACK_HOME", str(ledger))

    assert landing_cli.main(["evidence", "emit"]) == 0
    output = capsys.readouterr().out
    assert f"superseded {old_path.as_posix()}" in output
    assert not (repo.root / old_path).exists()


def test_emit_cli_warns_when_origin_main_is_unresolvable(
    repo: GitRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # No origin/main ref is set here, so retirement cannot confirm the stale
    # receipt is unlanded and the emit CLI warns (without failing the emit).
    (repo.root / "feature.txt").write_text("feature\n")
    reviewed = _commit(repo.root, "feature work")
    ledger = _write_ledger(tmp_path, _review_line(reviewed))
    emit_v2(repo, ledger_root=ledger)
    _commit(repo.root, "first receipt")
    (repo.root / "feature.txt").write_text("revised feature\n")
    revised = _commit(repo.root, "revised feature")
    ledger.joinpath("projects/florianhorner-mammamiradio/branch-reviews.jsonl").write_bytes(_review_line(revised))
    monkeypatch.chdir(repo.root)
    monkeypatch.setenv("GSTACK_HOME", str(ledger))

    # Emit writes the receipt but cannot retire the stale one without origin/main;
    # the ceremony is incomplete, so the CLI must fail loud (non-zero) rather than
    # report success and let automation ship a CI-failing branch.
    assert landing_cli.main(["evidence", "emit"]) == 1
    captured = capsys.readouterr()
    assert "FAIL" in captured.err
    assert "could not resolve origin/main" in captured.err


def test_path_in_commit_detects_presence_absence_and_non_ascii(repo: GitRepository) -> None:
    path, _ = _add_receipt(repo)
    head = repo.head()
    assert repo.path_in_commit(head, os.fsencode(path.as_posix())) is True
    absent = f"{RECEIPT_ROOT}/{'a' * 64}/{'b' * 64}.json".encode()
    assert repo.path_in_commit(head, absent) is False
    # A non-ASCII path can never be a validated receipt path; reported absent.
    assert repo.path_in_commit(head, b"proof/preship-reviews/v2/\xff\xfe.json") is False


def _witness_entry(path: bytes, *, mode: bytes = b"100644", kind: bytes = b"blob", raw: bytes = b"") -> TreeEntry:
    record = raw or b"%s %s %s\t%s" % (mode, kind, b"0" * 40, path)
    return TreeEntry(raw=record, mode=mode, kind=kind, oid="0" * 40, path=path)


_HA_WITNESS_PATH = b"proof/media/ha-green-release-evidence/run-00000000-0000-4000-8000-000000000000.json"
_V2_WITNESS_PATH = b"proof/preship-reviews/v2/" + b"1" * 64 + b"/" + b"2" * 64 + b".json"


@pytest.mark.parametrize(
    "case, expected",
    [
        ("duplicate", "duplicate path"),
        ("bad-namespace", "unknown entry in reserved v2 namespace"),
        ("v2-mode", "must be a non-executable regular blob"),
        ("ha-mode", "HA Green receipt .* must be a non-executable regular blob"),
        ("ha-flood", "more than 0 HA Green receipts"),
        ("entry-flood", "more than 0 ordinary entries"),
        ("byte-flood", "more than 1 ordinary bytes"),
    ],
)
def test_tree_content_digest_rejects_malformed_witness_trees(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected: str,
) -> None:
    ordinary = _witness_entry(b"seed.txt")
    if case == "duplicate":
        entries = [ordinary, _witness_entry(b"seed.txt")]
    elif case == "bad-namespace":
        entries = [_witness_entry(b"proof/preship-reviews/v2/evil")]
    elif case == "v2-mode":
        entries = [_witness_entry(_V2_WITNESS_PATH, mode=b"100755")]
    elif case == "ha-mode":
        entries = [_witness_entry(_HA_WITNESS_PATH, mode=b"120000", kind=b"blob")]
    elif case == "ha-flood":
        monkeypatch.setattr(evidence_module, "MAX_HA_RECEIPTS", 0)
        entries = [_witness_entry(_HA_WITNESS_PATH)]
    elif case == "entry-flood":
        monkeypatch.setattr(evidence_module, "MAX_TREE_ENTRIES", 0)
        entries = [ordinary]
    else:
        monkeypatch.setattr(evidence_module, "MAX_TREE_BYTES", 1)
        entries = [ordinary]

    tree_oid = repo.resolve_tree("HEAD")
    monkeypatch.setattr(
        repo,
        "tree_entries_for_tree",
        lambda oid, *, max_record_bytes: iter(entries),
    )
    with pytest.raises((EvidenceError, GitError), match=expected):
        evidence_module._tree_content_digest(repo, tree_oid)


@pytest.mark.parametrize(
    "stdout, expected",
    [
        (b"aaa\nbbb\n", "did not resolve to exactly one object ID"),
        (b"\xff" * 40 + b"\n", "non-ASCII object ID"),
        (b"abc\n", "malformed object ID"),
    ],
)
def test_resolve_tree_rejects_bad_plumbing(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
    stdout: bytes,
    expected: str,
) -> None:
    monkeypatch.setattr(repo, "run_result", lambda args, **kwargs: _completed(stdout=stdout))
    with pytest.raises(GitError, match=expected):
        repo.resolve_tree("HEAD")


@pytest.mark.parametrize(
    "returncode, stdout, expected",
    [
        (2, b"", "requires git >= 2.38"),
        (0, b"", "returned no tree object ID"),
        (0, b"\xff" * 40 + b"\n", "non-ASCII object ID"),
        (0, b"abc\n", "malformed object ID"),
    ],
)
def test_merge_tree_commits_rejects_bad_plumbing(
    repo: GitRepository,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: bytes,
    expected: str,
) -> None:
    head = repo.head()
    real_run_result = repo.run_result
    seen_argv: list[tuple[str, ...]] = []

    def dispatch(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        args_tuple = tuple(args)
        if args_tuple and args_tuple[0] == "merge-tree":
            seen_argv.append(args_tuple)
            return _completed(returncode=returncode, stdout=stdout, stderr=b"boom")
        return real_run_result(args_tuple, **kwargs)

    monkeypatch.setattr(repo, "run_result", dispatch)
    with pytest.raises(GitError, match=expected):
        repo.merge_tree_commits(head, head)
    # Pin the exact invocation: the flags are load-bearing (--write-tree makes a
    # pre-2.38 git parse it as a tree-ish, --no-messages suppresses conflict text).
    assert seen_argv == [("merge-tree", "--write-tree", "--no-messages", head, head)]


def test_merge_tree_commits_selection_is_deterministic_across_equal_candidates(repo: GitRepository) -> None:
    # Two receipts bind the same head content; _matching_receipts / the reattest
    # short-circuit must pick the lexicographically smallest path, deterministically.
    base = repo.head()
    content = snapshot_tree(repo, "HEAD").content_sha256
    first, _ = _add_receipt(
        repo,
        reviewed_commit=base,
        content_digest=content,
        source_digest=hashlib.sha256(b"alpha").hexdigest(),
        commit=False,
    )
    second, _ = _add_receipt(
        repo,
        reviewed_commit=base,
        content_digest=content,
        source_digest=hashlib.sha256(b"omega").hexdigest(),
    )
    _set_origin_main(repo, base)
    chosen, created, _ = evidence_module.reattest_v2(repo, base=base)
    assert not created
    assert chosen == min(first, second, key=lambda p: os.fsencode(p.as_posix()))
