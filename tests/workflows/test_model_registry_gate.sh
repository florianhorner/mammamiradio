#!/usr/bin/env bash
# Self-test for pre-release-check.sh section 11 (scripts/model-registry-gate.sh): model
# registry review age and pinned-model liveness.
#
# The section must fail the cut on a registry nobody decided in 45 days (stale, future,
# malformed, or missing stamp), fail it on a pin the provider has deprecated or retired,
# and WAIVE, never PASS, when the provider docs cannot be read. Most cases drive the
# sourced section directly with stub reporters that print the real script's lines; four
# cases run the whole release check to prove the wiring, the CI skip, and the summary.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/pre-release-check.sh"
GATE="$REPO_ROOT/scripts/model-registry-gate.sh"
CHECKER="$REPO_ROOT/scripts/check_model_registry.py"
FIXTURES="$REPO_ROOT/tests/scripts/fixtures/model_registry"
PYTHON="$REPO_ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

die() { echo "FAIL: $1" >&2; exit 1; }
passed() { echo "PASS: $1"; }

cd "$REPO_ROOT"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Run the sourced section alone, with reporters shaped like the real script's, in a
# subshell that carries only the test-only passthroughs.
run_section() {  # $1 registry path, $2 fixture dir
  set +e
  out="$(MMR_MODEL_REGISTRY="$1" MMR_MODEL_REGISTRY_FIXTURES="$2" MMR_MODEL_REGISTRY_GATE=always \
    bash -c 'source "$0"; ok() { echo "  [PASS] $*"; }; fail() { echo "  [FAIL] $*"; }; waive() { echo "  [WAIVED] $*"; }; model_registry_gate "$1" "$2"' \
    "$GATE" "$PYTHON" "$CHECKER" 2>&1)"
  rc=$?
  set -e
}

# Run the whole release check (all sections) with the passthroughs.
run_script() {  # $1 registry path, $2 fixture dir, $3 gate mode, $4 GITHUB_ACTIONS value
  set +e
  out="$(GITHUB_ACTIONS="$4" MMR_MODEL_REGISTRY_GATE="$3" MMR_MODEL_REGISTRY="$1" MMR_MODEL_REGISTRY_FIXTURES="$2" MMR_REQUIRE_HA_RECEIPTS=0 bash "$SCRIPT" 2>&1)"
  rc=$?
  set -e
}

stamp_registry() {  # $1 output path, $2 replacement for the last_reviewed line ("" deletes it)
  if [[ -z "$2" ]]; then
    grep -v '^last_reviewed = ' model_registry.toml > "$1"
  else
    sed "s/^last_reviewed = .*/$2/" model_registry.toml > "$1"
  fi
}

TODAY="$(date -u +%Y-%m-%d)"
stamp_registry "$TMP/fresh.toml" "last_reviewed = \"$TODAY\""
stamp_registry "$TMP/stale.toml" 'last_reviewed = "2026-01-01"'

# Cases 1-4: a stale, future, malformed, or missing stamp fails on the age gate itself.
for spec in 'stale:last_reviewed = "2026-01-01"' 'future:last_reviewed = "2999-01-01"' 'malformed:last_reviewed = "yesterday"' 'missing:'; do
  name="${spec%%:*}"
  stamp_registry "$TMP/$name.toml" "${spec#*:}"
  run_section "$TMP/$name.toml" "$FIXTURES"
  grep -q "\[FAIL\] Model registry review age" <<<"$out" || die "a $name stamp should fail the review-age gate"
  grep -q "\[PASS\] Model registry review age" <<<"$out" && die "a $name stamp still passed the review-age gate"
done
grep -q '^last_reviewed' "$TMP/missing.toml" && die "test setup: the stamp line was not removed"
run_section "$TMP/stale.toml" "$FIXTURES"
grep -q "days old (max 45)" <<<"$out" || die "the stale failure must name the age and the limit"
grep -q -- "--providers" <<<"$out" || die "the stale failure must point at the --providers step (the way out)"
passed "stale, future, malformed and missing stamps fail on the review-age gate with the way out"

# Case 5: malformed TOML is a checker/source failure, not a malformed review stamp.
sed 's/^\[models\]$/[models/' model_registry.toml > "$TMP/malformed-toml.toml"
run_section "$TMP/malformed-toml.toml" "$FIXTURES"
grep -q "\[FAIL\] Model registry review age: the checker/source failed" <<<"$out" || die "malformed TOML should be reported as a checker failure"
grep -q "last_reviewed is missing, malformed, or older" <<<"$out" && die "malformed TOML was misreported as a review-stamp finding"
passed "malformed TOML fails closed as a checker/source error"

# Case 6: a fresh stamp plus the real captures passes both gates. The repo pins are a
# generation behind (drift), but every one is alive, and the cut asks only about liveness.
run_section "$TMP/fresh.toml" "$FIXTURES"
grep -q "\[PASS\] Model registry review age" <<<"$out" || die "a fresh stamp should pass the review-age gate"
grep -q "\[PASS\] Pinned models are alive" <<<"$out" || die "alive pins should pass the liveness gate"
grep -q "\[FAIL\]" <<<"$out" && die "fresh stamp and alive pins still reported a failure"
passed "fresh stamp and alive pins pass both gates"

