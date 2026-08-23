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
        "command": "bash ./scripts/conductor-run.sh",
        "default": True,
        "icon": "play",
    }


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
