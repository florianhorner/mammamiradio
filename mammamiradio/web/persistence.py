"""Credential persistence for the admin setup flow.

Extracted verbatim from ``web/streamer.py`` (god-module split). Writes operator
credentials to ``.env`` (standalone) or ``/config/secrets.env`` (HA add-on)
and applies them to the live env/config/state. Persistence I/O and live
application plus the credential field maps; the request-body parsing and the
route handlers stay in ``streamer``.
"""

from __future__ import annotations

import os
import shlex
import threading
from pathlib import Path

from mammamiradio.core.models import StationState

_CREDENTIAL_FIELDS: dict[str, tuple[str, str]] = {
    "anthropic_api_key": ("ANTHROPIC_API_KEY", "anthropic_api_key"),
    "openai_api_key": ("OPENAI_API_KEY", "openai_api_key"),
    "azure_speech_key": ("AZURE_SPEECH_KEY", "azure_speech_key"),
    "azure_speech_region": ("AZURE_SPEECH_REGION", "azure_speech_region"),
    "elevenlabs_api_key": ("ELEVENLABS_API_KEY", "elevenlabs_api_key"),
}
_CREDENTIAL_ENV_TO_FIELD = {env_key: field for field, (env_key, _config_attr) in _CREDENTIAL_FIELDS.items()}
_ADDON_OPTIONS_LOCK = threading.Lock()


def _sanitize_credential_value(value: str) -> str:
    """Strip env-breaking characters before persistence or live application."""
    return value.replace("\n", "").replace("\r", "")


def _env_assignment(key: str, value: str) -> str:
    """Serialize a KEY=VALUE line that the add-on secrets parser can read back."""
    return f"{key}={shlex.quote(value)}"


def _write_owner_only_text(path: Path, text: str) -> None:
    """Write a local operator-owned credential file with owner-only permissions."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            # Local HA add-on credentials are intentionally stored on the
            # operator's own config volume. The file is created 0600 and keeps
            # provider keys out of Supervisor options/diagnostics.
            # lgtm[py/clear-text-storage-sensitive-data]
            # codeql[py/clear-text-storage-sensitive-data]
            handle.write(text)
    finally:
        if fd != -1:
            os.close(fd)


def _apply_live_credentials(state: StationState, config, updates: dict[str, str]) -> None:
    for env_key, value in updates.items():
        os.environ[env_key] = value

    if "ANTHROPIC_API_KEY" in updates:
        config.anthropic_api_key = updates["ANTHROPIC_API_KEY"]
        from mammamiradio.hosts.scriptwriter import reset_provider_backoff

        reset_provider_backoff()
        state.anthropic_disabled_until = 0.0
        state.anthropic_last_error = ""
        # New key: prior verdict is meaningless until re-probed (save_keys schedules it).
        state.anthropic_key_status = "unverified"
        state.anthropic_key_checked_at = 0.0
    if "OPENAI_API_KEY" in updates:
        config.openai_api_key = updates["OPENAI_API_KEY"]
        state.openai_key_status = "unverified"
        state.openai_key_checked_at = 0.0
    if "AZURE_SPEECH_KEY" in updates:
        config.azure_speech_key = updates["AZURE_SPEECH_KEY"]
    if "AZURE_SPEECH_REGION" in updates:
        config.azure_speech_region = updates["AZURE_SPEECH_REGION"]
    if "ELEVENLABS_API_KEY" in updates:
        config.elevenlabs_api_key = updates["ELEVENLABS_API_KEY"]


def _save_dotenv(updates: dict[str, str]) -> None:
    """Write key=value pairs to .env, updating existing keys or appending new ones."""
    env_path = Path(".env")
    lines = env_path.read_text().splitlines() if env_path.exists() else []

    safe_updates = {k: _sanitize_credential_value(v) for k, v in updates.items()}

    written = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in safe_updates:
                new_lines.append(f'{key}="{safe_updates[key]}"')
                written.add(key)
                continue
        new_lines.append(line)

    for key, value in safe_updates.items():
        if key not in written:
            new_lines.append(f'{key}="{value}"')

    tmp = env_path.with_suffix(".env.tmp")
    tmp.write_text("\n".join(new_lines) + "\n")
    tmp.replace(env_path)


def _save_addon_option(key: str, value) -> None:
    """Persist a single option into /data/options.json atomically."""
    import json as _json
    import os as _os

    with _ADDON_OPTIONS_LOCK:
        options_path = Path("/data/options.json")
        options: dict = {}
        if options_path.exists():
            try:
                options = _json.loads(options_path.read_text())
            except (ValueError, OSError):
                options = {}
        options[key] = value
        tmp_path = options_path.with_suffix(options_path.suffix + ".tmp")
        tmp_path.write_text(_json.dumps(options, indent=2))
        _os.replace(tmp_path, options_path)


def _save_addon_options(updates: dict[str, str]) -> None:
    """Update /config/secrets.env with new provider credential values."""
    import json as _json
    import os as _os

    with _ADDON_OPTIONS_LOCK:
        options_path = Path("/data/options.json")
        secrets_path = Path("/config/secrets.env")
        lines = secrets_path.read_text().splitlines() if secrets_path.exists() else []

        options: dict = {}
        if options_path.exists():
            try:
                loaded_options = _json.loads(options_path.read_text())
                if isinstance(loaded_options, dict):
                    options = loaded_options
            except (ValueError, OSError):
                options = {}

        safe_updates = {k: _sanitize_credential_value(v) for k, v in updates.items() if k in _CREDENTIAL_ENV_TO_FIELD}
        legacy_updates = {}
        for opt_key, (env_key, _config_attr) in _CREDENTIAL_FIELDS.items():
            value = options.get(opt_key)
            if value and env_key not in safe_updates:
                legacy_updates[env_key] = _sanitize_credential_value(str(value))

        secret_updates = {**legacy_updates, **safe_updates}
        if not secret_updates:
            return

        written = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            candidate = stripped[7:].lstrip() if stripped.startswith("export ") else stripped
            if candidate and not candidate.startswith("#") and "=" in candidate:
                key = candidate.split("=", 1)[0].strip()
                if key in secret_updates:
                    new_lines.append(_env_assignment(key, secret_updates[key]))
                    written.add(key)
                    continue
            new_lines.append(line)
        for _opt_key, (env_key, _config_attr) in _CREDENTIAL_FIELDS.items():
            if env_key in secret_updates and env_key not in written:
                new_lines.append(_env_assignment(env_key, secret_updates[env_key]))

        tmp_path = secrets_path.with_suffix(secrets_path.suffix + ".tmp")
        _write_owner_only_text(tmp_path, "\n".join(new_lines) + "\n")
        _os.replace(tmp_path, secrets_path)

        pruned_options = dict(options)
        for opt_key in _CREDENTIAL_FIELDS:
            pruned_options.pop(opt_key, None)
        if pruned_options != options:
            tmp_options_path = options_path.with_suffix(options_path.suffix + ".tmp")
            tmp_options_path.write_text(_json.dumps(pruned_options, indent=2) + "\n")
            _os.replace(tmp_options_path, options_path)
