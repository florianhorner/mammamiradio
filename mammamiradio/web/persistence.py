"""Credential persistence for the admin setup flow.

Extracted verbatim from ``web/streamer.py`` (god-module split). Writes operator
credentials to ``.env`` (standalone) or ``/data/options.json`` (HA add-on) and
applies them to the live env/config/state. Persistence I/O and live application
plus the credential field maps; the request-body parsing and the route handlers
stay in ``streamer``.
"""

from __future__ import annotations

import os
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
_ADDON_SECRETS_PATH = "/config/secrets.env"
_ADDON_OPTIONS_PATH = "/data/options.json"


def _sanitize_credential_value(value: str) -> str:
    """Strip env-breaking characters before persistence or live application."""
    return value.replace("\n", "").replace("\r", "")


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
    """Update /config/secrets.env with provider credential values."""
    import json as _json
    import os as _os
    import shlex as _shlex

    with _ADDON_OPTIONS_LOCK:
        secrets_path = Path(_ADDON_SECRETS_PATH)
        secrets_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        existing: dict[str, str] = {}
        passthrough: list[str] = []
        if secrets_path.exists():
            for raw_line in secrets_path.read_text().splitlines():
                stripped = raw_line.strip()
                if not stripped or stripped.startswith("#"):
                    passthrough.append(raw_line)
                    continue
                try:
                    parsed = _shlex.split(stripped, comments=False, posix=True)
                except ValueError:
                    passthrough.append(raw_line)
                    continue
                if len(parsed) != 1 or "=" not in parsed[0]:
                    passthrough.append(raw_line)
                    continue
                key, value = parsed[0].split("=", 1)
                if key in _CREDENTIAL_ENV_TO_FIELD:
                    existing[key] = value
                else:
                    passthrough.append(raw_line)

        for env_key, value in updates.items():
            if env_key in _CREDENTIAL_ENV_TO_FIELD:
                existing[env_key] = _sanitize_credential_value(value)

        lines = passthrough
        lines.extend(f"{key}={_shlex.quote(value)}" for key, value in existing.items() if value)
        tmp_path = secrets_path.with_suffix(secrets_path.suffix + ".tmp")
        # Create with 0600 from the start — a secrets file must never have a
        # world-readable window between write and chmod.
        fd = _os.open(tmp_path, _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
        try:
            with _os.fdopen(fd, "w") as handle:
                handle.write("\n".join(lines) + "\n")
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        _os.replace(tmp_path, secrets_path)

        options_path = Path(_ADDON_OPTIONS_PATH)
        if options_path.exists():
            try:
                options = _json.loads(options_path.read_text())
            except (ValueError, OSError):
                options = None
            if isinstance(options, dict):
                changed = False
                for opt_key in _CREDENTIAL_FIELDS:
                    if opt_key in options:
                        options.pop(opt_key, None)
                        changed = True
                if changed:
                    tmp_options_path = options_path.with_suffix(options_path.suffix + ".tmp")
                    tmp_options_path.write_text(_json.dumps(options, indent=2))
                    _os.replace(tmp_options_path, options_path)
