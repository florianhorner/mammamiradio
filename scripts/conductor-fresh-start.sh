#!/usr/bin/env bash
set -euo pipefail

umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"

usage() {
  cat <<'EOF'
Usage: scripts/conductor-fresh-start.sh [--yes]

Archive and recreate the effective Conductor radio cache and temp runtime.

Without --yes this prints the resolved paths and makes no changes.
With --yes it refuses to run while this workspace's radio is active or
starting, or while the configured port is listening. It then archives the
cache and temp directories before recreating them empty. For destructive
runs, the resolved directory names must be exactly cache and tmp.
EOF
}

die() {
  printf 'conductor-fresh-start: %s\n' "$*" >&2
  exit 1
}

[ "$#" -le 1 ] || { usage >&2; exit 2; }

YES=0
case "${1:-}" in
  "") ;;
  --yes) YES=1 ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

# Match the effective Run environment: conductor-run.sh loads the home file or
# falls back to the repository file, then fills runtime defaults; start.sh
# always loads the repository file again. Do not create runtime directories
# during a dry run.
HOME_DIR="${HOME:-}"
ENV_SAFE="${HOME_DIR:+$HOME_DIR/.config/mammamiradio/.env}"
if [ -n "$ENV_SAFE" ] && [ -f "$ENV_SAFE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_SAFE"
  set +a
elif [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi
RUNTIME_ROOT="$ROOT/.context/conductor"
export MAMMAMIRADIO_PORT="${MAMMAMIRADIO_PORT:-${CONDUCTOR_PORT:-8000}}"
export MAMMAMIRADIO_TMP_DIR="${MAMMAMIRADIO_TMP_DIR:-$RUNTIME_ROOT/tmp}"
export MAMMAMIRADIO_CACHE_DIR="${MAMMAMIRADIO_CACHE_DIR:-$RUNTIME_ROOT/cache}"
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

PORT="${MAMMAMIRADIO_PORT:-${CONDUCTOR_PORT:-8000}}"
RAW_CACHE_DIR="${MAMMAMIRADIO_CACHE_DIR:-$RUNTIME_ROOT/cache}"
RAW_TMP_DIR="${MAMMAMIRADIO_TMP_DIR:-$RUNTIME_ROOT/tmp}"

if [ -x "$ROOT/.venv/bin/python" ]; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
else
  die "cannot resolve runtime paths because Python 3 is unavailable"
fi

to_absolute() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *) printf '%s/%s\n' "$ROOT" "$1" ;;
  esac
}

strip_trailing_slashes() {
  local path="$1"
  while [ "${path%/}" != "$path" ] && [ "$path" != "/" ]; do
    path="${path%/}"
  done
  printf '%s\n' "$path"
}

canonicalize_target() {
  local path="$1"
  "$PYTHON_BIN" -c \
    'import os, sys; print(os.path.realpath(os.path.abspath(sys.argv[1])))' \
    "$path"
}

# Use a conservative, case-folded key for safety comparisons. macOS commonly
# uses a case-insensitive filesystem while realpath preserves the spelling the
# caller supplied; comparing display paths directly would let /users alias
# /Users. Case-folding can reject a harmless case-only distinction on a
# case-sensitive filesystem, which is preferable to moving a broader target.
comparison_key() {
  "$PYTHON_BIN" -c \
    'import os, sys; print(os.path.normcase(sys.argv[1]).casefold())' \
    "$1"
}

