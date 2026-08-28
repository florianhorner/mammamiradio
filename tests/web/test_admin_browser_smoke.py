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
        "healthy Resume sent anything other than one normal /api/resume request",
        "cancelling assetless Resume sent a force request",
        "confirmed assetless Resume did not send exactly one /api/resume?force=true request",
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
        "late search response repopulated results after playlist revision invalidation",
        "older overlapping query overwrote the newest search ownership",
        "cross-revision pagination retained mixed search rows",
        "cross-revision pagination gave no search-again recovery",
        "fresh playlist Next posted the wrong target contract",
        "successful playlist Next left actionable rows on the submitted revision",
        "stale playlist Next hid the backend recovery message",
        "stale playlist Next showed a false success toast",
        "stale search Next hid the backend recovery message",
        "stale search Next showed a false success toast",
        "stale playlist page contaminated the authoritative cache",
        "authoritative post-restart status was rejected as a lower revision",
        "stopped producer controls stayed interactive",
        "a stopped Next-track control remained keyboard-focusable",
        "the synthetic stopped segment restarted the elapsed timer",
        "dynamic stopped control escaped synchronization",
        "resume overwrote an independent capability-disabled state",
        "empty pool claimed no source before capabilities arrived",
        "capability success waited for setup status before exposing recovery",
        "no-source recovery lost actionable golden-path guidance",
        "first capability failure claimed no source",
        "later capability failure discarded the last known recovery action",
        "empty-pool Library tools recovery was inert while stopped",
        "empty-pool setup recovery was inert while stopped",
        # While First Listen entry is required, the setup tab is one opaque
        # journey surface: the producer deck steps aside and the Station
        # controls escape is the operator's only way back. The smoke must
        # assert the escape exists and that using it restores the tab bar —
        # otherwise a broken escape traps a fresh-install operator.
        "required First Listen journey lost its Station controls escape",
        "the Station controls escape did not restore the producer desk tab bar",
        "for (const width of [320, 375, 414, 600, 768])",
        "catalogue row acquired internal horizontal overflow",
        "catalogue control escaped its row",
        "catalogue controls lost visibility or touch size",
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
        # The live /api/setup/status probe must authenticate like the real
        # dashboard client: meta-tag CSRF token in the X-Radio-CSRF-Token
        # header. A bare fetch gets 403 from the active-setup gate.
        'meta[name="mammamiradio-csrf-token"]',
        "X-Radio-CSRF-Token",
        "admin page did not embed the CSRF token meta tag",
        # A fresh install lands on the First Listen setup tab with the producer
        # console hidden; the smoke must open the producer desk like an
        # operator before asserting focus or geometry inside it.
        "showAdminTab('scaletta'",
        "setup status chips lost semantic mapping",
        "Demo Radio provider state was not ready",
        "valid AI provider was not ready",
        "unverified AI provider was not checking",
        "backed-up AI provider lost degraded truth",
        "rejected AI provider was not blocked",
        "rejected AI provider was masked by cooldown",
        "unverified fallback provider was masked by rejection",
        "valid provider cooldown was masked by rejected fallback",
        "valid fallback provider did not keep AI hosts ready",
        "admin native controls did not declare dark color scheme",
        "host descriptions stayed below readable body size",
        "untouched privacy default looked unsaved",
        "heard-session privacy recovery disappeared",
        "same-page privacy save failure disappeared",
        "forced colors hid setup status glyphs",
        "local library row did not report active tracks",
        "local library row did not show the configured music folder",
        "local library row hid scan counts",
        "local library row lost its explicit scan action",
        "local library row rebuilt upload/delete controls",
        "incomplete scan lost its recovery",
        "incomplete scan toast lost its recovery",
    ):
        assert needle in code, f"admin browser smoke lost behavior guard: {needle}"
    assert "waitForTimeout(" not in code, "admin browser smoke must use state-based waits, not timing sleeps."
    assert code.index("await page.route('**/*'") < code.index("page.goto("), (
        "same-origin-only routing must be installed before the first admin navigation"
    )
    assert "await route.fallback()" in code
    assert "blocked_off_origin_requests" in code
    listener_failure_helper = "async function exerciseListenerSongFailureRows()"
    listener_failure_invocation = "await exerciseListenerSongFailureRows();"
    assert listener_failure_helper in code
    assert listener_failure_invocation in code
    assert code.index(listener_failure_helper) < code.index(listener_failure_invocation)
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
