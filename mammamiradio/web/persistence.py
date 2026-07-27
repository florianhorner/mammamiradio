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
import stat
import threading
from pathlib import Path

import httpx

from mammamiradio.core.models import StationState

_CREDENTIAL_FIELDS: dict[str, tuple[str, str]] = {
    "anthropic_api_key": ("ANTHROPIC_API_KEY", "anthropic_api_key"),
    "openai_api_key": ("OPENAI_API_KEY", "openai_api_key"),
    "azure_speech_key": ("AZURE_SPEECH_KEY", "azure_speech_key"),
    "azure_speech_region": ("AZURE_SPEECH_REGION", "azure_speech_region"),
    "elevenlabs_api_key": ("ELEVENLABS_API_KEY", "elevenlabs_api_key"),
}
_CREDENTIAL_ENV_TO_FIELD = {env_key: field for field, (env_key, _config_attr) in _CREDENTIAL_FIELDS.items()}
_ADDON_OPTIONS_LOCK = threading.RLock()
_ADDON_SECRETS_PATH = Path("/config/secrets.env")
_SUPERVISOR_API_DEFAULT = "http://supervisor"
# Serializes the .env read-modify-write. Without it, two admin saves of DIFFERENT
# settings (each guarded by its own asyncio lock in web/streamer.py, and each run
# in an executor thread) race on the shared .env.tmp path and one can silently lose
# the other's update. Add-on writes use Supervisor's option store under the separate
# _ADDON_OPTIONS_LOCK, so the two locks never contend.
_DOTENV_LOCK = threading.Lock()


class _AddonPersistenceError(RuntimeError):
    """A deliberately detail-free add-on persistence failure."""


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
        # mode is ignored when O_CREAT opens an existing stale temp file.
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            # Local HA add-on credentials are intentionally stored on the
            # operator's own config volume. The file is created 0600 and keeps
            # provider keys out of Supervisor options/diagnostics.
            # lgtm[py/clear-text-storage-sensitive-data]
            # codeql[py/clear-text-storage-sensitive-data]
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd != -1:
            os.close(fd)


def _fsync_parent_directory(path: Path) -> None:
    """Make an atomic replacement durable across a host crash."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path.parent, flags)
    try:
        os.fsync(fd)
    finally:
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
    with _DOTENV_LOCK:
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
        # The .env holds provider API keys; create it 0600 so it is not
        # world-readable under a default umask (same hardening as secrets.env).
        _write_owner_only_text(tmp, "\n".join(new_lines) + "\n")
        tmp.replace(env_path)


def _save_addon_option(key: str, value) -> None:
    """Persist a single add-on option through Home Assistant Supervisor."""
    _save_addon_option_batch({key: value})


def _read_secret_file(path: Path) -> tuple[list[str], dict[str, str]]:
    """Read provider assignments without evaluating shell expansions."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except (OSError, UnicodeError):
        raise _AddonPersistenceError("Unable to persist add-on credentials") from None

    assignments: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.lstrip("\ufeff") if line_number == 1 else raw_line
        stripped = line.strip()
        candidate = stripped[7:].lstrip() if stripped.startswith("export ") else stripped
        if not candidate or candidate.startswith("#") or "=" not in candidate:
            continue
        key, raw_value = candidate.split("=", 1)
        key = key.strip()
        if key not in _CREDENTIAL_ENV_TO_FIELD:
            continue
        value = raw_value.strip()
        if value[:1] in ('"', "'"):
            try:
                values = shlex.split(value, comments=False, posix=True)
            except ValueError:
                continue
            if len(values) != 1:
                continue
            value = values[0].strip()
        if value:
            assignments[key] = value
    return lines, assignments


