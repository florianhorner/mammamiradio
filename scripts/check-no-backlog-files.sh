#!/usr/bin/env bash
# Backlog-file guard: fails if a tracked catch-all TODO/backlog file exists.
#
# CLAUDE.md bans canonical tracked backlog files — public engineering work goes
# to GitHub issues, private strategy goes to a private durable store. The prose
# rule alone proved insufficient (docs/todos.md was created in violation of it
# and dissolved on 2026-05-17), so this guard makes the rule structural.
#
# Filenames only — no content keyword scanning. Scanning the repo for words like
# "deferred" or "follow-up" produces false positives in changelogs, tests, and
# prose; a filename check is sufficient and zero-false-positive.
#
# Run locally:    bash scripts/check-no-backlog-files.sh
# CI invocation:  see .github/workflows/quality.yml

set -euo pipefail

BANNED=(
  TODO.md
  TODOS.md
  docs/todos.md
  docs/backlog.md
  BACKLOG.md
)

found=0
for f in "${BANNED[@]}"; do
  if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
    echo "ERROR: tracked backlog file '$f' found." >&2
    found=1
  fi
done

if [ "$found" -ne 0 ]; then
  echo "" >&2
  echo "Catch-all TODO/backlog files are banned (see CLAUDE.md)." >&2
  echo "Public engineering work goes to GitHub issues; private strategy goes" >&2
  echo "to a private durable store. Delete the file and re-route its content." >&2
  exit 1
fi

echo "Backlog-file guard: OK — no tracked catch-all backlog files."
