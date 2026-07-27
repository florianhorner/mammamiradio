"""Tests for web/persistence.py — credential persistence + facade re-export wiring."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

import mammamiradio.web.persistence as persistence
from mammamiradio.core.config import _apply_addon_options, _read_addon_provider_secrets

ACTIVE_SCHEMA = [
    "station_name",
    "super_italian_mode",
    "chaos_mode_active",
    "festival_mode",
    "quality_profile",
    "broadcast_chain",
    "songs_between_banter",
    "songs_between_ads",
    "ad_spots_per_break",
    "enable_home_assistant",
    "ha_context_enabled",
    "ha_context_poll_interval",
    "ha_media_player_push",
    "admin_token",
    "jamendo_client_id",
    "guest_host",
    "norm_cache_mb",
]


class _FakeSupervisor:
    """Stateful GET/full-replacement-POST model of the Supervisor app API."""

    def __init__(self, options: dict | None = None, schema: list | None = None):
        self.options = dict(options or {})
        names = ACTIVE_SCHEMA if schema is None else schema
        self.schema = [{"name": name, "type": "str"} for name in names]
        self.get_count = 0
        self.post_count = 0
        self.posts: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.get_responses: list[httpx.Response | Exception] = []
        self.post_mode = "ok"
        self.request_overlap = 0
        self.max_request_overlap = 0
        self._request_lock = threading.Lock()

    def _enter_request(self) -> None:
        with self._request_lock:
            self.request_overlap += 1
            self.max_request_overlap = max(self.max_request_overlap, self.request_overlap)

    def _exit_request(self) -> None:
        with self._request_lock:
            self.request_overlap -= 1

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self._enter_request()
        try:
            if request.method == "GET" and request.url.path == "/addons/self/info":
                self.get_count += 1
                if self.get_responses:
                    response = self.get_responses.pop(0)
                    if isinstance(response, Exception):
                        raise response
                    return response
                # Make an unlocked implementation likely to interleave two GETs
                # before either replacement POST, exposing a lost update.
                time.sleep(0.01)
                return httpx.Response(
                    200,
                    json={
                        "result": "ok",
                        "data": {"options": dict(self.options), "schema": self.schema},
                    },
                )
            if request.method == "POST" and request.url.path == "/addons/self/options":
                self.post_count += 1
                body = json.loads(request.content)
                self.posts.append(body)
                if self.post_mode == "http_error":
                    return httpx.Response(503, json={"result": "error", "message": "private response body"})
                if self.post_mode == "disconnect":
                    raise httpx.ReadError("private transport detail", request=request)
                self.options = dict(body["options"])
                if self.post_mode == "commit_disconnect":
                    raise httpx.ReadError("private transport detail", request=request)
                if self.post_mode == "commit_malformed":
                    return httpx.Response(200, content=b"private malformed response")
                if self.post_mode == "commit_error_envelope":
                    return httpx.Response(200, json={"result": "error", "message": "private response body"})
                return httpx.Response(200, json={"result": "ok", "data": {}})
            return httpx.Response(404, json={"result": "error"})
        finally:
            self._exit_request()


def _install_fake_supervisor(monkeypatch, fake: _FakeSupervisor) -> list[dict]:
    real_client = httpx.Client
    client_kwargs: list[dict] = []

    def client_factory(**kwargs):
        client_kwargs.append(kwargs)
        return real_client(transport=httpx.MockTransport(fake.handle), **kwargs)

    monkeypatch.setattr(persistence.httpx, "Client", client_factory)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-secret-token")
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    return client_kwargs


def _default_options(**updates) -> dict:
    options = {
        "station_name": "Mamma Mi Radio",
        "super_italian_mode": False,
        "chaos_mode_active": False,
        "festival_mode": False,
        "quality_profile": "balanced",
        "broadcast_chain": False,
        "songs_between_banter": 3,
        "songs_between_ads": 8,
        "ad_spots_per_break": 2,
        "enable_home_assistant": True,
        "ha_context_enabled": True,
        "ha_context_poll_interval": 300,
        "ha_media_player_push": True,
        "admin_token": "admin-canary",
        "jamendo_client_id": "jamendo-canary",
        "guest_host": True,
        "norm_cache_mb": 1500,
    }
    options.update(updates)
    return options


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
        "_save_addon_option_batch",
        "_save_addon_options",
        "_apply_live_credentials",
        "_sanitize_credential_value",
        "_CREDENTIAL_FIELDS",
        "_CREDENTIAL_ENV_TO_FIELD",
    ):
        assert getattr(streamer, name) is getattr(persistence, name), name


def test_save_addon_options_writes_only_env_keys_to_secrets_env(monkeypatch, tmp_path):
    """Credential env keys are written to /config/secrets.env, preserving unrelated lines."""
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text('# keep this\nOPENAI_API_KEY="old"\n')
    monkeypatch.setattr(persistence, "_ADDON_SECRETS_PATH", secrets_file)
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
    # The secrets file holds provider keys — it must be owner-only (0600), the
    # core security claim of the secrets.env hardening.
    assert (secrets_file.stat().st_mode & 0o777) == 0o600


def test_save_dotenv_writes_owner_only_file(tmp_path, monkeypatch):
    """Standalone .env holds provider keys — it must land 0600, not umask-default."""
    monkeypatch.chdir(tmp_path)
    persistence._save_dotenv({"ANTHROPIC_API_KEY": "sk-standalone"})
    env_file = tmp_path / ".env"
    assert 'ANTHROPIC_API_KEY="sk-standalone"' in env_file.read_text()
    assert (env_file.stat().st_mode & 0o777) == 0o600


def test_save_addon_options_appends_to_existing_plaintext_file(monkeypatch, tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("not valid json {{{\n")
    monkeypatch.setattr(persistence, "_ADDON_SECRETS_PATH", secrets_file)
    persistence._save_addon_options({"ANTHROPIC_API_KEY": "sk-x"})
    assert secrets_file.read_text() == "not valid json {{{\nANTHROPIC_API_KEY=sk-x\n"


def test_save_addon_options_sanitizes_non_utf8_secret_file_failure(monkeypatch, tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_bytes(b"\xffprivate-provider-key")
    monkeypatch.setattr(persistence, "_ADDON_SECRETS_PATH", secrets_file)

    with pytest.raises(persistence._AddonPersistenceError) as exc_info:
        persistence._save_addon_options({"ANTHROPIC_API_KEY": "replacement"})

    assert str(exc_info.value) == "Unable to persist add-on credentials"
    assert "private-provider-key" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


def test_save_addon_options_hardens_a_stale_world_readable_temp_file(monkeypatch, tmp_path):
    secrets_file = tmp_path / "secrets.env"
    stale_temp = tmp_path / "secrets.env.tmp"
    stale_temp.write_text("stale")
    stale_temp.chmod(0o644)
    monkeypatch.setattr(persistence, "_ADDON_SECRETS_PATH", secrets_file)

    persistence._save_addon_options({"ANTHROPIC_API_KEY": "sk-x"})

    assert (secrets_file.stat().st_mode & 0o777) == 0o600


def test_save_addon_options_round_trips_through_addon_config_loader(monkeypatch, tmp_path):
    options_file = tmp_path / "options.json"
    options_file.write_text("{}")
    secrets_file = tmp_path / "secrets.env"

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(persistence, "_ADDON_SECRETS_PATH", secrets_file)
    try:
        persistence._save_addon_options({"ANTHROPIC_API_KEY": 'sk"quote'})
        assert "ANTHROPIC_API_KEY='sk\"quote'" in secrets_file.read_text()

        with patch("mammamiradio.core.config.Path", side_effect=[options_file, secrets_file]):
            _apply_addon_options()
        assert os.environ["ANTHROPIC_API_KEY"] == 'sk"quote'
        assert options_file.read_text() == "{}"
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)


def test_supervisor_save_migrates_retired_credentials_and_existing_nonempty_wins(monkeypatch, tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("\ufeffexport OPENAI_API_KEY=existing#value\nAZURE_SPEECH_KEY=existing value #literal\n")
    monkeypatch.setattr(persistence, "_ADDON_SECRETS_PATH", secrets_file)
    fake = _FakeSupervisor(
        {
            **_default_options(),
            "anthropic_api_key": "legacy-ant",
            "openai_api_key": "legacy-openai",
            "azure_speech_key": "legacy-azure",
            "elevenlabs_api_key": "",
        }
    )
    _install_fake_supervisor(monkeypatch, fake)

    persistence._save_addon_option("broadcast_chain", True)

    written = secrets_file.read_text()
    assert "ANTHROPIC_API_KEY=legacy-ant" in written
    assert "\ufeffexport OPENAI_API_KEY=existing#value" in written
    assert "AZURE_SPEECH_KEY=existing value #literal" in written
    assert "legacy-openai" not in written
    assert "legacy-azure" not in written
    assert "ELEVENLABS_API_KEY" not in written
    assert (secrets_file.stat().st_mode & 0o777) == 0o600
    assert _read_addon_provider_secrets(secrets_file) == {
        "ANTHROPIC_API_KEY": "legacy-ant",
        "OPENAI_API_KEY": "existing#value",
        "AZURE_SPEECH_KEY": "existing value #literal",
    }
    assert fake.post_count == 1
    assert all(key not in fake.options for key in persistence._CREDENTIAL_FIELDS)
    assert fake.options["broadcast_chain"] is True
    assert fake.options["admin_token"] == "admin-canary"
    assert fake.options["jamendo_client_id"] == "jamendo-canary"


def test_supervisor_save_rejects_multiline_retired_credential_before_post(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "_ADDON_SECRETS_PATH", tmp_path / "secrets.env")
    fake = _FakeSupervisor({**_default_options(), "anthropic_api_key": "\r\n"})
    _install_fake_supervisor(monkeypatch, fake)

    with pytest.raises(persistence._AddonPersistenceError, match="unsupported retired"):
        persistence._save_addon_option("broadcast_chain", True)

    assert fake.post_count == 0
    assert fake.options["anthropic_api_key"] == "\r\n"


@pytest.mark.parametrize("stale_value", ["\r\n", {"unexpected": "shape"}])
def test_existing_secret_wins_over_malformed_retired_credential(monkeypatch, tmp_path, stale_value):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("ANTHROPIC_API_KEY=durable-provider-key\n")
    secrets_file.chmod(0o644)
    monkeypatch.setattr(persistence, "_ADDON_SECRETS_PATH", secrets_file)
    fake = _FakeSupervisor({**_default_options(), "anthropic_api_key": stale_value})
    _install_fake_supervisor(monkeypatch, fake)

    persistence._save_addon_option("broadcast_chain", True)

    assert fake.post_count == 1
    assert "anthropic_api_key" not in fake.options
    assert _read_addon_provider_secrets(secrets_file)["ANTHROPIC_API_KEY"] == "durable-provider-key"
    assert (secrets_file.stat().st_mode & 0o777) == 0o600


def test_supervisor_save_preserves_provider_key_still_in_active_schema(monkeypatch, tmp_path):
    secrets_file = tmp_path / "secrets.env"
    monkeypatch.setattr(persistence, "_ADDON_SECRETS_PATH", secrets_file)
    options = {**_default_options(), "anthropic_api_key": "schema-owned-provider-key"}
    fake = _FakeSupervisor(options, schema=[*ACTIVE_SCHEMA, "anthropic_api_key"])
    _install_fake_supervisor(monkeypatch, fake)

    persistence._save_addon_option("broadcast_chain", True)

    assert fake.post_count == 1
    assert fake.options["anthropic_api_key"] == "schema-owned-provider-key"
    assert not secrets_file.exists()


def test_supervisor_save_preserves_complete_schema_known_options(monkeypatch, tmp_path):
    original = _default_options()
    fake = _FakeSupervisor(original)
    kwargs = _install_fake_supervisor(monkeypatch, fake)
    materialized = tmp_path / "options.json"
    materialized.write_text(json.dumps(original))
    updates = {"songs_between_banter": 5, "songs_between_ads": 9, "ad_spots_per_break": 3}

    persistence._save_addon_option_batch(updates)

    assert fake.post_count == 1
    expected = {**original, **updates}
    assert fake.posts == [{"options": expected}]
    assert fake.options == expected
    for key in original.keys() - updates.keys():
        assert fake.options[key] == original[key]
        assert type(fake.options[key]) is type(original[key])
    assert json.loads(materialized.read_text()) == original

    assert len(kwargs) == 1
    client = kwargs[0]
    assert client["base_url"] == "http://supervisor"
    assert client["headers"] == {"Authorization": "Bearer supervisor-secret-token"}
    assert client["trust_env"] is False
    assert client["follow_redirects"] is False
    assert client["timeout"].connect == 1.0
    assert client["timeout"].pool == 1.0
    assert client["timeout"].read == 5.0
    assert client["timeout"].write == 5.0


def test_supervisor_token_precedence_and_hassio_fallback(monkeypatch):
    fake = _FakeSupervisor(_default_options())
    kwargs = _install_fake_supervisor(monkeypatch, fake)
    monkeypatch.setenv("SUPERVISOR_API", "http://supervisor-custom/")
    monkeypatch.setenv("SUPERVISOR_TOKEN", "preferred")
    monkeypatch.setenv("HASSIO_TOKEN", "fallback")
    persistence._save_addon_option("super_italian_mode", True)
    assert kwargs[-1]["base_url"] == "http://supervisor-custom"
    assert kwargs[-1]["headers"]["Authorization"] == "Bearer preferred"

    monkeypatch.delenv("SUPERVISOR_TOKEN")
    persistence._save_addon_option("super_italian_mode", False)
    assert kwargs[-1]["headers"]["Authorization"] == "Bearer fallback"


def test_supervisor_ignores_ha_token(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)
    monkeypatch.setenv("HA_TOKEN", "must-not-authorize")
    with pytest.raises(persistence._AddonPersistenceError, match="credentials are unavailable"):
        persistence._save_addon_option("chaos_mode_active", True)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"result": "error", "data": {}}, "invalid add-on option response"),
        ({"result": "ok", "data": []}, "invalid add-on option response"),
        ({"result": "ok", "data": {"options": [], "schema": []}}, "invalid add-on option response"),
        ({"result": "ok", "data": {"options": {}, "schema": {}}}, "invalid add-on option schema"),
        (
            {"result": "ok", "data": {"options": {}, "schema": [{"name": "x"}, {"name": "x"}]}},
            "invalid add-on option schema",
        ),
        ({"result": "ok", "data": {"options": {}, "schema": [{"name": "  "}]}}, "invalid add-on option schema"),
    ],
)
def test_supervisor_rejects_malformed_info_envelopes(monkeypatch, payload, message):
    fake = _FakeSupervisor()
    fake.get_responses = [httpx.Response(200, json=payload)]
    _install_fake_supervisor(monkeypatch, fake)
    with pytest.raises(persistence._AddonPersistenceError, match=message):
        persistence._save_addon_option("chaos_mode_active", True)
    assert fake.post_count == 0


def test_supervisor_rejects_non_json_info_without_leaking_body(monkeypatch):
    fake = _FakeSupervisor()
    fake.get_responses = [httpx.Response(200, content=b"private invalid response body")]
    _install_fake_supervisor(monkeypatch, fake)

    with pytest.raises(persistence._AddonPersistenceError, match="invalid add-on option response") as exc_info:
        persistence._save_addon_option("chaos_mode_active", True)

    assert "private invalid response body" not in str(exc_info.value)
    assert fake.post_count == 0


def test_supervisor_rejects_non_2xx_info_without_post(monkeypatch):
    fake = _FakeSupervisor()
    fake.get_responses = [httpx.Response(503, content=b"token=private")]
    _install_fake_supervisor(monkeypatch, fake)
    with pytest.raises(persistence._AddonPersistenceError, match="rejected"):
        persistence._save_addon_option("chaos_mode_active", True)
    assert fake.post_count == 0


def test_supervisor_initial_get_transport_failure_does_not_post_or_reconcile(monkeypatch):
    fake = _FakeSupervisor()
    fake.get_responses = [httpx.ReadTimeout("private transport detail")]
    _install_fake_supervisor(monkeypatch, fake)
    with pytest.raises(persistence._AddonPersistenceError, match="Unable to read") as exc_info:
        persistence._save_addon_option("chaos_mode_active", True)
    assert "private transport detail" not in str(exc_info.value)
    assert fake.get_count == 1
    assert fake.post_count == 0


def test_supervisor_rejects_requested_and_stored_unknown_keys(monkeypatch):
    fake = _FakeSupervisor(_default_options())
    _install_fake_supervisor(monkeypatch, fake)
    with pytest.raises(persistence._AddonPersistenceError, match="not in the active schema"):
        persistence._save_addon_option("not_a_real_option", True)
    assert fake.post_count == 0

    fake.options["retired_mystery"] = "private"
    with pytest.raises(persistence._AddonPersistenceError, match="unsupported retired"):
        persistence._save_addon_option("chaos_mode_active", True)
    assert fake.post_count == 0


def test_retired_claude_model_requires_quality_profile(monkeypatch):
    options = _default_options()
    options.pop("quality_profile")
    options["claude_model"] = "claude-retired"
    fake = _FakeSupervisor(options)
    _install_fake_supervisor(monkeypatch, fake)

    with pytest.raises(persistence._AddonPersistenceError, match="unsupported retired"):
        persistence._save_addon_option("chaos_mode_active", True)
    assert fake.post_count == 0

    persistence._save_addon_option_batch({"chaos_mode_active": True, "quality_profile": "balanced"})
    assert fake.post_count == 1
    assert "claude_model" not in fake.options
    assert fake.options["quality_profile"] == "balanced"


@pytest.mark.parametrize("mode", ["commit_disconnect", "commit_malformed", "commit_error_envelope"])
def test_ambiguous_committed_post_reconciles_once_without_retry(monkeypatch, mode):
    fake = _FakeSupervisor(_default_options())
    fake.post_mode = mode
    _install_fake_supervisor(monkeypatch, fake)

    persistence._save_addon_option("chaos_mode_active", True)

    assert fake.post_count == 1
    assert fake.get_count == 2
    assert fake.options["chaos_mode_active"] is True


def test_ambiguous_uncommitted_post_fails_after_one_reconciliation_get(monkeypatch):
    fake = _FakeSupervisor(_default_options())
    fake.post_mode = "disconnect"
    _install_fake_supervisor(monkeypatch, fake)
    with pytest.raises(persistence._AddonPersistenceError, match="Unable to confirm"):
        persistence._save_addon_option("chaos_mode_active", True)
    assert fake.post_count == 1
    assert fake.get_count == 2
    assert fake.options["chaos_mode_active"] is False


def test_reconciliation_requires_exact_json_type(monkeypatch):
    fake = _FakeSupervisor(_default_options(chaos_mode_active=1))
    fake.post_mode = "disconnect"
    _install_fake_supervisor(monkeypatch, fake)
    with pytest.raises(persistence._AddonPersistenceError, match="Unable to confirm"):
        persistence._save_addon_option("chaos_mode_active", True)
    assert fake.post_count == 1
    assert fake.get_count == 2


def test_non_2xx_post_is_definitive_and_not_reconciled(monkeypatch):
    fake = _FakeSupervisor(_default_options())
    fake.post_mode = "http_error"
    _install_fake_supervisor(monkeypatch, fake)
    with pytest.raises(persistence._AddonPersistenceError, match="rejected"):
        persistence._save_addon_option("broadcast_chain", True)
    assert fake.post_count == 1
    assert fake.get_count == 1


def test_reconciliation_get_failure_is_sanitized(monkeypatch):
    fake = _FakeSupervisor(_default_options())
    fake.post_mode = "disconnect"
    fake.get_responses = [
        httpx.Response(
            200,
            json={"result": "ok", "data": {"options": _default_options(), "schema": fake.schema}},
        ),
        httpx.Response(500, content=b"SUPERVISOR_TOKEN=supervisor-secret-token"),
    ]
    _install_fake_supervisor(monkeypatch, fake)
    with pytest.raises(persistence._AddonPersistenceError, match="Unable to confirm") as exc_info:
        persistence._save_addon_option("broadcast_chain", True)
    assert "supervisor-secret-token" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


def test_reconciliation_transport_failure_still_performs_only_one_fresh_get(monkeypatch):
    fake = _FakeSupervisor(_default_options())
    fake.post_mode = "disconnect"
    fake.get_responses = [
        httpx.Response(
            200,
            json={"result": "ok", "data": {"options": _default_options(), "schema": fake.schema}},
        ),
        httpx.ReadTimeout("private transport detail"),
    ]
    _install_fake_supervisor(monkeypatch, fake)
    with pytest.raises(persistence._AddonPersistenceError, match="Unable to confirm") as exc_info:
        persistence._save_addon_option("broadcast_chain", True)
    assert "private transport detail" not in str(exc_info.value)
    assert fake.get_count == 2
    assert fake.post_count == 1


def test_credential_write_failure_aborts_before_supervisor_post(monkeypatch, tmp_path):
    secrets_file = tmp_path / "secrets.env"
    monkeypatch.setattr(persistence, "_ADDON_SECRETS_PATH", secrets_file)
    fake = _FakeSupervisor({**_default_options(), "anthropic_api_key": "private-provider-key"})
    _install_fake_supervisor(monkeypatch, fake)
    monkeypatch.setattr(
        persistence,
        "_write_owner_only_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private-provider-key")),
    )
    with pytest.raises(persistence._AddonPersistenceError, match="Unable to persist add-on credentials") as exc_info:
        persistence._save_addon_option("broadcast_chain", True)
    assert "private-provider-key" not in str(exc_info.value)
    assert fake.post_count == 0


def test_credential_unreadable_file_aborts_before_supervisor_post(monkeypatch, tmp_path):
    secrets_file = tmp_path / "secrets.env"
    secrets_file.mkdir()
    monkeypatch.setattr(persistence, "_ADDON_SECRETS_PATH", secrets_file)
    fake = _FakeSupervisor({**_default_options(), "anthropic_api_key": "private-provider-key"})
    _install_fake_supervisor(monkeypatch, fake)

    with pytest.raises(persistence._AddonPersistenceError, match="Unable to persist add-on credentials"):
        persistence._save_addon_option("broadcast_chain", True)

    assert fake.post_count == 0


def test_credential_read_back_failure_aborts_before_supervisor_post(monkeypatch, tmp_path):
    secrets_file = tmp_path / "secrets.env"
    monkeypatch.setattr(persistence, "_ADDON_SECRETS_PATH", secrets_file)
    fake = _FakeSupervisor({**_default_options(), "anthropic_api_key": "private-provider-key"})
    _install_fake_supervisor(monkeypatch, fake)
    real_read = persistence._read_secret_file
    read_count = 0

    def fail_second_read(path):
        nonlocal read_count
        read_count += 1
        if read_count == 2:
            raise persistence._AddonPersistenceError("Unable to persist add-on credentials")
        return real_read(path)

    monkeypatch.setattr(persistence, "_read_secret_file", fail_second_read)
    with pytest.raises(persistence._AddonPersistenceError, match="Unable to persist add-on credentials"):
        persistence._save_addon_option("broadcast_chain", True)
    assert read_count == 2
    assert fake.post_count == 0


def test_sensitive_values_never_reach_exception_or_logs(monkeypatch, caplog):
    canaries = (
        "supervisor-secret-token",
        "private-provider-key",
        "admin-canary",
        "jamendo-canary",
        "private response body",
        "private transport detail",
    )
    fake = _FakeSupervisor(
        {
            **_default_options(),
            "anthropic_api_key": "private-provider-key",
            "retired_mystery": "private response body",
        }
    )
    _install_fake_supervisor(monkeypatch, fake)

    with pytest.raises(persistence._AddonPersistenceError) as exc_info:
        persistence._save_addon_option("broadcast_chain", True)
    rendered = f"{exc_info.value}\n{caplog.text}"
    assert all(canary not in rendered for canary in canaries)
    assert exc_info.value.__cause__ is None


def test_transport_response_and_option_canaries_never_reach_exception_or_logs(monkeypatch, caplog):
    canaries = (
        "supervisor-secret-token",
        "private transport detail",
        "admin-canary",
        "jamendo-canary",
    )
    fake = _FakeSupervisor(_default_options())
    fake.post_mode = "disconnect"
    _install_fake_supervisor(monkeypatch, fake)

    with pytest.raises(persistence._AddonPersistenceError) as exc_info:
        persistence._save_addon_option("broadcast_chain", True)
    rendered = f"{exc_info.value}\n{caplog.text}"
    assert all(canary not in rendered for canary in canaries)
    assert exc_info.value.__cause__ is None


def test_concurrent_supervisor_saves_are_serialized_without_lost_updates(monkeypatch):
    fake = _FakeSupervisor(_default_options())
    _install_fake_supervisor(monkeypatch, fake)
    start = threading.Barrier(3)

    def save(key, value):
        start.wait()
        persistence._save_addon_option(key, value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(save, "chaos_mode_active", True)
        second = pool.submit(save, "broadcast_chain", True)
        start.wait()
        first.result(timeout=5)
        second.result(timeout=5)

    assert fake.post_count == 2
    assert fake.options["chaos_mode_active"] is True
    assert fake.options["broadcast_chain"] is True
    assert fake.max_request_overlap == 1


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
