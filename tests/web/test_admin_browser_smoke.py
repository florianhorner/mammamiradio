"""Executable, opt-in browser guard for the admin producer desk."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
from pathlib import Path
from urllib.request import urlopen

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUN_CODE = Path(__file__).with_name("admin_browser_smoke.js")
CLI_VERSION_FILE = ROOT / ".playwright-cli-version"
CLI_VERSION = CLI_VERSION_FILE.read_text(encoding="utf-8").strip()
COMMAND_TIMEOUT_SEC = 90
CLOSE_TIMEOUT_SEC = 15


def _playwright_cli() -> list[str]:
    override = os.environ.get("PLAYWRIGHT_CLI", "").strip()
    if override:
        return shlex.split(override)
    npx = shutil.which("npx")
    if not npx:
        pytest.fail("admin browser smoke needs npx or PLAYWRIGHT_CLI")
    return [npx, "--yes", "--package", f"@playwright/cli@{CLI_VERSION}", "playwright-cli"]


def _signal_process_group(process: subprocess.Popen[str], sig: signal.Signals) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def _run(
    command: list[str],
    *,
    cwd: Path,
    check: bool = True,
    timeout: int = COMMAND_TIMEOUT_SEC,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        _signal_process_group(process, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            _signal_process_group(process, signal.SIGKILL)
            stdout, stderr = process.communicate()
        stderr = f"{stderr}\ncommand timed out after {timeout}s"
        result = subprocess.CompletedProcess(command, 124, stdout, stderr)
    except BaseException:
        _signal_process_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _signal_process_group(process, signal.SIGKILL)
            process.wait()
        raise
    if check and result.returncode:
        pytest.fail(f"command failed ({result.returncode}): {' '.join(command)}\n{result.stdout}\n{result.stderr}")
    return result


def _assert_smoke_result(command_result: subprocess.CompletedProcess[str]) -> None:
    try:
        payload = json.loads(command_result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"admin browser smoke returned invalid CLI JSON: {exc}\n{command_result.stdout}")
    if payload.get("isError"):
        pytest.fail(f"admin browser assertions failed: {payload.get('error', 'unknown error')}")
    try:
        smoke_result = json.loads(payload["result"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        pytest.fail(f"admin browser smoke returned no structured result: {exc}\n{payload}")
    if not isinstance(smoke_result, dict) or smoke_result.get("ok") is not True:
        pytest.fail(f"admin browser smoke did not report success: {smoke_result!r}")


def test_admin_browser_smoke_contract_is_bounded() -> None:
    code = RUN_CODE.read_text(encoding="utf-8")
    python_code = Path(__file__).read_text(encoding="utf-8")
    for needle in (
        "page.setDefaultTimeout(5000)",
        "building ahead · station paused",
        "building ahead · waiting for listeners",
        "failed poll kept a stale production-state label",
        "HTTP error was treated as a valid production status",
        "listener-request failure replaced healthy production state",
        "hosts failure replaced healthy production state",
        "server-seeded stopped state exposed Stop on first paint",
        "never-settling listener request blocked authoritative status",
        "never-settling hosts request blocked authoritative status",
        "stale status success overwrote the newest response",
        "stale status failure showed a false reconnecting state",
        "declined skip showed success instead of the backend error",
        "network-failed skip did not show the offline recovery message",
        "stopped producer controls stayed interactive",
        "a stopped Next-track control remained keyboard-focusable",
        "the synthetic stopped segment restarted the elapsed timer",
        "dynamic stopped control escaped synchronization",
        "resume overwrote an independent capability-disabled state",
        "for (const width of [320, 375])",
        "geometry.airNext.length === 4",
        "geometry.coreTransport.length === 3",
        "control label clipped internally",
        "normal motion exposed a future empty speaker row",
        "reduced motion left typewriter rows hidden or animated",
        "recent production text is still faded by ancestor opacity",
        "blockedOffOriginRequests",
        "page.on('pageerror'",
        "uncaught page errors",
    ):
        assert needle in code, f"admin browser smoke lost behavior guard: {needle}"
    assert "waitForTimeout(" not in code, "admin browser smoke must use state-based waits, not timing sleeps."
    assert code.index("await page.route('**/*'") < code.index("page.goto("), (
        "same-origin-only routing must be installed before the first admin navigation"
    )
    assert "await route.fallback()" in code
    assert "blocked_off_origin_requests" in code
    assert CLI_VERSION == "0.1.17"
    assert '"--json", "run-code"' in python_code
    assert 'payload.get("isError")' in python_code
    assert 'smoke_result.get("ok") is not True' in python_code


def test_admin_browser_behavior(tmp_path: Path) -> None:
    base_url = os.environ.get("ADMIN_BROWSER_SMOKE_URL", "").strip().rstrip("/")
    if not base_url:
        pytest.skip("set ADMIN_BROWSER_SMOKE_URL to run the real-browser admin guard")

    with urlopen(f"{base_url}/admin", timeout=5) as response:
        assert response.status == 200

    cli = _playwright_cli()
    session = f"mammamiradio-admin-smoke-{os.getpid()}"
    try:
        _run([*cli, "--session", session, "open", f"about:blank#{base_url}"], cwd=tmp_path)
        smoke_result = _run(
            [*cli, "--session", session, "--json", "run-code", "--filename", str(RUN_CODE)], cwd=tmp_path
        )
        _assert_smoke_result(smoke_result)
    finally:
        _run([*cli, "--session", session, "close"], cwd=tmp_path, check=False, timeout=CLOSE_TIMEOUT_SEC)
