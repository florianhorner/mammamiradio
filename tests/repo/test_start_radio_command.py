"""The Mac launcher's first-run setup: Python selection and the locked install."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "Start Radio.command"

# What the stubbed pip is asked to run, in order (`.venv/bin/pip` is the program).
EXPECTED_CALLS = [
    "install --force-reinstall --require-hashes -r requirements.txt --quiet",
    "install --no-deps -e . --quiet",
    "check",
]
AFTER_BLOCK = "REACHED_AFTER_BLOCK"


def _launcher_block(pattern: str) -> str:
    """One first-run block of the launcher, verbatim.

    Running the whole launcher is not hermetic: it prepends Homebrew to PATH and can
    run `brew install`.
    """
    match = re.search(pattern, LAUNCHER.read_text(), re.MULTILINE | re.DOTALL)
    assert match, f"block not found in Start Radio.command: {pattern}"
    return match.group(1)


def _run(block: str, tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        # The sentinel proves whether the launcher would carry on past the block.
        ["/bin/bash", "-c", f"{block}\necho {AFTER_BLOCK}\n"],
        cwd=tmp_path,
        env=env,
        input="x",  # a key press, so only `exit 1` can end an error branch
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _run_install_block(tmp_path: Path, *, fail_on: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    pip = tmp_path / ".venv/bin/pip"
    pip.parent.mkdir(parents=True)
    pip.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$PIP_LOG"\n'
        'if [ -n "$PIP_FAIL_ON" ] && [[ "$*" == *"$PIP_FAIL_ON"* ]]; then exit 1; fi\n'
    )
    pip.chmod(0o755)
    log = tmp_path / "pip-calls"
    block = _launcher_block(r"^    (if ! \.venv/bin/pip.*?^    fi)$")
    result = _run(block, tmp_path, {"PATH": "/usr/bin:/bin", "PIP_LOG": str(log), "PIP_FAIL_ON": fail_on})
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


def _run_python_selection(tmp_path: Path, interpreters: dict[str, bool]) -> subprocess.CompletedProcess[str]:
    """Run the interpreter search with only stub interpreters on PATH.

    Each stub answers the version probe with success when it stands for 3.11+.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, supported in interpreters.items():
        stub = bin_dir / name
        stub.write_text(f"#!/bin/bash\nexit {0 if supported else 1}\n")
        stub.chmod(0o755)
    block = _launcher_block(r'^(    PY=""\n.*?^    if \[ -z "\$PY" \]; then\n.*?^    fi)$')
    return _run(f"{block}\necho SELECTED=$PY", tmp_path, {"PATH": str(bin_dir)})


def test_first_run_install_continues_when_every_step_passes(tmp_path: Path) -> None:
    result, calls = _run_install_block(tmp_path, fail_on="")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ERROR" not in result.stdout
    assert AFTER_BLOCK in result.stdout
    assert calls == EXPECTED_CALLS


@pytest.mark.parametrize(
    ("fail_on", "steps_run"),
    [("--force-reinstall", 1), ("--no-deps", 2), ("check", 3)],
    ids=["locked-runtime", "editable-install", "pip-check"],
)
def test_first_run_install_stops_at_the_first_failed_step(tmp_path: Path, fail_on: str, steps_run: int) -> None:
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=keep\n")
    (tmp_path / "music").mkdir()
    (tmp_path / "music/track.mp3").write_bytes(b"keep")

    result, calls = _run_install_block(tmp_path, fail_on=fail_on)

    assert result.returncode == 1, result.stdout + result.stderr
    assert AFTER_BLOCK not in result.stdout
    assert "ERROR: pip install failed" in result.stdout
    assert calls == EXPECTED_CALLS[:steps_run]
    # A failed install never touches the operator's keys or music.
    assert (tmp_path / ".env").read_text() == "ANTHROPIC_API_KEY=keep\n"
    assert (tmp_path / "music/track.mp3").read_bytes() == b"keep"


def test_python_selection_skips_interpreters_older_than_3_11(tmp_path: Path) -> None:
    result = _run_python_selection(tmp_path, {"python3.13": False, "python3.12": True, "python3": True})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "SELECTED=python3.12" in result.stdout


def test_python_selection_stops_with_a_way_out_when_only_old_python_exists(tmp_path: Path) -> None:
    # Apple's /usr/bin/python3 is 3.9 on current macOS; the locked runtime needs 3.11+.
    result = _run_python_selection(tmp_path, {"python3": False})

    assert result.returncode == 1, result.stdout + result.stderr
    assert "SELECTED=" not in result.stdout
    assert "needs Python 3.11 or newer" in result.stdout
    assert "then double-click again" in result.stdout


def test_python_check_runs_before_the_venv_is_created() -> None:
    # "double-click again" is only true if a too-old Python stops setup before
    # `.venv` exists; otherwise the next launch would skip setup.
    source = LAUNCHER.read_text()
    assert source.index('if [ -z "$PY" ]; then') < source.index('"$PY" -m venv .venv')


def test_launcher_is_executable_and_parses() -> None:
    # Finder only runs an executable .command file, and the launcher is outside
    # the `shellcheck scripts/*.sh` CI scope.
    assert LAUNCHER.stat().st_mode & 0o111
    result = subprocess.run(["/bin/bash", "-n", str(LAUNCHER)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
