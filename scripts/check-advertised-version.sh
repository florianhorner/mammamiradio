#!/usr/bin/env bash
# Verify that the version main advertises to the HA Supervisor actually exists.
#
# The Supervisor clones this repo's default branch, reads
# ha-addon/mammamiradio/config.yaml's `version:` field, and pulls
# `{image}:{version}`. If that tag was never published, the add-on entry still
# LOOKS healthy in the store (the Supervisor never contacts the registry when
# reading the store) and fails only when someone clicks Install or Update:
# a fresh install fails and rolls back, an update fails to download.
#
# Between 2026-05-18 and 2026-08-02 this repo was in that state for 74 of 76
# days, because the release model parked the *next* unreleased number on main.
# See docs/release-process.md. This script is the guard for that invariant.
#
# Usage:
#   scripts/check-advertised-version.sh              # check config.yaml's version
#   scripts/check-advertised-version.sh --version X.Y.Z
#
# Exit codes:
#   0  PASS    - the advertised tag exists for every architecture
#   0  UNKNOWN - the registry could not be reached or answered ambiguously.
#                Deliberately NOT a failure: a DNS blip, an expired token, a
#                rate limit or a GHCR outage must never be reported as a broken
#                release. Only a definitive "this tag does not exist" fails.
#   1  FAIL    - the registry definitively does not have that tag
#   2  usage/parse error
#
# PASS and UNKNOWN share exit 0 on purpose, so a naive `if ! script` caller never
# reds a build over an unreachable registry. Callers that must tell them apart
# read the last line instead, which is always one of:
#   VERDICT: pass | VERDICT: unknown | VERDICT: fail
# advertised-version.yml uses that to decide whether to open, close, or leave the
# drift issue alone. Branching on the exit code alone would let an UNKNOWN close a
# live drift issue with a false all-clear.
#
# Do NOT wire this into scripts/pre-release-check.sh. That script runs from
# quality.yml on every PR that touches a version file, including the
# `chore(release): cut X.Y.Z` PR whose whole job is to name a version whose
# image does not exist yet. Adding it there deadlocks every release cut.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$REPO_ROOT/ha-addon/mammamiradio/config.yaml"
ARCHES=("amd64" "aarch64")
REGISTRY_HOST="${MAMMAMIRADIO_REGISTRY_HOST:-ghcr.io}"

VERSION=""
while [ $# -gt 0 ]; do
    case "$1" in
        --version)
            VERSION="${2:-}"
            if [ -z "$VERSION" ]; then
                echo "ERROR: --version needs a value" >&2
                exit 2
            fi
            shift 2
            ;;
        -h|--help)
            sed -n '2,30p' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

if [ -z "$VERSION" ]; then
    if [ ! -f "$CONFIG" ]; then
        echo "ERROR: cannot read $CONFIG" >&2
        exit 2
    fi
    VERSION="$(awk '/^version:/{print $2; exit}' "$CONFIG" | tr -d '"')"
fi

if [ -z "$VERSION" ]; then
    echo "ERROR: could not determine the advertised version" >&2
    exit 2
fi

# Derive owner/image from the config's own image: field so the two can't drift.
# e.g. ghcr.io/florianhorner/mammamiradio-addon-{arch}
IMAGE_TEMPLATE="$(awk '/^image:/{print $2; exit}' "$CONFIG" | tr -d '"')"
if [ -z "$IMAGE_TEMPLATE" ]; then
    echo "ERROR: no image: field in $CONFIG" >&2
    exit 2
fi
# Strip the registry host prefix -> florianhorner/mammamiradio-addon-{arch}
REPO_TEMPLATE="${IMAGE_TEMPLATE#*/}"

