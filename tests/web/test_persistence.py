"""Tests for web/persistence.py — credential persistence + facade re-export wiring."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import mammamiradio.web.persistence as persistence


def test_persistence_facade_reexport_identity():
    """Every persistence symbol the streamer facade re-exports must resolve to the SAME
    object as its new home.

    Routes still call these by bare name through the streamer namespace, and the existing
    `@patch("...streamer._save_dotenv")` test sites target that namespace — so the
    re-export must point at the moved implementation, not a forked copy. The streamer
    import is local so the pure-persistence tests below don't load the god-module.
    """
    import mammamiradio.web.streamer as streamer

    for name in (
        "_save_dotenv",
        "_save_addon_option",
        "_save_addon_options",
        "_apply_live_credentials",
        "_sanitize_credential_value",
        "_CREDENTIAL_FIELDS",
        "_CREDENTIAL_ENV_TO_FIELD",
    ):
        assert getattr(streamer, name) is getattr(persistence, name), name


def test_save_addon_options_maps_env_keys_to_fields(tmp_path):
    """Credential env keys are written to /data/options.json under their field names,
    preserving unrelated existing keys.
    """
    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps({"existing": "keep"}))
    with patch("mammamiradio.web.persistence.Path", return_value=options_file):
        persistence._save_addon_options({"ANTHROPIC_API_KEY": "sk-test", "OPENAI_API_KEY": "oa-test"})
    written = json.loads(options_file.read_text())
    assert written["anthropic_api_key"] == "sk-test"
    assert written["openai_api_key"] == "oa-test"
    assert written["existing"] == "keep"


def test_save_addon_options_treats_corrupt_file_as_empty(tmp_path):
    options_file = tmp_path / "options.json"
    options_file.write_text("not valid json {{{")
    with patch("mammamiradio.web.persistence.Path", return_value=options_file):
        persistence._save_addon_options({"ANTHROPIC_API_KEY": "sk-x"})
    assert json.loads(options_file.read_text())["anthropic_api_key"] == "sk-x"


def test_apply_live_credentials_updates_config_env_and_clears_backoff(monkeypatch):
    # setenv (not delenv) so monkeypatch tracks both keys and reliably restores them on
    # teardown — _apply_live_credentials writes os.environ directly, which delenv on an
    # absent key would not clean up, leaking the test values into later tests.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    config = SimpleNamespace(anthropic_api_key="", openai_api_key="")
    state = SimpleNamespace(anthropic_disabled_until=99.0, anthropic_last_error="boom")

    persistence._apply_live_credentials(state, config, {"ANTHROPIC_API_KEY": "sk-new", "OPENAI_API_KEY": "oa-new"})

    assert config.anthropic_api_key == "sk-new"
    assert config.openai_api_key == "oa-new"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-new"
    assert state.anthropic_disabled_until == 0.0
    assert state.anthropic_last_error == ""
