from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLOUD_BOOTSTRAP = ROOT / "scripts/conductor-cloud-bootstrap.sh"
SETUP = ROOT / "scripts/conductor-setup.sh"


def _install_script_fixture(tmp_path: Path, script: Path) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(script, scripts / script.name)
    (scripts / script.name).chmod(0o755)
    return scripts / script.name


def _fake_bootstrap(tmp_path: Path) -> None:
    bootstrap = tmp_path / "scripts/bootstrap-conductor.sh"
    bootstrap.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "mkdir -p .venv/bin\n"
        "printf '%s' \"${PYTHON_BIN:-unset}\" > bootstrap-python\n"
        "printf '%s\\n' 'export PATH=\"$PWD/.venv/bin:$PATH\"' > .venv/bin/activate\n"
        "printf '%s\\n' '#!/bin/bash' 'printf \"%s\" \"$*\" > .venv/pip-call' > .venv/bin/python\n"
        "chmod +x .venv/bin/python\n"
    )
    bootstrap.chmod(0o755)


def _fake_python(bin_dir: Path, name: str, *, supported: bool = True) -> None:
    executable = bin_dir / name
    probe_result = "exit 0" if supported else "exit 1"
    executable.write_text(f'#!/bin/bash\nif [ "${{1:-}}" = "-c" ]; then\n  {probe_result}\nfi\nexit 0\n')
    executable.chmod(0o755)


def _path_without_system_python(tmp_path: Path) -> str:
    """Provide the POSIX utilities used by the script without system Python."""
    bin_dir = tmp_path / "bin"
    for utility in ("chmod", "dirname", "ln", "mkdir", "pwd"):
        (bin_dir / utility).symlink_to(Path("/usr/bin") / utility)
    return str(bin_dir)


def _run(script: Path, cwd: Path, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(script)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.fixture()
def shell_env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "CONDUCTOR_IS_LOCAL": "0",
        }
    )
    return env


def test_setup_dispatches_cloud_workspaces_to_cloud_bootstrap(tmp_path: Path, shell_env: dict[str, str]) -> None:
    setup = _install_script_fixture(tmp_path, SETUP)
    cloud = tmp_path / "scripts/conductor-cloud-bootstrap.sh"
    cloud.write_text("#!/usr/bin/env bash\nprintf cloud > cloud-dispatch\n")
    cloud.chmod(0o755)

    result = _run(setup, tmp_path, env=shell_env)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "cloud-dispatch").read_text() == "cloud"


def test_cloud_bootstrap_selects_first_supported_interpreter(tmp_path: Path, shell_env: dict[str, str]) -> None:
    cloud = _install_script_fixture(tmp_path, CLOUD_BOOTSTRAP)
    _fake_bootstrap(tmp_path)
    _fake_python(tmp_path / "bin", "python3.12")
    shell_env["PATH"] = _path_without_system_python(tmp_path)

    result = _run(cloud, tmp_path, env=shell_env)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "bootstrap-python").read_text() == "python3.12"


def test_cloud_bootstrap_rejects_pre_311_fallback_interpreter(tmp_path: Path, shell_env: dict[str, str]) -> None:
    cloud = _install_script_fixture(tmp_path, CLOUD_BOOTSTRAP)
    _fake_bootstrap(tmp_path)
    _fake_python(tmp_path / "bin", "python3", supported=False)
    shell_env["PATH"] = _path_without_system_python(tmp_path)

    result = _run(cloud, tmp_path, env=shell_env)

    assert result.returncode == 1
    assert "Missing a Python 3.11+ interpreter" in result.stderr
    assert not (tmp_path / "bootstrap-python").exists()


def test_cloud_bootstrap_honors_explicit_interpreter(tmp_path: Path, shell_env: dict[str, str]) -> None:
    cloud = _install_script_fixture(tmp_path, CLOUD_BOOTSTRAP)
    _fake_bootstrap(tmp_path)
    _fake_python(tmp_path / "bin", "custom-python")
    shell_env["PYTHON_BIN"] = "custom-python"

    result = _run(cloud, tmp_path, env=shell_env)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "bootstrap-python").read_text() == "custom-python"


def test_cloud_bootstrap_installs_development_requirements_after_activation(
    tmp_path: Path, shell_env: dict[str, str]
) -> None:
    cloud = _install_script_fixture(tmp_path, CLOUD_BOOTSTRAP)
    _fake_bootstrap(tmp_path)
    _fake_python(tmp_path / "bin", "python3.12")
    (tmp_path / "requirements-dev.txt").write_text("pytest\n")

    result = _run(cloud, tmp_path, env=shell_env)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".venv/pip-call").read_text() == "-m pip install -r requirements-dev.txt"


def test_cloud_bootstrap_links_workspace_env_when_root_env_is_available(
    tmp_path: Path, shell_env: dict[str, str]
) -> None:
    cloud = _install_script_fixture(tmp_path, CLOUD_BOOTSTRAP)
    _fake_bootstrap(tmp_path)
    _fake_python(tmp_path / "bin", "python3.12")
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_env = source_root / ".env"
    source_env.write_text("MAMMAMIRADIO_TEST=1\n")
    shell_env["CONDUCTOR_ROOT_PATH"] = str(source_root)

    result = _run(cloud, tmp_path, env=shell_env)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".env").is_symlink()
    assert (tmp_path / ".env").read_text() == source_env.read_text()


def test_cloud_bootstrap_rejects_missing_explicit_interpreter(tmp_path: Path, shell_env: dict[str, str]) -> None:
    cloud = _install_script_fixture(tmp_path, CLOUD_BOOTSTRAP)
    shell_env["PYTHON_BIN"] = "does-not-exist"

    result = _run(cloud, tmp_path, env=shell_env)

    assert result.returncode == 1
    assert "Missing a Python 3.11+ interpreter" in result.stderr
