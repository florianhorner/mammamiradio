#!/usr/bin/env bash
# Disposable, local-only Home Assistant lab for the First Listen golden path.
#
# Runtime state and credentials live below gitignored tmp/. The script never
# connects to a live HA host, changes the global Docker context, restarts HA, or
# deletes lab state. Bash 3.2 compatible for the macOS system shell.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
LAB_ROOT="$ROOT/tmp/first-listen-ha-lab"
HA_CONFIG="$LAB_ROOT/ha-config"
RADIO_CACHE="$LAB_ROOT/radio-cache"
RADIO_TMP="$LAB_ROOT/radio-tmp"
RADIO_MUSIC="$LAB_ROOT/radio-music"
RADIO_RUNTIME="$LAB_ROOT/radio-runtime"
VLC_RUNTIME="$LAB_ROOT/vlc-runtime"
BRIDGE_RUNTIME="$LAB_ROOT/bridge-runtime"
ARCHIVE_ROOT="$LAB_ROOT/archives"
LOG_ROOT="$LAB_ROOT/logs"
COMPONENT_BACKUP_ROOT="$LAB_ROOT/component-backups"
ENV_FILE="$LAB_ROOT/lab.env"
LAB_MARKER="$HA_CONFIG/.mammamiradio-first-listen-lab"
LAB_MARKER_TEXT="Disposable Mamma Mi Radio First Listen lab. Never mount live HA data here."
RADIO_ENV_FILE="$RADIO_RUNTIME/.env"
RADIO_CONFIG_FILE="$RADIO_RUNTIME/radio.toml"
RADIO_MODEL_REGISTRY_FILE="$RADIO_RUNTIME/model_registry.toml"
LOCAL_TRACK="$RADIO_MUSIC/Mamma Mi Lab - First Listen.mp3"
FIRST_LISTEN_SHOW="$ROOT/mammamiradio/assets/demo/first_listen/first_listen_show.mp3"

COLIMA_PROFILE="mmr-first-listen"
DOCKER_CONTEXT="colima-mmr-first-listen"
HA_CONTAINER="mmr-first-listen-ha"
HA_IMAGE="ghcr.io/home-assistant/home-assistant:stable"
HA_LABEL="mammamiradio.first-listen-lab=true"
HA_CONTAINER_MIGRATION_MARKER="$LAB_ROOT/.ha-container-loopback-migration-v1"
HA_URL_DEFAULT="http://127.0.0.1:8123"
RADIO_URL="http://127.0.0.1:8000"
VLC_PORT="4212"
RADIO_PORT="8000"
DOCKER_GATEWAY_IP="172.17.0.1"
VLC_TUNNEL_PORT="14212"
RADIO_TUNNEL_PORT="18000"
REMOTE_BRIDGE_RUNTIME="/tmp/mammamiradio-first-listen-bridge-v1"
REMOTE_VLC_BRIDGE_PID_FILE="$REMOTE_BRIDGE_RUNTIME/vlc-$VLC_PORT-$VLC_TUNNEL_PORT.pid"
REMOTE_RADIO_BRIDGE_PID_FILE="$REMOTE_BRIDGE_RUNTIME/radio-$RADIO_PORT-$RADIO_TUNNEL_PORT.pid"
COLIMA_HOME_ROOT="${COLIMA_HOME:-${HOME:?HOME is required to locate the isolated Colima profile}/.colima}"
COLIMA_SSH_CONFIG="$COLIMA_HOME_ROOT/_lima/colima-$COLIMA_PROFILE/ssh.config"
COLIMA_SSH_HOST="lima-colima-$COLIMA_PROFILE"
SSH_BINARY="/usr/bin/ssh"

RADIO_PID_FILE="$LAB_ROOT/radio.pid"
VLC_PID_FILE="$LAB_ROOT/vlc.pid"
SSH_TUNNEL_PID_FILE="$BRIDGE_RUNTIME/ssh-tunnel.pid"
VLC_BRIDGE_PID_FILE="$BRIDGE_RUNTIME/vlc-bridge.pid"
RADIO_BRIDGE_PID_FILE="$BRIDGE_RUNTIME/radio-bridge.pid"
BRIDGE_MARKER="$BRIDGE_RUNTIME/.secure-bridge-v1"
RADIO_LOG="$LOG_ROOT/radio.log"
VLC_LOG="$LOG_ROOT/vlc.log"
SSH_TUNNEL_LOG="$LOG_ROOT/ssh-tunnel.log"
VLC_BRIDGE_LOG="$LOG_ROOT/vlc-bridge.log"
RADIO_BRIDGE_LOG="$LOG_ROOT/radio-bridge.log"

usage() {
    cat <<'EOF'
Usage: scripts/first-listen-lab.sh COMMAND

Disposable local Home Assistant + real Mac speaker lab for First Listen.

Commands:
  start             Start/reuse the isolated HA, VLC speaker, and branch radio
  set-ha-token      Securely save a local-lab HA long-lived access token
  show-setup        Show one-time local HA integration values (local secrets)
  sync-integration  Copy this branch's HA integration into the lab; never restart HA
  replay            Reopen First Listen while preserving radio caches and HA
  fresh-radio       Archive all radio state for a genuine fresh-radio test; keep HA
  status            Show redacted lab status and URLs
  stop              Stop owned lab processes/profile while preserving all state
  -h, --help        Show this help

Typical daily test:
  scripts/first-listen-lab.sh replay
  open http://127.0.0.1:8000/admin

True fresh-radio test (HA onboarding and speaker remain configured):
  scripts/first-listen-lab.sh fresh-radio
EOF
}

note() {
    printf '[first-listen-lab] %s\n' "$*"
}

die() {
    printf '[first-listen-lab] ERROR: %s\n' "$*" >&2
    exit 1
}

assert_safe_lab_root() {
    case "$LAB_ROOT" in
        "$ROOT"/tmp/first-listen-ha-lab) ;;
        *) die "unsafe lab root: $LAB_ROOT" ;;
    esac

    # Checking only LAB_ROOT's string is not enough: mkdir/chmod/bind mounts all
    # follow symlinks.  Refuse every fixed directory before any stateful command
    # can adopt or mutate a path outside this checkout's disposable tree.
    local path
    for path in \
        "$ROOT/tmp" \
        "$LAB_ROOT" \
        "$HA_CONFIG" \
        "$RADIO_CACHE" \
        "$RADIO_TMP" \
        "$RADIO_MUSIC" \
        "$RADIO_RUNTIME" \
        "$VLC_RUNTIME" \
        "$BRIDGE_RUNTIME" \
        "$ARCHIVE_ROOT" \
        "$LOG_ROOT" \
        "$COMPONENT_BACKUP_ROOT"
    do
        assert_existing_directory_path "$path"
    done

    for path in \
        "$ENV_FILE" \
        "$LAB_MARKER" \
        "$HA_CONTAINER_MIGRATION_MARKER" \
        "$HA_CONFIG/configuration.yaml" \
        "$RADIO_ENV_FILE" \
        "$RADIO_CONFIG_FILE" \
        "$RADIO_MODEL_REGISTRY_FILE" \
        "$LOCAL_TRACK" \
        "$RADIO_PID_FILE" \
        "$VLC_PID_FILE" \
        "$SSH_TUNNEL_PID_FILE" \
        "$VLC_BRIDGE_PID_FILE" \
        "$RADIO_BRIDGE_PID_FILE" \
        "$BRIDGE_MARKER" \
        "$RADIO_LOG" \
        "$VLC_LOG" \
        "$SSH_TUNNEL_LOG" \
        "$VLC_BRIDGE_LOG" \
        "$RADIO_BRIDGE_LOG"
    do
        assert_safe_regular_file_path "$path"
    done

    assert_ha_config_ownership
}

path_exists() {
    [ -e "$1" ] || [ -L "$1" ]
}

assert_existing_directory_path() {
    local path physical
    path="$1"
    [ ! -L "$path" ] || die "refusing symbolic-link lab path: $path"
    if [ -e "$path" ]; then
        [ -d "$path" ] || die "lab directory path is not a directory: $path"
        physical="$(cd "$path" 2>/dev/null && pwd -P)" || die "cannot resolve lab directory: $path"
        [ "$physical" = "$path" ] || die "lab directory resolves outside its fixed path: $path -> $physical"
    fi
}

assert_safe_regular_file_path() {
    local path
    path="$1"
    [ ! -L "$path" ] || die "refusing symbolic-link lab file: $path"
    if [ -e "$path" ]; then
        [ -f "$path" ] || die "lab file path is not a regular file: $path"
    fi
}

