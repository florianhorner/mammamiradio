"""Offline safety and lifecycle contracts for the disposable First Listen HA lab."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "first-listen-lab.sh"
RUNBOOK = ROOT / "docs" / "runbooks" / "first-listen-local-ha.md"
LAB_MARKER_NAME = ".mammamiradio-first-listen-lab"


def _mark_owned_ha_config(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / LAB_MARKER_NAME).write_text("Disposable Mamma Mi Radio First Listen lab. Never mount live HA data here.\n")


def _run(
    script: Path,
    command: str,
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), command],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _write_shim(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


@pytest.fixture()
def fake_repo(tmp_path: Path) -> tuple[Path, Path, dict[str, str], Path]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / SCRIPT.name
    shutil.copy2(SCRIPT, script)
    show = tmp_path / "mammamiradio" / "assets" / "demo" / "first_listen" / "first_listen_show.mp3"
    show.parent.mkdir(parents=True)
    show.write_bytes(b"packaged-first-listen-show")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "external-calls.log"
    # Lab reset/replay tests must be hermetic even when the developer has the
    # real lab listening on 8000/4212. Docker/Colima are poison shims: a
    # radio-only reset must never reach either lifecycle.
    _write_shim(bin_dir / "lsof", "exit 0\n")
    _write_shim(bin_dir / "docker", 'printf "docker %s\\n" "$*" >> "$POISON_LOG"\nexit 1\n')
    _write_shim(bin_dir / "colima", 'printf "colima %s\\n" "$*" >> "$POISON_LOG"\nexit 1\n')

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '/usr/bin:/bin')}"
    env["POISON_LOG"] = str(calls)
    return tmp_path, script, env, calls


def test_script_exists_is_executable_and_parses() -> None:
    assert SCRIPT.exists()
    assert SCRIPT.stat().st_mode & 0o111
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_is_side_effect_free_with_poisoned_external_tools(
    fake_repo: tuple[Path, Path, dict[str, str], Path], flag: str
) -> None:
    repo, script, env, calls = fake_repo

    result = _run(script, flag, cwd=repo, env=env)

    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout
    assert "fresh-radio" in result.stdout
    assert "replay" in result.stdout
    assert not (repo / "tmp").exists()
    assert not calls.exists()


def test_lab_is_local_isolated_and_never_changes_global_docker_context() -> None:
    body = SCRIPT.read_text()

    assert 'LAB_ROOT="$ROOT/tmp/first-listen-ha-lab"' in body
    assert 'COLIMA_PROFILE="mmr-first-listen"' in body
    assert 'DOCKER_CONTEXT="colima-mmr-first-listen"' in body
    assert '"$binary" --context "$DOCKER_CONTEXT" "$@"' in body
    assert 'colima start --profile "$COLIMA_PROFILE" --activate=false' in body
    assert "docker context use" not in body
    assert ".context" not in body
    assert "homeassistant.local" not in body
    assert "ha-green" not in body
    assert "100.98.177.107" not in body
    assert "scp " not in body
    assert "rsync" not in body


def test_lab_installs_packaged_mini_show_instead_of_generating_brrrr(tmp_path: Path) -> None:
    source = tmp_path / "show.mp3"
    target = tmp_path / "lab.mp3"
    source.write_bytes(b"authored-mini-show")
    target.write_bytes(b"old-brrrr")
    command = r"""
