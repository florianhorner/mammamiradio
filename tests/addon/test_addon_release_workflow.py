"""Guards for the add-on release workflow (addon-release.yml).

Contract: addon-release.yml triggers on v* tag push only and is responsible for:
  1. Validating the semver tag format (vX.Y.Z — not vfoo, not v1.2)
  2. Confirming the tag version matches ha-addon/mammamiradio/config.yaml version:
  3. Confirming the GHCR tag does not already exist (immutability guard)
  4. Publishing :X.Y.Z and :latest for both amd64 and aarch64 with fail-fast: true
  5. Running a smoke test against the :sha image built by addon-build.yml

addon-build.yml (main-push) must NOT publish :X.Y.Z or :latest after this split.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "addon-release.yml"
BUILD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "addon-build.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _build_workflow_text() -> str:
    return BUILD_WORKFLOW.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Pre-flight job contract
# ---------------------------------------------------------------------------


def test_release_workflow_triggers_on_version_tags_only():
    """Workflow must trigger on v* tag push; must not trigger on branch pushes."""
    text = _workflow_text()
    trigger_section = re.search(r"\bon:\s*\n(.*?)(?=\njobs:)", text, re.DOTALL)
    assert trigger_section, "Could not locate `on:` block in addon-release.yml"
    trigger_block = trigger_section.group(0)
    assert "tags:" in trigger_block and "v*" in trigger_block, "addon-release.yml must trigger on v* tag push"
    assert "branches:" not in trigger_block, (
        "addon-release.yml must not trigger on branch pushes — tag-only.\n"
        "A branch trigger would publish :X.Y.Z on every main merge, recreating the mutability bug."
    )


def test_release_workflow_validates_semver_format():
    """Pre-flight must strip the v prefix and validate X.Y.Z semver format."""
    text = _workflow_text()
    assert "TAG#v" in text, (
        "pre-flight must strip the 'v' prefix from the tag using ${TAG#v}.\n"
        "Tags like 'vfoo' would otherwise pass through and create invalid image tags."
    )
    assert re.search(r"grep\b.*\[0-9\]", text), (
        "pre-flight must validate semver X.Y.Z format (e.g. grep -Eq '^[0-9]+\\.[0-9]+\\.[0-9]+$').\n"
        "Without this, 'git push tag vfoo' would bypass the pre-flight and attempt a build."
    )


def test_release_workflow_validates_tag_version_matches_config_yaml():
    """Pre-flight must read config.yaml version: and compare against tag version."""
    text = _workflow_text()
    assert "ha-addon/mammamiradio/config.yaml" in text, (
        "pre-flight must reference ha-addon/mammamiradio/config.yaml to read the version field.\n"
        "Without this, tagging v2.12.4 while config.yaml says 2.12.3 would publish a mismatched image."
    )
    assert re.search(r"grep.*version.*config\.yaml|CONFIG_VERSION", text), (
        "pre-flight must extract the version from config.yaml and compare it against the tag."
    )


def test_release_workflow_immutability_preflight_present():
    """Pre-flight must run docker manifest inspect to block tag overwrites."""
    text = _workflow_text()
    assert "docker manifest inspect" in text, (
        "pre-flight must run `docker manifest inspect` to check GHCR tag immutability.\n"
        "Without this, pushing v2.12.4 a second time would silently overwrite the published image."
    )
    assert "exit 1" in text, (
        "pre-flight must exit 1 (fail loudly) if the tag already exists.\n"
        "A silent no-op would hide the overwrite attempt."
    )


# ---------------------------------------------------------------------------
# Build job contract
# ---------------------------------------------------------------------------


def test_release_workflow_publishes_versioned_per_arch_images():
    """:X.Y.Z tag must appear in the build job, driven by pre-flight outputs."""
    text = _workflow_text()
    assert "needs.pre-flight.outputs.version" in text, (
        "build job must use ${{ needs.pre-flight.outputs.version }} for the :X.Y.Z tag.\n"
        "This ensures the tag is validated by pre-flight before it reaches the build step."
    )
    assert "IMAGE_BASE" in text and "matrix.arch" in text, (
        "build job must publish per-arch images using IMAGE_BASE and matrix.arch."
    )


def test_release_workflow_publishes_latest():
    """:latest must appear in addon-release.yml (addon-build.yml no longer owns it)."""
    text = _workflow_text()
    assert ":latest" in text, (
        "addon-release.yml must publish :latest — addon-build.yml no longer owns this tag.\n"
        ":latest should point to the latest stable release, not the latest edge build."
    )


def test_release_workflow_does_not_hardcode_version():
    """No literal semver string must appear in the tags block."""
    text = _workflow_text()
    tags_blocks = re.findall(r"tags:\s*\|(.*?)(?=\n\s{6}\S|\Z)", text, re.DOTALL)
    for block in tags_blocks:
        assert not re.search(r":[0-9]+\.[0-9]+\.[0-9]+\b", block), (
            f"A literal semver version was found in a tags block.\n"
            f"Use ${{{{ needs.pre-flight.outputs.version }}}} instead.\nBlock: {block}"
        )


def test_release_workflow_matrix_includes_aarch64():
    """aarch64 must be in the release build matrix."""
    text = _workflow_text()
    build_section = re.search(r"\n  build:\n((?:    .+\n|\n)*)", text)
    assert build_section, "Could not locate `build:` job in addon-release.yml"
    assert "aarch64" in build_section.group(1), (
        "aarch64 missing from addon-release.yml build matrix.\n"
        "HA Green (Raspberry Pi) users would receive a 404 on every stable update."
    )


def test_release_workflow_matrix_includes_amd64():
    """amd64 must be in the release build matrix."""
    text = _workflow_text()
    build_section = re.search(r"\n  build:\n((?:    .+\n|\n)*)", text)
    assert build_section, "Could not locate `build:` job in addon-release.yml"
    assert "amd64" in build_section.group(1), "amd64 missing from addon-release.yml build matrix."


def test_release_workflow_matrix_fails_fast():
    """fail-fast: true must be set — partial-arch stable publish is worse than no publish."""
    text = _workflow_text()
    build_section = re.search(r"\n  build:\n((?:    .+\n|\n)*)", text)
    assert build_section, "Could not locate `build:` job in addon-release.yml"
    assert "fail-fast: true" in build_section.group(1), (
        "addon-release.yml build matrix must use `fail-fast: true`.\n"
        "If aarch64 fails after amd64 publishes, HA Green users get a broken stable tag.\n"
        "addon-build.yml uses fail-fast: false (edge builds are best-effort); stable releases are not."
    )


def test_release_workflow_permissions_scoped():
    """packages: write must be at job level, not workflow level."""
    text = _workflow_text()
    top_level = re.search(r"^permissions:\s*\n((?:  .+\n)*)", text, re.MULTILINE)
    if top_level:
        assert "packages: write" not in top_level.group(0), (
            "packages: write must not be at workflow level — scope to the build job only.\n"
            "Workflow-level permissions apply to all jobs including pre-flight."
        )
    build_section = re.search(r"\n  build:\n((?:    .+\n|\n)*)", text)
    assert build_section and "packages: write" in build_section.group(1), (
        "build job must declare `packages: write` in its job-level permissions block."
    )


def test_release_workflow_fork_guard():
    """Build job must have a fork guard to prevent forks from publishing stable images."""
    text = _workflow_text()
    build_section = re.search(r"\n  build:\n((?:    .+\n|\n)*)", text)
    assert build_section, "Could not locate `build:` job in addon-release.yml"
    assert re.search(
        r"github\.repository\s*==\s*['\"]florianhorner/mammamiradio['\"]",
        build_section.group(1),
    ), (
        "build job must guard with `if: github.repository == 'florianhorner/mammamiradio'`.\n"
        "Without this, a fork that pushes a v* tag could publish images to the upstream GHCR namespace."
    )


def test_release_workflow_concurrency_defined():
    """concurrency: block must reference github.ref to prevent race on rapid re-tags."""
    text = _workflow_text()
    assert "concurrency:" in text, "addon-release.yml must define a concurrency block."
    assert "github.ref" in text, (
        "concurrency group must reference github.ref so rapid re-pushes of the same tag cancel in-progress runs."
    )


# ---------------------------------------------------------------------------
# Smoke job contract
# ---------------------------------------------------------------------------


def test_release_workflow_build_needs_preflight():
    """Build job must declare needs: pre-flight."""
    text = _workflow_text()
    build_section = re.search(r"\n  build:\n((?:    .+\n|\n)*)", text)
    assert build_section, "Could not locate `build:` job in addon-release.yml"
    build_block = build_section.group(1)
    assert "needs: pre-flight" in build_block or "needs: [pre-flight" in build_block, (
        "build job must declare `needs: pre-flight` so it does not run if pre-flight validation fails.\n"
        "Without this, a mismatched tag version or existing GHCR tag would not block the build."
    )


def test_release_workflow_runs_smoke_test():
    """Smoke job must pull :sha, run on port 8765:8000, and call /healthz."""
    text = _workflow_text()
    smoke_section = re.search(r"\n  smoke:\n((?:    .+\n|\n)*)", text)
    assert smoke_section, "Could not locate `smoke:` job in addon-release.yml"
    smoke_block = smoke_section.group(1)
    assert "github.sha" in smoke_block, (
        "smoke job must pull :${{ github.sha }} (the image already built by addon-build.yml), "
        "not :X.Y.Z (which was just published and is not independently proven by this run)."
    )
    assert "8765:8000" in smoke_block, (
        "smoke job must map port 8765:8000 — matches the smoke contract in addon-build.yml."
    )
    assert "/healthz" in smoke_block, "smoke job must call /healthz."
    assert "failing" in smoke_block, "smoke job must check that status != 'failing'."


# ---------------------------------------------------------------------------
# Action pin contract
# ---------------------------------------------------------------------------


def test_release_workflow_uses_same_pinned_actions_as_build():
    """Every pinned action SHA in addon-build.yml must appear in addon-release.yml."""
    build_text = _build_workflow_text()
    release_text = _workflow_text()

    pinned_shas = re.findall(r"uses:\s+\S+@([0-9a-f]{40})", build_text)
    assert pinned_shas, "Expected pinned action SHAs in addon-build.yml — found none."

    missing = [sha for sha in pinned_shas if sha not in release_text]
    assert not missing, (
        f"addon-release.yml is missing action SHAs from addon-build.yml: {missing}\n"
        "Both workflows must use identical pinned SHAs to prevent supply chain divergence."
    )
