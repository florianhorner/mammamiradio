"""Guards for the add-on release workflow (addon-release.yml).

Contract: addon-release.yml triggers on v* tag push only and is responsible for:
  1. Validating the semver tag format (vX.Y.Z — not vfoo, not v1.2)
  2. Confirming the tag version matches ha-addon/mammamiradio/config.yaml version:
  3. Confirming prebuilt :sha images exist for both architectures
  4. Promoting those exact :sha images to :X.Y.Z and :latest
  5. Running smoke tests before and after stable-tag promotion

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
    """Workflow must trigger on v* tag push; dispatch is guarded to tag refs."""
    text = _workflow_text()
    trigger_section = re.search(r"\bon:\s*\n(.*?)(?=\njobs:)", text, re.DOTALL)
    assert trigger_section, "Could not locate `on:` block in addon-release.yml"
    trigger_block = trigger_section.group(0)
    assert "tags:" in trigger_block and "v*" in trigger_block, "addon-release.yml must trigger on v* tag push"
    assert "branches:" not in trigger_block, (
        "addon-release.yml must not trigger on branch pushes — tag-only.\n"
        "A branch trigger would publish :X.Y.Z on every main merge, recreating the mutability bug."
    )
    assert "workflow_dispatch:" in trigger_block, "manual dispatch is allowed only with a tag-ref guard."


def test_release_workflow_dispatch_requires_tag_ref():
    """Manual dispatch must not publish stable tags from branch refs."""
    text = _workflow_text()
    assert "GITHUB_REF_TYPE" in text, "pre-flight must inspect GITHUB_REF_TYPE."
    assert '"tag"' in text or "'tag'" in text, (
        "pre-flight must require github.ref_type/GITHUB_REF_TYPE == tag.\n"
        "Without this, a branch named vX.Y.Z could be manually dispatched and stable-published."
    )
    assert "Stable add-on releases must run from a tag ref" in text


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


def test_release_workflow_requires_prebuilt_sha_images():
    """Pre-flight must fail before publishing if addon-build.yml did not create :sha."""
    text = _workflow_text()
    assert "${{ github.sha }}" in text, "release workflow must reference the source :sha tag."
    assert "Missing $SHA_TAG" in text, (
        "pre-flight must fail with a clear message when the :sha image is missing.\n"
        "Without this, a tag on an unbuilt commit could publish stable tags before smoke fails."
    )
    assert re.search(r"for ARCH in amd64 aarch64", text), (
        "pre-flight must require prebuilt :sha images for both stable architectures."
    )


def test_release_workflow_allows_matching_partial_reruns_only():
    """Existing stable tags may recover partial reruns only when they match :sha."""
    text = _workflow_text()
    assert "VERSION_DIGEST" in text and "SHA_DIGEST" in text, (
        "pre-flight/promote must compare existing version-tag digest to the source SHA digest.\n"
        "This permits recovery after a partial arch publish without allowing silent retags."
    )
    assert "rerun recovery is allowed" in text, (
        "workflow should explicitly allow matching existing tags so partial arch publishes are recoverable."
    )
    assert "does not match $SHA_TAG" in text or "Refusing to overwrite" in text, (
        "workflow must fail if an existing :X.Y.Z tag points at a different digest."
    )
    assert "exit 1" in text, "pre-flight/promote must exit 1 (fail loudly) if an existing stable tag is mismatched."


# ---------------------------------------------------------------------------
# Build job contract
# ---------------------------------------------------------------------------


def test_release_workflow_promotes_versioned_per_arch_images():
    """:X.Y.Z tag must be created from the prebuilt SHA image."""
    text = _workflow_text()
    assert "needs.pre-flight.outputs.version" in text, (
        "promote job must use ${{ needs.pre-flight.outputs.version }} for the :X.Y.Z tag.\n"
        "This ensures the tag is validated by pre-flight before stable promotion."
    )
    assert "IMAGE_BASE" in text and "matrix.arch" in text, (
        "promote job must publish per-arch images using IMAGE_BASE and matrix.arch."
    )
    assert 'docker buildx imagetools create --tag "$VERSION_TAG" "$SHA_TAG"' in text, (
        "release workflow must promote the already-built SHA artifact instead of rebuilding."
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


def test_release_workflow_does_not_rebuild_stable_images():
    """Stable release must promote prebuilt :sha images instead of rebuilding."""
    text = _workflow_text()
    assert "docker/build-push-action" not in text, (
        "addon-release.yml must not rebuild stable images.\n"
        "Promoting :sha avoids moving-dependency drift between main CI and tag release."
    )
    assert "Copy source into addon build context" not in text


def test_release_workflow_matrix_includes_aarch64():
    """aarch64 must be in the release promote matrix."""
    text = _workflow_text()
    promote_section = re.search(r"\n  promote:\n((?:    .+\n|\n)*)", text)
    assert promote_section, "Could not locate `promote:` job in addon-release.yml"
    assert "aarch64" in promote_section.group(1), (
        "aarch64 missing from addon-release.yml promote matrix.\n"
        "HA Green (Raspberry Pi) users would receive a 404 on every stable update."
    )


def test_release_workflow_matrix_includes_amd64():
    """amd64 must be in the release promote matrix."""
    text = _workflow_text()
    promote_section = re.search(r"\n  promote:\n((?:    .+\n|\n)*)", text)
    assert promote_section, "Could not locate `promote:` job in addon-release.yml"
    assert "amd64" in promote_section.group(1), "amd64 missing from addon-release.yml promote matrix."


def test_release_workflow_matrix_fails_fast():
    """fail-fast: true must be set for stable promotion."""
    text = _workflow_text()
    promote_section = re.search(r"\n  promote:\n((?:    .+\n|\n)*)", text)
    assert promote_section, "Could not locate `promote:` job in addon-release.yml"
    assert "fail-fast: true" in promote_section.group(1), (
        "addon-release.yml promote matrix must use `fail-fast: true`.\n"
        "Matching existing version tags are allowed so reruns can recover a partial arch publish."
    )


def test_release_workflow_permissions_scoped():
    """packages: write must be at promote job level, not workflow level."""
    text = _workflow_text()
    top_level = re.search(r"^permissions:\s*\n((?:  .+\n)*)", text, re.MULTILINE)
    if top_level:
        assert "packages: write" not in top_level.group(0), (
            "packages: write must not be at workflow level — scope to the promote job only.\n"
            "Workflow-level permissions apply to all jobs including pre-flight."
        )
    promote_section = re.search(r"\n  promote:\n((?:    .+\n|\n)*)", text)
    assert promote_section and "packages: write" in promote_section.group(1), (
        "promote job must declare `packages: write` in its job-level permissions block."
    )


def test_release_workflow_fork_guard():
    """Promote job must have a fork guard to prevent forks from publishing stable images."""
    text = _workflow_text()
    build_section = re.search(r"\n  promote:\n((?:    .+\n|\n)*)", text)
    assert build_section, "Could not locate `promote:` job in addon-release.yml"
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
    """Promote job must declare needs: pre-flight and smoke-prebuilt."""
    text = _workflow_text()
    promote_section = re.search(r"\n  promote:\n((?:    .+\n|\n)*)", text)
    assert promote_section, "Could not locate `promote:` job in addon-release.yml"
    promote_block = promote_section.group(1)
    assert "needs: [pre-flight, smoke-prebuilt]" in promote_block, (
        "promote job must wait for both pre-flight and the prebuilt SHA smoke test.\n"
        "Without this, stable tags could be published before the source artifact is proven."
    )


def test_release_workflow_runs_smoke_test():
    """Smoke jobs must gate promotion on :sha and verify the published release tag."""
    text = _workflow_text()
    prebuilt_section = re.search(r"\n  smoke-prebuilt:\n((?:    .+\n|\n)*)", text)
    assert prebuilt_section, "Could not locate `smoke-prebuilt:` job in addon-release.yml"
    prebuilt_block = prebuilt_section.group(1)
    assert "github.sha" in prebuilt_block, (
        "smoke-prebuilt must pull :${{ github.sha }} before stable tags are promoted."
    )
    assert "needs: pre-flight" in prebuilt_block

    smoke_section = re.search(r"\n  smoke:\n((?:    .+\n|\n)*)", text)
    assert smoke_section, "Could not locate `smoke:` job in addon-release.yml"
    smoke_block = smoke_section.group(1)
    assert "needs: [pre-flight, promote]" in smoke_block, "final smoke must run after stable promotion."
    assert "needs.pre-flight.outputs.version" in smoke_block, (
        "final smoke must pull the published :X.Y.Z release tag, not only :${{ github.sha }}."
    )
    assert "8765:8000" in smoke_block, (
        "smoke job must map port 8765:8000 — matches the smoke contract in addon-build.yml."
    )
    assert "/healthz" in smoke_block, "smoke job must call /healthz."
    assert "failing" in smoke_block, "smoke job must check that status != 'failing'."


# ---------------------------------------------------------------------------
# Action pin contract
# ---------------------------------------------------------------------------


def test_release_workflow_uses_pinned_actions():
    """Every action used by addon-release.yml must be pinned to a commit SHA."""
    release_text = _workflow_text()
    unpinned = re.findall(r"uses:\s+\S+@(?![0-9a-f]{40})(\S+)", release_text)
    assert not unpinned, f"addon-release.yml has unpinned action refs: {unpinned}"


def test_release_workflow_keeps_common_action_pins_aligned_with_build():
    """Actions shared with addon-build.yml must use the same pinned SHAs."""
    build_text = _build_workflow_text()
    release_text = _workflow_text()

    build_actions = dict(re.findall(r"uses:\s+(\S+)@([0-9a-f]{40})", build_text))
    release_actions = dict(re.findall(r"uses:\s+(\S+)@([0-9a-f]{40})", release_text))
    shared_actions = sorted(set(build_actions) & set(release_actions))
    assert shared_actions, "Expected shared pinned actions between addon-build.yml and addon-release.yml."

    mismatched = [action for action in shared_actions if build_actions[action] != release_actions[action]]
    assert not mismatched, (
        f"Shared actions use different pinned SHAs: {mismatched}\n"
        "Shared workflow actions must stay aligned to prevent supply chain divergence."
    )
