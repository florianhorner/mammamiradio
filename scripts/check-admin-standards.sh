#!/usr/bin/env bash
# CI guard: when admin.html or listener.html changed, PR body must include
# at least one checked item from the Admin Panel Standards checklist.
# Exit 0 = pass, Exit 1 = fail.

set -euo pipefail

ADMIN_FILES=(
  "mammamiradio/web/templates/admin.html"
  "mammamiradio/web/templates/listener.html"
)

# Determine changed files relative to base (PR context) or HEAD~1 (push context)
if [[ -n "${GITHUB_BASE_REF:-}" ]]; then
  git fetch origin "$GITHUB_BASE_REF" --depth=1 2>/dev/null || true
  CHANGED=$(git diff --name-only "origin/${GITHUB_BASE_REF}" HEAD 2>/dev/null || git diff --name-only HEAD~1 HEAD)
else
  CHANGED=$(git diff --name-only HEAD~1 HEAD 2>/dev/null || echo "")
fi

# Check if any admin HTML file was modified
ADMIN_CHANGED=0
for f in "${ADMIN_FILES[@]}"; do
  if echo "$CHANGED" | grep -qF "$f"; then
    ADMIN_CHANGED=1
    echo "Detected change to $f — Admin Panel Standards check required."
  fi
done

if [[ "$ADMIN_CHANGED" -eq 0 ]]; then
  echo "No admin panel files changed — standards check skipped."
  exit 0
fi

# Read PR body from GitHub event payload (only available in PR context)
PR_BODY=""
if [[ -n "${GITHUB_EVENT_PATH:-}" && -f "${GITHUB_EVENT_PATH}" ]]; then
  PR_BODY=$(jq -r '.pull_request.body // ""' "${GITHUB_EVENT_PATH}")
fi

if [[ -z "$PR_BODY" ]]; then
  echo "::warning::Could not read PR body — skipping standards enforcement (push context)."
  exit 0
fi

# Require at least one checked item in the Admin Panel Standards section.
# Extract only that section: heading (any ATX level) through the next heading or EOF.
# awk always exits 0, so this is safe under set -e even when the heading is absent.
SECTION=$(printf '%s\n' "$PR_BODY" | awk '
  /^[[:space:]]*#+[[:space:]]*Admin Panel Standards[[:space:]]*$/ { grabbing = 1 }
  grabbing && /^[[:space:]]*#+[[:space:]]/ && !/^[[:space:]]*#+[[:space:]]*Admin Panel Standards[[:space:]]*$/ { exit }
  grabbing { print }
')

if [[ -z "$SECTION" ]]; then
  echo "::error::Admin panel files changed but the PR body is missing the Admin Panel Standards checklist."
  echo "::error::Copy the checklist from docs/design/admin-panel.md and check the applicable items before merging."
  exit 1
fi

if printf '%s\n' "$SECTION" | grep -qF -- "- [x]"; then
  echo "Admin Panel Standards section contains checked items — OK."
  exit 0
fi

echo "::error::Admin panel files changed but the PR body has no checked items in the Admin Panel Standards section."
echo "::error::Copy the checklist from docs/design/admin-panel.md and check the applicable items before merging."
exit 1
