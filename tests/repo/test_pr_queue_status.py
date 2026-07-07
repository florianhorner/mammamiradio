from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PR_QUEUE_STATUS = ROOT / "scripts" / "pr-queue-status.sh"


def _run(cmd: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _init_repo(path: Path) -> None:
    _run(["git", "init", "-q"], cwd=path)
    _run(["git", "config", "user.email", "tests@example.com"], cwd=path)
    _run(["git", "config", "user.name", "Test User"], cwd=path)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=path)
    _run(["git", "config", "core.hooksPath", "/dev/null"], cwd=path)
    _write(path / "README.md", "test repo\n")
    _run(["git", "add", "README.md"], cwd=path)
    _run(["git", "commit", "-qm", "init"], cwd=path)
    _run(["git", "branch", "-M", "main"], cwd=path)
    _run(["git", "remote", "add", "origin", str(path / "unreachable-origin.git")], cwd=path)
    _run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=path)


def _fake_gh(bin_dir: Path, prs: list[dict[str, object]]) -> None:
    payload = json.dumps(prs)
    script = bin_dir / "gh"
    _write(
        script,
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "pr" ] && [ "$2" = "list" ]; then\n'
        "  cat <<'JSON'\n"
        f"{payload}\n"
        "JSON\n"
        "  exit 0\n"
        "fi\n"
        'echo "unexpected gh call: $*" >&2\n'
        "exit 2\n",
    )
    script.chmod(0o755)


def _env_with_fake_gh(tmp_path: Path, prs: list[dict[str, object]]) -> dict[str, str]:
    bin_dir = tmp_path.parent / f"{tmp_path.name}-bin"
    bin_dir.mkdir()
    _fake_gh(bin_dir, prs)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    return env


def _env_with_raw_fake_gh(tmp_path: Path, script_body: str) -> dict[str, str]:
    bin_dir = tmp_path.parent / f"{tmp_path.name}-bin"
    bin_dir.mkdir()
    _write(bin_dir / "gh", f"#!/usr/bin/env bash\n{script_body}\n")
    (bin_dir / "gh").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    return env


