#!/bin/bash
# Smoke test for scripts/docker-entrypoint.sh.
#
# Verifies the standalone Docker entrypoint's ADMIN_TOKEN handling without
# building the image: cold start generates + persists a token, restart loads
# the persisted value, externally-set ADMIN_TOKEN is honored, and a
# read-only /data degrades to logging the token once.

set -e

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENTRYPOINT="$REPO_ROOT/scripts/docker-entrypoint.sh"
PASS=0
FAIL=0

assert() {
    local name="$1"
    local got="$2"
    local want="$3"
    if [ "$got" = "$want" ]; then
        echo "  PASS  $name"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $name  got='$got'  want='$want'"
        FAIL=$((FAIL + 1))
    fi
}

assert_nonempty() {
    local name="$1"
    local got="$2"
    if [ -n "$got" ]; then
        echo "  PASS  $name (got: ${got:0:8}...)"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $name  got=empty"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== docker-entrypoint.sh smoke tests ==="

# Case 1: cold start with writable /data — generate and persist
TMP1="$(mktemp -d)"
unset ADMIN_TOKEN
TOKEN1="$(MAMMAMIRADIO_ADMIN_TOKEN_FILE="$TMP1/admin_token" "$ENTRYPOINT" sh -c 'echo "$ADMIN_TOKEN"')"
assert_nonempty "cold start generates ADMIN_TOKEN" "$TOKEN1"
[ -f "$TMP1/admin_token" ] && echo "  PASS  token file persisted" && PASS=$((PASS + 1)) || { echo "  FAIL  token file missing"; FAIL=$((FAIL + 1)); }
assert "persisted file contents match exported var" "$(cat "$TMP1/admin_token")" "$TOKEN1"

# Case 2: restart with existing file — load persisted value
unset ADMIN_TOKEN
TOKEN2="$(MAMMAMIRADIO_ADMIN_TOKEN_FILE="$TMP1/admin_token" "$ENTRYPOINT" sh -c 'echo "$ADMIN_TOKEN"')"
assert "restart loads persisted token (same value)" "$TOKEN2" "$TOKEN1"

# Case 3: externally-set ADMIN_TOKEN wins — don't overwrite the file or the value
ADMIN_TOKEN="external-pinned-token" \
  TOKEN3="$(MAMMAMIRADIO_ADMIN_TOKEN_FILE="$TMP1/admin_token" ADMIN_TOKEN="external-pinned-token" "$ENTRYPOINT" sh -c 'echo "$ADMIN_TOKEN"')"
assert "external ADMIN_TOKEN is honored verbatim" "$TOKEN3" "external-pinned-token"
assert "persisted file was NOT overwritten by external value" "$(cat "$TMP1/admin_token")" "$TOKEN1"

# Case 4: read-only /data degrades to logging the token.
# Skipped under root: uid 0 ignores the directory write bit, so `chmod 555`
# does not make the path unwritable and the entrypoint would still create the
# file. The image itself runs as the non-root `radio` user, so the real
# runtime path is the one exercised here on non-root hosts.
if [ "$(id -u)" = "0" ]; then
    echo "  SKIP  read-only /data case (running as root — write bit not enforced)"
else
    TMP2="$(mktemp -d)"
    chmod 555 "$TMP2"
    unset ADMIN_TOKEN
    TOKEN4="$(MAMMAMIRADIO_ADMIN_TOKEN_FILE="$TMP2/admin_token" "$ENTRYPOINT" sh -c 'echo "$ADMIN_TOKEN"' 2>/dev/null)"
    assert_nonempty "read-only /data still generates ADMIN_TOKEN" "$TOKEN4"
    [ ! -f "$TMP2/admin_token" ] && echo "  PASS  read-only /data does not create token file" && PASS=$((PASS + 1)) || { echo "  FAIL  unexpected file created"; FAIL=$((FAIL + 1)); }
    chmod 755 "$TMP2"
    rm -rf "$TMP2"
fi

# Cleanup
rm -rf "$TMP1"

echo "=== $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
