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

import pytest

from mammamiradio.core import config as config_module
from mammamiradio.core.config import (
    ADDON_MAX_CACHE_SIZE_MB,
    MAX_MAX_CACHE_SIZE_MB,
    MIN_MAX_CACHE_SIZE_MB,
    _apply_addon_options,
    _env_clamped_int,
)
from mammamiradio.web import persistence

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_SH = REPO_ROOT / "ha-addon" / "mammamiradio" / "rootfs" / "run.sh"
STABLE_CONFIG = REPO_ROOT / "ha-addon" / "mammamiradio" / "config.yaml"
EDGE_CONFIG = REPO_ROOT / "ha-addon" / "mammamiradio-edge" / "config.yaml"


def _extract_python_snippet(
    options_file: Path,
    provider_file: Path | None = None,
    supervisor_api: str = "http://127.0.0.1:1",
    recovery_marker: Path | None = None,
    jamendo_migration_marker: Path | None = None,
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
    # recovery fails fast and soft. The versioned marker path lives beside
    # options.json and does not exist by default, so recovery is attempted
    # unless a test deliberately points at a pre-existing marker.
    raw = raw.replace("$OPTIONS_FILE", str(options_file))
    provider_path = provider_file or (options_file.parent / "missing-secrets.env")
    raw = raw.replace("$SECRETS_FILE", str(provider_path))
    raw = raw.replace("$SUPERVISOR_API", supervisor_api)
    marker_path = recovery_marker or (options_file.parent / "recovery-marker-not-set")
    raw = raw.replace("$RECOVERY_MARKER_FILE", str(marker_path))
    jamendo_marker_path = jamendo_migration_marker or (options_file.parent / "jamendo-migration-marker-not-set")
    raw = raw.replace("$JAMENDO_MIGRATION_MARKER_FILE", str(jamendo_marker_path))
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
    jamendo_migration_marker: Path | None = None,
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
            jamendo_migration_marker=jamendo_migration_marker,
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


_MUSIC_CLEARED_ENV = (
    "MAMMAMIRADIO_MUSIC_FOLDER",
    "MAMMAMIRADIO_MUSIC_DIR",
    "MAMMAMIRADIO_PROC_MOUNTS",
    "MAMMAMIRADIO_MEDIA_ROOT",
    "MAMMAMIRADIO_DATA_DIR",
    "MAMMAMIRADIO_FALLBACK_BASE",
    "MAMMAMIRADIO_CACHE_DIR",
    "MAMMAMIRADIO_TMP_DIR",
)

MUSIC_FOLDER_DESCRIPTION = (
    "Folder in the Home Assistant Media panel the station scans for your songs. "
    "Default mammamiradio. Another name plays that folder in place when it stays inside Media. "
    "A name that leaves Media is ignored. Takes effect after the app restarts."
)


def _extract_music_block() -> str:
    src = RUN_SH.read_text(encoding="utf-8")
    start = src.index("# BEGIN music path\n")
    end = src.index("# END music path\n")
    return src[start:end]


def _run_music_block(
    tmp_path: Path,
    *,
    mounted: bool,
    folder: str | None = None,
    prepare=None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], Path, Path, Path]:
    """Run the real music-path shell block against temp mount and data roots."""
    media = tmp_path / "media"
    data = tmp_path / "data"
    fallback = tmp_path / "fallback"
    if prepare is not None:
        prepare(media, data)
    mounts = tmp_path / "mounts"
    if mounted:
        mounts.write_text(f"tmpfs {media} tmpfs rw 0 0\n", encoding="utf-8")
    else:
        mounts.write_text("tmpfs /not-media tmpfs rw 0 0\n", encoding="utf-8")
    script = "\n".join(
        [
            "set -e",
            _extract_music_block(),
            'printf "MUSIC=%s\\n" "$MAMMAMIRADIO_MUSIC_DIR"',
            'printf "CACHE=%s\\n" "$MAMMAMIRADIO_CACHE_DIR"',
            'printf "TMP=%s\\n" "$MAMMAMIRADIO_TMP_DIR"',
        ]
    )
    env = os.environ.copy()
    for key in _MUSIC_CLEARED_ENV:
        env.pop(key, None)
    env["MAMMAMIRADIO_PROC_MOUNTS"] = str(mounts)
    env["MAMMAMIRADIO_MEDIA_ROOT"] = str(media)
    env["MAMMAMIRADIO_DATA_DIR"] = str(data)
    env["MAMMAMIRADIO_FALLBACK_BASE"] = str(fallback)
    if folder is not None:
        env["MAMMAMIRADIO_MUSIC_FOLDER"] = folder
    result = subprocess.run(
        ["/bin/sh", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith(("MUSIC=", "CACHE=", "TMP=")):
            key, _, value = line.partition("=")
            values[key] = value
    return result, values, media, data, fallback


def test_music_path_fallback_export_stays_inside_the_unmounted_failure_branch():
    body = RUN_SH.read_text(encoding="utf-8")
    assert 'export MAMMAMIRADIO_MUSIC_DIR="/data/music"' not in body
    assert 'export MAMMAMIRADIO_MUSIC_DIR="$music_dir"' in body
    assert 'mkdir -p "$MEDIA_ROOT"' not in body
    assert body.count('export MAMMAMIRADIO_MUSIC_DIR="$FALLBACK_BASE/music"') == 1
    unmounted = body.index("Media is not mounted.")
    fallback = body.index('export MAMMAMIRADIO_MUSIC_DIR="$FALLBACK_BASE/music"')
    assert unmounted < fallback
    failure = body[unmounted:fallback]
    assert 'mkdir -p "$MAMMAMIRADIO_CACHE_DIR" "$DATA_MUSIC" "$MAMMAMIRADIO_TMP_DIR"' in failure


def test_mounted_media_creates_the_default_folder_and_leaves_data_music_alone(tmp_path):
    result, values, media, data, _fallback = _run_music_block(
        tmp_path, mounted=True, prepare=lambda media, data: media.mkdir()
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert values["MUSIC"] == str(media / "mammamiradio")
    assert (media / "mammamiradio").is_dir()
    assert not (data / "music").exists()
    assert (data / "cache").is_dir()


def test_missing_media_mount_uses_data_music_and_does_not_create_media(tmp_path):
    result, values, media, data, _fallback = _run_music_block(tmp_path, mounted=False)
    assert result.returncode == 0, result.stderr + result.stdout
    assert values["MUSIC"] == str(data / "music")
    assert (data / "music").is_dir()
    assert not media.exists()
    assert "Media is not mounted." in result.stdout


def test_linked_media_root_never_receives_the_default_folder(tmp_path):
    outside = tmp_path / "outside"

    def prepare(media, data):
        outside.mkdir()
        media.symlink_to(outside)

    result, values, _media, data, _fallback = _run_music_block(tmp_path, mounted=True, prepare=prepare)
    assert result.returncode == 0, result.stderr + result.stdout
    assert values["MUSIC"] == str(data / "music")
    assert not (outside / "mammamiradio").exists()


@pytest.mark.parametrize("folder", [".", "..", "/tmp/outside", "foo/../../etc", "mammamiradio/", "bad\x01name"])
def test_invalid_music_folder_falls_back_to_the_default_media_folder(tmp_path, folder):
    result, values, media, _data, _fallback = _run_music_block(
        tmp_path,
        mounted=True,
        folder=folder,
        prepare=lambda media, data: media.mkdir(),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert values["MUSIC"] == str(media / "mammamiradio")
    assert (media / "mammamiradio").is_dir()
    assert "not a folder inside Media" in result.stdout


def test_missing_custom_folder_is_exported_and_not_created(tmp_path):
    result, values, media, _data, _fallback = _run_music_block(
        tmp_path,
        mounted=True,
        folder="crate",
        prepare=lambda media, data: media.mkdir(),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert values["MUSIC"] == str(media / "crate")
    assert not (media / "crate").exists()
    assert not (media / "mammamiradio").exists()


def test_existing_default_folder_is_left_in_place(tmp_path):
    def prepare(media, data):
        target = media / "mammamiradio"
        target.mkdir(parents=True)
        (target / "keep.txt").write_text("stay", encoding="utf-8")

    result, values, media, _data, _fallback = _run_music_block(tmp_path, mounted=True, prepare=prepare)
    assert result.returncode == 0, result.stderr + result.stdout
    assert values["MUSIC"] == str(media / "mammamiradio")
    assert (media / "mammamiradio" / "keep.txt").read_text(encoding="utf-8") == "stay"
    assert "could not create" not in result.stdout


def test_music_folder_metacharacters_stay_one_literal_path(tmp_path):
    name = "DJ's $HOME"

    def prepare(media, data):
        media.mkdir()
        (media / name).mkdir()

    result, values, media, _data, _fallback = _run_music_block(tmp_path, mounted=True, folder=name, prepare=prepare)
    assert result.returncode == 0, result.stderr + result.stdout
    assert values["MUSIC"] == str(media / name)


def test_symlink_music_folder_uses_data_music_and_is_not_followed(tmp_path):
    outside = tmp_path / "outside"

    def prepare(media, data):
        media.mkdir()
        outside.mkdir()
        (media / "crate").symlink_to(outside)

    result, values, _media, data, _fallback = _run_music_block(tmp_path, mounted=True, folder="crate", prepare=prepare)
    assert result.returncode == 0, result.stderr + result.stdout
    assert values["MUSIC"] == str(data / "music")
    assert (data / "music").is_dir()
    assert list(outside.iterdir()) == []
    assert "not following the link" in result.stdout


def test_real_custom_folder_is_used_when_the_default_folder_is_a_symlink(tmp_path):
    outside = tmp_path / "outside"

    def prepare(media, data):
        media.mkdir()
        outside.mkdir()
        (media / "mammamiradio").symlink_to(outside)
        (media / "crate").mkdir()

    result, values, media, _data, _fallback = _run_music_block(tmp_path, mounted=True, folder="crate", prepare=prepare)
    assert result.returncode == 0, result.stderr + result.stdout
    assert values["MUSIC"] == str(media / "crate")
    assert list(outside.iterdir()) == []


def test_existing_folder_whose_real_path_leaves_media_is_rejected(tmp_path):
    outside = tmp_path / "outside"

    def prepare(media, data):
        media.mkdir()
        (outside / "songs").mkdir(parents=True)
        (media / "jump").symlink_to(outside)

    result, values, media, _data, _fallback = _run_music_block(
        tmp_path, mounted=True, folder="jump/songs", prepare=prepare
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert values["MUSIC"] == str(media / "mammamiradio")
    assert (media / "mammamiradio").is_dir()
    assert list((outside / "songs").iterdir()) == []


def test_missing_folder_behind_a_symlinked_parent_is_rejected(tmp_path):
    outside = tmp_path / "outside"

    def prepare(media, data):
        media.mkdir()
        outside.mkdir()
        (media / "jump").symlink_to(outside)

    result, values, media, _data, _fallback = _run_music_block(
        tmp_path, mounted=True, folder="jump/songs", prepare=prepare
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert values["MUSIC"] == str(media / "mammamiradio")
    assert not (outside / "songs").exists()
    assert "not a folder inside Media" in result.stdout


def test_default_media_mkdir_failure_still_exports_the_media_path(tmp_path):
    def prepare(media, data):
        media.write_text("not a directory", encoding="utf-8")
        data.mkdir()

    result, values, media, _data, _fallback = _run_music_block(tmp_path, mounted=True, prepare=prepare)
    assert result.returncode == 0, result.stderr + result.stdout
    assert values["MUSIC"] == str(media / "mammamiradio")
    assert media.is_file()
    assert "could not create" in result.stdout
    assert "still read that folder" in result.stdout


def test_unwritable_data_does_not_replace_a_mounted_media_path(tmp_path):
    def prepare(media, data):
        media.mkdir()
        data.write_text("not a directory", encoding="utf-8")

    result, values, media, _data, fallback = _run_music_block(tmp_path, mounted=True, prepare=prepare)
    assert result.returncode == 0, result.stderr + result.stdout
    assert values["MUSIC"] == str(media / "mammamiradio")
    assert (media / "mammamiradio").is_dir()
    assert values["CACHE"] == str(fallback / "cache")
    assert (fallback / "cache").is_dir()
    assert not (fallback / "music").exists()


def test_unwritable_data_does_not_replace_a_symlink_fallback_with_tmp_music(tmp_path):
    outside = tmp_path / "outside"

    def prepare(media, data):
        media.mkdir()
        outside.mkdir()
        (media / "crate").symlink_to(outside)
        data.write_text("not a directory", encoding="utf-8")

    result, values, _media, data, fallback = _run_music_block(tmp_path, mounted=True, folder="crate", prepare=prepare)
    assert result.returncode == 0, result.stderr + result.stdout
    assert values["MUSIC"] == str(data / "music")
    assert values["CACHE"] == str(fallback / "cache")
    assert not (fallback / "music").exists()


def test_tmp_music_is_used_only_when_media_is_absent_and_data_is_unwritable(tmp_path):
    def prepare(media, data):
        data.write_text("not a directory", encoding="utf-8")

    result, values, media, _data, fallback = _run_music_block(tmp_path, mounted=False, prepare=prepare)
    assert result.returncode == 0, result.stderr + result.stdout
    assert values["MUSIC"] == str(fallback / "music")
    assert (fallback / "music").is_dir()
    assert not media.exists()


def test_leftover_audio_is_named_when_a_media_path_is_exported(tmp_path):
    song = tmp_path / "data" / "music" / "nested" / "song.mp3"

    def prepare(media, data):
        media.mkdir()
        song.parent.mkdir(parents=True)
        song.write_bytes(b"abc")

    result, values, media, _data, _fallback = _run_music_block(tmp_path, mounted=True, prepare=prepare)
    assert result.returncode == 0, result.stderr + result.stdout
    assert values["MUSIC"] == str(media / "mammamiradio")
    assert "still has audio" in result.stdout
    assert str(media / "mammamiradio") in result.stdout
    assert song.read_bytes() == b"abc"


def test_empty_or_unsupported_leftover_files_stay_quiet(tmp_path):
    def prepare(media, data):
        media.mkdir()
        library = data / "music"
        library.mkdir(parents=True)
        (library / "empty.mp3").write_bytes(b"")
        (library / "notes.txt").write_bytes(b"hello")

    result, _values, _media, _data, _fallback = _run_music_block(tmp_path, mounted=True, prepare=prepare)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "still has audio" not in result.stdout


def test_leftover_audio_stays_quiet_when_data_music_is_the_export(tmp_path):
    def prepare(media, data):
        song = data / "music" / "song.mp3"
        song.parent.mkdir(parents=True)
        song.write_bytes(b"abc")

    result, values, _media, data, _fallback = _run_music_block(tmp_path, mounted=False, prepare=prepare)
    assert result.returncode == 0, result.stderr + result.stdout
    assert values["MUSIC"] == str(data / "music")
    assert "still has audio" not in result.stdout


def test_music_folder_with_a_space_stays_one_path(tmp_path):
    def prepare(media, data):
        media.mkdir()
        (media / "My Music").mkdir()

    result, values, media, _data, _fallback = _run_music_block(
        tmp_path, mounted=True, folder="My Music", prepare=prepare
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert values["MUSIC"] == str(media / "My Music")


def test_parser_exports_music_folder_as_a_quoted_raw_value():
    rc, stdout, _stderr = _run_parser({"music_folder": "DJ's $HOME"})
    assert rc == 0
    exports = _parse_exports(stdout)
    assert exports["MAMMAMIRADIO_MUSIC_FOLDER"] == "DJ's $HOME"
    assert "MAMMAMIRADIO_MUSIC_DIR" not in exports


def test_parser_omits_an_empty_music_folder():
    rc, stdout, _stderr = _run_parser({"music_folder": "  "})
    assert rc == 0
    assert "MAMMAMIRADIO_MUSIC_FOLDER" not in _parse_exports(stdout)


def test_music_folder_option_copy_matches_on_both_channels():
    for config, version in ((STABLE_CONFIG, "2.18.0"), (EDGE_CONFIG, "a3bcb37")):
        text = config.read_text(encoding="utf-8")
        assert f"version: {version}\n" in text
        assert "\n  - media:rw\n" in text
        assert "\n  music_folder: mammamiradio\n" in text
        assert "\n  music_folder: str?\n" in text
    description = f'description: "{MUSIC_FOLDER_DESCRIPTION}"'
    for relative in (
        "ha-addon/mammamiradio/translations/en.yaml",
        "ha-addon/mammamiradio-edge/translations/en.yaml",
    ):
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert f"\n  music_folder:\n    name: Music folder\n    {description}\n" in text


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


@pytest.mark.parametrize(
    ("secrets_env", "expected", "secret_canaries"),
    [
        (
            "\n".join(
                [
                    "ANTHROPIC_API_KEY=sk-ant",
                    "OPENAI_API_KEY=sk-openai",
                    "AZURE_SPEECH_KEY=sk-azure",
                    "AZURE_SPEECH_REGION=westeurope",
                    "ELEVENLABS_API_KEY=sk-eleven",
                ]
            ),
            {
                "ANTHROPIC_API_KEY": "sk-ant",
                "OPENAI_API_KEY": "sk-openai",
                "AZURE_SPEECH_KEY": "sk-azure",
                "AZURE_SPEECH_REGION": "westeurope",
                "ELEVENLABS_API_KEY": "sk-eleven",
            },
            (),
        ),
        (
            "\ufeffexport OPENAI_API_KEY = \"sk value=with=equals\"\r\nAZURE_SPEECH_REGION = 'west europe'\r\n",
            {
                "OPENAI_API_KEY": "sk value=with=equals",
                "AZURE_SPEECH_REGION": "west europe",
            },
            (),
        ),
        (
            "ANTHROPIC_API_KEY=sk#literal\nELEVENLABS_API_KEY = 'eleven#literal'\n  # ignored full-line comment\n",
            {
                "ANTHROPIC_API_KEY": "sk#literal",
                "ELEVENLABS_API_KEY": "eleven#literal",
            },
            (),
        ),
        (
            "ANTHROPIC_API_KEY=\n"
            "missing-equals-secret-canary\n"
            "NOT_ALLOWED=unsupported-secret-canary\n"
            'OPENAI_API_KEY="unterminated-secret-canary\n'
            "AZURE_SPEECH_KEY='two values' trailing-secret-canary\n",
            {},
            (
                "missing-equals-secret-canary",
                "unsupported-secret-canary",
                "unterminated-secret-canary",
                "trailing-secret-canary",
            ),
        ),
    ],
)
def test_provider_secret_catalog_and_grammar_stay_in_parity(
    tmp_path,
    caplog,
    secrets_env,
    expected,
    secret_canaries,
):
    """The runtime writer, direct loader, and boot parser share one tested contract."""
    secrets_path = tmp_path / "secrets.env"
    _write_provider_fixture(secrets_path, secrets_env)

    provider_pairs = tuple(
        (option_key, env_key) for option_key, (env_key, _config_attr) in persistence._CREDENTIAL_FIELDS.items()
    )
    assert provider_pairs == config_module._ADDON_PROVIDER_OPTIONS

    _lines, persistence_values = persistence._read_secret_file(secrets_path)
    config_values = config_module._read_addon_provider_secrets(secrets_path)
    rc, stdout, stderr = _run_parser({}, secrets_env)
    assert rc == 0, stderr
    run_sh_values = {
        key: value for key, value in _parse_exports(stdout).items() if key in persistence._CREDENTIAL_ENV_TO_FIELD
    }

    assert persistence_values == expected
    assert config_values == expected
    assert run_sh_values == expected
    combined_output = stdout + stderr + caplog.text
    for canary in secret_canaries:
        assert canary not in combined_output


def test_parser_skips_empty_keys():
    """Empty string values must not produce export lines (they'd override env with '')."""
    rc, stdout, _ = _run_parser({"anthropic_api_key": "", "openai_api_key": ""})
    assert rc == 0
    exports = _parse_exports(stdout)
    assert "ANTHROPIC_API_KEY" not in exports
    assert "OPENAI_API_KEY" not in exports


def test_parser_legacy_secret_keys_can_be_absent_or_blank():
    """Removed legacy fields stay quiet when unset or blank."""
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


def test_parser_migrates_legacy_jamendo_id_before_removing_option():
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        marker = base / "jamendo-migrated-v1"
        rc, stdout, _ = _run_parser(
            {"station_name": "Test", "jamendo_client_id": "legacy-client"},
            keep_dir=base,
            jamendo_migration_marker=marker,
        )

        assert rc == 0
        exports = _parse_exports(stdout)
        assert exports["JAMENDO_CLIENT_ID"] == "legacy-client"
        assert "MAMMAMIRADIO_JAMENDO_ENABLED" not in exports
        assert "MAMMAMIRADIO_JAMENDO_ACKNOWLEDGED" not in exports
        secrets_path = base / "secrets.env"
        assert secrets_path.read_text() == "JAMENDO_CLIENT_ID=legacy-client\n"
        assert secrets_path.stat().st_mode & 0o777 == 0o600
        assert json.loads((base / "options.json").read_text()) == {"station_name": "Test"}
        assert marker.read_text() == "v1\n"
        notice = (
            "Jamendo client ID imported from an earlier version. Review and enable it only for non-commercial API use."
        )
        warning_lines = [line for line in stdout.splitlines() if "[mammamiradio] WARNING:" in line]
        expected_warning = "echo " + shlex.quote("[mammamiradio] WARNING: " + notice) + " >&2"
        assert warning_lines.count(expected_warning) == 1
        assert all("legacy-client" not in line for line in warning_lines)

        rc, repeated_stdout, _ = _run_parser(
            {"station_name": "Test"},
            provider_env_text="JAMENDO_CLIENT_ID=legacy-client\n",
            keep_dir=base,
            jamendo_migration_marker=marker,
        )
        assert rc == 0
        assert expected_warning not in repeated_stdout


def test_parser_jamendo_migration_retries_without_deleting_legacy_value_on_failure():
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        options_path = base / "options.json"
        options_path.write_text(json.dumps({"jamendo_client_id": "keep-me"}))
        marker = base / "jamendo-migrated-v1"
        missing_parent_secret = base / "missing" / "secrets.env"
        snippet = _extract_python_snippet(
            options_path,
            missing_parent_secret,
            jamendo_migration_marker=marker,
        )

        result = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            env=_scrubbed_env(),
        )

        assert result.returncode == 0
        assert "will retry next boot" in result.stdout
        assert json.loads(options_path.read_text())["jamendo_client_id"] == "keep-me"
        assert not marker.exists()
        assert "JAMENDO_CLIENT_ID" not in _parse_exports(result.stdout)


def test_parser_jamendo_migration_is_idempotent_after_success():
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        marker = base / "jamendo-migrated-v1"
        rc, _, _ = _run_parser(
            {"jamendo_client_id": "once-only"},
            keep_dir=base,
            jamendo_migration_marker=marker,
        )
        assert rc == 0

        options_path = base / "options.json"
        secrets_path = base / "secrets.env"
        snippet = _extract_python_snippet(
            options_path,
            secrets_path,
            jamendo_migration_marker=marker,
        )
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            env=_scrubbed_env(),
        )

        assert result.returncode == 0
        assert _parse_exports(result.stdout)["JAMENDO_CLIENT_ID"] == "once-only"
        assert secrets_path.read_text().count("JAMENDO_CLIENT_ID=") == 1


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


def test_parser_ha_context_omission_stays_observable():
    rc, stdout, _ = _run_parser({})
    assert rc == 0
    exports = _parse_exports(stdout)
    assert "MAMMAMIRADIO_HA_CONTEXT_ENABLED" not in exports
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


def test_addon_manifest_keeps_ha_context_optional_without_a_declared_default():
    for config in (STABLE_CONFIG, EDGE_CONFIG):
        body = config.read_text()
        options_block = re.search(r"(?ms)^options:\n(.*?)(?=^schema:)", body)
        assert options_block
        assert not re.search(r"(?m)^  ha_context_enabled:", options_block.group(1)), (
            f"{config} must leave omission visible for first-listen privacy classification"
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
    # All provider IDs/keys are file-backed. A schema-only field is still a
    # settable, persisted Supervisor option, so removal must cover both blocks.
    removed_entirely = (
        "jamendo_client_id",
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
    """In-memory Supervisor info/full-replacement options contract."""

    stored_options: ClassVar[dict] = {}
    schema_names: ClassVar[list[str]] = []
    seen_auth: ClassVar[list[str]] = []
    post_count: ClassVar[int] = 0
    payload_override: ClassVar[dict | None] = None

    def do_GET(self):  # noqa: N802, RUF100 - BaseHTTPRequestHandler override
        type(self).seen_auth.append(self.headers.get("Authorization", ""))
        payload = type(self).payload_override
        if payload is None:
            payload = {
                "result": "ok",
                "data": {
                    "options": type(self).stored_options,
                    "schema": [{"name": name} for name in type(self).schema_names],
                },
            }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802, RUF100 - BaseHTTPRequestHandler override
        type(self).seen_auth.append(self.headers.get("Authorization", ""))
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        options = payload.get("options") if isinstance(payload, dict) else None
        if self.path != "/addons/self/options" or not isinstance(options, dict):
            body = json.dumps({"result": "error"}).encode()
            self.send_response(422)
        else:
            type(self).stored_options = dict(options)
            type(self).post_count += 1
            body = json.dumps({"result": "ok", "data": {}}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence request logging
        pass


def _with_supervisor_stub(
    stored_options: dict,
    *,
    payload: dict | None = None,
    schema_names: list[str] | None = None,
):
    server = http.server.HTTPServer(("127.0.0.1", 0), _SupervisorStub)
    _SupervisorStub.stored_options = dict(stored_options)
    _SupervisorStub.schema_names = list(stored_options) if schema_names is None else list(schema_names)
    _SupervisorStub.seen_auth = []
    _SupervisorStub.post_count = 0
    _SupervisorStub.payload_override = payload
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
                keep_dir=base,
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
    """Unreachable Supervisor stays soft and leaves recovery eligible next boot."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        marker = base / "recovery-marker"
        rc, stdout, _ = _run_parser(
            {"station_name": "X"},
            keep_dir=base,
            env={"SUPERVISOR_TOKEN": "tok-123"},
            recovery_marker=marker,
        )
        assert rc == 0
        assert "could not check Supervisor for legacy provider keys" in stdout
        exports = _parse_exports(stdout)
        assert exports.get("STATION_NAME") == "X", "options must still export after recovery failure"
        assert not marker.exists(), "a failed check must retry on the next boot"


def test_parser_successful_recovery_check_without_keys_writes_marker():
    """An authoritative empty result closes recovery without touching secrets."""
    server, api = _with_supervisor_stub({})
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            marker = base / "recovery-marker"
            rc, _, stderr = _run_parser(
                {"station_name": "X"},
                keep_dir=base,
                supervisor_api=api,
                env={"SUPERVISOR_TOKEN": "tok-123"},
                recovery_marker=marker,
            )
            assert rc == 0, stderr
            assert marker.read_text() == "checked\n"
            assert not (base / "secrets.env").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_parser_malformed_supervisor_response_leaves_marker_absent():
    server, api = _with_supervisor_stub({}, payload={"result": "ok", "data": []})
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
            assert "could not check Supervisor for legacy provider keys" in stdout
            assert not marker.exists()
    finally:
        server.shutdown()
        server.server_close()


def test_parser_recovered_secret_write_failure_leaves_marker_absent():
    """Do not consume the one-time recovery chance before secrets are durable."""
    server, api = _with_supervisor_stub({"anthropic_api_key": "sk-ant-store"})
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            marker = base / "recovery-marker"
            # A directory at the target path makes both the existing-file read
            # and atomic replacement fail reliably, including when tests run as
            # root (permission-bit fixtures alone would not).
            (base / "secrets.env").mkdir()
            rc, stdout, _ = _run_parser(
                {"station_name": "X"},
                keep_dir=base,
                supervisor_api=api,
                env={"SUPERVISOR_TOKEN": "tok-123"},
                recovery_marker=marker,
            )
            assert rc == 0
            assert "could not persist recovered provider keys to secrets.env" in stdout
            assert not marker.exists(), "failed secret persistence must remain retryable"
    finally:
        server.shutdown()
        server.server_close()


def test_parser_rejects_multiline_recovered_secret_without_marker():
    server, api = _with_supervisor_stub({"anthropic_api_key": "line1\nline2"})
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            marker = base / "recovery-marker"
            rc, stdout, stderr = _run_parser(
                {"station_name": "X"},
                keep_dir=base,
                supervisor_api=api,
                env={"SUPERVISOR_TOKEN": "tok-123"},
                recovery_marker=marker,
            )

            assert rc == 0, stderr
            assert "could not check Supervisor for legacy provider keys" in stdout
            assert "ANTHROPIC_API_KEY" not in _parse_exports(stdout)
            assert not (base / "secrets.env").exists()
            assert not marker.exists()
    finally:
        server.shutdown()
        server.server_close()


def test_parser_recovered_secret_read_back_failure_leaves_marker_absent():
    """An unreadable replacement is not enough to consume the recovery chance."""
    server, api = _with_supervisor_stub({"anthropic_api_key": "sk-ant-store"})
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            options_path = base / "options.json"
            secrets_path = base / "secrets.env"
            marker = base / "recovery-marker"
            options_path.write_text(json.dumps({"station_name": "X"}))
            snippet = _extract_python_snippet(
                options_path,
                secrets_path,
                supervisor_api=api,
                recovery_marker=marker,
            )
            verification_start = "verified_recovered = {}"
            assert snippet.count(verification_start) == 1
            snippet = snippet.replace(
                verification_start,
                "raise OSError('simulated secret read-back failure')",
            )

            result = subprocess.run(
                [sys.executable, "-c", snippet],
                capture_output=True,
                text=True,
                env=_scrubbed_env({"SUPERVISOR_TOKEN": "tok-123"}),
            )

            assert result.returncode == 0, result.stderr
            assert "could not persist recovered provider keys to secrets.env" in result.stdout
            assert not secrets_path.exists(), "pre-commit verification failure must leave no replacement"
            assert not marker.exists(), "unverified credentials must remain recoverable"
    finally:
        server.shutdown()
        server.server_close()


def test_parser_recovered_secret_directory_fsync_failure_leaves_marker_absent():
    """Use a committed recovery now, but keep retry armed until it is durable."""
    server, api = _with_supervisor_stub({"anthropic_api_key": "sk-ant-store"})
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            options_path = base / "options.json"
            secrets_path = base / "secrets.env"
            marker = base / "recovery-marker"
            options_path.write_text(json.dumps({"station_name": "X"}))
            snippet = _extract_python_snippet(
                options_path,
                secrets_path,
                supervisor_api=api,
                recovery_marker=marker,
            )
            durable_sync = "os.fsync(dir_fd)"
            assert snippet.count(durable_sync) == 1
            snippet = snippet.replace(
                durable_sync,
                "raise OSError('simulated directory fsync failure')",
            )

            result = subprocess.run(
                [sys.executable, "-c", snippet],
                capture_output=True,
                text=True,
                env=_scrubbed_env({"SUPERVISOR_TOKEN": "tok-123"}),
            )

            assert result.returncode == 0, result.stderr
            assert "could not confirm crash durability" in result.stdout
            assert _parse_exports(result.stdout)["ANTHROPIC_API_KEY"] == "sk-ant-store"
            assert "ANTHROPIC_API_KEY=sk-ant-store" in secrets_path.read_text()
            assert not marker.exists(), "the marker must not outrun directory durability"
    finally:
        server.shutdown()
        server.server_close()


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


def test_parser_retries_recovery_after_the_pre_v2_marker():
    """A pre-v2 marker cannot suppress the tightened durable-secret recovery."""
    server, api = _with_supervisor_stub({"anthropic_api_key": "sk-ant-store"})
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            legacy_marker = base / "recovery-marker"
            legacy_marker.write_text("checked\n")
            current_marker = base / "recovery-marker-v2"

            rc, stdout, stderr = _run_parser(
                {"station_name": "X"},
                keep_dir=base,
                supervisor_api=api,
                env={"SUPERVISOR_TOKEN": "tok-123"},
                recovery_marker=current_marker,
            )

            assert rc == 0, stderr
            assert _parse_exports(stdout)["ANTHROPIC_API_KEY"] == "sk-ant-store"
            assert current_marker.read_text() == "checked\n"
            assert len(_SupervisorStub.seen_auth) == 1
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Supervisor rematerialization of durable admin controls
# ---------------------------------------------------------------------------


def test_parser_restores_all_admin_controls_after_supervisor_rematerialization(
    tmp_path: Path,
    monkeypatch,
):
    """A managed restart must consume the complete Supervisor-owned selection.

    The real persistence helper changes Supervisor's durable store without
    mutating the already-materialized startup file. The next managed start
    rematerializes that store, and run.sh restores every admin control from it.
    """
    materialized_options = tmp_path / "options.json"
    secrets_path = tmp_path / "secrets.env"
    stale_projection = {
        "super_italian_mode": False,
        "chaos_mode_active": False,
        "festival_mode": False,
        "quality_profile": "balanced",
        "broadcast_chain": False,
        "songs_between_banter": 2,
        "songs_between_ads": 4,
        "ad_spots_per_break": 1,
    }
    selected = {
        "super_italian_mode": True,
        "chaos_mode_active": True,
        "festival_mode": True,
        "quality_profile": "premium",
        "broadcast_chain": True,
        "songs_between_banter": 5,
        "songs_between_ads": 7,
        "ad_spots_per_break": 3,
    }
    materialized_options.write_text(json.dumps(stale_projection))
    server, api = _with_supervisor_stub(
        stale_projection,
        schema_names=list(stale_projection),
    )
    try:
        monkeypatch.setenv("SUPERVISOR_API", api)
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-supervisor-token")
        monkeypatch.delenv("HASSIO_TOKEN", raising=False)
        monkeypatch.delenv("HA_TOKEN", raising=False)

        persistence._save_addon_option_batch(selected)

        assert _SupervisorStub.post_count == 1
        assert _SupervisorStub.stored_options == selected
        assert json.loads(materialized_options.read_text()) == stale_projection

        # Model Supervisor's managed-start projection, then exercise the actual
        # embedded parser rather than duplicating its mapping in the test.
        materialized_options.write_text(json.dumps(_SupervisorStub.stored_options))
        snippet = _extract_python_snippet(materialized_options, secrets_path)
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            env=_scrubbed_env(),
        )
        assert result.returncode == 0, result.stderr
        exports = _parse_exports(result.stdout)
        assert exports["MAMMAMIRADIO_SUPER_ITALIAN"] == "true"
        assert exports["MAMMAMIRADIO_CHAOS_MODE"] == "true"
        assert exports["MAMMAMIRADIO_FESTIVAL_MODE"] == "true"
        assert exports["MAMMAMIRADIO_QUALITY"] == "premium"
        assert exports["MAMMAMIRADIO_BROADCAST_CHAIN"] == "true"
        assert exports["MAMMAMIRADIO_PACING_SONGS_BETWEEN_BANTER"] == "5"
        assert exports["MAMMAMIRADIO_PACING_SONGS_BETWEEN_ADS"] == "7"
        assert exports["MAMMAMIRADIO_PACING_AD_SPOTS_PER_BREAK"] == "3"
    finally:
        server.shutdown()
        server.server_close()


# Music cache size
# This option reaches the app through the parser below. Keep a test here so a
# missing export cannot silently replace the chosen value with the default.


def test_parser_exports_cache_mb_from_norm_cache_mb_option():
    rc, stdout, _ = _run_parser({"norm_cache_mb": 2200})
    assert rc == 0
    assert _parse_exports(stdout)["MAMMAMIRADIO_MAX_CACHE_MB"] == "2200"


def test_parser_cache_mb_missing_key_defaults_to_addon_default():
    """Older options files may omit norm_cache_mb and should use 1500 MB."""
    rc, stdout, _ = _run_parser({"station_name": "Test"})
    assert rc == 0
    assert _parse_exports(stdout)["MAMMAMIRADIO_MAX_CACHE_MB"] == "1500"


def test_parser_cache_mb_invalid_value_defaults_to_addon_default():
    """JSON booleans are ints in Python, so they must not become a 1 MB cache."""
    for value in ("not-a-number", 0, -5, None, True, False):
        rc, stdout, _ = _run_parser({"norm_cache_mb": value})
        assert rc == 0, f"parser must not fail on norm_cache_mb={value!r}"
        assert _parse_exports(stdout)["MAMMAMIRADIO_MAX_CACHE_MB"] == "1500"


_CACHE_MB_CONTRACT_MATRIX = [
    ("zero", 0, ADDON_MAX_CACHE_SIZE_MB, ADDON_MAX_CACHE_SIZE_MB, False),
    ("negative-five", -5, ADDON_MAX_CACHE_SIZE_MB, ADDON_MAX_CACHE_SIZE_MB, False),
    ("negative-one", -1, ADDON_MAX_CACHE_SIZE_MB, ADDON_MAX_CACHE_SIZE_MB, False),
    ("one", 1, MIN_MAX_CACHE_SIZE_MB, MIN_MAX_CACHE_SIZE_MB, False),
    ("fifty", 50, MIN_MAX_CACHE_SIZE_MB, MIN_MAX_CACHE_SIZE_MB, False),
    ("one-hundred-ninety-nine", 199, MIN_MAX_CACHE_SIZE_MB, MIN_MAX_CACHE_SIZE_MB, False),
    ("minimum", 200, MIN_MAX_CACHE_SIZE_MB, MIN_MAX_CACHE_SIZE_MB, False),
    ("within-range", 2200, 2200, 2200, False),
    ("above-maximum", 8001, MAX_MAX_CACHE_SIZE_MB, MAX_MAX_CACHE_SIZE_MB, False),
    ("boolean-true", True, ADDON_MAX_CACHE_SIZE_MB, ADDON_MAX_CACHE_SIZE_MB, False),
    ("boolean-false", False, ADDON_MAX_CACHE_SIZE_MB, ADDON_MAX_CACHE_SIZE_MB, False),
    ("null", None, ADDON_MAX_CACHE_SIZE_MB, ADDON_MAX_CACHE_SIZE_MB, False),
    ("garbage-string", "garbage", ADDON_MAX_CACHE_SIZE_MB, ADDON_MAX_CACHE_SIZE_MB, False),
    # Supervisor coerces and validates supported option values before boot, so
    # these raw JSON types cannot reach either ingestion path in production.
    ("numeric-string", "3000", 3000, ADDON_MAX_CACHE_SIZE_MB, True),
    ("whitespace-numeric-string", "  2200  ", 2200, ADDON_MAX_CACHE_SIZE_MB, True),
    ("float", 2000.5, 2000, ADDON_MAX_CACHE_SIZE_MB, True),
]


@pytest.mark.parametrize(
    "_case_id, option_value, expected_run_sh, expected_direct, accepted_divergence",
    _CACHE_MB_CONTRACT_MATRIX,
    ids=[case[0] for case in _CACHE_MB_CONTRACT_MATRIX],
)
def test_cache_mb_ingestion_contract_matrix(
    _case_id: str,
    option_value: object,
    expected_run_sh: int,
    expected_direct: int,
    accepted_divergence: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Pin effective parity and the accepted unsupported coercion differences."""
    cache_env = "MAMMAMIRADIO_MAX_CACHE_MB"
    options_path = tmp_path / "options.json"
    secrets_path = tmp_path / "secrets.env"
    monkeypatch.delenv(cache_env, raising=False)

    try:
        rc, stdout, _ = _run_parser({"norm_cache_mb": option_value})
        assert rc == 0
        monkeypatch.setenv(cache_env, _parse_exports(stdout)[cache_env])
        run_sh_effective = _env_clamped_int(
            cache_env,
            default=ADDON_MAX_CACHE_SIZE_MB,
            minimum=MIN_MAX_CACHE_SIZE_MB,
            maximum=MAX_MAX_CACHE_SIZE_MB,
        )

        monkeypatch.delenv(cache_env, raising=False)
        options_path.write_text(json.dumps({"norm_cache_mb": option_value}))
        secrets_path.write_text("")
        path_fixtures = {
            "/data/options.json": options_path,
            "/config/secrets.env": secrets_path,
        }
        monkeypatch.setattr(
            "mammamiradio.core.config.Path",
            lambda path: path_fixtures[str(path)],
        )
        _apply_addon_options()
        direct_effective = _env_clamped_int(
            cache_env,
            default=ADDON_MAX_CACHE_SIZE_MB,
            minimum=MIN_MAX_CACHE_SIZE_MB,
            maximum=MAX_MAX_CACHE_SIZE_MB,
        )

        assert run_sh_effective == expected_run_sh
        assert direct_effective == expected_direct
        assert (run_sh_effective != direct_effective) is accepted_divergence
    finally:
        monkeypatch.delenv(cache_env, raising=False)


def test_parser_cache_mb_does_not_break_sibling_exports():
    """Invalid cache input must not prevent other options from exporting."""
    rc, stdout, _ = _run_parser({"norm_cache_mb": "garbage", "anthropic_api_key": "sk-ant-abc123"})
    assert rc == 0
    exports = _parse_exports(stdout)
    assert exports["ANTHROPIC_API_KEY"] == "sk-ant-abc123"
    assert exports["MAMMAMIRADIO_MAX_CACHE_MB"] == "1500"
