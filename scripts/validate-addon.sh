#!/usr/bin/env bash
# Local smoke test for HA addon — catches 80% of CI failures before push.
# Mirrors the validation steps in .github/workflows/addon-build.yml plus
# additional checks derived from real production failures.
#
# Usage: ./scripts/validate-addon.sh [--build]
#   --build    Also build the Docker image locally (slow, requires Docker)
set -euo pipefail

case "${1:-}" in
  -h|--help)
    cat <<'EOF'
Usage: scripts/validate-addon.sh [--build]

Local pre-flight for the HA add-on. Mirrors the validation steps in
.github/workflows/addon-build.yml plus checks derived from real production
failures (version sync, image path, options contract, critical files).

Options:
  -h, --help   Show this help and exit
  --build      Also build the Docker image locally (slow, requires Docker)

Runs in pre-commit and pre-push hooks for files that can break the add-on.
EOF
    exit 0
    ;;
esac

RED='\033[0;31m'
BLUE='\033[0;34m'
AMBER='\033[0;33m'
NC='\033[0m'
PASS="${BLUE}PASS${NC}"
FAIL="${RED}FAIL${NC}"
WARN="${AMBER}WARN${NC}"

errors=0
warnings=0

pass() { echo -e "  ${PASS}  $1"; }
fail() { echo -e "  ${FAIL}  $1"; errors=$((errors + 1)); }
warn() { echo -e "  ${WARN}  $1"; warnings=$((warnings + 1)); }
extract_yaml_block() {
    local section="$1"
    local file="$2"
    awk -v section="$section" '
        $0 == section ":" { in_section = 1; next }
        in_section && /^[^[:space:]]/ { exit }
        in_section { print }
    ' "$file"
}

ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$ROOT"

# Prefer a Python that can actually parse TOML for this repo.
PY=""
for candidate in .venv/bin/python3 python3.11 python3; do
    if "$candidate" -c "import tomllib" >/dev/null 2>&1 || \
       "$candidate" -c "import tomli" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    PY=python3
fi

echo "=== HA Addon Pre-Flight Validation ==="
echo ""

# ---- 1. Version consistency ----
echo "1. Version sync"
ADDON_VER=$(grep '^version:' ha-addon/mammamiradio/config.yaml | awk '{print $2}' | tr -d '"')
PYPROJECT_VER=$(grep '^version' pyproject.toml | head -1 | sed 's/.*= *"//;s/".*//')
if [ "$ADDON_VER" = "$PYPROJECT_VER" ]; then
    pass "config.yaml ($ADDON_VER) == pyproject.toml ($PYPROJECT_VER)"
else
    fail "Version mismatch: config.yaml=$ADDON_VER pyproject.toml=$PYPROJECT_VER"
fi

# ---- 2. Image path format ----
echo "2. Image path"
IMAGE=$(grep '^image:' ha-addon/mammamiradio/config.yaml | awk '{print $2}')
# Owner detection (primary: git remote → repository.yaml manifest → gh → unknown).
# Each `$(...)` ends in `|| true` so a failing stage (git with no origin, grep
# with no match) can't trip `set -euo pipefail`: under pipefail the pipeline
# inherits git's non-zero exit, which `set -e` would otherwise treat as fatal.
OWNER=$(git remote get-url origin 2>/dev/null | sed 's|.*github.com[:/]||;s|/.*||' || true)
# No remote (fresh/no-remote worktree): the repo manifest is the canonical owner.
# Prefer it over `gh api user`, which returns whoever is logged in — possibly a
# different account than the repo owner, which would make EXPECTED wrong and fail
# a correct config.yaml (the bogus mismatch this fallback exists to prevent).
if [ -z "$OWNER" ]; then
    OWNER=$(grep '^url:' repository.yaml 2>/dev/null | sed 's|.*github.com[:/]||;s|/.*||' || true)
fi
# Last resort if the manifest is missing/malformed: the logged-in gh account.
if [ -z "$OWNER" ]; then
    OWNER=$(gh api user -q .login 2>/dev/null || true)
fi
[ -z "$OWNER" ] && OWNER="unknown"
EXPECTED="ghcr.io/${OWNER}/mammamiradio-addon-{arch}"
if [ "$IMAGE" = "$EXPECTED" ]; then
    pass "Image path: $IMAGE"
else
    fail "Image path mismatch: got '$IMAGE', expected '$EXPECTED'"
fi

