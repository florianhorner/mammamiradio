"""Smoke test: every user-facing script in scripts/ must respond to --help.

Catches the DX paper cut where `scripts/foo.sh --help` runs the actual
validation instead of printing usage.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPTS = [
    "validate-addon.sh",
    "pre-release-check.sh",
    "check-release-cooldown.sh",
    "bootstrap-conductor.sh",
]


@pytest.mark.parametrize("flag", ["-h", "--help"])
@pytest.mark.parametrize("script", SCRIPTS)
def test_script_responds_to_help(script: str, flag: str) -> None:
    """Each script must exit 0 and print non-empty Usage text on -h / --help."""
    path = Path(__file__).resolve().parents[1] / "scripts" / script
    assert path.exists(), f"{path} missing"

    result = subprocess.run(
        ["bash", str(path), flag],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, (
        f"{script} {flag} exited {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    combined = (result.stdout + result.stderr).lower()
    assert "usage" in combined, (
        f"{script} {flag} did not print 'Usage' anywhere\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
