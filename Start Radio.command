#!/bin/bash
# Double-click this file in Finder to start Mamma Mi Radio.
# Drag it to your Dock for one-click launch.
if ! cd "$(dirname "$0")"; then
  printf '%s\n' "ERROR: The launcher could not open its own folder." \
    "The folder may have been moved, renamed or disconnected after the double-click." \
    "Find the Mamma Mi Radio folder in Finder and double-click Start Radio.command there." \
    'Recovery steps: README.md section "Mac launcher recovery".'
  if [ -t 0 ]; then
    printf '%s\n' "Press any key to close."
    read -r -n 1 _key || true
  fi
  exit 1
fi

# Use login shell environment so homebrew/pyenv/etc. are on PATH
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"
[ -f ~/.zprofile ] && source ~/.zprofile 2>/dev/null
[ -f ~/.zshrc ] && source ~/.zshrc 2>/dev/null

set -a
[ -f .env ] && source .env
set +a

# SETUP-BOUNDARY-START
# Tests execute this region with stubbed tools. Keep login-shell setup above it.
# Finder runs this with macOS /bin/bash 3.2: no mapfile, associative arrays or ${var,,}.
#
# Completion record .venv/.start-radio-setup-complete is plain text, never
# shell-sourced. It is written only after the dependency check, the same inputs, a
# healthy interpreter, and a working ffmpeg. Bump SETUP_CONTRACT_VERSION when
# that record or the required preparation changes meaning.
#
#   contract <version>
#   root <physical checkout path>
#   python <major.minor>
#   requirements <cksum> <bytes>
#   pyproject <cksum> <bytes>

# Undo what a sourced shell profile can change here: a CDPATH or a `cd` function
# changes what `cd` prints, noclobber refuses to overwrite scratch files, and
# errexit or nounset would end the script before it can explain a failure.
unset CDPATH
unset -f cd
set +o noclobber +o errexit +o nounset

PORT="${MAMMAMIRADIO_PORT:-8000}"
SETUP_CONTRACT_VERSION=1
MARKER_PATH=".venv/.start-radio-setup-complete"
TAKE_TIME="This can take several minutes. Leave this window open."
REBUILD_ACTION="If the station window is open, stop the station there and let any running setup finish. Then follow the Mac launcher recovery steps in README.md. Your .env and music/ stay in this checkout."
RETRY_ACTION="Double-click again. If it fails the same way, follow the Mac launcher recovery steps in README.md."
LOCKF_USAGE=64 # EX_USAGE: lockf before macOS 15 cannot lock a descriptor
LOCKF_BUSY=75  # EX_TEMPFAIL: the lock is held by another window
# Same lock as `lockf -s -t 0 9`: flock on the descriptor this shell holds.
FLOCK_FD9='use Fcntl ":flock"; open(my $fd, ">&=", 9) or exit 71;
flock($fd, LOCK_EX | LOCK_NB) and exit 0; exit($!{EWOULDBLOCK} ? 75 : 71);'
PY_MINOR_CHECK='import sys; print("%d.%d" % sys.version_info[:2])'
PY_PROBE_CHECK='import os, sys
ok = 1 if sys.version_info >= (3, 11) else 0
venv = 1 if sys.prefix != sys.base_prefix else 0
minor = "%d.%d" % sys.version_info[:2]
print("%d" % ok)
print("%d" % venv)
print(minor)
print(os.path.realpath(sys.prefix))'

fail_setup() {
  # Release the setup lock first, so a retry is not refused while this window waits.
  exec 9>&-
  printf '%s\n' "ERROR: $1" "$2" "$3" 'Recovery steps: README.md section "Mac launcher recovery".'
  if [ -t 0 ]; then
    printf '%s\n' "Press any key to close."
    read -r -n 1 _key || true
  fi
  exit 1
}

env_stop() {
  fail_setup "Setup could not prepare the station environment." "$1" "$REBUILD_ACTION"
}

is_uint() {
  case "$1" in ''|*[!0-9]*) return 1 ;; esac
}

is_minor() {
  is_uint "${1%%.*}" && is_uint "${1#*.}"
}