script="$1"
source_clip="$2"
target_clip="$3"
set -- --help
source "$script" >/dev/null
FIRST_LISTEN_SHOW="$source_clip"
LOCAL_TRACK="$target_clip"
ensure_local_track
"""

    result = subprocess.run(
        ["bash", "-c", command, "first-listen-show-test", str(SCRIPT), str(source), str(target)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == source.read_bytes()
    body = SCRIPT.read_text()
    assert 'FIRST_LISTEN_SHOW="$ROOT/mammamiradio/assets/demo/first_listen/first_listen_show.mp3"' in body
    assert "sine=frequency=110" not in body


def test_mac_endpoints_bind_only_to_loopback_but_legacy_pids_remain_owned() -> None:
    body = SCRIPT.read_text()

    # VLC 3 parses telnet-host as a URL. A bare 127.0.0.1 becomes a path,
    # leaving the host empty and opening wildcard IPv4/IPv6 listeners.
    assert '--telnet-host telnet://127.0.0.1 --telnet-port "$VLC_PORT"' in body
    assert '--telnet-host 127.0.0.1 --telnet-port "$VLC_PORT"' not in body
    assert '"$vlc_binary" --ignore-config --intf dummy --extraintf telnet' in body
    assert 'export MAMMAMIRADIO_BIND_HOST="127.0.0.1"' in body
    assert '--host 127.0.0.1 --port "$RADIO_PORT"' in body
    assert "0.0.0.0" not in body

    radio_owner = body[body.index("managed_radio_pid()") : body.index("managed_vlc_pid()")]
    vlc_owner = body[body.index("managed_vlc_pid()") : body.index("pid_listens_on_loopback_only()")]
    assert "pid_listens_on_loopback_only" not in radio_owner
    assert "pid_listens_on_loopback_only" not in vlc_owner
    assert "managed_secure_radio_pid" in body
    assert "managed_secure_vlc_pid" in body


def test_private_bridge_uses_owned_nonmultiplexed_ssh_and_atomic_collisions() -> None:
    body = SCRIPT.read_text()

    assert 'SSH_BINARY="/usr/bin/ssh"' in body
    assert 'COLIMA_SSH_CONFIG="$COLIMA_HOME_ROOT/_lima/colima-$COLIMA_PROFILE/ssh.config"' in body
    for option in (
        "-S none",
        "ControlMaster=no",
        "ControlPersist=no",
        "ControlPath=none",
        "ProxyCommand=none",
        "ProxyJump=none",
        "PermitLocalCommand=no",
        "ExitOnForwardFailure=yes",
    ):
        assert option in body
    assert '-R "127.0.0.1:$VLC_TUNNEL_PORT:127.0.0.1:$VLC_PORT"' in body
    assert '-R "127.0.0.1:$RADIO_TUNNEL_PORT:127.0.0.1:$RADIO_PORT"' in body
    assert '-N -T "$COLIMA_SSH_HOST"' in body
    assert "GatewayPorts=yes" not in body
    assert "pkill" not in body
    assert "killall" not in body
    assert "kill -9" not in body
    assert "kill -KILL" not in body


@pytest.mark.skipif(not Path("/usr/bin/ssh").is_file(), reason="requires the OpenSSH client used by the lab")
def test_colima_ssh_validation_accepts_omitted_disabled_control_path(tmp_path: Path) -> None:
    ssh_config = tmp_path / "ssh.config"
    ssh_config.write_text(
        """Host lima-colima-mmr-first-listen
  Hostname 127.0.0.1
  Port 22
  User lab
  BatchMode yes
  ControlMaster auto
  ControlPath /tmp/must-be-overridden.sock
  ControlPersist yes
"""
    )
    command = """
