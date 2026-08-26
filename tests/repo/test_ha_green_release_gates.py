from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _job(workflow: str, name: str) -> str:
    match = re.search(rf"\n  {re.escape(name)}:\n((?:    .+\n|\n)*)", workflow)
    assert match, f"missing {name} job"
    return match.group(1)


def test_ordinary_pr_keeps_one_cold_smoke_without_twenty_run_gate() -> None:
    pi_smoke = _read(".github/workflows/pi-smoke.yml")
    quality = _read(".github/workflows/quality.yml")
    makefile = _read("Makefile")

    assert "pull_request:" in pi_smoke
    assert "python scripts/ha-green-launch-smoke.py" in pi_smoke
    assert "--record-release-receipt" not in pi_smoke
    assert "validate-ha-green-release-evidence.py" not in quality
    check_line = re.search(r"(?m)^check:.*$", makefile)
    assert check_line
    assert "ha-green-release-proof" not in check_line.group(0)


def test_local_pre_release_is_actionably_strict() -> None:
    script = _read("scripts/pre-release-check.sh")
    makefile = _read("Makefile")

    assert '"$MEDIA_PYTHON" scripts/validate-ha-green-release-evidence.py' in script
    assert '--release-version "$ADDON_VER"' in script
    assert "record 20 runs" in script
    # The 20-run gate is scoped to a real release cut (version differs from
    # origin/main) or to an in-progress receipt set, so an ordinary PR that
    # merely touches a version file is not asked for Green hardware evidence.
    assert '[ "$ADDON_VER" = "$MAIN_ADDON_VER" ]' in script
    assert '[ "${HA_GREEN_RECEIPTS:-0}" -eq 0 ]' in script
    assert "physical HA Green evidence is required only on release cuts" in script
    assert re.search(r"(?m)^ha-green-release-proof:", makefile)
    assert re.search(r"(?m)^pre-release:", makefile)


def test_addon_stable_promotion_cannot_bypass_ha_green_evidence() -> None:
    workflow = _read(".github/workflows/addon-release.yml")
    preflight = _job(workflow, "pre-flight")
    promote = _job(workflow, "promote")

    validator = "python scripts/validate-ha-green-release-evidence.py"
    assert validator in preflight
    assert '--current-commit "$(git rev-parse HEAD^{commit})"' in preflight
    assert "fetch-depth: 0" in preflight
    assert "ha-green-release-proof-stable-${{ github.sha }}" in preflight
    assert "if-no-files-found: error" in preflight
    assert preflight.index(validator) < preflight.index("docker/login-action@")
    assert "needs: [pre-flight, media-proof, smoke-prebuilt]" in promote


def test_standalone_tag_publication_runs_same_gate_before_registry_write() -> None:
    workflow = _read(".github/workflows/docker.yml")
    job = _job(workflow, "build-and-push")

    validator = "python scripts/validate-ha-green-release-evidence.py"
    assert validator in job
    assert 'VERSION="${GITHUB_REF_NAME#v}"' in job
    assert "fetch-depth: 0" in job
    assert "ha-green-release-proof-standalone-${{ github.sha }}" in job
    assert job.index(validator) < job.index("docker/login-action@")
    assert job.index(validator) < job.index("push: true")


def test_receipt_schema_and_example_are_canonical_but_example_is_not_evidence() -> None:
    schema = json.loads(_read("proof/media/ha-green-release-receipt.schema.json"))
    example = json.loads(_read("proof/media/ha-green-release-receipt.example.json"))

    assert schema["properties"]["schema_version"]["const"] == 1
    assert schema["properties"]["assertions"]["additionalProperties"] is False
    assert example["evidence_kind"] == "example"
    assert example["hardware"]["model"].endswith("(example only; not release evidence)")
    assert "ha-green-release-evidence/" not in "proof/media/ha-green-release-receipt.example.json"
