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
import json
import os
import sys

if len(sys.argv) > 1 and sys.argv[1] == "runtime-json":
    message = os.getenv("FAKE_RUNTIME_FAIL", "")
    if message:
        print(message, file=sys.stderr)
        raise SystemExit(2)
    payload = {
        "bind_host": "127.0.0.1",
        "port": 8000,
        "fifo_path": os.environ["FAKE_FIFO_PATH"],
        "go_librespot_bin": os.environ["FAKE_GO_LIBRESPOT_BIN"],
        "go_librespot_config_dir": os.environ["FAKE_GO_LIBRESPOT_CONFIG_DIR"],
        "go_librespot_port": int(os.environ.get("FAKE_GO_LIBRESPOT_PORT", "3678")),
        "tmp_dir": os.environ["FAKE_TMP_DIR"],
    }
    print(json.dumps(payload))
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
    assert "FATAL: could not resolve runtime config: runtime-json exploded" in result.stderr
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
    state: dict[str, int | str] = {}
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
        _stop_pid(state.get("pid") if state else None)
        _stop_pid(drain_pid)