# Case 7: a deprecated pin fails the liveness gate, on the gate itself, and is named.
mkdir -p "$TMP/deprecated"
cp "$FIXTURES"/*.md "$FIXTURES"/*.html "$TMP/deprecated/"
sed 's/^| claude-haiku-4-5-20251001  | Active .*$/| claude-haiku-4-5-20251001  | Deprecated    | September 15, 2026 | November 15, 2026 |/' \
  "$FIXTURES/anthropic-deprecations.md" > "$TMP/deprecated/anthropic-deprecations.md"
grep -q "| Deprecated    | September 15, 2026" "$TMP/deprecated/anthropic-deprecations.md" || die "test setup: the Haiku row was not flipped to Deprecated"
run_section "$TMP/fresh.toml" "$TMP/deprecated"
grep -q "\[FAIL\] A pinned model is deprecated or retired" <<<"$out" || die "a deprecated pin should fail the liveness gate"
grep -q "anthropic.haiku" <<<"$out" || die "the liveness report must name the deprecated pin"
passed "deprecated pin fails on the liveness gate and is named"

# Case 8: unreadable provider docs are WAIVED, named, and never a PASS or a FAIL.
run_section "$TMP/fresh.toml" "$TMP/does-not-exist"
grep -q "\[WAIVED\] NOT CHECKED: the provider docs could not be read" <<<"$out" || die "unreadable docs should waive the liveness gate, in plain words"
grep -q "\[PASS\] Pinned models are alive" <<<"$out" && die "unreadable docs must never count as a liveness PASS"
grep -q "\[FAIL\] A pinned model" <<<"$out" && die "unreadable docs must not be reported as a dead pin"
passed "unreachable docs waive the liveness gate without passing or failing it"

# Case 9: anything but auto|always is a hard error, never a silent skip (the receipt
# gate's rule). One value through the whole script proves it stops before section 1.
for bad in yes on 1 true; do
  set +e
  MMR_MODEL_REGISTRY_GATE="$bad" bash -c 'source "$0"; model_registry_gate_validate' "$GATE" >/dev/null 2>&1
  bad_rc=$?
  set -e
  [[ "$bad_rc" -eq 2 ]] || die "MMR_MODEL_REGISTRY_GATE=$bad should return 2, got $bad_rc"
done
run_script "$TMP/fresh.toml" "$FIXTURES" yes ""
[[ "$rc" -eq 2 ]] || die "the release check should exit 2 on an invalid gate value, got $rc"
grep -q "1. Version consistency" <<<"$out" && die "an invalid gate value must stop the release check before its first section"
passed "invalid MMR_MODEL_REGISTRY_GATE values are a hard error, before any section runs"

# Case 10 (whole script): the section is wired in, the fresh stamp passes both gates, and the
# only waiver in the summary is section 9's receipt waiver.
run_script "$TMP/fresh.toml" "$FIXTURES" always ""
grep -q "11. Model registry" <<<"$out" || die "section 11 is not wired into the release check"
grep -q "\[PASS\] Model registry review age" <<<"$out" || die "whole-script run: the review-age gate did not pass"
grep -q "\[PASS\] Pinned models are alive" <<<"$out" || die "whole-script run: the liveness gate did not pass"
grep -qE "Waived: 1( |$)" <<<"$out" || die "expected only the receipt waiver, got: $(grep -E 'Waived:' <<<"$out")"
passed "whole release check runs section 11 and counts no liveness waiver"

# Case 11 (whole script): unreadable docs add exactly one waiver to the summary.
run_script "$TMP/fresh.toml" "$TMP/does-not-exist" always ""
grep -qE "Waived: 2( |$)" <<<"$out" || die "expected the receipt waiver plus the liveness waiver, got: $(grep -E 'Waived:' <<<"$out")"
passed "unreachable docs are counted as a waiver in the summary"

# Case 12 (whole script): in CI, a diff that changes no version line is not a cut; the
# section notes itself and skips, even with a stale stamp that would otherwise FAIL.
if ! git rev-parse --verify --quiet origin/main >/dev/null 2>&1; then
  echo "SKIP: origin/main is not available, cannot assert the non-cut skip"
elif git diff origin/main...HEAD -- pyproject.toml ha-addon/mammamiradio/config.yaml custom_components/mammamiradio/manifest.json \
     | grep -qE '^\+[[:space:]]*"?version"?[[:space:]]*[:=]'; then
  echo "SKIP: this branch changes a version line, cannot assert the non-cut skip"
else
  run_script "$TMP/stale.toml" "$FIXTURES" auto true
  grep -q "\[NOTE\] not a release cut" <<<"$out" || die "a CI run without a version change should note the skip"
  grep -q "Model registry review age" <<<"$out" && die "the review-age gate ran on a non-cut CI diff"
  grep -q "Pinned models" <<<"$out" && die "the liveness gate ran on a non-cut CI diff"
  passed "non-cut CI diff skips both gates and says so"
fi

# Case 13: MMR_MODEL_REGISTRY_GATE=always forces the section even in CI.
run_script "$TMP/fresh.toml" "$FIXTURES" always true
grep -q "\[PASS\] Model registry review age" <<<"$out" || die "always should run the review-age gate in CI"
grep -q "\[PASS\] Pinned models are alive" <<<"$out" || die "always should run the liveness gate in CI"
passed "MMR_MODEL_REGISTRY_GATE=always runs both gates in CI"

echo "All model registry gate cases passed."
