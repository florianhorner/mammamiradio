#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RUNTIME_ROOT="$ROOT/.context/conductor"
TMP_DIR="${MAMMAMIRADIO_TMP_DIR:-$RUNTIME_ROOT/tmp}"
STATE_FILE="$TMP_DIR/go-librespot.state.json"
DRAIN_PID_FILE="$TMP_DIR/fifo-drain.pid"
FIFO_PATH="${MAMMAMIRADIO_FIFO_PATH:-}"

if [ -z "$FIFO_PATH" ] && [ -n "${CONDUCTOR_WORKSPACE_NAME:-}" ]; then
  FIFO_PATH="/tmp/mammamiradio-${CONDUCTOR_WORKSPACE_NAME}.pcm"
fi

stop_pid() {
  local pid="${1:-}"
  if [ -z "$pid" ]; then
    return 0
  fi
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    sleep 1
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
  fi
}

if [ -f "$STATE_FILE" ]; then
  GO_PID="$(
    python3 - <<'PY' "$STATE_FILE"
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text())
except Exception:
    raise SystemExit(0)

pid = payload.get("pid")
if pid is not None:
    print(pid)
PY
  )"
  stop_pid "$GO_PID"
fi

if [ -f "$DRAIN_PID_FILE" ]; then
  DRAIN_PID="$(cat "$DRAIN_PID_FILE" 2>/dev/null || true)"
  stop_pid "$DRAIN_PID"
fi

rm -f "$STATE_FILE" "$DRAIN_PID_FILE"

if [ -n "$FIFO_PATH" ]; then
  rm -f "$FIFO_PATH"
fi

rm -rf "$RUNTIME_ROOT"
