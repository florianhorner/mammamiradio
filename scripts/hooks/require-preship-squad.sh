#!/usr/bin/env bash
# PreToolUse(Bash) guard — two rules:
#
#   1. `gh pr create` requires a pre-ship review squad entry for this code.
#      /ship logs the squad as a review-log entry with skill="review" (Step 9)
#      or "adversarial-review" (Step 11); this guard requires such an entry
#      whose commit is in HEAD's recent (<=2h) history.
#   2. `gh pr merge` is denied OUTRIGHT — landing goes through
#      scripts/land-pr.sh (the landing contract in CLAUDE.md "Quality gates").
#      The wrapper does its own squad check with code-state freshness (entry
#      commit covers the PR head AND nothing was pushed after the entry), so
#      soaked PRs land without ritual review re-runs. The wrapper's internal
#      gh calls run inside its own process and never hit this hook.
#      Exception: `gh pr merge --disable-auto` (disarming a queued merge) is
#      a cancel operation and passes.
#
# Why this exists: on the god-module refactor, PRs were opened with bare
# `gh pr create` (skipping /ship), so the mandatory pre-ship squad — including its
# docs/config-consistency check — never ran, and a doc-sync hard-rule violation
# reached a green, mergeable PR undetected. The merge rule was added 2026-06-12
# after hand-rolled base-integration (a `git reset --soft origin/main` onto a
# moved main) nearly shipped phantom reverts; land-pr.sh pins the merge to the
# exact reviewed head (--match-head-commit). CLAUDE.md: "Pre-ship review squad
# (mandatory in every worktree)" + "Landing contract".
#
# KNOWN LIMITATION (observed live 2026-06-12): this guard greps the WHOLE Bash
# command string, so heredoc/string CONTENT mentioning the guarded commands
# (e.g. a prompt file being written) trips it too. That false positive is
# accepted — reword the content or write it via the Write tool. A token-aware
# parse belongs in permission-guard.py, not here.
#
# FAILS OPEN: any internal error (no jq, not a git repo, no gstack, parse failure)
# exits 0 (allow). A bug in this guard can never block a PR. The ONLY paths that
# block are the two explicit denies below.

input="$(cat 2>/dev/null)"
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // ""' 2>/dev/null)" || exit 0

# Rule 2: deny raw `gh pr merge` (except --disable-auto). Landing = land-pr.sh.
if printf '%s' "$cmd" | grep -Eq '(^|[;&|[:space:]])gh[[:space:]]+pr[[:space:]]+merge([[:space:]]|$)'; then
  # --disable-auto must be an argument OF the merge command itself (no shell
  # operator between them) — `... merge 5 && echo "--disable-auto"` is a
  # bypass attempt, not a disarm.
  if printf '%s' "$cmd" | grep -Eq '(^|[;&|[:space:]])gh[[:space:]]+pr[[:space:]]+merge([[:space:]][^;&|]*)?[[:space:]]--disable-auto([[:space:]]|$|[^-A-Za-z])'; then
    exit 0
  fi
  cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Raw gh pr merge is retired. Land via scripts/land-pr.sh <PR#> — it verifies the pre-ship squad against the PR head, updates the branch if behind (CI re-runs), and arms auto-merge pinned to the exact reviewed head (--match-head-commit). Disarming with gh pr merge --disable-auto is allowed. See CLAUDE.md 'Landing contract'."}}
JSON
  exit 0
fi

# Rule 1: only guard `gh pr create` beyond this point. Everything else (incl.
# `gh pr view`, `gh pr checks`, `gh pr list`) passes untouched.
printf '%s' "$cmd" | grep -Eq '(^|[;&|[:space:]])gh[[:space:]]+pr[[:space:]]+create([[:space:]]|$)' || exit 0

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
  # The entry must fall inside a +/-2h window around now. Reject unverifiable
  # (unparseable/zero/non-numeric), far-future (clock skew or a forged-ahead
  # timestamp >2h out), and stale (>2h old). A guard fails toward "not authorized"
  # on data whose freshness it cannot trust; a few seconds of benign skew stays valid.
  if ! [ "$es" -gt 0 ] 2>/dev/null || [ "$((es - now))" -gt 7200 ] || [ "$((now - es))" -gt 7200 ]; then
    continue # unverifiable, far-future, or stale — outside the 2h work-session window
  fi
  if [ "$rc" = "$head" ] || git merge-base --is-ancestor "$rc" HEAD 2>/dev/null; then
    ok=1
    break
  fi
done < <("$reader" 2>/dev/null)

[ "$ok" = "1" ] && exit 0

cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"No pre-ship review squad logged for HEAD. Open the PR via /ship (it runs the mandatory squad, incl. the docs/config-consistency check) instead of a bare gh pr create. CLAUDE.md: 'Pre-ship review squad (mandatory in every worktree).'"}}
JSON
exit 0
