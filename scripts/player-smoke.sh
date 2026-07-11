#!/usr/bin/env bash
# Deterministic browser smoke for the listener interaction contract.
#
# This is intentionally opt-in (`make player-smoke`) and mocks only the
# mutable/public browser boundaries. The page HTML and production listener.js
# still come from the local server named by PLAYER_SMOKE_URL.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT

# Treat an optional run-code path exactly like the built-in fixture: relative
# paths are always resolved from the repository, even when this script is
# invoked from elsewhere.
cd "$REPO_ROOT"

if [[ "$#" -gt 1 ]]; then
  echo "Usage: $0 [run-code-file]" >&2
  exit 2
fi

RUN_CODE_FILE="${1:-$REPO_ROOT/scripts/player-smoke.js}"
if [[ ! -f "$RUN_CODE_FILE" ]]; then
  echo "player-smoke run-code file does not exist: $RUN_CODE_FILE" >&2
  exit 2
fi
readonly RUN_CODE_FILE
CLI_VERSION="$(tr -d '[:space:]' < "$REPO_ROOT/.playwright-cli-version")"
readonly CLI_VERSION
readonly BASE_URL="${PLAYER_SMOKE_URL:-http://127.0.0.1:8000}"
readonly SESSION="${PLAYER_SMOKE_SESSION:-mammamiradio-player-smoke-$$}"
readonly COMMAND_TIMEOUT_SEC="${PLAYER_SMOKE_CLI_TIMEOUT_SEC:-90}"
readonly CLOSE_TIMEOUT_SEC="${PLAYER_SMOKE_CLOSE_TIMEOUT_SEC:-15}"

if [[ ! "$COMMAND_TIMEOUT_SEC" =~ ^[1-9][0-9]*$ ]] || [[ ! "$CLOSE_TIMEOUT_SEC" =~ ^[1-9][0-9]*$ ]]; then
  echo "Player smoke timeouts must be positive integer seconds." >&2
  exit 2
fi

command -v python3 >/dev/null 2>&1 || {
  echo "player-smoke needs python3 to enforce process-group timeouts." >&2
  exit 2
}

if [[ -n "${PLAYWRIGHT_CLI:-}" ]]; then
  # Keep the admin smoke's former command-string override contract while
  # avoiding eval: Python's shlex parser emits NUL-delimited safe array items.
  cli=()
  while IFS= read -r -d '' cli_arg; do
    cli+=("$cli_arg")
  done < <(PLAYWRIGHT_CLI="$PLAYWRIGHT_CLI" python3 - <<'PY'
import os
import shlex
import sys

try:
    arguments = shlex.split(os.environ["PLAYWRIGHT_CLI"])
except ValueError as exc:
    print(f"player-smoke: invalid PLAYWRIGHT_CLI: {exc}", file=sys.stderr)
    raise SystemExit(2) from exc

for argument in arguments:
    sys.stdout.buffer.write(argument.encode("utf-8") + b"\0")
PY
)
  if [[ "${#cli[@]}" -eq 0 ]]; then
    echo "PLAYWRIGHT_CLI did not contain an executable command." >&2
    exit 2
  fi
  if [[ "${cli[0]}" == */* ]]; then
    [[ -x "${cli[0]}" ]] || {
      echo "PLAYWRIGHT_CLI is not executable: ${cli[0]}" >&2
      exit 2
    }
  else
    command -v "${cli[0]}" >/dev/null 2>&1 || {
      echo "PLAYWRIGHT_CLI is not on PATH: ${cli[0]}" >&2
      exit 2
    }
  fi
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
