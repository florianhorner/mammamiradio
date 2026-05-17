from __future__ import annotations

import logging
from pathlib import Path

from mammamiradio.main import _configure_dependency_loggers

REPO_ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__", ".context"}
ALLOW_LEGACY_NAMING = {"CHANGELOG.md", "ha-addon/mammamiradio/CHANGELOG.md"}
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".svg",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


def _repo_text_files():
    for path in REPO_ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix in TEXT_SUFFIXES:
            yield path


def test_legacy_mammamia_spelling_is_not_reintroduced():
    offenders = []
    legacy = "Mamma" + "Mia"
    legacy_radio = "Radio " + legacy
    for path in _repo_text_files():
        rel = str(path.relative_to(REPO_ROOT))
        if rel in ALLOW_LEGACY_NAMING:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if legacy in text or legacy_radio in text:
            offenders.append(rel)

    assert not offenders, f"legacy naming found in: {offenders}"


def test_dependency_http_loggers_are_warning_or_quieter(caplog):
    _configure_dependency_loggers()

    caplog.set_level(logging.INFO)
    logging.getLogger("httpx").info("HTTP Request: POST http://ha.local/api/states/media_player.mammamiradio")
    logging.getLogger("httpcore").info("HTTP Request: POST http://ha.local/api/states/media_player.mammamiradio")

    http_info_records = [
        record for record in caplog.records if record.levelno == logging.INFO and record.name in {"httpx", "httpcore"}
    ]
    assert not http_info_records
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