# ---- 3. Options contract ----
echo "3. Options contract"
OPTIONS_KEYS=$(sed -n '/^options:/,/^[^ ]/p' ha-addon/mammamiradio/config.yaml | grep -E '^[[:space:]]+[[:alnum:]_]+:' | awk -F: '{print $1}' | tr -d ' ')
SCHEMA_KEYS=$(sed -n '/^schema:/,/^[^ ]/p' ha-addon/mammamiradio/config.yaml | grep -E '^[[:space:]]+[[:alnum:]_]+:' | awk -F: '{print $1}' | tr -d ' ')
if [ "$OPTIONS_KEYS" = "$SCHEMA_KEYS" ]; then
    pass "options and schema key order match"
else
    fail "options and schema key order differ"
    echo "    options: $(echo "$OPTIONS_KEYS" | tr '\n' ' ')"
    echo "    schema:  $(echo "$SCHEMA_KEYS" | tr '\n' ' ')"
fi

MISSING=""
for key in $SCHEMA_KEYS; do
    if ! grep -q "$key" ha-addon/mammamiradio/rootfs/run.sh; then
        MISSING="$MISSING $key"
    fi
done
if [ -z "$MISSING" ]; then
    pass "All schema keys mapped in run.sh"
else
    fail "Keys missing from run.sh:$MISSING"
fi

# ---- 4. Critical files exist ----
echo "4. Critical files"
for f in mammamiradio/__init__.py radio.toml ha-addon/mammamiradio/Dockerfile \
         ha-addon/mammamiradio/rootfs/run.sh ha-addon/mammamiradio/config.yaml \
         ha-addon/mammamiradio/build.yaml ha-addon/mammamiradio/apparmor.txt \
         ha-addon/mammamiradio/translations/en.yaml; do
    if [ -f "$f" ]; then
        pass "$f"
    else
        fail "Missing: $f"
    fi
done

# ---- 5. Python import works ----
echo "5. Python import"
if $PY -c "import mammamiradio" 2>/dev/null; then
    pass "import mammamiradio"
else
    warn "import mammamiradio failed (is the venv active?)"
fi

# ---- 6. radio.toml parses ----
echo "6. Config parse"
if $PY -c "
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('radio.toml','rb') as f:
    tomllib.load(f)
" 2>/dev/null; then
    pass "radio.toml is valid TOML"
else
    fail "radio.toml parse error"
fi

echo "6b. radio.toml sync"
if cmp -s radio.toml ha-addon/mammamiradio/radio.toml; then
    pass "ha-addon/mammamiradio/radio.toml matches root radio.toml"
else
    fail "ha-addon/mammamiradio/radio.toml drifted from root radio.toml"
fi

# ---- 7. run.sh syntax check ----
echo "7. Shell syntax"
if bash -n ha-addon/mammamiradio/rootfs/run.sh 2>/dev/null; then
    pass "run.sh bash syntax OK"
else
    fail "run.sh has bash syntax errors"
fi

if command -v ash >/dev/null 2>&1; then
    if ash -n ha-addon/mammamiradio/rootfs/run.sh 2>/dev/null; then
        pass "run.sh ash syntax OK"
    else
        fail "run.sh has ash syntax errors"
    fi
elif sh -n ha-addon/mammamiradio/rootfs/run.sh 2>/dev/null; then
    pass "run.sh POSIX shell syntax OK"
else
    fail "run.sh has POSIX shell syntax errors"
fi

# ---- 8. Hardcoded value sync ----
echo "8. Hardcoded value sync"

STABLE_STAGE=$(grep '^stage:' ha-addon/mammamiradio/config.yaml | awk '{print $2}' | tr -d '"')
if [ "$STABLE_STAGE" = "stable" ]; then
    pass "stable add-on stage: stable"
else
    fail "stable add-on stage must be stable, got: ${STABLE_STAGE:-missing}"
fi

# Port 8000 must appear in config.yaml, run.sh
PORT_CONFIG=$(grep 'ingress_port:' ha-addon/mammamiradio/config.yaml | awk '{print $2}')
PORT_RUNSH=$(grep 'MAMMAMIRADIO_PORT=' ha-addon/mammamiradio/rootfs/run.sh | head -1 | sed 's/.*="//' | tr -d '"')
if [ "$PORT_CONFIG" = "8000" ] && [ "$PORT_RUNSH" = "8000" ]; then
    pass "Port 8000 consistent (config.yaml, run.sh)"
else
    fail "Port mismatch: config.yaml=$PORT_CONFIG run.sh=$PORT_RUNSH"
fi

# host_network required for stream access
if grep -q 'host_network: true' ha-addon/mammamiradio/config.yaml; then
    pass "host_network: true (required for local network stream access)"
