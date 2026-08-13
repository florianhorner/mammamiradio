#!/usr/bin/env bash
# Self-test for scripts/check-advertised-version.sh
#
# Hermetic: every case drives the registry lookup through MAMMAMIRADIO_TAG_PROBE,
# a stub the script calls instead of talking to GHCR. No network, no credentials.
# That matters twice over — CI must not depend on ghcr.io being up, and the
# script's whole point is that an unreachable registry is NOT a failed release.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/check-advertised-version.sh"

[[ -x "$SCRIPT" ]] || chmod +x "$SCRIPT"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Stub probes. Each takes <repo> <tag> and echoes present|absent|unknown.
make_probe() {
  local name="$1" answer="$2"
  cat > "$TMP/$name" <<EOF
#!/usr/bin/env bash
echo "$answer"
EOF
  chmod +x "$TMP/$name"
  echo "$TMP/$name"
}

PROBE_PRESENT="$(make_probe probe-present present)"
PROBE_ABSENT="$(make_probe probe-absent absent)"
PROBE_UNKNOWN="$(make_probe probe-unknown unknown)"

# A probe that answers only for aarch64, to prove a partial promotion still fails.
cat > "$TMP/probe-partial" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  *aarch64) echo present ;;
  *)        echo absent ;;
esac
EOF
chmod +x "$TMP/probe-partial"

# A probe that crashes, to prove a broken probe degrades to unknown, not to failure.
cat > "$TMP/probe-broken" <<'EOF'
#!/usr/bin/env bash
exit 3
EOF
chmod +x "$TMP/probe-broken"

run() {
  MAMMAMIRADIO_TAG_PROBE="$1" bash "$SCRIPT" "${@:2}"
}

# Case 1: tag published for both arches => PASS, exit 0
if ! run "$PROBE_PRESENT" >/dev/null 2>&1; then
  fail "published tag should exit 0"
fi
pass "published tag on both arches exits 0"

# Case 2: tag missing => FAIL, exit 1. This is the 2026-07-12..08-02 state.
if run "$PROBE_ABSENT" >/dev/null 2>&1; then
  fail "missing tag should exit 1, was allowed"
fi
pass "missing tag exits 1 (the uninstallable-stable state)"

# Case 3: the failure message must name the version and tell the operator what to do.
out="$(run "$PROBE_ABSENT" --version 9.9.9 2>&1 || true)"
case "$out" in
  *9.9.9*) : ;;
  *) fail "failure output should name the version, got: $out" ;;
esac
case "$out" in
  *"git revert"*) : ;;
  *) fail "failure output must tell the operator how to recover, got: $out" ;;
esac
# Reverting the cut COMMIT is the only recovery that passes both gates: a
# version-files-only revert leaves the changelog head at the unreleased number,
# which check-changelog-sync.sh refuses locally and pre-release-check.sh fails in CI.
case "$out" in
  *"not just the version files"*) : ;;
  *) fail "failure output must warn against a version-files-only revert, got: $out" ;;
esac
pass "failure names the version and a recovery that actually works"

# Case 4: registry unreachable => UNKNOWN, exit 0. A DNS blip is not a broken release.
if ! run "$PROBE_UNKNOWN" >/dev/null 2>&1; then
  fail "unreachable registry should exit 0 (UNKNOWN), not fail the release"
fi
pass "unreachable registry exits 0 as UNKNOWN"

out="$(run "$PROBE_UNKNOWN" 2>&1 || true)"
case "$out" in
  *UNKNOWN*) : ;;
  *) fail "unreachable registry should say UNKNOWN, got: $out" ;;
esac
pass "unreachable registry reports UNKNOWN explicitly"

# Case 5: one arch promoted, the other not => still FAIL. A partial promotion is
# exactly what a half-failed addon-release.yml leaves behind.
if run "$TMP/probe-partial" >/dev/null 2>&1; then
  fail "partial promotion (one arch missing) should exit 1"
fi
pass "partial promotion exits 1"

