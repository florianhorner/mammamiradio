"""Guards for the add-on build workflow.

Contract (from 2.10.3 onward): `ha-addon/mammamiradio/radio.toml` is byte-for-byte
identical to root `radio.toml`. The Pi-specific pacing overrides
(`songs_between_banter=3`, `ad_spots_per_break=1`, `lookahead_segments=2`) that lived
in the add-on copy before 2.10.3 are gone. CI must call the same canonical
validator as local development so this contract has one implementation:

  * `tests/test_addon_radio_sync.py` (Python: addon == root)
  * `scripts/validate-addon.sh` (shell: `cmp -s`)
  * `.github/workflows/addon-build.yml` (CI: calls `scripts/validate-addon.sh`)

Before 2.10.3 the CI workflow applied a sed transform to pre-add the Pi overrides
before comparing.  If that pattern comes back, CI will silently pass while the other
two gates fail — the exact kind of split-brain that caused the 2.10.0 manifest 404
in the opposite direction.

These tests lock down the structural invariants:

  1. The CI validate job calls the canonical shell validator.
  2. No sed substitution that re-introduces the Pi overrides.
  3. The build job cannot run if validate fails (`needs: validate`).
  4. Both target architectures are in the build matrix.
  5. The workflow triggers cover every file touched by a version-bump commit.
  6. The workflow publishes the versioned per-arch image tags that HA installs.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "addon-build.yml"

# Pacing keys whose historical HA-only overrides must never be re-applied at build time.
# If any future sed expression in the workflow substitutes these values, the test fails.
FORBIDDEN_OVERRIDE_KEYS = ("songs_between_banter", "ad_spots_per_break", "lookahead_segments")


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. CI must call the canonical validator
# ---------------------------------------------------------------------------


def test_ci_validate_job_calls_canonical_validator():
    """CI must share local validation instead of reimplementing shell fragments."""
    text = _workflow_text()
    assert "bash scripts/validate-addon.sh" in text, (
        "addon-build.yml must call `bash scripts/validate-addon.sh` in the validate job.\n"
        "Do not duplicate version, image, options, or radio.toml checks in CI."
    )


# ---------------------------------------------------------------------------
# 2. No sed substitution on radio.toml may re-introduce Pi overrides at build time
# ---------------------------------------------------------------------------


def test_ci_has_no_radio_toml_sed_transform():
    """Re-introducing the pre-2.10.3 sed transform would silently let the addon radio.toml drift."""
    text = _workflow_text()
    for key in FORBIDDEN_OVERRIDE_KEYS:
        assert not re.search(rf"sed[^\n]*{re.escape(key)}", text), (
            f"Forbidden: the CI workflow is transforming `{key}` via sed before the radio.toml comparison.\n"
            "Since 2.10.3 the add-on and root radio.toml are byte-identical. A sed-based CI transform "
            "re-creates the split-brain this rule exists to prevent."
        )


# ---------------------------------------------------------------------------
# 3. build must depend on validate
# ---------------------------------------------------------------------------


def test_ci_build_job_needs_validate():
    """If validate fails, build must be skipped — not run anyway.

    The `needs: validate` dependency is what stops broken images from being
    pushed.  Removing it would let bad images ship silently.
    """
    text = _workflow_text()

    # Find the build: block and check for needs: validate within it
    # We parse with regex rather than a YAML library to avoid adding a dep.
    build_section_match = re.search(r"\n  build:\n((?:    .+\n|\n)*)", text)
    assert build_section_match, "Could not locate `build:` job block in addon-build.yml"

    build_block = build_section_match.group(1)
    assert "needs: validate" in build_block, (
        "The build job must declare `needs: validate`.\nWithout it, image builds proceed even when validation fails."
    )


# ---------------------------------------------------------------------------
# 4. Both target architectures must be in the matrix
# ---------------------------------------------------------------------------


def test_ci_build_matrix_includes_aarch64():
    """aarch64 must be in the build matrix — it is the Raspberry Pi / HA Green arch."""
    build_section_match = re.search(r"\n  build:\n((?:    .+\n|\n)*)", _workflow_text())
    assert build_section_match, "Could not locate `build:` job block in addon-build.yml"
    build_block = build_section_match.group(1)
    assert re.search(r"arch:\s*\[[^\]]*\baarch64\b", build_block), (
        "aarch64 missing from addon-build.yml build matrix. "
        "HA Green and Raspberry Pi users would receive a 404 on every update."
    )


def test_ci_build_matrix_includes_amd64():
    """amd64 must be in the build matrix — it covers x86 NUC / VM HA installs."""
    build_section_match = re.search(r"\n  build:\n((?:    .+\n|\n)*)", _workflow_text())
    assert build_section_match, "Could not locate `build:` job block in addon-build.yml"
    build_block = build_section_match.group(1)
    assert re.search(r"arch:\s*\[[^\]]*\bamd64\b", build_block), "amd64 missing from addon-build.yml build matrix."


# ---------------------------------------------------------------------------
# 5. Workflow triggers must cover every file touched by a version-bump commit
# ---------------------------------------------------------------------------


def test_ci_trigger_paths_cover_version_bump_files():
    """All files that change in a version-bump commit must be covered by the trigger paths.

    If a trigger path is missing, the workflow doesn't run on version bumps and
    images are never built — exactly what happened with 2.10.0 (pyproject.toml is
    the key file bumped in a release; ha-addon/** is also touched).

    IMPORTANT: search only within the `on:` block, not the full file. Strings like
    "pyproject.toml" and "radio.toml" also appear in the build job's `cp` commands —
    a full-file search would pass even if the trigger path was removed.
    """
    text = _workflow_text()

    # Extract only the on: block (everything before the first `jobs:` heading)
    trigger_section_match = re.search(r"\bon:\s*\n(.*?)(?=\njobs:)", text, re.DOTALL)
    assert trigger_section_match, "Could not locate `on:` block in addon-build.yml"
    trigger_block = trigger_section_match.group(0)

    required_trigger_patterns = [
        "ha-addon/**",
        "mammamiradio/**",
        "pyproject.toml",
        "radio.toml",
    ]

    missing = [p for p in required_trigger_patterns if p not in trigger_block]
    assert not missing, (
        f"Trigger paths missing from addon-build.yml `on:` block: {missing}\n"
        "These files are touched on every version bump. Without matching trigger "
        "paths, the workflow won't run and images won't be built."
    )


# ---------------------------------------------------------------------------
# 6. Published image tags must match the Home Assistant add-on image contract
# ---------------------------------------------------------------------------


def test_ci_publishes_versioned_per_arch_addon_images():
    """addon-build.yml publishes :sha, :0.0.0, and :calver for every main merge.

    :X.Y.Z and :latest are owned by addon-release.yml (v* tag triggered) and must
    NOT appear in addon-build.yml — every main merge was silently overwriting the
    stable tag, making it mutable. addon-release.yml fixes this.
    """
    workflow_text = _workflow_text()
    config_text = (REPO_ROOT / "ha-addon" / "mammamiradio" / "config.yaml").read_text(encoding="utf-8")

    image_match = re.search(r"^image:\s*(\S+)\s*$", config_text, re.MULTILINE)
    assert image_match, "config.yaml must define the add-on image template."
    assert image_match.group(1) == "ghcr.io/florianhorner/mammamiradio-addon-{arch}"

    assert "IMAGE_BASE: ${{ github.repository_owner }}/mammamiradio-addon" in workflow_text

    required_tags = [
        "${{ env.REGISTRY }}/${{ env.IMAGE_BASE }}-${{ matrix.arch }}:${{ github.sha }}",
    ]
    missing_tags = [tag for tag in required_tags if tag not in workflow_text]
    assert not missing_tags, (
        f"addon-build.yml is missing image tags: {missing_tags}\n"
        "The :sha tag is required for the smoke job and for addon-release.yml "
        "to pull a proven image on tag-triggered stable builds."
    )

    assert "steps.version.outputs.version" not in workflow_text, (
        "addon-build.yml must not publish :X.Y.Z — stable image tags are owned by addon-release.yml.\n"
        "Every main merge was overwriting the stable tag; this is now fixed."
    )


def test_ci_version_step_removed():
    """The id: version step must not exist in addon-build.yml.

    Version-based publishing moved to addon-release.yml (v* tag triggered).
    Any reference to id: version or its outputs is dead code.
    """
    workflow_text = _workflow_text()
    assert "id: version" not in workflow_text, (
        "addon-build.yml must not have `id: version` step.\nVersion-based image publishing moved to addon-release.yml."
    )
    assert "steps.version.outputs.version" not in workflow_text, (
        "addon-build.yml must not reference steps.version.outputs.version.\n"
        "The id: version step was removed; any reference to its output is dead code."
    )


def _extract_step_block(workflow_text: str, step_name: str) -> str:
    """Return the YAML text from 'name: <step_name>' up to the next '- name:' or end of file."""
    pattern = rf"- name: {re.escape(step_name)}\n(.*?)(?=\n      - name:|\Z)"
    m = re.search(pattern, workflow_text, re.DOTALL)
    assert m, f"Step '{step_name}' not found in addon-build.yml"
    return m.group(0)


def test_ci_publishes_edge_tags_with_matching_image_labels():
    """Each edge build step's BUILD_VERSION must pair with its own pushed tag.

    Global string-presence checks would pass even if BUILD_VERSION and tags were
    swapped between steps. Scoping to the step block catches mis-pairing.
    """
    workflow_text = _workflow_text()

    assert "EDGE_SEED_VERSION: '0.0.0'" in workflow_text

    seed_block = _extract_step_block(workflow_text, "Build and push edge seed add-on image")
    assert "BUILD_VERSION=${{ env.EDGE_SEED_VERSION }}" in seed_block, (
        "seed step must set BUILD_VERSION to the edge seed version"
    )
    assert ":${{ env.EDGE_SEED_VERSION }}" in seed_block, "seed step must push the edge seed tag"

    calver_block = _extract_step_block(workflow_text, "Build and push edge calver add-on image")
    assert "BUILD_VERSION=${{ needs.validate.outputs.calver }}" in calver_block, (
        "calver step must set BUILD_VERSION to the calver output"
    )
    assert ":${{ needs.validate.outputs.calver }}" in calver_block, "calver step must push the calver tag"
