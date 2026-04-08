"""Extended tests for mammamiradio/go_librespot_runtime.py — coverage sprint."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from mammamiradio.go_librespot_runtime import (
    _command_matches_expected_state,
    _normalize_path,
    _pid_command,
    _read_state_file,
    build_go_librespot_runtime,
    claim_process,
    main,
    read_owned_pid,
)

# ---------------------------------------------------------------------------
# _normalize_path
# ---------------------------------------------------------------------------


def test_normalize_path_relative():
    """Normalizes a relative path to absolute."""
    result = _normalize_path("foo/bar")
    assert result.is_absolute()


def test_normalize_path_home():
    """Expands ~."""
    result = _normalize_path("~/test")
    assert "~" not in str(result)


# ---------------------------------------------------------------------------
# build_go_librespot_runtime
# ---------------------------------------------------------------------------


def test_build_runtime_deterministic(tmp_path):
    """Same inputs produce same fingerprint."""
    r1 = build_go_librespot_runtime("go-librespot", tmp_path / "cfg", tmp_path / "fifo", 3678, tmp_path / "tmp")
    r2 = build_go_librespot_runtime("go-librespot", tmp_path / "cfg", tmp_path / "fifo", 3678, tmp_path / "tmp")
    assert r1.fingerprint == r2.fingerprint


def test_build_runtime_different_port(tmp_path):
    """Different port produces different fingerprint."""
    r1 = build_go_librespot_runtime("go-librespot", tmp_path / "cfg", tmp_path / "fifo", 3678, tmp_path / "tmp")
    r2 = build_go_librespot_runtime("go-librespot", tmp_path / "cfg", tmp_path / "fifo", 3679, tmp_path / "tmp")
    assert r1.fingerprint != r2.fingerprint


def test_build_runtime_state_file(tmp_path):
    """State file is in tmp_dir."""
    r = build_go_librespot_runtime("go-librespot", tmp_path / "cfg", tmp_path / "fifo", 3678, tmp_path / "tmp")
    assert r.state_file == (tmp_path / "tmp").resolve() / "go-librespot.state.json"


# ---------------------------------------------------------------------------
# _read_state_file
# ---------------------------------------------------------------------------


def test_read_state_file_missing(tmp_path):
    """Returns None for missing file."""
    assert _read_state_file(tmp_path / "nope.json") is None


def test_read_state_file_valid(tmp_path):
    """Reads valid JSON state file."""
    f = tmp_path / "state.json"
    f.write_text('{"pid": 123}')
    assert _read_state_file(f) == {"pid": 123}


def test_read_state_file_invalid_json(tmp_path):
    """Returns None for invalid JSON."""
    f = tmp_path / "state.json"
    f.write_text("not json")
    assert _read_state_file(f) is None


# ---------------------------------------------------------------------------
# claim_process
# ---------------------------------------------------------------------------


def test_claim_process_creates_file(tmp_path):
    """Creates the state file with correct content."""
    state_file = tmp_path / "state.json"
    claim_process(state_file, pid=123, fingerprint="abc", go_librespot_bin="/usr/bin/gl", config_dir=tmp_path / "cfg")

    data = json.loads(state_file.read_text())
    assert data["pid"] == 123
    assert data["fingerprint"] == "abc"


def test_claim_process_creates_parent_dirs(tmp_path):
    """Creates parent directories if they don't exist."""
    state_file = tmp_path / "nested" / "dir" / "state.json"
    claim_process(state_file, pid=1, fingerprint="x", go_librespot_bin="gl", config_dir=tmp_path)
    assert state_file.exists()


# ---------------------------------------------------------------------------
# _pid_command
# ---------------------------------------------------------------------------


def test_pid_command_success():
    """Returns the command string for a running process."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="go-librespot --config_dir /cfg")
        assert _pid_command(123) == "go-librespot --config_dir /cfg"


def test_pid_command_not_found():
    """Returns None for non-existent process."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert _pid_command(99999) is None


def test_pid_command_os_error():
    """Returns None on OSError."""
    with patch("subprocess.run", side_effect=OSError("no ps")):
        assert _pid_command(123) is None


