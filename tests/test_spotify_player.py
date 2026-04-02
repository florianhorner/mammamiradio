from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mammamiradio.go_librespot_runtime import build_go_librespot_runtime, claim_process
from mammamiradio.spotify_player import SpotifyPlayer


def _make_config(config_dir: Path, tmp_path: Path) -> MagicMock:
    config = MagicMock()
    config.audio.go_librespot_config_dir = str(config_dir)
    config.audio.fifo_path = str(tmp_path / "mammamiradio.pcm")
    config.audio.go_librespot_port = 3678
    config.audio.go_librespot_bin = "go-librespot"
    config.tmp_dir = tmp_path
    return config


@pytest.mark.asyncio
async def test_try_transfer_playback_uses_configured_device_name(tmp_path):
    config_dir = tmp_path / "go-librespot"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text("device_name: italiradio\n")

    spotify_client = MagicMock()
    spotify_client.devices.return_value = {"devices": [{"id": "dev-1", "name": "italiradio"}]}

    with (
        patch("mammamiradio.spotify_auth.get_spotify_client", return_value=spotify_client),
        patch("mammamiradio.spotify_player.asyncio.sleep", new_callable=AsyncMock),
    ):
        player = SpotifyPlayer(_make_config(config_dir, tmp_path))
        await player._try_transfer_playback()

    spotify_client.transfer_playback.assert_called_once_with("dev-1", force_play=False)


def test_spotify_player_reads_device_name_from_config(tmp_path):
    config_dir = tmp_path / "go-librespot"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text("device_name: 'radio deluxe'\n")

    player = SpotifyPlayer(_make_config(config_dir, tmp_path))

    assert player.device_name == "radio deluxe"


def test_spotify_player_detects_owned_external_process(tmp_path):
    config_dir = tmp_path / "go-librespot"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text("device_name: italiradio\n")
    config = _make_config(config_dir, tmp_path)
    launcher = tmp_path / "go-librespot-bin"
    launcher.write_text("#!/bin/sh\ntrap '' HUP\ntrap 'exit 0' TERM INT\nwhile true; do sleep 1; done\n")
    launcher.chmod(0o755)
    config.audio.go_librespot_bin = str(launcher)
    runtime = build_go_librespot_runtime(
        go_librespot_bin=config.audio.go_librespot_bin,
        config_dir=config_dir,
        fifo_path=config.audio.fifo_path,
        port=config.audio.go_librespot_port,
        tmp_dir=tmp_path,
    )

    proc = subprocess.Popen([str(launcher), "--config_dir", str(runtime.config_dir)])
    try:
        claim_process(
            runtime.state_file,
            pid=proc.pid,
            fingerprint=runtime.fingerprint,
            go_librespot_bin=config.audio.go_librespot_bin,
            config_dir=runtime.config_dir,
        )

        player = SpotifyPlayer(config)

        assert player._is_golibrespot_running() is True
        assert player._external_pid == proc.pid
    finally:
        os.kill(proc.pid, signal.SIGTERM)
        deadline = time.time() + 3
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        proc.wait(timeout=1)