def test_pr_queue_status_reports_empty_open_queue(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    env = _env_with_fake_gh(tmp_path, [])

    result = _run(["bash", str(PR_QUEUE_STATUS)], cwd=tmp_path, env=env)

    assert result.returncode == 0
    assert "pr-queue-status: open PRs: 0" in result.stdout
    assert "advisory only" in result.stdout


def test_pr_queue_status_marks_clean_and_dirty_worktrees(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _run(["git", "checkout", "-qb", "feature-clean"], cwd=tmp_path)
    dirty_worktree = tmp_path.parent / f"{tmp_path.name}-dirty"
    _run(["git", "worktree", "add", "-q", "-b", "feature-dirty", str(dirty_worktree), "main"], cwd=tmp_path)
    _write(dirty_worktree / "scratch.txt", "uncommitted\n")

    env = _env_with_fake_gh(
        tmp_path,
        [
            {
                "number": 10,
                "title": "clean branch",
                "headRefName": "feature-clean",
                "headRefOid": "aaaaaaaaaaaa0000000000000000000000000000",
                "mergeStateStatus": "CLEAN",
                "isDraft": False,
                "updatedAt": "2026-07-07T00:00:00Z",
                "url": "https://example.test/pr/10",
            },
            {
                "number": 11,
                "title": "dirty branch",
                "headRefName": "feature-dirty",
                "headRefOid": "bbbbbbbbbbbb0000000000000000000000000000",
                "mergeStateStatus": "BEHIND",
                "isDraft": False,
                "updatedAt": "2026-07-07T00:01:00Z",
                "url": "https://example.test/pr/11",
            },
        ],
    )

    result = _run(["bash", str(PR_QUEUE_STATUS)], cwd=tmp_path, env=env)

    assert result.returncode == 0
    assert "PR #10: clean branch" in result.stdout
    assert "recommendation: land now" in result.stdout
    assert "PR #11: dirty branch" in result.stdout
    assert "local: dirty (1 file(s): ?? scratch.txt); contains local origin/main" in result.stdout
    assert "recommendation: commit dirty work" in result.stdout


def test_pr_queue_status_resolves_renamed_local_branch_via_upstream(tmp_path: Path) -> None:
    """Conductor workspaces commonly rename the local branch (e.g. to the
    workspace name) while still pushing/tracking the PR's real head branch.
    worktree_for_branch() must fall back to matching on upstream tracking
    when the local branch name doesn't equal headRefName."""
    _init_repo(tmp_path)
    workspace = tmp_path.parent / f"{tmp_path.name}-workspace"
    _run(["git", "worktree", "add", "-q", "-b", "cambridge-v1", str(workspace), "main"], cwd=tmp_path)
    _run(
        ["git", "update-ref", "refs/remotes/origin/florianhorner/some-feature", "HEAD"],
        cwd=workspace,
    )
    _run(
        ["git", "branch", "--set-upstream-to=origin/florianhorner/some-feature", "cambridge-v1"],
        cwd=workspace,
    )

    env = _env_with_fake_gh(
        tmp_path,
        [
            {
                "number": 13,
                "title": "renamed workspace branch",
                "headRefName": "florianhorner/some-feature",
                "headRefOid": "dddddddddddd0000000000000000000000000000",
                "mergeStateStatus": "CLEAN",
                "isDraft": False,
                "updatedAt": "2026-07-07T00:03:00Z",
                "url": "https://example.test/pr/13",
            }
        ],
    )

    result = _run(["bash", str(PR_QUEUE_STATUS)], cwd=tmp_path, env=env)

    assert result.returncode == 0
    assert f"worktree: {workspace}" in result.stdout
    assert "local: clean; contains local origin/main" in result.stdout
    assert "recommendation: land now" in result.stdout


def test_pr_queue_status_reports_missing_local_worktree_as_advisory(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    env = _env_with_fake_gh(
        tmp_path,
        [
            {
                "number": 12,
                "title": "remote only",
                "headRefName": "feature-remote-only",
                "headRefOid": "cccccccccccc0000000000000000000000000000",
                "mergeStateStatus": "CLEAN",
                "isDraft": False,
                "updatedAt": "2026-07-07T00:02:00Z",
                "url": "https://example.test/pr/12",
            }
        ],
    )

    result = _run(["bash", str(PR_QUEUE_STATUS)], cwd=tmp_path, env=env)

    assert result.returncode == 0
    assert "worktree: not found" in result.stdout
    assert "recommendation: inspect/no local worktree" in result.stdout


def test_pr_queue_status_dies_when_gh_pr_list_fails(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    env = _env_with_raw_fake_gh(
        tmp_path,
        'if [ "$1" = "pr" ] && [ "$2" = "list" ]; then\n'
        '  echo "gh: authentication required" >&2\n'
        "  exit 1\n"
        "fi\n"
        'echo "unexpected gh call: $*" >&2\n'
        "exit 2\n",
    )

    result = _run(["bash", str(PR_QUEUE_STATUS)], cwd=tmp_path, env=env)

    assert result.returncode == 1
    assert "could not list open PRs" in result.stderr


def test_pr_queue_status_dies_on_malformed_pr_json(tmp_path: Path) -> None:
    # A JSON *object* with keys (not an array) has nonzero `jq 'length'`, so
    # it reaches the `type == "array"` guard rather than short-circuiting on
    # the `count -eq 0` empty-queue path (which a bare `{}` would, since an
    # empty object also has length 0 — that shape isn't a realistic `gh pr
    # list` failure mode and doesn't exercise this guard).
    _init_repo(tmp_path)
    env = _env_with_raw_fake_gh(
        tmp_path,
        'if [ "$1" = "pr" ] && [ "$2" = "list" ]; then\n'
        '  echo \'{"error": "rate limited"}\'\n'
        "  exit 0\n"
        "fi\n"
        'echo "unexpected gh call: $*" >&2\n'
        "exit 2\n",
    )

    result = _run(["bash", str(PR_QUEUE_STATUS)], cwd=tmp_path, env=env)

    assert result.returncode == 1
    assert "gh returned invalid PR JSON" in result.stderr


def test_pr_queue_status_reports_local_origin_main_unavailable(tmp_path: Path) -> None:
    """local_base_summary()'s branch for a worktree with no local
    refs/remotes/origin/main ref (e.g. it was fetched before that ref
    existed, or the ref was pruned)."""
    _init_repo(tmp_path)
    _run(["git", "checkout", "-qb", "feature-no-origin-ref"], cwd=tmp_path)
    _run(["git", "update-ref", "-d", "refs/remotes/origin/main"], cwd=tmp_path)

    env = _env_with_fake_gh(
        tmp_path,
        [
            {
                "number": 14,
                "title": "no origin/main ref locally",
                "headRefName": "feature-no-origin-ref",
                "headRefOid": "eeeeeeeeeeee0000000000000000000000000000",
                "mergeStateStatus": "CLEAN",
                "isDraft": False,
                "updatedAt": "2026-07-07T00:04:00Z",
                "url": "https://example.test/pr/14",
            }
        ],
    )

    result = _run(["bash", str(PR_QUEUE_STATUS)], cwd=tmp_path, env=env)

    assert result.returncode == 0
    assert "local: clean; local origin/main unavailable" in result.stdout


def test_pr_queue_status_recommendation_covers_draft_conflict_and_checks_pending(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _run(["git", "checkout", "-qb", "feature-checks-pending"], cwd=tmp_path)
    checks_pending_worktree = tmp_path.parent / f"{tmp_path.name}-checks-pending"
    _run(
        ["git", "worktree", "add", "-q", "-b", "feature-checks-pending-wt", str(checks_pending_worktree), "main"],
        cwd=tmp_path,
    )

    env = _env_with_fake_gh(
        tmp_path,
        [
            {
                "number": 20,
                "title": "draft PR",
                "headRefName": "feature-draft",
                "headRefOid": "1111aaaaaaaa0000000000000000000000000000",
                "mergeStateStatus": "CLEAN",
                "isDraft": True,
                "updatedAt": "2026-07-07T00:05:00Z",
                "url": "https://example.test/pr/20",
            },
            {
                "number": 21,
                "title": "conflicting PR",
                "headRefName": "feature-conflict",
                "headRefOid": "2222bbbbbbbb0000000000000000000000000000",
                "mergeStateStatus": "DIRTY",
                "isDraft": False,
                "updatedAt": "2026-07-07T00:06:00Z",
                "url": "https://example.test/pr/21",
            },
            {
                "number": 22,
                "title": "checks pending, no local worktree",
                "headRefName": "feature-checks-pending-no-wt",
                "headRefOid": "3333cccccccc0000000000000000000000000000",
                "mergeStateStatus": "BLOCKED",
                "isDraft": False,
                "updatedAt": "2026-07-07T00:07:00Z",
                "url": "https://example.test/pr/22",
            },
            {
                "number": 23,
                "title": "checks pending, worktree mapped",
                "headRefName": "feature-checks-pending-wt",
                "headRefOid": "4444dddddddd0000000000000000000000000000",
                "mergeStateStatus": "UNSTABLE",
                "isDraft": False,
                "updatedAt": "2026-07-07T00:08:00Z",
                "url": "https://example.test/pr/23",
            },
        ],
    )

    result = _run(["bash", str(PR_QUEUE_STATUS)], cwd=tmp_path, env=env)

    assert result.returncode == 0
    assert "PR #20: draft PR" in result.stdout
    assert "recommendation: draft" in result.stdout
    assert "PR #21: conflicting PR" in result.stdout
    assert "recommendation: conflict/manual" in result.stdout
    # No local worktree wins over "checks pending" — without a worktree the
    # actionable next step is the same regardless of merge state, so this PR
    # falls into the same bucket as any other unmapped PR.
    assert "PR #22: checks pending, no local worktree" in result.stdout
    assert "recommendation: inspect/no local worktree" in result.stdout
    # Only reachable when a local worktree IS mapped and clean/current: this
    # is the actual "wait/checks" precondition.
    assert "PR #23: checks pending, worktree mapped" in result.stdout
    assert "recommendation: wait/checks" in result.stdout