def test_pid_command_empty_stdout():
    """Returns None when stdout is empty."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        assert _pid_command(123) is None


# ---------------------------------------------------------------------------
# _command_matches_expected_state
# ---------------------------------------------------------------------------


def test_command_matches_valid():
    """Matches when command contains the binary and config_dir."""
    assert _command_matches_expected_state(
        "go-librespot --config_dir /home/user/config",
        "go-librespot",
        "/home/user/config",
    )


def test_command_matches_full_path():
    """Matches full path binary."""
    assert _command_matches_expected_state(
        "/usr/bin/go-librespot --config_dir /cfg",
        "/usr/bin/go-librespot",
        "/cfg",
    )


def test_command_no_match():
    """Doesn't match when config_dir is different."""
    assert not _command_matches_expected_state(
        "go-librespot --config_dir /other",
        "go-librespot",
        "/cfg",
    )


def test_command_invalid_shell():
    """Returns False on unparseable command."""
    assert not _command_matches_expected_state(
        "unclosed 'quote",
        "go-librespot",
        "/cfg",
    )


# ---------------------------------------------------------------------------
# read_owned_pid
# ---------------------------------------------------------------------------


def test_read_owned_pid_no_state(tmp_path):
    """Returns None when state file doesn't exist."""
    assert read_owned_pid(tmp_path / "nope.json", "abc") is None


def test_read_owned_pid_fingerprint_mismatch(tmp_path):
    """Returns None and cleans up on fingerprint mismatch."""
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "pid": 123,
                "fingerprint": "old",
                "go_librespot_bin": "gl",
                "config_dir": "/cfg",
            }
        )
    )
    assert read_owned_pid(state_file, "new") is None
    assert not state_file.exists()


def test_read_owned_pid_invalid_state(tmp_path):
    """Returns None and cleans up on missing keys."""
    state_file = tmp_path / "state.json"
    state_file.write_text('{"pid": 123}')
    assert read_owned_pid(state_file, "abc") is None
    assert not state_file.exists()


def test_read_owned_pid_process_gone(tmp_path):
    """Returns None when process command doesn't match."""
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "pid": 99999,
                "fingerprint": "fp",
                "go_librespot_bin": "go-librespot",
                "config_dir": "/cfg",
            }
        )
    )

    with patch("mammamiradio.go_librespot_runtime._pid_command", return_value=None):
        assert read_owned_pid(state_file, "fp") is None


def test_read_owned_pid_valid(tmp_path):
    """Returns the PID when everything matches."""
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "pid": 123,
                "fingerprint": "fp",
                "go_librespot_bin": "go-librespot",
                "config_dir": "/cfg",
            }
        )
    )

    with patch(
        "mammamiradio.go_librespot_runtime._pid_command",
        return_value="go-librespot --config_dir /cfg",
    ):
        assert read_owned_pid(state_file, "fp") == 123


# ---------------------------------------------------------------------------
# main() CLI
# ---------------------------------------------------------------------------


def test_main_claim(tmp_path):
    """CLI 'claim' subcommand creates state file."""
    state_file = str(tmp_path / "state.json")
    assert main(["claim", state_file, "42", "fp123", "go-librespot", str(tmp_path)]) == 0
    assert Path(state_file).exists()


def test_main_owned_pid_found(tmp_path):
    """CLI 'owned-pid' prints the PID."""
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "pid": 42,
                "fingerprint": "fp",
                "go_librespot_bin": "go-librespot",
                "config_dir": "/cfg",
            }
        )
    )

    with patch(
        "mammamiradio.go_librespot_runtime._pid_command",
        return_value="go-librespot --config_dir /cfg",
    ):
        assert main(["owned-pid", str(state_file), "fp"]) == 0


def test_main_invalid_args():
    """CLI with invalid args returns 1."""
    assert main(["invalid"]) == 1


def test_main_claim_invalid_pid(tmp_path):
    """CLI 'claim' with non-numeric PID returns 1."""
    state_file = str(tmp_path / "state.json")
    assert main(["claim", state_file, "not-a-number", "fp", "gl", str(tmp_path)]) == 1
