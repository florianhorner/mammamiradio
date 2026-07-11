#!/usr/bin/env bash
# Public documentation safety lint.
#
# Guards the current install/operator entry points against:
#   - live-surgery recovery instructions,
#   - retired Home Assistant install navigation, and
#   - stale Edge release promises, and
#   - relative Markdown links whose target or fragment does not exist.
#
# Run locally: bash scripts/check-docs-safety.sh
# Tests may pass explicit Markdown files as arguments.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck disable=SC1091  # resolved from SCRIPT_DIR at runtime
source "$SCRIPT_DIR/lint-patterns.sh"

DEFAULT_COPY_FILES=(
  README.md
  ha-addon/README.md
  ha-addon/mammamiradio/DOCS.md
  docs/troubleshooting.md
)

# Install wording has a wider documentation surface than live-recovery copy.
# Keep maintainer docs in this list without sending their legitimate repo-level
# release commands through the running-add-on mutation scanner.
DEFAULT_INSTALL_FILES=(
  README.md
  ha-addon/README.md
  ha-addon/mammamiradio/DOCS.md
  docs/troubleshooting.md
  docs/operations.md
  docs/runbooks/ha-addon.md
)

DEFAULT_LINK_FILES=(
  README.md
  ha-addon/README.md
  ha-addon/mammamiradio/DOCS.md
  CONTRIBUTING.md
  docs/troubleshooting.md
)

if [ "$#" -gt 0 ]; then
  COPY_FILES=("$@")
  INSTALL_FILES=("$@")
  LINK_FILES=("$@")
else
  COPY_FILES=("${DEFAULT_COPY_FILES[@]}")
  INSTALL_FILES=("${DEFAULT_INSTALL_FILES[@]}")
  LINK_FILES=("${DEFAULT_LINK_FILES[@]}")
fi

cd "$REPO_ROOT"

FAIL=0
HITS=0
EXISTING_COPY_FILES=()
EXISTING_LINK_FILES=()
MISSING_FILES=()

record_missing_file() {
  local file=$1
  local existing
  for existing in "${MISSING_FILES[@]}"; do
    if [ "$existing" = "$file" ]; then
      return
    fi
  done
  MISSING_FILES+=("$file")
  echo "FAIL: $file: documentation file is missing"
  FAIL=1
  HITS=$((HITS + 1))
}

for FILE in "${INSTALL_FILES[@]}"; do
  if [ ! -f "$FILE" ]; then
    record_missing_file "$FILE"
    continue
  fi

  for PATTERN in "${DOCS_RETIRED_INSTALL_PATTERNS[@]}"; do
    MATCHES=$(grep -niE "$PATTERN" "$FILE" 2>/dev/null || true)
    if [ -n "$MATCHES" ]; then
      while IFS= read -r LINE; do
        echo "FAIL: $FILE:$LINE  [retired Home Assistant install wording]"
        HITS=$((HITS + 1))
      done <<< "$MATCHES"
      FAIL=1
    fi
  done
done

for FILE in "${COPY_FILES[@]}"; do
  if [ ! -f "$FILE" ]; then
    record_missing_file "$FILE"
    continue
  fi
  EXISTING_COPY_FILES+=("$FILE")

  for PATTERN in "${DOCS_RELEASE_TRUTH_PATTERNS[@]}"; do
    MATCHES=$(grep -niE "$PATTERN" "$FILE" 2>/dev/null || true)
    if [ -n "$MATCHES" ]; then
      while IFS= read -r LINE; do
        echo "FAIL: $FILE:$LINE  [incorrect Edge release wording]"
        HITS=$((HITS + 1))
      done <<< "$MATCHES"
      FAIL=1
    fi
  done
done

for FILE in "${LINK_FILES[@]}"; do
  if [ ! -f "$FILE" ]; then
    record_missing_file "$FILE"
    continue
  fi
  EXISTING_LINK_FILES+=("$FILE")
done

STRUCTURAL_OUTPUT=""
if ! STRUCTURAL_OUTPUT=$(python3 "$SCRIPT_DIR/docs_safety.py" \
  --copy "${EXISTING_COPY_FILES[@]}" \
  --links "${EXISTING_LINK_FILES[@]}" 2>&1); then
  printf '%s\n' "$STRUCTURAL_OUTPUT"
  STRUCTURAL_HITS=$(printf '%s\n' "$STRUCTURAL_OUTPUT" | grep -c '^FAIL:' || true)
  if [ "$STRUCTURAL_HITS" -eq 0 ]; then
    echo "FAIL: structural documentation checker did not complete"
    STRUCTURAL_HITS=1
  fi
  HITS=$((HITS + STRUCTURAL_HITS))
  FAIL=1
fi

if [ "$FAIL" -ne 0 ]; then
  echo ""
  echo "Found $HITS documentation safety violation(s)."
  exit 1
fi

echo "Documentation safety lint clean."
