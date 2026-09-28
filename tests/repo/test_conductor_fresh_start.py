from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE_SCRIPT = ROOT / "scripts" / "conductor-fresh-start.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}")
    path.chmod(0o755)


@pytest.fixture()
def script_repo(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / SOURCE_SCRIPT.name
    shutil.copy2(SOURCE_SCRIPT, script)
    script.chmod(0o755)

    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "lsof",
        """if [ "${1:-}" = "-a" ]; then
  [ -n "${FAKE_PROCESS_CWD:-}" ] || exit 1
  printf 'p%s\\nfcwd\\nn%s\\n' "${FAKE_PROCESS_PID:-4242}" "$FAKE_PROCESS_CWD"
  exit 0
fi
if [ -n "${FAKE_ACTIVE_PORT:-}" ]; then
  case " $* " in
    *" -iTCP:${FAKE_ACTIVE_PORT} "*) exit 0 ;;
  esac
fi
exit "${FAKE_LSOF_STATUS:-1}"
""",
    )
    _write_executable(
        bin_dir / "ps",
        """if [ -n "${FAKE_PS_STATUS:-}" ]; then
  exit "$FAKE_PS_STATUS"
fi
if [ -n "${FAKE_PS_OUTPUT:-}" ]; then
  printf '%s\\n' "$FAKE_PS_OUTPUT"
  exit 0
fi
exit 0
""",
    )

    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "CONDUCTOR_PORT": "43123",
            "FAKE_LSOF_STATUS": "1",
        }
    )
    for key in ("MAMMAMIRADIO_CACHE_DIR", "MAMMAMIRADIO_TMP_DIR", "MAMMAMIRADIO_PORT"):
        env.pop(key, None)
    return repo, script, env