def _write_and_verify_addon_secrets(updates: dict[str, str], *, existing_nonempty_wins: bool) -> None:
    """Atomically upsert provider secrets, then prove their value and permissions."""
    if not updates:
        return

    path = _ADDON_SECRETS_PATH
    lines, existing = _read_secret_file(path)
    chosen: dict[str, str] = {}
    preserve_existing: set[str] = set()
    for key, value in updates.items():
        if key not in _CREDENTIAL_ENV_TO_FIELD:
            continue
        if existing_nonempty_wins and existing.get(key):
            chosen[key] = existing[key]
            preserve_existing.add(key)
        else:
            chosen[key] = _sanitize_credential_value(value)
    if not chosen:
        return

    written: set[str] = set()
    new_lines: list[str] = []
    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.lstrip("\ufeff") if line_number == 1 else raw_line
        stripped = line.strip()
        candidate = stripped[7:].lstrip() if stripped.startswith("export ") else stripped
        key = candidate.split("=", 1)[0].strip() if "=" in candidate else ""
        if key in chosen:
            if key in preserve_existing:
                new_lines.append(raw_line)
                written.add(key)
                continue
            if key not in written:
                new_lines.append(_env_assignment(key, chosen[key]))
                written.add(key)
            continue
        new_lines.append(raw_line)
    for key in _CREDENTIAL_ENV_TO_FIELD:
        if key in chosen and key not in written:
            new_lines.append(_env_assignment(key, chosen[key]))

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        _write_owner_only_text(tmp_path, "\n".join(new_lines) + "\n")
        os.replace(tmp_path, path)
        _fsync_parent_directory(path)
        _lines, verified = _read_secret_file(path)
        if any(verified.get(key, "") != value for key, value in chosen.items()):
            raise _AddonPersistenceError("Unable to verify add-on credentials")
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise _AddonPersistenceError("Unable to verify add-on credentials")
    except _AddonPersistenceError:
        raise
    except OSError:
        raise _AddonPersistenceError("Unable to persist add-on credentials") from None
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _save_addon_options(updates: dict[str, str]) -> None:
    """Update only /config/secrets.env with provider credential values."""
    safe_updates: dict[str, str] = {}
    for key, value in updates.items():
        if key not in _CREDENTIAL_ENV_TO_FIELD:
            continue
        if not isinstance(value, str):
            raise _AddonPersistenceError("Unable to persist add-on credentials")
        safe_updates[key] = _sanitize_credential_value(value)
    with _ADDON_OPTIONS_LOCK:
        _write_and_verify_addon_secrets(safe_updates, existing_nonempty_wins=False)


def _parse_supervisor_info(response: httpx.Response) -> tuple[dict, set[str]]:
    """Validate the Supervisor info envelope and return options plus UI-schema names."""
    if not 200 <= response.status_code < 300:
        raise _AddonPersistenceError("Supervisor rejected the add-on option request")
    try:
        payload = response.json()
    except (TypeError, ValueError):
        raise _AddonPersistenceError("Supervisor returned an invalid add-on option response") from None
    if not isinstance(payload, dict) or payload.get("result") != "ok":
        raise _AddonPersistenceError("Supervisor returned an invalid add-on option response")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("options"), dict):
        raise _AddonPersistenceError("Supervisor returned an invalid add-on option response")
    schema = data.get("schema")
    if not isinstance(schema, list):
        raise _AddonPersistenceError("Supervisor returned an invalid add-on option schema")

    schema_names: set[str] = set()
    for entry in schema:
        if not isinstance(entry, dict):
            raise _AddonPersistenceError("Supervisor returned an invalid add-on option schema")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip() or name in schema_names:
            raise _AddonPersistenceError("Supervisor returned an invalid add-on option schema")
        schema_names.add(name)
    return dict(data["options"]), schema_names


def _get_supervisor_info(client: httpx.Client, *, reconciliation: bool = False) -> tuple[dict, set[str]]:
    try:
        response = client.get("/addons/self/info")
    except httpx.TransportError:
        message = (
            "Unable to confirm the Supervisor add-on option save"
            if reconciliation
            else "Unable to read Supervisor add-on options"
        )
        raise _AddonPersistenceError(message) from None
    try:
        return _parse_supervisor_info(response)
    except _AddonPersistenceError:
        if reconciliation:
            raise _AddonPersistenceError("Unable to confirm the Supervisor add-on option save") from None
        raise


