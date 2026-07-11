"""Static guards for the opt-in listener browser smoke."""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import yaml

from mammamiradio.core.config import DEFAULT_STATION_NAME

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"
RUNNER = ROOT / "scripts" / "player-smoke.sh"
RUN_CODE = ROOT / "scripts" / "player-smoke.js"
QUALITY_WORKFLOW = ROOT / ".github" / "workflows" / "quality.yml"
RADIO_CONFIG = ROOT / "radio.toml"
CLI_VERSION_FILE = ROOT / ".playwright-cli-version"


def _quality_job() -> dict:
    workflow = yaml.safe_load(QUALITY_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    quality = jobs.get("quality")
    assert isinstance(quality, dict)
    return quality


def _workflow_step(quality: dict, name: str) -> dict:
    steps = quality.get("steps")
    assert isinstance(steps, list)
    matches = [step for step in steps if isinstance(step, dict) and step.get("name") == name]
    assert len(matches) == 1, f"quality.yml must contain exactly one {name!r} step"
    return matches[0]


def test_player_smoke_target_is_opt_in_and_uses_the_bounded_runner() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert "player-smoke:" in makefile
    assert "scripts/player-smoke.sh" in makefile
    check_line = next(line for line in makefile.splitlines() if line.startswith("check:"))
    assert "player-smoke" not in check_line, "local make check must not require a browser download."

    assert CLI_VERSION_FILE.read_text(encoding="utf-8").strip() == "0.1.17"

    quality = _quality_job()
    assert quality.get("timeout-minutes") == 45, "the complete quality job needs an external deadline"

    node_step = _workflow_step(quality, "Set up Node.js for browser smoke")
    assert node_step.get("uses") == "actions/setup-node@48b55a011bda9f5d6aeb4c2d9c7362e8dae4041e"
    assert node_step.get("with") == {"node-version": "22.17.1", "check-latest": False}

    install_step = _workflow_step(quality, "Install listener smoke browser")
    assert install_step.get("timeout-minutes") == 10
    assert "continue-on-error" not in install_step
    install_run = str(install_step.get("run") or "")
    assert ".playwright-cli-version" in install_run
    assert 'npx --yes --package "@playwright/cli@$PLAYWRIGHT_CLI_VERSION"' in install_run
    assert "install-browser chromium --with-deps --only-shell" in install_run

    smoke_step = _workflow_step(quality, "Listener interaction browser smoke")
    assert smoke_step.get("timeout-minutes") == 10
    assert "continue-on-error" not in smoke_step
    smoke_env = smoke_step.get("env")
    assert isinstance(smoke_env, dict)
    expected_url = "http://127.0.0.1:8765"
    assert smoke_env.get("PLAYER_SMOKE_URL") == expected_url
    assert smoke_env.get("ADMIN_BROWSER_SMOKE_URL") == expected_url
    assert smoke_env.get("PLAYER_SMOKE_CLI_TIMEOUT_SEC") == "90"
    assert smoke_env.get("PLAYER_SMOKE_CLOSE_TIMEOUT_SEC") == "15"
    smoke_run = str(smoke_step.get("run") or "")
    smoke_commands = {line.strip() for line in smoke_run.splitlines()}
    assert "make player-smoke" in smoke_commands
    assert "python -m pytest tests/web/test_admin_browser_smoke.py -q" in smoke_commands
    for lifecycle_guard in (
        "setsid python -m uvicorn",
        'kill -TERM -- "-$server_pid"',
        'kill -KILL -- "-$server_pid"',
        'cat "$server_log"',
    ):
        assert lifecycle_guard in smoke_run, f"CI smoke lost lifecycle guard: {lifecycle_guard}"

    runner = RUNNER.read_text(encoding="utf-8")
    assert ".playwright-cli-version" in runner
    assert "@playwright/cli@$CLI_VERSION" in runner
    assert "PLAYWRIGHT_CLI" in runner, "offline/preinstalled CLI override must remain supported."
    assert "import shlex" in runner, "PLAYWRIGHT_CLI command strings must preserve quoted arguments."
    assert "NUL-delimited safe array items" in runner
    assert "start_new_session=True" in runner
    assert 'RUN_CODE_FILE="${1:-$REPO_ROOT/scripts/player-smoke.js}"' in runner
    assert "Usage: $0 [run-code-file]" in runner
    assert runner.index('cd "$REPO_ROOT"') < runner.index("RUN_CODE_FILE="), (
        "alternate run-code paths must resolve from the repo before validation and execution."
    )
    assert 'readonly COMMAND_TIMEOUT_SEC="${PLAYER_SMOKE_CLI_TIMEOUT_SEC:-90}"' in runner
    assert 'readonly CLOSE_TIMEOUT_SEC="${PLAYER_SMOKE_CLOSE_TIMEOUT_SEC:-15}"' in runner
    assert "run_bounded" in runner
    assert "trap cleanup EXIT" in runner
    assert "trap 'exit 130' INT" in runner
    assert "trap 'exit 143' TERM" in runner
    assert "--max-time 5" in runner
    assert '--session "$SESSION"' in runner
    assert "--json run-code" in runner, "CLI errors must be returned as machine-readable JSON"
    assert 'payload.get("isError")' in runner, "playwright-cli exits zero even when run-code assertions fail"
    assert 'result.get("ok") is not True' in runner
    assert 'run-code --filename "$RUN_CODE_FILE"' in runner
    assert 'cd "$REPO_ROOT"' in runner
    assert os.access(RUNNER, os.X_OK), "scripts/player-smoke.sh must remain executable."
    subprocess.run(["bash", "-n", str(RUNNER)], check=True)


def test_player_smoke_pins_the_listener_interaction_contract() -> None:
    code = RUN_CODE.read_text(encoding="utf-8")
    for needle in (
        "page.setDefaultTimeout(5000)",
        "page.setDefaultNavigationTimeout(10000)",
        "__stale_station_identity__",
        "authoritativeName",
        "visible document title disagrees with authoritative identity",
        "nav wordmark disagrees with authoritative identity",
        "server identity did not repair stale localStorage",
        "focusing the dedication form started audio",
        "empty dedication reached the request API",
        "success_shoutout",
        "rate_limited",
        "queue_full",
        "declined",
        "form_network_error",
        "reducedMotion: 'no-preference'",
        "reducedMotion: 'reduce'",
        "stream request intent took",
        "pending play was not cancellable",
        "play click left the audio element paused",
        "external pause did not restore listen copy",
        "one click after an external pause did not request the stream again",
        "pause cancel left the audio element playing",
        "scheduled retry restarted audio after explicit pause",
        "disabled stopped control requested audio",
        "blockedOffOriginRequests",
        "page.on('pageerror'",
        "uncaught page errors",
    ):
        assert needle in code, f"player smoke lost interaction guard: {needle}"

    assert code.index("await page.route('**/*'") < code.index("page.goto("), (
        "same-origin-only routing must be installed before the first navigation"
    )
    assert "await route.fallback()" in code
    assert "blocked_off_origin_requests" in code

    wait_count = code.count("waitForFunction(")
    assert wait_count, "player smoke must use bounded browser waits."
    assert code.count("{ timeout:") >= wait_count, "every waitForFunction call needs an explicit timeout."
    assert "async function waitForRouteCount" in code
    assert "const deadline = Date.now() + timeoutMs" in code
    assert ".catch(() => {})" not in code, "smoke assertions must never suppress browser failures."


def test_default_listener_identity_fixture_is_canonical() -> None:
    config = tomllib.loads(RADIO_CONFIG.read_text(encoding="utf-8"))
    assert DEFAULT_STATION_NAME == "Mamma Mi Radio"
    assert config["station"]["name"] == DEFAULT_STATION_NAME
    assert config["brand"]["station_name"] == DEFAULT_STATION_NAME
