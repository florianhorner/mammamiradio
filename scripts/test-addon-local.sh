#!/usr/bin/env bash
# Local smoke test for HA addon — catches 80% of CI failures before push.
# Mirrors the validation steps in .github/workflows/addon-build.yml plus
# additional checks derived from real production failures.
#
# Usage: ./scripts/test-addon-local.sh [--build]
#   --build    Also build the Docker image locally (slow, requires Docker)
set -euo pipefail

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
OWNER=$(git remote get-url origin 2>/dev/null | sed 's|.*github.com[:/]||;s|/.*||' || gh api user -q .login 2>/dev/null || echo "unknown")
EXPECTED="ghcr.io/${OWNER}/mammamiradio-addon-{arch}"
if [ "$IMAGE" = "$EXPECTED" ]; then
    pass "Image path: $IMAGE"
else
    fail "Image path mismatch: got '$IMAGE', expected '$EXPECTED'"
fi

# ---- 3. Options mapped in run.sh ----
echo "3. Options → run.sh mapping"
SCHEMA_KEYS=$(sed -n '/^schema:/,/^[^ ]/p' ha-addon/mammamiradio/config.yaml | grep -E '^\s+\w+:' | awk -F: '{print $1}' | tr -d ' ')
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
         ha-addon/mammamiradio/build.yaml \
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

# ---- 7. run.sh syntax check ----
echo "7. Shell syntax"
if bash -n ha-addon/mammamiradio/rootfs/run.sh 2>/dev/null; then
    pass "run.sh syntax OK"
else
    fail "run.sh has syntax errors"
fi

# ---- 8. Hardcoded value sync ----
echo "8. Hardcoded value sync"

# Port 8000 must appear in config.yaml, run.sh
PORT_CONFIG=$(grep 'ingress_port:' ha-addon/mammamiradio/config.yaml | awk '{print $2}')
PORT_RUNSH=$(grep 'MAMMAMIRADIO_PORT=' ha-addon/mammamiradio/rootfs/run.sh | head -1 | sed 's/.*="//' | tr -d '"')
if [ "$PORT_CONFIG" = "8000" ] && [ "$PORT_RUNSH" = "8000" ]; then
    pass "Port 8000 consistent (config.yaml, run.sh)"
else
    fail "Port mismatch: config.yaml=$PORT_CONFIG run.sh=$PORT_RUNSH"
fi

# host_network for local network stream access
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
echo "10. Ingress safety"
if grep -q "_inject_ingress_prefix" mammamiradio/streamer.py; then
    if CHECK_OUTPUT=$($PY -c "
import ast
from pathlib import Path

src = Path('mammamiradio/streamer.py').read_text()
tree = ast.parse(src)
target = None
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name == '_inject_ingress_prefix':
        target = node
        break

if target is None:
    raise SystemExit('missing _inject_ingress_prefix')

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
        raise SystemExit(f'rewrites single-quoted JS path: {pattern}')

print('safe')
" 2>&1); then
        pass "Ingress prefix injection only rewrites safe patterns"
    else
        fail "Ingress safety check failed: $CHECK_OUTPUT"
    fi
else
    warn "No _inject_ingress_prefix found (ingress may not work)"
fi

# ---- 11. Dockerfile doesn't COPY to /data/ ----
echo "11. Dockerfile safety"
if grep -qE '^COPY.*\s/data/' ha-addon/mammamiradio/Dockerfile; then
    fail "Dockerfile COPYs to /data/ — this overwrites persistent volumes on update"
else
    pass "No COPY to /data/ (persistent volume safe)"
fi

# No bare eval 2>&1 in run.sh (subshell captures like SYNC_MSG="$(...2>&1)" are safe)
# Collapse continuation lines, then reject 2>&1 that is NOT inside a $() capture
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
