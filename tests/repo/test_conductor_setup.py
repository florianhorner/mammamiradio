from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from itertools import groupby
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
        resolved = shutil.which(utility, path="/usr/bin:/bin")
        assert resolved is not None
        (bin_dir / utility).symlink_to(resolved)
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
    env.pop("CONDUCTOR_ROOT_PATH", None)
    env.pop("PYTHON_BIN", None)
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


def test_setup_keeps_local_workspaces_on_shared_bootstrap(tmp_path: Path, shell_env: dict[str, str]) -> None:
    setup = _install_script_fixture(tmp_path, SETUP)
    _fake_bootstrap(tmp_path)
    (tmp_path / "requirements-dev.txt").write_text("")
    shell_env["CONDUCTOR_IS_LOCAL"] = "1"

    result = _run(setup, tmp_path, env=shell_env)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "bootstrap-python").exists()
    # The shared bootstrap owns the whole install; neither wrapper installs again.
    assert not (tmp_path / ".venv/pip-call").exists()


@pytest.mark.parametrize("script", [SETUP, CLOUD_BOOTSTRAP], ids=lambda path: path.name)
def test_setup_wrappers_never_install_packages_themselves(script: Path) -> None:
    assert not _direct_package_install(script.read_text())


def _direct_package_install(source: str) -> bool:
    """Check literal installer commands; runtime tests separately prove delegation."""
    # Keep quotes until command boundaries are identified; quoted operators are data.
    quoted = r"\x27[^\x27]*\x27|\"(?:\\[\s\S]|[^\"\\])*\"|\\."
    tokens = re.findall(r"#[^\n]*|[;&|()\n]+|(?:" + quoted + r"|\\\n|[^\s;&|()\x27\"\\])+", source)
    separators = ";&|()\n"
    for separator, group in groupby(tokens, lambda token: token.startswith("#") or all(c in separators for c in token)):
        if separator:
            continue
        # Remove continuations only after comments have been separated. Single
        # quotes preserve them; elsewhere they join words without adding spaces.
        raw_words = [
            re.sub(
                quoted + r"|\\\n",
                lambda match: match[0] if match[0].startswith("'") else match[0].replace("\\\n", ""),
                word,
            )
            for word in group
        ]
        raw_words = [word for word in raw_words if word]
        for word in raw_words:
            for part in re.findall(quoted + r"|[^\x27\"\\]+", word):
                if not part.startswith(("'", "\\")):
                    for command in re.findall(r"(?<!\\)\$\(([^()]*)\)", part):
                        if _direct_package_install(command):
                            return True
        words = [shlex.split(word)[0] for word in raw_words]
        while words and (
            words[0]
            in {
                "if",
                "then",
                "elif",
                "else",
                "while",
                "until",
                "do",
                "!",
                "time",
                "sudo",
                "nohup",
                "exec",
                "command",
                "env",
            }
            or raw_words[0] == "{"
            or "=" in words[0]
        ):
            prefix = words.pop(0)
            raw_words.pop(0)
            if prefix == "env":
                while words:
                    if words[0] in {"-i", "--ignore-environment", "--"}:
                        count = 1
                    elif words[0] == "-u" and len(words) > 1:
                        count = 2
                    else:
                        break
                    del words[:count]
                    del raw_words[:count]
        if not words:
            continue
        executable, rest = Path(words[0]).name, words[1:]
        if (
            re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable)
            or raw_words[0] in {"$PYTHON_BIN", "${PYTHON_BIN}", '"$PYTHON_BIN"', '"${PYTHON_BIN}"'}
        ) and "-m" in rest:
            words = rest[rest.index("-m") + 1 :]
            executable, rest = (words[0], words[1:]) if words else ("", [])
        if re.fullmatch(r"pip(?:\d+(?:\.\d+)*)?", executable) and "install" in rest:
            return True
        if executable == "uv" and ("install" in rest or "sync" in rest):
            return True
        if executable == "eval" and _direct_package_install(" ".join(rest)):
            return True
        if executable in {"sh", "bash", "dash", "zsh"}:
            for index, flag in enumerate(rest[:-1]):
                if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", flag) and _direct_package_install(rest[index + 1]):
                    return True
    return False


