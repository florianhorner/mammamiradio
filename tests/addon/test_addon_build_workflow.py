"""Guards for the add-on build workflow.

Contract (from 2.10.3 onward): `ha-addon/mammamiradio/radio.toml` is byte-for-byte
identical to root `radio.toml`. The Pi-specific pacing overrides
(`songs_between_banter=3`, `ad_spots_per_break=1`, `lookahead_segments=2`) that lived
in the add-on copy before 2.10.3 are gone. Three places must agree on this:

  * `tests/test_addon_radio_sync.py` (Python: addon == root)
  * `scripts/test-addon-local.sh` (shell: `cmp -s`)
  * `.github/workflows/addon-build.yml` (CI: `cmp -s`)

Before 2.10.3 the CI workflow applied a sed transform to pre-add the Pi overrides
before comparing.  If that pattern comes back, CI will silently pass while the other
two gates fail — the exact kind of split-brain that caused the 2.10.0 manifest 404
in the opposite direction.

These tests lock down the structural invariants:

  1. The CI check uses `cmp -s` (matches shell validator + Python test).
  2. No sed substitution that re-introduces the Pi overrides.
  3. The build job cannot run if validate fails (`needs: validate`).
  4. Both target architectures are in the build matrix.
  5. The workflow triggers cover every file touched by a version-bump commit.
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
# 1. CI must use `cmp -s` (strict byte equality) for the radio.toml check
# ---------------------------------------------------------------------------


def test_ci_radio_toml_uses_strict_cmp():
    """CI must enforce byte-for-byte identity, matching the Python test and the shell validator."""
    text = _workflow_text()
    assert "cmp -s radio.toml ha-addon/mammamiradio/radio.toml" in text, (
        "addon-build.yml must run `cmp -s radio.toml ha-addon/mammamiradio/radio.toml` in the validate job.\n"
        "This mirrors tests/test_addon_radio_sync.py and scripts/test-addon-local.sh. If you "
        "change the contract, update all three locations together."
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