else
    fail "host_network must be true for local network stream access"
fi

# timeout >= 120 (addon needs time to install Python deps)
TIMEOUT=$(grep '^timeout:' ha-addon/mammamiradio/config.yaml | awk '{print $2}')
if [ "${TIMEOUT:-0}" -ge 120 ] 2>/dev/null; then
    pass "timeout: $TIMEOUT (>= 120)"
else
    fail "timeout should be >= 120, got: ${TIMEOUT:-missing}"
fi

# ---- 9. Translations cover all options ----
echo "9. Translations"
trans_errors=0
for key in $SCHEMA_KEYS; do
    if ! grep -q "$key" ha-addon/mammamiradio/translations/en.yaml 2>/dev/null; then
        fail "Translation missing for option: $key"
        trans_errors=$((trans_errors + 1))
    fi
done
if [ $trans_errors -eq 0 ]; then
    pass "All options have translations"
fi

# ---- 10. No JS string rewriting in ingress prefix injection ----
# Discover _inject_ingress_prefix anywhere under mammamiradio/web/*.py rather
# than hardcoding a single source file (issue #467 — the helper has moved
# modules twice). The Python scan reports one of three sentinel tokens so the
# three outcomes stay an explicit, testable contract instead of overloaded
# exit codes:
#   found + all safe   -> stdout "safe"          -> pass
#   found + any unsafe -> stdout "unsafe:<path>" -> fail (same message as before)
#   not found anywhere -> stdout "none"          -> warn (ingress may not work)
# All matching definitions are validated (not just the first), so a stale
# unsafe copy left behind by a refactor cannot slip through. Each file is
# parsed in isolation: a syntax error in an unrelated web module is skipped
# (ruff/mypy/pytest surface that loudly elsewhere) instead of masquerading as
# an ingress-safety failure.
echo "10. Ingress safety"
if [ ! -d mammamiradio/web ]; then
    fail "Missing mammamiradio/web (cannot run ingress safety check)"
elif CHECK_OUTPUT=$($PY -c "
import ast
from pathlib import Path

targets = []
for path in sorted(Path('mammamiradio/web').glob('*.py')):
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        continue
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == '_inject_ingress_prefix':
            targets.append(node)

if not targets:
    print('none')
    raise SystemExit(0)

for target in targets:
    for node in ast.walk(target):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != 'replace':
            continue
        if not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        pattern = first.value
        if pattern.startswith(\"'/\") and pattern != \"'/sw.js'\":
            print(f'unsafe:{pattern}')
            raise SystemExit(0)

print('safe')
" 2>&1); then
    case "$CHECK_OUTPUT" in
        safe)
            pass "Ingress prefix injection only rewrites safe patterns" ;;
        none)
            warn "No _inject_ingress_prefix found (ingress may not work)" ;;
        unsafe:*)
            fail "Ingress safety check failed: rewrites single-quoted JS path: ${CHECK_OUTPUT#unsafe:}" ;;
        *)
            fail "Ingress safety check failed: $CHECK_OUTPUT" ;;
    esac
else
    fail "Ingress safety check failed: $CHECK_OUTPUT"
fi

# ---- 11. Dockerfile doesn't COPY to /data/ ----
echo "11. Dockerfile safety"
if grep -qE '^COPY.*\s/data/' ha-addon/mammamiradio/Dockerfile; then
    fail "Dockerfile COPYs to /data/ — this overwrites persistent volumes on update"
else
    pass "No COPY to /data/ (persistent volume safe)"
fi

for label in 'io.hass.version="${BUILD_VERSION}"' 'io.hass.type="app"' 'io.hass.arch="${BUILD_ARCH}"'; do
    if grep -Fq "$label" ha-addon/mammamiradio/Dockerfile; then
        pass "Dockerfile label: $label"
    else
        fail "Dockerfile missing required Home Assistant image label: $label"
    fi
done

# No bare eval 2>&1 in run.sh (subshell captures like SYNC_MSG="$(...2>&1)" are safe)
# Collapse continuation lines, then reject 2>&1 that is NOT inside a $() capture.
UNSAFE_2_1=$(awk '/\\$/{buf=buf $0; next} {if(buf){print buf $0; buf=""} else print}' \
    ha-addon/mammamiradio/rootfs/run.sh | grep '2>&1' | grep -v '"\$(' | grep -v "'\$(" || true)
if [ -n "$UNSAFE_2_1" ]; then
    fail "run.sh uses bare 2>&1 outside subshell capture — stderr injection risk"