version_supported() {
  local maj min
  is_minor "$1" || return 1
  maj=${1%%.*}
  min=${1#*.}
  [ "$maj" -gt 3 ] || { [ "$maj" -eq 3 ] && [ "$min" -ge 11 ]; }
}

fingerprint_into() {
  local _out _sum _count
  if [ ! -f "$2" ] || [ ! -r "$2" ]; then
    fail_setup "Setup could not read the project files." \
      "Setup could not read ${2}." \
      "Restore ${2} in this checkout, then double-click again."
  fi
  _out=$(cksum "$2" 2>/dev/null) || _out=""
  read -r _sum _count _ <<<"$_out"
  if ! is_uint "$_sum" || ! is_uint "$_count"; then
    fail_setup "Setup could not check the project files." \
      "cksum ${2} did not succeed." \
      "Restore ${2}, then double-click again."
  fi
  printf -v "$1" '%s %s' "$_sum" "$_count"
}

read_fingerprint() {
  fingerprint_into FP_REQ requirements.txt
  fingerprint_into FP_PY pyproject.toml
}

two_numbers() {
  local first second rest
  read -r first second rest <<<"$1"
  [ -z "$rest" ] && is_uint "$first" && is_uint "$second"
}

load_marker() {
  local _line _count=0
  MARKER_STATE=missing
  MARKER_CONTRACT=""
  MARKER_ROOT=""
  MARKER_PYTHON=""
  MARKER_REQUIREMENTS=""
  MARKER_PYPROJECT=""
  [ -e "$MARKER_PATH" ] || return 0
  MARKER_STATE=malformed
  [ -f "$MARKER_PATH" ] && [ -r "$MARKER_PATH" ] || return 0
  while IFS= read -r _line || [ -n "$_line" ]; do
    _count=$((_count + 1))
    case "$_line" in
      "contract "*) MARKER_CONTRACT=${_line#"contract "} ;;
      "root "*) MARKER_ROOT=${_line#"root "} ;;
      "python "*) MARKER_PYTHON=${_line#"python "} ;;
      "requirements "*) MARKER_REQUIREMENTS=${_line#"requirements "} ;;
      "pyproject "*) MARKER_PYPROJECT=${_line#"pyproject "} ;;
      *) return 0 ;;
    esac
  done < "$MARKER_PATH"
  # Five lines with five non-empty fields means each key appeared exactly once.
  if [ "$_count" -eq 5 ] && is_uint "$MARKER_CONTRACT" && [ -n "$MARKER_ROOT" ] &&
    is_minor "$MARKER_PYTHON" && two_numbers "$MARKER_REQUIREMENTS" &&
    two_numbers "$MARKER_PYPROJECT"; then
    MARKER_STATE=ok
  fi
}

run_probe() {
  local _out _extra
  PROBE_OK=""
  PROBE_VENV=""
  PROBE_MINOR=""
  PROBE_PREFIX=""
  [ -x .venv/bin/python ] || return 1
  _out=$(.venv/bin/python -c "$PY_PROBE_CHECK" 2>/dev/null) || return 1
  { IFS= read -r PROBE_OK && IFS= read -r PROBE_VENV && IFS= read -r PROBE_MINOR &&
    IFS= read -r PROBE_PREFIX && ! IFS= read -r _extra; } <<<"$_out" || return 1
  case "$PROBE_OK" in 0|1) ;; *) return 1 ;; esac
  case "$PROBE_VENV" in 0|1) ;; *) return 1 ;; esac
  case "$PROBE_MINOR" in *.*.*|"") return 1 ;; esac
}

env_healthy() {
  local expected activated
  version_supported "$PROBE_MINOR" || return 1
  [ "$PROBE_OK" = "1" ] || return 1
  [ "$PROBE_VENV" = "1" ] || return 1
  [ -n "$PROBE_PREFIX" ] || return 1
  expected=$(cd .venv && /bin/pwd -P) || return 1
  [ "$PROBE_PREFIX" = "$expected" ] || return 1
  [ -r .venv/pyvenv.cfg ] || return 1
  # start.sh activates .venv. After the checkout moves, the activation script still
  # names the old folder, so ask it, the way start.sh will, which folder it means.
  activated=$(unset VIRTUAL_ENV; . .venv/bin/activate >/dev/null 2>&1 &&
    [ -n "$VIRTUAL_ENV" ] && cd "$VIRTUAL_ENV" 2>/dev/null && /bin/pwd -P) || return 1
  [ "$activated" = "$expected" ]
}

