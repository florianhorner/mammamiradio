#!/usr/bin/env bash
# Self-test for scripts/check-docs-safety.sh.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHECK="$ROOT/scripts/check-docs-safety.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

expect_failure() {
  local label=$1
  local expected=$2
  local file=$3
  local output

  if output=$(bash "$CHECK" "$file" 2>&1); then
    echo "FAIL: $label unexpectedly passed"
    exit 1
  fi
  if ! grep -Fq "$expected" <<< "$output"; then
    echo "FAIL: $label returned the wrong failure"
    echo "$output"
    exit 1
  fi
}

expect_success() {
  local label=$1
  local file=$2
  local output

  if ! output=$(bash "$CHECK" "$file" 2>&1); then
    echo "FAIL: $label unexpectedly failed"
    echo "$output"
    exit 1
  fi
}

expect_unsafe() {
  local label=$1
  local file=$2
  expect_failure "$label" "unsafe recovery instruction" "$file"
}

printf '# Guide\n' > "$TMP/guide.md"
printf '# Safe\n\nPlease do not SSH in to edit container or runtime files, delete live cache, or restart as an experiment.\n\n[Guide](guide.md)\n' > "$TMP/safe.md"
expect_success "directly negated warning" "$TMP/safe.md"

printf '# Safe\n\nPlease do not SSH in to edit container or runtime files,\ndelete live cache, or restart as an experiment.\n' > "$TMP/safe-wrapped.md"
expect_success "wrapped directly negated warning" "$TMP/safe-wrapped.md"

# shellcheck disable=SC2016  # literal Markdown code span in the fixture
printf '# Safe diagnosis\n\nFor diagnosis, `docker exec addon cat /data/options.json` is read-only.\n' > "$TMP/safe-docker-exec.md"
expect_success "read-only docker exec" "$TMP/safe-docker-exec.md"

printf '# Retired\n\nGo to Settings > Add-ons > Add-on Store.\n' > "$TMP/retired.md"
expect_failure "retired install path" "retired Home Assistant install wording" "$TMP/retired.md"

printf '# Unsafe\n\n**Fix**: SSH to the host, edit /addon_configs/mammamiradio/run.sh, then restart.\n' > "$TMP/unsafe.md"
expect_unsafe "live surgery" "$TMP/unsafe.md"

printf '# Unsafe\n\n**Fix**:\n1. SSH to the Home Assistant host.\n2. Edit runtime files in the running app.\n3. Restart the app.\n' > "$TMP/unsafe-multiline.md"
expect_unsafe "multiline live surgery" "$TMP/unsafe-multiline.md"

printf '# Unsafe\n\n**Fix**: Do not wait; SSH to the host, edit /data/options.json, then restart the app.\n' > "$TMP/unsafe-unrelated-negation.md"
expect_unsafe "unrelated negation does not suppress danger" "$TMP/unsafe-unrelated-negation.md"

printf '# Unsafe\n\nDo not SSH for diagnosis, but docker restart the app.\n' > "$TMP/unsafe-contrast-negation.md"
expect_unsafe "negation does not cross a contrasting clause" "$TMP/unsafe-contrast-negation.md"

printf '# Unsafe\n\n**Fix**: docker cp patch.py addon:/app/patch.py\n' > "$TMP/unsafe-docker-cp.md"
expect_unsafe "docker cp" "$TMP/unsafe-docker-cp.md"

# `docker exec` itself remains allowed for read-only diagnosis; a mutating
# command after it is the forbidden family this fixture pins.
printf '# Unsafe\n\n**Fix**: docker exec addon touch /tmp/patch-marker\n' > "$TMP/unsafe-docker-exec.md"
expect_unsafe "write-capable docker exec" "$TMP/unsafe-docker-exec.md"

printf '# Unsafe\n\n**Fix**: docker exec addon sh -c '"'"'echo changed > /app/runtime.py'"'"'\n' > "$TMP/unsafe-docker-exec-redirect.md"
expect_unsafe "docker exec redirection" "$TMP/unsafe-docker-exec-redirect.md"

printf '# Unsafe\n\n**Fix**: docker restart addon_mammamiradio\n' > "$TMP/unsafe-docker-restart.md"
expect_unsafe "docker restart" "$TMP/unsafe-docker-restart.md"