directory_is_empty() {
    local directory entry
    directory="$1"
    [ -r "$directory" ] && [ -x "$directory" ] || die "cannot inspect HA config ownership: $directory"
    for entry in "$directory"/* "$directory"/.[!.]* "$directory"/..?*; do
        if path_exists "$entry"; then
            return 1
        fi
    done
    return 0
}

assert_ha_config_ownership() {
    [ -d "$HA_CONFIG" ] || return 0
    if [ -f "$LAB_MARKER" ]; then
        return 0
    fi
    directory_is_empty "$HA_CONFIG" || die \
        "refusing nonempty unmarked HA config: $HA_CONFIG (move it aside or add the lab marker deliberately)"
}

ensure_directory() {
    local path mode
    path="$1"
    mode="$2"
    if [ ! -d "$path" ]; then
        mkdir "$path"
    fi
    assert_existing_directory_path "$path"
    chmod "$mode" "$path"
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "$1 is required"
}

docker_binary() {
    if command -v docker >/dev/null 2>&1; then
        command -v docker
    elif [ -x /opt/homebrew/opt/docker/bin/docker ]; then
        printf '%s\n' /opt/homebrew/opt/docker/bin/docker
    else
        return 1
    fi
}

docker_lab() {
    local binary
    binary="$(docker_binary)" || die "Docker CLI is required (brew install docker)"
    "$binary" --context "$DOCKER_CONTEXT" "$@"
}

ensure_lab_dirs() {
    assert_safe_lab_root
    # ROOT is already physical (pwd -P). Create one component at a time so no
    # mkdir -p operation can traverse a path that was not checked above.
    ensure_directory "$ROOT/tmp" 700
    ensure_directory "$LAB_ROOT" 700
    ensure_directory "$HA_CONFIG" 700
    ensure_directory "$RADIO_CACHE" 700
    ensure_directory "$RADIO_TMP" 700
    ensure_directory "$RADIO_MUSIC" 700
    ensure_directory "$RADIO_RUNTIME" 700
    ensure_directory "$VLC_RUNTIME" 700
    ensure_directory "$BRIDGE_RUNTIME" 700
    ensure_directory "$ARCHIVE_ROOT" 700
    ensure_directory "$LOG_ROOT" 700
    ensure_directory "$COMPONENT_BACKUP_ROOT" 700
    # Home Assistant writes integration credentials below .storage with its
    # normal file modes. Keep the entire disposable tree non-traversable by
    # other local accounts even when those child files are 0644.
    chmod 700 "$LAB_ROOT" "$HA_CONFIG" "$RADIO_CACHE" "$RADIO_TMP" \
        "$RADIO_MUSIC" "$RADIO_RUNTIME" "$VLC_RUNTIME" "$BRIDGE_RUNTIME" "$ARCHIVE_ROOT" \
        "$LOG_ROOT" "$COMPONENT_BACKUP_ROOT"
    if [ ! -f "$LAB_MARKER" ]; then
        # The preflight above allows this only for a new/empty HA_CONFIG.
        printf '%s\n' "$LAB_MARKER_TEXT" > "$LAB_MARKER"
    fi
    chmod 600 "$LAB_MARKER"
    if [ ! -f "$HA_CONFIG/configuration.yaml" ]; then
        cat > "$HA_CONFIG/configuration.yaml" <<'EOF'
default_config:

logger:
  default: info
  logs:
    custom_components.mammamiradio: debug
EOF
    fi
    chmod 600 "$HA_CONFIG/configuration.yaml"
}

random_hex() {
    require_command openssl
    openssl rand -hex 24
}

ensure_env_file() {
    ensure_lab_dirs
    if [ ! -f "$ENV_FILE" ]; then
        local admin_token vlc_password
        admin_token="$(random_hex)"
        vlc_password="$(random_hex)"
        umask 077
        {
            printf 'HA_TOKEN=\n'
            printf 'VLC_TELNET_PASSWORD=%s\n' "$vlc_password"
            printf 'ADMIN_TOKEN=%s\n' "$admin_token"
        } > "$ENV_FILE"
    fi
    chmod 600 "$ENV_FILE"
}

lab_env_get() {
    local wanted line
    wanted="$1"
    [ -f "$ENV_FILE" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            "$wanted"=*) printf '%s\n' "${line#*=}"; return 0 ;;
        esac
    done < "$ENV_FILE"
}

set_lab_env_value() {
    local wanted value line found tmp_file
    wanted="$1"
    value="$2"
    case "$value" in
        *$'\n'*|*$'\r'*) die "$wanted cannot contain a newline" ;;
    esac
    tmp_file="$LAB_ROOT/.lab.env.$$"
    path_exists "$tmp_file" && die "temporary credential path already exists: $tmp_file"
    found=0
    umask 077
    : > "$tmp_file"
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            "$wanted"=*)
                printf '%s=%s\n' "$wanted" "$value" >> "$tmp_file"
                found=1
                ;;
            *) printf '%s\n' "$line" >> "$tmp_file" ;;
        esac
    done < "$ENV_FILE"
    if [ "$found" -eq 0 ]; then
        printf '%s=%s\n' "$wanted" "$value" >> "$tmp_file"
    fi
    chmod 600 "$tmp_file"
    mv "$tmp_file" "$ENV_FILE"
}

require_lab_value() {
    local key value
    key="$1"
    value="$(lab_env_get "$key")"
    [ -n "$value" ] || die "$key is missing; run scripts/first-listen-lab.sh $2"
    printf '%s\n' "$value"
}

ensure_local_track() {
    local track source
    track="$LOCAL_TRACK"
    source="$FIRST_LISTEN_SHOW"
    assert_safe_regular_file_path "$track"
    assert_safe_regular_file_path "$source"
    [ -s "$source" ] || die "packaged First Listen mini-show is missing: $source"
    if [ -s "$track" ] && cmp -s "$source" "$track"; then
        return 0
    fi
    note "Installing the packaged First Listen mini-show as local lab music"
    cp "$source" "$track"
    chmod 600 "$track"
}

colima_is_running() {
    command -v colima >/dev/null 2>&1 || return 1
    colima status --profile "$COLIMA_PROFILE" >/dev/null 2>&1
}

start_colima() {
    require_command colima
    if colima_is_running; then
        return 0
    fi
    note "Starting isolated Colima profile $COLIMA_PROFILE"
    colima start --profile "$COLIMA_PROFILE" --activate=false \
        --cpus 4 --memory 6 --disk 40 --runtime docker --arch aarch64
}

validate_docker_gateway() {
    local subnet gateway
    subnet="$(docker_lab network inspect bridge --format '{{(index .IPAM.Config 0).Subnet}}')"
    gateway="$(docker_lab network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')"
    [ "$subnet" = "172.17.0.0/16" ] || die \
        "isolated Docker bridge uses unexpected subnet $subnet; refusing to guess an HA integration host"
    [ "$gateway" = "$DOCKER_GATEWAY_IP" ] || die \
        "isolated Docker bridge uses unexpected gateway $gateway; refusing to guess an HA integration host"
}

container_exists() {
    docker_lab inspect "$HA_CONTAINER" >/dev/null 2>&1
}

validate_ha_container() {
    local image restart_policy config_mount label
    image="$(docker_lab inspect --format '{{.Config.Image}}' "$HA_CONTAINER")"
    restart_policy="$(docker_lab inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$HA_CONTAINER")"
    config_mount="$(docker_lab inspect --format '{{range .Mounts}}{{if eq .Destination "/config"}}{{.Source}}{{end}}{{end}}' "$HA_CONTAINER")"
    label="$(docker_lab inspect --format '{{index .Config.Labels "mammamiradio.first-listen-lab"}}' "$HA_CONTAINER")"

    [ "$image" = "$HA_IMAGE" ] || die "$HA_CONTAINER uses unexpected image $image"
    [ "$restart_policy" = "no" ] || die "$HA_CONTAINER has unexpected restart policy $restart_policy"
    [ "$config_mount" = "$HA_CONFIG" ] || die "$HA_CONTAINER is not mounted from the disposable lab config"
    if [ "$label" != "true" ]; then
        # Compatibility for the lab created during initial feature acceptance.
        # Exact image + mount + restart policy above form its ownership proof.
        note "Using verified legacy lab container (it will gain the ownership label only if recreated later)"
    fi
}

ha_container_running() {
    [ "$(docker_lab inspect --format '{{.State.Running}}' "$HA_CONTAINER" 2>/dev/null || true)" = "true" ]
}

ha_container_id() {
    docker_lab inspect --format '{{.Id}}' "$HA_CONTAINER"
}

ha_container_port_bindings() {
    docker_lab inspect --format \
        '{{range (index .HostConfig.PortBindings "8123/tcp")}}{{.HostIp}}:{{.HostPort}}{{println}}{{end}}' \
        "$HA_CONTAINER"
}

ha_container_is_loopback_only() {
    [ "$(ha_container_port_bindings)" = "127.0.0.1:8123" ]
}

authorize_ha_container_loopback_migration() {
    local container_id marker_tmp
    container_id="$(ha_container_id)"
    [ -n "$container_id" ] || die "cannot identify the verified HA lab container"
    marker_tmp="$LAB_ROOT/.ha-container-loopback-migration.$$"
    assert_safe_regular_file_path "$HA_CONTAINER_MIGRATION_MARKER"
    assert_safe_regular_file_path "$marker_tmp"
    path_exists "$marker_tmp" && die "temporary HA migration marker already exists: $marker_tmp"
    printf '%s\n' "$container_id" > "$marker_tmp"
    chmod 600 "$marker_tmp"
    mv "$marker_tmp" "$HA_CONTAINER_MIGRATION_MARKER"
}

ha_container_loopback_migration_id() {
    local expected_id actual_id
    [ -f "$HA_CONTAINER_MIGRATION_MARKER" ] || return 1
    IFS= read -r expected_id < "$HA_CONTAINER_MIGRATION_MARKER" || return 1
    actual_id="$(ha_container_id)" || return 1
    [ -n "$expected_id" ] && [ "$expected_id" = "$actual_id" ] || return 1
    printf '%s\n' "$actual_id"
}

ha_container_loopback_migration_authorized() {
    ha_container_loopback_migration_id >/dev/null
}

create_loopback_ha_container() {
    note "Creating loopback-only HA Container $HA_CONTAINER"
    docker_lab run -d --name "$HA_CONTAINER" --label "$HA_LABEL" \
        --restart=no -e TZ="${TZ:-Europe/Berlin}" \
        -p 127.0.0.1:8123:8123 -v "$HA_CONFIG:/config" "$HA_IMAGE" >/dev/null
    rm -f "$HA_CONTAINER_MIGRATION_MARKER"
}

stage_integration() {
    local source destination custom_components_root stage backup stamp
    source="$ROOT/custom_components/mammamiradio"
    custom_components_root="$HA_CONFIG/custom_components"
    destination="$custom_components_root/mammamiradio"
    [ -d "$source" ] || die "branch integration missing: $source"
    ensure_lab_dirs
    assert_existing_directory_path "$custom_components_root"
    [ ! -L "$destination" ] || die "refusing symbolic-link integration destination: $destination"
    if [ -e "$destination" ]; then
        [ -d "$destination" ] || die "integration destination is not a directory: $destination"
    fi
    stamp="$(date '+%Y%m%d-%H%M%S')"
    stage="$LAB_ROOT/.component-stage-$$"
    backup="$COMPONENT_BACKUP_ROOT/mammamiradio-$stamp"
    path_exists "$stage" && die "integration staging path already exists: $stage"
    mkdir "$stage"
    assert_existing_directory_path "$stage"
    (cd "$source" && tar --exclude='./__pycache__' --exclude='*.pyc' -cf - .) | (cd "$stage" && tar -xf -)
    if [ -d "$destination" ]; then
        while path_exists "$backup"; do
            backup="$backup-1"
        done
        mv "$destination" "$backup"
    fi
    ensure_directory "$custom_components_root" 700
    mv "$stage" "$destination"
    note "Staged this branch's custom integration in the disposable HA config"
}

start_ha() {
    local legacy_container_id
    if container_exists; then
        validate_ha_container
        if ! ha_container_is_loopback_only; then
            if ha_container_running; then
                die "verified legacy HA lab container still publishes beyond loopback; run an explicit lab stop, then start"
            fi
            legacy_container_id="$(ha_container_loopback_migration_id)" || die \
                "verified legacy HA lab container is stopped but migration was not authorized; run an explicit lab stop, then start"
            note "Recreating the stopped verified HA lab container with loopback-only port publishing; /config is preserved"
            # Deliberately no --force: Docker must refuse if another actor starts
            # the container between the running-state proof and this removal.
            docker_lab rm "$legacy_container_id" >/dev/null
            create_loopback_ha_container
            return 0
        else
            rm -f "$HA_CONTAINER_MIGRATION_MARKER"
        fi
        if ! ha_container_running; then
            note "Starting preserved HA lab container (not restarting it)"
            docker_lab start "$HA_CONTAINER" >/dev/null
        fi
    else
        stage_integration
        create_loopback_ha_container
    fi
}

port_pid() {
    local port
    port="$1"
    command -v lsof >/dev/null 2>&1 || return 0
    # lsof returns 1 when nothing is listening. Empty is an expected result,
    # not a set -e failure for status/start/reset callers.
    lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u | head -n 1 || true
}

pid_cwd() {
    local pid
    pid="$1"
    lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
}

pid_command() {
    ps -ww -p "$1" -o command= 2>/dev/null || true
}

managed_radio_pid() {
    local pid command cwd listener
    [ -f "$RADIO_PID_FILE" ] || return 1
    pid="$(sed -n '1p' "$RADIO_PID_FILE")"
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$pid" 2>/dev/null || return 1
    command="$(pid_command "$pid")"
    cwd="$(pid_cwd "$pid")"
    listener="$(port_pid "$RADIO_PORT")"
    [ "$cwd" = "$RADIO_RUNTIME" ] || return 1
    [ "$listener" = "$pid" ] || return 1
    case "$command" in
        *"uvicorn mammamiradio.main:app"*"--port $RADIO_PORT"*) printf '%s\n' "$pid" ;;
        *) return 1 ;;
    esac
}

managed_vlc_pid() {
    local pid command cwd listener
    [ -f "$VLC_PID_FILE" ] || return 1
    pid="$(sed -n '1p' "$VLC_PID_FILE")"
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$pid" 2>/dev/null || return 1
    command="$(pid_command "$pid")"
    cwd="$(pid_cwd "$pid")"
    listener="$(port_pid "$VLC_PORT")"
    [ "$cwd" = "$VLC_RUNTIME" ] || return 1
    [ "$listener" = "$pid" ] || return 1
    case "$command" in
        *"VLC"*"--telnet-port $VLC_PORT"*) printf '%s\n' "$pid" ;;
        *) return 1 ;;
    esac
}

pid_listens_on_loopback_only() {
    local pid port listeners
    pid="$1"
    port="$2"
    listeners="$(lsof -a -nP -p "$pid" -iTCP:"$port" -sTCP:LISTEN -Fn 2>/dev/null |
        sed -n 's/^n//p')"
    [ "$listeners" = "127.0.0.1:$port" ]
}

managed_secure_radio_pid() {
    local pid
    pid="$(managed_radio_pid)" || return 1
    pid_listens_on_loopback_only "$pid" "$RADIO_PORT" || return 1
    printf '%s\n' "$pid"
}

managed_secure_vlc_pid() {
    local pid
    pid="$(managed_vlc_pid)" || return 1
    pid_listens_on_loopback_only "$pid" "$VLC_PORT" || return 1
    printf '%s\n' "$pid"
}

refuse_legacy_lab_start() {
    local pid
    if pid="$(managed_radio_pid 2>/dev/null)" &&
        ! pid_listens_on_loopback_only "$pid" "$RADIO_PORT"
    then
        die "owned legacy radio is still listening beyond loopback (pid $pid); run an explicit lab stop, then start, then update both LOCAL HA integration hosts to $DOCKER_GATEWAY_IP"
    fi
    if pid="$(managed_vlc_pid 2>/dev/null)" &&
        ! pid_listens_on_loopback_only "$pid" "$VLC_PORT"
    then
        die "owned legacy VLC is still listening beyond loopback (pid $pid); run an explicit lab stop, then start, then update both LOCAL HA integration hosts to $DOCKER_GATEWAY_IP"
    fi
}

colima_ssh() {
    [ -f "$COLIMA_SSH_CONFIG" ] || die \
        "isolated Colima SSH config is missing: $COLIMA_SSH_CONFIG"
    "$SSH_BINARY" -F "$COLIMA_SSH_CONFIG" -S none \
        -o ControlMaster=no -o ControlPersist=no -o ControlPath=none \
        -o ProxyCommand=none -o ProxyJump=none -o PermitLocalCommand=no \
        -o BatchMode=yes -o ConnectTimeout=5 \
        "$COLIMA_SSH_HOST" "$@"
}

validate_colima_ssh_config() {
    local resolved hostname batch_mode control_master control_persist control_path
    [ -x "$SSH_BINARY" ] || die "macOS OpenSSH is required at $SSH_BINARY"
    [ -f "$COLIMA_SSH_CONFIG" ] || die \
        "isolated Colima SSH config is missing: $COLIMA_SSH_CONFIG"
    resolved="$("$SSH_BINARY" -G -F "$COLIMA_SSH_CONFIG" -S none \
        -o ControlMaster=no -o ControlPersist=no -o ControlPath=none \
        -o ProxyCommand=none -o ProxyJump=none -o PermitLocalCommand=no \
        -o BatchMode=yes "$COLIMA_SSH_HOST" 2>/dev/null)" || die \
        "could not resolve the isolated Colima SSH configuration"
    hostname="$(printf '%s\n' "$resolved" | awk '$1 == "hostname" { print $2; exit }')"
    batch_mode="$(printf '%s\n' "$resolved" | awk '$1 == "batchmode" { print $2; exit }')"
    control_master="$(printf '%s\n' "$resolved" | awk '$1 == "controlmaster" { print $2; exit }')"
    control_persist="$(printf '%s\n' "$resolved" | awk '$1 == "controlpersist" { print $2; exit }')"
    control_path="$(printf '%s\n' "$resolved" | awk '$1 == "controlpath" { print $2; exit }')"
    [ "$hostname" = "127.0.0.1" ] || die \
        "isolated Colima SSH host resolved outside loopback: $hostname"
    [ "$batch_mode" = "yes" ] || die "isolated Colima SSH must use batch mode"
    case "$control_master" in no|false) ;; *) die "isolated Colima SSH multiplexing is still enabled" ;; esac
    [ "$control_persist" = "no" ] || die "isolated Colima SSH persistence is still enabled"
    # OpenSSH omits `controlpath` from `ssh -G` when `-S none` disables the
    # socket. Other supported builds render the same effective state as
    # `controlpath none`. Both are safe after the master/persist checks above.
    case "$control_path" in ''|none) ;; *) die "isolated Colima SSH control path is still enabled" ;; esac
}

ensure_remote_bridge_tools() {
    if colima_ssh sh -s <<'EOF'
command -v socat >/dev/null 2>&1
command -v stat >/dev/null 2>&1
command -v setsid >/dev/null 2>&1
command -v ps >/dev/null 2>&1
EOF
    then
        return 0
    fi
    die "the isolated Colima VM needs socat for its private bridge; see the one-time prerequisite in docs/runbooks/first-listen-local-ha.md"
}

write_owned_pid_file() {
    local pid_file pid
    pid_file="$1"
    pid="$2"
    case "$pid" in ''|*[!0-9]*) die "refusing invalid owned process id: $pid" ;; esac
    assert_safe_regular_file_path "$pid_file"
    printf '%s\n' "$pid" > "$pid_file"
    chmod 600 "$pid_file"
}

remove_stale_pid_file_or_refuse_live_mismatch() {
    local pid_file kind pid
    pid_file="$1"
    kind="$2"
    [ -f "$pid_file" ] || return 0
    pid="$(sed -n '1p' "$pid_file")"
    case "$pid" in
        ''|*[!0-9]*) ;;
        *)
            if kill -0 "$pid" 2>/dev/null; then
                die "$kind PID $pid is live but fails the exact ownership check; refusing to erase its evidence or signal it"
            fi
            ;;
    esac
    rm -f "$pid_file"
}

command_contains_all() {
    local command expected
    command="$1"
    shift
    for expected in "$@"; do
        case "$command" in
            *"$expected"*) ;;
            *) return 1 ;;
        esac
    done
}

remote_gateway_bridge_pid_file() {
    local listen_port target_port
    listen_port="$1"
    target_port="$2"
    case "$listen_port:$target_port" in
        "$VLC_PORT:$VLC_TUNNEL_PORT") printf '%s\n' "$REMOTE_VLC_BRIDGE_PID_FILE" ;;
        "$RADIO_PORT:$RADIO_TUNNEL_PORT") printf '%s\n' "$REMOTE_RADIO_BRIDGE_PID_FILE" ;;
        *) return 1 ;;
    esac
}

remote_gateway_bridge_control() {
    local action listen_port target_port remote_pid_file
    action="$1"
    listen_port="$2"
    target_port="$3"
    remote_pid_file="$(remote_gateway_bridge_pid_file "$listen_port" "$target_port")" || return 1
    colima_ssh sh -s -- \
        "$action" "$REMOTE_BRIDGE_RUNTIME" "$remote_pid_file" \
        "$listen_port" "$DOCKER_GATEWAY_IP" "$target_port" <<'EOF'
set -eu
action="$1"
runtime="$2"
pid_file="$3"
listen_port="$4"
gateway="$5"
target_port="$6"

fail() {
    printf '%s\n' "remote gateway relay ownership check failed: $*" >&2
    exit 1
}

[ ! -L "$runtime" ] || fail "runtime is a symbolic link"
[ -d "$runtime" ] || fail "runtime is missing"
[ "$(stat -c '%u:%a' "$runtime")" = "$(id -u):700" ] || fail "runtime owner or mode changed"
[ ! -L "$pid_file" ] || fail "PID evidence is a symbolic link"
[ -f "$pid_file" ] || fail "PID evidence is missing"
[ "$(stat -c '%u:%a' "$pid_file")" = "$(id -u):600" ] || fail "PID evidence owner or mode changed"

pid="$(sed -n '1p' "$pid_file")"
case "$pid" in ''|*[!0-9]*) fail "PID evidence is invalid" ;; esac
[ -r "/proc/$pid/cmdline" ] || fail "owned PID $pid is not live"
listen_arg="TCP4-LISTEN:$listen_port,bind=$gateway,reuseaddr,fork,max-children=32,range=172.17.0.0/16"
target_arg="TCP4-CONNECT:127.0.0.1:$target_port,connect-timeout=3"
actual="$(tr '\000' '\n' < "/proc/$pid/cmdline")"
expected="$(printf 'socat\n%s\n%s\n' "$listen_arg" "$target_arg")"
[ "$actual" = "$expected" ] || fail "PID $pid command or ports changed"
pgid="$(ps -o pgid= -p "$pid" | tr -d ' ')"
[ "$pgid" = "$pid" ] || fail "PID $pid is not the owned relay process-group leader"

case "$action" in
    status)
        printf '%s\n' "$pid"
        ;;
    stop)
        if ! kill -TERM "-$pid" 2>/dev/null; then
            kill -0 "-$pid" 2>/dev/null && fail "owned process group $pid could not be signalled"
        fi
        attempts=0
        while kill -0 "-$pid" 2>/dev/null && [ "$attempts" -lt 40 ]; do
            attempts=$((attempts + 1))
            sleep 0.25
        done
        if kill -0 "-$pid" 2>/dev/null; then
            fail "owned process group $pid survived TERM; PID evidence was preserved"
        fi
        rm -f "$pid_file"
        ;;
    *) fail "unsupported action $action" ;;
esac
EOF
}

managed_remote_gateway_bridge_pid() {
    remote_gateway_bridge_control status "$1" "$2"
}

stop_remote_gateway_bridge_if_managed() {
    remote_gateway_bridge_control stop "$1" "$2"
}

remove_stale_remote_pid_file_or_refuse_live_mismatch() {
    local listen_port target_port remote_pid_file
    listen_port="$1"
    target_port="$2"
    remote_pid_file="$(remote_gateway_bridge_pid_file "$listen_port" "$target_port")" || return 1
    colima_ssh sh -s -- "$REMOTE_BRIDGE_RUNTIME" "$remote_pid_file" <<'EOF'
set -eu
runtime="$1"
pid_file="$2"
[ ! -L "$runtime" ] || {
    printf '%s\n' "remote gateway relay runtime is a symbolic link; refusing cleanup" >&2
    exit 1
}
[ -e "$runtime" ] || exit 0
[ -d "$runtime" ] || {
    printf '%s\n' "remote gateway relay runtime is not a directory; refusing cleanup" >&2
    exit 1
}
[ "$(stat -c '%u:%a' "$runtime")" = "$(id -u):700" ] || {
    printf '%s\n' "remote gateway relay runtime owner or mode changed; refusing cleanup" >&2
    exit 1
}
[ ! -L "$pid_file" ] || {
    printf '%s\n' "remote gateway relay PID evidence is a symbolic link; refusing cleanup" >&2
    exit 1
}
[ -e "$pid_file" ] || exit 0
[ -f "$pid_file" ] || {
    printf '%s\n' "remote gateway relay PID evidence is not a file; refusing cleanup" >&2
    exit 1
}
pid="$(sed -n '1p' "$pid_file")"
case "$pid" in
    ''|*[!0-9]*) ;;
    *)
        if kill -0 "$pid" 2>/dev/null; then
            printf '%s\n' "remote gateway relay PID $pid is live but lacks matching local and remote ownership proof; refusing to erase its evidence or signal it" >&2
            exit 1
        fi
        ;;
esac
rm -f "$pid_file"
EOF
}

managed_ssh_tunnel_pid() {
    local pid command cwd
    [ -f "$SSH_TUNNEL_PID_FILE" ] || return 1
    pid="$(sed -n '1p' "$SSH_TUNNEL_PID_FILE")"
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$pid" 2>/dev/null || return 1
    command="$(pid_command "$pid")"
    cwd="$(pid_cwd "$pid")"
    [ "$cwd" = "$BRIDGE_RUNTIME" ] || return 1
    command_contains_all "$command" \
        "$SSH_BINARY" "-F $COLIMA_SSH_CONFIG" "-S none" \
        "ControlMaster=no" "ControlPersist=no" "ControlPath=none" \
        "ProxyCommand=none" "ProxyJump=none" "PermitLocalCommand=no" \
        "ExitOnForwardFailure=yes" \
        "-R 127.0.0.1:$VLC_TUNNEL_PORT:127.0.0.1:$VLC_PORT" \
        "-R 127.0.0.1:$RADIO_TUNNEL_PORT:127.0.0.1:$RADIO_PORT" \
        "-N" "-T" "$COLIMA_SSH_HOST" || return 1
    printf '%s\n' "$pid"
}

managed_gateway_bridge_wrapper_pid() {
    local pid_file listen_port target_port pid command cwd
    pid_file="$1"
    listen_port="$2"
    target_port="$3"
    [ -f "$pid_file" ] || return 1
    pid="$(sed -n '1p' "$pid_file")"
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$pid" 2>/dev/null || return 1
    command="$(pid_command "$pid")"
    cwd="$(pid_cwd "$pid")"
    [ "$cwd" = "$BRIDGE_RUNTIME" ] || return 1
    command_contains_all "$command" \
        "$SSH_BINARY" "-F $COLIMA_SSH_CONFIG" "-S none" \
        "ControlMaster=no" "ControlPersist=no" "ControlPath=none" \
        "ProxyCommand=none" "ProxyJump=none" "PermitLocalCommand=no" \
        "$COLIMA_SSH_HOST" "sh -s --" "$REMOTE_BRIDGE_RUNTIME" \
        "$(remote_gateway_bridge_pid_file "$listen_port" "$target_port")" \
        "$listen_port" "$DOCKER_GATEWAY_IP" "$target_port" || return 1
    printf '%s\n' "$pid"
}

managed_gateway_bridge_pid() {
    local pid
    pid="$(managed_gateway_bridge_wrapper_pid "$1" "$2" "$3")" || return 1
    managed_remote_gateway_bridge_pid "$2" "$3" >/dev/null || return 1
    printf '%s\n' "$pid"
}

start_ssh_tunnel() {
    local pid attempts
    if pid="$(managed_ssh_tunnel_pid 2>/dev/null)"; then
        note "Private Colima tunnel already running (pid $pid)"
        return 0
    fi
    remove_stale_pid_file_or_refuse_live_mismatch \
        "$SSH_TUNNEL_PID_FILE" "private SSH tunnel"
    : > "$SSH_TUNNEL_LOG"
    chmod 600 "$SSH_TUNNEL_LOG"
    (
        cd "$BRIDGE_RUNTIME"
        exec nohup "$SSH_BINARY" -F "$COLIMA_SSH_CONFIG" -S none \
            -o ControlMaster=no -o ControlPersist=no -o ControlPath=none \
            -o ProxyCommand=none -o ProxyJump=none -o PermitLocalCommand=no \
            -o BatchMode=yes -o ConnectTimeout=5 \
            -o ExitOnForwardFailure=yes \
            -o ServerAliveInterval=15 -o ServerAliveCountMax=2 \
            -R "127.0.0.1:$VLC_TUNNEL_PORT:127.0.0.1:$VLC_PORT" \
            -R "127.0.0.1:$RADIO_TUNNEL_PORT:127.0.0.1:$RADIO_PORT" \
            -N -T "$COLIMA_SSH_HOST"
    ) >> "$SSH_TUNNEL_LOG" 2>&1 &
    pid=$!
    write_owned_pid_file "$SSH_TUNNEL_PID_FILE" "$pid"
    BRIDGE_STARTED_TUNNEL=1
    attempts=0
    while [ "$attempts" -lt 20 ]; do
        kill -0 "$pid" 2>/dev/null || die \
            "private tunnel refused a guest-port collision or failed; see $SSH_TUNNEL_LOG"
        attempts=$((attempts + 1))
        sleep 0.1
    done
    managed_ssh_tunnel_pid >/dev/null || die "private tunnel process identity check failed"
}

start_gateway_bridge() {
    local pid_file log_file kind listen_port target_port remote_pid_file pid attempts
    pid_file="$1"
    log_file="$2"
    kind="$3"
    listen_port="$4"
    target_port="$5"
    remote_pid_file="$(remote_gateway_bridge_pid_file "$listen_port" "$target_port")" || die \
        "$kind gateway relay uses an unexpected port pair"
    if pid="$(managed_gateway_bridge_pid "$pid_file" "$listen_port" "$target_port" 2>/dev/null)"; then
        note "$kind gateway relay already running (pid $pid)"
        return 0
    fi
    remove_stale_pid_file_or_refuse_live_mismatch "$pid_file" "$kind gateway relay"
    remove_stale_remote_pid_file_or_refuse_live_mismatch "$listen_port" "$target_port"
    : > "$log_file"
    chmod 600 "$log_file"
    (
        cd "$BRIDGE_RUNTIME"
        exec nohup "$SSH_BINARY" -F "$COLIMA_SSH_CONFIG" -S none \
            -o ControlMaster=no -o ControlPersist=no -o ControlPath=none \
            -o ProxyCommand=none -o ProxyJump=none -o PermitLocalCommand=no \
            -o BatchMode=yes -o ConnectTimeout=5 \
            -o ServerAliveInterval=15 -o ServerAliveCountMax=2 \
            -T "$COLIMA_SSH_HOST" sh -s -- \
            "$REMOTE_BRIDGE_RUNTIME" "$remote_pid_file" \
            "$listen_port" "$DOCKER_GATEWAY_IP" "$target_port" <<'EOF'
set -eu
runtime="$1"
pid_file="$2"
listen_port="$3"
gateway="$4"
target_port="$5"

[ ! -L "$runtime" ] || {
    printf '%s\n' "remote gateway relay runtime is a symbolic link" >&2
    exit 1
}
if [ ! -e "$runtime" ]; then
    umask 077
    mkdir "$runtime"
    chmod 700 "$runtime"
fi
[ -d "$runtime" ] || {
    printf '%s\n' "remote gateway relay runtime is not a directory" >&2
    exit 1
}
[ "$(stat -c '%u:%a' "$runtime")" = "$(id -u):700" ] || {
    printf '%s\n' "remote gateway relay runtime owner or mode changed" >&2
    exit 1
}
[ ! -L "$pid_file" ] || {
    printf '%s\n' "remote gateway relay PID evidence is a symbolic link" >&2
    exit 1
}
if [ -e "$pid_file" ]; then
    [ -f "$pid_file" ] || {
        printf '%s\n' "remote gateway relay PID evidence is not a file" >&2
        exit 1
    }
    old_pid="$(sed -n '1p' "$pid_file")"
    case "$old_pid" in
        ''|*[!0-9]*) ;;
        *)
            if kill -0 "$old_pid" 2>/dev/null; then
                printf '%s\n' "remote gateway relay PID $old_pid is already live; refusing replacement" >&2
                exit 1
            fi
            ;;
    esac
    rm -f "$pid_file"
fi
tmp_pid_file="$runtime/.relay-pid.$$"
[ ! -e "$tmp_pid_file" ] && [ ! -L "$tmp_pid_file" ] || {
    printf '%s\n' "remote gateway relay temporary PID path already exists" >&2
    exit 1
}
setsid socat \
    "TCP4-LISTEN:$listen_port,bind=$gateway,reuseaddr,fork,max-children=32,range=172.17.0.0/16" \
    "TCP4-CONNECT:127.0.0.1:$target_port,connect-timeout=3" &
relay_pid=$!
umask 077
printf '%s\n' "$relay_pid" > "$tmp_pid_file"
chmod 600 "$tmp_pid_file"
mv "$tmp_pid_file" "$pid_file"
result=0
wait "$relay_pid" || result=$?
if [ -f "$pid_file" ] && [ "$(sed -n '1p' "$pid_file")" = "$relay_pid" ]; then
    rm -f "$pid_file"
fi
exit "$result"
EOF
    ) >> "$log_file" 2>&1 &
    pid=$!
    write_owned_pid_file "$pid_file" "$pid"
    case "$kind" in
        VLC) BRIDGE_STARTED_VLC_RELAY=1 ;;
        radio) BRIDGE_STARTED_RADIO_RELAY=1 ;;
    esac
    attempts=0
    while [ "$attempts" -lt 20 ]; do
        kill -0 "$pid" 2>/dev/null || die \
            "$kind gateway relay refused an occupied port or failed; see $log_file"
        attempts=$((attempts + 1))
        sleep 0.1
    done
    managed_gateway_bridge_pid "$pid_file" "$listen_port" "$target_port" >/dev/null || die \
        "$kind gateway relay process identity check failed"
}

remote_tunnel_port_free() {
    local bind_host bind_port
    bind_host="$1"
    bind_port="$2"
    if colima_ssh socat -T 1 -u OPEN:/dev/null \
        "TCP4-CONNECT:$bind_host:$bind_port" </dev/null >/dev/null 2>&1
    then
        return 1
    fi
    return 0
}

gateway_port_free_from_ha() {
    local gateway_port
    gateway_port="$1"
    docker_lab exec "$HA_CONTAINER" python3 -c \
        'import socket,sys; sock=socket.socket(); sock.settimeout(1); result=sock.connect_ex((sys.argv[1], int(sys.argv[2]))); sock.close(); raise SystemExit(0 if result else 1)' \
        "$DOCKER_GATEWAY_IP" "$gateway_port"
}

wait_gateway_port_free_from_ha() {
    local gateway_port attempts
    gateway_port="$1"
    attempts=0
    while [ "$attempts" -lt 40 ]; do
        gateway_port_free_from_ha "$gateway_port" >/dev/null 2>&1 && return 0
        attempts=$((attempts + 1))
        sleep 0.25
    done
    return 1
}

stop_gateway_bridge_if_managed() {
    local pid_file kind listen_port target_port wrapper_pid current_pid remote_pid
    pid_file="$1"
    kind="$2"
    listen_port="$3"
    target_port="$4"

    wrapper_pid=""
    if wrapper_pid="$(managed_gateway_bridge_wrapper_pid "$pid_file" "$listen_port" "$target_port" 2>/dev/null)"; then
        :
    else
        remove_stale_pid_file_or_refuse_live_mismatch "$pid_file" "$kind gateway relay"
        wrapper_pid=""
    fi

    remote_pid=""
    if remote_pid="$(managed_remote_gateway_bridge_pid "$listen_port" "$target_port" 2>/dev/null)"; then
        note "Stopping owned remote $kind gateway relay process (pid $remote_pid)"
        stop_remote_gateway_bridge_if_managed "$listen_port" "$target_port" || die \
            "$kind gateway relay survived TERM in the VM; preserving ownership evidence and refusing to stop the VM"
        if [ -n "$wrapper_pid" ]; then
            if current_pid="$(managed_gateway_bridge_wrapper_pid "$pid_file" "$listen_port" "$target_port" 2>/dev/null)"; then
                stop_managed_pid "$kind gateway relay SSH wrapper" "$pid_file" "$current_pid"
            else
                remove_stale_pid_file_or_refuse_live_mismatch \
                    "$pid_file" "$kind gateway relay SSH wrapper"
            fi
        fi
        wait_gateway_port_free_from_ha "$listen_port" || die \
            "$kind gateway relay port remained reachable after its owned remote PID stopped; refusing to stop the VM"
        return 0
    fi

    remove_stale_remote_pid_file_or_refuse_live_mismatch "$listen_port" "$target_port"
    [ -z "$wrapper_pid" ] || die \
        "$kind gateway relay SSH wrapper is owned but its remote PID evidence is missing; refusing to signal it or stop the VM"
    gateway_port_free_from_ha "$listen_port" && return 1
    die "$kind gateway port is occupied by an unmanaged process; refusing to stop the VM"
}

stop_ssh_tunnel_if_managed() {
    local pid
    if pid="$(managed_ssh_tunnel_pid 2>/dev/null)"; then
        stop_managed_pid "private SSH tunnel" "$SSH_TUNNEL_PID_FILE" "$pid"
        # Reverse forwards are owned by this exact SSH connection and cannot
        # outlive it. The bounded PID wait above is the teardown proof.
        return 0
    fi
    remove_stale_pid_file_or_refuse_live_mismatch \
        "$SSH_TUNNEL_PID_FILE" "private SSH tunnel"
    if ! remote_tunnel_port_free 127.0.0.1 "$VLC_TUNNEL_PORT" ||
        ! remote_tunnel_port_free 127.0.0.1 "$RADIO_TUNNEL_PORT"
    then
        die "guest tunnel port is occupied by an unmanaged process; refusing to stop the VM"
    fi
    return 1
}

bridge_health_from_ha() {
    local include_radio
    include_radio="$1"
    docker_lab exec -i "$HA_CONTAINER" python3 - \
        "$DOCKER_GATEWAY_IP" "$VLC_PORT" "$RADIO_PORT" "$include_radio" <<'PY'
import socket
import sys
import urllib.request

gateway, vlc_port, radio_port, include_radio = sys.argv[1:]
with socket.create_connection((gateway, int(vlc_port)), timeout=2) as vlc:
    vlc.settimeout(2)
    greeting = vlc.recv(256)
    if b"Password" not in greeting:
        raise SystemExit(1)
if include_radio == "true":
    with urllib.request.urlopen(
        f"http://{gateway}:{radio_port}/healthz", timeout=2
    ) as response:
        if response.status != 200:
            raise SystemExit(1)
PY
}

rollback_term_owned_pid() {
    local kind pid_file pid attempts
    kind="$1"
    pid_file="$2"
    pid="$3"
    note "Rolling back newly started owned $kind process (pid $pid)"
    if ! kill -TERM "$pid" 2>/dev/null; then
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$pid_file"
            return 0
        fi
        return 1
    fi
    attempts=0
    while kill -0 "$pid" 2>/dev/null && [ "$attempts" -lt 40 ]; do
        attempts=$((attempts + 1))
        sleep 0.25
    done
    if kill -0 "$pid" 2>/dev/null; then
        note "ERROR: $kind did not stop after TERM; preserving its PID proof"
        return 1
    fi
    rm -f "$pid_file"
}

rollback_new_gateway_bridge() {
    local pid_file kind listen_port target_port pid current_pid wrapper_was_live
    pid_file="$1"
    kind="$2"
    listen_port="$3"
    target_port="$4"
    [ -f "$pid_file" ] || return 0
    pid="$(sed -n '1p' "$pid_file")"
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    wrapper_was_live=0
    if kill -0 "$pid" 2>/dev/null; then
        managed_gateway_bridge_wrapper_pid \
            "$pid_file" "$listen_port" "$target_port" >/dev/null 2>&1 || return 1
        wrapper_was_live=1
    else
        rm -f "$pid_file"
    fi
    if managed_remote_gateway_bridge_pid "$listen_port" "$target_port" >/dev/null 2>&1; then
        stop_remote_gateway_bridge_if_managed "$listen_port" "$target_port" || return 1
    elif [ "$wrapper_was_live" -eq 1 ]; then
        return 1
    else
        remove_stale_remote_pid_file_or_refuse_live_mismatch \
            "$listen_port" "$target_port" || return 1
    fi
    if [ "$wrapper_was_live" -eq 1 ]; then
        if current_pid="$(managed_gateway_bridge_wrapper_pid "$pid_file" "$listen_port" "$target_port" 2>/dev/null)"; then
            rollback_term_owned_pid "$kind gateway relay SSH wrapper" "$pid_file" "$current_pid"
        else
            remove_stale_pid_file_or_refuse_live_mismatch \
                "$pid_file" "$kind gateway relay SSH wrapper"
        fi
    fi
}

rollback_new_ssh_tunnel() {
    local pid
    [ -f "$SSH_TUNNEL_PID_FILE" ] || return 0
    pid="$(sed -n '1p' "$SSH_TUNNEL_PID_FILE")"
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$SSH_TUNNEL_PID_FILE"
        return 0
    fi
    managed_ssh_tunnel_pid >/dev/null 2>&1 || return 1
    rollback_term_owned_pid "private SSH tunnel" "$SSH_TUNNEL_PID_FILE" "$pid"
}

rollback_private_bridge_on_exit() {
    local status rollback_ok
    status=$?
    trap - EXIT
    rollback_ok=1
    if [ "${BRIDGE_STARTED_RADIO_RELAY:-0}" -eq 1 ]; then
        rollback_new_gateway_bridge \
            "$RADIO_BRIDGE_PID_FILE" radio "$RADIO_PORT" "$RADIO_TUNNEL_PORT" || rollback_ok=0
    fi
    if [ "${BRIDGE_STARTED_VLC_RELAY:-0}" -eq 1 ]; then
        rollback_new_gateway_bridge \
            "$VLC_BRIDGE_PID_FILE" VLC "$VLC_PORT" "$VLC_TUNNEL_PORT" || rollback_ok=0
    fi
    if [ "${BRIDGE_STARTED_TUNNEL:-0}" -eq 1 ]; then
        rollback_new_ssh_tunnel || rollback_ok=0
    fi
    if [ "$rollback_ok" -eq 1 ] && [ "${BRIDGE_MARKER_WAS_PRESENT:-0}" -eq 0 ]; then
        rm -f "$BRIDGE_MARKER"
    fi
    exit "$status"
}

start_private_bridge() {
    local include_radio attempts
    include_radio="$1"
    BRIDGE_STARTED_TUNNEL=0
    BRIDGE_STARTED_VLC_RELAY=0
    BRIDGE_STARTED_RADIO_RELAY=0
    BRIDGE_MARKER_WAS_PRESENT=0
    [ ! -f "$BRIDGE_MARKER" ] || BRIDGE_MARKER_WAS_PRESENT=1
    trap rollback_private_bridge_on_exit EXIT
    printf '%s\n' 'Owned loopback-only First Listen bridge v1.' > "$BRIDGE_MARKER"
    chmod 600 "$BRIDGE_MARKER"
    start_ssh_tunnel
    start_gateway_bridge \
        "$VLC_BRIDGE_PID_FILE" "$VLC_BRIDGE_LOG" VLC "$VLC_PORT" "$VLC_TUNNEL_PORT"
    start_gateway_bridge \
        "$RADIO_BRIDGE_PID_FILE" "$RADIO_BRIDGE_LOG" radio "$RADIO_PORT" "$RADIO_TUNNEL_PORT"
    attempts=0
    while [ "$attempts" -lt 40 ]; do
        if bridge_health_from_ha "$include_radio" >/dev/null 2>&1; then
            note "Private HA-to-Mac gateway bridge is healthy"
            trap - EXIT
            return 0
        fi
        managed_ssh_tunnel_pid >/dev/null || die \
            "private tunnel exited during health verification; see $SSH_TUNNEL_LOG"
        attempts=$((attempts + 1))
        sleep 0.25
    done
    die "HA could not reach the loopback-only Mac endpoints through the private bridge"
}

wait_for_port() {
    local port attempts
    port="$1"
    attempts=0
    while [ "$attempts" -lt 120 ]; do
        [ -n "$(port_pid "$port")" ] && return 0
        attempts=$((attempts + 1))
        sleep 0.25
    done
    return 1
}

prepare_radio_runtime() {
    ensure_lab_dirs
    assert_safe_regular_file_path "$RADIO_CONFIG_FILE"
    assert_safe_regular_file_path "$RADIO_MODEL_REGISTRY_FILE"
    assert_safe_regular_file_path "$RADIO_ENV_FILE"
    cp "$ROOT/radio.toml" "$RADIO_CONFIG_FILE"
    cp "$ROOT/model_registry.toml" "$RADIO_MODEL_REGISTRY_FILE"
    if [ ! -f "$RADIO_ENV_FILE" ]; then
        umask 077
        : > "$RADIO_ENV_FILE"
    fi
    chmod 600 "$RADIO_ENV_FILE"
}

start_vlc() {
    local existing password pid vlc_binary
    if pid="$(managed_secure_vlc_pid 2>/dev/null)"; then
        note "VLC lab speaker already running (pid $pid)"
        return 0
    fi
    if pid="$(managed_vlc_pid 2>/dev/null)"; then
        die "owned legacy VLC is still listening beyond loopback (pid $pid); run an explicit lab stop, then start, then update both LOCAL HA integration hosts to $DOCKER_GATEWAY_IP"
    fi
    existing="$(port_pid "$VLC_PORT")"
    [ -z "$existing" ] || die "port $VLC_PORT is owned by an unmanaged process; refusing to adopt or kill it"
    vlc_binary="/Applications/VLC.app/Contents/MacOS/VLC"
    [ -x "$vlc_binary" ] || die "VLC.app is required at $vlc_binary"
    password="$(require_lab_value VLC_TELNET_PASSWORD show-setup)"
    (
        cd "$VLC_RUNTIME"
        exec nohup "$vlc_binary" --ignore-config --intf dummy --extraintf telnet \
            --telnet-host telnet://127.0.0.1 --telnet-port "$VLC_PORT" \
            --telnet-password "$password" --no-video
    ) >> "$VLC_LOG" 2>&1 &
    pid=$!
    printf '%s\n' "$pid" > "$VLC_PID_FILE"
    if ! wait_for_port "$VLC_PORT"; then
        die "VLC did not open port $VLC_PORT; see $VLC_LOG"
    fi
    managed_vlc_pid >/dev/null || die "VLC process identity check failed"
    note "VLC lab speaker ready"
}

start_radio() {
    local existing pid ha_token admin_token attempts
    if pid="$(managed_secure_radio_pid 2>/dev/null)"; then
        note "Branch radio already running (pid $pid)"
        return 0
    fi
    if pid="$(managed_radio_pid 2>/dev/null)"; then
        die "owned legacy radio is still listening beyond loopback (pid $pid); run an explicit lab stop, then start, then update both LOCAL HA integration hosts to $DOCKER_GATEWAY_IP"
    fi
    existing="$(port_pid "$RADIO_PORT")"
    [ -z "$existing" ] || die "port $RADIO_PORT is owned by an unmanaged process; refusing to adopt or kill it"
    [ -x "$ROOT/.venv/bin/python" ] || die "repo venv missing; run make install first"
    prepare_radio_runtime
    ha_token="$(require_lab_value HA_TOKEN set-ha-token)"
    admin_token="$(require_lab_value ADMIN_TOKEN show-setup)"

    (
        cd "$RADIO_RUNTIME"
        export PYTHONPATH="$ROOT"
        export MAMMAMIRADIO_BIND_HOST="127.0.0.1"
        export MAMMAMIRADIO_PORT="$RADIO_PORT"
        export MAMMAMIRADIO_CACHE_DIR="$RADIO_CACHE"
        export MAMMAMIRADIO_TMP_DIR="$RADIO_TMP"
        export MAMMAMIRADIO_MUSIC_DIR="$RADIO_MUSIC"
        export MAMMAMIRADIO_ALLOW_YTDLP="false"
        export MAMMAMIRADIO_HA_CONTEXT_ENABLED=""
        export MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH="false"
        export HA_ENABLED="true"
        # Never pair the disposable token with an editable destination. The
        # lab always talks to the exact loopback HA instance it owns.
        export HA_URL="$HA_URL_DEFAULT"
        export HA_TOKEN="$ha_token"
        export ADMIN_PASSWORD=""
        export ADMIN_TOKEN="$admin_token"
        # Explicit empty values block python-dotenv from inheriting provider
        # credentials from the repository .env. This is the real no-key path.
        export ANTHROPIC_API_KEY=""
        export OPENAI_API_KEY=""
        export AZURE_SPEECH_KEY=""
        export AZURE_SPEECH_REGION=""
        export ELEVENLABS_API_KEY=""
        export JAMENDO_CLIENT_ID=""
        export SPOTIFY_CLIENT_ID=""
        export SPOTIFY_CLIENT_SECRET=""
        exec nohup "$ROOT/.venv/bin/python" -m uvicorn mammamiradio.main:app \
            --host 127.0.0.1 --port "$RADIO_PORT"
    ) >> "$RADIO_LOG" 2>&1 &
    pid=$!
    printf '%s\n' "$pid" > "$RADIO_PID_FILE"

    attempts=0
    while [ "$attempts" -lt 240 ]; do
        if curl -fsS "$RADIO_URL/healthz" >/dev/null 2>&1; then
            managed_radio_pid >/dev/null || die "radio process identity check failed"
            note "Branch radio ready at $RADIO_URL/admin"
            return 0
        fi
        kill -0 "$pid" 2>/dev/null || die "radio exited during startup; see $RADIO_LOG"
        attempts=$((attempts + 1))
        sleep 0.25
    done
    die "radio did not become healthy; see $RADIO_LOG"
}

stop_managed_pid() {
    local kind pid_file pid attempts
    kind="$1"
    pid_file="$2"
    pid="$3"
    note "Stopping owned $kind process (pid $pid)"
    kill -TERM "$pid"
    attempts=0
    while kill -0 "$pid" 2>/dev/null && [ "$attempts" -lt 80 ]; do
        attempts=$((attempts + 1))
        sleep 0.25
    done
    if kill -0 "$pid" 2>/dev/null; then
        die "$kind did not stop after TERM; refusing to force-kill it"
    fi
    rm -f "$pid_file"
}

stop_radio_if_managed() {
    local pid existing
    if pid="$(managed_radio_pid 2>/dev/null)"; then
        stop_managed_pid radio "$RADIO_PID_FILE" "$pid"
        return 0
    fi
    existing="$(port_pid "$RADIO_PORT")"
    [ -z "$existing" ] || die "port $RADIO_PORT is unmanaged; refusing to stop or reset it"
    rm -f "$RADIO_PID_FILE"
    return 1
}

stop_vlc_if_managed() {
    local pid existing
    if pid="$(managed_vlc_pid 2>/dev/null)"; then
        stop_managed_pid VLC "$VLC_PID_FILE" "$pid"
        return 0
    fi
    existing="$(port_pid "$VLC_PORT")"
    [ -z "$existing" ] || die "port $VLC_PORT is unmanaged; refusing to stop it"
    rm -f "$VLC_PID_FILE"
    return 1
}

require_secure_lab_for_partial_reset() {
    local pid
    if pid="$(managed_radio_pid 2>/dev/null)" &&
        ! pid_listens_on_loopback_only "$pid" "$RADIO_PORT"
    then
        die "replay/fresh-radio cannot partially migrate the legacy wildcard lab; run stop, start, update both LOCAL HA integration hosts to $DOCKER_GATEWAY_IP, then retry"
    fi
    if pid="$(managed_vlc_pid 2>/dev/null)" &&
        ! pid_listens_on_loopback_only "$pid" "$VLC_PORT"
    then
        die "replay/fresh-radio cannot partially migrate the legacy wildcard lab; run stop, start, update both LOCAL HA integration hosts to $DOCKER_GATEWAY_IP, then retry"
    fi
}

unique_archive_dir() {
    local purpose stamp candidate suffix
    purpose="$1"
    stamp="$(date '+%Y%m%d-%H%M%S')"
    candidate="$ARCHIVE_ROOT/$purpose-$stamp"
    suffix=1
    while path_exists "$candidate"; do
        candidate="$ARCHIVE_ROOT/$purpose-$stamp-$suffix"
        suffix=$((suffix + 1))
    done
    printf '%s\n' "$candidate"
}

replay_first_listen() {
    local was_running receipt archive
    assert_safe_lab_root
    require_secure_lab_for_partial_reset
    ensure_lab_dirs
    was_running=0
    if stop_radio_if_managed; then
        was_running=1
    fi
    # Branch updates may replace the old deterministic lab source. Migrate it
    # only after the owned radio has stopped so replay never rewrites a file
    # while the playback pipeline could still be reading it.
    ensure_local_track
    receipt="$RADIO_CACHE/state/first_listen_receipt_v1.json"
    if [ -f "$receipt" ]; then
        archive="$(unique_archive_dir replay)"
        mkdir "$archive"
        assert_existing_directory_path "$archive"
        chmod 700 "$archive"
        mv "$receipt" "$archive/first_listen_receipt_v1.json"
        note "Archived the completion receipt; HA, DB, music, and audio cache are unchanged"
    else
        note "No First Listen receipt existed; flow is already open"
    fi
    if [ "$was_running" -eq 1 ]; then
        start_radio
    else
        note "Radio was stopped; run scripts/first-listen-lab.sh start when ready"
    fi
}

fresh_radio() {
    local was_running archive
    assert_safe_lab_root
    require_secure_lab_for_partial_reset
    ensure_lab_dirs
    was_running=0
    if stop_radio_if_managed; then
        was_running=1
    fi
    ensure_local_track
    archive="$(unique_archive_dir fresh-radio)"
    mkdir "$archive"
    assert_existing_directory_path "$archive"
    chmod 700 "$archive"
    [ ! -e "$RADIO_CACHE" ] || mv "$RADIO_CACHE" "$archive/cache"
    [ ! -e "$RADIO_TMP" ] || mv "$RADIO_TMP" "$archive/tmp"
    [ ! -e "$RADIO_RUNTIME" ] || mv "$RADIO_RUNTIME" "$archive/runtime"
    ensure_directory "$RADIO_CACHE" 700
    ensure_directory "$RADIO_TMP" 700
    ensure_directory "$RADIO_RUNTIME" 700
    note "Archived all radio DB/receipts/temp/runtime state at $archive"
    note "Preserved the disposable HA install, integrations, speaker, credentials, and lab music"
    if [ "$was_running" -eq 1 ]; then
        start_radio
    else
        note "Radio was stopped; run scripts/first-listen-lab.sh start when ready"
    fi
}

show_setup() {
    local admin_token vlc_password
    ensure_env_file
    admin_token="$(require_lab_value ADMIN_TOKEN show-setup)"
    vlc_password="$(require_lab_value VLC_TELNET_PASSWORD show-setup)"
    cat <<EOF
Local-only setup values (do not use these on live Home Assistant):

  VLC Telnet integration
    Host:     $DOCKER_GATEWAY_IP
    Port:     $VLC_PORT
    Password: $vlc_password

  Mamma Mi Radio integration
    Host:        $DOCKER_GATEWAY_IP
    Port:        $RADIO_PORT
    Admin token: $admin_token

HA: $HA_URL_DEFAULT
Create a long-lived access token in the LOCAL HA profile, then run:
  scripts/first-listen-lab.sh set-ha-token
EOF
}

set_ha_token() {
    local token
    ensure_env_file
    if [ ! -t 0 ]; then
        die "set-ha-token needs an interactive terminal so the token stays out of shell history"
    fi
    printf 'Paste the LOCAL lab HA long-lived access token (input hidden): ' >&2
    IFS= read -r -s token
    printf '\n' >&2
    [ -n "$token" ] || die "token cannot be empty"
    set_lab_env_value HA_TOKEN "$token"
    unset token
    note "Saved the local HA token in gitignored $ENV_FILE (mode 0600)"
}

integration_in_sync() {
    [ -d "$HA_CONFIG/custom_components/mammamiradio" ] || return 1
    diff -qr -x __pycache__ -x '*.pyc' \
        "$ROOT/custom_components/mammamiradio" \
        "$HA_CONFIG/custom_components/mammamiradio" >/dev/null 2>&1
}

status() {
    local pid existing
    assert_safe_lab_root
    printf 'First Listen local HA lab\n'
    printf '  Runtime:        %s\n' "$LAB_ROOT"
    printf '  HA:             %s\n' "$HA_URL_DEFAULT"
    printf '  Radio admin:    %s/admin\n' "$RADIO_URL"
    printf '  Docker context: %s (explicit; global context is never changed)\n' "$DOCKER_CONTEXT"
    printf '  Credentials:    %s (values hidden)\n' "$ENV_FILE"

    if pid="$(managed_secure_radio_pid 2>/dev/null)"; then
        printf '  Radio:          running, managed, loopback-only (pid %s)\n' "$pid"
    elif pid="$(managed_radio_pid 2>/dev/null)"; then
        printf '  Radio:          running, managed legacy wildcard (pid %s; stop/start migration required)\n' "$pid"
    else
        existing="$(port_pid "$RADIO_PORT")"
        if [ -n "$existing" ]; then
            printf '  Radio:          unmanaged listener on port %s (refusing lifecycle actions)\n' "$RADIO_PORT"
        else
            printf '  Radio:          stopped\n'
        fi
    fi

    if pid="$(managed_secure_vlc_pid 2>/dev/null)"; then
        printf '  VLC speaker:    running, managed, loopback-only (pid %s)\n' "$pid"
    elif pid="$(managed_vlc_pid 2>/dev/null)"; then
        printf '  VLC speaker:    running, managed legacy wildcard (pid %s; stop/start migration required)\n' "$pid"
    else
        existing="$(port_pid "$VLC_PORT")"
        if [ -n "$existing" ]; then
            printf '  VLC speaker:    unmanaged listener on port %s (refusing lifecycle actions)\n' "$VLC_PORT"
        else
            printf '  VLC speaker:    stopped\n'
        fi
    fi

    if [ -f "$BRIDGE_MARKER" ]; then
        if managed_ssh_tunnel_pid >/dev/null 2>&1 &&
            managed_gateway_bridge_pid \
                "$VLC_BRIDGE_PID_FILE" "$VLC_PORT" "$VLC_TUNNEL_PORT" >/dev/null 2>&1 &&
            managed_gateway_bridge_pid \
                "$RADIO_BRIDGE_PID_FILE" "$RADIO_PORT" "$RADIO_TUNNEL_PORT" >/dev/null 2>&1
        then
            printf '  Private bridge: running, owned (HA gateway %s)\n' "$DOCKER_GATEWAY_IP"
        else
            printf '  Private bridge: ownership incomplete; lifecycle actions will fail closed\n'
        fi
    else
        printf '  Private bridge: not started (legacy labs migrate on explicit stop/start)\n'
    fi

    if docker_binary >/dev/null 2>&1 && docker_lab inspect "$HA_CONTAINER" >/dev/null 2>&1; then
        validate_ha_container
        if ha_container_is_loopback_only; then
            if ha_container_running; then
                printf '  HA container:   running, loopback-only\n'
            else
                printf '  HA container:   stopped, loopback-only\n'
            fi
        elif ha_container_running; then
            printf '  HA container:   running, legacy wildcard (explicit stop/start migration required)\n'
        elif ha_container_loopback_migration_authorized; then
            printf '  HA container:   stopped, loopback migration authorized for next start\n'
        else
            printf '  HA container:   stopped, legacy wildcard (run an explicit stop before start)\n'
        fi
    else
        printf '  HA container:   not found or Docker context unavailable\n'
    fi

    if integration_in_sync; then
        printf '  Integration:    matches this branch\n'
    else
        printf '  Integration:    differs or missing; run sync-integration\n'
    fi
}

start_all() {
    local include_radio
    [ "$(uname -s)" = "Darwin" ] || die "this local speaker lab currently supports macOS only"
    require_command lsof
    require_command ps
    require_command curl
    assert_safe_lab_root
    # Migration refusal happens before Colima, HA, credentials, or generated
    # media can be changed. Only an explicit stop may end the legacy listeners.
    refuse_legacy_lab_start
    ensure_env_file
    ensure_local_track
    start_colima
    validate_docker_gateway
    validate_colima_ssh_config
    ensure_remote_bridge_tools
    start_ha
    start_vlc
    include_radio=false
    if [ -n "$(lab_env_get HA_TOKEN)" ]; then
        start_radio
        include_radio=true
    else
        note "HA is ready for local onboarding. Save its token with set-ha-token, then run start again."
    fi
    start_private_bridge "$include_radio"
    status
}

sync_integration() {
    ensure_lab_dirs
    stage_integration
    if docker_binary >/dev/null 2>&1 && container_exists && ha_container_running; then
        note "HA is running; staged files will load on your next explicit lab stop/start. HA was not restarted."
    fi
}

stop_all() {
    local ignored legacy_ha_container
    assert_safe_lab_root
    ignored=0
    if colima_is_running && [ -f "$BRIDGE_MARKER" ]; then
        stop_gateway_bridge_if_managed \
            "$RADIO_BRIDGE_PID_FILE" radio "$RADIO_PORT" "$RADIO_TUNNEL_PORT" || ignored=1
        stop_gateway_bridge_if_managed \
            "$VLC_BRIDGE_PID_FILE" VLC "$VLC_PORT" "$VLC_TUNNEL_PORT" || ignored=1
        stop_ssh_tunnel_if_managed || ignored=1
        rm -f "$BRIDGE_MARKER"
    elif [ -f "$SSH_TUNNEL_PID_FILE" ] || [ -f "$VLC_BRIDGE_PID_FILE" ] ||
        [ -f "$RADIO_BRIDGE_PID_FILE" ]
    then
        die "bridge ownership files exist without a running marked bridge; refusing indirect cleanup"
    fi
    stop_radio_if_managed || ignored=1
    stop_vlc_if_managed || ignored=1
    if docker_binary >/dev/null 2>&1 && container_exists; then
        validate_ha_container
        legacy_ha_container=0
        if ! ha_container_is_loopback_only; then
            legacy_ha_container=1
        fi
        if ha_container_running; then
            note "Stopping preserved HA lab container"
            docker_lab stop "$HA_CONTAINER" >/dev/null
        fi
        if [ "$legacy_ha_container" -eq 1 ]; then
            ha_container_running && die \
                "verified legacy HA lab container did not stop; refusing to authorize its replacement"
            authorize_ha_container_loopback_migration
            note "Authorized loopback-only recreation of this stopped verified HA lab container on next start"
        else
            rm -f "$HA_CONTAINER_MIGRATION_MARKER"
        fi
    fi
    if colima_is_running; then
        note "Stopping isolated Colima profile; all lab data is preserved"
        colima stop --profile "$COLIMA_PROFILE"
    fi
    [ "$ignored" -eq 0 ] || note "Nothing else was stopped; unmanaged listeners are always preserved"
}

command_name="${1:---help}"
case "$command_name" in
    -h|--help|help) usage ;;
    start|up) start_all ;;
    set-ha-token) set_ha_token ;;
    show-setup|configure) show_setup ;;
    sync-integration) sync_integration ;;
    replay) replay_first_listen ;;
    fresh-radio|fresh) fresh_radio ;;
    status) status ;;
    stop|down) stop_all ;;
    *) usage >&2; die "unknown command: $command_name" ;;
esac