# Case 6: a probe that errors out degrades to unknown, never to a false FAIL.
if ! run "$TMP/probe-broken" >/dev/null 2>&1; then
  fail "a crashing probe should degrade to UNKNOWN (exit 0), not report a missing tag"
fi
pass "crashing probe degrades to UNKNOWN, not to a false failure"

# Case 7: --version overrides config.yaml.
out="$(run "$PROBE_PRESENT" --version 1.2.3 2>&1)"
case "$out" in
  *1.2.3*) : ;;
  *) fail "--version should override config.yaml, got: $out" ;;
esac
pass "--version overrides config.yaml"

# Case 8: bad usage is exit 2, distinct from a real drift failure (exit 1).
set +e
run "$PROBE_PRESENT" --version >/dev/null 2>&1; rc=$?
set -e
[ "$rc" = "2" ] || fail "--version with no value should exit 2, got $rc"
pass "usage error exits 2, distinct from drift failure"

# Case 9: the real config.yaml is readable and names a semver. Guards against the
# script silently checking an empty string if the config format ever changes.
out="$(run "$PROBE_PRESENT" 2>&1)"
case "$out" in
  *"Advertised add-on version: "[0-9]*.[0-9]*.[0-9]*) : ;;
  *) fail "should read a semver version out of config.yaml, got: $out" ;;
esac
pass "reads a semver version from the real config.yaml"

# Case 10: the check must NOT be wired into pre-release-check.sh. Doing so
# deadlocks every `chore(release): cut X.Y.Z` PR, because quality.yml runs
# pre-release-check.sh on any version-file PR and the cut names a version whose
# image does not exist yet.
# Guard the whole cut-PR gate closure, not just the two obvious files. Every entry
# point below runs on a PR that touches a version file, so adding the registry check
# to ANY of them deadlocks the cut. Match invocations, not mentions —
# pre-release-check.sh deliberately explains in a comment why the check lives
# elsewhere, and that comment must stay legal.
CUT_PATH_FILES="
scripts/pre-release-check.sh
scripts/check-merge-gate.sh
scripts/check-release-invariants.sh
scripts/check-version-sync.sh
.github/workflows/quality.yml
.pre-commit-config.yaml
"
for rel in $CUT_PATH_FILES; do
  [ -f "$REPO_ROOT/$rel" ] || continue
  if grep -v '^[[:space:]]*#' "$REPO_ROOT/$rel" | grep -q "check-advertised-version"; then
    fail "check-advertised-version.sh must not run on the cut-PR path ($rel) — it names a version whose image is not built yet, so the cut PR could never go green"
  fi
done
pass "absent from the whole cut-PR gate closure (6 files)"

# The Makefile needs recipe-scoped matching: a standalone target that runs the check
# is legitimate, but the `pre-release` recipe specifically must not.
if awk '/^pre-release:/{inrecipe=1; next} /^[^\t]/{inrecipe=0} inrecipe' "$REPO_ROOT/Makefile" \
   | grep -v '^[[:space:]]*#' | grep -q "check-advertised-version"; then
  fail "check-advertised-version.sh must not be in the pre-release make recipe (cut-PR deadlock)"
fi
pass "not in the make pre-release recipe (cut-PR deadlock guard)"

# ---------------------------------------------------------------------------
# The REAL probe path. Everything above stubs probe_tag out entirely, so the
# HTTP-code -> verdict mapping the script exists to get right was untested.
# Shim `curl` onto PATH instead (the script calls it unqualified), which keeps
# this hermetic — no network, no credentials.
# ---------------------------------------------------------------------------
SHIM="$TMP/bin"
mkdir -p "$SHIM"
cat > "$SHIM/curl" <<'EOF'
#!/usr/bin/env bash
# The manifest request is the one asking for an HTTP code; the other is the token.
for a in "$@"; do
  if [ "$a" = "--write-out" ]; then
    printf '%s' "${SHIM_MANIFEST_CODE}"
    exit 0
  fi
done
printf '%s' "${SHIM_TOKEN_BODY}"
EOF
chmod +x "$SHIM/curl"

