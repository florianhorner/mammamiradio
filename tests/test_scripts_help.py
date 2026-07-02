"""Smoke test: every user-facing script in scripts/ must respond to --help.

Catches the DX paper cut where `scripts/foo.sh --help` runs the actual
validation instead of printing usage.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = [
    "validate-addon.sh",
    "pre-release-check.sh",
    "check-release-cooldown.sh",
    "bootstrap-conductor.sh",
]

# User-facing Python CLIs in scripts/ — same --help contract, run via the
# interpreter rather than bash.
PY_SCRIPTS = [
    "generate_welcome_clips.py",
    "validate-release-beat.py",
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


@pytest.mark.parametrize("flag", ["-h", "--help"])
@pytest.mark.parametrize("script", PY_SCRIPTS)
def test_python_script_responds_to_help(script: str, flag: str) -> None:
    """Each user-facing Python CLI must exit 0 and print Usage on -h / --help."""
    path = Path(__file__).resolve().parents[1] / "scripts" / script
    assert path.exists(), f"{path} missing"

    result = subprocess.run(
        [sys.executable, str(path), flag],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, (
        f"{script} {flag} exited {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    combined = (result.stdout + result.stderr).lower()
    assert "usage" in combined, (
        f"{script} {flag} did not print 'Usage' anywhere\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
