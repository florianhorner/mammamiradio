from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK_COMMIT_MSG = ROOT / "scripts" / "check-commit-msg.sh"
CHECK_VERSION_SYNC = ROOT / "scripts" / "check-version-sync.sh"
CHECK_CHANGELOG_SYNC = ROOT / "scripts" / "check-changelog-sync.sh"
VALIDATE_ADDON = ROOT / "scripts" / "validate-addon.sh"


def _run(cmd: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _init_git_repo(path: Path) -> None:
    _run(["git", "init", "-q"], cwd=path)
    _run(["git", "config", "user.email", "tests@example.com"], cwd=path)
    _run(["git", "config", "user.name", "Test User"], cwd=path)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=path)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_check_commit_msg_accepts_conventional_prefix(tmp_path: Path) -> None:
    msg_file = tmp_path / "message.txt"
    _write(msg_file, "fix(addon): enforce staged version sync\n")

    result = _run(["bash", str(CHECK_COMMIT_MSG), str(msg_file)], cwd=tmp_path)

    assert result.returncode == 0


def test_check_commit_msg_rejects_non_conventional_prefix(tmp_path: Path) -> None:
    msg_file = tmp_path / "message.txt"
    _write(msg_file, "update addon hook docs\n")

    result = _run(["bash", str(CHECK_COMMIT_MSG), str(msg_file)], cwd=tmp_path)

    assert result.returncode == 1
    assert "Commit message must start with a conventional prefix" in result.stdout


