"""Tests for web/persistence.py — credential persistence + facade re-export wiring."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import mammamiradio.web.persistence as persistence
from mammamiradio.core.config import _apply_addon_options


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


def test_save_addon_options_writes_env_keys_to_secrets_env(tmp_path):
    """Credential env keys are written to /config/secrets.env, preserving unrelated lines."""
    options_file = tmp_path / "options.json"
    options_file.write_text("{}")
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text('# keep this\nOPENAI_API_KEY="old"\n')
    with patch("mammamiradio.web.persistence.Path", side_effect=[options_file, secrets_file]):
        persistence._save_addon_options(
            {
                "ANTHROPIC_API_KEY": "sk-test\nEVIL=1",
                "OPENAI_API_KEY": "oa-test",
                "AZURE_SPEECH_KEY": "az-test",
                "AZURE_SPEECH_REGION": "westeurope",
                "ELEVENLABS_API_KEY": "el-test",
            }
        )
    written = secrets_file.read_text()
    assert "# keep this" in written
    assert "ANTHROPIC_API_KEY=sk-testEVIL=1" in written
    assert "OPENAI_API_KEY=oa-test" in written
    assert "AZURE_SPEECH_KEY=az-test" in written
    assert "AZURE_SPEECH_REGION=westeurope" in written
    assert "ELEVENLABS_API_KEY=el-test" in written
    assert "anthropic_api_key" not in written


def test_save_addon_options_appends_to_existing_plaintext_file(tmp_path):
    options_file = tmp_path / "options.json"
    options_file.write_text("{}")
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("not valid json {{{\n")
    with patch("mammamiradio.web.persistence.Path", side_effect=[options_file, secrets_file]):
        persistence._save_addon_options({"ANTHROPIC_API_KEY": "sk-x"})
    assert secrets_file.read_text() == "not valid json {{{\nANTHROPIC_API_KEY=sk-x\n"


def test_save_addon_options_round_trips_through_addon_config_loader(monkeypatch, tmp_path):
    options_file = tmp_path / "options.json"
    options_file.write_text("{}")
    secrets_file = tmp_path / "secrets.env"

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    try:
        with patch("mammamiradio.web.persistence.Path", side_effect=[options_file, secrets_file]):
            persistence._save_addon_options({"ANTHROPIC_API_KEY": 'sk"quote'})
        assert "ANTHROPIC_API_KEY='sk\"quote'" in secrets_file.read_text()

        with patch("mammamiradio.core.config.Path", side_effect=[options_file, secrets_file]):
            _apply_addon_options()
        assert os.environ["ANTHROPIC_API_KEY"] == 'sk"quote'
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)


def test_save_addon_options_migrates_and_prunes_legacy_provider_options(tmp_path):
    options_file = tmp_path / "options.json"
    options_file.write_text(
        json.dumps(
            {
                "anthropic_api_key": "old-ant",
                "openai_api_key": "old-openai",
                "station_name": "Mamma Mi Radio",
                "broadcast_chain": False,
            }
        )
    )
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("# keep\n")

    with patch("mammamiradio.web.persistence.Path", side_effect=[options_file, secrets_file]):
        persistence._save_addon_options({"ANTHROPIC_API_KEY": "new-ant"})

    assert secrets_file.read_text() == "# keep\nANTHROPIC_API_KEY=new-ant\nOPENAI_API_KEY=old-openai\n"
    assert json.loads(options_file.read_text()) == {
        "station_name": "Mamma Mi Radio",
        "broadcast_chain": False,
    }


def test_apply_live_credentials_updates_config_env_and_clears_backoff(monkeypatch):
    # setenv (not delenv) so monkeypatch tracks both keys and reliably restores them on
    # teardown — _apply_live_credentials writes os.environ directly, which delenv on an
    # absent key would not clean up, leaking the test values into later tests.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("AZURE_SPEECH_KEY", "")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "")
    config = SimpleNamespace(
        anthropic_api_key="",
        openai_api_key="",
        azure_speech_key="",
        azure_speech_region="",
        elevenlabs_api_key="",
    )
    state = SimpleNamespace(anthropic_disabled_until=99.0, anthropic_last_error="boom")

    persistence._apply_live_credentials(
        state,
        config,
        {
            "ANTHROPIC_API_KEY": "sk-new",
            "OPENAI_API_KEY": "oa-new",
            "AZURE_SPEECH_KEY": "az-new",
            "AZURE_SPEECH_REGION": "westeurope",
            "ELEVENLABS_API_KEY": "el-new",
        },
    )

    assert config.anthropic_api_key == "sk-new"
    assert config.openai_api_key == "oa-new"
    assert config.azure_speech_key == "az-new"
    assert config.azure_speech_region == "westeurope"
    assert config.elevenlabs_api_key == "el-new"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-new"
    assert os.environ["AZURE_SPEECH_KEY"] == "az-new"
    assert os.environ["AZURE_SPEECH_REGION"] == "westeurope"
    assert os.environ["ELEVENLABS_API_KEY"] == "el-new"
    assert state.anthropic_disabled_until == 0.0
    assert state.anthropic_last_error == ""
