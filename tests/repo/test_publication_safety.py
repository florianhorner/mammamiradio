"""Public examples and publication text must not copy installation identity."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _docs_check(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(ROOT / "scripts/check-docs-safety.sh"), str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "body",
    [
        "Preship V2 comes before HA runs.",
        "Finalize version, changelogs, and the V2 preship receipt\nbefore recording.",
        "Refresh the V2 review receipt before landing.",
        "Commit the V2 review receipt before landing.",
        "Run scripts/emit-review-evidence.sh before recording.",
        "Run `scripts/emit-review-evidence.sh` before recording.",
        "1. Run `scripts/emit-review-evidence.sh` before recording.",
        "The V2 review-receipt is required before release.",
        "A V2 preship receipt is required before release.",
        "V2 admission is retired; however, restore the V2 receipt before release.",
        "V2 review receipts aren't required; however, refresh the V2 review receipt before release.",
        "You must no longer refresh V2 review receipts, create a V2 review receipt instead.",
        "V2 review receipts must no longer be refreshed, but a V2 review receipt is required before release.",
        "You must no longer refresh V2 review receipts and must create a V2 review receipt before release.",
        "You must no longer refresh V2 review receipts and a new V2 review receipt is required before release.",
        "You must no longer refresh V2 review receipts and a new V2 review receipt is still required before release.",
        "You must no longer refresh V2 review receipts and should create a V2 review receipt before release.",
        "| Gate | Requirement |\n|---|---|\n| V2 preship receipt | Required before HA runs |",
        "```bash\nscripts/emit-review-evidence.sh --reattest\n```",
    ],
)
def test_release_docs_reject_retired_receipt_requirements(tmp_path: Path, body: str) -> None:
    guide = tmp_path / "release.md"
    guide.write_text(body)
    result = _docs_check(guide)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "retired review-receipt requirement" in result.stdout


@pytest.mark.parametrize(
    "body",
    [
        "Historical V2 preship receipts remain readable with the standalone verifier.",
        "Historical V2 preship receipts name the reviewed commit.",
        "V2 preship receipts were required before recording.",
        "V2 preship receipts are no longer required for release.",
        "V2 review receipts must no longer be refreshed before release.",
        "You must no longer refresh V2 review receipts before release.",
        "V2 review receipts must no longer be required before release.",
        "V2 review receipts should no longer be required before release.",
        "V2 review receipts mustn't be refreshed before release.",
        "You must no longer refresh V2 review receipts and must not create a V2 review receipt.",
        "Do not create V2 review receipts or treat them as required for release.",
        "Do not create V2 review receipts or claim they are required for release.",
        "Do not create V2 review receipts or claim they must be created before release.",
        "You must no longer refresh V2 review receipts and should not create a V2 review receipt.",
        "A V2 review receipt isn't required before release.",
        "A V2 review receipt isn’t required before release.",
        "V2 review receipts aren't required before release.",
        "V2 review receipts aren’t required before release.",
        "No V2 preship receipt is required before recording.",
        "Do not emit or refresh V2 receipts before HA runs.",
        "Do not run `scripts/emit-review-evidence.sh` before HA runs.",
        "The old release required V2 preship receipts. That admission is retired.",
        "HA Green receipts are required when the physical evidence gate is armed.",
        "Save the First Listen privacy-review receipt before completing setup.",
        "Run scripts/check-preship-evidence.sh to read historical receipts.",
    ],
)
def test_release_docs_preserve_history_and_other_receipts(tmp_path: Path, body: str) -> None:
    guide = tmp_path / "release.md"
    guide.write_text(body)
    result = _docs_check(guide)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("domain", ["person", "device_tracker", "lock"])
def test_public_examples_require_synthetic_identity(tmp_path: Path, domain: str) -> None:
    guide = tmp_path / "example.md"
    canary = f"{domain}.recorded_household_value"
    guide.write_text(f"Use `{canary}` in this example.")
    result = _docs_check(guide)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "non-synthetic household identifier" in result.stdout
    assert canary not in result.stdout + result.stderr

    guide.write_text(f"Use `{domain}.example_resident` in this example.")
    result = _docs_check(guide)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("surface", ["pr", "issue", "changelog"])
@pytest.mark.parametrize(
    "body",
    [
        "<!-- conductor-workspace-link -->",
        "See [workspace](https://app.conductor.build/workspace/private-canary-123).",
        "See [workspace](https://APP.CONDUCTOR.BUILD/workspace/private-canary-123).",
        "Release notes.\n\n[proof]: https://app.conductor.build/workspace/private-canary-123",
        "https://app.conductor.build/workspace?name=private-canary-123",
        "conductor://prompt=private-canary-123&path=/Users/example/private-canary-123",
        "See [workspace](conductor://prompt=private-canary-123&path=%2FUsers%2Fexample).",
        "Release notes.\n\n[proof]: conductor://prompt=private-canary-123&path=%2FUsers%2Fexample",
        "ConDuCtOr://prompt=private-canary-123&path=%2FUsers%2Fexample",
    ],
)
def test_publication_checks_reject_workspace_metadata_without_echoing_it(
    tmp_path: Path, surface: str, body: str
) -> None:
    body_path = tmp_path / "body.md"
    body_path.write_text(body)
    if surface == "changelog":
        (tmp_path / "CHANGELOG.md").write_text(body)
        addon = tmp_path / "ha-addon/mammamiradio"
        addon.mkdir(parents=True)
        (addon / "CHANGELOG.md").write_text("# Release notes\n")
        command = ["bash", str(ROOT / "scripts/check-changelog-lint.sh")]
    else:
        command = ["bash", str(ROOT / f"scripts/check-{surface}-body-lint.sh"), str(body_path)]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "private-canary-123" not in result.stdout + result.stderr


@pytest.mark.parametrize("surface", ["pr", "issue"])
def test_publication_checks_preserve_public_links_and_attribution(tmp_path: Path, surface: str) -> None:
    body = tmp_path / "body.md"
    body.write_text(
        "See https://conductor.build/docs/reference/settings.\n"
        "API guide: https://app.conductor.build/workspace-api.\n"
        "Music credit: Example Artist.\n"
        "Co-authored-by: Example Contributor <contributor@example.com>\n"
    )
    result = subprocess.run(
        ["bash", str(ROOT / f"scripts/check-{surface}-body-lint.sh"), str(body)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("path", "poison"),
    [
        ("docs/runbooks/ha-addon.md", "Refresh the V2 preship receipt before recording."),
        ("docs/release-process.md", "Preship V2 comes before HA runs."),
        ("docs/music-sources.md", "Finalize the V2 preship receipt before recording."),
        ("docs/2026-05-30-ha-context-ingestion-pipeline.md", "Use person.recorded_household_value."),
        ("scripts/showreel/README.md", "Use lock.recorded_household_value."),
        ("scripts/showreel_out/door-bentornato-fable-v2-notes.md", "Use person.recorded_household_value."),
        ("scripts/showreel_out/ma-pr-3836-notes.md", "Use device_tracker.recorded_household_value."),
        ("proof/h4-journey-validation.md", "Use person.recorded_household_value."),
    ],
)
def test_default_docs_check_covers_each_publication_surface(tmp_path: Path, path: str, poison: str) -> None:
    files = [
        "CLAUDE.md",
        "README.md",
        "CONTRIBUTING.md",
        "ha-addon/README.md",
        "ha-addon/mammamiradio/DOCS.md",
        "docs/REPO_MAP.md",
        "docs/agents.md",
        "docs/architecture.md",
        "docs/conductor.md",
        "docs/festival-mode.md",
        "docs/listener-qs-train.md",
        "docs/troubleshooting.md",
        "docs/operations.md",
        "docs/runbooks/parallel-workspaces.md",
        "docs/runbooks/ha-addon.md",
        "docs/release-process.md",
        "docs/music-sources.md",
        "docs/2026-05-30-ha-context-ingestion-pipeline.md",
        "scripts/showreel/README.md",
        "scripts/showreel_out/door-bentornato-fable-v2-notes.md",
        "scripts/showreel_out/ma-pr-3836-notes.md",
        "proof/h4-journey-validation.md",
    ]
    for name in files:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Safe\n")
    for script in ("check-docs-safety.sh", "lint-patterns.sh", "docs_safety.py"):
        shutil.copy2(ROOT / "scripts" / script, tmp_path / "scripts" / script)
    (tmp_path / path).write_text(poison)
    result = subprocess.run(
        ["bash", str(tmp_path / "scripts/check-docs-safety.sh")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert path in result.stdout
    assert "missing" not in result.stdout
