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
        "runCalmJourneySmoke",
        "required First Listen path is not four stages",
        "optional AI received aria-current",
        "unfinished journey does not have one obvious action",
        "completed journey forced a current step",
        "firstListenPlayBtn",
        "firstListenStationAudio",
        "firstListenRetryBtn",
        "firstListenRetestBtn",
        "firstListenSaveAttemptBtn",
        "/api/setup/first-listen/listener-confirm",
        "guide-audio-lifecycle",
        "welcome guide did not request its local audio asset",
        "failed guide retained its terminal audio URL",
        "guide retry did not request a fresh audio response",
        "ingress guide escaped its base path",
        "fresh install did not land on First Listen",
        "degraded source lost its honest runtime status",
        "Not yet was not recorded explicitly",
        "failed receipt recovery replayed the station",
        "receipt recovery replayed the station",
        "page reload lost server-owned receipt recovery",
        "lost-response recovery sent a second playback request",
        "verification with a mismatched heard echo",
        "heard verification without achievement proof",
        "heard verification with false achievement proof",
        "HTTP 403 advanced the privacy choice",
        "HTTP 403 triggered the success celebration",
        "missing-ok response advanced the privacy choice",
        "missing-ok response unlocked optional AI",
        "privacy success without persisted proof",
        "privacy success with false persisted proof",
        "privacy success without reviewed proof",
        "privacy success with a mismatched choice echo",
        "pre-save':'pre-status",
        "completed Setup swallowed its authorization recovery",
        "completed recovery copy was not Setup-neutral",
        "privacy receipt repair without persisted proof",
        "privacy receipt repair with a mismatched choice echo",
        "privacy receipt repair over HTTP 200",
        "canonical active-off compensation",
        "stale Home context preview",
        "Home context preview that already sent data",
        "Home context preview with an unknown value class",
        "rendered untrusted preview rows",
        "private path requested a Home preview",
        "expired preview did not return focus to a fresh preview",
        "enabled receipt failure hid unsaved progress",
        "private receipt repair lost the safe live state",
        "ambient-only preview was sold as meaningful Home context",
        "enabled path skipped the fresh filtered preview",
        "fresh private completion did not reach the success moment",
        "completed choices are not visibly revisitable",
        "same-speaker retest receipt failure reused old durable heard proof",
        "existing install without a saved speaker exposed a dead retest action",
        "choosing a speaker to retest started playback automatically",
        "existing install replayed the station",
        "configured AI provider was not summarized safely",
        "stored AI key value was rendered into the page",
        "guide playback left room proof actionable",
        "page-hidden guide did not unlock proof controls",
        "page-hidden guide changed proof state",
        "speaker picker returned to First Listen",
        "[320, 568]",
        "[375, 667]",
        "[430, 932]",
        "[554, 800]",
        "[720, 900]",
        "[768, 1024]",
        "[1024, 768]",
        "[1440, 900]",
        "state: 'active'",
        "state: 'completed'",
        "active 200% zoom",
        "completed 200% zoom",
        "320px/200% first-listen geometry overflowed",
        "page.on('pageerror'",
        "uncaught page errors",
    ):
        assert needle in code, f"First Listen browser smoke lost guard: {needle}"
    assert "waitForTimeout(" not in code
    for exercised_control in (
        "failNextPrivacyReceipt",
        "ambientOnlyPreview",
        "nextPrivacyChoiceFailure",
        "nextVerifyResponse",
        "nextPreviewResponse",
        "failNextGuideKey",
        "failNextResume",
        "nextResumeResponse",
        "discardNextConfirmResponse",
    ):
        assert code.count(exercised_control) >= 3, f"First Listen browser smoke does not exercise {exercised_control}"
    assert "_firstListenUi.guideButton = button" not in code
    assert "guide.dataset.state =" not in code
    assert code.count("RECEIPT_FAILURE_STATUS") >= 11, (
        "receipt failure and malformed-partial fixtures must exercise HTTP 503"
    )
    assert code.count("PREVIEW_REQUIRED_STATUS") >= 5, "expired and active-choice preview guards must exercise HTTP 409"
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
