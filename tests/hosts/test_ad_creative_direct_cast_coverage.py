"""Fail-closed coverage for direct campaign casting edge cases.

These cases exercise the deliberate safety exits in ``ad_creative``: malformed
direct identities must be withheld, and a reserved character must never be
silently replaced by another campaign's voice.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from mammamiradio.core.models import HostPersonality, StationState
from mammamiradio.hosts.ad_creative import (
    AdBrand,
    AdVoice,
    CampaignSpine,
    _cast_voices,
    _pick_brand,
    _select_ad_creative,
    compile_ad_cast,
)


def _brand(
    name: str = "Campaign",
    *,
    spokesperson_voice: str = "Primary",
    spokesperson_role: str = "",
    format_pool: list[str] | None = None,
    cast_eligible: bool = True,
) -> AdBrand:
    return AdBrand(
        name=name,
        tagline="T",
        cast_eligible=cast_eligible,
        campaign=CampaignSpine(
            spokesperson_voice=spokesperson_voice,
            spokesperson_role=spokesperson_role,
            format_pool=[] if format_pool is None else format_pool,
        ),
    )


def _voice(
    name: str = "Primary",
    *,
    role: str = "hammer",
    approved: bool = True,
    secondary_only: bool = False,
    reserved_for: frozenset[str] = frozenset(),
) -> AdVoice:
    return AdVoice(
        name=name,
        voice=f"voice-{name}",
        style="test",
        role=role,
        airtime_approved=approved,
        secondary_only=secondary_only,
        reserved_for=reserved_for,
    )


def test_compiler_ignores_blank_names_and_rejects_duplicate_identity_names():
    report = compile_ad_cast(
        [_brand(spokesperson_voice="Duplicate", format_pool=["live_remote"])],
        [_voice(""), _voice("Duplicate"), _voice("Duplicate")],
    )

    assert report.excluded_brands == frozenset({"Campaign"})
    assert "ambiguous" in report.warnings[0]


def test_compiler_rejects_unsupported_direct_role_and_invalid_format_pools():
    bad_role = _brand("Bad Role", spokesperson_voice="Alien", format_pool=["live_remote"])
    empty_pool = _brand("Empty Pool", format_pool=[])
    unknown_format = _brand("Unknown Format", format_pool=["impossible_format"])

    report = compile_ad_cast(
        [bad_role, empty_pool, unknown_format],
        [_voice("Alien", role="alien"), _voice(), _voice("House Disclaimer", role="disclaimer_goblin")],
    )

    assert report.excluded_brands == frozenset({"Bad Role", "Empty Pool", "Unknown Format"})
    assert any("supported ad role" in warning for warning in report.warnings)
    assert any("explicit compatible format pool" in warning for warning in report.warnings)
    assert any("unsupported format" in warning for warning in report.warnings)


def test_compiler_excludes_every_duplicate_brand_name_without_granting_ownership():
    owned = _brand("Repeated", spokesperson_voice="Owned Hammer", format_pool=["live_remote"])
    duplicate_generic = AdBrand(name="Repeated", tagline="T")

    report = compile_ad_cast([owned, duplicate_generic], [_voice("Owned Hammer")])

    assert report.excluded_brands == frozenset({"Repeated"})
    assert report.reserved_voice_owners == {}
    assert report.quarantined_voice_names == frozenset({"Owned Hammer"})
    assert "campaign name is ambiguous" in report.warnings[0]
    # The duplicate name only yields one public warning, and neither the direct
    # nor generic duplicate can gain ownership through the shared name key.
    assert len(report.warnings) == 1


def test_compiler_quarantines_direct_identity_when_campaign_name_is_blank():
    owned = _brand("", spokesperson_voice="Owned Hammer", format_pool=["live_remote"])
    blank_generic = AdBrand(name="", tagline="T")

    report = compile_ad_cast([owned, blank_generic], [_voice("Owned Hammer")])

    assert report.excluded_brands == frozenset({""})
    assert report.reserved_voice_owners == {}
    assert report.quarantined_voice_names == frozenset({"Owned Hammer"})
    assert "campaign name is malformed" in report.warnings[0]


def test_compiler_excludes_malformed_or_ambiguous_legacy_campaign_names():
    blank = _brand("", spokesperson_voice="", format_pool=["live_remote"])
    first_duplicate = _brand("Repeated", spokesperson_voice="", format_pool=["live_remote"])
    second_duplicate = _brand("Repeated", spokesperson_voice="", format_pool=["live_remote"])

    report = compile_ad_cast([blank, first_duplicate, second_duplicate], [])

    assert report.excluded_brands == frozenset({"", "Repeated"})
    assert any("campaign name is malformed" in warning for warning in report.warnings)
    assert any("campaign name is ambiguous" in warning for warning in report.warnings)


def test_compiler_excludes_direct_campaign_without_house_supporting_role():
    report = compile_ad_cast(
        [_brand(format_pool=["classic_pitch"])],
        [_voice()],
    )

    assert report.excluded_brands == frozenset({"Campaign"})
    assert "no unreserved supporting character" in report.warnings[0]


def test_invalid_direct_identity_is_quarantined_from_partner_and_generic_casts():
    invalid = _brand("Invalid Disclaimer", spokesperson_voice="Leaked Disclaimer", format_pool=["live_remote"])
    safe = _brand("Safe Hammer", spokesperson_voice="Safe Hammer", format_pool=["classic_pitch"])
    voices = [
        _voice("Leaked Disclaimer", role="disclaimer_goblin"),
        _voice("Safe Hammer", role="hammer"),
        _voice("House Disclaimer", role="disclaimer_goblin"),
    ]

    report = compile_ad_cast([invalid, safe], voices)
    compiled = [
        replace(voice, direct_identity_quarantined=voice.name in report.quarantined_voice_names) for voice in voices
    ]
    safe_brand = replace(
        safe,
        campaign=replace(safe.campaign, spokesperson_role=report.primary_roles["Safe Hammer"]),
    )

    assert report.excluded_brands == frozenset({"Invalid Disclaimer"})
    assert report.quarantined_voice_names == frozenset({"Leaked Disclaimer"})
    cast = _cast_voices(safe_brand, compiled, [], ["hammer", "disclaimer_goblin"])
    generic_cast = _cast_voices(AdBrand(name="Generic", tagline="T"), compiled, [], ["disclaimer_goblin"])

    assert cast["disclaimer_goblin"].name == "House Disclaimer"
    assert generic_cast["disclaimer_goblin"].name == "House Disclaimer"


def test_pick_brand_rejects_an_empty_safe_pool():
    with pytest.raises(ValueError, match="No safe ad campaigns"):
        _pick_brand([AdBrand(name="Unsafe", tagline="T", cast_eligible=False)], [])


@pytest.mark.parametrize(
    ("brand", "num_voices", "match"),
    [
        (_brand(cast_eligible=False), 2, "excluded"),
        (_brand(spokesperson_role="hammer", format_pool=["not-a-format"]), 2, "no safe configured format"),
        (_brand(spokesperson_role="hammer", format_pool=[]), 2, "no explicit format pool"),
        (_brand(spokesperson_role="hammer", format_pool=["duo_scene"]), 1, "no safe single-voice format"),
        (_brand(spokesperson_role="hammer", format_pool=["institutional_psa"]), 2, "incompatible"),
    ],
)
def test_direct_creative_selection_fails_closed(brand: AdBrand, num_voices: int, match: str):
    with pytest.raises(ValueError, match=match):
        _select_ad_creative(brand, StationState(), num_voices)


def test_direct_cast_rejects_excluded_or_unavailable_character():
    excluded = _brand(spokesperson_role="hammer", cast_eligible=False)
    unavailable = _brand(spokesperson_role="hammer")

    with pytest.raises(ValueError, match="excluded"):
        _cast_voices(excluded, [_voice()], [], ["hammer"])
    with pytest.raises(ValueError, match="no safe character voice"):
        _cast_voices(unavailable, [_voice(approved=False)], [], ["hammer"])


def test_direct_cast_rejects_role_assignment_mismatches_and_missing_partner():
    primary_not_requested = _brand(spokesperson_role="hammer")
    with pytest.raises(ValueError, match="not assigned"):
        _cast_voices(primary_not_requested, [_voice()], [], ["maniac"])

    wrong_primary_role = _brand(spokesperson_role="hammer")
    with pytest.raises(ValueError, match="does not match"):
        _cast_voices(wrong_primary_role, [_voice(role="maniac")], [], ["hammer"])

    no_partner = _brand(spokesperson_role="hammer")
    with pytest.raises(ValueError, match="no safe supporting character"):
        _cast_voices(no_partner, [_voice()], [], ["hammer", "disclaimer_goblin"])


def test_generic_cast_falls_back_to_a_host_when_only_house_support_is_available():
    host = HostPersonality(name="Host", voice="host-voice", style="warm")
    result = _cast_voices(
        AdBrand(name="Generic", tagline="T"),
        [_voice("House Support", secondary_only=True)],
        [host],
        ["unknown_role"],
    )

    assert result["unknown_role"].name == "Host"

    with pytest.raises(ValueError, match="non-supporting host or ad voice"):
        _cast_voices(
            AdBrand(name="Generic", tagline="T"),
            [_voice("House Support", secondary_only=True)],
            [],
            ["unknown_role"],
        )
