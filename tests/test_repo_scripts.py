from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK_COMMIT_MSG = ROOT / "scripts" / "check-commit-msg.sh"
CHECK_VERSION_SYNC = ROOT / "scripts" / "check-version-sync.sh"
VALIDATE_ADDON = ROOT / "scripts" / "validate-addon.sh"


def _run(cmd: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _init_git_repo(path: Path) -> None:
    _run(["git", "init", "-q"], cwd=path)
    _run(["git", "config", "user.email", "tests@example.com"], cwd=path)
    _run(["git", "config", "user.name", "Test User"], cwd=path)


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
                "  spotify_client_id: password?",
                "  spotify_client_secret: password?",
                "  station_name: str?",
                "  claude_model: str?",
                "  playlist_spotify_url: str?",
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
                "spotify_client_id=${spotify_client_id:-}",
                "spotify_client_secret=${spotify_client_secret:-}",
                "station_name=${station_name:-}",
                "claude_model=${claude_model:-}",
                "playlist_spotify_url=${playlist_spotify_url:-}",
                "",
            ]
        ),
    )
    _write(tmp_path / "ha-addon/mammamiradio/Dockerfile", "FROM scratch\nCOPY app /app\n")
    _write(tmp_path / "ha-addon/mammamiradio/go-librespot-config.yml", "device_name: test\n")
    _write(tmp_path / "ha-addon/mammamiradio/build.yaml", "build_from: {}\n")
    _write(
        tmp_path / "ha-addon/mammamiradio/translations/en.yaml",
        "\n".join(
            [
                "configuration:",
                "  anthropic_api_key: key",
                "  spotify_client_id: key",
                "  spotify_client_secret: key",
                "  station_name: key",
                "  claude_model: key",
                "  playlist_spotify_url: key",
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
