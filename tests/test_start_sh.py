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


def test_start_sh_caddy_detection():
    """start.sh checks for caddy on PATH."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert "command -v caddy" in content


def test_start_sh_caddy_uses_internal_port():
    """In caddy mode, uvicorn binds to PORT+1."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert "INTERNAL_PORT" in content
    # Arithmetic expansion for PORT+1
    assert "PORT + 1" in content or "PORT+1" in content


def test_start_sh_caddy_has_flush_interval():
    """Caddyfile template must include flush_interval -1 for audio streaming."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert "flush_interval -1" in content


def test_start_sh_caddy_no_health_uri():
    """Caddyfile must NOT include health_uri (healthz/readyz return 503 in normal operation)."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert "health_uri" not in content
    assert "health_path" not in content


def test_start_sh_caddy_trap_cleanup():
    """Caddy mode must define a cleanup trap and null-guard the PIDs."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert "trap cleanup" in content or 'trap "cleanup"' in content or "trap 'cleanup'" in content
    assert "CADDY_PID" in content
    assert "UVICORN_PID" in content


def test_start_sh_no_caddy_warning_message():
    """No-caddy fallback must print an actionable install hint."""
    content = (REPO_ROOT / "start.sh").read_text()
    assert "brew install caddy" in content
