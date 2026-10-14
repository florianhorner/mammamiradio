#!/usr/bin/env python3
"""Run an isolated radio demo with separate runtime files.

Use .venv/bin/python scripts/isolated_demo.py [--check] [--no-ytdlp].
Cloud writing/voice services remain enabled through existing credentials.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import re
import shutil
import socket
import stat
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
PORT = 8765


class DemoStartupError(RuntimeError):
    """A deliberate, content-free launcher diagnostic safe for the console."""


class DemoLogFormatter(logging.Formatter):
    """Retain event locations and approved metrics, never free-form content."""

    def format(self, record: logging.LogRecord) -> str:
        row: dict[str, object] = {
            "time": round(record.created, 3),
            "level": record.levelno,
            "logger": record.name if re.fullmatch(r"(?:mammamiradio|uvicorn)(?:\.[a-z_]+)*", record.name) else "other",
            "line": record.lineno,
        }
        if record.msg == "language_guard":
            metrics = getattr(record, "language_metrics", {})
            for key in ("id", "caller", "profile", "mode", "requested_mode", "provider", "model", "attempt", "outcome"):
                value = metrics.get(key)
                if isinstance(value, int) or (
                    isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,100}", value)
                ):
                    row[key] = value
        if record.msg == "Quality gate rejected ad break (%s): %s" and isinstance(record.args, tuple):
            gap = re.fullmatch(r"ad has a long silent gap \(([0-9.]+)s > ([0-9.]+)s\)", str(record.args[-1]))
            if gap:
                row.update(event="ad_silence_rejected", gap_sec=float(gap[1]), limit_sec=float(gap[2]))
        # No getMessage(), exception text, arbitrary extras, URLs or arguments.
        return json.dumps(row)


class DemoConsole(logging.StreamHandler):
    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging API
        # A broken stderr must not dump the original record or break production.
        pass


class PrivateDemoLog(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Demo log must be a private regular file")
            os.fchmod(fd, 0o600)
            return os.fdopen(fd, "a", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging API
        # logging's default error handler dumps the original message and args.
        try:
            print("Demo log unavailable; check the private runtime folder.", file=sys.stderr)
        except OSError:
            pass


def configure_logging(runtime: Path | None = None) -> None:
    formatter = DemoLogFormatter()
    console = DemoConsole()
    handlers: list[logging.Handler] = [console]
    if runtime is not None:
        from mammamiradio.playlist import downloader

        handlers.append(PrivateDemoLog(runtime / "events.jsonl", maxBytes=1024 * 1024, backupCount=2))
        # Extractor errors otherwise bypass Python logging and write raw stderr.
        downloader.diagnostic_logger = logging.getLogger(downloader.__name__)
    for sink in handlers:
        sink.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True


def runtime_directory(root: Path) -> Path:
    """Reject links that could send demo writes to another runtime."""
    runtime = root.resolve() / "tmp" / "isolated-demo"
    if runtime.resolve() != runtime:
        raise DemoStartupError("The demo folder points elsewhere. Use a normal tmp/isolated-demo folder and try again.")
    pending = list(runtime.iterdir()) if runtime.exists() else []
    while pending:
        path = pending.pop()
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DemoStartupError(
                "A demo runtime path points elsewhere or is not a private regular file. "
                "Remove links and special files from tmp/isolated-demo, then try again."
            )
    return runtime


def demo_environment(root: Path, *, ytdlp: bool) -> dict[str, str]:
    """Load credentials without executing shell text; apply safety settings last."""
    from dotenv import dotenv_values

    runtime = runtime_directory(root)
    safe_env = Path.home() / ".config" / "mammamiradio" / ".env"
    credential_file = safe_env if safe_env.is_file() else root / ".env"
    environment: dict[str, str] = {}
    for path in (credential_file, runtime / ".env"):
        if path.is_file():
            environment.update(
                {key: value for key, value in dotenv_values(path, interpolate=False).items() if value is not None}
            )
    environment.update(os.environ)
    environment.update(
        {
            "PYTHON_DOTENV_DISABLED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "MAMMAMIRADIO_BIND_HOST": HOST,
            "MAMMAMIRADIO_PORT": str(PORT),
            "MAMMAMIRADIO_CACHE_DIR": str(runtime / "cache"),
            "MAMMAMIRADIO_TMP_DIR": str(runtime / "tmp"),
            "MAMMAMIRADIO_MUSIC_DIR": str(runtime / "music"),
            "TMPDIR": str(runtime / "tmp"),
            "TMP": str(runtime / "tmp"),
            "TEMP": str(runtime / "tmp"),
            "HA_ENABLED": "false",
            "MAMMAMIRADIO_HA_CONTEXT_ENABLED": "false",
            "MAMMAMIRADIO_HA_MOOD_LLM": "false",
            "MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH": "false",
            "HA_TOKEN": "",
            "HA_URL": "",
            "SUPERVISOR_TOKEN": "",
            "HASSIO_TOKEN": "",
            "MAMMAMIRADIO_JAMENDO_ENABLED": "false",
            "MAMMAMIRADIO_LEDGER_ENABLED": "false",
            "MAMMAMIRADIO_ALLOW_YTDLP": "true" if ytdlp else "false",
        }
    )
    environment.pop("MAMMAMIRADIO_SKIP_QUALITY_GATE", None)
    return environment


def require_dependencies(*, ytdlp: bool) -> None:
    if ytdlp and importlib.util.find_spec("yt_dlp") is None:
        raise DemoStartupError(
            "Song downloads need the optional yt-dlp package. Run "
            ".venv/bin/python -m pip install --no-deps 'yt-dlp>=2026.7.4' "
            "or start this launcher with --no-ytdlp."
        )
    if not shutil.which("ffmpeg"):
        raise DemoStartupError("Audio needs FFmpeg. Install it, then start the demo again.")


def configuration_summary(config_path: Path) -> dict[str, object]:
    """Validate the real app configuration without booting workers or generating audio."""
    from mammamiradio.core.config import load_config
    from mammamiradio.home.ha_context import _media_player_push_enabled
    from mammamiradio.playlist.downloader import external_media_enabled

    config = load_config(str(config_path))
    return {
        "mode": "isolated_demo",
        "listener": f"http://{config.bind_host}:{config.port}/listen",
        "admin": f"http://{config.bind_host}:{config.port}/admin",
        "home_assistant": config.homeassistant.enabled,
        "home_context": config.homeassistant.context_enabled,
        "home_token_present": bool(config.ha_token),
        "addon": config.is_addon,
        "jamendo": config.playlist.jamendo_enabled,
        "home_media_player_push": _media_player_push_enabled(),
        "ledger": config.ledger_enabled,
        "ytdlp": external_media_enabled(config.allow_ytdlp),
        "cache": str(config.cache_dir),
        "audio_work": str(config.tmp_dir),
        "music": str(config.music_dir),
    }


def prepare_runtime(root: Path) -> Path:
    runtime = runtime_directory(root)
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime.chmod(0o700)
    for name in ("cache", "tmp", "music"):
        (runtime / name).mkdir(exist_ok=True, mode=0o700)
    # Copies keep admin-side edits away from the source configuration.
    # Preserve runtime configuration across subsequent launches.
    for name in ("radio.toml", "model_registry.toml"):
        target = runtime / name
        if not target.exists():
            shutil.copyfile(root / name, target)
    return runtime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check isolation without starting or generating anything")
    parser.add_argument("--no-ytdlp", action="store_true", help="disable external extraction for this demo")
    args = parser.parse_args(argv)
    configure_logging()  # Also protects dotenv/config warnings during --check; creates no files.
    try:
        ytdlp = not args.no_ytdlp
        sys.dont_write_bytecode = True
        os.environ.update(demo_environment(ROOT, ytdlp=ytdlp))
        os.environ.pop("MAMMAMIRADIO_SKIP_QUALITY_GATE", None)
        sys.path.insert(0, str(ROOT))
        tempfile.tempdir = os.environ["TMPDIR"]
        require_dependencies(ytdlp=ytdlp)
        runtime = runtime_directory(ROOT)
        config_path = runtime / "radio.toml"
        if not config_path.exists():
            config_path = ROOT / "radio.toml"
        summary = configuration_summary(config_path)
        if args.check:
            print(json.dumps(summary, indent=2))
            return 0

        # Reserve the socket through startup, rather than checking then racing
        # another process. Never use start.sh's port-reclaim/kill behavior.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind((HOST, PORT))
            except OSError as exc:
                raise DemoStartupError(
                    f"The demo can't use {HOST}:{PORT}. Stop its current owner yourself, then try again. "
                    "No process was stopped."
                ) from exc
            runtime = prepare_runtime(ROOT)
            os.chdir(runtime)
            os.umask(0o077)
            configure_logging(runtime)
            print(
                f"ISOLATED DEMO | Home/Jamendo OFF | yt-dlp {'ON' if summary['ytdlp'] else 'OFF'}\n"
                f"Listen: {summary['listener']}\nProducer desk: {summary['admin']}\n"
                "Private event log: tmp/isolated-demo/events.jsonl (3 files, 1 MiB each)\nStop: Ctrl-C",
                flush=True,
            )
            import uvicorn

            server = uvicorn.Server(
                uvicorn.Config("mammamiradio.main:app", host=HOST, port=PORT, log_config=None, access_log=False)
            )
            server.run(sockets=[listener])
        return 0 if server.started else 1
    except DemoStartupError as exc:
        print(f"Demo not started: {exc}", file=sys.stderr)
        return 1
    except Exception:
        logging.getLogger("mammamiradio.demo").error("startup_failed", exc_info=True)
        print("Demo not started. Check the demo settings and folder permissions, then try again.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