script="$1"
ssh_config="$2"
set -- --help
source "$script" >/dev/null
COLIMA_SSH_CONFIG="$ssh_config"
validate_colima_ssh_config
"""

    result = subprocess.run(
        ["bash", "-c", command, "first-listen-ssh-test", str(SCRIPT), str(ssh_config)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_gateway_relays_are_vm_private_owned_and_end_to_end_verified() -> None:
    body = SCRIPT.read_text()
    remote_control = body[
        body.index("remote_gateway_bridge_control()") : body.index("managed_remote_gateway_bridge_pid()")
    ]

    assert 'DOCKER_GATEWAY_IP="172.17.0.1"' in body
    assert '[ "$subnet" = "172.17.0.0/16" ]' in body
    assert '[ "$gateway" = "$DOCKER_GATEWAY_IP" ]' in body
    assert 'REMOTE_BRIDGE_RUNTIME="/tmp/mammamiradio-first-listen-bridge-v1"' in body
    assert '"TCP4-LISTEN:$listen_port,bind=$gateway,reuseaddr,fork,max-children=32,range=172.17.0.0/16"' in body
    assert '"TCP4-CONNECT:127.0.0.1:$target_port,connect-timeout=3"' in body
    assert "actual=\"$(tr '\\000' '\\n' < \"/proc/$pid/cmdline\")\"" in remote_control
    assert "expected=\"$(printf 'socat\\n%s\\n%s\\n'" in remote_control
    assert remote_control.index('[ "$actual" = "$expected" ]') < remote_control.index('kill -TERM "-$pid"')
    assert '[ "$pgid" = "$pid" ]' in remote_control
    assert 'kill -TERM "-$pid"' in remote_control
    assert 'kill -0 "-$pid"' in remote_control
    assert "owned process group $pid survived TERM; PID evidence was preserved" in remote_control
    assert 'docker_lab exec -i "$HA_CONTAINER" python3 -' in body
    assert 'if b"Password" not in greeting:' in body
    assert 'f"http://{gateway}:{radio_port}/healthz"' in body
    assert "Host:     $DOCKER_GATEWAY_IP" in body
    assert "Host:        $DOCKER_GATEWAY_IP" in body


def test_bridge_start_rolls_back_only_new_exactly_owned_processes() -> None:
    body = SCRIPT.read_text()
    start = body[body.index("start_private_bridge()") : body.index("wait_for_port()")]
    rollback = body[body.index("rollback_term_owned_pid()") : body.index("start_private_bridge()")]

    assert "trap rollback_private_bridge_on_exit EXIT" in start
    assert "BRIDGE_STARTED_TUNNEL=0" in start
    assert "BRIDGE_STARTED_VLC_RELAY=0" in start
    assert "BRIDGE_STARTED_RADIO_RELAY=0" in start
    assert "trap - EXIT" in start
    assert rollback.index("BRIDGE_STARTED_RADIO_RELAY") < rollback.index("BRIDGE_STARTED_VLC_RELAY")
    assert rollback.index("BRIDGE_STARTED_VLC_RELAY") < rollback.index("BRIDGE_STARTED_TUNNEL")
    assert "managed_gateway_bridge_wrapper_pid" in rollback
    assert "managed_remote_gateway_bridge_pid" in rollback
    gateway_rollback = rollback[
        rollback.index("rollback_new_gateway_bridge()") : rollback.index("rollback_new_ssh_tunnel()")
    ]
    assert gateway_rollback.index("stop_remote_gateway_bridge_if_managed") < gateway_rollback.index(
        "rollback_term_owned_pid"
    )
    assert "managed_ssh_tunnel_pid" in rollback
    assert 'kill -TERM "$pid"' in rollback


def test_live_mismatched_bridge_pid_evidence_is_never_erased_or_signalled() -> None:
    body = SCRIPT.read_text()
    guard = body[body.index("remove_stale_pid_file_or_refuse_live_mismatch()") : body.index("command_contains_all()")]
    tunnel_start = body[body.index("start_ssh_tunnel()") : body.index("start_gateway_bridge()")]
    relay_start = body[body.index("start_gateway_bridge()") : body.index("remote_tunnel_port_free()")]

    assert guard.index('kill -0 "$pid"') < guard.index("fails the exact ownership check")
    assert guard.index("fails the exact ownership check") < guard.index('rm -f "$pid_file"')
    assert "kill -TERM" not in guard
    assert tunnel_start.index("remove_stale_pid_file_or_refuse_live_mismatch") < tunnel_start.index(
        ': > "$SSH_TUNNEL_LOG"'
    )
    assert relay_start.index("remove_stale_pid_file_or_refuse_live_mismatch") < relay_start.index(': > "$log_file"')


def test_cleanup_is_bounded_owned_reverse_order_and_never_force_kills() -> None:
    body = SCRIPT.read_text()
    stop = body[body.index("stop_all()") : body.index('command_name="${1:---help}"')]

    radio_bridge = stop.index('"$RADIO_BRIDGE_PID_FILE" radio')
    vlc_bridge = stop.index('"$VLC_BRIDGE_PID_FILE" VLC')
    tunnel = stop.index("stop_ssh_tunnel_if_managed")
    radio = stop.index("stop_radio_if_managed")
    vlc = stop.index("stop_vlc_if_managed")
    ha = stop.index('docker_lab stop "$HA_CONTAINER"')
    colima = stop.index('colima stop --profile "$COLIMA_PROFILE"')
    assert radio_bridge < vlc_bridge < tunnel < radio < vlc < ha < colima
    assert 'while kill -0 "$pid"' in body
    assert "refusing to force-kill it" in body


def test_stop_tears_down_owned_remote_relay_after_local_ssh_wrapper_exits(tmp_path: Path) -> None:
    pid_file = tmp_path / "radio-bridge.pid"
    pid_file.write_text("999999\n")
    calls = tmp_path / "calls"
    command = r"""
script="$1"
pid_file="$2"
calls="$3"
set -- --help
source "$script" >/dev/null
managed_gateway_bridge_wrapper_pid() { return 1; }
remove_stale_pid_file_or_refuse_live_mismatch() {
    printf 'remove-local-stale %s\n' "$1" >> "$calls"
    rm -f "$1"
}
managed_remote_gateway_bridge_pid() { printf '4321\n'; }
stop_remote_gateway_bridge_if_managed() {
    printf 'stop-remote %s %s\n' "$1" "$2" >> "$calls"
}
wait_gateway_port_free_from_ha() {
    printf 'wait-free %s\n' "$1" >> "$calls"
}
remove_stale_remote_pid_file_or_refuse_live_mismatch() {
    printf 'unexpected-remote-cleanup\n' >> "$calls"
    return 1
}
gateway_port_free_from_ha() {
    printf 'unexpected-port-probe\n' >> "$calls"
    return 1
}
stop_gateway_bridge_if_managed "$pid_file" radio "$RADIO_PORT" "$RADIO_TUNNEL_PORT"
"""

    result = subprocess.run(
        ["bash", "-c", command, "remote-survivor-test", str(SCRIPT), str(pid_file), str(calls)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == [
        f"remove-local-stale {pid_file}",
        "stop-remote 8000 18000",
        "wait-free 8000",
    ]
    assert not pid_file.exists()


def test_remote_relay_surviving_term_preserves_evidence_and_blocks_later_teardown(tmp_path: Path) -> None:
    pid_file = tmp_path / "radio-bridge.pid"
    pid_file.write_text("111\n")
    calls = tmp_path / "calls"
    command = r"""
