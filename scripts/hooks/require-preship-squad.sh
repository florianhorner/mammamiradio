#!/usr/bin/env bash
# PreToolUse(Bash) guard — raw merges must go through the landing wrapper.
#
# Review remains mandatory through /ship, but local ledgers and committed
# review receipts are not PR admission requirements.
#
# Raw merge attempts are denied OUTRIGHT — landing goes through
#      scripts/land-pr.sh (the landing contract in CLAUDE.md "Quality gates").
#      This blocks `gh pr merge` and mutating `gh api` merge calls (REST
#      /pulls/<n>/merge PUT, plus GraphQL mergePullRequest/auto-merge
#      mutations). The wrapper checks branch freshness, bot threads and cut
#      admission before head-matched arming. The
#      wrapper's internal gh calls run inside its own process and never hit this
#      hook.
#      Exception: `gh pr merge --disable-auto` (disarming a queued merge) is
#      a cancel operation and passes.
#
# History of the retired PR rule: on the god-module refactor, PRs were opened with bare
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
# FAILS OPEN: an internal input/parsing error
# exits 0 (allow). A bug in this guard can never block a PR. The ONLY paths that
# block are the explicit deny families below.

input="$(cat 2>/dev/null)"
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // ""' 2>/dev/null)" || exit 0

