from pathlib import Path


def test_addon_radio_toml_matches_root_except_addon_pacing_overrides():
    root = Path("radio.toml").read_text(encoding="utf-8")
    addon = Path("ha-addon/mammamiradio/radio.toml").read_text(encoding="utf-8")

    expected = (
        root.replace("songs_between_banter = 2", "songs_between_banter = 3")
        .replace("ad_spots_per_break = 2", "ad_spots_per_break = 1")
        .replace("lookahead_segments = 3", "lookahead_segments = 2")
    )

    assert addon == expected
