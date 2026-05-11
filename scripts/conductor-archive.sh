#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RUNTIME_ROOT="$ROOT/.context/conductor"

if [ -d "$ROOT/.context" ] && command -v mine-context >/dev/null 2>&1; then
  LAST_MINED="$ROOT/.context/.last-mined"
  SKIP=false
  if [ -f "$LAST_MINED" ] && [ -n "$(find "$LAST_MINED" -mmin -60 2>/dev/null)" ]; then
    SKIP=true
  fi
  if [ "$SKIP" = false ]; then
    echo "[archive] Mining .context/ into MemPalace..."
    mine-context "$ROOT" || echo "[archive] mine-context failed; cron audit will retry"
  else
    echo "[archive] .context/ mined recently, skipping"
  fi
fi

rm -rf "$RUNTIME_ROOT"
