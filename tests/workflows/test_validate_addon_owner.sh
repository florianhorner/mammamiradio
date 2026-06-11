#!/usr/bin/env bash
# Regression guard for the GHCR owner resolution in scripts/validate-addon.sh.
#
# In a worktree with no git remote and no gh CLI, the expected image owner must
# fall back to repository.yaml's `url:` (the single-source repo manifest), NOT to
# an empty owner. Before the fix, the owner pipeline ended in `sed`, which exits 0
# on empty input, so the `|| gh || echo unknown` chain never fired and OWNER became
# "" — yielding the bogus expectation `ghcr.io//mammamiradio-addon-{arch}` and a
# false validation failure on fresh/no-remote checkouts.
#
# This shims `git` and `gh` to be unavailable (validate-addon.sh only calls `git`
# at the owner-detection line, so the stub disturbs nothing else), runs the real
# validator against the real repo, and asserts the owner resolves to the manifest.
# No network.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

VALIDATE="scripts/validate-addon.sh"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

[ -f repository.yaml ] || fail "repository.yaml not found at repo root"
grep -q '^url:.*github.com' repository.yaml || fail "repository.yaml has no github url: to fall back to"

# Shim git + gh to "unavailable" so owner detection must use repository.yaml.
SHIM="$(mktemp -d)"
trap 'rm -rf "$SHIM"' EXIT
printf '#!/usr/bin/env sh\nexit 1\n' > "$SHIM/git"
printf '#!/usr/bin/env sh\nexit 1\n' > "$SHIM/gh"
chmod +x "$SHIM/git" "$SHIM/gh"

OUT="$(PATH="$SHIM:$PATH" bash "$VALIDATE" 2>&1)" || true

if echo "$OUT" | grep -qF 'ghcr.io//mammamiradio-addon-{arch}'; then
  fail "owner resolved to empty (ghcr.io//...) — repository.yaml fallback not working"
fi
echo "$OUT" | grep -qF 'Image path: ghcr.io/florianhorner/mammamiradio-addon-{arch}' \
  || fail "expected florianhorner owner from repository.yaml fallback"

pass "owner falls back to repository.yaml when git/gh are unavailable"
echo "All validate-addon owner-fallback scenarios passed."
