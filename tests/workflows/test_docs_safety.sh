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

expect_unsafe_at_line() {
  local label=$1
  local file=$2
  local expected_line=$3
  local output

  if output=$(bash "$CHECK" "$file" 2>&1); then
    echo "FAIL: $label unexpectedly passed"
    exit 1
  fi
  if ! grep -Fq "$file:$expected_line  [unsafe recovery instruction]" <<< "$output"; then
    echo "FAIL: $label reported the wrong source line"
    echo "$output"
    exit 1
  fi
}

expect_durability_at_line() {
  local label=$1
  local file=$2
  local expected_line=$3
  local output

  if output=$(bash "$CHECK" "$file" 2>&1); then
    echo "FAIL: $label unexpectedly passed"
    exit 1
  fi
  if ! grep -Fq "$file:$expected_line  [direct options.json durability claim]" <<< "$output"; then
    echo "FAIL: $label reported the wrong source line"
    echo "$output"
    exit 1
  fi
}

expect_default_install_guard() {
  local label=$1
  local guarded_file=$2
  local fixture_root="$TMP/default-$label"
  local output

  mkdir -p \
    "$fixture_root/scripts" \
    "$fixture_root/ha-addon/mammamiradio" \
    "$fixture_root/docs/runbooks"
  cp "$CHECK" "$fixture_root/scripts/check-docs-safety.sh"
  cp "$ROOT/scripts/lint-patterns.sh" "$fixture_root/scripts/lint-patterns.sh"
  cp "$ROOT/scripts/docs_safety.py" "$fixture_root/scripts/docs_safety.py"

  for file in \
    CLAUDE.md \
    README.md \
    CONTRIBUTING.md \
    ha-addon/README.md \
    ha-addon/mammamiradio/DOCS.md \
    docs/architecture.md \
    docs/festival-mode.md \
    docs/troubleshooting.md \
    docs/operations.md \
    docs/runbooks/ha-addon.md; do
    printf '# Safe\n' > "$fixture_root/$file"
  done

  # A maintainer runbook may edit repository files. The default check should
  # catch its retired Home Assistant navigation without treating that repo-level
  # command as live surgery against a running add-on.
  # shellcheck disable=SC2016  # literal Markdown code span in the fixture
  printf '# Canonical guide\n\n`sed -i old/new repository-file`\n\nSettings > Add-ons > Add-on Store.\n' \
    > "$fixture_root/$guarded_file"

  if output=$(bash "$fixture_root/scripts/check-docs-safety.sh" 2>&1); then
    echo "FAIL: default install scope omitted $guarded_file"
    exit 1
  fi
  if ! grep -Fq "$guarded_file" <<< "$output" || ! grep -Fq "retired Home Assistant install wording" <<< "$output"; then
    echo "FAIL: default install scope returned the wrong failure for $guarded_file"
    echo "$output"
    exit 1
  fi
  if grep -Fq "unsafe recovery instruction" <<< "$output"; then
    echo "FAIL: default install scope sent maintainer commands through the live-surgery scanner"
    echo "$output"
    exit 1
  fi
}

expect_default_persistence_guard() {
  local label=$1
  local guarded_file=$2
  local fixture_root="$TMP/default-persistence-$label"
  local output

  mkdir -p \
    "$fixture_root/scripts" \
    "$fixture_root/ha-addon/mammamiradio" \
    "$fixture_root/docs/runbooks"
  cp "$CHECK" "$fixture_root/scripts/check-docs-safety.sh"
  cp "$ROOT/scripts/lint-patterns.sh" "$fixture_root/scripts/lint-patterns.sh"
  cp "$ROOT/scripts/docs_safety.py" "$fixture_root/scripts/docs_safety.py"

  for file in \
    CLAUDE.md \
    README.md \
    CONTRIBUTING.md \
    ha-addon/README.md \
    ha-addon/mammamiradio/DOCS.md \
    docs/architecture.md \
    docs/festival-mode.md \
    docs/troubleshooting.md \
    docs/operations.md \
    docs/runbooks/ha-addon.md; do
    printf '# Safe\n' > "$fixture_root/$file"
  done

  # shellcheck disable=SC2016  # literal Markdown code span in the fixture
  printf '# Unsafe durability\n\nAdmin controls persist to `/data/options.json` across restarts.\n' \
    > "$fixture_root/$guarded_file"

  if output=$(bash "$fixture_root/scripts/check-docs-safety.sh" 2>&1); then
    echo "FAIL: default persistence scope omitted $guarded_file"
    exit 1
  fi
  if ! grep -Fq "$guarded_file" <<< "$output" || ! grep -Fq "[direct options.json durability claim]" <<< "$output"; then
    echo "FAIL: default persistence scope returned the wrong failure for $guarded_file"
    echo "$output"
    exit 1
  fi
}

