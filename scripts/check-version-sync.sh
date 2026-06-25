#!/usr/bin/env bash
# Pre-commit hook: verify config.yaml, pyproject.toml, and the HACS integration
# manifest.json all declare the same version. Only runs when one of them is staged.
set -euo pipefail

# Check if any version file is staged
STAGED=$(git diff --cached --name-only 2>/dev/null || true)
if ! echo "$STAGED" | grep -qE '(ha-addon/mammamiradio/config\.yaml|pyproject\.toml|custom_components/mammamiradio/manifest\.json)'; then
    exit 0
fi

ADDON_VER=$(git show :ha-addon/mammamiradio/config.yaml | awk '/^version:/{print $2}' | tr -d '"' | head -1)
PYPROJECT_VER=$(git show :pyproject.toml | sed -n 's/^version *= *"\([^"]*\)".*/\1/p' | head -1)
if ! MANIFEST_VER=$(git show :custom_components/mammamiradio/manifest.json 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('version',''))" 2>/dev/null); then
    MANIFEST_VER=""
fi

if [ -z "$ADDON_VER" ] || [ -z "$PYPROJECT_VER" ] || [ -z "$MANIFEST_VER" ]; then
    echo "ERROR: Could not parse version from staged files."
    echo "  ha-addon/mammamiradio/config.yaml: ${ADDON_VER:-<missing>}"
    echo "  pyproject.toml: ${PYPROJECT_VER:-<missing>}"
    echo "  custom_components/mammamiradio/manifest.json: ${MANIFEST_VER:-<missing>}"
    exit 1
fi

MISMATCH=0
if [ "$ADDON_VER" != "$PYPROJECT_VER" ]; then
    MISMATCH=1
fi
if [ "$MANIFEST_VER" != "$PYPROJECT_VER" ]; then
    MISMATCH=1
fi

if [ "$MISMATCH" -ne 0 ]; then
    echo "ERROR: Version mismatch!"
    echo "  ha-addon/mammamiradio/config.yaml: $ADDON_VER"
    echo "  pyproject.toml: $PYPROJECT_VER"
    echo "  custom_components/mammamiradio/manifest.json: $MANIFEST_VER"
    echo ""
    echo "  All must match. Bump them in the same commit."
    exit 1
fi
