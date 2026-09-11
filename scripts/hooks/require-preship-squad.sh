#!/usr/bin/env bash
# PreToolUse(Bash) guard — two rules:
#
#   1. `gh pr create` requires a pre-ship review squad entry for this code.
#      /ship logs the squad as a review-log entry with skill="review" (Step 9)
#      or "adversarial-review" (Step 11); this guard requires such an entry
#      whose commit is in HEAD's recent (<=2h) history.
#   2. Raw merge attempts are denied OUTRIGHT — landing goes through
#      scripts/land-pr.sh (the landing contract in CLAUDE.md "Quality gates").
#      This blocks `gh pr merge` and mutating `gh api` merge calls (REST
#      /pulls/<n>/merge PUT, plus GraphQL mergePullRequest/auto-merge
#      mutations). The wrapper does its own squad check with code-state
#      freshness (entry commit covers the PR head AND nothing was pushed after
#      the entry), so soaked PRs land without ritual review re-runs. The
#      wrapper's internal gh calls run inside its own process and never hit this
#      hook.
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
# block are the explicit deny families below.

input="$(cat 2>/dev/null)"
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // ""' 2>/dev/null)" || exit 0

deny_raw_merge() {
  cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Raw GitHub merge commands are retired. Land via scripts/land-pr.sh <PR#> — it verifies the pre-ship squad against the PR head, updates the branch if behind (CI re-runs), and arms auto-merge pinned to the exact reviewed head (--match-head-commit). This hook denies gh pr merge and mutating gh api merge attempts. Disarming with gh pr merge --disable-auto is allowed. See CLAUDE.md 'Landing contract'."}}
JSON
  exit 0
}

_graphql_merge_pattern='mergePullRequest|enablePullRequestAutoMerge'

strip_hook_quotes() {
  local value="$1"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  printf '%s' "$value"
}

graphql_payload_file_is_unsafe() {
  local path="$1"
  path="${path#@}"
  path="$(strip_hook_quotes "$path")"
  [ -n "$path" ] || return 0
  # Stdin or unreadable file payloads cannot be inspected by this grep-based
  # guard. Treat them as unsafe instead of creating a file-backed merge bypass.
  [ "$path" = "-" ] && return 0
  [ -r "$path" ] || return 0
  grep -Eq "$_graphql_merge_pattern" "$path"
}

graphql_file_payload_mentions_merge() {
  local token prev
  prev=""
  for token in $cmd; do
    token="$(strip_hook_quotes "$token")"
    case "$prev" in
      --input)
        graphql_payload_file_is_unsafe "$token" && return 0
        ;;
      -F | --field | -f | --raw-field)
        case "$token" in
          query=@*) graphql_payload_file_is_unsafe "${token#query=@}" && return 0 ;;
        esac
        ;;
    esac
    prev=""

    case "$token" in
      --input)
        prev="--input"
        ;;
      --input=*)
        graphql_payload_file_is_unsafe "${token#--input=}" && return 0
        ;;
      -F | --field | -f | --raw-field)
        prev="$token"
        ;;
      -F=query=@* | --field=query=@* | -f=query=@* | --raw-field=query=@*)
        graphql_payload_file_is_unsafe "${token#*=query=@}" && return 0
        ;;
    esac
  done
  return 1
}

# Rule 2: deny raw `gh pr merge` (except --disable-auto). Landing = land-pr.sh.
if printf '%s' "$cmd" | grep -Eq '(^|[;&|[:space:]])gh[[:space:]]+pr[[:space:]]+merge([[:space:]]|$)'; then
  # --disable-auto must be an argument OF the merge command itself (no shell
  # operator between them) — `... merge 5 && echo "--disable-auto"` is a
  # bypass attempt, not a disarm.
  if printf '%s' "$cmd" | grep -Eq '(^|[;&|[:space:]])gh[[:space:]]+pr[[:space:]]+merge([[:space:]][^;&|]*)?[[:space:]]--disable-auto([[:space:]]|$|[^-A-Za-z])'; then
    exit 0
  fi
  deny_raw_merge
fi

# Rule 2b: deny raw GitHub API merge attempts. Read-only `gh api` calls still
# pass; the REST deny requires the pull merge endpoint AND an explicit PUT.
if printf '%s' "$cmd" | grep -Eq '(^|[;&|[:space:]])gh[[:space:]]+api([[:space:]]|$)'; then
  if printf '%s' "$cmd" | grep -Eq '/pulls/[0-9]+/merge([^[:alnum:]_-]|$)' \
    && printf '%s' "$cmd" | grep -Eiq '(^|[[:space:]])((--method)(=|[[:space:]]+)PUT|-X(=|[[:space:]]*)PUT)([^A-Za-z]|$)'; then
    deny_raw_merge
  fi

  if printf '%s' "$cmd" | grep -Eq '(^|[;&|[:space:]])gh[[:space:]]+api[[:space:]]+graphql([[:space:]]|$)' \
    && { printf '%s' "$cmd" | grep -Eq "$_graphql_merge_pattern" || graphql_file_payload_mentions_merge; }; then
    deny_raw_merge
  fi
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

if [ "$ok" != "1" ]; then
  cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"No pre-ship review squad logged for HEAD. Open the PR via /ship (it runs the mandatory squad, incl. the docs/config-consistency check) instead of a bare gh pr create. CLAUDE.md: 'Pre-ship review squad (mandatory in every worktree).'"}}
JSON
  exit 0
fi

