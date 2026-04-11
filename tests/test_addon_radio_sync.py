from pathlib import Path


def test_addon_radio_toml_matches_root_copy():
    root = Path("radio.toml").read_text(encoding="utf-8")
    addon = Path("ha-addon/mammamiradio/radio.toml").read_text(encoding="utf-8")
    assert addon == root