def test_check_version_sync_uses_staged_versions(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", 'version: "1.0.0"\n')
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.0.0"\n')
    _run(["git", "add", "ha-addon/mammamiradio/config.yaml", "pyproject.toml"], cwd=tmp_path)
    _run(["git", "commit", "-qm", "init"], cwd=tmp_path)

    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", 'version: "1.1.0"\n')
    _run(["git", "add", "ha-addon/mammamiradio/config.yaml"], cwd=tmp_path)
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.1.0"\n')

    result = _run(["bash", str(CHECK_VERSION_SYNC)], cwd=tmp_path)

    assert result.returncode == 1
    assert "ERROR: Version mismatch!" in result.stdout
    assert "ha-addon/mammamiradio/config.yaml: 1.1.0" in result.stdout
    assert "pyproject.toml: 1.0.0" in result.stdout


def test_check_version_sync_passes_when_index_matches(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", 'version: "1.0.0"\n')
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.0.0"\n')
    _run(["git", "add", "ha-addon/mammamiradio/config.yaml", "pyproject.toml"], cwd=tmp_path)
    _run(["git", "commit", "-qm", "init"], cwd=tmp_path)

    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", 'version: "1.1.0"\n')
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.1.0"\n')
    _run(["git", "add", "ha-addon/mammamiradio/config.yaml", "pyproject.toml"], cwd=tmp_path)

    result = _run(["bash", str(CHECK_VERSION_SYNC)], cwd=tmp_path)

    assert result.returncode == 0
    assert result.stdout == ""


def test_check_changelog_sync_requires_both_changelogs_on_version_bump(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", 'version: "1.0.0"\n')
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.0.0"\n')
    _write(tmp_path / "CHANGELOG.md", "# Changelog\n")
    _write(tmp_path / "ha-addon/mammamiradio/CHANGELOG.md", "# Changelog\n")
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-qm", "init"], cwd=tmp_path)

    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", 'version: "1.1.0"\n')
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.1.0"\n')
    _write(tmp_path / "CHANGELOG.md", "# Changelog\n## [1.1.0]\n")
    _run(["git", "add", "ha-addon/mammamiradio/config.yaml", "pyproject.toml", "CHANGELOG.md"], cwd=tmp_path)

    result = _run(["bash", str(CHECK_CHANGELOG_SYNC)], cwd=tmp_path)

    assert result.returncode == 1
    assert "ha-addon/mammamiradio/CHANGELOG.md" in result.stdout


def test_check_changelog_sync_passes_when_both_changelogs_staged(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", 'version: "1.0.0"\n')
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.0.0"\n')
    _write(tmp_path / "CHANGELOG.md", "# Changelog\n")
    _write(tmp_path / "ha-addon/mammamiradio/CHANGELOG.md", "# Changelog\n")
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-qm", "init"], cwd=tmp_path)

    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", 'version: "1.1.0"\n')
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.1.0"\n')
    _write(tmp_path / "CHANGELOG.md", "# Changelog\n## [1.1.0]\n")
    _write(tmp_path / "ha-addon/mammamiradio/CHANGELOG.md", "# Changelog\n## 1.1.0\n")
    _run(
        [
            "git",
            "add",
            "ha-addon/mammamiradio/config.yaml",
            "pyproject.toml",
            "CHANGELOG.md",
            "ha-addon/mammamiradio/CHANGELOG.md",
        ],
        cwd=tmp_path,
    )

    result = _run(["bash", str(CHECK_CHANGELOG_SYNC)], cwd=tmp_path)

    assert result.returncode == 0


def _create_validate_addon_repo(
    tmp_path: Path, *, streamer_body: str, broken_dotvenv_python: bool = False
) -> dict[str, str]:
    _init_git_repo(tmp_path)
    _run(["git", "remote", "add", "origin", "https://github.com/florianhorner/fakeitaliradio.git"], cwd=tmp_path)

    _write(
        tmp_path / "ha-addon/mammamiradio/config.yaml",
        "\n".join(
            [
                'version: "1.1.0"',
                "image: ghcr.io/florianhorner/mammamiradio-addon-{arch}",
                "timeout: 300",
                "host_network: true",
                "ingress_port: 8000",
                "schema:",
                "  anthropic_api_key: password?",
                "  openai_api_key: password?",
                "  station_name: str?",
                "  claude_model: str?",
                "",
            ]
        ),
    )
    _write(
        tmp_path / "ha-addon/mammamiradio/rootfs/run.sh",
        "\n".join(
            [
                "#!/usr/bin/env sh",
                'export MAMMAMIRADIO_PORT="8000"',
                "anthropic_api_key=${anthropic_api_key:-}",
                "openai_api_key=${openai_api_key:-}",
                "station_name=${station_name:-}",
                "claude_model=${claude_model:-}",
                "",
            ]
        ),
    )
    _write(tmp_path / "ha-addon/mammamiradio/Dockerfile", "FROM scratch\nCOPY app /app\n")
    _write(tmp_path / "ha-addon/mammamiradio/build.yaml", "build_from: {}\n")
    _write(
        tmp_path / "ha-addon/mammamiradio/translations/en.yaml",
        "\n".join(
            [
                "configuration:",
                "  anthropic_api_key: key",
                "  openai_api_key: key",
                "  station_name: key",
                "  claude_model: key",
                "",
            ]
        ),
    )
    _write(tmp_path / "mammamiradio/__init__.py", "")
    _write(tmp_path / "mammamiradio/streamer.py", streamer_body)
    _write(tmp_path / "radio.toml", "[station]\nname = 'Test'\n")
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.1.0"\n')
    _write(tmp_path / "repository.yaml", "name: test\n")

    if broken_dotvenv_python:
        _write(tmp_path / ".venv/bin/python3", "#!/usr/bin/env sh\nexit 1\n")
        os.chmod(tmp_path / ".venv/bin/python3", 0o755)

    bin_dir = tmp_path / "bin"
    _write(bin_dir / "gh", "#!/usr/bin/env sh\nexit 1\n")
    os.chmod(bin_dir / "gh", 0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{Path(sys.executable).parent}:{env['PATH']}"
    return env


def test_validate_addon_rejects_single_quoted_js_rewrites(tmp_path: Path) -> None:
    env = _create_validate_addon_repo(
        tmp_path,
        streamer_body="""
def _inject_ingress_prefix(html: str, prefix: str) -> str:
    html = html.replace("'/api/hosts'", f"'{prefix}/api/hosts'")
    return html
""".strip()
        + "\n",
    )

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert result.returncode != 0
    assert "rewrites single-quoted JS path" in result.stdout


def test_validate_addon_allows_service_worker_rewrite(tmp_path: Path) -> None:
    env = _create_validate_addon_repo(
        tmp_path,
        streamer_body="""
def _inject_ingress_prefix(html: str, prefix: str) -> str:
    html = html.replace('href="/listen"', f'href="{prefix}/listen"')
    html = html.replace("'/sw.js'", f"'{prefix}/sw.js'")
    return html
""".strip()
        + "\n",
    )

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert result.returncode == 0
    assert "Ingress prefix injection only rewrites safe patterns" in result.stdout


def test_validate_addon_falls_back_when_dotvenv_python_is_broken(tmp_path: Path) -> None:
    env = _create_validate_addon_repo(
        tmp_path,
        streamer_body="""
def _inject_ingress_prefix(html: str, prefix: str) -> str:
    html = html.replace('href="/listen"', f'href="{prefix}/listen"')
    return html
""".strip()
        + "\n",
        broken_dotvenv_python=True,
    )

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert result.returncode == 0
    assert "radio.toml is valid TOML" in result.stdout


def test_addon_dockerfile_does_not_drop_root_before_supervisor_mounts() -> None:
    dockerfile = (ROOT / "ha-addon" / "mammamiradio" / "Dockerfile").read_text()

    assert "USER radio" not in dockerfile


def test_port_is_consistent_across_addon_files() -> None:
    """config.yaml ingress_port, run.sh MAMMAMIRADIO_PORT, and config.py default must all match.

    Port drift across these three files causes silent HA add-on breakage:
    the supervisor health-check points at one port, uvicorn binds another.
    """
    import re

    config_yaml = (ROOT / "ha-addon" / "mammamiradio" / "config.yaml").read_text()
    run_sh = (ROOT / "ha-addon" / "mammamiradio" / "rootfs" / "run.sh").read_text()
    config_py = (ROOT / "mammamiradio" / "config.py").read_text()

    # ingress_port: 8000
    ingress_match = re.search(r"^ingress_port:\s*(\d+)", config_yaml, re.MULTILINE)
    assert ingress_match, "ingress_port not found in config.yaml"
    ingress_port = ingress_match.group(1)

    # export MAMMAMIRADIO_PORT="8000"
    env_match = re.search(r'MAMMAMIRADIO_PORT=["\']?(\d+)["\']?', run_sh)
    assert env_match, "MAMMAMIRADIO_PORT not found in run.sh"
    env_port = env_match.group(1)

    # --port 8000  (uvicorn CLI flag)
    uvicorn_match = re.search(r"--port\s+(\d+)", run_sh)
    assert uvicorn_match, "--port not found in run.sh"
    uvicorn_port = uvicorn_match.group(1)

    # port: int = 8000  (config.py dataclass default)
    py_match = re.search(r"port:\s*int\s*=\s*(\d+)", config_py)
    assert py_match, "port default not found in config.py"
    py_port = py_match.group(1)

    assert ingress_port == env_port == uvicorn_port == py_port, (
        f"Port mismatch: config.yaml ingress_port={ingress_port}, "
        f"run.sh MAMMAMIRADIO_PORT={env_port}, "
        f"run.sh --port={uvicorn_port}, "
        f"config.py default={py_port}"
    )


def test_dockerfile_port_matches_config() -> None:
    """Standalone Dockerfile ENV, EXPOSE, and CMD --port must match config.py default.

    The Dockerfile hardcodes port in three places independently of the HA addon.
    If someone bumps the default port in config.py without updating the Dockerfile,
    the standalone container silently binds the wrong port.
    """
    import re

    dockerfile = (ROOT / "Dockerfile").read_text()
    config_py = (ROOT / "mammamiradio" / "config.py").read_text()

    # ENV MAMMAMIRADIO_PORT=8000
    env_match = re.search(r"ENV MAMMAMIRADIO_PORT=(\d+)", dockerfile)
    assert env_match, "ENV MAMMAMIRADIO_PORT not found in Dockerfile"
    env_port = env_match.group(1)

    # EXPOSE 8000
    expose_match = re.search(r"^EXPOSE\s+(\d+)", dockerfile, re.MULTILINE)
    assert expose_match, "EXPOSE not found in Dockerfile"
    expose_port = expose_match.group(1)

    # CMD [..., "--port", "8000"]
    cmd_match = re.search(r"--port[\"',\s]+(\d+)", dockerfile)
    assert cmd_match, "--port not found in Dockerfile CMD"
    cmd_port = cmd_match.group(1)

    # port: int = 8000  (config.py dataclass default)
    py_match = re.search(r"port:\s*int\s*=\s*(\d+)", config_py)
    assert py_match, "port default not found in config.py"
    py_port = py_match.group(1)

    assert env_port == expose_port == cmd_port == py_port, (
        f"Port mismatch in Dockerfile vs config.py: "
        f"ENV MAMMAMIRADIO_PORT={env_port}, "
        f"EXPOSE={expose_port}, "
        f"CMD --port={cmd_port}, "
        f"config.py default={py_port}"
    )
