#!/usr/bin/env bash
# Pre-commit hook: on version bumps, require both changelogs to be staged.
set -euo pipefail

STAGED=$(git diff --cached --name-only 2>/dev/null || true)

# Only inspect the index when a version source file was staged.
if ! echo "$STAGED" | grep -qE '^(pyproject\.toml|ha-addon/mammamiradio/config\.yaml)$'; then
    exit 0
fi

version_from_addon() {
    git show "$1" 2>/dev/null | awk '/^version:/{print $2}' | tr -d '"' | head -1
}

version_from_pyproject() {
    git show "$1" 2>/dev/null | sed -n 's/^version *= *"\([^"]*\)".*/\1/p' | head -1
}

ADDON_VER=$(version_from_addon :ha-addon/mammamiradio/config.yaml)
PYPROJECT_VER=$(version_from_pyproject :pyproject.toml)

if [ -z "$ADDON_VER" ] || [ -z "$PYPROJECT_VER" ]; then
    echo "ERROR: Could not parse staged versions for changelog sync check."
    exit 1
fi

# Dependency, coverage, and formatting edits to version-bearing files are not
# release cuts. Compare the staged values to HEAD rather than treating the file
# path itself as proof of a bump. Worktree-only edits deliberately do not count.
HEAD_ADDON_VER=$(version_from_addon HEAD:ha-addon/mammamiradio/config.yaml)
HEAD_PYPROJECT_VER=$(version_from_pyproject HEAD:pyproject.toml)
if [ "$ADDON_VER" = "$HEAD_ADDON_VER" ] && [ "$PYPROJECT_VER" = "$HEAD_PYPROJECT_VER" ]; then
    exit 0
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