script="$1"
pid_file="$2"
calls="$3"
set -- --help
source "$script" >/dev/null
managed_gateway_bridge_wrapper_pid() { printf '111\n'; }
managed_remote_gateway_bridge_pid() { printf '4321\n'; }
stop_remote_gateway_bridge_if_managed() {
    printf 'stop-remote %s %s\n' "$1" "$2" >> "$calls"
    return 1
}
stop_managed_pid() { printf 'unexpected-local-stop\n' >> "$calls"; }
wait_gateway_port_free_from_ha() { printf 'unexpected-port-wait\n' >> "$calls"; }
stop_gateway_bridge_if_managed "$pid_file" radio "$RADIO_PORT" "$RADIO_TUNNEL_PORT"
"""

    result = subprocess.run(
        ["bash", "-c", command, "remote-term-survivor-test", str(SCRIPT), str(pid_file), str(calls)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "survived TERM in the VM" in result.stderr
    assert calls.read_text().splitlines() == ["stop-remote 8000 18000"]
    assert pid_file.read_text() == "111\n"


def test_stop_refuses_unmanaged_remote_gateway_listener_without_signalling_it(tmp_path: Path) -> None:
    calls = tmp_path / "calls"
    command = r"""
script="$1"
calls="$2"
set -- --help
source "$script" >/dev/null
managed_gateway_bridge_wrapper_pid() { return 1; }
remove_stale_pid_file_or_refuse_live_mismatch() { :; }
managed_remote_gateway_bridge_pid() { return 1; }
remove_stale_remote_pid_file_or_refuse_live_mismatch() { :; }
gateway_port_free_from_ha() { return 1; }
stop_remote_gateway_bridge_if_managed() { printf 'unexpected-remote-stop\n' >> "$calls"; }
stop_managed_pid() { printf 'unexpected-local-stop\n' >> "$calls"; }
stop_gateway_bridge_if_managed "$calls.local-pid" radio "$RADIO_PORT" "$RADIO_TUNNEL_PORT"
"""

    result = subprocess.run(
        ["bash", "-c", command, "unmanaged-relay-test", str(SCRIPT), str(calls)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "gateway port is occupied by an unmanaged process" in result.stderr
    assert not calls.exists()


def test_legacy_migration_refuses_start_and_partial_resets_before_side_effects() -> None:
    body = SCRIPT.read_text()
    start = body[body.index("start_all()") : body.index("sync_integration()")]
    replay = body[body.index("replay_first_listen()") : body.index("fresh_radio()")]
    fresh = body[body.index("fresh_radio()") : body.index("show_setup()")]

    assert start.index("refuse_legacy_lab_start") < start.index("ensure_env_file")
    assert start.index("refuse_legacy_lab_start") < start.index("start_colima")
    assert replay.index("require_secure_lab_for_partial_reset") < replay.index("stop_radio_if_managed")
    assert fresh.index("require_secure_lab_for_partial_reset") < fresh.index("stop_radio_if_managed")
    assert "Only an explicit stop may end the legacy listeners" in body
    assert "update both LOCAL HA integration hosts" in body


def test_runbook_documents_private_bridge_dependency_and_truthful_migration() -> None:
    docs = RUNBOOK.read_text()

    assert "172.17.0.1" in docs
    assert "owned, non-multiplexed SSH reverse tunnel" in docs
    assert "socat" in docs
    assert "cat /etc/os-release" in docs
    assert "Debian or Ubuntu guest only" in docs
    assert "Existing lab migration" in docs
    assert "host.lima.internal" in docs
    assert "Do not make these changes in the live home" in docs


def test_ha_container_is_owned_loopback_only_and_never_auto_restarted() -> None:
    body = SCRIPT.read_text()

    assert 'HA_LABEL="mammamiradio.first-listen-lab=true"' in body
    assert "--restart=no" in body
    assert "-p 127.0.0.1:8123:8123" in body
    assert '"$HA_CONFIG:/config"' in body
    assert "--network=host" not in body
    assert "--privileged" not in body
    assert "docker_lab restart" not in body
    assert "docker_lab kill" not in body
    assert 'docker_lab rm "$legacy_container_id"' in body
    assert "docker_lab rm -f" not in body
    assert "docker_lab rm --force" not in body
    assert "colima delete" not in body
    assert "ha core restart" not in body
    assert "supervisor restart" not in body
    assert "rm -rf" not in body


def test_legacy_ha_container_requires_exact_stop_authorization_before_recreation() -> None:
    body = SCRIPT.read_text()
    start = body[body.index("start_ha()") : body.index("port_pid()")]
    stop = body[body.index("stop_all()") : body.index('command_name="${1:---help}"')]

    assert start.index("validate_ha_container") < start.index("ha_container_is_loopback_only")
    assert start.index("ha_container_is_loopback_only") < start.index("ha_container_running")
    assert start.index("ha_container_running") < start.index("ha_container_loopback_migration_id")
    assert start.index("ha_container_loopback_migration_id") < start.index('docker_lab rm "$legacy_container_id"')
    assert start.index('docker_lab rm "$legacy_container_id"') < start.index("create_loopback_ha_container")
    assert "Deliberately no --force" in start

    assert stop.index("validate_ha_container") < stop.index("ha_container_is_loopback_only")
    assert stop.index("ha_container_is_loopback_only") < stop.index('docker_lab stop "$HA_CONTAINER"')
    assert stop.index('docker_lab stop "$HA_CONTAINER"') < stop.index("authorize_ha_container_loopback_migration")
    assert 'container_id="$(ha_container_id)"' in body
    assert 'expected_id" = "$actual_id' in body
    assert '"$HA_CONFIG:/config"' in body


@pytest.mark.parametrize(
    ("bindings", "is_loopback_only"),
    [
        ("127.0.0.1:8123", True),
        ("0.0.0.0:8123", False),
        (":::8123", False),
        (":8123", False),
        ("127.0.0.1:8123\n::1:8123", False),
    ],
)
def test_ha_container_port_classification_accepts_only_the_exact_loopback_binding(
    bindings: str, is_loopback_only: bool
) -> None:
    command = r"""
