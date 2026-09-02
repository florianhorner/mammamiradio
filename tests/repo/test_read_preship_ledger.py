from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
READER = ROOT / "scripts" / "read-preship-ledger.sh"


def _run_reader(repo: Path, gstack_home: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GSTACK_HOME"] = str(gstack_home)
    return subprocess.run([str(READER)], cwd=repo, env=env, capture_output=True, text=True, check=False)


def _repo(path: Path, *, origin: str | None = None) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    if origin:
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=path, check=True)
    return path


def test_reader_returns_only_config_without_matching_ledger(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "mammamiradio", origin="https://github.com/example/mammamiradio.git")
    (tmp_path / "gstack" / "projects" / "unrelated").mkdir(parents=True)

    result = _run_reader(repo, tmp_path / "gstack")

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == "---CONFIG---\n"


def test_reader_filters_matching_ledgers_and_orders_valid_entries(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", origin="git@github.com:florianhorner/mammamiradio.git")
    projects = tmp_path / "gstack" / "projects"
    exact = projects / "mammamiradio"
    nested = projects / "florianhorner-mammamiradio" / "nested"
    unrelated = projects / "other-mammamiradio-copy"
    for directory in (exact, nested, unrelated):
        directory.mkdir(parents=True)
    (exact / "old-reviews.jsonl").write_text(
        '{"skill":"review","commit":"abcdef12","timestamp":"2026-08-27T09:00:00Z"}\n'
        '{"skill":"qa","commit":"bbbbbb","timestamp":"2026-08-27T12:00:00Z"}\n'
        '{"skill":"review","commit":"unknown","timestamp":"2026-08-27T12:00:00Z"}\n'
        "not-json\n"
    )
    (nested / "new-reviews.jsonl").write_text(
        '{"skill":"adversarial-review","commit":"ABCDEF34","timestamp":"2026-08-27T11:00:00Z"}\n'
        '["not", "an", "object"]\n'
        '{"skill":"review","commit":"abc","timestamp":"2026-08-27T12:00:00Z"}\n'
    )
    (unrelated / "ignored-reviews.jsonl").write_text(
        '{"skill":"review","commit":"dddddd","timestamp":"2026-08-27T13:00:00Z"}\n'
    )

    result = _run_reader(repo, tmp_path / "gstack")

    lines = result.stdout.splitlines()
    assert result.returncode == 0
    assert [json.loads(line) for line in lines[:-1]] == [
        {"skill": "adversarial-review", "commit": "abcdef34", "timestamp": "2026-08-27T11:00:00Z"},
        {"skill": "review", "commit": "abcdef12", "timestamp": "2026-08-27T09:00:00Z"},
    ]
    assert lines[-1] == "---CONFIG---"


def test_reader_falls_back_to_worktree_name_without_origin(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "fallback-repo")
    ledger = tmp_path / "gstack" / "projects" / "fallback-repo" / "branch-reviews.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"skill":"review","commit":"abcdef","timestamp":"2026-08-27T10:00:00Z"}\n')

    result = _run_reader(repo, tmp_path / "gstack")

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        '{"skill":"review","commit":"abcdef","timestamp":"2026-08-27T10:00:00Z"}',
        "---CONFIG---",
    ]
