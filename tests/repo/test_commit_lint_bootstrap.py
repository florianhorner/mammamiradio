"""Keep trusted policy execution separate from pull request contents."""

from __future__ import annotations

import hashlib
import json
import runpy
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TRUSTED_WORKFLOW = ".github/workflows/commit-lint-trusted.yml"
LEGACY_FILES = (
    ".github/workflows/commit-lint.yml",
    ".commitlintrc.json",
    ".config/commit-rules.json",
    ".config/commit-rules.meta.json",
)
POLICY_FILES = {TRUSTED_WORKFLOW} | {
    f".config/commit-lint/{name}" for name in ("commit-msg", "validate-pr.sh", "validate-trusted.py")
}


def test_trusted_workflow_executes_only_default_branch_policy() -> None:
    workflow = yaml.safe_load((ROOT / TRUSTED_WORKFLOW).read_text())
    # PyYAML's YAML 1.1 loader decodes the unquoted GitHub Actions key as True.
    events = workflow.get("on", workflow.get(True))
    assert events is not None
    assert set(events) == {"pull_request_target"}
    assert "edited" in events["pull_request_target"]["types"]
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert set(workflow["jobs"]) == {"commit-lint"}
    job = workflow["jobs"]["commit-lint"]
    assert "permissions" not in job
    assert job["timeout-minutes"] <= 10
    checkout, validate = job["steps"]
    action, revision = checkout["uses"].split("@")
    assert action == "actions/checkout"
    assert len(revision) == 40 and all(char in "0123456789abcdef" for char in revision)
    assert checkout["with"] == {
        "ref": "${{ github.sha }}",
        "path": "trusted-policy",
        "fetch-depth": 1,
        "persist-credentials": False,
        "submodules": False,
        "lfs": False,
    }
    assert "run" not in checkout
    assert "uses" not in validate
    assert validate["working-directory"] == "trusted-policy"
    assert validate["run"] == "python3 -I .config/commit-lint/validate-trusted.py"
    assert validate["env"] == {"GITHUB_TOKEN": "${{ github.token }}"}


def test_installation_stage_keeps_required_check_continuity() -> None:
    stage = json.loads((ROOT / ".config/commit-lint/MANIFEST.json").read_text())["stage"]
    assert stage in {"bootstrap", "active"}
    name = yaml.safe_load((ROOT / TRUSTED_WORKFLOW).read_text())["jobs"]["commit-lint"]["name"]
    if stage == "bootstrap":
        assert name == "commit-lint / trusted policy preview"
        assert all((ROOT / path).is_file() for path in LEGACY_FILES)
        legacy = yaml.safe_load((ROOT / LEGACY_FILES[0]).read_text())
        events = legacy.get("on", legacy.get(True))
        assert set(events) == {"pull_request"}
        assert {"opened", "reopened", "synchronize", "edited"} <= set(events["pull_request"]["types"])
        assert set(legacy["jobs"]) == {"commit-lint"}
        assert set(legacy["jobs"]["commit-lint"]) == {"uses"}
        caller = legacy["jobs"]["commit-lint"]["uses"]
        assert caller.startswith("florianhorner/engineering-standards/.github/workflows/commit-lint-reusable.yml@")
        revision = caller.split("@", 1)[1]
        assert len(revision) == 40 and all(char in "0123456789abcdef" for char in revision)
    else:
        assert name == "commit-lint / validate commit messages"
        assert all(not (ROOT / path).exists() for path in LEGACY_FILES)


def test_manifest_binds_the_complete_trusted_policy() -> None:
    manifest = json.loads((ROOT / ".config/commit-lint/MANIFEST.json").read_text())
    assert manifest["managed_by"] == "commit-lint-kit-v1"
    assert set(manifest["files"]) == POLICY_FILES
    source = manifest["source_revision"]
    assert len(source) == 40 and all(char in "0123456789abcdef" for char in source)
    for path, digest in manifest["files"].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path


def test_generated_policy_style_exceptions_stay_narrow() -> None:
    config = tomllib.loads((ROOT / ".config/commit-lint/ruff.toml").read_text())
    assert config == {
        "extend": "../../pyproject.toml",
        "lint": {"extend-per-file-ignores": {"validate-trusted.py": ["I001", "N818"]}},
        "format": {"exclude": ["validate-trusted.py"]},
    }


@pytest.mark.parametrize(
    ("filename", "source", "expected_rule"),
    [
        ("validate-trusted.py", "print(missing_name)\n", "F821"),
        ("other.py", "class Unusable(Exception):\n    pass\n", "N818"),
    ],
)
def test_generated_policy_exceptions_preserve_other_lint_rules(filename: str, source: str, expected_rule: str) -> None:
    command = [sys.executable, "-m", "ruff", "check", "--no-cache", "--output-format", "json"]
    result = subprocess.run(
        [*command, "--stdin-filename", str(ROOT / ".config/commit-lint" / filename), "-"],
        input=source,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert expected_rule in {diagnostic["code"] for diagnostic in json.loads(result.stdout)}


def test_trusted_policy_reports_non_exempt_bot_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    controller = runpy.run_path(str(ROOT / ".config/commit-lint/validate-trusted.py"))
    trusted = tmp_path / "trusted-policy"
    subprocess.check_output(["git", "clone", "--shared", "--no-checkout", "-q", str(ROOT), str(trusted)])
    for name in controller["POLICY_FILES"]:
        target = trusted / ".config/commit-lint" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / ".config/commit-lint" / name).read_bytes())
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Policy fixture")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "fixture@example.com")
    git = ["git", "-C", str(trusted), "-c", "core.hooksPath=/dev/null"]
    subprocess.check_output([*git, "add", ".config"])
    subprocess.check_output([*git, "commit", "--no-gpg-sign", "-qm", "test: establish trusted policy"])
    head = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    main = runpy.run_path(str(trusted / ".config/commit-lint/validate-trusted.py"))["main"]
    pull = {"number": 1, "state": "open", "title": "fix: " + "x" * 80 + ".", "user": {"login": "dependabot[bot]"}}
    pull.update(head={"sha": head}, base={"sha": head, "repo": {"full_name": "example/policy"}})
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"number": 1, "repository": {"full_name": "example/policy"}, "pull_request": pull}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/policy")
    monkeypatch.setenv("GITHUB_SHA", head)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setitem(main.__globals__, "read_pull", lambda *_: pull)

    def fetch(_repo, _number, _base, bare, env, _token):
        command = ["git", "fetch", "--depth=1", str(trusted), f"{head}:refs/heads/pull"]
        subprocess.check_output(command, cwd=bare, env=env)

    monkeypatch.setitem(main.__globals__, "fetch_objects", fetch)
    assert main() == 1
    output = capsys.readouterr().out
    assert "| PR title | SUBJECT_TOO_LONG,SUBJECT_TRAILING_PERIOD |" in output
    assert "bash .config/commit-lint/commit-msg <message-file>" in output
    assert str(pull["title"]) not in output
    assert controller["failure_rows"](b"| PR title | UNKNOWN |\n") == [("PR title", ("UNKNOWN",))]