script="$1"
set -- --help
source "$script" >/dev/null
docker_lab() { printf '%s\n' "$TEST_BINDINGS"; }
if ha_container_is_loopback_only; then
    exit 0
fi
exit 3
"""
    env = dict(os.environ)
    env["TEST_BINDINGS"] = bindings

    result = subprocess.run(
        ["bash", "-c", command, "port-binding-test", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert (result.returncode == 0) is is_loopback_only


def test_start_ha_never_removes_a_running_legacy_container(tmp_path: Path) -> None:
    calls = tmp_path / "calls"
    command = r"""
script="$1"
sandbox="$2"
set -- --help
source "$script" >/dev/null
container_exists() { return 0; }
validate_ha_container() { printf 'validated\n' >> "$sandbox/calls"; }
ha_container_is_loopback_only() { return 1; }
ha_container_running() { return 0; }
docker_lab() { printf 'docker %s\n' "$*" >> "$sandbox/calls"; }
start_ha
"""

    result = subprocess.run(
        ["bash", "-c", command, "legacy-running-test", str(SCRIPT), str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "still publishes beyond loopback" in result.stderr
    assert calls.read_text().splitlines() == ["validated"]


@pytest.mark.parametrize("marker_id", [None, "different-container-id"])
def test_start_ha_never_removes_an_unauthorized_stopped_legacy_container(tmp_path: Path, marker_id: str | None) -> None:
    lab = tmp_path / "lab"
    lab.mkdir()
    marker = lab / ".ha-container-loopback-migration-v1"
    if marker_id is not None:
        marker.write_text(f"{marker_id}\n")
    calls = tmp_path / "calls"
    command = r"""
script="$1"
sandbox="$2"
set -- --help
source "$script" >/dev/null
LAB_ROOT="$sandbox/lab"
HA_CONTAINER_MIGRATION_MARKER="$LAB_ROOT/.ha-container-loopback-migration-v1"
container_exists() { return 0; }
validate_ha_container() { printf 'validated\n' >> "$sandbox/calls"; }
ha_container_is_loopback_only() { return 1; }
ha_container_running() { return 1; }
ha_container_id() { printf 'verified-container-id\n'; }
docker_lab() { printf 'docker %s\n' "$*" >> "$sandbox/calls"; }
start_ha
"""

    result = subprocess.run(
        ["bash", "-c", command, "legacy-unauthorized-test", str(SCRIPT), str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "migration was not authorized" in result.stderr
    assert calls.read_text().splitlines() == ["validated"]


def test_start_ha_recreates_only_the_stopped_authorized_container(tmp_path: Path) -> None:
    lab = tmp_path / "lab"
    ha_config = lab / "ha-config"
    ha_config.mkdir(parents=True)
    marker = lab / ".ha-container-loopback-migration-v1"
    marker.write_text("verified-container-id\n")
    calls = tmp_path / "calls"
    command = r"""