def _run(script: Path, repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(script), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _seed_runtime(repo: Path) -> tuple[Path, Path]:
    runtime = repo / ".context" / "conductor"
    cache = runtime / "cache"
    temp = runtime / "tmp"
    (cache / "state").mkdir(parents=True)
    temp.mkdir(parents=True)
    (cache / "mammamiradio.db").write_bytes(b"database")
    (cache / "state" / "first_listen_receipt_v1.json").write_text('{"heard_at":1}\n')
    (cache / "state" / "first_listen_install_origin_v1.json").write_text('{"database_preexisted":false}\n')
    (temp / "rendered.mp3").write_bytes(b"audio")
    (runtime / "keep.lock").write_text("keep")
    (repo / ".env").write_text("MAMMAMIRADIO_TEST_SECRET=preserve\n")
    (repo / "ha-config").mkdir()
    (repo / "ha-config" / "keep").write_text("preserve")
    return cache, temp


def test_script_exists_is_executable_and_parses() -> None:
    assert SOURCE_SCRIPT.exists()
    assert SOURCE_SCRIPT.stat().st_mode & 0o111
    result = subprocess.run(
        ["bash", "-n", str(SOURCE_SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_without_yes_is_a_side_effect_free_dry_run(script_repo: tuple[Path, Path, dict[str, str]]) -> None:
    repo, script, env = script_repo

    result = _run(script, repo, env)

    assert result.returncode == 0, result.stderr
    assert "No changes made" in result.stdout
    assert str(repo / ".context/conductor/cache") in result.stdout
    assert str(repo / ".context/conductor/tmp") in result.stdout
    assert not (repo / ".context").exists()


def test_fake_ps_has_no_host_process_fallback(script_repo: tuple[Path, Path, dict[str, str]]) -> None:
    _, _, env = script_repo

    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_repo_dotenv_overrides_home_dotenv_for_reset_targets_and_port(
    script_repo: tuple[Path, Path, dict[str, str]],
) -> None:
    repo, script, env = script_repo
    home_runtime = repo.parent / "home-runtime"
    repo_runtime = repo.parent / "repo-runtime"
    for runtime, payload in ((home_runtime, "home"), (repo_runtime, "repo")):
        (runtime / "cache").mkdir(parents=True)
        (runtime / "tmp").mkdir()
        (runtime / "cache" / "state.db").write_text(payload)
        (runtime / "tmp" / "rendered.mp3").write_text(payload)

    home_env = Path(env["HOME"]) / ".config/mammamiradio/.env"
    home_env.parent.mkdir(parents=True)
    home_env.write_text(
        f"MAMMAMIRADIO_CACHE_DIR={home_runtime / 'cache'}\n"
        f"MAMMAMIRADIO_TMP_DIR={home_runtime / 'tmp'}\n"
        "MAMMAMIRADIO_PORT=43124\n"
    )
    (repo / ".env").write_text(
        f"MAMMAMIRADIO_CACHE_DIR={repo_runtime / 'cache'}\n"
        f"MAMMAMIRADIO_TMP_DIR={repo_runtime / 'tmp'}\n"
        "MAMMAMIRADIO_PORT=43125\n"
    )

    dry_run = _run(script, repo, env)
    assert dry_run.returncode == 0, dry_run.stderr
    assert f"  cache: {repo_runtime / 'cache'}" in dry_run.stdout
    assert f"  temp:  {repo_runtime / 'tmp'}" in dry_run.stdout
    assert "  port:  43125" in dry_run.stdout
    assert (home_runtime / "cache/state.db").read_text() == "home"
    assert (repo_runtime / "cache/state.db").read_text() == "repo"

    env["FAKE_ACTIVE_PORT"] = "43125"
    blocked = _run(script, repo, env, "--yes")
    assert blocked.returncode != 0
    assert "port 43125 is active" in blocked.stderr
    assert (repo_runtime / "cache/state.db").read_text() == "repo"
    assert (repo_runtime / "tmp/rendered.mp3").read_text() == "repo"

    env.pop("FAKE_ACTIVE_PORT")
    reset = _run(script, repo, env, "--yes")
    assert reset.returncode == 0, reset.stderr
    assert (home_runtime / "cache/state.db").read_text() == "home"
    assert (home_runtime / "tmp/rendered.mp3").read_text() == "home"
    assert not list((repo_runtime / "cache").iterdir())
    assert not list((repo_runtime / "tmp").iterdir())
    archives = list((repo_runtime / "fresh-start-archives").glob("*"))
    assert len(archives) == 1
    assert (archives[0] / "cache/state.db").read_text() == "repo"
    assert (archives[0] / "tmp/rendered.mp3").read_text() == "repo"


@pytest.mark.parametrize("home_env_present", [True, False])
def test_conditional_repo_dotenv_matches_run_load_order(
    script_repo: tuple[Path, Path, dict[str, str]],
    home_env_present: bool,
) -> None:
    repo, script, env = script_repo
    if home_env_present:
        home_env = Path(env["HOME"]) / ".config/mammamiradio/.env"
        home_env.parent.mkdir(parents=True)
        home_env.write_text("MAMMAMIRADIO_TEST_SECRET=preserve\n")
    (repo / ".env").write_text(
        'MAMMAMIRADIO_CACHE_DIR="${MAMMAMIRADIO_CACHE_DIR:-alternate/cache}"\n'
        'MAMMAMIRADIO_TMP_DIR="${MAMMAMIRADIO_TMP_DIR:-alternate/tmp}"\n'
        'MAMMAMIRADIO_PORT="${MAMMAMIRADIO_PORT:-43210}"\n'
    )

    result = _run(script, repo, env)

    assert result.returncode == 0, result.stderr
    runtime = repo / (".context/conductor" if home_env_present else "alternate")
    assert f"  cache: {runtime / 'cache'}" in result.stdout
    assert f"  temp:  {runtime / 'tmp'}" in result.stdout
    assert f"  port:  {43123 if home_env_present else 43210}" in result.stdout
    assert not (repo / ".context").exists()
    assert not (repo / "alternate").exists()


def test_yes_archives_and_recreates_only_the_radio_runtime(
    script_repo: tuple[Path, Path, dict[str, str]],
) -> None:
    repo, script, env = script_repo
    cache, temp = _seed_runtime(repo)

    result = _run(script, repo, env, "--yes")

    assert result.returncode == 0, result.stderr
    assert "confirmed run" in result.stdout
    assert "dry run" not in result.stdout
    assert cache.is_dir()
    assert temp.is_dir()
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    assert stat.S_IMODE(temp.stat().st_mode) == 0o700
    assert not list(cache.iterdir())
    assert not list(temp.iterdir())
    assert (repo / ".context/conductor/keep.lock").read_text() == "keep"
    assert (repo / ".env").read_text() == "MAMMAMIRADIO_TEST_SECRET=preserve\n"
    assert (repo / "ha-config/keep").read_text() == "preserve"

    archives = sorted((repo / ".context/conductor/fresh-start-archives").glob("*"))
    assert len(archives) == 1
    archive = archives[0]
    assert (archive / "cache/mammamiradio.db").read_bytes() == b"database"
    assert (archive / "cache/state/first_listen_receipt_v1.json").exists()
    assert (archive / "cache/state/first_listen_install_origin_v1.json").exists()
    assert (archive / "tmp/rendered.mp3").read_bytes() == b"audio"


def test_yes_honors_external_cache_and_temp_overrides(
    script_repo: tuple[Path, Path, dict[str, str]],
) -> None:
    repo, script, env = script_repo
    external = repo.parent / "slot-2-runtime"
    cache = external / "cache"
    temp = external / "tmp"
    (cache / "state").mkdir(parents=True)
    temp.mkdir(parents=True)
    (cache / "mammamiradio.db").write_bytes(b"external-db")
    (temp / "rendered.mp3").write_bytes(b"external-audio")
    env["MAMMAMIRADIO_CACHE_DIR"] = str(cache)
    env["MAMMAMIRADIO_TMP_DIR"] = str(temp)

    result = _run(script, repo, env, "--yes")

    assert result.returncode == 0, result.stderr
    assert cache.is_dir() and not list(cache.iterdir())
    assert temp.is_dir() and not list(temp.iterdir())
    archives = sorted((external / "fresh-start-archives").glob("*"))
    assert len(archives) == 1
    archive = archives[0]
    assert (archive / "cache/mammamiradio.db").read_bytes() == b"external-db"
    assert (archive / "tmp/rendered.mp3").read_bytes() == b"external-audio"


def test_yes_resolves_symlinked_ancestor_before_archiving(
    script_repo: tuple[Path, Path, dict[str, str]],
) -> None:
    repo, script, env = script_repo
    physical = repo.parent / "physical-runtime"
    cache = physical / "cache"
    temp = physical / "tmp"
    cache.mkdir(parents=True)
    temp.mkdir()
    (cache / "mammamiradio.db").write_bytes(b"physical-db")
    alias = repo.parent / "runtime-alias"
    alias.symlink_to(physical, target_is_directory=True)
    env["MAMMAMIRADIO_CACHE_DIR"] = str(alias / "cache")
    env["MAMMAMIRADIO_TMP_DIR"] = str(alias / "tmp")

    result = _run(script, repo, env, "--yes")

    assert result.returncode == 0, result.stderr
    assert str(physical / "cache") in result.stdout
    assert str(alias / "cache") not in result.stdout
    archives = sorted((physical / "fresh-start-archives").glob("*"))
    assert len(archives) == 1
    assert (archives[0] / "cache/mammamiradio.db").read_bytes() == b"physical-db"


def test_yes_refuses_targets_that_are_physical_aliases(
    script_repo: tuple[Path, Path, dict[str, str]],
) -> None:
    repo, script, env = script_repo
    external = repo.parent / "alias-overlap"
    slot = external / "slot"
    shared = external / "cache"
    nested_temp = shared / "tmp"
    slot.mkdir(parents=True)
    nested_temp.mkdir(parents=True)
    (shared / "preserve").write_text("preserve")
    env["MAMMAMIRADIO_CACHE_DIR"] = str(slot / ".." / "cache")
    env["MAMMAMIRADIO_TMP_DIR"] = str(nested_temp)

    result = _run(script, repo, env, "--yes")

    assert result.returncode != 0
    assert "cache and temp targets overlap" in result.stderr
    assert (shared / "preserve").read_text() == "preserve"
    assert not (external / "fresh-start-archives").exists()


def test_yes_refuses_dotdot_alias_that_resolves_to_repo_root(
    script_repo: tuple[Path, Path, dict[str, str]],
) -> None:
    repo, script, env = script_repo
    cache, temp = _seed_runtime(repo)
    env["MAMMAMIRADIO_CACHE_DIR"] = str(repo / "scripts" / "..")

    result = _run(script, repo, env, "--yes")

    assert result.returncode != 0
    assert f"unsafe cache target: {repo}" in result.stderr
    assert (cache / "mammamiradio.db").exists()
    assert (temp / "rendered.mp3").exists()
    assert not (repo / ".context/conductor/fresh-start-archives").exists()


def test_yes_refuses_symlinked_ancestor_that_resolves_to_repo_root(
    script_repo: tuple[Path, Path, dict[str, str]],
) -> None:
    repo, script, env = script_repo
    cache, temp = _seed_runtime(repo)
    alias_parent = repo.parent / "repo-alias-parent"
    alias_parent.symlink_to(repo.parent, target_is_directory=True)
    env["MAMMAMIRADIO_CACHE_DIR"] = str(alias_parent / repo.name)

    result = _run(script, repo, env, "--yes")

    assert result.returncode != 0
    assert f"unsafe cache target: {repo}" in result.stderr
    assert (cache / "mammamiradio.db").exists()
    assert (temp / "rendered.mp3").exists()
    assert not (repo / ".context/conductor/fresh-start-archives").exists()


def test_yes_refuses_case_only_alias_that_resolves_to_repo_root(
    script_repo: tuple[Path, Path, dict[str, str]],
) -> None:
    repo, script, env = script_repo
    cache, temp = _seed_runtime(repo)
    case_alias = repo.with_name(repo.name.swapcase())
    try:
        same_directory = case_alias.samefile(repo)
    except FileNotFoundError:
        same_directory = False
    if not same_directory:
        pytest.skip("filesystem is case-sensitive")
    env["MAMMAMIRADIO_CACHE_DIR"] = str(case_alias)

    result = _run(script, repo, env, "--yes")

    assert result.returncode != 0
    assert "unsafe cache target" in result.stderr
    assert (cache / "mammamiradio.db").exists()
    assert (temp / "rendered.mp3").exists()
    assert not (repo / ".context/conductor/fresh-start-archives").exists()


def test_yes_refuses_broad_home_descendant_not_named_for_its_runtime_role(
    script_repo: tuple[Path, Path, dict[str, str]],
) -> None:
    repo, script, env = script_repo
    cache, temp = _seed_runtime(repo)
    documents = Path(env["HOME"]) / "Documents"
    documents.mkdir(parents=True)
    (documents / "preserve").write_text("preserve")
    env["MAMMAMIRADIO_CACHE_DIR"] = str(documents)

    result = _run(script, repo, env, "--yes")

    assert result.returncode != 0
    assert "cache target must end in /cache" in result.stderr
    assert (documents / "preserve").read_text() == "preserve"
    assert (cache / "mammamiradio.db").exists()
    assert (temp / "rendered.mp3").exists()
    assert not (repo / ".context/conductor/fresh-start-archives").exists()


def test_yes_refuses_while_radio_port_is_active(
    script_repo: tuple[Path, Path, dict[str, str]],
) -> None:
    repo, script, env = script_repo
    cache, temp = _seed_runtime(repo)
    env["FAKE_LSOF_STATUS"] = "0"

    result = _run(script, repo, env, "--yes")

    assert result.returncode != 0
    assert "port 43123 is active" in result.stderr
    assert (cache / "mammamiradio.db").exists()
    assert (temp / "rendered.mp3").exists()
    assert not (repo / ".context/conductor/fresh-start-archives").exists()


def test_yes_fails_closed_when_port_state_cannot_be_verified(
    script_repo: tuple[Path, Path, dict[str, str]],
) -> None:
    repo, script, env = script_repo
    cache, temp = _seed_runtime(repo)
    env["FAKE_LSOF_STATUS"] = "2"

    result = _run(script, repo, env, "--yes")

    assert result.returncode != 0
    assert "could not verify radio port 43123 state" in result.stderr
    assert (cache / "mammamiradio.db").exists()
    assert (temp / "rendered.mp3").exists()
    assert not (repo / ".context/conductor/fresh-start-archives").exists()


def test_yes_refuses_workspace_radio_that_is_starting_before_port_bind(
    script_repo: tuple[Path, Path, dict[str, str]],
) -> None:
    repo, script, env = script_repo
    cache, temp = _seed_runtime(repo)
    env["FAKE_PS_OUTPUT"] = "4242 /bin/bash ./start.sh"
    env["FAKE_PROCESS_PID"] = "4242"
    env["FAKE_PROCESS_CWD"] = str(repo)

    result = _run(script, repo, env, "--yes")

    assert result.returncode != 0
    assert "workspace radio process 4242 is active or starting" in result.stderr
    assert (cache / "mammamiradio.db").exists()
    assert (temp / "rendered.mp3").exists()
    assert not (repo / ".context/conductor/fresh-start-archives").exists()


def test_yes_fails_closed_when_process_state_cannot_be_verified(
    script_repo: tuple[Path, Path, dict[str, str]],
) -> None:
    repo, script, env = script_repo
    cache, temp = _seed_runtime(repo)
    env["FAKE_PS_STATUS"] = "2"

    result = _run(script, repo, env, "--yes")

    assert result.returncode != 0
    assert "could not verify workspace radio process state" in result.stderr
    assert (cache / "mammamiradio.db").exists()
    assert (temp / "rendered.mp3").exists()
    assert not (repo / ".context/conductor/fresh-start-archives").exists()


def test_yes_rejects_extra_arguments_before_changing_state(
    script_repo: tuple[Path, Path, dict[str, str]],
) -> None:
    repo, script, env = script_repo
    cache, temp = _seed_runtime(repo)

    result = _run(script, repo, env, "--yes", "--help")

    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert (cache / "mammamiradio.db").exists()
    assert (temp / "rendered.mp3").exists()
    assert not (repo / ".context/conductor/fresh-start-archives").exists()


@pytest.mark.parametrize("target_name", ["repo", "home", "root"])
def test_yes_refuses_unsafe_broad_targets(script_repo: tuple[Path, Path, dict[str, str]], target_name: str) -> None:
    repo, script, env = script_repo
    _seed_runtime(repo)
    targets = {
        "repo": repo,
        "home": Path(env["HOME"]),
        "root": Path("/"),
    }
    env["MAMMAMIRADIO_CACHE_DIR"] = str(targets[target_name])

    result = _run(script, repo, env, "--yes")

    assert result.returncode != 0
    assert "unsafe cache target" in result.stderr
    assert (repo / ".context/conductor/cache/mammamiradio.db").exists()
    assert not (repo / ".context/conductor/fresh-start-archives").exists()
