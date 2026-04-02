"""Helpers for reading and syncing go-librespot's lightweight YAML config."""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

DEFAULT_DEVICE_NAME = "mammamiradio"
_DEVICE_NAME_RE = re.compile(r"^(?P<indent>\s*)device_name:\s*(?P<value>.*?)\s*$")


def _strip_yaml_comment(value: str) -> str:
    in_single = False
    in_double = False
    out: list[str] = []

    for char in value:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            break
        out.append(char)

    return "".join(out).strip()


def _parse_yaml_scalar(value: str) -> str:
    raw = _strip_yaml_comment(value)
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        unquoted = raw[1:-1]
        if raw[0] == "'":
            return unquoted.replace("''", "'")
        return unquoted.replace('\\"', '"')
    return raw


def _quote_yaml_scalar(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _resolve_config_path(config_path: Path | str) -> Path:
    path = Path(config_path)
    if path.is_dir():
        return path / "config.yml"
    return path


def load_go_librespot_device_name(config_path: Path | str) -> str:
    """Read the configured device name, falling back to the default when absent."""
    resolved = _resolve_config_path(config_path)
    try:
        lines = resolved.read_text().splitlines()
    except OSError:
        return DEFAULT_DEVICE_NAME

    for line in lines:
        match = _DEVICE_NAME_RE.match(line)
        if not match:
            continue
        value = _parse_yaml_scalar(match.group("value"))
        if value:
            return value

    return DEFAULT_DEVICE_NAME


def sync_go_librespot_config(default_config_path: Path | str, target_config_path: Path | str) -> str:
    """Ensure the target config exists and its device name matches the shipped default."""
    default_path = _resolve_config_path(default_config_path)
    target_path = _resolve_config_path(target_config_path)
    device_name = load_go_librespot_device_name(default_path)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    if not target_path.exists():
        shutil.copyfile(default_path, target_path)
        return f"Initialized go-librespot config (device_name: {device_name})"

    original = target_path.read_text()
    lines = original.splitlines()
    updated = False

    for idx, line in enumerate(lines):
        match = _DEVICE_NAME_RE.match(line)
        if not match:
            continue
        current_name = _parse_yaml_scalar(match.group("value"))
        if current_name != device_name:
            lines[idx] = f"{match.group('indent')}device_name: {_quote_yaml_scalar(device_name)}"
            updated = True
        break
    else:
        lines.insert(0, f"device_name: {_quote_yaml_scalar(device_name)}")
        updated = True

    if not updated:
        return f"go-librespot config already current (device_name: {device_name})"

    newline = "\n" if original.endswith("\n") or not original else ""
    target_path.write_text("\n".join(lines) + newline)
    return f"Refreshed go-librespot config (device_name: {device_name})"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if len(args) == 2 and args[0] == "device-name":
        print(load_go_librespot_device_name(args[1]))
        return 0

    if len(args) == 3 and args[0] == "sync":
        print(sync_go_librespot_config(args[1], args[2]))
        return 0

    print(
        "Usage: python -m mammamiradio.go_librespot_config "
        "{device-name <config-path>|sync <default-config> <target-config>}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