printf '# Unsafe\n\n**Fix**: ha apps restart mammamiradio\n' > "$TMP/unsafe-ha-apps-restart.md"
expect_unsafe "ha apps restart" "$TMP/unsafe-ha-apps-restart.md"

printf '# Unsafe\n\n**Fix**: pkill -f uvicorn\n' > "$TMP/unsafe-pkill.md"
expect_unsafe "pkill" "$TMP/unsafe-pkill.md"

printf '# Unsafe\n\n**Fix**: sed -i '"'"'s/old/new/'"'"' /config/radio.toml\n' > "$TMP/unsafe-sed.md"
expect_unsafe "sed -i" "$TMP/unsafe-sed.md"

printf '# Unsafe\n\n**Fix**: printf changed | tee /data/options.json\n' > "$TMP/unsafe-tee.md"
expect_unsafe "tee" "$TMP/unsafe-tee.md"

printf '# Unsafe\n\n**Fix**: write a replacement to /config/secrets.env\n' > "$TMP/unsafe-config-write.md"
expect_unsafe "write under config" "$TMP/unsafe-config-write.md"

printf '# Unsafe\n\n**Fix**: edit /data/options.json in place\n' > "$TMP/unsafe-data-write.md"
expect_unsafe "write under data" "$TMP/unsafe-data-write.md"

printf '# Unsafe\n\n**Fix**: create /addon_configs/mammamiradio/run.sh\n' > "$TMP/unsafe-addon-config-write.md"
expect_unsafe "write under addon_configs" "$TMP/unsafe-addon-config-write.md"

printf '# Unsafe\n\n**Fix**: delete the live cache before trying again\n' > "$TMP/unsafe-cache-delete.md"
expect_unsafe "cache deletion" "$TMP/unsafe-cache-delete.md"

printf '# Unsafe\n\n**Fix**: export MAMMAMIRADIO_SKIP_QUALITY_GATE=1\n' > "$TMP/unsafe-quality-bypass.md"
expect_unsafe "quality-gate bypass" "$TMP/unsafe-quality-bypass.md"

# shellcheck disable=SC2016  # literal Markdown code span in the fixture
printf '# Edge\n\nMamma Mi Radio (Edge) updates on every change merged to `main`.\n' > "$TMP/stale-edge.md"
expect_failure "stale Edge release promise" "incorrect Edge release wording" "$TMP/stale-edge.md"

printf '# Broken\n\n[Missing](does-not-exist.md)\n' > "$TMP/broken.md"
expect_failure "broken relative link" "target does not exist" "$TMP/broken.md"

printf '# Parenthesized target\n' > "$TMP/guide(with-notes).md"
printf '# Link\n\n[Guide](guide(with-notes).md)\n' > "$TMP/parenthesized-link.md"
expect_success "parenthesized relative target" "$TMP/parenthesized-link.md"

printf '# Link\n\n[Missing](missing(with-notes).md)\n' > "$TMP/broken-parenthesized-link.md"
expect_failure "broken parenthesized target" "target does not exist" "$TMP/broken-parenthesized-link.md"

printf '# Reference\n\n[Guide][details]\n\n[details]: guide.md\n' > "$TMP/reference-link.md"
expect_success "reference-style relative target" "$TMP/reference-link.md"

printf '# Reference\n\n[Missing][details]\n\n[details]: missing-reference.md\n' > "$TMP/broken-reference-link.md"
expect_failure "broken reference target" "target does not exist" "$TMP/broken-reference-link.md"

printf '# Reference\n\n[Missing][undefined]\n' > "$TMP/undefined-reference-link.md"
expect_failure "undefined reference label" "reference is undefined" "$TMP/undefined-reference-link.md"

printf '# Runbook\n\n## Edge channel (dev releases)\n' > "$TMP/runbook.md"
printf '# Link\n\n[Edge](runbook.md#edge-channel-dev-releases)\n' > "$TMP/fragment-link.md"
expect_success "GitHub-style heading fragment" "$TMP/fragment-link.md"

printf '# Link\n\n[Edge](runbook.md#edge-channel)\n' > "$TMP/broken-fragment-link.md"
expect_failure "broken GitHub-style heading fragment" "fragment does not exist" "$TMP/broken-fragment-link.md"

# The repository's current public entry points must pass the same check.
bash "$CHECK" >/dev/null

echo "Docs-safety lint self-test passed."
