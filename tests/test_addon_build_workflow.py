"""Structural guards for .github/workflows/addon-build.yml.

This file replaced an earlier version that guarded the sed-based radio.toml
comparison approach introduced in 2.10.1.  The workflow was subsequently
simplified: the validate job now uses a direct byte comparison (`cmp -s`)
and the build job explicitly copies the root radio.toml into the addon build
context, ensuring the container always ships the same config as the repo root.

These tests lock down the new structural invariants so the workflow cannot
silently regress to the old broken patterns.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "addon-build.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _trigger_block(text: str) -> str:
    """Return the `on:` trigger block (everything before the first `jobs:` heading)."""
    m = re.search(r"\bon:\s*\n(.*?)(?=\njobs:)", text, re.DOTALL)
    assert m, "Could not locate `on:` block in addon-build.yml"
    return m.group(0)


def _build_block(text: str) -> str:
    """Return the `build:` job block."""
    m = re.search(r"\n  build:\n((?:    .+\n|\n)*)", text)
    assert m, "Could not locate `build:` job block in addon-build.yml"
    return m.group(1)


# ---------------------------------------------------------------------------
# 1. Validate step: cmp -s (direct byte comparison) must be present
# ---------------------------------------------------------------------------


def test_ci_radio_toml_check_uses_cmp_s():
    """The validate step must use `cmp -s` to compare the two radio.toml files.

    The simplified workflow no longer needs the sed-based substitution approach.
    `cmp -s` is the canonical way to verify the files are byte-for-byte identical,
    which is now the enforced invariant: the build step copies radio.toml into the
    addon context, so at CI time they must be identical.
    """
    text = _workflow_text()
    assert "cmp -s radio.toml ha-addon/mammamiradio/radio.toml" in text, (
        "Expected `cmp -s radio.toml ha-addon/mammamiradio/radio.toml` in addon-build.yml validate step.\n"
        "The workflow must use a direct byte comparison after the build step syncs the files."
    )


# ---------------------------------------------------------------------------
# 2. Validate step: sed-based substitution approach must be absent
# ---------------------------------------------------------------------------


def test_ci_radio_toml_check_has_no_sed_substitutions():
    """The sed-based radio.toml comparison must no longer be present.

    The 2.10.1 workaround applied three sed substitutions to apply the known
    HA pacing overrides before comparing.  This was replaced with a plain
    `cmp -s` comparison after the build step began copying the root radio.toml
    verbatim.  The sed approach must not return.
    """
    text = _workflow_text()
    # The old pattern: EXPECTED=$(sed ... radio.toml)
    assert "EXPECTED=$(sed" not in text, (
        "Forbidden: `EXPECTED=$(sed ...)` pattern found in addon-build.yml.\n"
        "The sed-based radio.toml comparison was replaced with `cmp -s`. "
        "Do not re-introduce the sed workaround."
    )


def test_ci_radio_toml_check_has_no_songs_between_banter_sed():
    """The specific sed substitution for songs_between_banter must be absent."""
    text = _workflow_text()
    assert "songs_between_banter" not in text, (
        "Forbidden: sed substitution for `songs_between_banter` found in addon-build.yml.\n"
        "The pacing-override approach was replaced with a direct file copy + cmp -s."
    )


# ---------------------------------------------------------------------------
# 3. Build step: root radio.toml must be copied into the addon build context
# ---------------------------------------------------------------------------


def test_ci_build_step_copies_root_radio_toml():
    """The build job must explicitly copy radio.toml into the addon build context.

    Prior to 2.10.2, the root radio.toml was intentionally NOT copied so that
    the HA-specific pacing-tuned file would survive.  After the pacing overrides
    were eliminated (the two files are now kept identical), the build step must
    copy the root radio.toml to ensure the Docker image always ships the
    version-controlled config, not a stale checkout artifact.
    """
    text = _workflow_text()
    assert "cp radio.toml ha-addon/mammamiradio/" in text, (
        "Expected `cp radio.toml ha-addon/mammamiradio/` in the build step.\n"
        "The build job must sync the root radio.toml into the addon context so "
        "the Docker image ships the correct config."
    )


# ---------------------------------------------------------------------------
# 4. Build job must depend on validate
# ---------------------------------------------------------------------------


def test_ci_build_job_needs_validate():
    """If validate fails, build must be skipped — not run anyway.

    The `needs: validate` dependency is what stops broken images from being
    pushed.  Removing it would let bad images ship silently.
    """
    text = _workflow_text()
    build_block = _build_block(text)
    assert "needs: validate" in build_block, (
        "The build job must declare `needs: validate`.\n"
        "Without it, image builds proceed even when validation fails."
    )


# ---------------------------------------------------------------------------
# 5. Both target architectures must be in the build matrix
# ---------------------------------------------------------------------------


def test_ci_build_matrix_includes_aarch64():
    """aarch64 must be in the build matrix — it is the Raspberry Pi / HA Green arch."""
    build_block = _build_block(_workflow_text())
    assert re.search(r"arch:\s*\[[^\]]*\baarch64\b", build_block), (
        "aarch64 missing from addon-build.yml build matrix. "
        "HA Green and Raspberry Pi users would receive a 404 on every update."
    )


def test_ci_build_matrix_includes_amd64():
    """amd64 must be in the build matrix — it covers x86 NUC / VM HA installs."""
    build_block = _build_block(_workflow_text())
    assert re.search(r"arch:\s*\[[^\]]*\bamd64\b", build_block), (
        "amd64 missing from addon-build.yml build matrix."
    )


# ---------------------------------------------------------------------------
# 6. Trigger paths must cover version-bump files
# ---------------------------------------------------------------------------


def test_ci_trigger_paths_cover_version_bump_files():
    """All files touched in a version-bump commit must appear in the trigger paths.

    If a trigger path is missing, the workflow does not run on version bumps
    and images are never built.
    """
    trigger_block = _trigger_block(_workflow_text())

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
# 7. The validate step must exit 1 on radio.toml drift
# ---------------------------------------------------------------------------


def test_ci_validate_step_exits_on_drift():
    """The validate step must call `exit 1` when cmp reports a difference.

    Without an explicit `exit 1`, a failed comparison would still produce an
    error message but CI would mark the step as successful, allowing the build
    to proceed with mismatched files.
    """
    text = _workflow_text()
    # Scope to the validate job section only
    validate_section = re.search(r"\n  validate:\n((?:    .+\n|\n)*)", text)
    assert validate_section, "Could not locate `validate:` job block in addon-build.yml"
    validate_block = validate_section.group(1)
    assert "exit 1" in validate_block, (
        "The validate job must contain at least one `exit 1` to fail CI on drift."
    )