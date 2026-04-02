#!/usr/bin/env bash
# Pre-commit hook: verify config.yaml and pyproject.toml versions match.
# Only runs when either file is staged.
set -euo pipefail

# Check if either version file is staged
STAGED=$(git diff --cached --name-only 2>/dev/null || true)
if ! echo "$STAGED" | grep -qE '(ha-addon/mammamiradio/config\.yaml|pyproject\.toml)'; then
    exit 0
fi

ADDON_VER=$(git show :ha-addon/mammamiradio/config.yaml | awk '/^version:/{print $2}' | tr -d '"' | head -1)
PYPROJECT_VER=$(git show :pyproject.toml | sed -n 's/^version *= *"\([^"]*\)".*/\1/p' | head -1)

if [ -z "$ADDON_VER" ] || [ -z "$PYPROJECT_VER" ]; then
    echo "ERROR: Could not parse version from staged files."
    echo "  ha-addon/mammamiradio/config.yaml: ${ADDON_VER:-<missing>}"
    echo "  pyproject.toml: ${PYPROJECT_VER:-<missing>}"
    exit 1
fi

if [ "$ADDON_VER" != "$PYPROJECT_VER" ]; then
    echo "ERROR: Version mismatch!"
    echo "  ha-addon/mammamiradio/config.yaml: $ADDON_VER"
    echo "  pyproject.toml: $PYPROJECT_VER"
    echo ""
    echo "  Both must match. Bump both in the same commit."
    exit 1
fi
