#!/usr/bin/env bash
# check-merge-gate.sh — assert the merge-gate repo settings have not drifted.
#
# The landing contract (CLAUDE.md "Quality gates") rests on three GitHub
# settings that live OUTSIDE the repo and would fail silently if toggled:
#
#   1. branch protection on main: strict status checks (branch must be up to
#      date before merging) — the setting whose absence enabled the 2026-06-11
#      stale-base rebase footgun;
#   2. repo: allow_update_branch (the Update-branch affordance land-pr.sh uses);
#   3. repo: allow_auto_merge (arming `--auto` at all).
#
# Run locally (user-auth gh): wired into `make pre-release` and the runbook
# pre-merge checklist. In CI this SKIPS LOUDLY — GITHUB_TOKEN lacks admin read
# on branch protection, so a CI run could only ever false-fail. This is a
# local drift tripwire, not a CI gate (accepted residual: drift is caught at
# the next release check, not the next ship).
set -euo pipefail

if [ -n "${CI:-}" ]; then
  echo "check-merge-gate: SKIPPED in CI (GITHUB_TOKEN cannot read branch protection)."
  echo "check-merge-gate: run locally before releasing: bash scripts/check-merge-gate.sh"
  exit 0
fi

command -v gh >/dev/null 2>&1 || { echo "check-merge-gate: FAIL — gh CLI not found." >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "check-merge-gate: FAIL — jq not found." >&2; exit 1; }

rc=0

repo_json="$(gh api 'repos/{owner}/{repo}' 2>/dev/null)" || {
  echo "check-merge-gate: FAIL — could not read repo settings (gh auth?)." >&2
  exit 1
}
prot_json="$(gh api 'repos/{owner}/{repo}/branches/main/protection/required_status_checks' 2>/dev/null)" || {
  echo "check-merge-gate: FAIL — could not read main branch protection (needs admin-scoped gh auth)." >&2
  exit 1
}

assert_true() { # <label> <json> <jq-path>
  local val
  val="$(printf '%s' "$2" | jq -r "$3" 2>/dev/null)"
  if [ "$val" = "true" ]; then
    echo "check-merge-gate: PASS — $1"
  else
    echo "check-merge-gate: FAIL — $1 is '$val', expected 'true'. Restore it: see docs/runbooks/ha-addon.md 'Landing a PR'." >&2
    rc=1
  fi
}

assert_true "branch protection strict (up-to-date before merge)" "$prot_json" '.strict'
assert_true "repo allow_update_branch" "$repo_json" '.allow_update_branch'
assert_true "repo allow_auto_merge" "$repo_json" '.allow_auto_merge'

# The required contexts the strict flag protects — drift here would let PRs
# land without the quality/pi-smoke verdicts entirely.
for ctx in quality pi-smoke; do
  if printf '%s' "$prot_json" | jq -e --arg c "$ctx" '.contexts | index($c)' >/dev/null 2>&1; then
    echo "check-merge-gate: PASS — required check '$ctx' present"
  else
    echo "check-merge-gate: FAIL — required check '$ctx' missing from branch protection." >&2
    rc=1
  fi
done

if [ "$rc" -eq 0 ]; then
  echo "check-merge-gate: all merge-gate settings intact."
else
  echo "check-merge-gate: drift detected — fix the settings above before landing anything." >&2
fi
exit "$rc"
