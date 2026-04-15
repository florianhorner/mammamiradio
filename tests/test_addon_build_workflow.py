"""Guards for the addon-build.yml CI workflow.

The HA addon ships a radio.toml with Pi-appropriate pacing overrides that differ
from the root radio.toml. The CI workflow uses a sed-based transformation to apply
those overrides before comparing — this is the correct and intentional approach.

These tests lock down the structural invariants:

  1. The workflow uses sed-based comparison (not cmp -s) to handle pacing overrides.
  2. The build job still depends on the validate job (needs: validate).
  3. Both target architectures remain in the build matrix.
  4. The trigger paths still cover every file touched by a version-bump commit.
  5. The sed-based comparison must NOT revert to a strict cmp -s check (which would
     break whenever pacing overrides diverge from root).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "addon-build.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The workflow must use the sed-based comparison for radio.toml
# ---------------------------------------------------------------------------


def test_ci_radio_toml_check_uses_sed_transform():
    """The validate step must use sed to apply pacing overrides before comparison.

    The HA addon radio.toml has different pacing defaults (fewer songs between
    banter, fewer ad spots) to suit Raspberry Pi hardware. A plain `cmp -s`
    comparison would fail whenever these intentional overrides diverge from root.
    The sed approach is the correct and intentional validation strategy.
    """
    text = _workflow_text()
    assert "EXPECTED=$(sed" in text, (
        "Expected a sed-based EXPECTED=$(sed ...) comparison in addon-build.yml.\n"
        "The HA addon radio.toml has intentional pacing overrides — a plain cmp -s\n"
        "would reject valid configurations. Use sed to normalize before comparing."
    )


# ---------------------------------------------------------------------------
# 2. The build job must still depend on validate
# ---------------------------------------------------------------------------


def test_ci_build_job_needs_validate():
    """If validate fails, build must be skipped — not run anyway.

    The `needs: validate` dependency stops bad images from being pushed.
    """
    text = _workflow_text()
    build_section_match = re.search(r"\n  build:\n((?:    .+\n|\n)*)", text)
    assert build_section_match, "Could not locate `build:` job block in addon-build.yml"

    build_block = build_section_match.group(1)
    assert "needs: validate" in build_block, (
        "The build job must declare `needs: validate`.\nWithout it, image builds proceed even when validation fails."
    )


# ---------------------------------------------------------------------------
# 3. Both target architectures must remain in the build matrix
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
# 4. Trigger paths must still cover version-bump files
# ---------------------------------------------------------------------------


def test_ci_trigger_paths_cover_version_bump_files():
    """All files that change in a version-bump commit must be covered by the trigger paths."""
    text = _workflow_text()

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
# 5. Guard: do not regress to strict cmp -s (would break pacing overrides)
# ---------------------------------------------------------------------------


def test_ci_strict_cmp_is_absent():
    """The workflow must NOT use `cmp -s radio.toml ha-addon/mammamiradio/radio.toml`.

    A strict byte-for-byte comparison would fail because the HA addon intentionally
    ships different pacing defaults. If this assert fires, someone replaced the
    sed-based validation with a naive cmp check.
    """
    text = _workflow_text()
    assert "cmp -s radio.toml ha-addon/mammamiradio/radio.toml" not in text, (
        "Strict `cmp -s` comparison found in addon-build.yml.\n"
        "The HA addon radio.toml has intentional pacing overrides — a plain cmp -s\n"
        "would reject valid configurations. Use the sed-based comparison instead."
    )