CACHE_INPUT="$(strip_trailing_slashes "$(to_absolute "$RAW_CACHE_DIR")")"
TMP_INPUT="$(strip_trailing_slashes "$(to_absolute "$RAW_TMP_DIR")")"
[ ! -L "$CACHE_INPUT" ] || die "refusing symlink cache target: $CACHE_INPUT"
[ ! -L "$TMP_INPUT" ] || die "refusing symlink temp target: $TMP_INPUT"
CACHE_DIR="$(canonicalize_target "$CACHE_INPUT")" || die "cannot resolve cache target: $CACHE_INPUT"
TMP_DIR="$(canonicalize_target "$TMP_INPUT")" || die "cannot resolve temp target: $TMP_INPUT"
ROOT_KEY="$(comparison_key "$ROOT")" || die "cannot normalize repository path: $ROOT"
CACHE_KEY="$(comparison_key "$CACHE_DIR")" || die "cannot normalize cache target: $CACHE_DIR"
TMP_KEY="$(comparison_key "$TMP_DIR")" || die "cannot normalize temp target: $TMP_DIR"
PHYSICAL_HOME=""
HOME_KEY=""
if [ -n "$HOME_DIR" ]; then
  PHYSICAL_HOME="$(canonicalize_target "$HOME_DIR")" || die "cannot resolve home directory: $HOME_DIR"
  HOME_KEY="$(comparison_key "$PHYSICAL_HOME")" || die "cannot normalize home directory: $PHYSICAL_HOME"
fi

is_same_or_descendant() {
  local parent="$1"
  local candidate="$2"
  case "$candidate/" in
    "$parent/"*) return 0 ;;
    *) return 1 ;;
  esac
}

validate_target_safety() {
  local label="$1"
  local path="$2"
  local key="$3"

  [ -n "$path" ] || die "$label path is empty"
  case "$key" in
    /|/tmp|/private/tmp|"$ROOT_KEY")
      die "refusing unsafe $label target: $path"
      ;;
  esac
  if [ -n "$HOME_KEY" ] && [ "$key" = "$HOME_KEY" ]; then
    die "refusing unsafe $label target: $path"
  fi
  # Refuse a target that would contain the repository or the user's home
  # directory. This still permits a deliberately configured external
  # .../cache or .../tmp slot directory.
  if is_same_or_descendant "$key" "$ROOT_KEY" || {
    [ -n "$HOME_KEY" ] && is_same_or_descendant "$key" "$HOME_KEY"
  }; then
    die "refusing unsafe $label target: $path"
  fi
  if [ -L "$path" ]; then
    die "refusing symlink $label target: $path"
  fi
  if [ -e "$path" ] && [ ! -d "$path" ]; then
    die "$label target is not a directory: $path"
  fi
}

validate_target() {
  local label="$1"
  local path="$2"
  local key="$3"
  local expected_leaf="$4"
  local leaf_key

  validate_target_safety "$label" "$path" "$key"
  leaf_key="$(comparison_key "$(basename "$path")")" || die "cannot normalize $label target name: $path"
  if [ "$leaf_key" != "$expected_leaf" ]; then
    die "$label target must end in /$expected_leaf: $path"
  fi
}

validate_target_safety "cache" "$CACHE_DIR" "$CACHE_KEY"
validate_target_safety "temp" "$TMP_DIR" "$TMP_KEY"

if is_same_or_descendant "$CACHE_KEY" "$TMP_KEY" || is_same_or_descendant "$TMP_KEY" "$CACHE_KEY"; then
  die "cache and temp targets overlap: $CACHE_DIR / $TMP_DIR"
fi

validate_port() {
  case "$PORT" in
    ''|*[!0-9]*) die "configured radio port is not numeric: $PORT" ;;
  esac
  [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "configured radio port is out of range: $PORT"
}

validate_target "cache" "$CACHE_DIR" "$CACHE_KEY" "cache"
validate_target "temp" "$TMP_DIR" "$TMP_KEY" "tmp"
validate_port

CACHE_PARENT="$(dirname "$CACHE_DIR")"
TMP_PARENT="$(dirname "$TMP_DIR")"
STAMP="$(date -u '+%Y%m%dT%H%M%SZ')-$$"
if [ "$CACHE_PARENT" = "$TMP_PARENT" ]; then
  ARCHIVE_ROOT="$CACHE_PARENT/fresh-start-archives/$STAMP"
  CACHE_ARCHIVE="$ARCHIVE_ROOT/$(basename "$CACHE_DIR")"
  TMP_ARCHIVE="$ARCHIVE_ROOT/$(basename "$TMP_DIR")"