def _post_was_acknowledged(response: httpx.Response) -> bool:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("result") == "ok"


def _matches_requested(options: dict, requested: dict) -> bool:
    return all(
        key in options and type(options[key]) is type(value) and options[key] == value
        for key, value in requested.items()
    )


def _migrate_retired_credentials(options: dict, schema_names: set[str]) -> None:
    retired: list[tuple[str, object]] = []
    for option_key, (env_key, _config_attr) in _CREDENTIAL_FIELDS.items():
        if option_key in schema_names or option_key not in options:
            continue
        value = options[option_key]
        if value in ("", None):
            continue
        retired.append((env_key, value))
    if not retired:
        return

    _lines, existing = _read_secret_file(_ADDON_SECRETS_PATH)
    legacy_updates: dict[str, str] = {}
    for env_key, value in retired:
        if existing.get(env_key):
            legacy_updates[env_key] = existing[env_key]
            continue
        if not isinstance(value, str):
            raise _AddonPersistenceError("Supervisor contains unsupported retired add-on options")
        sanitized = _sanitize_credential_value(value)
        if sanitized != value:
            raise _AddonPersistenceError("Supervisor contains unsupported retired add-on options")
        legacy_updates[env_key] = sanitized
    _write_and_verify_addon_secrets(legacy_updates, existing_nonempty_wins=True)


def _prepare_complete_options(current: dict, schema_names: set[str], updates: dict) -> dict:
    if any(not isinstance(key, str) or key not in schema_names for key in updates):
        raise _AddonPersistenceError("Requested add-on option is not in the active schema")

    unknown = set(current) - schema_names
    allowed_retired = set(_CREDENTIAL_FIELDS)
    if "claude_model" in unknown and ("quality_profile" in current or "quality_profile" in updates):
        allowed_retired.add("claude_model")
    if unknown - allowed_retired:
        raise _AddonPersistenceError("Supervisor contains unsupported retired add-on options")

    _migrate_retired_credentials(current, schema_names)
    merged = {key: value for key, value in current.items() if key in schema_names}
    merged.update(updates)
    return merged


def _save_addon_option_batch(updates: dict) -> None:
    """Persist several add-on options as one Supervisor-owned replacement."""
    if not updates:
        return

    token = os.getenv("SUPERVISOR_TOKEN") or os.getenv("HASSIO_TOKEN")
    if not token:
        raise _AddonPersistenceError("Supervisor credentials are unavailable")
    base_url = os.getenv("SUPERVISOR_API") or _SUPERVISOR_API_DEFAULT
    timeout = httpx.Timeout(connect=1.0, pool=1.0, read=5.0, write=5.0)

    with _ADDON_OPTIONS_LOCK:
        try:
            with httpx.Client(
                base_url=base_url.rstrip("/"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                current, schema_names = _get_supervisor_info(client)
                merged = _prepare_complete_options(current, schema_names, updates)
                try:
                    response = client.post("/addons/self/options", json={"options": merged})
                except httpx.TransportError:
                    response = None

                if response is not None and not 200 <= response.status_code < 300:
                    raise _AddonPersistenceError("Supervisor rejected the add-on option save")
                if response is not None and _post_was_acknowledged(response):
                    return

                reconciled, _schema_names = _get_supervisor_info(client, reconciliation=True)
                if _matches_requested(reconciled, updates):
                    return
                raise _AddonPersistenceError("Unable to confirm the Supervisor add-on option save")
        except _AddonPersistenceError:
            raise
        except (httpx.HTTPError, OSError, TypeError, ValueError):
            raise _AddonPersistenceError("Unable to persist Supervisor add-on options") from None
