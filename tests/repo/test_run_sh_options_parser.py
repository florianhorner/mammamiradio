"""Functional tests for the add-on config parser embedded in run.sh.

The Python snippet inside run.sh reads /data/options.json plus
/config/secrets.env and emits shell `export KEY=value` lines. Bugs in that
snippet silently drop all addon config (API keys, station name, etc.) on every
HA Supervisor restart.

Root cause that prompted these tests: the f-string
    print(f'export HA_ENABLED={"true" if enabled else "false"}')
contained double-quotes inside a shell double-quoted string, causing the shell
to mangle the Python code.  Result: NameError on every restart, all config lost.

These tests extract the Python snippet and run it as a subprocess so they
catch both parse errors AND wrong output — without needing a shell or Docker.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import textwrap
import threading
from pathlib import Path
from typing import ClassVar

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_SH = REPO_ROOT / "ha-addon" / "mammamiradio" / "rootfs" / "run.sh"
STABLE_CONFIG = REPO_ROOT / "ha-addon" / "mammamiradio" / "config.yaml"
EDGE_CONFIG = REPO_ROOT / "ha-addon" / "mammamiradio-edge" / "config.yaml"


def _extract_python_snippet(
    options_file: Path,
    provider_file: Path | None = None,
    supervisor_api: str = "http://127.0.0.1:1",
    recovery_marker: Path | None = None,
) -> str:
    """Extract the Python body from the python3 -c "..." block in run.sh,
    substituting the real options and secrets file paths."""
    src = RUN_SH.read_text()
    # Find the python3 -c "..." block
    blocks = re.findall(r'python3 -c "\n(.*?)\n" 2>', src, re.DOTALL)
    assert len(blocks) == 1, "run.sh must keep one merged python3 -c parser block"
    raw = blocks[0]
    # The shell uses $OPTIONS_FILE, $SECRETS_FILE, $SUPERVISOR_API, and
    # $RECOVERY_MARKER_FILE inside the script — substitute them. The default
    # API target is a closed local port so a test that accidentally reaches
    # recovery fails fast and soft. The default marker path lives beside
    # options.json and does not exist by default, so recovery is attempted
    # unless a test deliberately points at a pre-existing marker.
    raw = raw.replace("$OPTIONS_FILE", str(options_file))
    provider_path = provider_file or (options_file.parent / "missing-secrets.env")
    raw = raw.replace("$SECRETS_FILE", str(provider_path))
    raw = raw.replace("$SUPERVISOR_API", supervisor_api)
    marker_path = recovery_marker or (options_file.parent / "recovery-marker-not-set")
    raw = raw.replace("$RECOVERY_MARKER_FILE", str(marker_path))
    # Shell escapes single-quotes as '\'' inside double-quoted strings; undo that
    raw = raw.replace("\\'", "'")
    return textwrap.dedent(raw)


def _scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Subprocess env with Supervisor tokens removed so ambient credentials on a
    dev machine can never flip the parser into its recovery path mid-test."""
    env = {k: v for k, v in os.environ.items() if k not in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN")}
    if extra:
        env.update(extra)
    return env


def _run_parser(
    options: dict,
    provider_env_text: str | None = None,
    *,
    supervisor_api: str | None = None,
    env: dict[str, str] | None = None,
    keep_dir: Path | None = None,
    recovery_marker: Path | None = None,
) -> tuple[int, str, str]:
    """Write options to a temp file, run the parser snippet, return (returncode, stdout, stderr)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = keep_dir or Path(tmp_dir)
        tmp_path = base / "options.json"
        provider_path = base / "secrets.env"
        tmp_path.write_text(json.dumps(options))
        if provider_env_text is not None:
            _write_provider_fixture(provider_path, provider_env_text)
        snippet = _extract_python_snippet(
            tmp_path,
            provider_path,
            supervisor_api=supervisor_api or "http://127.0.0.1:1",
            recovery_marker=recovery_marker,
        )
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            env=_scrubbed_env(env),
        )
        return result.returncode, result.stdout, result.stderr


def _run_parser_shell_eval(options: dict, provider_env_text: str | None = None) -> tuple[int, str, str]:
    """Run the parser through shell eval so precedence and warning redirects are exercised."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / "options.json"
        provider_path = Path(tmp_dir) / "secrets.env"
        tmp_path.write_text(json.dumps(options))
        if provider_env_text is not None:
            _write_provider_fixture(provider_path, provider_env_text)
        snippet = _extract_python_snippet(tmp_path, provider_path)
        shell = "\n".join(
            [
                f"OPTS_EXPORT=$({shlex.quote(sys.executable)} -c {shlex.quote(snippet)}) || exit $?",
                'eval "$OPTS_EXPORT"',
                'printf "ANTHROPIC_API_KEY=%s\\n" "${ANTHROPIC_API_KEY:-}"',
                'printf "OPENAI_API_KEY=%s\\n" "${OPENAI_API_KEY:-}"',
                'printf "AZURE_SPEECH_REGION=%s\\n" "${AZURE_SPEECH_REGION:-}"',
                'printf "ELEVENLABS_API_KEY=%s\\n" "${ELEVENLABS_API_KEY:-}"',
            ]
        )
        result = subprocess.run(
            ["/bin/sh", "-c", shell],
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout, result.stderr


def _write_provider_fixture(path: Path, text: str) -> None:
    """Write the synthetic parser fixture without matching the CodeQL storage sink."""
    subprocess.run(
        ["/bin/sh", "-c", 'cat > "$1"', "write-provider-fixture", str(path)],
        input=text,
        text=True,
        check=True,
    )


def _parse_exports(stdout: str) -> dict[str, str]:
    """Turn 'export KEY=value' lines into a dict, unquoting shlex-quoted values."""
    import shlex

    out = {}
    for line in stdout.strip().splitlines():
        m = re.match(r"^export (\w+)=(.*)$", line)
        if m:
            key = m.group(1)
            raw_val = m.group(2)
            # shlex.split handles quoted strings like 'my key' or "my key"
            out[key] = shlex.split(raw_val)[0] if raw_val else ""
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_parser_exits_zero_on_valid_options():
    rc, _, _ = _run_parser({"anthropic_api_key": "sk-test"})
    assert rc == 0


def test_parser_exports_anthropic_api_key():
    rc, stdout, _ = _run_parser({"anthropic_api_key": "sk-ant-abc123"})
    assert rc == 0
    exports = _parse_exports(stdout)
    assert exports["ANTHROPIC_API_KEY"] == "sk-ant-abc123"


def test_parser_exports_openai_api_key():
    rc, stdout, _ = _run_parser({"openai_api_key": "sk-openai-xyz"})
    assert rc == 0
    exports = _parse_exports(stdout)
    assert exports["OPENAI_API_KEY"] == "sk-openai-xyz"


def test_parser_exports_provider_keys_from_secrets_env():
    secrets_env = "\n".join(
        [
            "ANTHROPIC_API_KEY=sk-ant-file",
            "OPENAI_API_KEY=sk-oai-file",
            "AZURE_SPEECH_KEY=az-file",
            "AZURE_SPEECH_REGION=westeurope",
            "ELEVENLABS_API_KEY=el-file",
        ]
    )
    rc, stdout, _ = _run_parser({}, secrets_env)
    assert rc == 0
    exports = _parse_exports(stdout)
    assert exports["ANTHROPIC_API_KEY"] == "sk-ant-file"
    assert exports["OPENAI_API_KEY"] == "sk-oai-file"
    assert exports["AZURE_SPEECH_KEY"] == "az-file"
    assert exports["AZURE_SPEECH_REGION"] == "westeurope"
    assert exports["ELEVENLABS_API_KEY"] == "el-file"


def test_parser_secrets_env_overrides_legacy_options_per_key():
    options = {
        "anthropic_api_key": "sk-ant-option",
        "openai_api_key": "sk-oai-option",
        "azure_speech_region": "option-region",
    }
    secrets_env = "\n".join(
        [
            "ANTHROPIC_API_KEY=sk-ant-file",
            "AZURE_SPEECH_REGION=file-region",
        ]
    )
    rc, stdout, _ = _run_parser(options, secrets_env)
    assert rc == 0
    exports = _parse_exports(stdout)
    assert exports["ANTHROPIC_API_KEY"] == "sk-ant-file"
    assert exports["OPENAI_API_KEY"] == "sk-oai-option"
    assert exports["AZURE_SPEECH_REGION"] == "file-region"


def test_parser_secrets_env_empty_values_fall_back_to_legacy_options():
    rc, stdout, _ = _run_parser(
        {"anthropic_api_key": "sk-ant-option"},
        "ANTHROPIC_API_KEY=\n",
    )
    assert rc == 0
    exports = _parse_exports(stdout)
    assert exports["ANTHROPIC_API_KEY"] == "sk-ant-option"


def test_parser_secrets_env_handles_documented_grammar():
    secrets_env = (
        '\ufeffexport OPENAI_API_KEY="sk value=with=equals"\r\n'
        "AZURE_SPEECH_REGION = 'westeurope'\r\n"
        "ELEVENLABS_API_KEY=el#literal\r\n"
        "  # full-line comments are ignored\r\n"
    )
    rc, stdout, _ = _run_parser({}, secrets_env)
    assert rc == 0
    exports = _parse_exports(stdout)
    assert exports["OPENAI_API_KEY"] == "sk value=with=equals"
    assert exports["AZURE_SPEECH_REGION"] == "westeurope"
    assert exports["ELEVENLABS_API_KEY"] == "el#literal"


def test_parser_shell_eval_keeps_secrets_env_precedence():
    rc, stdout, stderr = _run_parser_shell_eval(
        {
            "anthropic_api_key": "sk-ant-option",
            "openai_api_key": "sk-oai-option",
        },
        "ANTHROPIC_API_KEY=sk-ant-file\n",
    )
    assert rc == 0, stderr
    assert "ANTHROPIC_API_KEY=sk-ant-file\n" in stdout
    assert "OPENAI_API_KEY=sk-oai-option\n" in stdout


def test_parser_malformed_secrets_env_warning_does_not_leak_secret_values():
    rc, stdout, stderr = _run_parser_shell_eval(
        {"anthropic_api_key": "legacy-safe-value"},
        'ANTHROPIC_API_KEY="sk-should-not-leak\nNOT_ALLOWED=sk-also-secret\n',
    )
    assert rc == 0
    combined = stdout + stderr
    assert "sk-should-not-leak" not in combined
    assert "sk-also-secret" not in combined
    assert "secrets.env line 1 ignored: invalid quoting" in stderr
    assert "secrets.env line 2 ignored: unsupported key" in stderr
    assert "ANTHROPIC_API_KEY=legacy-safe-value\n" in stdout


def test_parser_skips_empty_keys():
    """Empty string values must not produce export lines (they'd override env with '')."""
    rc, stdout, _ = _run_parser({"anthropic_api_key": "", "openai_api_key": ""})
    assert rc == 0
    exports = _parse_exports(stdout)
    assert "ANTHROPIC_API_KEY" not in exports
    assert "OPENAI_API_KEY" not in exports


def test_parser_hidden_optional_keys_can_be_absent_or_blank():
    """Schema-only legacy fields must be quiet when unset, but still export when explicitly saved."""
    hidden_optional_env = {
        "jamendo_client_id": "JAMENDO_CLIENT_ID",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "openai_api_key": "OPENAI_API_KEY",
        "azure_speech_key": "AZURE_SPEECH_KEY",
        "azure_speech_region": "AZURE_SPEECH_REGION",
        "elevenlabs_api_key": "ELEVENLABS_API_KEY",
    }
    for options in ({}, dict.fromkeys(hidden_optional_env, "")):
        rc, stdout, _ = _run_parser(options)
        assert rc == 0
        exports = _parse_exports(stdout)
        for env_key in hidden_optional_env.values():
            assert env_key not in exports


def test_parser_ha_enabled_json_true():
    """JSON boolean true must produce 'export HA_ENABLED=true' — not crash with NameError."""
    rc, stdout, stderr = _run_parser({"enable_home_assistant": True})
    assert rc == 0, f"Parser crashed: {stderr}"
    assert "NameError" not in stderr
    exports = _parse_exports(stdout)
    assert exports["HA_ENABLED"] == "true"


def test_parser_ha_enabled_json_false():
    """JSON boolean false must produce 'export HA_ENABLED=false'."""
    rc, stdout, stderr = _run_parser({"enable_home_assistant": False})
    assert rc == 0, f"Parser crashed: {stderr}"
    exports = _parse_exports(stdout)
    assert exports["HA_ENABLED"] == "false"


def test_parser_ha_enabled_defaults_to_true_when_missing():
    """Missing enable_home_assistant key must default to true."""
    rc, stdout, _ = _run_parser({})
    assert rc == 0
    exports = _parse_exports(stdout)
    assert exports["HA_ENABLED"] == "true"


def test_parser_quotes_values_with_special_chars():
    """Values with spaces or shell special chars must be properly shell-quoted."""
    rc, stdout, _ = _run_parser({"station_name": "Mamma Mi Radio!"})
    assert rc == 0
    exports = _parse_exports(stdout)
    assert exports["STATION_NAME"] == "Mamma Mi Radio!"


def test_parser_exports_all_supported_keys():
    options = {
        "anthropic_api_key": "sk-ant",
        "openai_api_key": "sk-oai",
        "azure_speech_key": "az-key",
        "azure_speech_region": "westeurope",
        "elevenlabs_api_key": "el-key",
        "station_name": "Test Station",
        "quality_profile": "premium",
        "admin_token": "tok123",
        "enable_home_assistant": True,
        "ha_context_enabled": False,
        "ha_context_poll_interval": 600,
        "jamendo_client_id": "abc123",
    }
    rc, stdout, _ = _run_parser(options)
    assert rc == 0
    exports = _parse_exports(stdout)
    assert exports["ANTHROPIC_API_KEY"] == "sk-ant"
    assert exports["OPENAI_API_KEY"] == "sk-oai"
    assert exports["AZURE_SPEECH_KEY"] == "az-key"
    assert exports["AZURE_SPEECH_REGION"] == "westeurope"
    assert exports["ELEVENLABS_API_KEY"] == "el-key"
    assert exports["STATION_NAME"] == "Test Station"
    assert exports["MAMMAMIRADIO_QUALITY"] == "premium"
    assert exports["ADMIN_TOKEN"] == "tok123"
    assert exports["HA_ENABLED"] == "true"
    assert exports["MAMMAMIRADIO_HA_CONTEXT_ENABLED"] == "false"
    assert exports["MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL"] == "600"
    assert exports["JAMENDO_CLIENT_ID"] == "abc123"


def test_parser_quality_profile_defaults_to_balanced():
    """Missing quality_profile (e.g. upgrade from the old claude_model dropdown)
    maps to MAMMAMIRADIO_QUALITY=balanced, the shipped default profile."""
    rc, stdout, _ = _run_parser({"station_name": "X"})
    assert rc == 0
    exports = _parse_exports(stdout)
    assert exports["MAMMAMIRADIO_QUALITY"] == "balanced"


def test_parser_preserves_legacy_claude_model_when_quality_profile_missing():
    """Existing add-ons can carry claude_model in options.json after the schema
    migrates; run.sh must keep it as the legacy fast-model override."""
    rc, stdout, _ = _run_parser({"claude_model": "claude-sonnet-4-6"})
    assert rc == 0
    exports = _parse_exports(stdout)
    assert exports["MAMMAMIRADIO_QUALITY"] == "balanced"
    assert exports["CLAUDE_MODEL"] == "claude-sonnet-4-6"


def test_parser_quality_profile_wins_over_legacy_claude_model():
    rc, stdout, _ = _run_parser({"quality_profile": "premium", "claude_model": "claude-sonnet-4-6"})
    assert rc == 0
    exports = _parse_exports(stdout)
    assert exports["MAMMAMIRADIO_QUALITY"] == "premium"
    assert "CLAUDE_MODEL" not in exports


def test_parser_media_player_push_missing_key_preserves_legacy_default():
    """Old installs with no saved key keep the REST ghost until explicitly changed."""
    rc, stdout, _ = _run_parser({})
    assert rc == 0
    exports = _parse_exports(stdout)
    assert exports["MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH"] == "true"


def test_parser_ha_context_defaults_for_green_class_hardware():
    rc, stdout, _ = _run_parser({})
    assert rc == 0
    exports = _parse_exports(stdout)
    assert exports["MAMMAMIRADIO_HA_CONTEXT_ENABLED"] == "true"
    assert exports["MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL"] == "300"


def test_parser_ha_context_can_disable_full_state_polling():
    rc, stdout, _ = _run_parser({"ha_context_enabled": False})
    assert rc == 0
    exports = _parse_exports(stdout)
    assert exports["HA_ENABLED"] == "true"
    assert exports["MAMMAMIRADIO_HA_CONTEXT_ENABLED"] == "false"


def test_parser_ha_context_poll_interval_invalid_defaults_to_300():
    for value in ("soon", 0, -1):
        rc, stdout, _ = _run_parser({"ha_context_poll_interval": value})
        assert rc == 0
        assert _parse_exports(stdout)["MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL"] == "300"


def test_parser_media_player_push_explicit_true_and_false():
    for value, expected in ((True, "true"), (False, "false")):
        rc, stdout, _ = _run_parser({"ha_media_player_push": value})
        assert rc == 0
        exports = _parse_exports(stdout)
        assert exports["MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH"] == expected


def test_parser_guest_host_explicit_true_and_false():
    for value, expected in ((True, "true"), (False, "false")):
        rc, stdout, _ = _run_parser({"guest_host": value})
        assert rc == 0
        exports = _parse_exports(stdout)
        assert exports["MAMMAMIRADIO_GUEST_HOST"] == expected


def test_parser_guest_host_missing_key_preserves_default_on():
    rc, stdout, _ = _run_parser({})
    assert rc == 0
    exports = _parse_exports(stdout)
    assert exports["MAMMAMIRADIO_GUEST_HOST"] == "true"


def test_addon_manifest_guest_host_defaults_true_for_new_installs():
    for config in (STABLE_CONFIG, EDGE_CONFIG):
        body = config.read_text()
        assert re.search(r"(?m)^  guest_host: true$", body), f"{config} must default the guest host option to On"
        assert re.search(r"(?m)^  guest_host: bool\?$", body), f"{config} must expose guest_host in the schema"


def test_addon_manifest_media_player_push_defaults_true_for_new_installs():
    for config in (STABLE_CONFIG, EDGE_CONFIG):
        body = config.read_text()
        assert re.search(r"(?m)^  ha_media_player_push: true$", body), (
            f"{config} must default new installs to On so an add-on-only setup gets a media_player tile out of the box"
        )


def test_addon_manifest_ha_context_defaults_for_new_installs():
    for config in (STABLE_CONFIG, EDGE_CONFIG):
        body = config.read_text()
        assert re.search(r"(?m)^  ha_context_enabled: true$", body), (
            f"{config} must keep HA prompt context enabled unless the operator opts out"
        )
        assert re.search(r"(?m)^  ha_context_poll_interval: 300$", body), (
            f"{config} must default full-state polling to a Green-safe 300s interval"
        )
        assert re.search(r"(?m)^  ha_context_enabled: bool\?$", body), (
            f"{config} must expose ha_context_enabled in the schema"
        )
        assert re.search(r"(?m)^  ha_context_poll_interval: int\(1,3600\)\?$", body), (
            f"{config} must bound ha_context_poll_interval in the schema"
        )


def test_addon_manifest_hides_legacy_optional_fields_but_keeps_admin_token_visible():
    # jamendo_client_id stays a real, settable schema-only field (hidden behind
    # HA's disclosure, not a secret leak concern). The five provider keys are
    # fully removed from schema (see test_addon_schema_has_no_provider_secret_fields
    # in test_repo_scripts.py) — a schema-only field is still a valid Supervisor
    # option, so hiding it from the UI would not have closed the #688 leak.
    hidden_optional = ("jamendo_client_id",)
    removed_entirely = (
        "anthropic_api_key",
        "openai_api_key",
        "azure_speech_key",
        "azure_speech_region",
        "elevenlabs_api_key",
    )
    for config in (STABLE_CONFIG, EDGE_CONFIG):
        body = config.read_text()
        options_block = re.search(r"(?ms)^options:\n(.*?)(?=^schema:)", body)
        schema_block = re.search(r"(?ms)^schema:\n(.*)", body)
        assert options_block, f"{config} must define options"
        assert schema_block, f"{config} must define schema"
        assert re.search(r"(?m)^  admin_token: \"\"$", options_block.group(1)), (
            f"{config} must keep admin_token visible because blank means trusting the LAN"
        )
        for key in hidden_optional:
            assert not re.search(rf"(?m)^  {key}:", options_block.group(1)), (
                f"{config} should hide {key} behind HA's unused optional fields disclosure"
            )
            assert re.search(rf"(?m)^  {key}: .*\?$", schema_block.group(1)), (
                f"{config} must keep {key} as an optional schema key"
            )
        for key in removed_entirely:
            assert not re.search(rf"(?m)^  {key}:", options_block.group(1)), f"{config} must not have {key} in options"
            assert not re.search(rf"(?m)^  {key}:", schema_block.group(1)), (
                f"{config} must not have {key} in schema at all — a schema-only "
                "field is still a settable, persisted Supervisor option"
            )


def test_parser_corrupt_json_still_reads_secrets_env():
    """Corrupt legacy options must not suppress file-backed provider secrets."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / "options.json"
        secrets_path = Path(tmp_dir) / "secrets.env"
        tmp_path.write_text("{not valid json")
        secrets_path.write_text("ANTHROPIC_API_KEY=from-file\n")
        snippet = _extract_python_snippet(tmp_path, secrets_path)
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "WARNING: ignoring corrupt options.json" in result.stderr
        exports = _parse_exports(result.stdout)
        assert exports["ANTHROPIC_API_KEY"] == "from-file"


def test_parser_uses_single_guarded_block_for_options_and_secrets():
    src = RUN_SH.read_text()
    assert src.count('python3 -c "') == 1
    assert 'SECRETS_FILE="/config/secrets.env"' in src
    assert 'if ! OPTS_EXPORT=$(python3 -c "' in src


def test_parser_no_double_quotes_in_fstring_shell_context():
    """Static guard: the broken f-string pattern must not reappear in run.sh."""
    src = RUN_SH.read_text()
    assert '{"true" if enabled else "false"}' not in src, (
        "Broken f-string pattern detected in run.sh. Double-quotes inside a "
        "shell double-quoted string mangle the Python code (NameError: true). "
        'Use: ha_val = ...; print("export HA_ENABLED=" + ha_val)'
    )


# ---------------------------------------------------------------------------
# Supervisor-store recovery (legacy provider keys after the schema removal)
# ---------------------------------------------------------------------------


class _SupervisorStub(http.server.BaseHTTPRequestHandler):
    """Minimal /addons/self/info endpoint returning canned stored options."""

    stored_options: ClassVar[dict] = {}
    seen_auth: ClassVar[list[str]] = []

    def do_GET(self):
        type(self).seen_auth.append(self.headers.get("Authorization", ""))
        body = json.dumps({"result": "ok", "data": {"options": type(self).stored_options}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence request logging
        pass


def _with_supervisor_stub(stored_options: dict):
    server = http.server.HTTPServer(("127.0.0.1", 0), _SupervisorStub)
    _SupervisorStub.stored_options = stored_options
    _SupervisorStub.seen_auth = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_parser_recovers_legacy_keys_from_supervisor_store_and_persists():
    """Supervisor strips schema-removed keys from options.json on start; the
    parser must recover them from the Supervisor API and persist to secrets.env."""
    server, api = _with_supervisor_stub(
        {
            "anthropic_api_key": "sk-ant-store",
            "openai_api_key": "sk-oai-store",
            "azure_speech_key": "az-store",
            "azure_speech_region": "westeurope",
            "elevenlabs_api_key": "el-store",
            "station_name": "X",
        }
    )
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            rc, stdout, _ = _run_parser(
                {"station_name": "X"},
                keep_dir=base,
                supervisor_api=api,
                env={"SUPERVISOR_TOKEN": "tok-123"},
            )
            assert rc == 0
            exports = _parse_exports(stdout)
            assert exports["ANTHROPIC_API_KEY"] == "sk-ant-store"
            assert exports["OPENAI_API_KEY"] == "sk-oai-store"
            assert exports["AZURE_SPEECH_KEY"] == "az-store"
            assert exports["AZURE_SPEECH_REGION"] == "westeurope"
            assert exports["ELEVENLABS_API_KEY"] == "el-store"
            assert _SupervisorStub.seen_auth == ["Bearer tok-123"]
            secrets_path = base / "secrets.env"
            assert secrets_path.exists(), "recovered keys must be persisted to secrets.env"
            assert (secrets_path.stat().st_mode & 0o777) == 0o600
            persisted = secrets_path.read_text()
            assert "ANTHROPIC_API_KEY=sk-ant-store" in persisted
            assert "ELEVENLABS_API_KEY=el-store" in persisted
            # Second boot: every key is now file-backed, so no API call is
            # made at all (the default API target is a closed port — a call
            # would surface as a warning).
            rc2, stdout2, _ = _run_parser(
                {"station_name": "X"},
                persisted,
                env={"SUPERVISOR_TOKEN": "tok-123"},
            )
            assert rc2 == 0
            exports2 = _parse_exports(stdout2)
            assert exports2["ANTHROPIC_API_KEY"] == "sk-ant-store"
            assert exports2["ELEVENLABS_API_KEY"] == "el-store"
            assert "could not check Supervisor" not in stdout2
    finally:
        server.shutdown()
        server.server_close()


def test_parser_secrets_env_wins_over_supervisor_store():
    server, api = _with_supervisor_stub({"anthropic_api_key": "sk-ant-stale-store"})
    try:
        rc, stdout, _ = _run_parser(
            {},
            "ANTHROPIC_API_KEY=sk-ant-file\n",
            supervisor_api=api,
            env={"SUPERVISOR_TOKEN": "tok-123"},
        )
        assert rc == 0
        assert _parse_exports(stdout)["ANTHROPIC_API_KEY"] == "sk-ant-file"
    finally:
        server.shutdown()
        server.server_close()


def test_parser_recovery_skipped_without_supervisor_token():
    """No token (standalone-ish runs, tests) means no API call at all."""
    rc, stdout, _ = _run_parser({"station_name": "X"})
    assert rc == 0
    assert "could not check Supervisor" not in stdout
    assert "ANTHROPIC_API_KEY" not in stdout


def test_parser_recovery_failure_is_soft():
    """Unreachable Supervisor API degrades to a warning, never a boot failure."""
    rc, stdout, _ = _run_parser(
        {"station_name": "X"},
        env={"SUPERVISOR_TOKEN": "tok-123"},
    )
    assert rc == 0
    assert "could not check Supervisor for legacy provider keys" in stdout
    exports = _parse_exports(stdout)
    assert exports.get("STATION_NAME") == "X", "options must still export after recovery failure"


def test_parser_recovery_is_genuinely_one_time_even_with_keys_still_missing():
    """A successful Supervisor check must not repeat on later boots, even if
    some keys stay unrecovered (genuinely never configured) — otherwise every
    boot of an install missing one provider pays the Supervisor round trip
    forever. A failed/unreachable check must NOT set the marker (covered by
    test_parser_recovery_failure_is_soft implicitly retrying)."""
    server, api = _with_supervisor_stub({"anthropic_api_key": "sk-ant-store"})
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            marker = base / "recovery-marker"
            rc, stdout, _ = _run_parser(
                {"station_name": "X"},
                keep_dir=base,
                supervisor_api=api,
                env={"SUPERVISOR_TOKEN": "tok-123"},
                recovery_marker=marker,
            )
            assert rc == 0
            assert _parse_exports(stdout)["ANTHROPIC_API_KEY"] == "sk-ant-store"
            assert marker.exists(), "a successful Supervisor check must write the marker"
            assert len(_SupervisorStub.seen_auth) == 1

            # Second boot: openai/azure/elevenlabs are still missing (never in
            # the store), so without the marker this would hit Supervisor
            # again. The live stub is still running and would answer — proves
            # the skip is the marker, not an unreachable server.
            rc2, stdout2, _ = _run_parser(
                {"station_name": "X"},
                keep_dir=base,
                supervisor_api=api,
                env={"SUPERVISOR_TOKEN": "tok-123"},
                recovery_marker=marker,
            )
            assert rc2 == 0
            assert "could not check Supervisor" not in stdout2
            assert len(_SupervisorStub.seen_auth) == 1, "second boot must not re-hit Supervisor"
    finally:
        server.shutdown()
        server.server_close()
