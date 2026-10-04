"""The Conductor bootstrap runs the locked install in order and aborts at any failed step."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "scripts/bootstrap-conductor.sh"

LOCKED_SETUP = [
    "-m pip install --upgrade pip setuptools wheel",
    "-m pip install -r requirements-dev.txt",
    "-m pip install --force-reinstall --require-hashes -r requirements.txt",
    "-m pip install --no-deps -e .",
    "-m pip check",
]

# Stands in for the venv's python: answers the version probe, records pip calls.
PYTHON_STUB = """#!/bin/bash
if [ "${1:-}" = "-" ]; then cat >/dev/null; exit 0; fi
printf '%s\\n' "$*" >> "$PIP_LOG"
if [ -n "$PIP_FAIL_ON" ] && [[ "$*" == *"$PIP_FAIL_ON"* ]]; then exit 1; fi
"""


def _run_bootstrap(tmp_path: Path, *, fail_on: str = "") -> tuple[subprocess.CompletedProcess[str], list[str]]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(BOOTSTRAP, scripts / BOOTSTRAP.name)
    venv_bin = tmp_path / ".venv/bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text(PYTHON_STUB)
    (venv_bin / "python").chmod(0o755)
    (venv_bin / "activate").write_text(f'export PATH="{venv_bin}:$PATH"\n')
    # The interpreter only has to exist: an existing .venv is reused, not recreated.
    host_bin = tmp_path / "bin"
    host_bin.mkdir()
    (host_bin / "python3.11").write_text("#!/bin/bash\nexit 0\n")
    (host_bin / "python3.11").chmod(0o755)
    log = tmp_path / "pip-calls"
    result = subprocess.run(
        ["/bin/bash", str(scripts / BOOTSTRAP.name)],
        cwd=tmp_path,
        env={
            "PATH": f"{host_bin}:/usr/bin:/bin",
            "PYTHON_BIN": "python3.11",
            "PIP_LOG": str(log),
            "PIP_FAIL_ON": fail_on,
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    calls = [call for call in log.read_text().splitlines() if call != "-m pip --version"] if log.exists() else []
    return result, calls


def test_bootstrap_runs_the_locked_install_in_order(tmp_path: Path) -> None:
    result, calls = _run_bootstrap(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == LOCKED_SETUP
    assert "Environment ready." in result.stdout


@pytest.mark.parametrize(
    ("fail_on", "steps_run"),
    [("--upgrade pip", 1), ("requirements-dev.txt", 2), ("--require-hashes", 3), ("--no-deps", 4), ("pip check", 5)],
    ids=["pip-upgrade", "dev-tools", "locked-runtime", "editable-install", "pip-check"],
)
def test_bootstrap_aborts_at_the_first_failed_step(tmp_path: Path, fail_on: str, steps_run: int) -> None:
    result, calls = _run_bootstrap(tmp_path, fail_on=fail_on)

    assert result.returncode != 0
    assert calls == LOCKED_SETUP[:steps_run]
    assert "Environment ready." not in result.stdout