# Rule 1b: a logged squad is not the gate — the committed v2 receipt is.
#
# The ledger entry above lives only on this machine, so it proves nothing about
# what the PR carries. check-preship-evidence.sh is the runtime-independent half,
# and it was already wired at land time (scripts/land-gates.sh) and into the queue
# view (scripts/pr-queue-status.sh) — but not here. That let a PR open, go fully
# green, and only fail at the landing attempt, which is the most expensive place
# to learn the receipt was never emitted. Same checker, same verdict, moved to the
# cheapest moment. Observed 2026-09-11 on #1126: a malformed ledger entry (findings
# nested under specialists instead of top-level) made emit-review-evidence.sh refuse
# to write a receipt, the emit step was skipped entirely, and the "pre-ship evidence"
# CI check is report-only, so nothing objected until the lander did.
#
# FAILS OPEN, like the rest of this guard: deny ONLY when the verifier actually
# rendered a verdict (its output carries the `landing-evidence:` prefix). A missing
# checker, an unusable Python, or an unresolvable base leaves the PR alone.
evidence_checker="${MMR_PRESHIP_EVIDENCE_CHECKER:-$(git rev-parse --show-toplevel 2>/dev/null)/scripts/check-preship-evidence.sh}"
[ -r "$evidence_checker" ] || exit 0

target="$(git rev-parse HEAD 2>/dev/null)" || exit 0
[ -z "$target" ] && exit 0

# Prefer an explicit --base on the command; fall back to the usual default branch.
#
# Word-splitting $cmd loses quoting, so `--body 'see --base foo'` hands us `foo`
# as the base. That ref does not resolve, the block below exits 0, and the gate
# is silently off — fail-open in the one direction that matters. This very PR's
# body contains the token `--base`, which is how the hole was found. Tokenize the
# way the shell would instead, isolate the matched command's argument vector,
# and read --base only as an option, never as prose or a later command's option.
base_ref="$(printf '%s' "$cmd" | python3 -c '
import shlex, sys
try:
    lexer = shlex.shlex(sys.stdin.read(), posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = "#"
    tokens = list(lexer)
except ValueError:
    sys.exit(0)          # unbalanced quotes: let the default stand
command_start = next(
    (i for i in range(len(tokens) - 2)
     if tokens[i:i + 3] == ["gh", "pr", "create"]),
    None,
)
if command_start is None:
    sys.exit(0)
tokens = tokens[command_start + 3:]
skip = {"--body", "--title", "-b", "-t", "--body-file", "-F"}
i = 0
while i < len(tokens):
    tok = tokens[i]
    if tok and all(char in ";&|" for char in tok):
        break
    if tok in skip:      # step over the value so prose is never scanned
        i += 2
        continue
    if tok.startswith("--base="):
        print(tok.split("=", 1)[1])
        break
    if tok == "--base" and i + 1 < len(tokens):
        print(tokens[i + 1])
        break
    i += 1
' 2>/dev/null)"
[ -z "$base_ref" ] && base_ref="main"

# A bare `main` is the stale LOCAL branch; the PR is cut against the remote, and
# the two disagree often enough to render a different verdict on the same HEAD.
#
# An unresolvable ref falls back to the default branch rather than exiting: base
# extraction must never be load-bearing for whether the gate runs at all. Letting
# a ref that does not resolve skip the check turns any odd command line into a
# silent bypass, which is the one fail-open direction that costs something. A
# slightly-wrong base only widens the receipt window, so it cannot false-deny.
base_sha=""
for candidate in "origin/$base_ref" "$base_ref" "origin/main" "main"; do
  base_sha="$(git rev-parse --verify --quiet "$candidate^{commit}" 2>/dev/null)" || base_sha=""
  [ -n "$base_sha" ] && break
done
[ -z "$base_sha" ] && exit 0   # no base resolves at all: genuinely cannot judge

# Verify against the FORK POINT, never the base tip. Being behind origin/main is
# the normal state at PR-open, and the checker rejects a base that is not an
# ancestor of the target with a `landing-evidence:` verdict — which would read as
# "no receipt" and deny a PR whose only fault is needing an integrate, with a
# remedy (emit a receipt) that cannot fix it. land-pr.sh treats behind-ness as
# "integrate and come back"; this hook must not turn it into a block.
base="$(git merge-base "$base_sha" HEAD 2>/dev/null)" || exit 0
[ -z "$base" ] && exit 0

evidence_out="$(bash "$evidence_checker" --v2 --target "$target" --base "$base" --mode pr 2>&1)"
evidence_rc=$?
[ "$evidence_rc" -eq 0 ] && exit 0

# Non-zero without a verdict means the checker could not run. Stay out of the way.
printf '%s' "$evidence_out" | grep -q 'landing-evidence:' || exit 0

# Escape backslashes before quotes, and drop control characters: an unparseable
# deny payload is silently discarded, which would retire this rule without a trace.
reason="$(printf '%s' "$evidence_out" | tr '\n' ' ' | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')"

# Only a missing receipt is fixed by emitting one. The checker uses the same
# prefix for evidence it considers present but wrong (a modified base receipt, a
# receipt-count violation), and telling someone to re-emit for those sends them
# round a loop that cannot terminate. Name the remedy only when it is the remedy.
case "$evidence_out" in
  *"no new v2 review receipt"* | *"no review ledger record"* | *"does not cover"*)
    remedy="Run scripts/emit-review-evidence.sh, commit the receipt it writes under proof/preship-reviews/v2/, then open the PR." ;;
  *)
    remedy="Resolve what the checker reports below before opening the PR; re-emitting a receipt does not fix this class of failure." ;;
esac

cat <<JSON
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Pre-ship evidence does not cover HEAD, so this PR would be refused at landing. ${remedy} Checker said: ${reason}"}}
JSON
exit 0