minor_from_cfg() {
  local line ver maj min
  [ -r .venv/pyvenv.cfg ] || return 1
  line=$(grep -E '^version[[:space:]]*=' .venv/pyvenv.cfg | head -n 1)
  ver=${line#*=}
  ver=${ver//[[:space:]]/}
  maj=${ver%%.*}
  min=${ver#*.}
  min=${min%%.*}
  is_uint "$maj" && is_uint "$min" || return 1
  printf '%s.%s\n' "$maj" "$min"
}

python_minor() {
  command -v "$1" >/dev/null 2>&1 && "$1" -c "$PY_MINOR_CHECK" 2>/dev/null
}

choose_base() {
  local want got p
  want=$1
  OTHER_MINOR=""
  for p in "python${want}" python3.13 python3.12 python3.11 python3; do
    got=$(python_minor "$p") || continue
    if [ "$got" = "$want" ]; then
      BASE=$p
      return 0
    fi
    if [ -z "$OTHER_MINOR" ] && version_supported "$got"; then
      OTHER_MINOR=$got
    fi
  done
  if got=$(python_minor .venv/bin/python) && [ "$got" = "$want" ]; then
    BASE=".venv/bin/python"
    return 0
  fi
  return 1
}

select_python() {
  local p got
  for p in python3.13 python3.12 python3.11 python3; do
    got=$(python_minor "$p") || continue
    if version_supported "$got"; then
      BASE=$p
      EXPECTED_MINOR=$got
      return 0
    fi
  done
  return 1
}

check_ffmpeg() {
  FF_OUT=""
  FF_MISSING=0
  if ! command -v ffmpeg >/dev/null 2>&1; then
    FF_MISSING=1
    return 1
  fi
  FF_OUT=$(ffmpeg -version 2>&1)
}

ffmpeg_cause() {
  if [ "$FF_MISSING" = "1" ]; then
    printf '%s\n' "ffmpeg was not found on PATH."
  elif [ -n "$FF_OUT" ]; then
    printf '%s\n' "ffmpeg -version did not succeed. ${FF_OUT}"
  else
    printf '%s\n' "ffmpeg -version did not succeed."
  fi
}

require_ffmpeg() {
  check_ffmpeg || fail_setup "$1" "$(ffmpeg_cause)" \
    "Install ffmpeg, then double-click again. With Homebrew: brew install ffmpeg."
}

station_running() {
  pgrep -f "uvicorn mammamiradio" >/dev/null 2>&1
}

no_base_stop() {
  if [ -n "$OTHER_MINOR" ]; then
    env_stop "The available Python is ${OTHER_MINOR}, which does not match this environment's Python ${1}."
  fi
  env_stop "No Python ${1} interpreter is available to repair this environment."
}

decide_setup() {
  local want
  if [ ! -d .venv ]; then
    if [ -e .venv ]; then
      env_stop ".venv exists and is not a directory."
    fi
    PREP_KIND=first
    ENV_MODE=create
    read_fingerprint
    return 0
  fi
  load_marker
  read_fingerprint
  if run_probe; then
    if ! version_supported "$PROBE_MINOR"; then
      fail_setup "The station environment uses Python older than 3.11." \
        ".venv/bin/python reported Python ${PROBE_MINOR}." \
        "$REBUILD_ACTION"
    fi
    if env_healthy; then
      ENV_MODE=reuse
    else
      choose_base "$PROBE_MINOR" || no_base_stop "$PROBE_MINOR"
      ENV_MODE=reseed
    fi
    EXPECTED_MINOR=$PROBE_MINOR
  else
    want=$(minor_from_cfg) ||
      env_stop "Setup could not read the environment's Python version from .venv/bin/python or pyvenv.cfg."
    if ! version_supported "$want"; then
      fail_setup "The station environment uses Python older than 3.11." \
        "pyvenv.cfg names Python ${want}." \
        "$REBUILD_ACTION"
    fi
    choose_base "$want" || no_base_stop "$want"
    ENV_MODE=relink
    EXPECTED_MINOR=$want
  fi
  if [ "$MARKER_STATE" = "ok" ] && [ "$MARKER_ROOT" != "$START_ROOT" ]; then
    PREP_KIND=moved
  elif [ "$ENV_MODE" != "reuse" ] || [ "$MARKER_STATE" != "ok" ]; then
    PREP_KIND=legacy
  elif [ "$MARKER_CONTRACT" != "$SETUP_CONTRACT_VERSION" ]; then
    PREP_KIND=contract
  elif [ "$MARKER_REQUIREMENTS" != "$FP_REQ" ] || [ "$MARKER_PYPROJECT" != "$FP_PY" ]; then
    PREP_KIND=changed
  elif [ "$MARKER_PYTHON" != "$PROBE_MINOR" ]; then
    PREP_KIND=minor
  else
    PREP_KIND=ready
  fi
}

missing_python_stop() {
  fail_setup "Mamma Mi Radio needs Python 3.11 or newer, and this Mac has none." \
    "Setup found no supported Python on PATH." \
    "Install it from python.org or with Homebrew, then double-click again."
}

install_with_brew() {
  local pkg
  pkg=$1
  printf '%s\n' "Installing ${pkg}..."
  if ! brew install "$pkg"; then
    fail_setup "Installing ${pkg} failed." \
      "brew install ${pkg} did not succeed. See the output above." \
      "Fix the Homebrew error, then double-click again."
  fi
}

ensure_tools() {
  local need_python need_ffmpeg shellenv brew_err
  need_python=0
  need_ffmpeg=0
  if [ "$ENV_MODE" = "create" ] && ! select_python; then
    need_python=1
  fi
  if ! check_ffmpeg; then
    need_ffmpeg=1
  fi
  if [ "$need_python" = "0" ] && [ "$need_ffmpeg" = "0" ]; then
    return 0
  fi
  if ! command -v brew >/dev/null 2>&1; then
    if [ "$need_python" = "1" ]; then
      missing_python_stop
    fi
    fail_setup "ffmpeg is not usable." \
      "$(ffmpeg_cause) Homebrew is not available to install it." \
      "Install ffmpeg, then double-click again."
  fi
  if [ "$need_python" = "1" ]; then
    install_with_brew python@3.13
  fi
  if [ "$need_ffmpeg" = "1" ]; then
    install_with_brew ffmpeg
  fi
  if ! shellenv=$(brew shellenv 2>tmp/start-radio-brew.err); then
    brew_err=$(cat tmp/start-radio-brew.err 2>/dev/null || true)
    fail_setup "Installing tools failed." \
      "brew shellenv did not succeed.${brew_err:+ $brew_err}" \
      "Fix the Homebrew error, then double-click again."
  fi
  eval "$shellenv" || fail_setup "Installing tools failed." \
    "brew shellenv could not be applied." \
    "Fix the Homebrew error, then double-click again."
  hash -r
  if [ "$need_python" = "1" ] && ! select_python; then
    fail_setup "Mamma Mi Radio needs Python 3.11 or newer, and this Mac has none." \
      "brew install python@3.13 finished, but setup still found no supported Python on PATH." \
      "Install Python 3.11 or newer, then double-click again."
  fi
  if ! check_ffmpeg; then
    fail_setup "ffmpeg is not usable." \
      "brew install ffmpeg finished, and ffmpeg is still not usable. $(ffmpeg_cause)" \
      "Install ffmpeg, then double-click again."
  fi
}

check_env() {
  if ! run_probe || ! env_healthy; then
    env_stop "$1"
  fi
  if [ "$PROBE_MINOR" != "$EXPECTED_MINOR" ]; then
    fail_setup "The station environment changed during setup." \
      "Setup started with Python ${EXPECTED_MINOR}, and .venv/bin/python now reports Python ${PROBE_MINOR}." \
      "Double-click again."
  fi
}

prepare_env() {
  if [ "$ENV_MODE" != "reuse" ]; then
    if [ "$ENV_MODE" = "relink" ] && [ "$BASE" != ".venv/bin/python" ]; then
      # The environment's interpreter failed the check, and python -m venv keeps an
      # existing interpreter file or link as it is, so drop them before relinking.
      rm -f .venv/bin/python .venv/bin/python3 .venv/bin/python3.*
    fi
    if ! "$BASE" -m venv .venv; then
      env_stop "$BASE -m venv .venv did not succeed. See the output above."
    fi
  fi
  check_env "After preparing .venv, .venv/bin/python still did not pass the environment check."
}

ensure_pip() {
  if .venv/bin/python -m pip --version >/dev/null 2>&1; then
    return 0
  fi
  if ! .venv/bin/python -m ensurepip --upgrade; then
    fail_setup "The station environment has no pip." \
      ".venv/bin/python -m ensurepip did not succeed. See the output above." \
      "$REBUILD_ACTION"
  fi
  if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
    fail_setup "The station environment has no pip." \
      ".venv/bin/python -m pip --version did not succeed after ensurepip." \
      "$REBUILD_ACTION"
  fi
}

update_installer() {
  if ! .venv/bin/python -m pip install --upgrade pip setuptools --quiet; then
    fail_setup "Updating the installer failed." \
      ".venv/bin/python -m pip install --upgrade pip setuptools did not succeed. See the output above." \
      "$RETRY_ACTION"
  fi
}

fetch_components() {
  # Downloads what the update installs and installs nothing. With the index unreachable,
  # pip still reports an installed pip as current, so only this tells an offline update.
  .venv/bin/python -m pip install --dry-run --quiet --retries 1 --force-reinstall --require-hashes -r requirements.txt
}

components_installed() {
  # Without the network: every pinned component is installed at its pinned version.
  .venv/bin/python -m pip install --dry-run --quiet --no-index --require-hashes -r requirements.txt >/dev/null 2>&1
}

install_components() {
  if ! .venv/bin/python -m pip install --force-reinstall --require-hashes -r requirements.txt --quiet; then
    fail_setup "Installing station components failed." \
      "The install from requirements.txt did not succeed. See the output above." \
      "$RETRY_ACTION"
  fi
  if ! .venv/bin/python -m pip install --no-deps -e . --quiet; then
    fail_setup "Installing the station package failed." \
      "The package install did not succeed. See the output above." \
      "$RETRY_ACTION"
  fi
}

clear_marker() {
  if [ -e "$MARKER_PATH" ] && [ ! -f "$MARKER_PATH" ]; then
    fail_setup "Setup could not remove the previous setup record." \
      "The setup record path ${MARKER_PATH} is not a regular file." \
      "$REBUILD_ACTION"
  fi
  if ! rm -f "$MARKER_PATH"; then
    fail_setup "Setup could not remove the previous setup record." \
      "rm could not remove the old setup record." \
      "Make .venv writable, then double-click again."
  fi
}

verify_same_inputs() {
  local now_req now_py end_root changed=""
  fingerprint_into now_req requirements.txt
  fingerprint_into now_py pyproject.toml
  end_root=$(/bin/pwd -P) || end_root=""
  if [ "$end_root" != "$START_ROOT" ]; then
    fail_setup "The project files changed during setup." \
      "The checkout path changed while components were installing." \
      "Double-click again from the station checkout."
  fi
  [ "$now_req" = "$FP_REQ" ] || changed="requirements.txt"
  [ "$now_py" = "$FP_PY" ] || changed="${changed:+$changed and }pyproject.toml"
  if [ -n "$changed" ]; then
    fail_setup "The project files changed during setup." \
      "${changed} changed while components were installing." \
      "Double-click again so setup uses the current files."
  fi
  check_env "After installing components, .venv/bin/python did not pass the environment check."
  require_ffmpeg "ffmpeg is not usable, so setup did not finish."
}

publish_marker() {
  local body tmp_path="" pub_err written
  body=$(printf 'contract %s\nroot %s\npython %s\nrequirements %s\npyproject %s' \
    "$SETUP_CONTRACT_VERSION" "$START_ROOT" "$EXPECTED_MINOR" "$FP_REQ" "$FP_PY")
  # mktemp creates the file with mode 600.
  if ! { tmp_path=$(mktemp "${MARKER_PATH}.XXXXXX") &&
    printf '%s\n' "$body" >"$tmp_path" &&
    mv "$tmp_path" "$MARKER_PATH"; } 2>tmp/start-radio-publish.err; then
    pub_err=$(cat tmp/start-radio-publish.err 2>/dev/null || true)
    [ -z "$tmp_path" ] || rm -f "$tmp_path" 2>/dev/null
    fail_setup "Recording setup failed." \
      "Setup installed and checked the components but could not write the setup record.${pub_err:+ $pub_err}" \
      "Free space under .venv and make it writable, then double-click again."
  fi
  written=$(cat "$MARKER_PATH" 2>/dev/null) || written=""
  if [ "$written" != "$body" ]; then
    rm -f "$MARKER_PATH"
    fail_setup "Recording setup failed." \
      "Setup installed and checked the components, but the setup record it wrote read back differently." \
      "Free space under .venv and make it writable, then double-click again."
  fi
}

announce_prep() {
  local lead
  case "$PREP_KIND" in
    first) lead="First run. Preparing the station." ;;
    legacy) lead="Preparing the existing station environment." ;;
    moved) lead="This station folder moved. Preparing the station for its new place." ;;
    changed) lead="Station components changed. Preparing the updated components." ;;
    contract) lead="Setup steps changed. Preparing the station again." ;;
    minor) lead="The recorded Python does not match this environment. Preparing the station environment again." ;;
  esac
  printf '%s %s\n' "$lead" "$TAKE_TIME"
}

