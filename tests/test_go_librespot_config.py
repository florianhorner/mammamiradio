from __future__ import annotations

from mammamiradio.go_librespot_config import (
    DEFAULT_DEVICE_NAME,
    load_go_librespot_device_name,
    sync_go_librespot_config,
)


def test_load_go_librespot_device_name_reads_config_file(tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text("device_name: italiradio\nserver:\n  port: 3678\n")

    assert load_go_librespot_device_name(config_path) == "italiradio"


def test_load_go_librespot_device_name_falls_back_to_default(tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text("server:\n  port: 3678\n")

    assert load_go_librespot_device_name(config_path) == DEFAULT_DEVICE_NAME


def test_load_go_librespot_device_name_missing_file_returns_default(tmp_path):
    assert load_go_librespot_device_name(tmp_path / "missing.yml") == DEFAULT_DEVICE_NAME


def test_sync_go_librespot_config_initializes_missing_target(tmp_path):
    default_path = tmp_path / "default.yml"
    target_path = tmp_path / "target.yml"
    default_path.write_text("device_name: mammamiradio\nserver:\n  port: 3678\n")

    message = sync_go_librespot_config(default_path, target_path)

    assert "Initialized go-librespot config" in message
    assert target_path.read_text() == default_path.read_text()


def test_sync_go_librespot_config_refreshes_only_device_name(tmp_path):
    default_path = tmp_path / "default.yml"
    target_path = tmp_path / "target.yml"
    default_path.write_text("device_name: mammamiradio\nserver:\n  port: 3678\n")
    target_path.write_text("device_name: italiradio\nserver:\n  port: 3678\ncredentials:\n  type: zeroconf\n")

    message = sync_go_librespot_config(default_path, target_path)

    assert "Refreshed go-librespot config" in message
    text = target_path.read_text()
    assert "device_name: 'mammamiradio'" in text
    assert "credentials:\n  type: zeroconf\n" in text


def test_sync_go_librespot_config_initializes_empty_target(tmp_path):
    default_path = tmp_path / "default.yml"
    target_path = tmp_path / "target.yml"
    default_path.write_text("device_name: mammamiradio\nserver:\n  port: 3678\n")
    target_path.write_text("")

    message = sync_go_librespot_config(default_path, target_path)

    assert "Refreshed go-librespot config" in message
    assert target_path.read_text() == "device_name: 'mammamiradio'\n"


def test_load_go_librespot_device_name_decodes_special_characters(tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text('device_name: "my-device:\\u00df!@#"\n')

    assert load_go_librespot_device_name(config_path) == "my-device:\xdf!@#"
