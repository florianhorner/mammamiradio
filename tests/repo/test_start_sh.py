"""Tests for start.sh dev entrypoint.

Note: The go-librespot lifecycle management was removed along with Spotify
integration. start.sh is now a simple uvicorn launcher. These tests verify
basic contract behavior.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_shim(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


@pytest.fixture()
def _caddy_env(tmp_path):
    """Minimal directory that start.sh can run in with shimmed binaries.

    Provides controllable caddy, lsof, ps, and python shims so integration
    tests can exercise the runtime branches without a real venv or network.
    """
    port = 19100
    internal_port = port + 1

    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)

    # Python shim: handles the three call shapes start.sh makes
    #   1. -m mammamiradio.core.config runtime-json  → fake JSON
    #   2. -c '...json.load...'                      → delegate to real python3
    #   3. -m uvicorn ...                            → sleep (long-lived server)
    _write_shim(
        venv_bin / "python",
        textwrap.dedent(
            f"""\
            args="$*"
            if [[ "$args" == *"runtime-json"* ]]; then
                echo '{{"bind_host":"127.0.0.1","port":{port}}}'
            elif [[ "$args" == *"json.load"* ]]; then
                python3 "$@"
            elif [[ "$args" == *"uvicorn"* ]]; then
                exec sleep 60
            else
                python3 "$@"
            fi
            """
        ),
    )

    # activate: prepend venv_bin so 'python' resolves to our shim
    (venv_bin / "activate").write_text(f'export PATH="{venv_bin}:$PATH"\n')
    (tmp_path / ".env").write_text("")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    # lsof: reports a foreign PID only on internal_port, empty otherwise
    _write_shim(
        bin_dir / "lsof",
        textwrap.dedent(
            f"""\
            port="${{2#:}}"
            [[ "$port" == "{internal_port}" ]] && echo 99999 || true
            """
        ),
    )

    # ps: always returns a non-mammamiradio command line
    _write_shim(bin_dir / "ps", 'echo "/usr/bin/nginx --daemon"\n')

    # caddy: default is long-lived (tests override as needed)
    _write_shim(bin_dir / "caddy", "sleep 60\n")

    shutil.copy(REPO_ROOT / "start.sh", tmp_path / "start.sh")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"

    return {"cwd": tmp_path, "bin_dir": bin_dir, "env": env}


def test_start_sh_exists_and_is_executable():
    start_sh = REPO_ROOT / "start.sh"
    assert start_sh.exists()
    assert start_sh.stat().st_mode & 0o111  # has execute bits


def test_start_sh_sources_dotenv():
    """start.sh should source .env if it exists."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert ".env" in content
    assert "source .env" in content or ". .env" in content


def test_start_sh_uses_runtime_json():
    """start.sh should call mammamiradio.core.config runtime-json to get host/port."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert "runtime-json" in content
    assert "bind_host" in content
    # Guard the cathedral move: ensure the module path is the new nave-prefixed one.
    # If anyone reverts to `mammamiradio.config`, start.sh fails at runtime but tests
    # were silent — this assertion closes that gap.
    assert "mammamiradio.core.config" in content, (
        "start.sh must invoke `python -m mammamiradio.core.config runtime-json`. "
        "The flat `mammamiradio.config` module no longer exists post-cathedral."
    )
    assert "port" in content


def test_start_sh_launches_uvicorn():
    """start.sh should exec uvicorn with reload."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert "uvicorn" in content
    assert "--reload" in content
    assert "mammamiradio.main:app" in content


def test_start_sh_caddy_detection():
    """start.sh checks for caddy on PATH."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert "command -v caddy" in content


def test_start_sh_caddy_uses_internal_port():
    """In caddy mode, uvicorn binds to PORT+1."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert "INTERNAL_PORT" in content
    # Arithmetic expansion for PORT+1
    assert "PORT + 1" in content or "PORT+1" in content


def test_start_sh_caddy_has_flush_interval():
    """Caddyfile template must include flush_interval -1 for audio streaming."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert "flush_interval -1" in content


def test_start_sh_caddy_no_health_uri():
    """Caddyfile must NOT include health_uri (healthz/readyz return 503 in normal operation)."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert "health_uri" not in content
    assert "health_path" not in content


def test_start_sh_caddy_trap_cleanup():
    """Caddy mode must define a cleanup trap and null-guard the PIDs."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert "trap cleanup" in content or 'trap "cleanup"' in content or "trap 'cleanup'" in content
    assert "CADDY_PID" in content
    assert "UVICORN_PID" in content


def test_start_sh_no_caddy_warning_message():
    """No-caddy fallback must print an actionable install hint."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert "brew install caddy" in content


def test_start_sh_caddy_internal_host_127():
    """Backend uvicorn must bind to 127.0.0.1, not the public $HOST."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert "INTERNAL_HOST" in content
    assert "127.0.0.1" in content
    # Uvicorn uses INTERNAL_HOST, not HOST, for the backend bind
    assert '--host "$INTERNAL_HOST"' in content


def test_start_sh_caddy_identity_check_before_kill():
    """INTERNAL_PORT reclaim must verify process identity before killing."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert "mammamiradio.main:app" in content
    # Must refuse to kill unrecognised processes
    assert "refusing to kill" in content


def test_start_sh_caddy_supervises_both_pids():
    """Supervision loop must exit if caddy dies, not just wait on uvicorn."""
    content = (REPO_ROOT / "start.sh").read_text()
    # Poll loop checks both PIDs
    assert 'kill -0 "$CADDY_PID"' in content
    assert 'kill -0 "$UVICORN_PID"' in content
    # Explicit error message when caddy exits
    assert "caddy exited unexpectedly" in content


# ---------------------------------------------------------------------------
# Integration tests — actually execute start.sh with shimmed binaries
# ---------------------------------------------------------------------------


def test_start_sh_integration_identity_check_exits_on_foreign_port(_caddy_env):
    """start.sh must exit 1 when INTERNAL_PORT is held by a non-mammamiradio PID.

    Exercises the runtime branch that the grep tests cannot: lsof returns a
    PID, ps returns a non-mammamiradio command, and start.sh must refuse to
    kill it and exit non-zero with the expected message.
    """
    result = subprocess.run(
        ["bash", str(_caddy_env["cwd"] / "start.sh")],
        cwd=_caddy_env["cwd"],
        capture_output=True,
        text=True,
        timeout=10,
        env=_caddy_env["env"],
    )
    assert result.returncode == 1
    assert "refusing to kill" in result.stderr


def test_start_sh_integration_caddy_supervision_exits_when_caddy_dies(_caddy_env):
    """start.sh must exit 1 and log an error when caddy exits after startup.

    Exercises the supervision loop: caddy survives the initial 1-second check
    then exits, uvicorn stays alive, and start.sh must detect the dead caddy,
    kill uvicorn, and exit 1 with the expected message.
    """
    # Override lsof to return empty so the identity check is bypassed
    _write_shim(_caddy_env["bin_dir"] / "lsof", "")
    # Override caddy to survive the initial sleep-1 check then die
    _write_shim(_caddy_env["bin_dir"] / "caddy", "sleep 2\nexit 1\n")

    result = subprocess.run(
        ["bash", str(_caddy_env["cwd"] / "start.sh")],
        cwd=_caddy_env["cwd"],
        capture_output=True,
        text=True,
        timeout=15,
        env=_caddy_env["env"],
    )
    assert result.returncode == 1
    assert "caddy exited unexpectedly" in result.stderr
