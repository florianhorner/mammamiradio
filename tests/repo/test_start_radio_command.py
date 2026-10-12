"""The Mac launcher records setup only after it finishes, and retries when it did not."""

from __future__ import annotations

import os
import pty
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import tty
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "Start Radio.command"
ANCHOR = 'Recovery steps: README.md section "Mac launcher recovery".'
RECOVERY = (
    '[ -f "./Start Radio.command" ] && [ -f ./mammamiradio/main.py ] && mkdir -p ./tmp '
    '&& backup_dir="$(mktemp -d "./tmp/venv-backup.XXXXXX")" '
    '&& mv .venv "$backup_dir/environment" && open "./Start Radio.command"'
)
EXPECTED_PIP = [
    "install --upgrade pip setuptools --quiet",
    "install --force-reinstall --require-hashes -r requirements.txt --quiet",
    "install --no-deps -e . --quiet",
    "check",
]
# Before an update changes anything, the launcher checks whether it can download it, and,
# when it cannot, whether the installed components already match requirements.txt.
FETCH_PIP = "install --dry-run --quiet --retries 1 --force-reinstall --require-hashes -r requirements.txt"
INSTALLED_CHECK_PIP = "install --dry-run --quiet --no-index --require-hashes -r requirements.txt"
CONTRACT_MATCH = re.search(r"^SETUP_CONTRACT_VERSION=(\d+)$", LAUNCHER.read_text(), re.M)
assert CONTRACT_MATCH is not None
CONTRACT = CONTRACT_MATCH.group(1)
# The install steps each setup contract stands for. Changing the steps without bumping
# SETUP_CONTRACT_VERSION would leave every recorded environment "ready" without the new step.
CONTRACT_STEPS = {
    "1": [
        "install --upgrade pip setuptools --quiet",
        "install --force-reinstall --require-hashes -r requirements.txt --quiet",
        "install --no-deps -e . --quiet",
        "check",
    ],
}
# Host tools the launcher and the stubs run. Any Python, ffmpeg, Homebrew or lockf on the
# host stays off PATH, so each test decides what exists (CI runners ship Python and ffmpeg).
HOST_TOOLS = (
    "cat",
    "chmod",
    "cksum",
    "cp",
    "grep",
    "head",
    "mkdir",
    "mktemp",
    "mv",
    "perl",
    "rm",
    "seq",
    "sleep",
    "touch",
)


def boundary() -> str:
    text = LAUNCHER.read_text()
    start = text.index("# SETUP-BOUNDARY-START\n") + len("# SETUP-BOUNDARY-START\n")
    end = text.index("# SETUP-BOUNDARY-END\n")
    return text[start:end]


PYTHON_STUB = """\
#!/bin/bash
MINOR="@MINOR@"
: "${STATE:?STATE}"
: "${PIP_LOG:?PIP_LOG}"
: "${VENV_LOG:?VENV_LOG}"
supported() {
  local maj min
  maj=${MINOR%%.*}
  min=${MINOR#*.}
  if [ "$maj" -gt 3 ] || { [ "$maj" -eq 3 ] && [ "$min" -ge 11 ]; }; then
    return 0
  fi
  return 1
}
if [ "${1:-}" = "-c" ]; then
  case "$2" in
    *realpath*)
      prefix=$(builtin cd .venv && /bin/pwd -P) || exit 1
      if [ "${BAD_PREFIX:-}" = "1" ]; then
        prefix="/not/this/checkout"
      fi
      if supported; then ok=1; else ok=0; fi
      printf '%s\\n' "$ok" "${IS_VENV:-1}" "$MINOR" "$prefix"
      exit 0
      ;;
    *%d.%d*)
      printf '%s\\n' "$MINOR"
      exit 0
      ;;
  esac
  printf '%s\\n' "unexpected python -c" >&2
  exit 1
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then
  dest=${3:-.venv}
  printf '%s\\n' "$0 -m venv $dest" >> "$VENV_LOG"
  if [ "${VENV_EXIT:-0}" != "0" ]; then
    exit 1
  fi
  mkdir -p "$dest/bin" "$dest/lib"
  # Like CPython, create the interpreter only where nothing exists yet: an existing
  # file or link, even one that does not run, stays as it is.
  if [ ! -e "$dest/bin/python" ] && [ ! -L "$dest/bin/python" ]; then
    cp "$0" "$dest/bin/python"
    chmod +x "$dest/bin/python"
  fi
  if [ ! -f "$dest/pyvenv.cfg" ]; then
    printf 'version = %s.0\\n' "$MINOR" > "$dest/pyvenv.cfg"
  fi
  # Like CPython, write the activation script for this folder on every run.
  printf 'VIRTUAL_ENV=%q\\nexport VIRTUAL_ENV\\n' "$(builtin cd "$dest" && /bin/pwd -P)" > "$dest/bin/activate"
  exit 0
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "ensurepip" ]; then
  printf '%s\\n' "$0 ensurepip" >> "$VENV_LOG"
  if [ "${ENSUREPIP_FAIL:-}" = "1" ]; then
    printf '%s\\n' "ensurepip failed" >&2
    exit 1
  fi
  : > "$STATE/pip-bootstrapped"
  exit 0
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then
  printf '%s\\n' "$0" >> "$STATE/pip-interpreters"
  shift 2
  if [ "${1:-}" = "--version" ]; then
    if [ "${PIP_MISSING:-}" = "1" ] && [ ! -f "$STATE/pip-bootstrapped" ]; then
      printf '%s\\n' "no pip" >&2
      exit 1
    fi
    printf '%s\\n' "pip 24.0"
    exit 0
  fi
  printf '%s\\n' "$*" >> "$PIP_LOG"
  if [ -n "${PIP_FAIL_ON:-}" ] && [[ "$*" == *"$PIP_FAIL_ON"* ]]; then
    printf '%s\\n' "pip failed" >&2
    exit 1
  fi
  if [ "${PIP_HOLD:-}" = "1" ] && [[ "$*" == *force-reinstall* ]]; then
    printf '%s\\n' "$PPID" > "$STATE/pip-parent.pid"
    printf '%s\\n' "$$" > "$STATE/pip-child.pid"
    : > "$STATE/pip-held"
    while [ ! -f "$STATE/pip-release" ]; do
      /bin/sleep 0.05
    done
  fi
  exit 0
fi
printf '%s\\n' "unexpected python args: $*" >&2
exit 1
"""


def python_stub(minor: str) -> str:
    return PYTHON_STUB.replace("@MINOR@", minor)


LOCKF_SHIM = """\
#!/usr/bin/python3
import fcntl
import os
import sys

args = sys.argv[1:]
with open(os.environ["LOCKF_LOG"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(args) + "\\n")
mode = os.environ.get("LOCKF_MODE", "ok")
if mode == "old":
    # lockf before macOS 15 needs a file and a command.
    sys.stderr.write("usage: lockf [-kns] [-t seconds] file command [arguments]\\n")
    sys.exit(64)
if mode == "error":
    sys.stderr.write("lockf: resource unavailable\\n")
    sys.exit(1)
if mode == "contention":
    sys.exit(75)
if args != ["-s", "-t", "0", "9"]:
    sys.stderr.write("lockf: usage\\n")
    sys.exit(64)
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(75)
except OSError as exc:
    sys.stderr.write(f"lockf: {exc}\\n")
    sys.exit(1)
sys.exit(0)
"""

FFMPEG_STUB = """\
#!/bin/bash
if [ "${FFMPEG_BROKEN:-}" = "1" ] && [ ! -f "$STATE/ffmpeg-ok" ]; then
  printf '%s\\n' "ffmpeg failed" >&2
  exit 1
fi
exit 0
"""

