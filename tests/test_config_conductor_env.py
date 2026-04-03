from __future__ import annotations

from mammamiradio.config import load_config


def test_load_config_accepts_conductor_runtime_overrides(monkeypatch) -> None:
    monkeypatch.setenv("MAMMAMIRADIO_FIFO_PATH", "/tmp/mammamiradio-test-workspace.pcm")
    monkeypatch.setenv("MAMMAMIRADIO_GO_LIBRESPOT_BIN", "/opt/bin/go-librespot")
    monkeypatch.setenv("MAMMAMIRADIO_GO_LIBRESPOT_CONFIG_DIR", "/tmp/go-librespot-test")
    monkeypatch.setenv("MAMMAMIRADIO_GO_LIBRESPOT_PORT", "48123")

    config = load_config()

    assert config.audio.fifo_path == "/tmp/mammamiradio-test-workspace.pcm"
    assert config.audio.go_librespot_bin == "/opt/bin/go-librespot"
    assert config.audio.go_librespot_config_dir == "/tmp/go-librespot-test"
    assert config.audio.go_librespot_port == 48123
