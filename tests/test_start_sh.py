from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _make_start_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "app"
    workspace.mkdir()
    _write(workspace / "start.sh", (REPO_ROOT / "start.sh").read_text())
    (workspace / "start.sh").chmod(0o755)

    _write(workspace / "mammamiradio" / "__init__.py", '"""test package"""\n')
    _write(
        workspace / "mammamiradio" / "config.py",
        """
import os
import shlex
import sys

from mammamiradio.go_librespot_runtime import build_go_librespot_runtime, read_owned_pid

if len(sys.argv) > 1 and sys.argv[1] == "startup-env":
    message = os.getenv("FAKE_RUNTIME_FAIL", "")
    if message:
        print(message, file=sys.stderr)
        raise SystemExit(2)
    fifo = os.environ["FAKE_FIFO_PATH"]
    go_bin = os.environ["FAKE_GO_LIBRESPOT_BIN"]
    config_dir = os.environ["FAKE_GO_LIBRESPOT_CONFIG_DIR"]
    port = int(os.environ.get("FAKE_GO_LIBRESPOT_PORT", "3678"))
    tmp_dir = os.environ["FAKE_TMP_DIR"]
    glr = build_go_librespot_runtime(go_bin, config_dir, fifo, port, tmp_dir)
    owned = read_owned_pid(glr.state_file, glr.fingerprint)
    pairs = [
        ("HOST", "127.0.0.1"), ("PORT", "8000"),
        ("FIFO", fifo), ("GO_LIBRESPOT_BIN", go_bin),
        ("GO_LIBRESPOT_CONFIG_DIR", str(glr.config_dir)),
        ("GO_LIBRESPOT_PORT", str(glr.port)),
        ("TMP_DIR", str(glr.tmp_dir)),
        ("GO_LIBRESPOT_FINGERPRINT", glr.fingerprint),
        ("GO_LIBRESPOT_STATE_FILE", str(glr.state_file)),
        ("GOLIBRESPOT_OWNED_PID", str(owned) if owned else ""),
    ]
    for k, v in pairs:
        print(f"{k}={shlex.quote(v)}")
    raise SystemExit(0)

print("unsupported", file=sys.stderr)
raise SystemExit(1)
""".lstrip(),
    )
    _write(
        workspace / "mammamiradio" / "go_librespot_runtime.py",
        (REPO_ROOT / "mammamiradio" / "go_librespot_runtime.py").read_text(),
    )
    _write(
        workspace / "uvicorn.py",
        """
import os
from pathlib import Path

Path(os.environ["FAKE_UVICORN_LOG"]).write_text("uvicorn invoked\\n")
""".lstrip(),
    )

    python_wrapper = workspace / ".venv" / "bin" / "python"
    _write(
        python_wrapper,
        f"""#!/bin/sh
exec {sys.executable!r} "$@"
""",
    )
    python_wrapper.chmod(0o755)
    _write(
        workspace / ".venv" / "bin" / "activate",
        """export PATH="$PWD/.venv/bin:$PATH"
""",
    )
    return workspace