BREW_STUB = """\
#!/bin/bash
printf '%s\\n' "$*" >> "$BREW_LOG"
if [ "${1:-}" = "install" ]; then
  if [ "${BREW_FAIL:-}" = "${2:-}" ]; then
    printf '%s\\n' "brew install failed" >&2
    exit 1
  fi
  # Installs land outside PATH; only the shellenv output puts them on it.
  mkdir -p "$STATE/brewbin"
  if [ "${2:-}" = "python@3.13" ]; then
    cp "$STATE/python-template" "$STATE/brewbin/python3.13"
    chmod +x "$STATE/brewbin/python3.13"
  fi
  if [ "${2:-}" = "ffmpeg" ]; then
    touch "$STATE/ffmpeg-ok"
    cp "$STATE/ffmpeg-template" "$STATE/brewbin/ffmpeg"
    chmod +x "$STATE/brewbin/ffmpeg"
  fi
  exit 0
fi
if [ "${1:-}" = "shellenv" ]; then
  if [ "${SHELLENV_FAIL:-}" = "1" ]; then
    printf '%s\\n' "shellenv failed" >&2
    exit 1
  fi
  printf 'export PATH="%s/brewbin:$PATH"\\n' "$STATE"
  exit 0
fi
printf '%s\\n' "unexpected brew $*" >&2
exit 1
"""

