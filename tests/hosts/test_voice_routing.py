"""Guards the shipped radio.toml voice routing stays coherent.

These are credential-independent structural invariants (they hold with or
without TTS API keys in the environment, i.e. in CI):

- Every ad voice carries a *canonical* speaker role, so it is actually
  castable. A voice with an unknown role lands in the role index but is never
  requested by the casting engine — dead config.
- Every canonical role has at least one voice, so it is castable. A role may
  carry several voices (e.g. an ElevenLabs character plus the original
  OpenAI/Azure voice); the caster picks one at random per spot for variety.
- Ad-voice names are unique, since the caster dedupes by name within a spot.
- Legacy role-only ``spokesperson`` pins remain a temporary compatibility
  hint while PR2 migrates every campaign to direct ``spokesperson_voice``
  ownership. They create no reservation. Synthetic tests below guard the new
  direct-map invariant: the named character must fit *all* pooled formats and
  can never bleed into another brand's cast.
- No brand carries a ``spokesperson`` key at the top level. The loader only
  reads it from inside ``[ads.brands.campaign]``; a top-level key is silently
  dropped, so the pin never takes effect. This guard reads the raw TOML
  because the dropped key is already gone from the parsed config.

Regression: an earlier edit introduced invented role names (``patriarch``,
``ghost``, ...) and bare brand-level ``spokesperson`` keys. The invented roles
fail ``test_every_ad_voice_has_a_canonical_role``; the misplaced keys fail
``test_no_brand_has_top_level_spokesperson``. Both were silently dropped at
load time, so the new voices never aired.
"""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path