@pytest.mark.parametrize(
    "source",
    [
        "pip install -r requirements.txt",
        'sh -c "pip install package"',
        "bash -c 'pip install package'",
        'eval "pip install package"',
        'sh -c "pip3 install package"',
        'bash -c "uv sync"',
        'eval "uv pip sync requirements.txt"',
        'sh -c "python -m pip --quiet install package"',
        "[ $# -gt 0 ] && pip install package",
        "n=${#args[@]}; uv sync",
        "n=${value#prefix}; uv sync",
        "printf %s foo#bar; pip install package",
        'echo "# status"; uv sync',
        "pip3\tinstall package",
        "pip3.11 --quiet install package",
        '"/path with spaces/bin/pip" install package',
        "python3 -m pip --disable-pip-version-check install package",
        "python -m pip \\\n  install package",
        "# note \\\npip install package",
        "pi\\\np install package",
        "uv\\\n sync",
        '"pi\\\np" install package',
        "uv pip install package",
        "uv pip sync requirements.txt",
        "uv --directory project sync",
        "uv sync; echo ready",
        "uv sync||exit 1",
        'sh -c "printf ready\\n\npip install package"',
        "echo ready # setup\n\npip install package",
        "if uv sync; then echo ready; fi",
        "if true; then :; else pip install package; fi",
        "{ pip install package; }",
        "time pip install package",
        "sudo pip install package",
        "nohup pip install package",
        "env FOO=bar pip install package",
        "env -i FOO=bar pip install package",
        "env --ignore-environment pip install package",
        "env -- pip install package",
        "env -u NAME pip install package",
        "env -i -u NAME FOO=bar pip install package",
        '"$PYTHON_BIN" -m pip install package',
        "${PYTHON_BIN} -m pip install package",
        'bash -ec "pip install package"',
        'bash -lc "uv sync"',
        'echo "$(pip install package)"',
    ],
)
def test_wrapper_install_guard_detects_alternate_forms(source: str) -> None:
    assert _direct_package_install(source)


@pytest.mark.parametrize(
    "source",
    [
        "# pip3 install package",
        '"$ROOT/scripts/bootstrap-conductor.sh" # uv sync belongs in the shared bootstrap',
        "python3 -c 'import sys'",
        "uv --version; echo sync",
        "[ $# -gt 0 ] # uv sync remains a comment",
        "printf %s foo#bar # pip install remains a comment",
        r"printf %s \# # uv sync remains a comment",
        'echo "It\'s ready"',
        'printf "%s\\n" "Run uv sync in the shared bootstrap"',
        'echo "Workspace\'s environment is ready\nRun the app next"',
        "uv --version # status\nsync",
        "uv --version\n\nsync",
        "printf '%s' uv sync",
        "printf '%s' ';' pip install package",
        "printf '%s' '\n' uv sync",
        "printf '%s' '|' uv sync",
        "echo '$(pip install package)'",
        r"\{ pip install package",
        r"env -i \{ pip install package",
        '"{" pip install package',
        "uv\\\nsync",
        "'pi\\\np' install package",
        '"$UNRELATED_BIN" -m pip install package',
        "${UNRELATED_BIN} -m pip install package",
        "'$PYTHON_BIN' -m pip install package",
        r"\$PYTHON_BIN -m pip install package",
    ],
)
def test_wrapper_install_guard_allows_delegation_and_comments(source: str) -> None:
    assert not _direct_package_install(source)


def test_cloud_bootstrap_selects_first_supported_interpreter(tmp_path: Path, shell_env: dict[str, str]) -> None:
    cloud = _install_script_fixture(tmp_path, CLOUD_BOOTSTRAP)
    _fake_bootstrap(tmp_path)
    _fake_python(tmp_path / "bin", "python3.12")
    shell_env["PATH"] = _path_without_system_python(tmp_path)
    shell_env.pop("PYTHON_BIN", None)

    result = _run(cloud, tmp_path, env=shell_env)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "bootstrap-python").read_text() == "python3.12"