else
    pass "No unsafe 2>&1 in eval context"
fi

# ---- 12. repository.yaml on main ----
echo "12. Repository discovery"
if [ -f repository.yaml ]; then
    pass "repository.yaml exists"
else
    fail "repository.yaml missing (HA can't discover the addon repo)"
fi

# ---- 13. Edge add-on folder ----
# The edge add-on runs the SAME image as stable, so its config must stay
# schema-locked to stable. Its version is a manual edge release (`make edge-release`).
echo "13. Edge add-on"
EDGE_CONFIG="ha-addon/mammamiradio-edge/config.yaml"
STABLE_CONFIG="ha-addon/mammamiradio/config.yaml"
STABLE_TRANS="ha-addon/mammamiradio/translations/en.yaml"
EDGE_TRANS="ha-addon/mammamiradio-edge/translations/en.yaml"
if [ ! -f "$EDGE_CONFIG" ]; then
    echo "  (no edge add-on — skipping)"
else
    # YAML validity (best-effort — pyyaml may not be installed)
    if $PY -c "import yaml" 2>/dev/null; then
        if $PY -c "import yaml,sys; yaml.safe_load(open('$EDGE_CONFIG'))" 2>/dev/null; then
            pass "edge config.yaml is valid YAML"
        else
            fail "edge config.yaml YAML parse error"
        fi
    else
        warn "pyyaml not available — skipped edge YAML parse check"
    fi

    # slug
    EDGE_SLUG=$(grep '^slug:' "$EDGE_CONFIG" | awk '{print $2}' | tr -d '"')
    if [ "$EDGE_SLUG" = "mammamiradio-edge" ]; then
        pass "edge slug: $EDGE_SLUG"
    else
        fail "edge slug must be 'mammamiradio-edge', got '$EDGE_SLUG'"
    fi

    # image path — must match stable's expected image (shared repo)
    EDGE_IMAGE=$(grep '^image:' "$EDGE_CONFIG" | awk '{print $2}')
    if [ "$EDGE_IMAGE" = "$EXPECTED" ]; then
        pass "edge image: $EDGE_IMAGE"
    else
        fail "edge image mismatch: got '$EDGE_IMAGE', expected '$EXPECTED'"
    fi

    # version — a manual edge release sets this to the main short SHA (7-char hex,
    # see `make edge-release`). The dotted-numeric form is still accepted so the
    # pre-migration calver value validates until the first SHA release is cut.
    EDGE_VER=$(grep '^version:' "$EDGE_CONFIG" | awk '{print $2}' | tr -d '"')
    if echo "$EDGE_VER" | grep -qE '^[0-9a-f]{7}$|^[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?$'; then
        pass "edge version format: $EDGE_VER"
    else
        fail "edge version must be a 7-char short SHA (make edge-release), got '$EDGE_VER'"
    fi

    EDGE_STAGE=$(grep '^stage:' "$EDGE_CONFIG" | awk '{print $2}' | tr -d '"')
    if [ "$EDGE_STAGE" = "experimental" ]; then
        pass "edge stage: experimental"
    else
        fail "edge stage must stay experimental, got: ${EDGE_STAGE:-missing}"
    fi

    # options + schema parity with stable (edge runs the same image/run.sh).
    # Block equality is byte-exact, so it also guarantees key parity and
    # (via the translations block check below) translation coverage.
    STABLE_OPT_BLOCK=$(extract_yaml_block options "$STABLE_CONFIG")
    EDGE_OPT_BLOCK=$(extract_yaml_block options "$EDGE_CONFIG")
    if [ "$STABLE_OPT_BLOCK" = "$EDGE_OPT_BLOCK" ]; then
        pass "edge options block matches stable"
    else
        fail "edge options block drifted from stable"
    fi

    STABLE_SCHEMA_BLOCK=$(extract_yaml_block schema "$STABLE_CONFIG")
    EDGE_SCHEMA_BLOCK=$(extract_yaml_block schema "$EDGE_CONFIG")
    if [ "$STABLE_SCHEMA_BLOCK" = "$EDGE_SCHEMA_BLOCK" ]; then
        pass "edge schema block matches stable"
    else
        fail "edge schema block drifted from stable"
    fi

    # Translations must be byte-identical to stable. stable's own translation
    # coverage is enforced by check 9, so equality here transitively guarantees
    # the edge translations cover every edge schema key.
    STABLE_TRANSLATIONS=$(extract_yaml_block configuration "$STABLE_TRANS")
    EDGE_TRANSLATIONS=$(extract_yaml_block configuration "$EDGE_TRANS")
    if [ "$STABLE_TRANSLATIONS" = "$EDGE_TRANSLATIONS" ]; then
        pass "edge translations block matches stable"
    else
        fail "edge translations block drifted from stable"
    fi

    # required files
    for f in ha-addon/mammamiradio-edge/icon.png ha-addon/mammamiradio-edge/logo.png \
             ha-addon/mammamiradio-edge/apparmor.txt "$EDGE_TRANS"; do
        if [ -f "$f" ]; then
            pass "$f"
        else
            fail "Missing: $f"
        fi
    done

    if cmp -s ha-addon/mammamiradio/apparmor.txt ha-addon/mammamiradio-edge/apparmor.txt; then
        pass "edge AppArmor profile matches stable"
    else
        fail "edge AppArmor profile drifted from stable"
    fi

    # ingress / network consistency with stable
    if grep -q 'host_network: true' "$EDGE_CONFIG"; then
        pass "edge host_network: true"
    else
        fail "edge host_network must be true"
    fi
    EDGE_PORT=$(grep 'ingress_port:' "$EDGE_CONFIG" | awk '{print $2}')
    if [ "$EDGE_PORT" = "8000" ]; then
        pass "edge ingress_port: 8000"
    else
        fail "edge ingress_port must be 8000, got: ${EDGE_PORT:-missing}"
    fi
    EDGE_TIMEOUT=$(grep '^timeout:' "$EDGE_CONFIG" | awk '{print $2}')
    if [ "${EDGE_TIMEOUT:-0}" -ge 120 ] 2>/dev/null; then
        pass "edge timeout: $EDGE_TIMEOUT (>= 120)"
    else
        fail "edge timeout should be >= 120, got: ${EDGE_TIMEOUT:-missing}"
    fi
