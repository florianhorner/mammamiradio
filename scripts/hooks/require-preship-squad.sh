#!/usr/bin/env bash
# PreToolUse(Bash) guard — refuse a bare `gh pr create` / `gh pr merge` unless a
# pre-ship review squad ran for this code. /ship logs the squad as a review-log
# entry with skill="review" (Step 9) or "adversarial-review" (Step 11); this guard
# requires such an entry whose commit is in HEAD's recent (<=2h) history.
#
# Why this exists: on the god-module refactor, PRs were opened with bare
# `gh pr create` (skipping /ship), so the mandatory pre-ship squad — including its
# docs/config-consistency check — never ran, and a doc-sync hard-rule violation
# reached a green, mergeable PR undetected. CLAUDE.md: "Pre-ship review squad
# (mandatory in every worktree)."
#
# FAILS OPEN: any internal error (no jq, not a git repo, no gstack, parse failure)
# exits 0 (allow). A bug in this guard can never block a PR. The ONLY path that
# blocks is the explicit deny at the end, reached only when a gh-pr-create/merge
# command has no qualifying squad entry for HEAD.

input="$(cat 2>/dev/null)"
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // ""' 2>/dev/null)" || exit 0

# Only guard `gh pr create` / `gh pr merge`. Everything else (incl. `gh pr view`,
# `gh pr checks`, `gh pr list`) passes untouched.
printf '%s' "$cmd" | grep -Eq '(^|[;&|[:space:]])gh[[:space:]]+pr[[:space:]]+(create|merge)([[:space:]]|$)' || exit 0

head="$(git rev-parse --short HEAD 2>/dev/null)" || exit 0
[ -z "$head" ] && exit 0

# Reader is overridable via env for testing only; defaults to the real gstack log.
reader="${MMR_PRESHIP_REVIEW_READER:-$HOME/.claude/skills/gstack/bin/gstack-review-read}"
[ -x "$reader" ] || exit 0   # no gstack review log here -> out of scope, allow

now="$(date +%s)"
ok=0
while IFS= read -r line; do
  case "$line" in ---CONFIG---*) break ;; esac
  skill="$(printf '%s' "$line" | jq -r '.skill // ""' 2>/dev/null)" || continue
  case "$skill" in review | adversarial-review) ;; *) continue ;; esac
  rc="$(printf '%s' "$line" | jq -r '.commit // ""' 2>/dev/null)"
  { [ -z "$rc" ] || [ "$rc" = "null" ]; } && continue
  ts="$(printf '%s' "$line" | jq -r '.timestamp // ""' 2>/dev/null)"
  # Parse the trailing-Z timestamp as UTC. macOS `date -j -f` ignores the Z and
  # reads local time without -u, which offsets the 2h window by the local UTC
  # offset (caught a non-UTC false-stale that blocked legit PRs). GNU `date -d`
  # honors the Z; -u there is harmless.
  es="$(date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$ts" +%s 2>/dev/null || date -u -d "$ts" +%s 2>/dev/null || echo 0)"
  # Treat an unparseable/missing/non-numeric timestamp as stale (skip it). A guard
  # must fail toward "not authorized" on data whose freshness it cannot verify —
  # never bless an entry just because its timestamp didn't parse.
  if ! [ "$es" -gt 0 ] 2>/dev/null || [ "$((now - es))" -gt 7200 ]; then
    continue # unverifiable or stale (>2h) — not this work session
  fi
  if [ "$rc" = "$head" ] || git merge-base --is-ancestor "$rc" HEAD 2>/dev/null; then
    ok=1
    break
  fi
done < <("$reader" 2>/dev/null)

[ "$ok" = "1" ] && exit 0

cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"No pre-ship review squad logged for HEAD. Land via /ship (it runs the mandatory squad, incl. the docs/config-consistency check) instead of a bare gh pr create/merge. CLAUDE.md: 'Pre-ship review squad (mandatory in every worktree).'"}}
JSON
exit 0
