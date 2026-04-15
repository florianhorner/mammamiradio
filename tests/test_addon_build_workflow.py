"""Guards for the addon-build.yml CI workflow after the 2.10.1 revert.

This PR reverted the sed-based radio.toml comparison back to a strict byte-for-byte
`cmp -s` check, and also restored the `cp radio.toml ha-addon/mammamiradio/` step
so that the HA addon always ships the same radio.toml as the root.

These tests lock down the new structural invariants:

  1. The workflow uses `cmp -s` for the strict radio.toml comparison.
  2. The build step copies the root radio.toml into the addon build context.
  3. The build job still depends on the validate job (needs: validate).
  4. Both target architectures remain in the build matrix.
  5. The trigger paths still cover every file touched by a version-bump commit.
  6. The sed-based comparison (introduced in 2.10.1) is absent.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "addon-build.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The workflow must use strict cmp -s for radio.toml comparison
# ---------------------------------------------------------------------------


def test_ci_radio_toml_check_uses_strict_cmp():
    """The validate step must use `cmp -s` for an exact byte comparison of radio.toml.

    After the 2.10.1 sed-based workaround was reverted, both files must be
    identical. The strict `cmp -s` comparison enforces this invariant.
    """
    text = _workflow_text()
    assert "cmp -s radio.toml ha-addon/mammamiradio/radio.toml" in text, (
        "Expected `cmp -s radio.toml ha-addon/mammamiradio/radio.toml` in addon-build.yml.\n"
        "The strict comparison was restored after the 2.10.1 sed-workaround was reverted."
    )


# ---------------------------------------------------------------------------
# 2. The build step must copy the root radio.toml into the addon build context
# ---------------------------------------------------------------------------


def test_ci_build_step_copies_root_radio_toml():
    """The build job must include `cp radio.toml ha-addon/mammamiradio/`.

    This was restored after the 2.10.1 revert. The addon build context needs the
    radio.toml file copied in alongside the mammamiradio source.
    """
    text = _workflow_text()
    assert "cp radio.toml ha-addon/mammamiradio/" in text, (
        "Expected `cp radio.toml ha-addon/mammamiradio/` in the build step.\n"
        "This copy was restored when the HA-specific pacing override approach was reverted."
    )


# ---------------------------------------------------------------------------
# 3. The build job must still depend on validate
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
        "The build job must declare `needs: validate`.\n"
        "Without it, image builds proceed even when validation fails."
    )


# ---------------------------------------------------------------------------
# 4. Both target architectures must remain in the build matrix
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
    assert re.search(r"arch:\s*\[[^\]]*\bamd64\b", build_block), (
        "amd64 missing from addon-build.yml build matrix."
    )


# ---------------------------------------------------------------------------
# 5. Trigger paths must still cover version-bump files
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
# 6. The sed-based comparison (from 2.10.1) must be absent
# ---------------------------------------------------------------------------


def test_ci_sed_based_radio_toml_comparison_is_absent():
    """The sed-based EXPECTED=$(sed ...) workaround from 2.10.1 must not be present.

    In 2.10.1 a sed transform was used to apply pacing overrides before comparing.
    That approach was reverted in this PR — the files must match exactly.
    Reintroducing the sed block would silently allow pacing drift again.
    """
    text = _workflow_text()
    assert "EXPECTED=$(sed" not in text, (
        "The sed-based EXPECTED=$(sed ...) block was reintroduced.\n"
        "This approach was removed when the HA pacing overrides were reverted.\n"
        "Use `cmp -s` for an exact comparison instead."
    )