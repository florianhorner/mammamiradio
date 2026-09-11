#!/usr/bin/env bash
# Self-test for the "Report unlanded coverage floors" step in quality.yml.
#
# Hermetic: the step's script is extracted straight out of the workflow and run
# against a stub `gh` on PATH, so no network, no credentials, no real issues.
#
# Why this test exists: the step it guards replaced `git push || echo "..."`,
# which reported a real failure into a log nobody reads and let the job go green.
# The floors were silently unprotected from 2026-04-15. A test that only proved
# the happy path would reproduce exactly that blindness, so every case below
# asserts on the REPORTING behaviour, including the two do-nothing cases.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
WORKFLOW="$REPO_ROOT/.github/workflows/quality.yml"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# This test parses the workflow rather than duplicating its script, which needs
# pyyaml. CI has it (requirements.txt, installed by setup-python-ci before this
# step), but a bare shell outside the venv does not — say so plainly instead of
# dying on an import traceback.
python3 -c 'import yaml' 2>/dev/null || fail \
  "pyyaml is required to parse the workflow. Run inside the venv: source .venv/bin/activate"

# Extract the step's `run:` block from the workflow rather than duplicating it,
# so the test cannot drift away from the thing it claims to test.
python3 - "$WORKFLOW" > "$TMP/step.sh" <<'PY'
import sys, yaml
wf = yaml.safe_load(open(sys.argv[1]))
steps = wf["jobs"]["coverage-ratchet"]["steps"]
matches = [s for s in steps if s.get("name") == "Report unlanded coverage floors"]
if len(matches) != 1:
    sys.exit(f"expected exactly 1 reporting step, found {len(matches)}")
sys.stdout.write(matches[0]["run"])
PY
[[ -s "$TMP/step.sh" ]] || fail "could not extract the reporting step from quality.yml"
pass "reporting step extracted from quality.yml"

# Stub gh. Records every subcommand, and answers `issue list` from GH_STUB_EXISTING.
cat > "$TMP/gh" <<'EOF'
#!/usr/bin/env bash
echo "gh $*" >> "$GH_STUB_LOG"
case "$1 $2" in
  "issue list") printf '%s' "${GH_STUB_EXISTING:-}" ;;
  *) : ;;
esac
EOF
chmod +x "$TMP/gh"

run_case() {
  local landed="$1" existing="$2"
  export GH_STUB_LOG="$TMP/log"; : > "$GH_STUB_LOG"
  export GH_STUB_EXISTING="$existing"
  export LANDED="$landed"
  export LABEL="coverage-ratchet-stale"
  export TITLE="Coverage floors are stale: the auto-ratchet cannot land them"
  export RUNNER_TEMP="$TMP"
  printf 'diff --git a/.coverage-floors.json b/.coverage-floors.json\n+  "x": 99,\n' > "$TMP/floors.diff"
  PATH="$TMP:$PATH" bash "$TMP/step.sh" > "$TMP/out" 2>&1 \
    || fail "step exited non-zero (landed=$landed existing='$existing'): $(cat "$TMP/out")"
}

# 1. Floors did not land and nothing is tracked yet -> open exactly one issue.
run_case false ""
grep -q "gh issue create" "$GH_STUB_LOG" || fail "unlanded floors did not open an issue"
grep -q "gh label create" "$GH_STUB_LOG" || fail "label was not ensured before creating the issue"
[[ "$(grep -c 'gh issue create' "$GH_STUB_LOG")" -eq 1 ]] || fail "opened more than one issue"
pass "unlanded floors open exactly one tracked issue"

# The issue body must carry the actual diff, or the fix is not copy-pasteable.
grep -q 'coverage-floors.json' "$TMP/coverage-ratchet-issue.md" \
  || fail "issue body does not include the computed floors diff"
grep -q 'make coverage-ratchet' "$TMP/coverage-ratchet-issue.md" \
  || fail "issue body does not tell the reader how to fix it"
pass "issue body carries the diff and the fix command"

# 2. Already tracked -> must not file a duplicate on every later push to main.
run_case false "412"
grep -q "gh issue create" "$GH_STUB_LOG" && fail "filed a duplicate issue while one was open"
pass "an already-tracked drift does not file a duplicate"

# 3. Floors landed while an issue is open -> close it automatically.
run_case true "412"
grep -q "gh issue close 412" "$GH_STUB_LOG" || fail "did not close the issue after floors landed"
pass "landing the floors closes the tracked issue"

# 4. Floors landed with nothing open -> no issue traffic at all.
run_case true ""
grep -qE "gh issue (create|close)" "$GH_STUB_LOG" && fail "touched issues when there was nothing to report"
pass "a clean run reports nothing"

# 5. The guard that makes this step reachable: the job needs issues: write.
python3 - "$WORKFLOW" <<'PY' || exit 1
import sys, yaml
job = yaml.safe_load(open(sys.argv[1]))["jobs"]["coverage-ratchet"]
perms = job.get("permissions") or {}
assert perms.get("issues") == "write", f"coverage-ratchet job lacks issues: write ({perms})"
PY
pass "coverage-ratchet job grants issues: write"

# 6. The regression this whole change exists to prevent: a swallowed push.
if grep -qE 'git push \|\| (echo|true)' "$WORKFLOW"; then
  fail "quality.yml still swallows a failing push with || echo — the 2026-04-15 silent-inertia bug"
fi
pass "no swallowed git push remains in quality.yml"

echo
echo "All coverage-ratchet reporting cases passed."
