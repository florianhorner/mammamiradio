"""Functional tests for the options.json parser embedded in run.sh.

The Python snippet inside run.sh reads /data/options.json and emits shell
`export KEY=value` lines. Bugs in that snippet silently drop all addon config
(API keys, station name, etc.) on every HA Supervisor restart.

Root cause that prompted these tests: the f-string
    print(f'export HA_ENABLED={"true" if enabled else "false"}')
contained double-quotes inside a shell double-quoted string, causing the shell
to mangle the Python code.  Result: NameError on every restart, all config lost.

These tests extract the Python snippet and run it as a subprocess so they
catch both parse errors AND wrong output — without needing a shell or Docker.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_SH = REPO_ROOT / "ha-addon" / "mammamiradio" / "rootfs" / "run.sh"


def _extract_python_snippet(options_file: Path) -> str:
    """Extract the Python body from the python3 -c "..." block in run.sh,
    substituting the real options file path."""
    src = RUN_SH.read_text()
    # Find the python3 -c "..." block
    m = re.search(r'python3 -c "\n(.*?)\n" 2>', src, re.DOTALL)
    assert m, "Could not find python3 -c block in run.sh"
    raw = m.group(1)
    # The shell uses $OPTIONS_FILE inside the script — substitute it
    raw = raw.replace("$OPTIONS_FILE", str(options_file))
    # Shell escapes single-quotes as '\'' inside double-quoted strings; undo that
    raw = raw.replace("\\'", "'")
    return textwrap.dedent(raw)


def _run_parser(options: dict) -> tuple[int, str, str]:
    """Write options to a temp file, run the parser snippet, return (returncode, stdout, stderr)."""
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        json.dump(options, tmp)
        tmp_path = Path(tmp.name)

    try:
        snippet = _extract_python_snippet(tmp_path)
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout, result.stderr
    finally:
        tmp_path.unlink(missing_ok=True)


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


def test_parser_skips_empty_keys():
    """Empty string values must not produce export lines (they'd override env with '')."""
    rc, stdout, _ = _run_parser({"anthropic_api_key": "", "openai_api_key": ""})
    assert rc == 0
    exports = _parse_exports(stdout)
    assert "ANTHROPIC_API_KEY" not in exports
    assert "OPENAI_API_KEY" not in exports


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
        "station_name": "Test Station",
        "claude_model": "claude-sonnet-4-6",
        "admin_token": "tok123",
        "enable_home_assistant": True,
    }
    rc, stdout, _ = _run_parser(options)
    assert rc == 0
    exports = _parse_exports(stdout)
    assert exports["ANTHROPIC_API_KEY"] == "sk-ant"
    assert exports["OPENAI_API_KEY"] == "sk-oai"
    assert exports["STATION_NAME"] == "Test Station"
    assert exports["CLAUDE_MODEL"] == "claude-sonnet-4-6"
    assert exports["ADMIN_TOKEN"] == "tok123"
    assert exports["HA_ENABLED"] == "true"


def test_parser_fails_on_corrupt_json():
    """Corrupt options.json must exit non-zero, not silently continue."""
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        tmp.write("{not valid json")
        tmp_path = Path(tmp.name)

    try:
        snippet = _extract_python_snippet(tmp_path)
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "FATAL" in result.stderr
    finally:
        tmp_path.unlink(missing_ok=True)


def test_parser_no_double_quotes_in_fstring_shell_context():
    """Static guard: the broken f-string pattern must not reappear in run.sh."""
    src = RUN_SH.read_text()
    assert '{"true" if enabled else "false"}' not in src, (
        "Broken f-string pattern detected in run.sh. Double-quotes inside a "
        "shell double-quoted string mangle the Python code (NameError: true). "
        'Use: ha_val = ...; print("export HA_ENABLED=" + ha_val)'
    )