fi

# ---- Optional: Docker build ----
if [ "${1:-}" = "--build" ]; then
    echo ""
    echo "=== Docker Build Test ==="

    # Simulate CI: copy source into build context
    TMPCTX=$(mktemp -d)
    trap 'rm -rf "$TMPCTX"' EXIT
    cp -r ha-addon/mammamiradio/* "$TMPCTX/"
    cp -r mammamiradio/ "$TMPCTX/mammamiradio/"
    cp pyproject.toml "$TMPCTX/"
    cp radio.toml "$TMPCTX/"

    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64)  BUILD_ARCH="amd64"; BASE="ghcr.io/home-assistant/amd64-base:3.20" ;;
        arm64|aarch64) BUILD_ARCH="aarch64"; BASE="ghcr.io/home-assistant/aarch64-base:3.20" ;;
        *) fail "Unknown arch: $ARCH"; exit 1 ;;
    esac

    echo "  Building for $BUILD_ARCH..."
    if docker build "$TMPCTX" \
        --build-arg BUILD_FROM="$BASE" \
        --build-arg BUILD_VERSION="$ADDON_VER" \
        --build-arg BUILD_ARCH="$BUILD_ARCH" \
        -t mammamiradio-addon-test:local 2>&1 | tail -5; then
        pass "Docker build succeeded"

        echo "  Testing container startup..."
        # Create minimal options.json
        echo '{"anthropic_api_key":"","openai_api_key":""}' > "$TMPCTX/options.json"

        CID=$(docker run -d --name mmr-test \
            -v "$TMPCTX/options.json:/data/options.json:ro" \
            -e SUPERVISOR_TOKEN=fake \
            mammamiradio-addon-test:local 2>&1)

        sleep 3
        if docker ps --filter "id=$CID" --filter "status=running" -q | grep -q .; then
            pass "Container running after 3s"
            # Check if uvicorn started
            if docker logs "$CID" 2>&1 | grep -q "Starting uvicorn"; then
                pass "Uvicorn started"
            else
                warn "Uvicorn not yet started (may still be initializing)"
            fi
        else
            fail "Container exited"
            docker logs "$CID" 2>&1 | tail -10
        fi
        docker rm -f mmr-test >/dev/null 2>&1 || true
    else
        fail "Docker build failed"
    fi
fi

# ---- Summary ----
echo ""
echo "=== Results ==="
if [ $errors -eq 0 ]; then
    echo -e "${BLUE}All checks passed.${NC} Safe to push."
else
    echo -e "${RED}$errors check(s) failed.${NC} Fix before pushing."
fi
if [ $warnings -gt 0 ]; then
    echo -e "${AMBER}$warnings warning(s).${NC}"
fi
exit $errors