def test_cloud_bootstrap_skips_unsupported_earlier_candidate(tmp_path: Path, shell_env: dict[str, str]) -> None:
    cloud = _install_script_fixture(tmp_path, CLOUD_BOOTSTRAP)
    _fake_bootstrap(tmp_path)
    _fake_python(tmp_path / "bin", "python3.11", supported=False)
    _fake_python(tmp_path / "bin", "python3.12")

    result = _run(cloud, tmp_path, env=shell_env)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "bootstrap-python").read_text() == "python3.12"


def test_cloud_bootstrap_rejects_pre_311_fallback_interpreter(tmp_path: Path, shell_env: dict[str, str]) -> None:
    cloud = _install_script_fixture(tmp_path, CLOUD_BOOTSTRAP)
    _fake_bootstrap(tmp_path)
    _fake_python(tmp_path / "bin", "python3", supported=False)
    shell_env["PATH"] = _path_without_system_python(tmp_path)
    shell_env.pop("PYTHON_BIN", None)

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


def test_cloud_bootstrap_rejects_unsupported_explicit_interpreter(tmp_path: Path, shell_env: dict[str, str]) -> None:
    cloud = _install_script_fixture(tmp_path, CLOUD_BOOTSTRAP)
    _fake_bootstrap(tmp_path)
    _fake_python(tmp_path / "bin", "custom-python", supported=False)
    shell_env["PYTHON_BIN"] = "custom-python"

    result = _run(cloud, tmp_path, env=shell_env)

    assert result.returncode == 1
    assert "Missing a Python 3.11+ interpreter" in result.stderr
    assert not (tmp_path / "bootstrap-python").exists()


def test_cloud_bootstrap_leaves_the_install_to_the_shared_bootstrap(tmp_path: Path, shell_env: dict[str, str]) -> None:
    cloud = _install_script_fixture(tmp_path, CLOUD_BOOTSTRAP)
    _fake_bootstrap(tmp_path)
    _fake_python(tmp_path / "bin", "python3.12")
    (tmp_path / "requirements-dev.txt").write_text("")

    result = _run(cloud, tmp_path, env=shell_env)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "bootstrap-python").exists()
    assert not (tmp_path / ".venv/pip-call").exists()


def test_cloud_bootstrap_preserves_same_root_env_file(tmp_path: Path, shell_env: dict[str, str]) -> None:
    cloud = _install_script_fixture(tmp_path, CLOUD_BOOTSTRAP)
    _fake_bootstrap(tmp_path)
    _fake_python(tmp_path / "bin", "python3.12")
    workspace_env = tmp_path / ".env"
    workspace_env.write_text("MAMMAMIRADIO_TEST=1\n")
    shell_env["CONDUCTOR_ROOT_PATH"] = str(tmp_path)

    result = _run(cloud, tmp_path, env=shell_env)

    assert result.returncode == 0, result.stderr
    assert not workspace_env.is_symlink()
    assert workspace_env.read_text() == "MAMMAMIRADIO_TEST=1\n"


def test_cloud_bootstrap_preserves_same_root_env_symlink(tmp_path: Path, shell_env: dict[str, str]) -> None:
    cloud = _install_script_fixture(tmp_path, CLOUD_BOOTSTRAP)
    _fake_bootstrap(tmp_path)
    _fake_python(tmp_path / "bin", "python3.12")
    shared_env = tmp_path / "shared.env"
    shared_env.write_text("MAMMAMIRADIO_TEST=1\n")
    workspace_env = tmp_path / ".env"
    workspace_env.symlink_to(shared_env.name)
    shell_env["CONDUCTOR_ROOT_PATH"] = str(tmp_path)

    result = _run(cloud, tmp_path, env=shell_env)

    assert result.returncode == 0, result.stderr
    assert workspace_env.is_symlink()
    assert workspace_env.readlink() == Path("shared.env")
    assert workspace_env.read_text() == "MAMMAMIRADIO_TEST=1\n"


def test_cloud_bootstrap_rejects_missing_explicit_interpreter(tmp_path: Path, shell_env: dict[str, str]) -> None:
    cloud = _install_script_fixture(tmp_path, CLOUD_BOOTSTRAP)
    shell_env["PYTHON_BIN"] = "does-not-exist"

    result = _run(cloud, tmp_path, env=shell_env)

    assert result.returncode == 1
    assert "Missing a Python 3.11+ interpreter" in result.stderr
