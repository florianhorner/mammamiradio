"""Guards for the Quality workflow permission boundary."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "quality.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text()


def _job_block(text: str, job_name: str) -> str:
    match = re.search(rf"\n  {re.escape(job_name)}:\n((?:    .+\n|\n)*)", text)
    assert match, f"Could not locate `{job_name}:` job in quality.yml"
    return match.group(1)


def test_quality_workflow_pr_job_is_read_only() -> None:
    text = _workflow_text()
    top_permissions = re.search(r"^permissions:\n((?:  .+\n)*)", text, re.MULTILINE)
    assert top_permissions and "contents: read" in top_permissions.group(1)

    quality_block = _job_block(text, "quality")
    assert "contents: read" in quality_block
    assert "contents: write" not in quality_block
    assert "python scripts/coverage-ratchet.py check" in quality_block


def test_quality_workflow_scopes_write_to_main_ratchet_job() -> None:
    text = _workflow_text()
    quality_block = _job_block(text, "quality")
    ratchet_block = _job_block(text, "coverage-ratchet")

    assert text.count("contents: write") == 1
    assert "contents: write" in ratchet_block
    assert "contents: write" not in quality_block
    assert "needs: quality" in ratchet_block
    assert "github.ref == 'refs/heads/main'" in ratchet_block
    assert "github.event_name == 'push'" in ratchet_block
    assert "python scripts/coverage-ratchet.py update" in ratchet_block
    assert "git add .coverage-floors.json pyproject.toml" in ratchet_block


def test_quality_workflow_passes_coverage_snapshot_by_artifact() -> None:
    text = _workflow_text()
    quality_block = _job_block(text, "quality")
    ratchet_block = _job_block(text, "coverage-ratchet")

    assert "COVERAGE_RATCHET_SNAPSHOT: coverage-ratchet-current.json" in quality_block
    assert re.search(r"actions/upload-artifact@v\d+", quality_block) is not None
    assert "name: coverage-ratchet-current" in quality_block
    assert re.search(r"actions/download-artifact@v\d+", ratchet_block) is not None
    assert "name: coverage-ratchet-current" in ratchet_block
    assert "COVERAGE_RATCHET_INPUT: coverage-ratchet-current.json" in ratchet_block
