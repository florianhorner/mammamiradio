#!/usr/bin/env bash
# Pre-commit hook: enforce conventional commit prefixes.
# Allows: feat, fix, refactor, test, chore, docs, security, ci, deps, style, release, merge, perf, revert
# Merge commits from GitHub ("Merge pull request") are allowed.
set -euo pipefail

MSG_FILE="$1"
MSG=$(head -1 "$MSG_FILE")

# Allow GitHub merge commits
if echo "$MSG" | grep -qE '^Merge (pull request|remote-tracking|branch)'; then
    exit 0
fi

# Allow conventional commit prefixes (with optional scope)
if echo "$MSG" | grep -qE '^(feat|fix|refactor|test|chore|docs|security|ci|deps|style|release|merge|perf|revert)(\([^)]+\))?(!)?:'; then
    exit 0
fi

echo "ERROR: Commit message must start with a conventional prefix."
echo "  Allowed: feat|fix|refactor|test|chore|docs|security|ci|deps|style|release|merge|perf|revert"
echo "  Example: feat: add personality sliders"
echo "  Example: fix(addon): ingress double-prefix bug"
echo "  Got:     $MSG"
exit 1