perform_setup() {
  if station_running; then
    fail_setup "The station is running, so setup will not change its environment." \
      "A station process is still running (uvicorn mammamiradio)." \
      "Stop it in its own window, then double-click again."
  fi
  announce_prep
  printf '%s\n' "Preparing tools..."
  ensure_tools
  if { [ "$PREP_KIND" = "changed" ] || [ "$PREP_KIND" = "contract" ]; } &&
    .venv/bin/python -m pip --version >/dev/null 2>&1 && ! fetch_components; then
    # The update cannot be downloaded, for example offline, and nothing has changed yet.
    # The recorded setup keeps playing if its components already match requirements.txt.
    if components_installed; then
      printf '%s\n' "Setup could not download the station update. Starting the station you already have." \
        "Double-click again when you are online to finish the update."
      return 0
    fi
    fail_setup "Setup could not download the station update." \
      "The update needs station components that are not installed yet." \
      "Connect to the internet, then double-click again."
  fi
  clear_marker
  printf '%s\n' "Preparing the station environment..."
  prepare_env
  ensure_pip
  printf '%s\n' "Installing station components..."
  update_installer
  install_components
  printf '%s\n' "Checking components..."
  if ! .venv/bin/python -m pip check; then
    fail_setup "The installed components are inconsistent." \
      ".venv/bin/python -m pip check did not succeed. See the output above." \
      "$RETRY_ACTION"
  fi
  verify_same_inputs
  printf '%s\n' "Recording setup..."
  publish_marker
  printf '%s\n' "Setup complete!"
}

