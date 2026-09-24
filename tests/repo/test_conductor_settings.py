import os
import shutil
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_shared_conductor_settings_use_committed_lifecycle_scripts() -> None:
    settings = tomllib.loads((ROOT / ".conductor/settings.toml").read_text())
    scripts = settings["scripts"]

    assert scripts["setup"] == "bash ./scripts/conductor-setup.sh"
    assert scripts["archive"] == "bash ./scripts/conductor-archive.sh"
    assert scripts["run_mode"] == "concurrent"
    assert scripts["run"]["run"] == {
        "available_in": ["local"],
        "command": "MAMMAMIRADIO_ALLOW_YTDLP=false bash ./scripts/conductor-run.sh",
        "default": True,
        "icon": "play",
    }
    assert scripts["run"]["run_ytdlp"] == {
        "available_in": ["local"],
        "command": "MAMMAMIRADIO_ALLOW_YTDLP=true bash ./scripts/conductor-run.sh",
        "icon": "radio",
    }
    assert scripts["run"]["fresh_start"] == {
        "available_in": ["local"],
        "command": "bash ./scripts/conductor-fresh-start.sh --yes",
        "icon": "refresh-cw",
    }


def test_conductor_run_preserves_explicit_ytdlp_profile_over_dotenv() -> None:
    run_script = (ROOT / "scripts/conductor-run.sh").read_text()
    start_script = (ROOT / "start.sh").read_text()

    for script in (run_script, start_script):
        assert '"${MAMMAMIRADIO_ALLOW_YTDLP+x}"' in script
        assert 'export MAMMAMIRADIO_ALLOW_YTDLP="$_MAMMAMIRADIO_YTDLP_REQUESTED"' in script


def test_conductor_run_explicit_ytdlp_profile_wins_over_dotenv(tmp_path: Path) -> None:
    script_root = tmp_path / "scripts"
    script_root.mkdir()
    run_script = script_root / "conductor-run.sh"
    shutil.copy(ROOT / "scripts/conductor-run.sh", run_script)
    run_script.chmod(0o755)

    safe_env = tmp_path / "home/.config/mammamiradio/.env"
    safe_env.parent.mkdir(parents=True)
    safe_env.write_text("MAMMAMIRADIO_ALLOW_YTDLP=true\n")

    output = tmp_path / "selected-profile"
    start_script = tmp_path / "start.sh"
    start_script.write_text('#!/usr/bin/env bash\nprintf "%s" "${MAMMAMIRADIO_ALLOW_YTDLP-}" > "$TEST_YTDLP_OUTPUT"\n')
    start_script.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(tmp_path / "home"),
            "MAMMAMIRADIO_ALLOW_YTDLP": "false",
            "TEST_YTDLP_OUTPUT": str(output),
        }
    )
    subprocess.run(["bash", str(run_script)], cwd=tmp_path, env=environment, check=True)

    assert output.read_text() == "false"


def test_general_prompt_preserves_workspace_operating_contract() -> None:
    settings = tomllib.loads((ROOT / ".conductor/settings.toml").read_text())
    prompt = settings["prompts"]["general"]

    assert "STEP 0 — SEAT" in prompt
    assert "LANDING CONDUCTOR" in prompt
    assert "FEATURE WORKER CONTRACT" in prompt
    assert "the train `HEAD` assigned to the slice for Path B" in prompt
    assert "git diff --name-only <base-sha>...HEAD" in prompt
    assert "confirm GitHub reports `MERGED`" in prompt
    assert "dependabot-automerge.yml` is the sole automated merge-path" in prompt
    assert "This repository uses `.conductor/settings.toml`, not `conductor.json`" in prompt
    assert "`chore(deps): ...`; `deps:` is not a valid type" in prompt
