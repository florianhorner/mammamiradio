"""Executable browser proof for the First Listen golden path."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from urllib.request import urlopen

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUN_CODE = Path(__file__).with_name("first_listen_browser_smoke.js")
RUNNER = ROOT / "scripts" / "player-smoke.sh"


def test_first_listen_browser_smoke_contract_is_deterministic() -> None:
    code = RUN_CODE.read_text(encoding="utf-8")
    for needle in (
        "page.setDefaultTimeout(5000)",
        "media-source://mammamiradio/live",
        "firstListenFindPlayersBtn",
        "firstListenQuickFindPlayersBtn",
        "firstListenRetryBtn",
        "firstListenChooseAnotherBtn",
        "firstListenSaveAttemptBtn",
        "/api/setup/first-listen/receipt/retry",
        "fresh install did not land on First Listen",
        "First Listen path remained buried in Motore",
        "later setup polling hijacked the operator tab",
        "completed fresh install",
        "existing install",
        "Not yet was not recorded explicitly",
        "same-speaker retry is unavailable",
        "receipt recovery sent a second playback request",
        "page reload lost server-owned receipt recovery",
        "discarded-response recovery sent a second playback request",
        "pending receipt required the speaker to stay available",
        "verification unlocked before the accepted attempt was saved",
        "Keep off fetched Home context without operator preview",
        "expired preview proof did not return focus to fresh preview",
        "enabled receipt repair claimed Home context was still off",
        "private receipt repair lost the safe live state",
        "reloaded active choice lost receipt recovery",
        "standby recovery was falsely described as on air",
        "partial Azure setup was presented as ready",
        "optional AI kept setup alert active",
        "320px/200% first-listen geometry overflowed",
        "page.on('pageerror'",
        "uncaught page errors",
    ):
        assert needle in code, f"First Listen browser smoke lost guard: {needle}"
    assert "waitForTimeout(" not in code
    assert code.index("await page.route('**/*'") < code.index("page.goto(")
    assert "await route.fallback()" in code


def test_first_listen_browser_behavior() -> None:
    base_url = os.environ.get("ADMIN_BROWSER_SMOKE_URL", "").strip().rstrip("/")
    if not base_url:
        pytest.skip("set ADMIN_BROWSER_SMOKE_URL to run the real-browser First Listen guard")

    with urlopen(f"{base_url}/admin", timeout=5) as response:
        assert response.status == 200

    environment = os.environ.copy()
    environment["PLAYER_SMOKE_URL"] = base_url
    environment["PLAYER_SMOKE_SESSION"] = f"mammamiradio-first-listen-smoke-{os.getpid()}"
    result = subprocess.run(
        [str(RUNNER), str(RUN_CODE)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"First Listen browser smoke failed ({result.returncode}):\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