acquire_setup_lock() {
  local mkdir_err lock_status lock_err
  if ! mkdir_err=$(mkdir -p tmp 2>&1); then
    fail_setup "Setup could not start." \
      "Setup could not create tmp/ for its lock in this checkout.${mkdir_err:+ $mkdir_err}" \
      "Make this checkout writable, then double-click again."
  fi
  if ! exec 9>>tmp/start-radio-setup.lock; then
    fail_setup "Setup could not start." \
      "Setup could not open its lock file tmp/start-radio-setup.lock." \
      "Make this checkout writable, then double-click again."
  fi
  if ! command -v lockf >/dev/null 2>&1; then
    fail_setup "Setup could not start." \
      "The lockf command is not available." \
      "This launcher needs macOS lockf. Double-click it on a Mac."
  fi
  lockf -s -t 0 9 >tmp/start-radio-lockf.err 2>&1
  lock_status=$?
  if [ "$lock_status" -eq "$LOCKF_USAGE" ]; then
    perl -e "$FLOCK_FD9" >tmp/start-radio-lockf.err 2>&1
    lock_status=$?
  fi
  if [ "$lock_status" -ne 0 ]; then
    if [ "$lock_status" -eq "$LOCKF_BUSY" ]; then
      fail_setup "Setup is already running." \
        "Another window is still preparing the station. Closing that window can leave an installer running." \
        "Let it finish, then double-click again. Do not delete the lock file."
    fi
    lock_err=$(cat tmp/start-radio-lockf.err 2>/dev/null || true)
    fail_setup "Setup could not start." \
      "lockf could not acquire the setup lock (exit ${lock_status}).${lock_err:+ $lock_err}" \
      "This launcher needs macOS lockf on a writable checkout. Fix the lock error above, then double-click again."
  fi
}

START_ROOT=$(/bin/pwd -P) || fail_setup "Setup could not start." \
  "Setup could not work out where this checkout is." \
  "Double-click again from the station checkout."
acquire_setup_lock
decide_setup
if [ "$PREP_KIND" = "ready" ]; then
  require_ffmpeg "ffmpeg is not usable, so the station cannot start."
else
  perform_setup
fi
exec 9>&-

if station_running; then
  echo "Radio already running."
else
  echo "Starting Mamma Mi Radio..."
  ./start.sh &
  for i in $(seq 1 30); do
    curl -sf -o /dev/null "http://localhost:${PORT}/listen" && break
    sleep 1
  done
fi

open "http://localhost:${PORT}/"
echo ""
echo "Dashboard: http://localhost:${PORT}/"
echo "Listener:  http://localhost:${PORT}/listen  (share this with friends)"
echo ""
echo "Close this window to stop the radio."
wait
# SETUP-BOUNDARY-END