deny_raw_merge() {
  cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Raw GitHub merge commands are retired. Land via scripts/land-pr.sh <PR#> — it refuses behind/conflicted branches, checks blocking reviews and cut admission, and arms auto-merge with --match-head-commit. This hook denies gh pr merge and mutating gh api merge attempts. Disarming with gh pr merge --disable-auto is allowed. See CLAUDE.md 'Landing contract'."}}
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
if printf '%s' "$cmd" | grep -Eq '(^|[;&|()[:space:]])gh[[:space:]]+pr[[:space:]]+merge([[:space:]]|$)'; then
  # --disable-auto must be an argument OF the merge command itself (no shell
  # operator between them) — `... merge 5 && echo "--disable-auto"` is a
  # bypass attempt, not a disarm.
  if printf '%s' "$cmd" | grep -Eq '(^|[;&|()[:space:]])gh[[:space:]]+pr[[:space:]]+merge([[:space:]][^;&|()]*)?[[:space:]]--disable-auto([[:space:]]|$|[^-A-Za-z])'; then
    exit 0
  fi
  deny_raw_merge
fi

# Rule 2b: deny raw GitHub API merge attempts. Read-only `gh api` calls still
# pass; the REST deny requires the pull merge endpoint AND an explicit PUT.
if printf '%s' "$cmd" | grep -Eq '(^|[;&|()[:space:]])gh[[:space:]]+api([[:space:]]|$)'; then
  if printf '%s' "$cmd" | grep -Eq '/pulls/[0-9]+/merge([^[:alnum:]_-]|$)' \
    && printf '%s' "$cmd" | grep -Eiq '(^|[[:space:]])((--method)(=|[[:space:]]+)PUT|-X(=|[[:space:]]*)PUT)([^A-Za-z]|$)'; then
    deny_raw_merge
  fi

  if printf '%s' "$cmd" | grep -Eq '(^|[;&|()[:space:]])gh[[:space:]]+api[[:space:]]+graphql([[:space:]]|$)' \
    && { printf '%s' "$cmd" | grep -Eq "$_graphql_merge_pattern" || graphql_file_payload_mentions_merge; }; then
    deny_raw_merge
  fi
fi

# Retired argument-reader helpers remain for their standalone regression
# tests. Admission does not invoke them or read a ledger/receipt.

# Reads the PR-opening command's argument vector, in one of two modes.
#
# Shared by the scope check below and the --base lookup further down, so the two
# cannot drift: the tokenizer is the delicate part (see the --base comment on why
# word-splitting $cmd is wrong) and having one copy of it means a fix lands in
# both places at once.
#
# Word-splitting $cmd loses quoting, so `--body 'see --base foo'` would hand back
# `foo`. Tokenize the way the shell would, isolate each matched command's argv,
# stop at a shell operator OR A NEWLINE, and step over option VALUES so prose is
# never scanned. Unbalanced quotes print nothing.
#
# The newline half was a bypass. shlex with whitespace_split folds a newline into
# whitespace, and the argv scan stopped only on `;&|`, so in
#
#     gh pr create --fill
#     gh pr create --repo other/repo --fill
#
# the FIRST (local, flagless) command absorbed the second command's target, both
# records read foreign, and the guard stood aside for a local PR. Three review
# bots found it independently. Commands are therefore split per line first; a
# trailing backslash still continues a line, as the shell does.
#
#   option <name>  first opening command, first match -- the pre-existing --base
#                  behaviour, unchanged and differentially verified against it.
#   targets        ONE LINE PER opening command in the whole command string,
#                  holding that command's target repo or empty for none. Two
#                  things the option mode deliberately does not do, both of which
#                  were live bypasses:
#                    * the real CLI is LAST-wins on a repeated flag, so
#                      `--repo foreign --repo local` lands locally while a
#                      first-match read calls it foreign;
#                    * a command string can hold several opening commands, and
#                      reading only the first let a foreign one exempt a local
#                      one behind `&&`.
gh_create_args() {
  printf '%s' "$cmd" | ARG_MODE="$1" ARG_NAME="${2:-}" python3 -c '
import os, re, shlex, sys

mode = os.environ["ARG_MODE"]
name = os.environ.get("ARG_NAME", "")
raw = sys.stdin.read()
# A newline ends a command. A backslash-newline is deleted outright,
# as the shell does, so a token split across lines rejoins.
raw = re.sub(r"\\\n", "", raw)   # POSIX deletes the pair; a space splits a token
tokens = []
for line in raw.split("\n"):
    if not line.strip():
        continue
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        line_tokens = list(lexer)
    except ValueError:
        sys.exit(0)      # unbalanced quotes: let the caller default stand
    if tokens:
        tokens.append(";")   # the newline itself, as the operator it is
    tokens.extend(line_tokens)

# Every value-taking flag of the opening command, long and short, verified
# against its own --help. A flag missing from this set lets its VALUE be read as
# an option: "--label --repo=other/repo" made the scan report a foreign target
# while the CLI landed the PR here. Boolean flags (--draft, --fill, --web, ...)
# are deliberately absent. The "--flag=value" form is one token and consumes
# nothing, so only an exact bare match skips the next token.
SKIP = {
    "--assignee", "-a", "--base", "-B", "--body", "-b", "--body-file", "-F",
    "--head", "-H", "--label", "-l", "--milestone", "-m", "--project", "-p",
    "--recover", "--reviewer", "-r", "--template", "-T", "--title", "-t",
}


def is_operator(tok):
    return bool(tok) and all(char in ";&|" for char in tok)


def starts():
    return [i for i in range(len(tokens) - 2)
            if tokens[i:i + 3] == ["gh", "pr", "create"]]


def argv_after(start):
    """Tokens of one opening command, up to the next shell operator."""
    out = []
    for tok in tokens[start + 3:]:
        if is_operator(tok):
            break
        out.append(tok)
    return out


if mode == "option":
    positions = starts()
    if not positions:
        sys.exit(0)
    argv = argv_after(positions[0])
    # A caller asks for the long name; the CLI also accepts the shorthand, and
    # a value given as -B was never returned at all. Silent before this branch
    # existed, and silently SKIPPED once the short forms joined SKIP.
    wanted = {name} | {"--base": {"-B"}, "--repo": {"-R"}}.get(name, set())
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":      # end of options; nothing after it is a flag
            break
        # The requested name wins over SKIP. Both sets overlap now that SKIP
        # holds every value-taking flag, and skipping first meant asking for
        # --base returned nothing at all -- so base_ref fell back to "main" and
        # a stacked PR was verified against an older fork point than its own.
        if tok in SKIP and tok not in wanted:
            i += 2           # step over the value so prose is never scanned
            continue
        hit = next((w for w in wanted if tok.startswith(w + "=")), None)
        if hit:
            print(tok.split("=", 1)[1])
            break
        if tok in wanted and i + 1 < len(argv):
            print(argv[i + 1])
            break
        i += 1
    sys.exit(0)

if mode == "targets":
    positions = starts()
    if not positions:
        sys.exit(0)
    for start in positions:
        argv = argv_after(start)
        found = ""
        i = 0
        while i < len(argv):
            tok = argv[i]
            if tok == "--":  # end of options; nothing after it is a flag
                break
            if tok in SKIP:
                i += 2
                continue
            # Last-wins, matching the CLI: keep scanning rather than breaking.
            if tok.startswith("--repo="):
                found = tok.split("=", 1)[1]
            elif tok in ("--repo", "-R") and i + 1 < len(argv):
                found = argv[i + 1]
                i += 2
                continue
            elif tok.startswith("-R") and len(tok) > 2 and not tok.startswith("-R-"):
                # Attached shorthand, -Rowner/repo. pflag also accepts -R=owner/repo
                # and strips that one "="; keeping it made the value never match
                # this repo, so the guard stood aside for a PR landing here.
                rest = tok[2:]
                found = rest[1:] if rest.startswith("=") else rest
            i += 1
        # Prefixed because an empty record is meaningful and command
        # substitution eats a trailing blank line, which silently dropped a
        # flagless command sitting last in a chain.
        print("target:" + found)
    sys.exit(0)
' 2>/dev/null
}

# Canonical owner/repo, or empty when the input names no repository.
#
# One normalizer for both sides of the comparison, because the CLI accepts far
# more spellings than `owner/repo` and `https://host/owner/repo`: a bare
# `host/owner/repo`, `http://`, `ssh://git@host/owner/repo.git`, and a `host:port`
# form all reach the same repository. Each spelling this failed to reduce was a
# silent bypass in one direction and, on a non-https `origin`, a silently
# disarmed guard for this very repo in the other.
#
# Taking the LAST TWO path segments over-normalizes rather than under-normalizes:
# a same-named repo on a different host compares equal and the guard stays on.
# That is the direction to be wrong in.
norm_repo() {
  printf '%s' "$1" \
    | sed -E 's#^[A-Za-z][A-Za-z0-9+.-]*://##; s#^[^/]*@##; s#^([^/]*):#\1/#; s#/+$##' \
    | tr '[:upper:]' '[:lower:]' \
    | sed -E 's#\.git$##; s#/+$##' \
    | awk -F/ 'NF >= 2 { printf "%s/%s", $(NF - 1), $NF }'
}

# Canonical owner/repo for the CHECKOUT, or empty when this remote does not name
# a hosted repository at all.
#
# norm_repo alone reduces any string with two path segments, so a local clone
# (`file:///tmp/mammamiradio`, or a plain path) yielded `tmp/mammamiradio` and
# then mismatched its own `--repo florianhorner/mammamiradio` — switching the
# guard OFF for its own repo, which is the one direction that must never happen.
# A local remote carries no owner/repo, so the honest answer is "unknown", and
# unknown keeps the guard on.
origin_repo() {
  local url="$1"
  case "$url" in
    file://*|/*|.*|~*) return 0 ;;          # local clone: no owner/repo to know
    *://*)
      # A scheme needs a non-empty authority before the path.
      printf '%s' "$url" | grep -Eq '^[A-Za-z][A-Za-z0-9+.-]*://[^/]+/' || return 0
      ;;
    *@*:*) : ;;                             # scp-like: user@host:owner/repo
    *)     return 0 ;;                      # anything else names no host, so no
                                            # owner/repo: a bare `repos/foo` is a
                                            # relative path, not a repository
  esac
  norm_repo "$url"
}

# PR creation has no repository-local receipt/ledger admission gate.
