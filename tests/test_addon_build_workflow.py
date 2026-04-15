"""Guards against the failure mode that caused the 2.10.0 manifest 404.

Root cause: addon-build.yml used `cmp -s radio.toml ha-addon/mammamiradio/radio.toml`
to validate the config sync, but the HA addon intentionally carries three pacing overrides
for Pi/HA Green performance.  The strict comparison always failed → the `validate` job
always failed → `build` (which `needs: validate`) never ran → images were never pushed
→ HA Supervisor got a 404 when trying to pull the update.

These tests lock down the three structural invariants that, had they existed, would have
caught the problem before it shipped:

  1. The forbidden `cmp -s` pattern is absent.
  2. The CI sed substitutions are identical to what the Python test expects.
  3. The build job cannot run if validate fails (needs: validate).
  4. Both target architectures are in the build matrix.
  5. The workflow triggers cover every file touched by a version-bump commit.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "addon-build.yml"

# The three substitutions the Python test applies (test_addon_radio_sync.py).
# This dict is the single source of truth — both tests and the CI sed script
# are verified against it.
HA_PACING_OVERRIDES: dict[str, str] = {
    "songs_between_banter = 2": "songs_between_banter = 3",
    "ad_spots_per_break = 2": "ad_spots_per_break = 1",
    "lookahead_segments = 3": "lookahead_segments = 2",
}


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The broken pattern must never come back
# ---------------------------------------------------------------------------


def test_ci_radio_toml_check_forbids_raw_cmp():
    """cmp -s on the two radio.toml files is the exact line that broke 2.10.0.

    A raw byte comparison fails whenever any intentional HA-specific value
    differs, making the validate job fail and silently preventing all image builds.
    """
    text = _workflow_text()
    assert "cmp -s radio.toml ha-addon" not in text, (
        "Forbidden: `cmp -s radio.toml ha-addon/mammamiradio/radio.toml` is too strict.\n"
        "It rejects the intentional HA pacing overrides and blocks the build job.\n"
        "Use the sed-based check that applies the known overrides before comparing."
    )


# ---------------------------------------------------------------------------
# 2. CI sed substitutions must exactly match the Python test
# ---------------------------------------------------------------------------


def test_ci_radio_toml_sed_substitutions_match_python_test():
    """The sed replacements in addon-build.yml must be identical to HA_PACING_OVERRIDES.

    Both the CI shell step and test_addon_radio_sync.py encode the same three
    substitutions.  If they drift, one check passes while the other fails — creating
    a false positive that hides real drift or re-introduces the broken-build bug.
    """
    text = _workflow_text()

    # Scope to the EXPECTED=$(sed ...) block in the validate step only.
    # Searching the full file would capture any unrelated sed commands added later.
    sed_block_match = re.search(
        r"EXPECTED=\$\(sed \\\n(.*?)\n\s*radio\.toml\)",
        text,
        re.DOTALL,
    )
    assert sed_block_match, "Could not locate EXPECTED=$(sed ...) block in addon-build.yml"
    sed_pairs = re.findall(r"-e\s+'s/([^/]+)/([^/]+)/'", sed_block_match.group(1))
    ci_overrides = dict(sed_pairs)

    assert ci_overrides == HA_PACING_OVERRIDES, (
        f"CI sed substitutions {ci_overrides} differ from expected HA_PACING_OVERRIDES.\n"
        f"Update addon-build.yml and HA_PACING_OVERRIDES in this file together."
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
    """aarch64 must be in the build matrix — it is the Raspberry Pi / HA Green arch.

    Scoped to the build job's arch: [...] list so the test doesn't false-pass from
    the string appearing in a base image name, comment, or other unrelated location.
    """
    build_section_match = re.search(r"\n  build:\n((?:    .+\n|\n)*)", _workflow_text())
    assert build_section_match, "Could not locate `build:` job block in addon-build.yml"
    build_block = build_section_match.group(1)
    assert re.search(r"arch:\s*\[[^\]]*\baarch64\b", build_block), (
        "aarch64 missing from addon-build.yml build matrix. "
        "HA Green and Raspberry Pi users would receive a 404 on every update."
    )


def test_ci_build_matrix_includes_amd64():
    """amd64 must be in the build matrix — it covers x86 NUC / VM HA installs.

    Scoped to the build job's arch: [...] list so the test doesn't false-pass from
    the string appearing in a base image name, comment, or other unrelated location.
    """
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
# 6. Build job must NOT overwrite the HA-specific radio.toml
# ---------------------------------------------------------------------------


def test_ci_build_step_does_not_overwrite_ha_radio_toml():
    """The build job must not copy the root radio.toml over the HA-specific one.

    ha-addon/mammamiradio/radio.toml carries Pi/HA Green performance tuning
    (songs_between_banter=3, ad_spots_per_break=1, lookahead_segments=2).
    Copying the root radio.toml (which has 2/2/3) into the build context at
    build time silently discards that tuning — the Docker image ships with the
    wrong pacing values baked in, and users on Raspberry Pi get higher CPU load.

    The HA-specific file is already in the build context at checkout time.
    It must NOT be overwritten.
    """
    text = _workflow_text()
    assert "cp radio.toml ha-addon/mammamiradio/" not in text, (
        "Forbidden: `cp radio.toml ha-addon/mammamiradio/` overwrites the "
        "HA-specific radio.toml with the root version.\n"
        "The HA-specific file (with Pi/HA Green tuned pacing) is already present "
        "at checkout — do not overwrite it in the build step."
    )
