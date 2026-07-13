"""Focused config-load guards for direct campaign casting and Eleven tuning."""

from __future__ import annotations

from pathlib import Path

import pytest

from mammamiradio.core import config as config_module
from mammamiradio.core.config import load_config

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _with_additions(tmp_path: Path, additions: str) -> Path:
    path = tmp_path / "radio.toml"
    path.write_text((_REPO_ROOT / "radio.toml").read_text() + additions, encoding="utf-8")
    return path


def test_load_config_compiles_direct_campaign_ownership(tmp_path):
    path = _with_additions(
        tmp_path,
        """

[[ads.brands]]
name = "Direct Test Brand"
tagline = "T"
category = "food"
[ads.brands.campaign]
format_pool = ["classic_pitch"]
spokesperson_voice = "L'Annunciatore"
""",
    )

    config = load_config(str(path))
    brand = next(brand for brand in config.ads.brands if brand.name == "Direct Test Brand")
    voice = next(voice for voice in config.ads.voices if voice.name == "L'Annunciatore")

    assert brand.cast_eligible is True
    assert brand.campaign is not None
    assert brand.campaign.spokesperson_voice == "L'Annunciatore"
    assert brand.campaign.spokesperson_role == "hammer"
    assert voice.reserved_for == frozenset({"Direct Test Brand"})
    assert config.ads.cast_report.reserved_voice_owners == {"L'Annunciatore": frozenset({"Direct Test Brand"})}


def test_load_config_excludes_invalid_direct_campaign_without_provider_id_warning(tmp_path):
    path = _with_additions(
        tmp_path,
        """

[[ads.brands]]
name = "Broken Identity"
tagline = "T"
category = "food"
[ads.brands.campaign]
format_pool = ["classic_pitch"]
spokesperson_voice = "Definitely Not Configured"
""",
    )

    config = load_config(str(path))
    brand = next(brand for brand in config.ads.brands if brand.name == "Broken Identity")

    assert brand.cast_eligible is False
    assert "Broken Identity" in config.ads.cast_report.excluded_brands
    assert config.ads.cast_report.warnings
    assert all("Definitely Not Configured" not in warning for warning in config.ads.cast_report.warnings)


@pytest.mark.parametrize("value", ['""', "42"])
def test_load_config_withholds_malformed_explicit_direct_mapping_even_with_legacy_role(tmp_path, value):
    path = _with_additions(
        tmp_path,
        f"""

[[ads.brands]]
name = "Malformed Identity"
tagline = "T"
category = "food"
[ads.brands.campaign]
format_pool = ["live_remote"]
spokesperson = "hammer"
spokesperson_voice = {value}
""",
    )

    config = load_config(str(path))
    brand = next(brand for brand in config.ads.brands if brand.name == "Malformed Identity")

    assert brand.cast_eligible is False
    assert "Malformed Identity" in config.ads.cast_report.excluded_brands
    assert any("malformed" in warning for warning in config.ads.cast_report.warnings)


@pytest.mark.parametrize("value", ['""', "42"])
def test_load_config_rejects_malformed_ad_brand_names(tmp_path, value):
    path = _with_additions(
        tmp_path,
        f"""

[[ads.brands]]
name = {value}
tagline = "T"
category = "food"
""",
    )

    with pytest.raises(ValueError, match=r"ads\.brands\[.*\]\.name.*non-empty string"):
        load_config(str(path))


@pytest.mark.parametrize(
    ("settings", "match"),
    [
        ('voice_settings = "not-a-table"', "must be a TOML table"),
        ("voice_settings = { stability = 1.1 }", "must be between 0 and 1"),
        ("voice_settings = { unknown = 0.5 }", "must be one of"),
        ("voice_settings = { stability = 0.5 }", "only for engine = 'elevenlabs'"),
    ],
)
def test_load_config_rejects_malformed_or_invalid_ad_voice_settings(tmp_path, settings, match):
    engine = "edge" if "only for engine" in match else "elevenlabs"
    path = _with_additions(
        tmp_path,
        f"""

[[ads.voices]]
name = "Bad Settings"
voice = "test-voice"
style = "test"
role = "hammer"
engine = "{engine}"
{settings}
""",
    )

    with pytest.raises(ValueError, match=match):
        load_config(str(path))


def test_load_config_preserves_explicit_eleven_v2_ad_override_only(tmp_path):
    path = _with_additions(
        tmp_path,
        """

[[ads.voices]]
name = "Tuned Eleven"
voice = "test-voice"
style = "test"
role = "hammer"
engine = "elevenlabs"
voice_settings = { stability = 0.6, use_speaker_boost = false }
""",
    )

    config = load_config(str(path))
    voice = next(voice for voice in config.ads.voices if voice.name == "Tuned Eleven")

    assert voice.voice_settings == {"stability": 0.6, "use_speaker_boost": False}


def test_load_config_stages_new_ad_voice_until_approval_is_explicit(tmp_path):
    path = _with_additions(
        tmp_path,
        """

[[ads.voices]]
name = " Pending Candidate "
voice = "test-voice"
style = "test"
role = "hammer"
engine = "elevenlabs"
""",
    )

    config = load_config(str(path))
    voice = next(voice for voice in config.ads.voices if voice.name == "Pending Candidate")

    assert voice.airtime_approved is False
    assert voice.name == "Pending Candidate"


@pytest.mark.parametrize("field", ["airtime_approved", "secondary_only"])
def test_load_config_requires_boolean_ad_voice_approval_flags(tmp_path, field):
    path = _with_additions(
        tmp_path,
        f"""

[[ads.voices]]
name = "Invalid {field}"
voice = "test-voice"
style = "test"
role = "hammer"
{field} = "true"
""",
    )

    with pytest.raises(ValueError, match="must be true or false"):
        load_config(str(path))


def test_ad_voice_settings_rejects_non_numeric_or_non_boolean_overrides() -> None:
    assert config_module._parse_ad_voice_settings(None, index=4, engine="elevenlabs") == {}

    with pytest.raises(ValueError, match="must be a finite number"):
        config_module._parse_ad_voice_settings({"stability": True}, index=4, engine="elevenlabs")
    with pytest.raises(ValueError, match="must be true or false"):
        config_module._parse_ad_voice_settings({"use_speaker_boost": 1}, index=4, engine="elevenlabs")