printf '# Guide\n' > "$TMP/guide.md"
printf '# Safe\n\nPlease do not SSH in to edit container or runtime files, delete live cache, or restart as an experiment.\n\n[Guide](guide.md)\n' > "$TMP/safe.md"
expect_success "directly negated warning" "$TMP/safe.md"

printf '# Safe\n\nPlease do not SSH in to edit container or runtime files,\ndelete live cache, or restart as an experiment.\n' > "$TMP/safe-wrapped.md"
expect_success "wrapped directly negated warning" "$TMP/safe-wrapped.md"

printf '# Safe\n\nDo not edit /config/x or rm /data/y.\n' > "$TMP/safe-negated-pair.md"
expect_success "directly negated paired mutations" "$TMP/safe-negated-pair.md"

printf '# Safe\n\nDo not SSH in and restart the add-on.\n' > "$TMP/safe-negated-ssh-restart.md"
expect_success "directly negated SSH restart" "$TMP/safe-negated-ssh-restart.md"

printf '# Safe\n\nRestart the add-on from the Home Assistant UI.\n' > "$TMP/safe-ha-ui-restart.md"
expect_success "Home Assistant UI restart without SSH" "$TMP/safe-ha-ui-restart.md"

# shellcheck disable=SC2016  # literal Markdown code span in the fixture
printf '# Safe diagnosis\n\nFor diagnosis, `docker exec addon cat /data/options.json` is read-only.\n' > "$TMP/safe-docker-exec.md"
expect_success "read-only docker exec" "$TMP/safe-docker-exec.md"

# shellcheck disable=SC2016  # literal Markdown code spans in the fixture
printf '# Safe authority\n\nSupervisor stores the durable app options. `/data/options.json` is a Supervisor-generated, read-only startup projection.\n' > "$TMP/safe-options-projection.md"
expect_success "generated options projection" "$TMP/safe-options-projection.md"

# shellcheck disable=SC2016  # literal Markdown code spans in the fixture
printf '# Safe startup\n\nAt startup, `run.sh` reads `/data/options.json` and exports the selected values. Runtime code never writes that file directly.\n' > "$TMP/safe-options-startup-read.md"
expect_success "options startup read" "$TMP/safe-options-startup-read.md"

# shellcheck disable=SC2016  # literal Markdown code span in the fixture
printf '# Safe warning\n\nNever persist admin controls to `/data/options.json`; save them through Supervisor.\n' > "$TMP/safe-options-never-write.md"
expect_success "never persist options directly" "$TMP/safe-options-never-write.md"

# shellcheck disable=SC2016  # literal Markdown code span in the fixture
printf '# Safe table\n\n| Deployment | Startup input |\n|---|---|\n| Add-on | Supervisor-generated, read-only `/data/options.json` projection |\n' > "$TMP/safe-options-table.md"
expect_success "generated options table cell" "$TMP/safe-options-table.md"

# shellcheck disable=SC2016  # literal Markdown code fence in the fixture
printf '# Historical example\n\n```\nAdmin controls persisted to /data/options.json.\n```\n' > "$TMP/safe-options-code-fence.md"
expect_success "options durability in code fence" "$TMP/safe-options-code-fence.md"

# shellcheck disable=SC2016  # literal Markdown code span in the fixture
printf '# Unsafe durability\n\nAdmin controls persist to `/data/options.json` across restarts.\n' > "$TMP/unsafe-options-persist.md"
expect_durability_at_line "direct options persistence" "$TMP/unsafe-options-persist.md" 3

