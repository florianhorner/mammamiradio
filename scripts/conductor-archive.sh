#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RUNTIME_ROOT="$ROOT/.context/conductor"

rm -rf "$RUNTIME_ROOT"