START_STUB = """\
#!/bin/bash
if [ -e /dev/fd/9 ]; then
  printf '%s\\n' FD9_OPEN
else
  printf '%s\\n' FD9_CLOSED
fi >> "$STATE/start-log"
if [ "${START_HOLD:-}" = "1" ]; then
  touch "$STATE/start-held"
  while [ ! -f "$STATE/start-release" ]; do
    /bin/sleep 0.05
  done
fi
exit 0
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


def _wait_for(path: Path, timeout: float = 10) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@dataclass
class Launch:
    returncode: int
    stdout: str
    stderr: str
    pip: list[str]
    brew: list[str]
    venv: list[str]
    lockf: list[str]
    start: str
    opened: str
    interpreters: list[str]

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        with_python: bool = True,
        with_ffmpeg: bool = True,
        lockf: str = "shim",
    ) -> None:
        self.root = tmp_path / "checkout"
        self.root.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.state = tmp_path / "state"
        self.state.mkdir()
        sysbin = tmp_path / "sysbin"
        sysbin.mkdir()
        for tool in HOST_TOOLS + (("lockf",) if lockf == "native" else ()):
            found = shutil.which(tool, path="/usr/bin:/bin")
            assert found is not None, tool
            (sysbin / tool).symlink_to(found)
        (self.root / "Start Radio.command").write_text("# placeholder: tests run boundary() instead\n")
        self.requirements = self.root / "requirements.txt"
        self.pyproject = self.root / "pyproject.toml"
        self.requirements.write_text("requests==1\n")
        self.pyproject.write_text('[project]\nname = "mammamiradio"\nversion = "0"\n')
        (self.root / "mammamiradio").mkdir()
        (self.root / "mammamiradio" / "main.py").write_text("")
        (self.root / ".env").write_text("ANTHROPIC_API_KEY=keep\n")
        music = self.root / "music"
        music.mkdir()
        (music / "track.mp3").write_bytes(b"keep")
        self.pip_log = self.state / "pip.log"
        self.brew_log = self.state / "brew.log"
        self.venv_log = self.state / "venv.log"
        self.lockf_log = self.state / "lockf.log"
        self.env = {
            "PATH": f"{self.bin}:{sysbin}",
            "HOME": str(tmp_path / "home"),
            "TMPDIR": str(self.state),
            "STATE": str(self.state),
            "BIN": str(self.bin),
            "PIP_LOG": str(self.pip_log),
            "BREW_LOG": str(self.brew_log),
            "VENV_LOG": str(self.venv_log),
            "LOCKF_LOG": str(self.lockf_log),
            "PYTHONNOUSERSITE": "1",
        }
        _write(self.state / "python-template", python_stub("3.13"))
        _write(self.state / "ffmpeg-template", FFMPEG_STUB)
        if with_python:
            self.add_python("python3.13", "3.13")
        if with_ffmpeg:
            _write(self.bin / "ffmpeg", FFMPEG_STUB)
        if lockf == "shim":
            # Absolute shebang: the fixture's python3.13 stub must not run this file.
            _write(self.bin / "lockf", LOCKF_SHIM)
        _write(self.bin / "pgrep", '#!/bin/bash\n[ -f "$STATE/station-running" ]\n')
        _write(self.bin / "curl", '#!/bin/bash\nprintf \'%s\\n\' "$*" >> "$STATE/curl-log"\nexit 0\n')
        _write(self.bin / "open", '#!/bin/bash\nprintf \'%s\\n\' "$*" >> "$STATE/open-log"\nexit 0\n')
        _write(self.root / "start.sh", START_STUB)

    def add_python(self, name: str, minor: str) -> None:
        _write(self.bin / name, python_stub(minor))

    def enable_brew(self) -> None:
        _write(self.bin / "brew", BREW_STUB)

    def physical_root(self) -> str:
        return str(self.root.resolve())

    def fingerprint(self, path: Path) -> str:
        output = subprocess.check_output(["/usr/bin/cksum", str(path)], text=True)
        checksum, count, _name = output.split(maxsplit=2)
        return f"{checksum} {count}"

    def marker_text(
        self,
        minor: str = "3.13",
        *,
        contract: str | None = None,
        root: str | None = None,
        requirements: str | None = None,
        pyproject: str | None = None,
    ) -> str:
        return (
            f"contract {CONTRACT if contract is None else contract}\n"
            f"root {self.physical_root() if root is None else root}\n"
            f"python {minor}\n"
            f"requirements {self.fingerprint(self.requirements) if requirements is None else requirements}\n"
            f"pyproject {self.fingerprint(self.pyproject) if pyproject is None else pyproject}\n"
        )

    def write_marker(self, minor: str = "3.13", *, raw: str | None = None, **overrides: str) -> Path:
        path = self.marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw if raw is not None else self.marker_text(minor, **overrides))
        return path

    def make_venv(
        self,
        minor: str = "3.13",
        *,
        python: str = "healthy",
        cfg: bool | str = True,
        activate: bool = True,
    ) -> None:
        venv = self.root / ".venv"
        bin_dir = venv / "bin"
        bin_dir.mkdir(parents=True)
        (venv / "lib").mkdir(exist_ok=True)
        if python == "healthy":
            _write(bin_dir / "python", python_stub(minor))
        elif python == "dead":
            _write(bin_dir / "python", "#!/bin/bash\nexit 1\n")
        elif python == "dangling":
            (bin_dir / "python").symlink_to(self.root / "removed-base" / "python3")
        elif python == "broken-link":
            base = self.root / "broken-base" / "python3"
            _write(base, "#!/bin/bash\nexit 1\n")
            (bin_dir / "python").symlink_to(base)
        elif python != "absent":
            raise AssertionError(python)
        if cfg is True:
            (venv / "pyvenv.cfg").write_text(f"version = {minor}.0\n")
        elif isinstance(cfg, str):
            (venv / "pyvenv.cfg").write_text(cfg)
        if activate:
            self.write_activate(str(venv.resolve()))

    def write_activate(self, venv_dir: str) -> None:
        (self.root / ".venv" / "bin" / "activate").write_text(
            f"VIRTUAL_ENV={shlex.quote(venv_dir)}\nexport VIRTUAL_ENV\n"
        )

    def marker_path(self) -> Path:
        return self.root / ".venv" / ".start-radio-setup-complete"

    def assert_recorded(self, minor: str = "3.13") -> None:
        assert self.marker_path().read_text() == self.marker_text(minor)

    def assert_operator_data(self) -> None:
        assert (self.root / ".env").read_text() == "ANTHROPIC_API_KEY=keep\n"
        assert (self.root / "music" / "track.mp3").read_bytes() == b"keep"

    def _text(self, path: Path) -> str:
        return path.read_text() if path.exists() else ""

    def run(self, cwd: Path | None = None) -> Launch:
        before = {name: self._text(path) for name, path in self._logs().items()}
        try:
            completed = subprocess.run(
                ["/bin/bash", "-c", boundary()],
                cwd=cwd or self.root,
                env=self.env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        finally:
            self.assert_operator_data()
        added = {name: self._text(path)[len(before[name]) :] for name, path in self._logs().items()}
        return Launch(
            completed.returncode,
            completed.stdout,
            completed.stderr,
            pip=added["pip"].splitlines(),
            brew=added["brew"].splitlines(),
            venv=added["venv"].splitlines(),
            lockf=added["lockf"].splitlines(),
            start=added["start"],
            opened=added["open"],
            interpreters=added["interpreters"].splitlines(),
        )

    @contextmanager
    def parked(self, stage: str) -> Iterator[subprocess.Popen[str]]:
        """Run a launch that waits inside the pip or start.sh stub until it is released."""
        proc = subprocess.Popen(
            ["/bin/bash", "-c", boundary()],
            cwd=self.root,
            env={**self.env, f"{stage.upper()}_HOLD": "1"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            _wait_for(self.state / f"{stage}-held")
            yield proc
        finally:
            self.release(stage)
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def release(self, stage: str) -> None:
        (self.state / f"{stage}-release").write_text("go\n")

    def _logs(self) -> dict[str, Path]:
        return {
            "pip": self.pip_log,
            "brew": self.brew_log,
            "venv": self.venv_log,
            "lockf": self.lockf_log,
            "start": self.state / "start-log",
            "open": self.state / "open-log",
            "interpreters": self.state / "pip-interpreters",
        }


def assert_failure(launch: Launch, problem: str) -> None:
    assert launch.returncode == 1, launch.output
    assert f"ERROR: {problem}" in launch.stdout
    assert ANCHOR in launch.stdout
    assert "Press any key" not in launch.stdout
    assert "Dashboard:" not in launch.stdout
    assert "Setup complete!" not in launch.stdout


def assert_handoff(launch: Launch) -> None:
    assert launch.returncode == 0, launch.output
    assert "Dashboard: http://localhost:8000/" in launch.stdout
    assert "FD9_CLOSED" in launch.start
    assert "http://localhost:8000/" in launch.opened


def test_launcher_is_executable_and_parses() -> None:
    # Finder only runs an executable .command file, and the launcher is outside
    # the `shellcheck scripts/*.sh` CI scope.
    assert LAUNCHER.stat().st_mode & 0o111
    result = subprocess.run(["/bin/bash", "-n", str(LAUNCHER)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    text = LAUNCHER.read_text()
    assert "rm -rf" not in text
    assert "rm -r " not in text
    assert "brew list" not in text
    block = boundary()
    assert "zprofile" not in block
    assert "source .env" not in block
    # Helpers are defined before the lock call, so order is checked on the run, not the first mention.
    run = block[block.index("START_ROOT=$(/bin/pwd -P)") :]
    assert run.index("acquire_setup_lock") < run.index("perform_setup")
    assert run.index("exec 9>&-") < run.index("./start.sh")


def test_launcher_avoids_syntax_newer_than_bash_3_2() -> None:
    # CI runs a newer bash, so only a scan catches syntax that macOS /bin/bash 3.2 rejects.
    code = "\n".join(line for line in LAUNCHER.read_text().splitlines() if not line.lstrip().startswith("#"))
    for pattern in (
        r"\bmapfile\b",
        r"\breadarray\b",
        r"\bcoproc\b",
        r"\b(?:declare|local|typeset)\s+-\w*A",
        r"\$\{\w+(?:,,|\^\^)",
        r"\$\{\w+@[QEPAa]\}",
        r"\|&",
        r";;?&",
        r"\[\[\s+-v\b",
    ):
        assert re.search(pattern, code) is None, pattern


def test_setup_contract_matches_the_install_steps() -> None:
    assert CONTRACT_STEPS.get(CONTRACT) == EXPECTED_PIP


def test_harness_path_hides_host_interpreters_and_tools(tmp_path: Path) -> None:
    harness = Harness(tmp_path, with_python=False, with_ffmpeg=False, lockf="none")
    found = subprocess.run(
        ["/bin/bash", "-c", "command -v python3 python3.11 python3.12 python3.13 python ffmpeg brew lockf"],
        env=harness.env,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert found == ""


def test_environment_probe_runs_on_the_test_interpreter() -> None:
    text = LAUNCHER.read_text()
    probe = re.search(r"PY_PROBE_CHECK='(.*?)'", text, re.S)
    minor = re.search(r"PY_MINOR_CHECK='(.*?)'", text)
    assert probe is not None and minor is not None
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    lines = subprocess.check_output([sys.executable, "-c", probe.group(1)], text=True).splitlines()
    assert len(lines) == 4
    assert lines[0] in {"0", "1"}
    assert lines[1] in {"0", "1"}
    assert lines[2] == running
    assert Path(lines[3]).is_dir()
    assert subprocess.check_output([sys.executable, "-c", minor.group(1)], text=True).strip() == running


def test_first_run_installs_the_locked_runtime_and_records_completion(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert "First run. Preparing the station." in launch.stdout
    assert "This can take several minutes. Leave this window open." in launch.stdout
    assert launch.pip == EXPECTED_PIP
    assert launch.brew == []
    assert launch.lockf == ["-s -t 0 9"]
    assert all(item.endswith(".venv/bin/python") for item in launch.interpreters)
    for stage in (
        "Preparing tools...",
        "Preparing the station environment...",
        "Installing station components...",
        "Checking components...",
        "Recording setup...",
        "Setup complete!",
    ):
        assert stage in launch.stdout
    harness.assert_recorded()
    assert harness.marker_path().stat().st_mode & 0o777 == 0o600
    assert_handoff(launch)


def test_finished_setup_is_skipped(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    first = harness.run()
    assert first.returncode == 0, first.output
    recorded = harness.marker_path().read_text()

    second = harness.run()

    assert second.returncode == 0, second.output
    assert second.pip == []
    assert second.venv == []
    assert "First run." not in second.stdout
    assert "Setup complete!" not in second.stdout
    assert "Starting Mamma Mi Radio..." in second.stdout
    assert harness.marker_path().read_text() == recorded
    assert_handoff(second)


@pytest.mark.parametrize(
    ("fail_on", "steps_run", "problem"),
    [
        ("--upgrade", 1, "Updating the installer failed."),
        ("--force-reinstall", 2, "Installing station components failed."),
        ("--no-deps", 3, "Installing the station package failed."),
        ("check", 4, "The installed components are inconsistent."),
    ],
)
def test_failed_setup_runs_again_on_the_next_launch(tmp_path: Path, fail_on: str, steps_run: int, problem: str) -> None:
    harness = Harness(tmp_path)
    harness.env["PIP_FAIL_ON"] = fail_on
    failed = harness.run()

    assert_failure(failed, problem)
    assert "Double-click again. If it fails the same way" in failed.stdout
    assert failed.pip == EXPECTED_PIP[:steps_run]
    assert harness.root.joinpath(".venv").is_dir()
    assert not harness.marker_path().exists()

    del harness.env["PIP_FAIL_ON"]
    retried = harness.run()

    assert retried.returncode == 0, retried.output
    assert "Preparing the existing station environment." in retried.stdout
    assert retried.pip == EXPECTED_PIP
    harness.assert_recorded()
    assert_handoff(retried)


def test_interrupted_install_keeps_the_lock_until_the_installer_exits_and_then_retries(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    with harness.parked("pip") as first:
        assert not harness.marker_path().exists()
        parent = int((harness.state / "pip-parent.pid").read_text())
        child = int((harness.state / "pip-child.pid").read_text())
        assert parent == first.pid
        os.kill(parent, signal.SIGKILL)
        first.wait(timeout=5)
        contender = harness.run()
        assert_failure(contender, "Setup is already running.")
        assert "Do not delete the lock file." in contender.stdout
        assert contender.pip == []
        harness.release("pip")
        deadline = time.time() + 5
        while _alive(child) and time.time() < deadline:
            time.sleep(0.05)
        assert not _alive(child)

    retried = harness.run()
    assert retried.returncode == 0, retried.output
    assert retried.pip == EXPECTED_PIP
    assert harness.marker_path().is_file()
    harness.assert_operator_data()


@pytest.mark.parametrize(
    "lock",
    [
        "lockf-shim",
        "perl-fallback",
        pytest.param(
            "native-lockf",
            marks=pytest.mark.skipif(not Path("/usr/bin/lockf").is_file(), reason="macOS lockf is not installed"),
        ),
    ],
)
def test_concurrent_launch_does_not_install(tmp_path: Path, lock: str) -> None:
    harness = Harness(tmp_path, lockf="native" if lock == "native-lockf" else "shim")
    if lock == "perl-fallback":
        harness.env["LOCKF_MODE"] = "old"
    with harness.parked("pip") as first:
        contender = harness.run()
        assert_failure(contender, "Setup is already running.")
        assert "Let it finish, then double-click again. Do not delete the lock file." in contender.stdout
        assert contender.pip == []
        assert harness.pip_log.read_text().count("force-reinstall") == 1
        harness.release("pip")
        stdout, stderr = first.communicate(timeout=10)
        assert first.returncode == 0, stdout + stderr
        assert harness.pip_log.read_text().count("force-reinstall") == 1
        assert harness.marker_path().is_file()
    harness.assert_operator_data()


def test_second_launch_during_station_start_is_not_blocked_by_the_setup_lock(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    with harness.parked("start"):
        (harness.state / "station-running").write_text("1\n")
        second = harness.run()
        assert second.returncode == 0, second.output
        assert "Radio already running." in second.stdout
        assert "Setup is already running." not in second.stdout
        assert second.pip == []
        assert "FD9_CLOSED" in (harness.state / "start-log").read_text()


@pytest.mark.parametrize("contract", ["0", "999"])
def test_old_or_unknown_setup_contract_runs_preparation_again(tmp_path: Path, contract: str) -> None:
    harness = Harness(tmp_path)
    harness.make_venv()
    harness.write_marker(contract=contract)
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert "Setup steps changed." in launch.stdout
    assert launch.pip == [FETCH_PIP, *EXPECTED_PIP]
    harness.assert_recorded()


@pytest.mark.parametrize("target", ["requirements.txt", "pyproject.toml"])
def test_changed_input_runs_preparation_again(tmp_path: Path, target: str) -> None:
    harness = Harness(tmp_path)
    harness.make_venv()
    harness.write_marker()
    with (harness.root / target).open("a") as handle:
        handle.write("# changed\n")

    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert "Station components changed." in launch.stdout
    assert launch.pip == [FETCH_PIP, *EXPECTED_PIP]
    harness.assert_recorded()


@pytest.mark.parametrize("target", ["requirements.txt", "pyproject.toml"])
def test_input_changed_during_install_blocks_startup(tmp_path: Path, target: str) -> None:
    harness = Harness(tmp_path)
    with harness.parked("pip") as first:
        with (harness.root / target).open("a") as handle:
            handle.write("# changed during install\n")
        harness.release("pip")
        stdout, stderr = first.communicate(timeout=10)

    assert first.returncode == 1, stdout + stderr
    assert "The project files changed during setup." in stdout
    assert target in stdout
    assert "Dashboard:" not in stdout
    assert not harness.marker_path().exists()
    harness.assert_operator_data()

    retried = harness.run()
    assert retried.returncode == 0, retried.output
    harness.assert_recorded()


def test_legacy_environment_without_a_marker_is_brought_onto_the_lock(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.make_venv()
    (harness.root / ".venv" / "lib" / "keep-me").write_text("keep\n")
    orphan = harness.root / ".venv" / ".start-radio-setup-complete.ORPHAN"
    orphan.write_text("leave me\n")

    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert "Preparing the existing station environment." in launch.stdout
    assert launch.venv == []
    assert launch.pip == EXPECTED_PIP
    harness.assert_recorded()
    assert (harness.root / ".venv" / "lib" / "keep-me").read_text() == "keep\n"
    assert orphan.read_text() == "leave me\n"


@pytest.mark.parametrize("raw", ["", "hello\n", "contract 1\n", "$(touch pwned)\n"])
def test_malformed_marker_prepares_the_existing_environment_without_executing_it(tmp_path: Path, raw: str) -> None:
    harness = Harness(tmp_path)
    harness.make_venv()
    harness.write_marker(raw=raw)
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert "Preparing the existing station environment." in launch.stdout
    assert "Setup steps changed." not in launch.stdout
    assert launch.pip == EXPECTED_PIP
    assert not (harness.root / "pwned").exists()


def test_marker_root_is_not_executed_as_shell(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.make_venv()
    harness.write_marker(root=f"{harness.physical_root()}; touch pwned")
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert "This station folder moved." in launch.stdout
    assert not (harness.root / "pwned").exists()
    harness.assert_recorded()


def test_moved_checkout_is_prepared_again_in_place(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.make_venv()
    old_venv = "/the/old/place/of/the/checkout/.venv"
    harness.write_marker(root="/the/old/place/of/the/checkout")
    # The activation script still names the folder the environment was made in.
    harness.write_activate(old_venv)
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert "This station folder moved." in launch.stdout
    assert launch.venv[0].endswith("python3.13 -m venv .venv")
    assert launch.pip == EXPECTED_PIP
    harness.assert_recorded()
    assert old_venv not in (harness.root / ".venv" / "bin" / "activate").read_text()
    assert_handoff(launch)


@pytest.mark.parametrize("original", ["moved away", "copied, still there"])
def test_unrecorded_environment_from_another_folder_is_rebuilt_in_place(tmp_path: Path, original: str) -> None:
    harness = Harness(tmp_path)
    harness.make_venv()
    other = tmp_path / "original-checkout" / ".venv"
    if original == "copied, still there":
        other.mkdir(parents=True)
    harness.write_activate(str(other))
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert launch.venv[0].endswith("python3.13 -m venv .venv")
    assert launch.pip == EXPECTED_PIP
    harness.assert_recorded()


def test_update_that_cannot_be_downloaded_keeps_playing_the_recorded_setup(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.make_venv()
    recorded = harness.write_marker().read_text()
    with harness.requirements.open("a") as handle:
        handle.write("# changed\n")
    # Offline, real pip still reports an installed pip as current, so only the download
    # probe fails here; the upgrade step would have passed.
    harness.env["PIP_FAIL_ON"] = "--retries"
    offline = harness.run()

    assert offline.returncode == 0, offline.output
    assert "Setup could not download the station update." in offline.stdout
    assert offline.pip == [FETCH_PIP, INSTALLED_CHECK_PIP]
    assert "Setup complete!" not in offline.stdout
    assert harness.marker_path().read_text() == recorded
    assert_handoff(offline)

    del harness.env["PIP_FAIL_ON"]
    online = harness.run()
    assert online.returncode == 0, online.output
    assert online.pip == [FETCH_PIP, *EXPECTED_PIP]
    harness.assert_recorded()


def test_update_that_needs_new_components_offline_stops_and_keeps_the_record(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.make_venv()
    recorded = harness.write_marker().read_text()
    with harness.requirements.open("a") as handle:
        handle.write("# changed\n")
    # Neither the download nor the installed components match: the new code would run
    # against packages it does not have, so the old setup must not start.
    harness.env["PIP_FAIL_ON"] = "--dry-run"
    launch = harness.run()

    assert_failure(launch, "Setup could not download the station update.")
    assert "Connect to the internet, then double-click again." in launch.stdout
    assert launch.pip == [FETCH_PIP, INSTALLED_CHECK_PIP]
    assert launch.start == ""
    assert harness.marker_path().read_text() == recorded


def test_update_that_fails_after_changing_the_setup_stops(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.make_venv()
    harness.write_marker()
    with harness.requirements.open("a") as handle:
        handle.write("# changed\n")
    harness.env["PIP_FAIL_ON"] = "install --force-reinstall"
    launch = harness.run()

    assert_failure(launch, "Installing station components failed.")
    assert launch.pip == [FETCH_PIP, *EXPECTED_PIP[:2]]
    assert not harness.marker_path().exists()


def test_unsupported_venv_python_stops_before_mutation(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.make_venv("3.10")
    launch = harness.run()

    assert_failure(launch, "The station environment uses Python older than 3.11.")
    assert "Python 3.10" in launch.stdout
    assert launch.pip == []
    assert launch.venv == []
    assert not harness.marker_path().exists()


def test_unsupported_cfg_stops_before_mutation(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.make_venv(python="dead", cfg="version = 3.10.8\n")
    launch = harness.run()

    assert_failure(launch, "The station environment uses Python older than 3.11.")
    assert "pyvenv.cfg names Python 3.10." in launch.stdout
    assert launch.venv == []
    assert launch.pip == []


def test_unknown_provenance_stops_before_mutation(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.make_venv(python="absent", cfg=False, activate=False)
    launch = harness.run()

    assert_failure(launch, "Setup could not prepare the station environment.")
    assert ".venv/bin/python or pyvenv.cfg" in launch.stdout
    assert launch.venv == []
    assert launch.pip == []


def test_same_minor_repair_uses_the_matching_base(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.add_python("python3.14", "3.14")
    harness.make_venv(python="dead", cfg="version = 3.13.2\n", activate=False)
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert launch.venv
    assert launch.venv[0].endswith("python3.13 -m venv .venv")
    assert "python3.14" not in "\n".join(launch.venv)
    harness.assert_recorded()


def test_newer_python_does_not_replace_a_healthy_older_venv(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.make_venv("3.12")
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert launch.venv == []
    assert all(item.endswith(".venv/bin/python") for item in launch.interpreters)
    harness.assert_recorded("3.12")


def test_mismatched_minor_stops_before_mutation(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.make_venv(python="dead", cfg="version = 3.12.1\n", activate=False)
    launch = harness.run()

    assert_failure(launch, "Setup could not prepare the station environment.")
    assert "Python is 3.13" in launch.stdout
    assert "Python 3.12" in launch.stdout
    assert launch.venv == []
    assert launch.pip == []


@pytest.mark.parametrize(("cfg", "activate"), [(True, False), (False, True), (False, False)])
def test_missing_activation_or_config_is_repaired_from_the_working_interpreter(
    tmp_path: Path, cfg: bool, activate: bool
) -> None:
    harness = Harness(tmp_path, with_python=False)
    harness.make_venv(cfg=cfg, activate=activate)
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert launch.venv[0].endswith(".venv/bin/python -m venv .venv")
    assert harness.marker_path().is_file()


def test_python_selection_skips_interpreters_older_than_3_11(tmp_path: Path) -> None:
    harness = Harness(tmp_path, with_python=False)
    harness.add_python("python3.13", "3.10")
    harness.add_python("python3.12", "3.12")
    harness.add_python("python3", "3.13")
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert launch.venv[0].endswith("python3.12 -m venv .venv")
    harness.assert_recorded("3.12")


def test_missing_python_without_homebrew_stops_before_creating_a_venv(tmp_path: Path) -> None:
    harness = Harness(tmp_path, with_python=False)
    launch = harness.run()

    assert_failure(launch, "Mamma Mi Radio needs Python 3.11 or newer, and this Mac has none.")
    assert "then double-click again" in launch.stdout
    assert not (harness.root / ".venv").exists()
    assert launch.brew == []


def test_homebrew_installs_only_missing_ffmpeg(tmp_path: Path) -> None:
    harness = Harness(tmp_path, with_ffmpeg=False)
    harness.enable_brew()
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert launch.brew == ["install ffmpeg", "shellenv"]


def test_homebrew_installs_only_missing_python(tmp_path: Path) -> None:
    harness = Harness(tmp_path, with_python=False)
    harness.enable_brew()
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert launch.brew == ["install python@3.13", "shellenv"]


def test_homebrew_is_not_used_when_prerequisites_exist(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.enable_brew()
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert launch.brew == []


def test_brew_install_failure_stops_before_the_venv(tmp_path: Path) -> None:
    harness = Harness(tmp_path, with_ffmpeg=False)
    harness.enable_brew()
    harness.env["BREW_FAIL"] = "ffmpeg"
    launch = harness.run()

    assert_failure(launch, "Installing ffmpeg failed.")
    assert "brew install failed" in launch.stderr
    assert not (harness.root / ".venv").exists()
    assert launch.pip == []


@pytest.mark.parametrize("with_brew", [False, True])
def test_broken_ffmpeg_on_a_ready_environment_does_not_mutate(tmp_path: Path, with_brew: bool) -> None:
    harness = Harness(tmp_path)
    if with_brew:
        harness.enable_brew()
    harness.make_venv()
    original = harness.write_marker()
    before = original.read_text()
    harness.env["FFMPEG_BROKEN"] = "1"
    launch = harness.run()

    assert_failure(launch, "ffmpeg is not usable, so the station cannot start.")
    assert "ffmpeg failed" in launch.stdout
    assert launch.pip == []
    assert launch.brew == []
    assert launch.venv == []
    assert original.read_text() == before


def test_broken_ffmpeg_during_prep_without_homebrew_stops(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.env["FFMPEG_BROKEN"] = "1"
    launch = harness.run()

    assert_failure(launch, "ffmpeg is not usable.")
    assert "Homebrew is not available" in launch.stdout
    assert not (harness.root / ".venv").exists()
    assert launch.pip == []


def test_brew_repairs_broken_ffmpeg_before_recording_setup(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.enable_brew()
    harness.env["FFMPEG_BROKEN"] = "1"
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert "install ffmpeg" in launch.brew
    assert harness.marker_path().is_file()


@pytest.mark.parametrize("kind", ["missing", "changed", "contract"])
def test_running_station_blocks_preparation(tmp_path: Path, kind: str) -> None:
    harness = Harness(tmp_path)
    harness.make_venv()
    if kind == "changed":
        path = harness.write_marker(requirements="0 0")
    elif kind == "contract":
        path = harness.write_marker(contract="0")
    else:
        path = None
    before = path.read_text() if path else None
    (harness.state / "station-running").write_text("1\n")
    launch = harness.run()

    assert_failure(launch, "The station is running, so setup will not change its environment.")
    assert "Stop it in its own window, then double-click again." in launch.stdout
    assert launch.pip == []
    assert launch.brew == []
    assert launch.venv == []
    if path is not None and before is not None:
        assert path.read_text() == before


def test_ready_station_that_is_already_running_opens_the_dashboard(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.enable_brew()
    harness.make_venv()
    original = harness.write_marker()
    before = original.read_text()
    (harness.state / "station-running").write_text("1\n")
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert "Radio already running." in launch.stdout
    assert launch.pip == []
    assert launch.brew == []
    assert launch.start == ""
    assert "http://localhost:8000/" in launch.opened
    assert original.read_text() == before


def test_lock_contention_message(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.env["LOCKF_MODE"] = "contention"
    launch = harness.run()

    assert_failure(launch, "Setup is already running.")
    assert launch.pip == []


def test_lockf_infrastructure_failure(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.env["LOCKF_MODE"] = "error"
    launch = harness.run()

    assert_failure(launch, "Setup could not start.")
    assert "exit 1" in launch.stdout
    assert "resource unavailable" in launch.stdout
    assert launch.pip == []


def test_missing_lockf_stops_before_setup(tmp_path: Path) -> None:
    harness = Harness(tmp_path, lockf="none")
    launch = harness.run()

    assert_failure(launch, "Setup could not start.")
    assert "lockf command is not available" in launch.stdout
    assert not (harness.root / ".venv").exists()


def test_lock_parent_is_not_a_directory(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    (harness.root / "tmp").write_text("not a directory\n")
    launch = harness.run()

    assert_failure(launch, "Setup could not start.")
    assert "could not create tmp/" in launch.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_lock_file_cannot_be_opened(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    lock_dir = harness.root / "tmp"
    lock_dir.mkdir()
    lock_dir.chmod(0o500)
    try:
        launch = harness.run()
    finally:
        lock_dir.chmod(0o700)

    assert_failure(launch, "Setup could not start.")
    assert "could not open its lock file" in launch.stdout


@pytest.mark.parametrize("filename", ["requirements.txt", "pyproject.toml"])
def test_unreadable_project_file_stops_before_install(tmp_path: Path, filename: str) -> None:
    harness = Harness(tmp_path)
    (harness.root / filename).unlink()
    launch = harness.run()

    assert_failure(launch, "Setup could not read the project files.")
    assert f"Setup could not read {filename}." in launch.stdout
    assert not (harness.root / ".venv").exists()


def test_cksum_failure_stops_before_install(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    _write(harness.bin / "cksum", "#!/bin/bash\nprintf '%s\\n' 'cksum: I/O error' >&2\nexit 1\n")
    launch = harness.run()

    assert_failure(launch, "Setup could not check the project files.")
    assert "cksum requirements.txt did not succeed." in launch.stdout
    assert not (harness.root / ".venv").exists()


def test_marker_creation_failure_blocks_startup(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    _write(harness.bin / "mktemp", "#!/bin/bash\nprintf '%s\\n' 'No space left on device' >&2\nexit 1\n")
    launch = harness.run()

    assert_failure(launch, "Recording setup failed.")
    assert "No space left on device" in launch.stdout
    assert "installed and checked" in launch.stdout
    assert launch.pip == EXPECTED_PIP
    assert not harness.marker_path().exists()


def test_marker_write_failure_keeps_unrelated_files_and_retries(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    _write(
        harness.bin / "mktemp",
        '#!/bin/bash\nname=$(/usr/bin/mktemp -d "./.venv/.start-radio-setup-complete.XXXXXX") || exit 1\n'
        "printf '%s\\n' \"$name\"\n",
    )
    with harness.parked("pip") as first:
        keep = harness.root / ".venv" / "lib" / "keep-me"
        keep.write_text("keep\n")
        harness.release("pip")
        stdout, stderr = first.communicate(timeout=10)

    assert first.returncode == 1, stdout + stderr
    assert "Recording setup failed." in stdout
    assert not harness.marker_path().exists()
    assert keep.read_text() == "keep\n"
    orphans = list((harness.root / ".venv").glob(".start-radio-setup-complete.*"))
    assert orphans
    harness.assert_operator_data()

    (harness.bin / "mktemp").unlink()
    retried = harness.run()
    assert retried.returncode == 0, retried.output
    assert harness.marker_path().is_file()
    assert keep.read_text() == "keep\n"
    assert orphans[0].exists()


def test_marker_rename_failure_removes_only_its_temp_file(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    _write(
        harness.bin / "mv",
        "#!/bin/bash\n"
        'if [ "${2:-}" = ".venv/.start-radio-setup-complete" ]; then\n'
        "  printf '%s\\n' 'mv: Operation not permitted' >&2\n"
        "  exit 1\n"
        "fi\n"
        'exec /bin/mv "$@"\n',
    )
    launch = harness.run()

    assert_failure(launch, "Recording setup failed.")
    assert "Operation not permitted" in launch.stdout
    assert not harness.marker_path().exists()
    assert list((harness.root / ".venv").glob(".start-radio-setup-complete*")) == []


def test_ensurepip_bootstraps_a_venv_without_pip(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.env["PIP_MISSING"] = "1"
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert any(line.endswith("ensurepip") for line in launch.venv)
    assert launch.pip == EXPECTED_PIP
    assert harness.marker_path().is_file()


def test_venv_create_failure_stops_before_pip(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.env["VENV_EXIT"] = "1"
    launch = harness.run()

    assert_failure(launch, "Setup could not prepare the station environment.")
    assert "-m venv .venv did not succeed" in launch.stdout
    assert launch.pip == []
    assert not harness.marker_path().exists()


def test_venv_that_is_not_a_directory_stops(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    (harness.root / ".venv").write_text("nope\n")
    launch = harness.run()

    assert_failure(launch, "Setup could not prepare the station environment.")
    assert ".venv exists and is not a directory." in launch.stdout
    assert launch.pip == []


def test_recorded_python_mismatch_prepares_again(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.make_venv("3.13")
    harness.write_marker("3.12")
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert "recorded Python does not match" in launch.stdout
    assert launch.venv == []
    assert launch.pip == EXPECTED_PIP
    harness.assert_recorded("3.13")


@pytest.mark.timeout(60)
def test_failed_window_waiting_for_a_key_does_not_block_a_retry(tmp_path: Path) -> None:
    harness = Harness(tmp_path, with_ffmpeg=False)
    terminal, window = pty.openpty()
    # bash 3.2's `read -n 1` only sees a key on this pty in raw mode; Terminal delivers it either way.
    tty.setraw(window)
    failed = subprocess.Popen(
        ["/bin/bash", "-c", boundary()],
        cwd=harness.root,
        env=harness.env,
        stdin=window,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    os.close(window)
    try:
        assert failed.stdout is not None
        seen = ""
        while "Press any key to close." not in seen:
            line = failed.stdout.readline()
            assert line, seen
            seen += line
        assert "ERROR: ffmpeg is not usable." in seen
        assert failed.poll() is None

        _write(harness.bin / "ffmpeg", FFMPEG_STUB)
        retried = harness.run()
        assert retried.returncode == 0, retried.output
        assert "Setup is already running." not in retried.output

        os.write(terminal, b"x")
        assert failed.wait(timeout=5) == 1
    finally:
        if failed.poll() is None:
            failed.kill()
            failed.wait(timeout=5)
        os.close(terminal)


@pytest.mark.parametrize(
    "leftover",
    [
        {"CDPATH": ".:/tmp"},
        {"SHELLOPTS": "noclobber"},
        # errexit would end the script at the first refused lockf form, before the fallback.
        {"SHELLOPTS": "errexit", "LOCKF_MODE": "old"},
        {"BASH_FUNC_cd%%": '() { builtin cd "$@" && echo "now in $PWD"; }'},
    ],
    ids=["cdpath", "noclobber", "errexit", "cd-function"],
)
def test_shell_profile_leftovers_keep_a_ready_environment_ready(tmp_path: Path, leftover: dict[str, str]) -> None:
    harness = Harness(tmp_path)
    harness.make_venv()
    harness.write_marker()
    # Scratch output an earlier launch left behind, which noclobber refuses to overwrite.
    _write(harness.root / "tmp" / "start-radio-lockf.err", "")
    harness.env.update(leftover)
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert launch.pip == []
    assert launch.venv == []
    assert_handoff(launch)


@pytest.mark.parametrize("python", ["dangling", "broken-link"])
def test_interpreter_link_that_does_not_run_is_relinked_from_the_matching_base(tmp_path: Path, python: str) -> None:
    harness = Harness(tmp_path)
    harness.make_venv(python=python, cfg="version = 3.13.1\n")
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert launch.venv[0].endswith("python3.13 -m venv .venv")
    assert launch.pip == EXPECTED_PIP
    harness.assert_recorded()


@pytest.mark.parametrize(("knob", "value"), [("IS_VENV", "0"), ("BAD_PREFIX", "1")])
def test_interpreter_that_is_not_this_checkouts_venv_is_never_installed_into(
    tmp_path: Path, knob: str, value: str
) -> None:
    harness = Harness(tmp_path)
    harness.make_venv()
    harness.write_marker()
    harness.env[knob] = value
    launch = harness.run()

    assert_failure(launch, "Setup could not prepare the station environment.")
    assert "still did not pass the environment check" in launch.stdout
    assert launch.pip == []
    assert not harness.marker_path().exists()


def test_lockf_without_descriptor_support_falls_back_to_perl(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.env["LOCKF_MODE"] = "old"
    launch = harness.run()

    assert launch.lockf == ["-s -t 0 9"]
    assert launch.pip == EXPECTED_PIP
    harness.assert_recorded()
    assert_handoff(launch)


def test_checkout_entered_through_a_symlink_records_the_physical_path(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(harness.root)
    # A terminal keeps the symlinked spelling in PWD.
    harness.env["PWD"] = str(alias)
    first = harness.run(cwd=alias)
    assert first.returncode == 0, first.output
    harness.assert_recorded()

    second = harness.run(cwd=alias)
    assert second.returncode == 0, second.output
    assert second.pip == []


def test_only_an_old_python_stops_before_creating_a_venv(tmp_path: Path) -> None:
    harness = Harness(tmp_path, with_python=False)
    # Apple's /usr/bin/python3 is 3.9.
    harness.add_python("python3", "3.9")
    launch = harness.run()

    assert_failure(launch, "Mamma Mi Radio needs Python 3.11 or newer, and this Mac has none.")
    assert launch.venv == []
    assert not (harness.root / ".venv").exists()


@pytest.mark.parametrize(
    ("missing", "problem", "cause"),
    [
        (
            "python",
            "Mamma Mi Radio needs Python 3.11 or newer, and this Mac has none.",
            "still found no supported Python on PATH",
        ),
        ("ffmpeg", "ffmpeg is not usable.", "brew install ffmpeg finished, and ffmpeg is still not usable."),
    ],
)
def test_brew_that_installs_nothing_stops_before_the_venv(
    tmp_path: Path, missing: str, problem: str, cause: str
) -> None:
    harness = Harness(tmp_path, with_python=missing != "python", with_ffmpeg=missing != "ffmpeg")
    _write(harness.bin / "brew", '#!/bin/bash\nprintf \'%s\\n\' "$*" >> "$BREW_LOG"\nexit 0\n')
    launch = harness.run()

    assert_failure(launch, problem)
    assert cause in launch.stdout
    assert launch.pip == []
    assert not (harness.root / ".venv").exists()


def test_ffmpeg_that_exits_127_reports_its_own_error(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    _write(harness.bin / "ffmpeg", "#!/bin/bash\nprintf '%s\\n' 'dyld: Library not loaded' >&2\nexit 127\n")
    launch = harness.run()

    assert_failure(launch, "ffmpeg is not usable.")
    assert "dyld: Library not loaded" in launch.stdout
    assert "not found on PATH" not in launch.stdout


def test_ffmpeg_that_breaks_during_install_blocks_the_record(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    with harness.parked("pip") as first:
        _write(harness.bin / "ffmpeg", "#!/bin/bash\nexit 1\n")
        harness.release("pip")
        stdout, stderr = first.communicate(timeout=10)

    assert first.returncode == 1, stdout + stderr
    assert "ERROR: ffmpeg is not usable, so setup did not finish." in stdout
    assert not harness.marker_path().exists()


def test_checkout_moved_during_install_blocks_the_record(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    moved = tmp_path / "moved"
    with harness.parked("pip") as first:
        harness.root.rename(moved)
        harness.release("pip")
        stdout, stderr = first.communicate(timeout=10)
    moved.rename(harness.root)

    assert first.returncode == 1, stdout + stderr
    assert "The checkout path changed while components were installing." in stdout
    assert not harness.marker_path().exists()
    harness.assert_operator_data()


def test_station_check_matches_how_start_sh_runs_the_station() -> None:
    pattern = re.search(r'pgrep -f "([^"]+)"', boundary())
    assert pattern is not None
    runs = [
        line
        for line in (ROOT / "start.sh").read_text().splitlines()
        if "-m uvicorn" in line and not line.lstrip().startswith("#")
    ]
    assert runs
    assert all(re.search(pattern.group(1), line) for line in runs)


def test_station_port_comes_from_the_environment(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.env["MAMMAMIRADIO_PORT"] = "8123"
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert "Dashboard: http://localhost:8123/" in launch.stdout
    assert launch.opened == "http://localhost:8123/\n"
    assert "http://localhost:8123/listen" in (harness.state / "curl-log").read_text()


def test_ensurepip_failure_stops_before_install(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.env.update(PIP_MISSING="1", ENSUREPIP_FAIL="1")
    launch = harness.run()

    assert_failure(launch, "The station environment has no pip.")
    assert "ensurepip did not succeed" in launch.stdout
    assert launch.pip == []
    assert not harness.marker_path().exists()


def test_brew_shellenv_failure_stops_before_the_venv(tmp_path: Path) -> None:
    harness = Harness(tmp_path, with_ffmpeg=False)
    harness.enable_brew()
    harness.env["SHELLENV_FAIL"] = "1"
    launch = harness.run()

    assert_failure(launch, "Installing tools failed.")
    assert "brew shellenv did not succeed. shellenv failed" in launch.stdout
    assert launch.brew == ["install ffmpeg", "shellenv"]
    assert not (harness.root / ".venv").exists()


def test_setup_record_that_is_not_a_file_stops_before_mutation(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.make_venv()
    harness.marker_path().mkdir()
    launch = harness.run()

    assert_failure(launch, "Setup could not remove the previous setup record.")
    assert "is not a regular file" in launch.stdout
    assert launch.pip == []
    assert launch.venv == []


def test_environment_without_any_matching_interpreter_stops(tmp_path: Path) -> None:
    harness = Harness(tmp_path, with_python=False)
    harness.make_venv(python="dead", cfg="version = 3.13.0\n", activate=False)
    launch = harness.run()

    assert_failure(launch, "Setup could not prepare the station environment.")
    assert "No Python 3.13 interpreter is available to repair this environment." in launch.stdout
    assert launch.venv == []
    assert launch.pip == []


def test_probe_prefix_matches_the_launchers_path_check_on_a_real_venv(tmp_path: Path) -> None:
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(tmp_path / ".venv")], check=True)
    probe = re.search(r"PY_PROBE_CHECK='(.*?)'", LAUNCHER.read_text(), re.S)
    assert probe is not None
    lines = subprocess.check_output([".venv/bin/python", "-c", probe.group(1)], cwd=tmp_path, text=True).splitlines()
    expected = subprocess.check_output(["/bin/pwd", "-P"], cwd=tmp_path / ".venv", text=True).strip()
    assert lines[:2] == ["1", "1"]
    assert lines[3] == expected


@pytest.mark.parametrize("name", ["My Radio (v2)", "Radio Stazione ü"])
def test_checkout_path_with_spaces_or_accents_is_recorded_and_skipped(tmp_path: Path, name: str) -> None:
    base = tmp_path / name
    base.mkdir()
    harness = Harness(base)
    first = harness.run()
    assert first.returncode == 0, first.output
    harness.assert_recorded()

    second = harness.run()
    assert second.returncode == 0, second.output
    assert second.pip == []
    assert "Setup complete!" not in second.stdout


def test_checkout_entered_with_different_letter_case_stays_ready(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    first = harness.run()
    assert first.returncode == 0, first.output
    other_case = harness.root.with_name(harness.root.name.upper())
    if not other_case.exists():
        pytest.skip("case-sensitive file system")
    # A terminal keeps the spelling the user typed in PWD; the launcher must not record it.
    harness.env["PWD"] = str(other_case)
    second = harness.run(cwd=other_case)

    assert second.returncode == 0, second.output
    assert second.pip == []
    assert "This station folder moved." not in second.stdout


def test_missing_ffmpeg_on_a_ready_environment_stops_before_starting(tmp_path: Path) -> None:
    harness = Harness(tmp_path, with_ffmpeg=False)
    harness.make_venv()
    harness.write_marker()
    launch = harness.run()

    assert_failure(launch, "ffmpeg is not usable, so the station cannot start.")
    assert "ffmpeg was not found on PATH." in launch.stdout
    assert launch.start == ""


def test_homebrew_installs_python_and_ffmpeg_together_on_a_bare_mac(tmp_path: Path) -> None:
    harness = Harness(tmp_path, with_python=False, with_ffmpeg=False)
    harness.enable_brew()
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert launch.brew == ["install python@3.13", "install ffmpeg", "shellenv"]
    assert launch.venv[0].endswith("python3.13 -m venv .venv")
    assert launch.pip == EXPECTED_PIP
    harness.assert_recorded()
    assert_handoff(launch)


@pytest.mark.parametrize(
    "overrides",
    [
        {"contract": "one"},
        {"root": ""},
        {"minor": "3.13.1"},
        {"minor": "latest"},
        {"requirements": "123"},
        {"requirements": "1 2 3"},
        {"pyproject": "abc def"},
    ],
    ids=[
        "contract-not-a-number",
        "root-empty",
        "python-with-patch",
        "python-not-a-version",
        "requirements-one-number",
        "requirements-three-numbers",
        "pyproject-not-numbers",
    ],
)
def test_setup_record_with_five_lines_but_a_bad_value_is_not_trusted(tmp_path: Path, overrides: dict[str, str]) -> None:
    harness = Harness(tmp_path)
    harness.make_venv()
    harness.write_marker(**overrides)
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert "Preparing the existing station environment." in launch.stdout
    assert launch.venv == []
    assert launch.pip == EXPECTED_PIP
    harness.assert_recorded()


def test_offline_update_keeps_playing_when_the_setup_steps_changed(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.make_venv()
    recorded = harness.write_marker(contract="0").read_text()
    harness.env["PIP_FAIL_ON"] = "--retries"
    offline = harness.run()

    assert offline.returncode == 0, offline.output
    assert "Setup steps changed." in offline.stdout
    assert "Setup could not download the station update." in offline.stdout
    assert offline.pip == [FETCH_PIP, INSTALLED_CHECK_PIP]
    assert offline.venv == []
    assert harness.marker_path().read_text() == recorded
    assert_handoff(offline)

    del harness.env["PIP_FAIL_ON"]
    online = harness.run()
    assert online.returncode == 0, online.output
    assert online.pip == [FETCH_PIP, *EXPECTED_PIP]
    harness.assert_recorded()


@pytest.mark.parametrize("kind", ["moved", "no record", "other python"])
def test_offline_update_never_keeps_an_environment_without_a_matching_record(tmp_path: Path, kind: str) -> None:
    # Only a recorded setup that still matches this folder may keep playing.
    harness = Harness(tmp_path)
    harness.make_venv()
    if kind == "moved":
        harness.write_marker(root="/the/old/place/of/the/checkout")
        harness.write_activate("/the/old/place/of/the/checkout/.venv")
    elif kind == "other python":
        harness.write_marker("3.12")
    harness.env["PIP_FAIL_ON"] = "--upgrade"
    launch = harness.run()

    assert_failure(launch, "Updating the installer failed.")
    assert launch.pip == EXPECTED_PIP[:1]
    assert launch.start == ""
    assert launch.opened == ""
    assert not harness.marker_path().exists()


def test_activation_script_naming_the_checkout_through_a_symlink_stays_ready(tmp_path: Path) -> None:
    # The same folder under another spelling is not a moved checkout.
    harness = Harness(tmp_path)
    harness.make_venv()
    harness.write_marker()
    alias = tmp_path / "alias"
    alias.symlink_to(harness.root)
    harness.write_activate(str(alias / ".venv"))
    launch = harness.run()

    assert launch.returncode == 0, launch.output
    assert "Preparing" not in launch.stdout
    assert launch.pip == []
    assert launch.venv == []
    assert_handoff(launch)


@pytest.mark.parametrize(
    ("refusals", "expected"),
    [
        (2, ["curl", "sleep", "curl", "sleep", "curl"]),
        (999, ["curl", "sleep"] * 30),
    ],
    ids=["answers-on-the-third-try", "never-answers"],
)
def test_slow_station_start_is_waited_for_before_the_dashboard_opens(
    tmp_path: Path, refusals: int, expected: list[str]
) -> None:
    harness = Harness(tmp_path)
    harness.make_venv()
    harness.write_marker()
    events = harness.state / "events"
    # curl refuses `refusals` times, then answers. sleep returns at once, and every stub
    # writes to one log, so the order shows: wait for the station, then open the page.
    _write(
        harness.bin / "curl",
        "#!/bin/bash\n"
        "printf '%s\\n' curl >> \"$STATE/events\"\n"
        "n=0\n"
        '[ -f "$STATE/curl-count" ] && n=$(cat "$STATE/curl-count")\n'
        "n=$((n + 1))\n"
        'printf \'%s\\n\' "$n" > "$STATE/curl-count"\n'
        '[ "$n" -gt "$CURL_REFUSALS" ]\n',
    )
    _write(harness.bin / "sleep", "#!/bin/bash\nprintf '%s\\n' sleep >> \"$STATE/events\"\n")
    _write(
        harness.bin / "open",
        '#!/bin/bash\nprintf \'%s\\n\' "$*" >> "$STATE/open-log"\nprintf \'%s\\n\' open >> "$STATE/events"\n',
    )
    harness.env["CURL_REFUSALS"] = str(refusals)
    launch = harness.run()

    assert_handoff(launch)
    assert events.read_text().splitlines() == [*expected, "open"]


def test_every_failure_tells_the_user_what_to_do_next() -> None:
    # assert_failure checks the problem line only, so a fail_setup call that lost its
    # third argument (the way out) would still pass every behavior test.
    code = boundary().replace("\\\n", " ")
    calls = [
        shlex.split(line[line.index("fail_setup ") :])
        for line in code.splitlines()
        if "fail_setup " in line and "fail_setup()" not in line and not line.lstrip().startswith("#")
    ]
    assert len(calls) >= 30, "the scan stopped finding the fail_setup calls"
    for call in calls:
        assert len(call) == 4, call  # fail_setup <problem> <cause> <way out>
        assert re.search(r"double-click|\$(REBUILD|RETRY)_ACTION", call[3], re.I), call
    for name in ("REBUILD_ACTION", "RETRY_ACTION"):
        action = re.search(rf'^{name}="([^"]+)"$', code, re.M)
        assert action is not None and "README.md" in action.group(1), name


def test_a_launcher_that_cannot_open_its_folder_says_what_to_do(tmp_path: Path) -> None:
    # The first step runs before the setup region, so the boundary harness cannot reach it.
    text = LAUNCHER.read_text()
    marker = "# Use login shell environment"
    assert marker in text
    head = text.split(marker, 1)[0]
    moved_away = tmp_path / "moved-away" / "Start Radio.command"

    result = subprocess.run(
        ["/bin/bash", "-c", head, str(moved_away)],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert lines[0] == "ERROR: The launcher could not open its own folder."
    assert "double-click Start Radio.command" in lines[2]
    assert lines[-1] == ANCHOR


def test_readme_documents_the_mac_launcher_and_recovery() -> None:
    readme = (ROOT / "README.md").read_text()
    heading = "#### Mac launcher recovery"
    assert heading in readme
    section = readme.split(heading, 1)[1]
    match = re.search(r"```bash\n(.*?)```", section, re.S)
    assert match is not None
    assert match.group(1).strip() == RECOVERY
    for phrase in (
        "Python 3.11 or newer",
        "ffmpeg",
        "several minutes",
        "requirements.txt",
        "pyproject.toml",
        "Start Radio.command",
        "setup-mac.sh",
        "./start.sh",
        ".env",
        "music/",
        "When both are already installed",
        "not a station you can run",
    ):
        assert phrase in readme


def test_readme_recovery_sequence_preserves_operator_data_and_retries(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    first_env = harness.root / ".venv"
    first_env.mkdir()
    (first_env / "id").write_text("first\n")
    _run_recovery(harness)
    backups = sorted((harness.root / "tmp").glob("venv-backup.*"))
    assert len(backups) == 1
    assert (backups[0] / "environment" / "id").read_text() == "first\n"
    assert not first_env.exists()

    second_env = harness.root / ".venv"
    second_env.mkdir()
    (second_env / "id").write_text("second\n")
    _run_recovery(harness)
    backups = list((harness.root / "tmp").glob("venv-backup.*"))
    assert len(backups) == 2
    assert {(path / "environment" / "id").read_text() for path in backups} == {"first\n", "second\n"}

    open_log = harness.state / "open-log"
    before = open_log.read_text()
    failed = _run_recovery(harness, check=False)
    assert failed.returncode != 0
    assert open_log.read_text() == before
    harness.assert_operator_data()

    launch = harness.run()
    assert launch.returncode == 0, launch.output
    assert "First run. Preparing the station." in launch.stdout
    assert harness.marker_path().is_file()


@pytest.mark.parametrize(
    "files",
    [
        {},
        {"Start Radio.command": "#!/bin/bash\n"},
        {
            "Start Radio.command": "#!/bin/bash\n",
            "pyproject.toml": '[project]\nname = "other"\n\n[[tool.uv.index]]\nname = "mammamiradio"\n',
        },
    ],
    ids=["plain-folder", "copied-launcher", "other-project-naming-an-index-mammamiradio"],
)
def test_readme_recovery_sequence_outside_a_checkout_moves_nothing(tmp_path: Path, files: dict[str, str]) -> None:
    harness = Harness(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / ".venv").mkdir(parents=True)
    for name, text in files.items():
        (elsewhere / name).write_text(text)
    open_log = harness.state / "open-log"

    failed = _run_recovery(harness, cwd=elsewhere, check=False)

    assert failed.returncode != 0
    assert (elsewhere / ".venv").is_dir()
    assert not (elsewhere / "tmp").exists()
    assert not open_log.exists()


def _run_recovery(harness: Harness, *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["/bin/bash", "-c", RECOVERY],
        cwd=cwd or harness.root,
        env=harness.env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result
