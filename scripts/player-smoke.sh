#!/usr/bin/env bash
# Deterministic browser smoke for the listener interaction contract.
#
# This is intentionally opt-in (`make player-smoke`) and mocks only the
# mutable/public browser boundaries. The page HTML and production listener.js
# still come from the local server named by PLAYER_SMOKE_URL.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT
CLI_VERSION="$(tr -d '[:space:]' < "$REPO_ROOT/.playwright-cli-version")"
readonly CLI_VERSION
readonly RUN_CODE_FILE="$REPO_ROOT/scripts/player-smoke.js"
readonly BASE_URL="${PLAYER_SMOKE_URL:-http://127.0.0.1:8000}"
readonly SESSION="${PLAYER_SMOKE_SESSION:-mammamiradio-player-smoke-$$}"
readonly COMMAND_TIMEOUT_SEC="${PLAYER_SMOKE_CLI_TIMEOUT_SEC:-90}"
readonly CLOSE_TIMEOUT_SEC="${PLAYER_SMOKE_CLOSE_TIMEOUT_SEC:-15}"

# The browser fixture uses a repo-relative packaged MP3. Make direct script
# invocation behave the same as `make player-smoke`, regardless of caller cwd.
cd "$REPO_ROOT"

if [[ ! "$COMMAND_TIMEOUT_SEC" =~ ^[1-9][0-9]*$ ]] || [[ ! "$CLOSE_TIMEOUT_SEC" =~ ^[1-9][0-9]*$ ]]; then
  echo "Player smoke timeouts must be positive integer seconds." >&2
  exit 2
fi

command -v python3 >/dev/null 2>&1 || {
  echo "player-smoke needs python3 to enforce process-group timeouts." >&2
  exit 2
}

if [[ -n "${PLAYWRIGHT_CLI:-}" ]]; then
  if [[ "$PLAYWRIGHT_CLI" == */* ]]; then
    [[ -x "$PLAYWRIGHT_CLI" ]] || {
      echo "PLAYWRIGHT_CLI is not executable: $PLAYWRIGHT_CLI" >&2
      exit 2
    }
  else
    command -v "$PLAYWRIGHT_CLI" >/dev/null 2>&1 || {
      echo "PLAYWRIGHT_CLI is not on PATH: $PLAYWRIGHT_CLI" >&2
      exit 2
    }
  fi
  cli=("$PLAYWRIGHT_CLI")
else
  command -v npx >/dev/null 2>&1 || {
    echo "player-smoke needs npx, or set PLAYWRIGHT_CLI to a preinstalled playwright-cli wrapper." >&2
    exit 2
  }
  cli=(npx --yes --package "@playwright/cli@$CLI_VERSION" playwright-cli)
fi

# Run each CLI command in a fresh process group. Page-level Playwright timeouts
# cannot stop a wedged npx/CLI/browser process, so this outer deadline escalates
# TERM to KILL and bounds both local runs and CI cleanup.
run_bounded() {
  local timeout_sec="$1"
  local label="$2"
  shift 2
  python3 - "$timeout_sec" "$label" "$@" <<'PY'
import os
import signal
import subprocess
import sys

timeout = int(sys.argv[1])
label = sys.argv[2]
command = sys.argv[3:]
process = subprocess.Popen(command, start_new_session=True)


def stop_process_group() -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


try:
    returncode = process.wait(timeout=timeout)
except subprocess.TimeoutExpired:
    print(f"player-smoke: {label} timed out after {timeout}s", file=sys.stderr)
    stop_process_group()
    raise SystemExit(124)
except KeyboardInterrupt:
    stop_process_group()
    raise SystemExit(130)

raise SystemExit(returncode)
PY
}

_cleanup_started=false
cleanup() {
  if [[ "$_cleanup_started" == "true" ]]; then
    return
  fi
  _cleanup_started=true
  run_bounded "$CLOSE_TIMEOUT_SEC" "browser session cleanup" \
    "${cli[@]}" --session "$SESSION" close >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

curl --fail --silent --show-error --max-time 5 "${BASE_URL%/}/" >/dev/null

run_bounded "$COMMAND_TIMEOUT_SEC" "browser session open" \
  "${cli[@]}" --session "$SESSION" open "about:blank#${BASE_URL%/}" >/dev/null
smoke_json="$(run_bounded "$COMMAND_TIMEOUT_SEC" "listener interaction smoke" \
  "${cli[@]}" --session "$SESSION" --json run-code --filename "$RUN_CODE_FILE")"
printf '%s\n' "$smoke_json"
SMOKE_JSON="$smoke_json" python3 - <<'PY'
import json
import os

try:
    payload = json.loads(os.environ["SMOKE_JSON"])
except (KeyError, json.JSONDecodeError) as exc:
    raise SystemExit(f"player-smoke: CLI returned invalid JSON: {exc}") from exc

if payload.get("isError"):
    raise SystemExit(f"player-smoke: browser assertions failed: {payload.get('error', 'unknown error')}")

try:
    result = json.loads(payload["result"])
except (KeyError, TypeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"player-smoke: CLI returned no structured result: {exc}") from exc

if not isinstance(result, dict) or result.get("ok") is not True:
    raise SystemExit(f"player-smoke: browser smoke did not report success: {result!r}")
PY
