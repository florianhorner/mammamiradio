from functools import reduce
from pathlib import Path

from tests.test_addon_build_workflow import HA_PACING_OVERRIDES


def test_addon_radio_toml_matches_root_except_addon_pacing_overrides():
    root = Path("radio.toml").read_text(encoding="utf-8")
    addon = Path("ha-addon/mammamiradio/radio.toml").read_text(encoding="utf-8")

    # Apply all known HA pacing overrides to the root file, then compare.
    # HA_PACING_OVERRIDES is the single source of truth — shared with
    # test_addon_build_workflow.py so all three enforcement points stay in sync.
    expected = reduce(
        lambda text, kv: text.replace(kv[0], kv[1]),
        HA_PACING_OVERRIDES.items(),
        root,
    )

    assert addon == expected
