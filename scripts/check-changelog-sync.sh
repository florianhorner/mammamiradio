#!/usr/bin/env bash
# Pre-commit hook: on version bumps, require both changelogs to be staged.
set -euo pipefail

STAGED=$(git diff --cached --name-only 2>/dev/null || true)

# Only run when a version source file changed in the index.
if ! echo "$STAGED" | grep -qE '^(pyproject\.toml|ha-addon/mammamiradio/config\.yaml)$'; then
    exit 0
fi

ADDON_VER=$(git show :ha-addon/mammamiradio/config.yaml 2>/dev/null | awk '/^version:/{print $2}' | tr -d '"' | head -1)
PYPROJECT_VER=$(git show :pyproject.toml 2>/dev/null | sed -n 's/^version *= *"\([^"]*\)".*/\1/p' | head -1)

if [ -z "$ADDON_VER" ] || [ -z "$PYPROJECT_VER" ]; then
    echo "ERROR: Could not parse staged versions for changelog sync check."
    exit 1
fi

# If versions disagree, let the dedicated version sync hook handle it.
if [ "$ADDON_VER" != "$PYPROJECT_VER" ]; then
    exit 0
fi

if ! echo "$STAGED" | grep -q '^CHANGELOG.md$'; then
    echo "ERROR: Version bump to $PYPROJECT_VER requires staged CHANGELOG.md update."
    exit 1
fi

if ! echo "$STAGED" | grep -q '^ha-addon/mammamiradio/CHANGELOG.md$'; then
    echo "ERROR: Version bump to $PYPROJECT_VER requires staged ha-addon/mammamiradio/CHANGELOG.md update."
    exit 1
fi