run_real() {
  local token_body="$1" manifest_code="$2"
  shift 2
  env PATH="$SHIM:$PATH" \
      SHIM_TOKEN_BODY="$token_body" \
      SHIM_MANIFEST_CODE="$manifest_code" \
      bash "$SCRIPT" "$@"
}

GOOD_TOKEN='{"token":"abc123"}'

# Case 12: a real 200 means the tag is published.
if ! run_real "$GOOD_TOKEN" 200 --version 1.0.0 >/dev/null 2>&1; then
  fail "HTTP 200 should PASS"
fi
pass "real probe: HTTP 200 -> PASS"

# Case 13: 404 is the registry actually answering "no such tag" -> the only FAIL.
if run_real "$GOOD_TOKEN" 404 --version 1.0.0 >/dev/null 2>&1; then
  fail "HTTP 404 should FAIL"
fi
pass "real probe: HTTP 404 -> FAIL (the only definitive answer)"

# Case 14: everything else is "we did not get an answer" -> UNKNOWN, never FAIL.
for code in 401 403 429 500 503 000; do
  if ! run_real "$GOOD_TOKEN" "$code" --version 1.0.0 >/dev/null 2>&1; then
    fail "HTTP $code should be UNKNOWN (exit 0), not a failed release"
  fi
done
pass "real probe: 401/403/429/500/503/000 -> UNKNOWN, exit 0"

# Case 15: REGRESSION. An explicit JSON null token used to slip past the
# empty-token guard as the string "None" and go out as `Bearer None`. Against a
# registry that answers 404 to a bad bearer, that turned an auth failure into a
# definitive FAIL — the exact false alarm this script must never raise.
if ! run_real '{"token":null}' 404 --version 1.0.0 >/dev/null 2>&1; then
  fail "a null token must degrade to UNKNOWN, never FAIL (Bearer None regression)"
fi
pass "real probe: null token -> UNKNOWN, not a false FAIL"

# Case 16: token missing entirely, or the endpoint returning non-JSON (an HTML
# error page from a proxy or outage) -> UNKNOWN.
for body in '{}' '' '<html>502 Bad Gateway</html>'; do
  if ! run_real "$body" 404 --version 1.0.0 >/dev/null 2>&1; then
    fail "unusable token body '$body' must degrade to UNKNOWN, not FAIL"
  fi
done
pass "real probe: missing/empty/non-JSON token body -> UNKNOWN"

# Case 17: the machine-readable verdict. PASS and UNKNOWN share exit 0, so
# advertised-version.yml branches on this line instead. Getting it wrong let an
# unreachable registry close a live drift issue with a false all-clear.
verdict_of() { printf '%s\n' "$1" | sed -n 's/^VERDICT: //p' | tail -1; }

v="$(verdict_of "$(run "$PROBE_PRESENT" 2>&1 || true)")"
[ "$v" = "pass" ] || fail "published tag should emit 'VERDICT: pass', got '$v'"

v="$(verdict_of "$(run "$PROBE_ABSENT" 2>&1 || true)")"
[ "$v" = "fail" ] || fail "missing tag should emit 'VERDICT: fail', got '$v'"

v="$(verdict_of "$(run "$PROBE_UNKNOWN" 2>&1 || true)")"
[ "$v" = "unknown" ] || fail "unreachable registry should emit 'VERDICT: unknown', got '$v'"
pass "emits a machine-readable VERDICT for all three outcomes"

# Case 18: the workflow must branch on the verdict, never on the exit code alone.
WF="$REPO_ROOT/.github/workflows/advertised-version.yml"
# shellcheck disable=SC2016  # these patterns match literal shell text in the YAML
grep -q 'VERDICT: //p' "$WF" || fail "workflow must parse the VERDICT line"
# shellcheck disable=SC2016
grep -q '\[ "\$VERDICT" = "fail" \]' "$WF" || fail "workflow must open the issue only on a fail verdict"
# shellcheck disable=SC2016
grep -q '\[ "\$VERDICT" != "pass" \]' "$WF" || fail "workflow must close the issue only on a pass verdict"
pass "workflow branches on VERDICT, so UNKNOWN cannot close a live drift issue"

echo
echo "All 19 advertised-version cases passed."