def _wait_for_pid_exit(pid: int, timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode != 0:
            return
        time.sleep(0.1)
    raise AssertionError(f"PID {pid} did not exit")


def _stop_pid(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    _wait_for_pid_exit(pid)


def _run_start_sh(workspace: Path, env: dict[str, str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    with (
        tempfile.NamedTemporaryFile("w+", delete=False) as stdout_file,
        tempfile.NamedTemporaryFile("w+", delete=False) as stderr_file,
    ):
        stdout_path = Path(stdout_file.name)
        stderr_path = Path(stderr_file.name)

    try:
        with stdout_path.open("w") as stdout_handle, stderr_path.open("w") as stderr_handle:
            result = subprocess.run(
                ["bash", "start.sh"],
                cwd=workspace,
                env=env,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                timeout=timeout,
            )
        return subprocess.CompletedProcess(
            args=result.args,
            returncode=result.returncode,
            stdout=stdout_path.read_text(),
            stderr=stderr_path.read_text(),
        )
    finally:
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)


def _wait_for_path(path: Path, timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.1)
    raise AssertionError(f"{path} was not created")


def test_start_sh_fails_closed_when_runtime_json_fails(tmp_path):
    workspace = _make_start_workspace(tmp_path)
    runtime_tmp = workspace / "runtime-tmp"
    result = _run_start_sh(
        workspace,
        {
            **os.environ,
            "FAKE_RUNTIME_FAIL": "runtime-json exploded",
            "FAKE_TMP_DIR": str(runtime_tmp),
            "FAKE_UVICORN_LOG": str(workspace / "uvicorn.log"),
        },
    )

    assert result.returncode != 0
    assert "FATAL: runtime-json exploded" in result.stderr
    assert not runtime_tmp.exists()
    assert not (workspace / "uvicorn.log").exists()


def test_start_sh_reuses_owned_go_librespot_process(tmp_path):
    workspace = _make_start_workspace(tmp_path)
    runtime_tmp = workspace / "runtime-tmp"
    config_dir = workspace / "go-librespot"
    config_dir.mkdir()
    fifo_path = workspace / "mammamiradio.pcm"
    go_librespot_log = workspace / "go-librespot-invocations.log"
    uvicorn_log = workspace / "uvicorn.log"
    launcher = workspace / "fake-go-librespot"
    _write(
        launcher,
        """#!/bin/sh
trap '' HUP
echo "$$ $*" >> "$FAKE_GO_LIBRESPOT_INVOCATIONS"
trap 'exit 0' TERM INT
while true; do sleep 1; done
""",
    )
    launcher.chmod(0o755)

    env = {
        **os.environ,
        "FAKE_TMP_DIR": str(runtime_tmp),
        "FAKE_FIFO_PATH": str(fifo_path),
        "FAKE_GO_LIBRESPOT_BIN": str(launcher),
        "FAKE_GO_LIBRESPOT_CONFIG_DIR": str(config_dir),
        "FAKE_GO_LIBRESPOT_INVOCATIONS": str(go_librespot_log),
        "FAKE_UVICORN_LOG": str(uvicorn_log),
    }

    drain_pid = None
    state: dict[str, object] = {}
    try:
        first = _run_start_sh(workspace, env)
        assert first.returncode == 0
        state_file = runtime_tmp / "go-librespot.state.json"
        state = json.loads(state_file.read_text())
        _wait_for_path(go_librespot_log)
        assert go_librespot_log.read_text().count("\n") == 1
        assert "Starting go-librespot..." in first.stdout

        second = _run_start_sh(workspace, env)
        assert second.returncode == 0
        assert go_librespot_log.read_text().count("\n") == 1
        assert f"go-librespot already running ({state['pid']})" in second.stdout

        drain_pid_file = runtime_tmp / "fifo-drain.pid"
        if drain_pid_file.exists():
            drain_pid = int(drain_pid_file.read_text().strip())
    finally:
        _stop_pid(int(state["pid"]) if state.get("pid") is not None else None)
        _stop_pid(drain_pid)


def test_start_sh_cleans_up_go_librespot_when_claim_fails(tmp_path):
    workspace = _make_start_workspace(tmp_path)
    runtime_tmp = workspace / "runtime-tmp"
    config_dir = workspace / "go-librespot"
    config_dir.mkdir()
    fifo_path = workspace / "mammamiradio.pcm"
    go_librespot_log = workspace / "go-librespot-invocations.log"
    launcher = workspace / "fake-go-librespot"
    claim_state = workspace / "claim-attempted"
    _write(
        launcher,
        """#!/bin/sh
trap '' HUP
echo "$$ $*" >> "$FAKE_GO_LIBRESPOT_INVOCATIONS"
trap 'exit 0' TERM INT
while true; do sleep 1; done
""",
    )
    launcher.chmod(0o755)
    _write(
        workspace / "mammamiradio" / "go_librespot_runtime.py",
        """
import os
import sys
from pathlib import Path
from types import SimpleNamespace


def build_go_librespot_runtime(go_bin, config_dir, fifo, port, tmp_dir):
    return SimpleNamespace(
        config_dir=Path(config_dir),
        port=port,
        tmp_dir=Path(tmp_dir),
        fingerprint="fake-fingerprint",
        state_file=Path(tmp_dir) / "go-librespot.state.json",
    )


def read_owned_pid(state_file, fingerprint):
    return None

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "claim":
        Path(os.environ["FAKE_CLAIM_MARKER"]).write_text("attempted\\n")
        raise SystemExit(7)
    raise SystemExit(0)
""".lstrip(),
    )

    result = _run_start_sh(
        workspace,
        {
            **os.environ,
            "FAKE_TMP_DIR": str(runtime_tmp),
            "FAKE_FIFO_PATH": str(fifo_path),
            "FAKE_GO_LIBRESPOT_BIN": str(launcher),
            "FAKE_GO_LIBRESPOT_CONFIG_DIR": str(config_dir),
            "FAKE_GO_LIBRESPOT_INVOCATIONS": str(go_librespot_log),
            "FAKE_CLAIM_MARKER": str(claim_state),
            "FAKE_UVICORN_LOG": str(workspace / "uvicorn.log"),
        },
    )

    assert result.returncode != 0
    assert "FATAL: failed to claim go-librespot ownership" in result.stderr
    assert claim_state.exists()
    state_file = runtime_tmp / "go-librespot.state.json"
    assert not state_file.exists()
    assert subprocess.run(["pgrep", "-f", str(launcher)], capture_output=True).returncode != 0
