"""Guards for the Explainer Pages publish boundary.

The deploy job holds ``pages: write`` and ``id-token: write`` and is reachable
by ``workflow_dispatch``, which accepts any ref. A one-line ``if:`` is all that
keeps a dispatch from a branch off the public site, and a workflow is not run
by its own pull request, so nothing else would notice the line disappearing.

These live in ``tests/repo/`` rather than the node suite on purpose: quality.yml
has no path filter, so they run on every pull request, including one that
touches only the workflow.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PAGES_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "explainer-pages.yml"
TESTS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "explainer.yml"
OPERATIONS_DOC = REPO_ROOT / "docs" / "operations.md"

MAIN_ONLY = "github.ref == 'refs/heads/main'"


def _job_block(text: str, job_name: str) -> str:
    match = re.search(rf"\n  {re.escape(job_name)}:\n((?:    .+\n|\n)*)", text)
    assert match, f"Could not locate `{job_name}:` job in explainer-pages.yml"
    return match.group(1)


def test_only_the_deploy_job_is_scoped_to_main() -> None:
    text = PAGES_WORKFLOW.read_text()
    deploy = _job_block(text, "deploy")
    build = _job_block(text, "build")

    assert f"if: {MAIN_ONLY}" in deploy, "the deploy job must refuse to publish from a non-main ref"
    assert "github.ref" not in build, "build stays unguarded so a dispatch elsewhere still tests"
    assert "needs: build" in deploy, "publishing an untested artifact is the failure this prevents"


def test_publish_permissions_stay_on_the_deploy_job() -> None:
    document = yaml.safe_load(PAGES_WORKFLOW.read_text())
    jobs = document["jobs"]

    assert jobs["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
    assert "permissions" not in jobs["build"], "build inherits the read-only top-level scope"
    assert document["permissions"] == {"contents": "read"}


def test_the_publish_workflow_triggers_on_its_own_path() -> None:
    # Without this the guard above can be deleted by a commit that runs no CI
    # at all: the push filter would not match, and the file has no
    # pull_request trigger.
    document = yaml.safe_load(PAGES_WORKFLOW.read_text())
    paths = document[True]["push"]["paths"]  # `on:` parses as the boolean True
    assert ".github/workflows/explainer-pages.yml" in paths
    assert "docs/explainer/**" in paths


def test_changing_the_publish_workflow_runs_the_explainer_suite() -> None:
    document = yaml.safe_load(TESTS_WORKFLOW.read_text())
    triggers = document[True]
    for event in ("push", "pull_request"):
        assert ".github/workflows/explainer-pages.yml" in triggers[event]["paths"], (
            f"explainer.yml must run on {event} for the publish workflow, "
            "or a change to the publish path ships untested"
        )


def test_operations_doc_states_the_gate_the_workflow_carries() -> None:
    # docs/operations.md tells an operator the deploy is main-only. If the
    # workflow stops saying it, the doc becomes a lie with nothing to catch it.
    doc = OPERATIONS_DOC.read_text()
    assert MAIN_ONLY in doc, "operations.md must quote the guard it claims exists"
    assert MAIN_ONLY in PAGES_WORKFLOW.read_text()
