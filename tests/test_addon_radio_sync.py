from functools import reduce
from pathlib import Path

# HA addon ships with lower-pacing defaults to avoid overwhelming the RPi
# and to ensure new installations start in a conservative mode.
# These overrides are applied on top of the root radio.toml before comparison.
HA_PACING_OVERRIDES: dict[str, str] = {
    "songs_between_banter = 2": "songs_between_banter = 3",
    "ad_spots_per_break = 2": "ad_spots_per_break = 1",
    "lookahead_segments = 3": "lookahead_segments = 2",
}


def test_addon_radio_toml_matches_root_except_addon_pacing_overrides():
    root = Path("radio.toml").read_text(encoding="utf-8")
    addon = Path("ha-addon/mammamiradio/radio.toml").read_text(encoding="utf-8")

    # Apply all known HA pacing overrides to the root file, then compare.
    expected = reduce(
        lambda text, kv: text.replace(kv[0], kv[1]),
        HA_PACING_OVERRIDES.items(),
        root,
    )

    assert addon == expected, (
        "ha-addon/mammamiradio/radio.toml differs from radio.toml beyond known HA pacing overrides.\n"
        "Check that HA_PACING_OVERRIDES is up to date with the actual differences, or run "
        "`cp radio.toml ha-addon/mammamiradio/radio.toml && apply-pacing-overrides` to resync."
    )