script="$1"
sandbox="$2"
set -- --help
source "$script" >/dev/null
LAB_ROOT="$sandbox/lab"
HA_CONFIG="$LAB_ROOT/ha-config"
HA_CONTAINER_MIGRATION_MARKER="$LAB_ROOT/.ha-container-loopback-migration-v1"
container_exists() { return 0; }
validate_ha_container() { printf 'validated\n' >> "$sandbox/calls"; }
ha_container_is_loopback_only() { return 1; }
ha_container_id() { printf 'verified-container-id\n'; }
ha_container_running() { [ -f "$sandbox/running" ]; }
docker_lab() {
    printf 'docker %s\n' "$*" >> "$sandbox/calls"
    if [ "$1" = run ]; then
        : > "$sandbox/running"
    fi
}
start_ha
"""

    result = subprocess.run(
        ["bash", "-c", command, "legacy-stopped-test", str(SCRIPT), str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    call_lines = calls.read_text().splitlines()
    assert call_lines[0] == "validated"
    assert call_lines[1] == "docker rm verified-container-id"
    assert call_lines[2].startswith("docker run -d --name mmr-first-listen-ha")
    assert "--label mammamiradio.first-listen-lab=true" in call_lines[2]
    assert "-p 127.0.0.1:8123:8123" in call_lines[2]
    assert f"-v {ha_config}:/config" in call_lines[2]
    assert not marker.exists()


def test_explicit_stop_authorizes_only_the_verified_stopped_legacy_container(tmp_path: Path) -> None:
    lab = tmp_path / "lab"
    lab.mkdir()
    running = tmp_path / "running"
    running.touch()
    calls = tmp_path / "calls"
    command = r"""
