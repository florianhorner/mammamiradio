"""Guards for the Quality workflow permission boundary and lane split."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "quality.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text()


def _job_block(text: str, job_name: str) -> str:
    match = re.search(rf"\n  {re.escape(job_name)}:\n((?:    .+\n|\n)*)", text)
    assert match, f"Could not locate `{job_name}:` job in quality.yml"
    return match.group(1)


def _workflow() -> dict:
    document = yaml.safe_load(_workflow_text())
    assert isinstance(document, dict)
    return document


def test_quality_workflow_cancels_superseded_pr_runs_only() -> None:
    concurrency = _workflow()["concurrency"]
    assert concurrency["group"] == "${{ github.workflow }}-${{ github.event.pull_request.number || github.sha }}"
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_quality_workflow_pr_job_is_read_only() -> None:
    text = _workflow_text()
    top_permissions = re.search(r"^permissions:\n((?:  .+\n)*)", text, re.MULTILINE)
    assert top_permissions and "contents: read" in top_permissions.group(1)

    quality_block = _job_block(text, "quality")
    assert "contents: read" in quality_block
    assert "contents: write" not in quality_block
    assert "python scripts/coverage-ratchet.py check" not in quality_block


def test_quality_aggregator_requires_every_lane() -> None:
    quality = _workflow()["jobs"]["quality"]
    assert quality["if"] == "${{ always() && !cancelled() }}"
    assert quality["needs"] == ["changes", "lint", "types", "tests", "invariants", "browser-smoke", "media-report"]
    assert quality["timeout-minutes"] == 5
    assert _workflow()["jobs"]["tests"]["timeout-minutes"] == 45
    assert _workflow()["jobs"]["browser-smoke"]["timeout-minutes"] == 15


def test_quality_aggregator_accepts_skipped_optional_lanes() -> None:
    quality_block = _job_block(_workflow_text(), "quality")
    assert "require_ok browser-smoke" in quality_block
    assert "require_ok media-report" in quality_block
    assert "require_success tests" in quality_block
    assert "require_success lint" in quality_block


def test_quality_path_gates_expensive_jobs() -> None:
    jobs = _workflow()["jobs"]
    assert jobs["browser-smoke"]["if"] == "needs.changes.outputs.browser == 'true'"
    assert jobs["media-report"]["if"] == "needs.changes.outputs.media == 'true'"
    assert jobs["browser-smoke"]["needs"] == ["changes"]
    assert jobs["media-report"]["needs"] == ["changes"]
    assert jobs["invariants"]["needs"] == ["changes"]
    assert "if: needs.changes.outputs.workflows == 'true'" in _job_block(_workflow_text(), "invariants")


def test_quality_workflow_fetches_history_for_pinned_audio_provenance() -> None:
    def checkout_fetch_depth(job_name: str) -> object:
        steps = _workflow()["jobs"][job_name]["steps"]
        checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@"))
        return checkout.get("with", {}).get("fetch-depth")

    tests_block = _job_block(_workflow_text(), "tests")
    assert checkout_fetch_depth("media-report") == 0
    assert checkout_fetch_depth("invariants") == 0
    assert checkout_fetch_depth("tests") == 0
    assert "pinned in the provenance manifest" in tests_block


def test_quality_workflow_scopes_write_to_main_ratchet_job() -> None:
    text = _workflow_text()
    quality_block = _job_block(text, "quality")
    tests_block = _job_block(text, "tests")
    ratchet_block = _job_block(text, "coverage-ratchet")

    assert text.count("contents: write") == 1
    assert "contents: write" in ratchet_block
    assert "contents: write" not in quality_block
    assert "contents: write" not in tests_block
    assert "needs: quality" in ratchet_block
    assert "github.ref == 'refs/heads/main'" in ratchet_block
    assert "github.event_name == 'push'" in ratchet_block
    assert "python scripts/coverage-ratchet.py update" in ratchet_block
    assert "git add .coverage-floors.json pyproject.toml" in ratchet_block


def test_quality_workflow_passes_coverage_snapshot_by_artifact() -> None:
    text = _workflow_text()
    tests_block = _job_block(text, "tests")
    ratchet_block = _job_block(text, "coverage-ratchet")

    assert "COVERAGE_RATCHET_SNAPSHOT: coverage-ratchet-current.json" in tests_block
    assert "COVERAGE_RATCHET_XDIST: auto" in tests_block
    assert "python scripts/coverage-ratchet.py check" in tests_block
    assert re.search(r"actions/upload-artifact@", tests_block) is not None
    assert "name: coverage-ratchet-current" in tests_block
    assert re.search(r"actions/download-artifact@", ratchet_block) is not None
    assert "name: coverage-ratchet-current" in ratchet_block
    assert "COVERAGE_RATCHET_INPUT: coverage-ratchet-current.json" in ratchet_block


def test_quality_lanes_use_shared_python_setup() -> None:
    jobs = _workflow()["jobs"]
    for name in ("lint", "types", "tests", "invariants", "browser-smoke", "media-report"):
        uses = [str(step.get("uses", "")) for step in jobs[name]["steps"]]
        assert "./.github/actions/setup-python-ci" in uses, f"{name} must use the shared Python CI setup"


def test_browser_smoke_caches_playwright_browsers() -> None:
    action = (REPO_ROOT / ".github" / "actions" / "setup-python-ci" / "action.yml").read_text()
    assert "cache-playwright" in action
    assert "hashFiles('.playwright-cli-version')" in action
    assert "actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830" in action

    browser_block = _job_block(_workflow_text(), "browser-smoke")
    assert 'cache-playwright: "true"' in browser_block


def test_quality_workflow_runs_shellcheck_on_scripts() -> None:
    lint_block = _job_block(_workflow_text(), "lint")

    assert "ShellCheck scripts" in lint_block
    assert "koalaman/shellcheck@sha256:" in lint_block
    assert "v0.11.0" in lint_block
    assert "scripts/*.sh" in lint_block


def test_quality_workflow_emits_strict_media_proof_on_prs_and_main() -> None:
    media_block = _job_block(_workflow_text(), "media-report")

    assert "python scripts/media-proof.py --quick --output media-proof.json" in media_block
    assert "name: Upload strict media proof" in media_block
    assert "if: always()" in media_block
    assert "name: media-proof-quality-${{ github.sha }}" in media_block
    assert "path: media-proof.json" in media_block
    assert "if-no-files-found: error" in media_block
