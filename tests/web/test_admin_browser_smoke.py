"""Executable, opt-in browser guard for the admin producer desk."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from urllib.request import urlopen

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUN_CODE = Path(__file__).with_name("admin_browser_smoke.js")
RUNNER = ROOT / "scripts" / "player-smoke.sh"
CLI_VERSION_FILE = ROOT / ".playwright-cli-version"
CLI_VERSION = CLI_VERSION_FILE.read_text(encoding="utf-8").strip()


def test_admin_browser_smoke_contract_is_bounded() -> None:
    code = RUN_CODE.read_text(encoding="utf-8")
    python_code = Path(__file__).read_text(encoding="utf-8")
    runner = RUNNER.read_text(encoding="utf-8")
    for needle in (
        "page.setDefaultTimeout(5000)",
        "building ahead · station paused",
        "building ahead · waiting for listeners",
        "failed poll kept a stale production-state label",
        "failed poll did not offer a manual retry control",
        "failed poll did not announce the delayed status through the persistent live region",
        "paused fallback made Try again now unavailable",
        "failed manual retry left Try again now busy or unavailable",
        "manual retry did not report a busy state while polling",
        "manual retry left the fallback control after recovery",
        "manual retry left the delayed-status announcement behind after recovery",
        "repeated failed polls re-announced the same status outage",
        "a recovered later outage was not announced once",
        "malformed production payload did not switch to update-delayed state",
        "malformed production payload kept stale production copy",
        "valid status did not recover from a malformed production payload",
        "HTTP error was treated as a valid production status",
        "concurrent automatic failure cleared the busy state of an in-flight manual retry",
        "listener-request failure replaced healthy production state",
        "hosts failure replaced healthy production state",
        "server-seeded stopped state exposed Stop on first paint",
        "never-settling listener request blocked authoritative status",
        "never-settling hosts request blocked authoritative status",
        "stale status success overwrote the newest response",
        "stale status failure showed a false update-delayed state",
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
        "recoveryFits",
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
    assert 'RUNNER = ROOT / "scripts" / "player-smoke.sh"' in python_code
    assert "subprocess.run" in python_code
    assert 'RUN_CODE_FILE="${1:-$REPO_ROOT/scripts/player-smoke.js}"' in runner
    assert 'run-code --filename "$RUN_CODE_FILE"' in runner


def test_admin_browser_behavior() -> None:
    base_url = os.environ.get("ADMIN_BROWSER_SMOKE_URL", "").strip().rstrip("/")
    if not base_url:
        pytest.skip("set ADMIN_BROWSER_SMOKE_URL to run the real-browser admin guard")

    with urlopen(f"{base_url}/admin", timeout=5) as response:
        assert response.status == 200

    environment = os.environ.copy()
    environment["PLAYER_SMOKE_URL"] = base_url
    environment["PLAYER_SMOKE_SESSION"] = f"mammamiradio-admin-smoke-{os.getpid()}"
    result = subprocess.run(
        [str(RUNNER), str(RUN_CODE)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"admin browser smoke failed ({result.returncode}):\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
