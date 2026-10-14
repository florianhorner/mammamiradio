"""Guards for the local demo's real environment and persistence boundaries."""

import importlib.util
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "isolated_demo.py"
spec = importlib.util.spec_from_file_location("isolated_demo", SCRIPT)
assert spec is not None and spec.loader is not None
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


@pytest.fixture
def isolated_root(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    for name in ("radio.toml", "model_registry.toml"):
        shutil.copyfile(ROOT / name, root / name)
    return root


@pytest.mark.parametrize("ytdlp", [True, False])
def test_conflicting_dotenv_and_shell_cannot_widen_demo(isolated_root, monkeypatch, ytdlp):
    hostile = {
        "HA_ENABLED": "true",
        "MAMMAMIRADIO_HA_CONTEXT_ENABLED": "true",
        "MAMMAMIRADIO_JAMENDO_ENABLED": "true",
        "HA_TOKEN": "private-home-token",
        "SUPERVISOR_TOKEN": "supervisor-secret",
        "HASSIO_TOKEN": "hassio-secret",
        "MAMMAMIRADIO_CACHE_DIR": "/shared/cache",
        "MAMMAMIRADIO_TMP_DIR": "/shared/tmp",
        "MAMMAMIRADIO_MUSIC_DIR": "/shared/music",
        "MAMMAMIRADIO_BIND_HOST": "0.0.0.0",
        "MAMMAMIRADIO_PORT": "8000",
        "TMPDIR": "/shared/tmp",
        "MAMMAMIRADIO_ALLOW_YTDLP": "false" if ytdlp else "true",
        "MAMMAMIRADIO_LEDGER_ENABLED": "true",
        "MAMMAMIRADIO_SKIP_QUALITY_GATE": "1",
    }
    env_file = isolated_root / ".env"
    env_file.write_text("\n".join(f"{key}={value}" for key, value in hostile.items()))
    original = env_file.read_bytes()
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)
    environment = demo.demo_environment(isolated_root, ytdlp=ytdlp)
    assert environment["MAMMAMIRADIO_LEDGER_ENABLED"] == "false"
    assert "MAMMAMIRADIO_SKIP_QUALITY_GATE" not in environment
    probe = (
        "import importlib.util,json,sys; "
        "spec=importlib.util.spec_from_file_location('demo',sys.argv[1]); "
        "demo=importlib.util.module_from_spec(spec); spec.loader.exec_module(demo); "
        "print(json.dumps(demo.configuration_summary(__import__('pathlib').Path(sys.argv[2]))))"
    )
    # Uses the actual loader in a fresh process, so dotenv import side effects
    # and add-on detection cannot be hidden by pytest's already-loaded config.
    result = subprocess.run(
        [sys.executable, "-c", probe, str(SCRIPT), str(isolated_root / "radio.toml")],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    summary = json.loads(result.stdout)
    assert summary["listener"] == "http://127.0.0.1:8765/listen"
    assert not any(summary[key] for key in ("home_assistant", "home_context", "home_token_present", "addon", "jamendo"))
    available = importlib.util.find_spec("yt_dlp") is not None
    assert summary["ytdlp"] == (ytdlp and available)
    runtime = isolated_root / "tmp" / "isolated-demo"
    for key in ("cache", "audio_work", "music"):
        assert Path(summary[key]).is_relative_to(runtime)
    for key in ("TMPDIR", "TMP", "TEMP"):
        assert environment[key] == str(runtime / "tmp")
    assert "private-home-token" not in result.stdout + result.stderr
    assert env_file.read_bytes() == original
    assert not runtime.exists()


def test_credentials_are_kept_without_execution_or_printing(isolated_root):
    (isolated_root / ".env").write_text('OPENAI_API_KEY="demo-test-key"\nUNUSED="$(touch should-not-exist)"\n')
    environment = demo.demo_environment(isolated_root, ytdlp=True)
    if "OPENAI_API_KEY" not in os.environ:
        assert environment["OPENAI_API_KEY"] == "demo-test-key"
    assert not (isolated_root / "should-not-exist").exists()


def test_missing_ytdlp_has_install_and_bundled_only_escape(monkeypatch):
    monkeypatch.setattr(demo.importlib.util, "find_spec", lambda _: None)
    monkeypatch.setattr(demo.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    with pytest.raises(RuntimeError, match="--no-ytdlp") as error:
        demo.require_dependencies(ytdlp=True)
    assert "pip install --no-deps" in str(error.value)
    demo.require_dependencies(ytdlp=False)


def test_runtime_uses_copies_and_preserves_local_edits(isolated_root):
    runtime = demo.prepare_runtime(isolated_root)
    copied = runtime / "radio.toml"
    assert copied.read_bytes() == (isolated_root / "radio.toml").read_bytes()
    assert not copied.is_symlink()
    original = (isolated_root / "radio.toml").read_bytes()
    copied.write_text("# experiment settings\n")
    demo.prepare_runtime(isolated_root)
    assert copied.read_text() == "# experiment settings\n"
    assert (isolated_root / "radio.toml").read_bytes() == original


def test_escaped_runtime_or_settings_link_is_refused(isolated_root, tmp_path):
    (isolated_root / "tmp").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(demo.DemoStartupError, match="points elsewhere"):
        demo.runtime_directory(isolated_root)
    (isolated_root / "tmp").unlink()
    runtime = demo.prepare_runtime(isolated_root)
    (runtime / ".env").symlink_to(isolated_root / ".env")
    with pytest.raises(demo.DemoStartupError, match="points elsewhere"):
        demo.demo_environment(isolated_root, ytdlp=True)


def test_occupied_port_is_not_reclaimed(isolated_root, monkeypatch, capsys):
    # main() is a process entrypoint; contain its global setup in this test.
    monkeypatch.setattr(demo.os, "environ", os.environ.copy())
    monkeypatch.setattr(demo.sys, "path", sys.path.copy())
    monkeypatch.setattr(demo.sys, "dont_write_bytecode", sys.dont_write_bytecode)
    monkeypatch.setattr(demo.tempfile, "tempdir", demo.tempfile.tempdir)
    monkeypatch.setattr(demo, "ROOT", isolated_root)
    monkeypatch.setattr(demo, "require_dependencies", lambda **_: None)
    monkeypatch.setattr(demo, "configuration_summary", lambda _: {})
    monkeypatch.setattr(demo, "configure_logging", lambda *_: None)
    with socket.socket() as owner:
        owner.bind(("127.0.0.1", 0))
        owner.listen()
        monkeypatch.setattr(demo, "PORT", owner.getsockname()[1])
        assert demo.main([]) == 1
        assert owner.fileno() >= 0
        with socket.create_connection(owner.getsockname(), timeout=1):
            pass
    assert "No process was stopped" in capsys.readouterr().err
    assert not (isolated_root / "tmp").exists()


@pytest.mark.parametrize("mode", [[], ["--no-ytdlp"]])
def test_check_is_identified_and_never_prepares_runtime(isolated_root, monkeypatch, capsys, mode):
    monkeypatch.setattr(demo, "configure_logging", lambda *_: None)
    monkeypatch.setattr(demo.os, "environ", os.environ.copy())
    monkeypatch.setattr(demo.sys, "path", sys.path.copy())
    monkeypatch.setattr(demo.sys, "dont_write_bytecode", sys.dont_write_bytecode)
    monkeypatch.setattr(demo.tempfile, "tempdir", demo.tempfile.tempdir)
    monkeypatch.setattr(demo, "ROOT", isolated_root)
    monkeypatch.setattr(demo, "require_dependencies", lambda **_: None)
    assert demo.main(["--check", *mode]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "isolated_demo"
    assert not (isolated_root / "tmp").exists()


def test_private_logs_redact_content_bound_size_and_reject_links(isolated_root):
    runtime = demo.prepare_runtime(isolated_root)
    path = runtime / "events.jsonl"
    handler = demo.PrivateDemoLog(path, maxBytes=1024, backupCount=2)
    handler.setFormatter(demo.DemoLogFormatter())
    try:
        for _ in range(80):
            record = logging.LogRecord(
                "mammamiradio.test", logging.WARNING, __file__, 42, "prompt %s", ("secret listener message",), None
            )
            record.exc_text = "credential-bearing traceback"
            handler.handle(record)
    finally:
        handler.close()
    logs = list(runtime.glob("events.jsonl*"))
    assert len(logs) == 3
    for log in logs:
        assert log.stat().st_size <= 1024
        assert log.stat().st_mode & 0o777 == 0o600
        contents = log.read_text()
        assert not any(word in contents for word in ("secret", "listener message", "prompt", "credential"))
        assert all(json.loads(line)["line"] == 42 for line in contents.splitlines())
    path.unlink()
    path.symlink_to(isolated_root / "outside")
    with pytest.raises(demo.DemoStartupError, match="points elsewhere"):
        demo.runtime_directory(isolated_root)


def test_logging_failure_never_dumps_original_record(tmp_path, monkeypatch, capsys):
    class BrokenConsole:
        def write(self, _text):
            raise OSError("console closed")

    record = logging.LogRecord("mammamiradio.test", logging.ERROR, __file__, 1, "secret %s", ("private",), None)
    console = demo.DemoConsole(BrokenConsole())
    console.setFormatter(demo.DemoLogFormatter())
    console.handle(record)
    handler = demo.PrivateDemoLog(tmp_path / "events.jsonl")
    handler.close()
    monkeypatch.setattr(handler, "_open", lambda: (_ for _ in ()).throw(OSError("disk unavailable")))
    handler.handle(record)
    output = capsys.readouterr().err
    assert "secret" not in output and "private" not in output.replace("private runtime", "runtime")
    monkeypatch.setattr(sys, "stderr", BrokenConsole())
    handler.handle(record)


def test_private_log_refuses_fifo_and_hardlink(tmp_path):
    path = tmp_path / "events.jsonl"
    os.mkfifo(path)
    with pytest.raises((OSError, ValueError)):
        demo.PrivateDemoLog(path)
    path.unlink()
    other = tmp_path / "other"
    other.write_text("preserve")
    path.hardlink_to(other)
    with pytest.raises(ValueError, match="private regular file"):
        demo.PrivateDemoLog(path)
    assert other.read_text() == "preserve"


def test_formatter_retains_metrics_and_gap_without_content():
    formatter = demo.DemoLogFormatter()
    record = logging.LogRecord("mammamiradio.hosts.scriptwriter", 20, __file__, 1, "language_guard", (), None)
    record.language_metrics = {
        "caller": "ad",
        "model": "model-a",
        "attempt": 1,
        "outcome": "repair_accepted",
        "prompt": "private words",
        "profile": "private words",
    }
    row = json.loads(formatter.format(record))
    assert row["caller"] == "ad" and row["attempt"] == 1 and row["outcome"] == "repair_accepted"
    assert row["model"] == "model-a" and "profile" not in row and "prompt" not in row
    record.msg = "Quality gate rejected ad break (%s): %s"
    record.args = ("private-name", ValueError("ad has a long silent gap (2.79s > 2.50s)"))
    row = json.loads(formatter.format(record))
    assert row["gap_sec"] == 2.79 and row["limit_sec"] == 2.50
    assert "private" not in json.dumps(row)


@pytest.mark.parametrize("failure", ["", "ValueError", "TypeError", "KeyError"])
def test_startup_diagnostics_never_expose_configuration(isolated_root, failure):
    probe = """
import builtins, importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('demo', sys.argv[1])
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)
demo.ROOT = Path(sys.argv[2])
Path.home = lambda: demo.ROOT
os.environ['MAMMAMIRADIO_MAX_CACHE_MB'] = 'SYNTHETIC_PRIVATE_CONFIG_VALUE'
if sys.argv[3]:
    error = getattr(builtins, sys.argv[3])
    demo.configuration_summary = lambda _: (_ for _ in ()).throw(error('SYNTHETIC_PRIVATE_CONFIG_VALUE'))
raise SystemExit(demo.main(['--check', '--no-ytdlp']))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, str(SCRIPT), str(isolated_root), str(failure)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == int(bool(failure))
    assert "SYNTHETIC_PRIVATE_CONFIG_VALUE" not in result.stdout + result.stderr
    assert result.stderr  # Diagnostic location remains observable.
    assert not (isolated_root / "tmp").exists()


@pytest.mark.parametrize("demo_logging", [False, True])
def test_extractor_diagnostics_follow_demo_logging_only(isolated_root, monkeypatch, capsys, demo_logging):
    from mammamiradio.core.models import Track
    from mammamiradio.playlist import downloader

    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers[:], root.level
    monkeypatch.setattr(downloader, "diagnostic_logger", None)
    extractor = MagicMock()
    monkeypatch.setattr(downloader, "_load_external_media_module", lambda: extractor)
    monkeypatch.setattr(downloader, "_ytdlp_enabled", lambda: True)
    try:
        if demo_logging:
            demo.configure_logging(demo.prepare_runtime(isolated_root))
        logger = downloader.diagnostic_logger
        track = Track(title="Fixture", artist="Artist", duration_ms=180000, source="youtube", youtube_id="abcdefghijk")
        with pytest.raises(FileNotFoundError):
            downloader._download_ytdlp(track, isolated_root)
        downloader.search_ytdlp_metadata_outcome("synthetic query")
        for call in extractor.YoutubeDL.call_args_list:
            assert call.args[0]["logger"] is logger
        assert extractor.YoutubeDL.call_count == 2
        if demo_logging:
            assert logger is not None
            logger.error("SYNTHETIC_PRIVATE_EXTRACTOR_ERROR")
            output = capsys.readouterr().err
            assert "SYNTHETIC_PRIVATE_EXTRACTOR_ERROR" not in output
            assert json.loads(output)["logger"] == downloader.__name__
        else:
            assert logger is None
    finally:
        for handler in root.handlers:
            if handler not in previous_handlers:
                handler.close()
        root.handlers, root.level = previous_handlers, previous_level