# Probe one tag. Echoes exactly one of: present | absent | unknown
# A test harness can replace this by pointing MAMMAMIRADIO_TAG_PROBE at an
# executable taking <repo> <tag>, which keeps the self-test hermetic (no network).
probe_tag() {
    local repo="$1" tag="$2"

    if [ -n "${MAMMAMIRADIO_TAG_PROBE:-}" ]; then
        "$MAMMAMIRADIO_TAG_PROBE" "$repo" "$tag" 2>/dev/null || echo "unknown"
        return 0
    fi

    local token
    token="$(
        curl --silent --show-error --max-time 15 \
            "https://${REGISTRY_HOST}/token?scope=repository:${repo}:pull&service=${REGISTRY_HOST}" 2>/dev/null |
            python3 -c 'import sys,json
try:
    # "or" rather than a .get default: an explicit JSON null returns None, and
    # print() turns that into the string "None". That is non-empty, so it sails
    # past the empty-token guard below and goes out as an Authorization header
    # reading "Bearer None". A registry that answers 404 rather than 401 to a bad
    # bearer would then look like a definitively missing tag, which is exactly the
    # false FAIL this script exists to avoid.
    print(json.load(sys.stdin).get("token") or "")
except Exception:
    print("")' 2>/dev/null || true
    )"

    # No token: anonymous auth is unavailable or the response was not JSON.
    # That is an environment problem, not a missing tag.
    if [ -z "$token" ]; then
        echo "unknown"
        return 0
    fi

    local code
    code="$(
        curl --silent --output /dev/null --write-out '%{http_code}' --max-time 15 \
            --request GET \
            --header "Authorization: Bearer ${token}" \
            --header 'Accept: application/vnd.oci.image.index.v1+json' \
            --header 'Accept: application/vnd.docker.distribution.manifest.list.v2+json' \
            --header 'Accept: application/vnd.oci.image.manifest.v1+json' \
            --header 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
            "https://${REGISTRY_HOST}/v2/${repo}/manifests/${tag}" 2>/dev/null || echo "000"
    )"

    case "$code" in
        200) echo "present" ;;
        # 404 is the registry answering the question. Everything else — 401, 403,
        # 429, 5xx, and curl's 000 for DNS/TLS/timeout — means we did not get an
        # answer, so we must not claim the tag is missing.
        404) echo "absent" ;;
        *)   echo "unknown" ;;
    esac
}

echo "Advertised add-on version: $VERSION"

missing=()
unknown=()
for arch in "${ARCHES[@]}"; do
    repo="${REPO_TEMPLATE//\{arch\}/$arch}"
    result="$(probe_tag "$repo" "$VERSION")"
    case "$result" in
        present) echo "  [PASS] ${repo}:${VERSION} exists" ;;
        absent)
            echo "  [FAIL] ${repo}:${VERSION} does not exist"
            missing+=("$repo")
            ;;
        *)
            echo "  [....] ${repo}:${VERSION} — registry did not answer"
            unknown+=("$repo")
            ;;
    esac
done

if [ ${#missing[@]} -gt 0 ]; then
    echo
    echo "FAIL: main advertises $VERSION, which is not published for: ${missing[*]}"
    echo
    echo "Home Assistant is offering an add-on version nobody can install."
    echo "A fresh install fails and rolls back; an update fails to download."
    echo
    echo "Fix it one of two ways:"
    echo "  1. If a release is mid-flight, wait for addon-release.yml to finish"
    echo "     promoting both architectures, then re-run this check."
    echo "  2. If the release failed or was abandoned, revert the whole cut commit"
    echo "     and land that immediately:"
    echo "       git revert <cut-sha>"
    echo "     Revert the commit, not just the version files: the cut also folded"
    echo "     both changelogs, and a version-only revert is refused by"
    echo "     check-changelog-sync.sh locally and by pre-release-check.sh in CI."
    echo "     Do not leave main advertising an unpublished version while debugging."
    echo "VERDICT: fail"
    exit 1
fi

if [ ${#unknown[@]} -gt 0 ]; then
    echo
    echo "UNKNOWN: could not reach the registry for: ${unknown[*]}"
    echo "Not treating this as a failure. An unreachable registry is not a broken release."
    echo "VERDICT: unknown"
    exit 0
fi

echo
echo "PASS: every architecture publishes $VERSION."
echo "VERDICT: pass"
exit 0
