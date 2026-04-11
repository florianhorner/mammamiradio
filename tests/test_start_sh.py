"""Tests for start.sh dev entrypoint.

Note: The go-librespot lifecycle management was removed along with Spotify
integration. start.sh is now a simple uvicorn launcher. These tests verify
basic contract behavior.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_start_sh_exists_and_is_executable():
    start_sh = REPO_ROOT / "start.sh"
    assert start_sh.exists()
    assert start_sh.stat().st_mode & 0o111  # has execute bits


def test_start_sh_sources_dotenv():
    """start.sh should source .env if it exists."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert ".env" in content
    assert "source .env" in content or ". .env" in content


def test_start_sh_uses_runtime_json():
    """start.sh should call mammamiradio.config runtime-json to get host/port."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert "runtime-json" in content
    assert "bind_host" in content
    assert "port" in content


def test_start_sh_launches_uvicorn():
    """start.sh should exec uvicorn with reload."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert "uvicorn" in content
    assert "--reload" in content
    assert "mammamiradio.main:app" in content