# shellcheck disable=SC2016  # literal Markdown code span in the fixture
printf '# Unsafe authority\n\n`/data/options.json` is the durable source of truth for add-on settings.\n' > "$TMP/unsafe-options-authority.md"
expect_durability_at_line "direct options authority" "$TMP/unsafe-options-authority.md" 3

# shellcheck disable=SC2016  # literal Markdown code span in the fixture
printf '# Unsafe table\n\n| Deployment | Storage |\n|---|---|\n| Add-on | `/data/options.json` stores settings across restarts |\n' > "$TMP/unsafe-options-table.md"
expect_durability_at_line "table-cell options durability" "$TMP/unsafe-options-table.md" 5

# shellcheck disable=SC2016  # literal Markdown code span in the fixture
printf '# Unsafe split table\n\n| File | Purpose |\n|---|---|\n| `/data/options.json` | Durable settings that survive restarts |\n' > "$TMP/unsafe-options-split-table.md"
expect_durability_at_line "cross-cell options durability" "$TMP/unsafe-options-split-table.md" 5

# shellcheck disable=SC2016  # literal Markdown code span in the fixture
printf '# Historical release notes\n\nThe old release persisted controls to `/data/options.json`.\n' > "$TMP/CHANGELOG.md"
expect_success "historical changelog exclusion" "$TMP/CHANGELOG.md"

printf '# Retired\n\nGo to Settings > Add-ons > Add-on Store.\n' > "$TMP/retired.md"
expect_failure "retired install path" "retired Home Assistant install wording" "$TMP/retired.md"

printf '# Unsafe\n\n**Fix**: SSH to the host, edit /addon_configs/mammamiradio/run.sh, then restart.\n' > "$TMP/unsafe.md"
expect_unsafe "live surgery" "$TMP/unsafe.md"

printf '# Unsafe\n\n**Fix**:\n1. SSH to the Home Assistant host.\n2. Edit runtime files in the running app.\n3. Restart the app.\n' > "$TMP/unsafe-multiline.md"
expect_unsafe "multiline live surgery" "$TMP/unsafe-multiline.md"

printf '# Unsafe\n\n**Fix**: Do not wait; SSH to the host, edit /data/options.json, then restart the app.\n' > "$TMP/unsafe-unrelated-negation.md"
expect_unsafe "unrelated negation does not suppress danger" "$TMP/unsafe-unrelated-negation.md"

printf '# Unsafe\n\nDo not edit /config/x, edit /data/y instead.\n' > "$TMP/unsafe-negation-comma-splice.md"
expect_unsafe "negation does not cover a comma-splice imperative" "$TMP/unsafe-negation-comma-splice.md"

printf '# Unsafe\n\nSSH into the Pi and restart the add-on.\n' > "$TMP/unsafe-ssh-restart.md"
expect_unsafe "SSH restart without another mutation" "$TMP/unsafe-ssh-restart.md"

printf '# Unsafe\n\nDo not edit /config/x,\nedit /data/y instead.\n' > "$TMP/unsafe-negation-line-attribution.md"
expect_unsafe_at_line "comma-splice points to the offending source line" "$TMP/unsafe-negation-line-attribution.md" 4

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

expect_default_install_guard "operations" "docs/operations.md"
expect_default_install_guard "addon-runbook" "docs/runbooks/ha-addon.md"

expect_default_persistence_guard "claude" "CLAUDE.md"
expect_default_persistence_guard "addon-readme" "ha-addon/README.md"
expect_default_persistence_guard "addon-docs" "ha-addon/mammamiradio/DOCS.md"
expect_default_persistence_guard "architecture" "docs/architecture.md"
expect_default_persistence_guard "festival-mode" "docs/festival-mode.md"
expect_default_persistence_guard "operations" "docs/operations.md"
expect_default_persistence_guard "addon-runbook" "docs/runbooks/ha-addon.md"

expect_failure "missing documentation file" "documentation file is missing" "$TMP/missing.md"

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