script="$1"
sandbox="$2"
set -- --help
source "$script" >/dev/null
LAB_ROOT="$sandbox/lab"
HA_CONTAINER_MIGRATION_MARKER="$LAB_ROOT/.ha-container-loopback-migration-v1"
BRIDGE_RUNTIME="$LAB_ROOT/bridge-runtime"
BRIDGE_MARKER="$BRIDGE_RUNTIME/.secure-bridge-v1"
SSH_TUNNEL_PID_FILE="$BRIDGE_RUNTIME/ssh-tunnel.pid"
VLC_BRIDGE_PID_FILE="$BRIDGE_RUNTIME/vlc-bridge.pid"
RADIO_BRIDGE_PID_FILE="$BRIDGE_RUNTIME/radio-bridge.pid"
assert_safe_lab_root() { :; }
colima_is_running() { return 1; }
stop_radio_if_managed() { return 0; }
stop_vlc_if_managed() { return 0; }
docker_binary() { printf '/fake/docker\n'; }
container_exists() { return 0; }
validate_ha_container() { printf 'validated\n' >> "$sandbox/calls"; }
ha_container_is_loopback_only() { return 1; }
ha_container_id() { printf 'verified-container-id\n'; }
ha_container_running() { [ -f "$sandbox/running" ]; }
docker_lab() {
    printf 'docker %s\n' "$*" >> "$sandbox/calls"
    if [ "$1" = stop ]; then
        rm "$sandbox/running"
    fi
}
stop_all
"""

    result = subprocess.run(
        ["bash", "-c", command, "legacy-stop-test", str(SCRIPT), str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == ["validated", "docker stop mmr-first-listen-ha"]
    assert not running.exists()
    assert (lab / ".ha-container-loopback-migration-v1").read_text() == "verified-container-id\n"


def test_radio_launch_is_no_key_and_uses_an_isolated_working_directory() -> None:
    body = SCRIPT.read_text()

    assert 'cd "$RADIO_RUNTIME"' in body
    assert 'RADIO_CONFIG_FILE="$RADIO_RUNTIME/radio.toml"' in body
    assert 'RADIO_MODEL_REGISTRY_FILE="$RADIO_RUNTIME/model_registry.toml"' in body
    assert 'cp "$ROOT/radio.toml" "$RADIO_CONFIG_FILE"' in body
    assert 'cp "$ROOT/model_registry.toml" "$RADIO_MODEL_REGISTRY_FILE"' in body
    assert 'export MAMMAMIRADIO_HA_CONTEXT_ENABLED=""' in body
    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "AZURE_SPEECH_KEY",
        "AZURE_SPEECH_REGION",
        "ELEVENLABS_API_KEY",
    ):
        assert f'export {key}=""' in body
    assert 'export JAMENDO_CLIENT_ID=""' in body
    assert 'export MAMMAMIRADIO_ALLOW_YTDLP="false"' in body
    assert 'source "$ROOT/.env"' not in body
    assert "source .env" not in body


def test_radio_ha_destination_is_hard_pinned_to_the_owned_loopback_lab() -> None:
    body = SCRIPT.read_text()

    assert 'HA_URL_DEFAULT="http://127.0.0.1:8123"' in body
    assert 'export HA_URL="$HA_URL_DEFAULT"' in body
    assert "lab_env_get HA_URL" not in body
    assert "require_lab_value HA_URL" not in body
    assert "printf 'HA_URL=" not in body


def test_status_pins_docker_to_the_lab_context_and_redacts_credentials(
    fake_repo: tuple[Path, Path, dict[str, str], Path],
) -> None:
    repo, script, env, calls = fake_repo
    lab = repo / "tmp" / "first-listen-ha-lab"
    lab.mkdir(parents=True)
    secret = "must-not-appear-in-status"
    (lab / "lab.env").write_text(f"HA_TOKEN={secret}\nADMIN_TOKEN={secret}\n")

    result = _run(script, "status", cwd=repo, env=env)

    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout
    assert "values hidden" in result.stdout
    assert calls.read_text().splitlines() == ["docker --context colima-mmr-first-listen inspect mmr-first-listen-ha"]


def test_replay_archives_only_receipt_and_preserves_radio_and_ha_state(
    fake_repo: tuple[Path, Path, dict[str, str], Path],
) -> None:
    repo, script, env, calls = fake_repo
    lab = repo / "tmp" / "first-listen-ha-lab"
    state = lab / "radio-cache" / "state"
    state.mkdir(parents=True)
    receipt = state / "first_listen_receipt_v1.json"
    receipt.write_text('{"heard_at": 1}\n')
    database = lab / "radio-cache" / "mammamiradio.db"
    database.write_bytes(b"database-sentinel")
    ha_sentinel = lab / "ha-config" / "ha-sentinel"
    _mark_owned_ha_config(ha_sentinel.parent)
    ha_sentinel.write_bytes(b"ha-must-survive")

    result = _run(script, "replay", cwd=repo, env=env)

    assert result.returncode == 0, result.stderr
    assert not receipt.exists()
    archived = list((lab / "archives").glob("replay-*/first_listen_receipt_v1.json"))
    assert len(archived) == 1
    assert archived[0].read_text() == '{"heard_at": 1}\n'
    assert database.read_bytes() == b"database-sentinel"
    assert ha_sentinel.read_bytes() == b"ha-must-survive"
    assert not calls.exists()


def test_fresh_radio_archives_station_state_twice_but_preserves_ha_credentials_and_music(
    fake_repo: tuple[Path, Path, dict[str, str], Path],
) -> None:
    repo, script, env, calls = fake_repo
    lab = repo / "tmp" / "first-listen-ha-lab"
    cache = lab / "radio-cache"
    runtime = lab / "radio-runtime"
    radio_tmp = lab / "radio-tmp"
    ha_config = lab / "ha-config"
    music = lab / "radio-music"
    for path in (cache, runtime, radio_tmp, ha_config, music):
        path.mkdir(parents=True, exist_ok=True)
    _mark_owned_ha_config(ha_config)
    (cache / "mammamiradio.db").write_bytes(b"first-db")
    (runtime / ".env").write_text('MAMMAMIRADIO_HA_CONTEXT_ENABLED="false"\n')
    (radio_tmp / "render.mp3").write_bytes(b"render")
    (ha_config / "home-assistant_v2.db").write_bytes(b"ha-must-survive")
    (music / "lab.mp3").write_bytes(b"music-must-survive")
    lab_env = lab / "lab.env"
    lab_env.write_bytes(b"HA_TOKEN=local-only\n")
    lab_env.chmod(0o600)

    first = _run(script, "fresh-radio", cwd=repo, env=env)
    assert first.returncode == 0, first.stderr
    assert cache.is_dir() and not any(cache.iterdir())
    assert runtime.is_dir() and not any(runtime.iterdir())
    assert radio_tmp.is_dir() and not any(radio_tmp.iterdir())
    assert (ha_config / "home-assistant_v2.db").read_bytes() == b"ha-must-survive"
    assert (music / "lab.mp3").read_bytes() == b"music-must-survive"
    assert lab_env.read_bytes() == b"HA_TOKEN=local-only\n"
    first_archives = list((lab / "archives").glob("fresh-radio-*"))
    assert len(first_archives) == 1
    assert (first_archives[0] / "cache" / "mammamiradio.db").read_bytes() == b"first-db"
    assert (first_archives[0] / "runtime" / ".env").exists()

    (cache / "mammamiradio.db").write_bytes(b"second-db")
    second = _run(script, "fresh-radio", cwd=repo, env=env)
    assert second.returncode == 0, second.stderr
    archives = list((lab / "archives").glob("fresh-radio-*"))
    assert len(archives) == 2
    assert sorted((item / "cache" / "mammamiradio.db").read_bytes() for item in archives) == [
        b"first-db",
        b"second-db",
    ]
    assert not calls.exists()


def test_stateful_command_rejects_symlinked_repo_tmp_without_touching_target(
    fake_repo: tuple[Path, Path, dict[str, str], Path],
) -> None:
    repo, script, env, calls = fake_repo
    outside = repo / "outside-tmp"
    outside.mkdir()
    (repo / "tmp").symlink_to(outside, target_is_directory=True)

    result = _run(script, "replay", cwd=repo, env=env)

    assert result.returncode != 0
    assert "symbolic-link lab path" in result.stderr
    assert not any(outside.iterdir())
    assert not calls.exists()


def test_stateful_command_rejects_symlinked_lab_root_without_touching_target(
    fake_repo: tuple[Path, Path, dict[str, str], Path],
) -> None:
    repo, script, env, calls = fake_repo
    (repo / "tmp").mkdir()
    outside = repo / "outside-lab"
    outside.mkdir()
    (repo / "tmp" / "first-listen-ha-lab").symlink_to(outside, target_is_directory=True)

    result = _run(script, "replay", cwd=repo, env=env)

    assert result.returncode != 0
    assert "symbolic-link lab path" in result.stderr
    assert not any(outside.iterdir())
    assert not calls.exists()


@pytest.mark.parametrize(
    "child",
    [
        "ha-config",
        "radio-cache",
        "radio-tmp",
        "radio-music",
        "radio-runtime",
        "vlc-runtime",
        "bridge-runtime",
        "archives",
        "logs",
        "component-backups",
    ],
)
def test_stateful_command_rejects_symlinked_critical_lab_directory(
    fake_repo: tuple[Path, Path, dict[str, str], Path],
    child: str,
) -> None:
    repo, script, env, calls = fake_repo
    lab = repo / "tmp" / "first-listen-ha-lab"
    lab.mkdir(parents=True)
    outside = repo / f"outside-{child}"
    outside.mkdir()
    (lab / child).symlink_to(outside, target_is_directory=True)

    result = _run(script, "replay", cwd=repo, env=env)

    assert result.returncode != 0
    assert "symbolic-link lab path" in result.stderr
    assert not any(outside.iterdir())
    assert not calls.exists()


def test_stateful_command_rejects_symlinked_ownership_marker(
    fake_repo: tuple[Path, Path, dict[str, str], Path],
) -> None:
    repo, script, env, calls = fake_repo
    ha_config = repo / "tmp" / "first-listen-ha-lab" / "ha-config"
    ha_config.mkdir(parents=True)
    outside = repo / "outside-marker"
    outside.write_text("not ownership proof\n")
    (ha_config / LAB_MARKER_NAME).symlink_to(outside)

    result = _run(script, "replay", cwd=repo, env=env)

    assert result.returncode != 0
    assert "symbolic-link lab file" in result.stderr
    assert outside.read_text() == "not ownership proof\n"
    assert not calls.exists()


def test_nonempty_unmarked_ha_config_is_never_adopted_or_modified(
    fake_repo: tuple[Path, Path, dict[str, str], Path],
) -> None:
    repo, script, env, calls = fake_repo
    ha_config = repo / "tmp" / "first-listen-ha-lab" / "ha-config"
    ha_config.mkdir(parents=True)
    sentinel = ha_config / "configuration.yaml"
    sentinel.write_text("live-home-sentinel\n")

    result = _run(script, "replay", cwd=repo, env=env)

    assert result.returncode != 0
    assert "nonempty unmarked HA config" in result.stderr
    assert sentinel.read_text() == "live-home-sentinel\n"
    assert not (ha_config / LAB_MARKER_NAME).exists()
    assert not calls.exists()


def test_new_empty_ha_config_is_marked_and_all_private_directories_are_0700(
    fake_repo: tuple[Path, Path, dict[str, str], Path],
) -> None:
    repo, script, env, calls = fake_repo
    lab = repo / "tmp" / "first-listen-ha-lab"
    ha_config = lab / "ha-config"
    ha_config.mkdir(parents=True)

    result = _run(script, "replay", cwd=repo, env=env)

    assert result.returncode == 0, result.stderr
    assert (ha_config / LAB_MARKER_NAME).is_file()
    assert (ha_config / "configuration.yaml").is_file()
    for child in (
        lab,
        ha_config,
        lab / "radio-cache",
        lab / "radio-tmp",
        lab / "radio-music",
        lab / "radio-runtime",
        lab / "vlc-runtime",
        lab / "bridge-runtime",
        lab / "archives",
        lab / "logs",
        lab / "component-backups",
    ):
        assert stat.S_IMODE(child.stat().st_mode) == 0o700
    assert stat.S_IMODE((ha_config / LAB_MARKER_NAME).stat().st_mode) == 0o600
    assert stat.S_IMODE((ha_config / "configuration.yaml").stat().st_mode) == 0o600
    assert not calls.exists()