from mammamiradio.core.config import load_config
from mammamiradio.core.models import StationState
from mammamiradio.hosts.ad_creative import (
    _FORMAT_ROLES,
    SPEAKER_ROLES,
    AdBrand,
    AdFormat,
    AdVoice,
    CampaignSpine,
    _cast_voices,
    _pick_brand,
    _select_ad_creative,
    compile_ad_cast,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RADIO_TOML = _REPO_ROOT / "radio.toml"


def _config():
    return load_config(str(_RADIO_TOML))


def test_every_ad_voice_has_a_canonical_role():
    cfg = _config()
    for v in cfg.ads.voices:
        assert v.role in SPEAKER_ROLES, (
            f"ad voice {v.name!r} has role {v.role!r} which is not a canonical "
            f"speaker role {sorted(SPEAKER_ROLES)} — it would never be cast"
        )


def test_every_canonical_role_has_at_least_one_voice():
    cfg = _config()
    by_role: dict[str, list[str]] = {}
    for v in cfg.ads.voices:
        by_role.setdefault(v.role, []).append(v.name)
    for role in SPEAKER_ROLES:
        names = by_role.get(role, [])
        assert names, (
            f"canonical role {role!r} has no configured voice — any format or "
            f"spokesperson that needs it casts a random pool fallback instead"
        )


def test_ad_voice_names_are_unique():
    """The caster dedupes by voice name within a spot; duplicate names would
    let one physical voice shadow another and break the not-already-used logic."""
    cfg = _config()
    names = [v.name for v in cfg.ads.voices]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate ad-voice names: {sorted(dupes)}"


def test_configured_ad_voices_all_declare_airtime_approval():
    """New config rows fail closed; legacy rows must affirm their approval."""
    with open(_RADIO_TOML, "rb") as f:
        raw = tomllib.load(f)

    missing = [
        voice.get("name", "<unnamed>")
        for voice in raw.get("ads", {}).get("voices", [])
        if "airtime_approved" not in voice
    ]
    assert not missing, f"ad voices need an explicit airtime_approved decision: {missing}"


def test_brand_spokesperson_pins_resolve_to_a_voice():
    cfg = _config()
    roles_with_voice = {v.role for v in cfg.ads.voices}
    known_formats = {f.value for f in AdFormat}
    for brand in cfg.ads.brands:
        if not (brand.campaign and brand.campaign.spokesperson):
            continue
        sp = brand.campaign.spokesperson
        assert sp in SPEAKER_ROLES, f"brand {brand.name!r} spokesperson {sp!r} is not a canonical role"
        assert sp in roles_with_voice, f"brand {brand.name!r} spokesperson {sp!r} has no configured ad voice"
        pool = [f for f in (brand.campaign.format_pool or []) if f in known_formats]
        # If a format pool is declared, the pinned role must appear in at least
        # one pooled format, or the casting engine overrides it to the default.
        if pool:
            assert any(sp in _FORMAT_ROLES.get(AdFormat(f), []) for f in pool), (
                f"brand {brand.name!r} spokesperson {sp!r} is absent from every "
                f"pooled format {pool}; it would be overridden and never used"
            )


def _compiled_cast(brands: list[AdBrand], voices: list[AdVoice]) -> tuple[list[AdBrand], list[AdVoice]]:
    """Apply the pure compiler output exactly as config load does."""
    report = compile_ad_cast(brands, voices)
    compiled_brands = [
        replace(
            brand,
            cast_eligible=brand.name not in report.excluded_brands,
            campaign=(
                replace(brand.campaign, spokesperson_role=report.primary_roles.get(brand.name, ""))
                if brand.campaign is not None
                else None
            ),
        )
        for brand in brands
    ]
    compiled_voices = [
        replace(
            voice,
            reserved_for=report.reserved_voice_owners.get(voice.name, frozenset()),
            direct_identity_quarantined=voice.name in report.quarantined_voice_names,
        )
        for voice in voices
    ]
    return compiled_brands, compiled_voices


def test_direct_character_must_fit_every_campaign_format():
    """Direct identity validation is all-format, not the old any-format rule."""
    brand = AdBrand(
        name="Too Broad",
        tagline="T",
        campaign=CampaignSpine(
            spokesperson_voice="Hammer",
            format_pool=["classic_pitch", "institutional_psa"],
        ),
    )
    voices = [
        AdVoice(name="Hammer", voice="voice-hammer", style="hard sell", role="hammer"),
        AdVoice(name="House Disclaimer", voice="voice-disclaimer", style="fast", role="disclaimer_goblin"),
    ]

    report = compile_ad_cast([brand], voices)

    assert report.excluded_brands == frozenset({"Too Broad"})
    assert report.reserved_voice_owners == {}
    assert all("voice-hammer" not in warning for warning in report.warnings)


def test_direct_cast_reserves_identity_and_only_uses_house_partner():
    owned = AdBrand(
        name="Owned Brand",
        tagline="T",
        campaign=CampaignSpine(spokesperson_voice="Owned Hammer", format_pool=["classic_pitch"]),
    )
    other = AdBrand(
        name="Other Brand",
        tagline="T",
        campaign=CampaignSpine(spokesperson_voice="Owned Hammer", format_pool=["live_remote"]),
    )
    generic = AdBrand(name="Generic Brand", tagline="T")
    voices = [
        AdVoice(name="Owned Hammer", voice="owned", style="hard sell", role="hammer"),
        AdVoice(name="House Hammer", voice="house-hammer", style="warm", role="hammer"),
        AdVoice(name="House Disclaimer", voice="house-disclaimer", style="fast", role="disclaimer_goblin"),
    ]
    brands, compiled_voices = _compiled_cast([owned, other, generic], voices)
    by_name = {brand.name: brand for brand in brands}

    ad_format, _sonic, roles = _select_ad_creative(by_name["Owned Brand"], StationState(), len(compiled_voices))
    assert ad_format == "classic_pitch"
    cast = _cast_voices(by_name["Owned Brand"], compiled_voices, [], roles)
    assert cast["hammer"].name == "Owned Hammer"
    assert cast["disclaimer_goblin"].name == "House Disclaimer"

    _format, _sonic, other_roles = _select_ad_creative(by_name["Other Brand"], StationState(), len(compiled_voices))
    other_cast = _cast_voices(by_name["Other Brand"], compiled_voices, [], other_roles)
    assert other_cast["hammer"].name == "Owned Hammer"
    assert other_cast["hammer"].reserved_for == frozenset({"Owned Brand", "Other Brand"})

    generic_cast = _cast_voices(by_name["Generic Brand"], compiled_voices, [], ["hammer"])
    assert generic_cast["hammer"].name == "House Hammer"
    assert generic_cast["hammer"].reserved_for == frozenset()
    assert _pick_brand([by_name["Owned Brand"], by_name["Generic Brand"]], []).name in {
        "Owned Brand",
        "Generic Brand",
    }


def test_direct_disclaimer_stays_in_its_classic_pitch_slot():
    brand = AdBrand(
        name="Scarpe Volanti",
        tagline="T",
        campaign=CampaignSpine(spokesperson_voice="Il Razzo", format_pool=["classic_pitch"]),
    )
    voices = [
        AdVoice(name="Il Razzo", voice="razzo", style="fast", role="disclaimer_goblin"),
        AdVoice(name="House Hammer", voice="house-hammer", style="hard sell", role="hammer"),
    ]
    [compiled_brand], compiled_voices = _compiled_cast([brand], voices)

    _format, _sonic, roles = _select_ad_creative(compiled_brand, StationState(), len(compiled_voices))

    assert roles == ["hammer", "disclaimer_goblin"]
    cast = _cast_voices(compiled_brand, compiled_voices, [], roles)
    assert list(cast) == ["hammer", "disclaimer_goblin"]
    assert cast["hammer"].name == "House Hammer"
    assert cast["disclaimer_goblin"].name == "Il Razzo"


def test_unapproved_candidate_cannot_bleed_into_legacy_or_generic_casting():
    staged = AdBrand(
        name="Existing Brand",
        tagline="T",
        campaign=CampaignSpine(
            spokesperson_voice="Pending Character",
            spokesperson="hammer",
            format_pool=["live_remote"],
        ),
    )
    new_brand = AdBrand(
        name="New Brand",
        tagline="T",
        campaign=CampaignSpine(spokesperson_voice="Pending Character", format_pool=["live_remote"]),
    )
    generic = AdBrand(name="Generic Brand", tagline="T")
    voices = [
        AdVoice(name="Legacy Hammer", voice="legacy", style="safe", role="hammer"),
        AdVoice(
            name="Pending Character",
            voice="candidate",
            style="pending",
            role="hammer",
            airtime_approved=False,
        ),
    ]

    brands, compiled_voices = _compiled_cast([staged, new_brand, generic], voices)
    by_name = {brand.name: brand for brand in brands}

    assert by_name["Existing Brand"].cast_eligible is True
    assert by_name["Existing Brand"].campaign.spokesperson_role == ""
    assert by_name["New Brand"].cast_eligible is False
    assert by_name["New Brand"].name in compile_ad_cast([staged, new_brand, generic], voices).excluded_brands

    _format, _sonic, roles = _select_ad_creative(by_name["Existing Brand"], StationState(), len(compiled_voices))
    legacy_cast = _cast_voices(by_name["Existing Brand"], compiled_voices, [], roles)
    generic_cast = _cast_voices(by_name["Generic Brand"], compiled_voices, [], ["hammer"])

    assert legacy_cast["hammer"].name == "Legacy Hammer"
    assert generic_cast["hammer"].name == "Legacy Hammer"


def test_house_supporting_hammer_never_becomes_a_spokesperson():
    unsafe_owner = AdBrand(
        name="Unsafe Owner",
        tagline="T",
        campaign=CampaignSpine(spokesperson_voice="House Hammer", format_pool=["live_remote"]),
    )
    direct = AdBrand(
        name="Direct Disclaimer",
        tagline="T",
        campaign=CampaignSpine(spokesperson_voice="Owned Disclaimer", format_pool=["classic_pitch"]),
    )
    generic = AdBrand(name="Generic", tagline="T")
    voices = [
        AdVoice(name="House Hammer", voice="house", style="support", role="hammer", secondary_only=True),
        AdVoice(name="Unreserved Hammer", voice="unreserved", style="support", role="hammer", secondary_only=True),
        AdVoice(name="Owned Disclaimer", voice="owned", style="fast", role="disclaimer_goblin"),
        AdVoice(name="Generic Witness", voice="generic", style="main", role="witness"),
    ]

    brands, compiled_voices = _compiled_cast([unsafe_owner, direct, generic], voices)
    by_name = {brand.name: brand for brand in brands}

    assert by_name["Unsafe Owner"].cast_eligible is False
    _format, _sonic, direct_roles = _select_ad_creative(by_name["Direct Disclaimer"], StationState(), len(voices))
    direct_cast = _cast_voices(by_name["Direct Disclaimer"], compiled_voices, [], direct_roles)
    generic_cast = _cast_voices(by_name["Generic"], compiled_voices, [], ["hammer"])

    assert direct_cast["hammer"].name == "Unreserved Hammer"
    assert generic_cast["hammer"].name != "House Hammer"


def test_both_hosts_route_to_elevenlabs():
    """Core deliverable: Marco and Giulia are on their ElevenLabs voices."""
    cfg = _config()
    by_name = {h.name: h for h in cfg.hosts}
    for name in ("Marco", "Giulia"):
        assert name in by_name, f"host {name!r} missing from radio.toml"
        assert by_name[name].engine == "elevenlabs", (
            f"host {name!r} expected engine 'elevenlabs', got {by_name[name].engine!r}"
        )
        assert by_name[name].edge_fallback_voice, f"host {name!r} on a cloud engine must declare an edge_fallback_voice"


def test_no_brand_has_top_level_spokesperson():
    """``spokesperson`` only takes effect inside ``[ads.brands.campaign]``.

    A top-level key is silently dropped by the loader, so the pin never airs.
    This reads the raw TOML because the dropped key is gone from the parsed
    config — it is the only guard that catches the misplaced-key mistake.
    """
    with open(_RADIO_TOML, "rb") as f:
        raw = tomllib.load(f)
    for brand in raw.get("ads", {}).get("brands", []):
        assert "spokesperson" not in brand, (
            f"brand {brand.get('name')!r} declares a top-level 'spokesperson'; "
            f"it must live under [ads.brands.campaign] or it is silently dropped"
        )