else
  CACHE_ARCHIVE="$CACHE_PARENT/fresh-start-archives/$STAMP-$(basename "$CACHE_DIR")"
  TMP_ARCHIVE="$TMP_PARENT/fresh-start-archives/$STAMP-$(basename "$TMP_DIR")"
fi

if [ "$YES" -eq 1 ]; then
  printf 'conductor-fresh-start: confirmed run\n'
else
  printf 'conductor-fresh-start: dry run\n'
fi
printf '  cache: %s\n' "$CACHE_DIR"
printf '  temp:  %s\n' "$TMP_DIR"
printf '  port:  %s\n' "$PORT"
printf '  cache archive: %s\n' "$CACHE_ARCHIVE"
printf '  temp archive:  %s\n' "$TMP_ARCHIVE"

if [ "$YES" -ne 1 ]; then
  printf 'No changes made. Re-run with --yes to archive and recreate this runtime.\n'
  exit 0
fi

command -v lsof >/dev/null 2>&1 || die "cannot verify radio state because lsof is unavailable"
command -v ps >/dev/null 2>&1 || die "cannot verify radio state because ps is unavailable"

workspace_radio_pid() {
  local process_rows pid command cwd_output cwd
  process_rows="$(ps -axo pid=,command= 2>/dev/null)" || return 2
  while read -r pid command; do
    case "$pid" in ''|*[!0-9]*) continue ;; esac
    case "$command" in
      *"scripts/conductor-run.sh"*|*" ./start.sh"*|*"$ROOT/start.sh"*|*"mammamiradio.main:app"*) ;;
      *) continue ;;
    esac
    if ! cwd_output="$(lsof -a -nP -p "$pid" -d cwd -Fn 2>/dev/null)"; then
      kill -0 "$pid" 2>/dev/null || continue
      return 2
    fi
    cwd="$(printf '%s\n' "$cwd_output" | sed -n 's/^n//p' | head -n 1)"
    if [ -z "$cwd" ] && kill -0 "$pid" 2>/dev/null; then
      return 2
    fi
    if [ "$cwd" = "$ROOT" ]; then
      printf '%s\n' "$pid"
      return 0
    fi
  done <<< "$process_rows"
  return 1
}

assert_radio_stopped() {
  local active_pid process_status lsof_status
  if active_pid="$(workspace_radio_pid)"; then
    die "workspace radio process $active_pid is active or starting; stop it before resetting runtime state"
  else
    process_status=$?
    [ "$process_status" -eq 1 ] || die "could not verify workspace radio process state"
  fi
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
    die "radio port $PORT is active; stop the local radio before resetting runtime state"
  else
    lsof_status=$?
    [ "$lsof_status" -eq 1 ] || die "could not verify radio port $PORT state (lsof exit $lsof_status)"
  fi
}

assert_radio_stopped

[ ! -e "$CACHE_ARCHIVE" ] || die "archive destination already exists: $CACHE_ARCHIVE"
[ ! -e "$TMP_ARCHIVE" ] || die "archive destination already exists: $TMP_ARCHIVE"

# Close the normal startup-before-bind window as tightly as this standalone
# utility can: a runner already launched for this workspace is caught by its
# cwd/command before the first move, even when its TCP listener is not ready.
assert_radio_stopped

archive_and_recreate() {
  local source="$1"
  local archive="$2"
  local archive_parent
  archive_parent="$(dirname "$archive")"
  mkdir -p "$archive_parent"
  chmod 700 "$archive_parent"
  if [ -e "$source" ]; then
    mv "$source" "$archive"
  fi
  mkdir -p "$source"
  chmod 700 "$source"
}

archive_and_recreate "$CACHE_DIR" "$CACHE_ARCHIVE"
archive_and_recreate "$TMP_DIR" "$TMP_ARCHIVE"

printf 'conductor-fresh-start: archived runtime state\n'
printf '  cache archive: %s\n' "$CACHE_ARCHIVE"
printf '  temp archive:  %s\n' "$TMP_ARCHIVE"
printf '  fresh cache:   %s\n' "$CACHE_DIR"
printf '  fresh temp:    %s\n' "$TMP_DIR"
