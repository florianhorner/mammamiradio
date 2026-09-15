#!/usr/bin/env bash
# Fast, deterministic lint bundle shared by local pre-push and Quality CI.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SHELLCHECK_IMAGE="koalaman/shellcheck@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d"

cd "$REPO_ROOT"

step() {
    echo ""
    echo "==> $1"
}

fail_tool() {
    echo "pre-lint: $1" >&2
    exit 2
}

expected_ruff="$(sed -n 's/^ruff==\([^[:space:]]*\).*/\1/p' requirements-dev.txt | head -1)"
if [ -x .venv/bin/ruff ]; then
    RUFF_BIN=".venv/bin/ruff"
elif command -v ruff >/dev/null 2>&1; then
    RUFF_BIN="$(command -v ruff)"
else
    fail_tool "Ruff is missing; install requirements-dev.txt or create .venv."
fi
actual_ruff="$($RUFF_BIN --version | awk '{print $2}')"
if [ -z "$expected_ruff" ] || [ "$actual_ruff" != "$expected_ruff" ]; then
    fail_tool "Ruff $actual_ruff does not match CI $expected_ruff; install requirements-dev.txt."
fi

step "ShellCheck scripts"
if command -v shellcheck >/dev/null 2>&1; then
    shellcheck_version="$(shellcheck --version | awk -F': ' '/^version:/ {print $2}')"
    [ "$shellcheck_version" = "0.11.0" ] \
        || fail_tool "ShellCheck $shellcheck_version does not match CI 0.11.0."
    shellcheck scripts/*.sh
elif command -v docker >/dev/null 2>&1; then
    docker run --rm -v "$REPO_ROOT:/mnt:ro" -w /mnt "$SHELLCHECK_IMAGE" scripts/*.sh
else
    fail_tool "ShellCheck 0.11.0 is missing; install it or make Docker available."
fi

step "Ruff lint"
"$RUFF_BIN" check .

step "Ruff format"
"$RUFF_BIN" format --check .

step "Changelog content lint"
bash scripts/check-changelog-lint.sh

step "Documentation safety lint"
bash scripts/check-docs-safety.sh

step "UI copy lint"
bash scripts/check-ui-copy-lint.sh

step "Backlog-file guard"
bash scripts/check-no-backlog-files.sh

echo ""
echo "pre-lint: all deterministic lint checks passed."
