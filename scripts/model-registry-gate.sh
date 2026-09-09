#!/usr/bin/env bash
# model-registry-gate.sh: section 11 of pre-release-check.sh, sourced, never executed.
#
# Two questions about model_registry.toml, answered by scripts/check_model_registry.py:
# did a maintainer decide the pins in the last 45 days, and are the pinned IDs still
# alive at their providers? The contract, the env vars and why the section never runs
# on feature PRs are in CLAUDE.md, Quality gates, "Model registry watch". The section
# lives in its own file so tests/workflows/test_model_registry_gate.sh can drive it
# without running the other ten sections of the release check each time.
#
# Callers provide ok(), fail() and waive(), pre-release-check.sh's reporters.
# shellcheck shell=bash

# Anything but auto|always is a hard error, never a silent skip: a gate that disables
# itself on MMR_MODEL_REGISTRY_GATE=true is worse than none. Returns 2 so the caller
# can stop before it prints ten sections.
model_registry_gate_validate() {
    case "${MMR_MODEL_REGISTRY_GATE:-auto}" in
        auto | always) return 0 ;;
        *)
            echo "MMR_MODEL_REGISTRY_GATE must be 'auto' or 'always', got '${MMR_MODEL_REGISTRY_GATE}'" >&2
            return 2
            ;;
    esac
}

# Does the section apply to this run? Locally always. In CI the release check also runs
# on non-cut PRs that touch pyproject.toml (quality.yml's version-sync step, Dependabot
# bumps included); only a cut changes a version line, so every other CI diff skips, and
# no PR goes red on day 46 or reaches the network. A failed diff falls through to
# running: the safe direction is "check".
model_registry_gate_applies() {
    [ "${MMR_MODEL_REGISTRY_GATE:-auto}" = "always" ] && return 0
    [ "${GITHUB_ACTIONS:-}" = "true" ] || return 0
    git rev-parse --verify --quiet origin/main >/dev/null 2>&1 || return 0
    local version_diff
    version_diff="$(git diff origin/main...HEAD -- pyproject.toml ha-addon/mammamiradio/config.yaml custom_components/mammamiradio/manifest.json 2>/dev/null)" || return 0
    grep -qE '^\+[[:space:]]*"?version"?[[:space:]]*[:=]' <<<"$version_diff"
}

# $1: python interpreter (3.11+), $2: path to check_model_registry.py.
# MMR_MODEL_REGISTRY / MMR_MODEL_REGISTRY_FIXTURES are test-only passthroughs.
model_registry_gate() {
    local python="$1" checker="$2"
    if ! model_registry_gate_applies; then
        echo "  [NOTE] not a release cut (no version line changes against origin/main); the review-age and liveness gates run on cut PRs and under make pre-release"
        return 0
    fi
    local registry_args=() fixture_args=()
    if [ -n "${MMR_MODEL_REGISTRY:-}" ]; then
        registry_args=(--registry "$MMR_MODEL_REGISTRY")
    fi
    if [ -n "${MMR_MODEL_REGISTRY_FIXTURES:-}" ]; then
        fixture_args=(--fixture-dir "$MMR_MODEL_REGISTRY_FIXTURES")
    fi
    # ${arr[@]+"${arr[@]}"} expands an empty array safely under set -u on bash 3.2.
    local age_rc=0
    "$python" "$checker" --age ${registry_args[@]+"${registry_args[@]}"} || age_rc=$?
    case "$age_rc" in
        0) ok "Model registry review age: a maintainer decided the pins within the last 45 days" ;;
        2) fail "Model registry review age: the checker/source failed, so last_reviewed could not be verified (see above)" ;;
        *) fail "Model registry review age: last_reviewed is missing, malformed, or older than 45 days (see above)" ;;
    esac
    local liveness_rc=0
    "$python" "$checker" --providers --gate liveness \
        ${registry_args[@]+"${registry_args[@]}"} ${fixture_args[@]+"${fixture_args[@]}"} || liveness_rc=$?
    case "$liveness_rc" in
        0) ok "Pinned models are alive at their providers" ;;
        2) waive "NOT CHECKED: the provider docs could not be read (or the checker failed), so pinned-model liveness is not verified. Run scripts/check_model_registry.py --providers before tagging." ;;
        *) fail "A pinned model is deprecated or retired at its provider (see the report above)" ;;
    esac
}
