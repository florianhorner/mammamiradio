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


def test_root_radio_toml_has_the_original_pacing_values():
    """Root radio.toml must contain the original pacing values that the inline
    replacements substitute. If the root file changes these keys, the sync test
    would silently pass even though the addon file has wrong values.
    """
    root = Path("radio.toml").read_text(encoding="utf-8")
    assert "songs_between_banter = 2" in root, (
        "Root radio.toml must contain 'songs_between_banter = 2'; "
        "the addon sync test replaces this with 3"
    )
    assert "ad_spots_per_break = 2" in root, (
        "Root radio.toml must contain 'ad_spots_per_break = 2'; "
        "the addon sync test replaces this with 1"
    )
    assert "lookahead_segments = 3" in root, (
        "Root radio.toml must contain 'lookahead_segments = 3'; "
        "the addon sync test replaces this with 2"
    )


def test_addon_radio_toml_has_overridden_pacing_values():
    """HA addon radio.toml must contain the Pi/HA Green tuned pacing values."""
    addon = Path("ha-addon/mammamiradio/radio.toml").read_text(encoding="utf-8")
    assert "songs_between_banter = 3" in addon, (
        "HA addon radio.toml must contain 'songs_between_banter = 3' (Pi pacing)"
    )
    assert "ad_spots_per_break = 1" in addon, (
        "HA addon radio.toml must contain 'ad_spots_per_break = 1' (Pi pacing)"
    )
    assert "lookahead_segments = 2" in addon, (
        "HA addon radio.toml must contain 'lookahead_segments = 2' (Pi pacing)"
    )


def test_addon_radio_toml_replacement_logic_is_idempotent():
    """Applying the inline replacements twice to the root must yield the same result.

    This guards against cascading substitutions where one replacement produces a
    value that a later replacement also matches (e.g., 3→something and 2→3 chained).
    """
    root = Path("radio.toml").read_text(encoding="utf-8")

    def _apply_overrides(text: str) -> str:
        return (
            text.replace("songs_between_banter = 2", "songs_between_banter = 3")
            .replace("ad_spots_per_break = 2", "ad_spots_per_break = 1")
            .replace("lookahead_segments = 3", "lookahead_segments = 2")
        )

    once = _apply_overrides(root)
    twice = _apply_overrides(once)
    assert once == twice, (
        "Replacements are not idempotent — applying them a second time changes the result. "
        "Check for cascading substitution patterns."
    )