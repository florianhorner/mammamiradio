"""Ad creative system: formats, sonic palettes, brand selection, and voice casting.

Single-file home for everything that defines *what ads are* — data models,
format descriptions, sonic palettes, and the pure-logic helpers that pick
and cast a spot.  The LLM call (write_ad) stays in scriptwriter.py; the
orchestration loop stays in producer.py.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mammamiradio.core.models import AdHistoryEntry, HostPersonality, StationState


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class AdFormat(StrEnum):
    """Available ad creative formats that shape how the joke is delivered."""

    CLASSIC_PITCH = "classic_pitch"
    TESTIMONIAL = "testimonial"
    DUO_SCENE = "duo_scene"
    LIVE_REMOTE = "live_remote"
    LATE_NIGHT_WHISPER = "late_night_whisper"
    INSTITUTIONAL_PSA = "institutional_psa"

    @property
    def voice_count(self) -> int:
        """Number of distinct voices this format needs."""
        return 2 if self in (AdFormat.CLASSIC_PITCH, AdFormat.DUO_SCENE, AdFormat.TESTIMONIAL) else 1


@dataclass
class SonicWorld:
    """Sonic palette for an ad: environment, music bed, and transition motif."""

    environment: str = ""
    music_bed: str = "lounge"
    transition_motif: str = "chime"
    sonic_signature: str = ""  # e.g. "ice_clink+startup_synth" for brand motif generation


@dataclass
class CampaignSpine:
    """Per-brand creative memory that shapes recurring ad campaigns."""

    premise: str = ""
    sonic_signature: str = ""  # e.g. "ice_clink+startup_synth"
    format_pool: list[str] = field(default_factory=list)
    # The configured character identity. This is an ``AdVoice.name`` rather
    # than a provider ID, so a character stays the same through a TTS rescue.
    spokesperson_voice: str = ""
    # Distinguish an omitted direct mapping from a malformed explicit one. The
    # latter must remain visible and fail closed instead of silently becoming a
    # legacy role-only campaign.
    spokesperson_voice_declared: bool = False
    # Derived from ``spokesperson_voice`` at config load; never read from TOML.
    spokesperson_role: str = ""
    # Compatibility-only role hint for pre-direct-cast radio.toml files. New
    # identity ownership must use ``spokesperson_voice``.
    spokesperson: str = ""
    escalation_rule: str = ""  # natural language for prompt


@dataclass
class AdBrand:
    """A fictional advertiser that can recur across breaks."""

    name: str
    tagline: str
    category: str = "general"
    recurring: bool = True
    campaign: CampaignSpine | None = None
    # Derived at config load. Invalid directly-owned campaigns stay visible in
    # inventory but are never selected for airtime.
    cast_eligible: bool = True


@dataclass
class AdVoice:
    """A non-host voice used to perform commercial copy."""

    name: str
    voice: str
    style: str  # character description for the prompt
    role: str = ""  # speaker role: "hammer", "seductress", etc.
    engine: str = "edge"  # edge|openai|azure|elevenlabs
    edge_fallback_voice: str = ""  # edge-tts voice used when a cloud TTS engine falls back
    # Empty means use the unchanged house defaults in ``audio.tts``.
    voice_settings: dict[str, float | bool] = field(default_factory=dict)
    # Candidate voices stay completely out of ordinary casting until an
    # operator has reviewed the provider audition. The config loader defaults
    # this to false for new TOML rows; the dataclass default keeps legacy
    # programmatic fixtures compatible.
    airtime_approved: bool = True
    # House support is deliberately a separate casting tier.  Such a voice may
    # answer a directly owned character, but never front a generic/legacy spot
    # or become a campaign's named spokesperson.
    secondary_only: bool = False
    # Derived from campaign ownership at config load; never read from TOML.
    # A recurring character may intentionally own several campaigns.
    reserved_for: frozenset[str] = field(default_factory=frozenset)
    # Derived from direct-cast compilation. An identity named by an invalid
    # direct campaign is withheld from every cast rather than leaking into a
    # different brand as a supposedly unreserved partner.
    direct_identity_quarantined: bool = False


@dataclass(frozen=True)
class AdCastReport:
    """Validated direct-character ownership derived from ad config.

    This is intentionally a narrow ad-specific compile result, not a generic
    eligibility framework. Keys are character names, never provider voice IDs.
    """

    reserved_voice_owners: dict[str, frozenset[str]] = field(default_factory=dict)
    quarantined_voice_names: frozenset[str] = field(default_factory=frozenset)
    primary_roles: dict[str, str] = field(default_factory=dict)
    excluded_brands: frozenset[str] = field(default_factory=frozenset)
    warnings: tuple[str, ...] = ()


@dataclass
class AdPart:
    """One structured unit inside an ad script: voice, SFX, pause, or environment."""

    type: str  # "voice", "sfx", "pause", "environment"
    text: str = ""
    voice: str = ""
    sfx: str = ""
    duration: float = 0.0
    role: str = ""  # which speaker role delivers this part
    environment: str = ""  # environment cue for ambience


@dataclass
class AdScript:
    """Structured ad script returned by the LLM before audio synthesis."""

    brand: str
    parts: list[AdPart] = field(default_factory=list)
    summary: str = ""
    mood: str = ""  # legacy alias, set to sonic.music_bed
    format: str = "classic_pitch"
    sonic: SonicWorld = field(default_factory=SonicWorld)
    roles_used: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Format and sonic palette constants
# ---------------------------------------------------------------------------

AD_FORMATS: dict[str, str] = {
    AdFormat.CLASSIC_PITCH: (
        "One aggressive announcer delivers the pitch, ending with a ultra-fast legal disclaimer. "
        "Structure: hook -> build tension -> deliver the pitch -> DISCLAIMER_GOBLIN rattles off "
        "the fine print at machine-gun speed. Two speakers: HAMMER sells it, DISCLAIMER_GOBLIN "
        "buries the bad news. Confident, polished, slightly unhinged sincerity."
    ),
    AdFormat.TESTIMONIAL: (
        "A fake customer testimonial followed by an announcer button. Two speakers: "
        "the WITNESS delivers their suspiciously specific praise, then the main speaker "
        "wraps with a tagline. The witness should sound rehearsed but trying to sound natural."
    ),
    AdFormat.DUO_SCENE: (
        "Two characters in a scene, arguing or negotiating. One is clearly losing the argument. "
        "The comedy comes from the dynamic between them. End with a product plug that resolves "
        "(or fails to resolve) the conflict. Two speakers with distinct roles."
    ),
    AdFormat.LIVE_REMOTE: (
        "A field reporter at a ridiculous location or event related to the brand. "
        "Background chaos implied. The reporter struggles to maintain professionalism. "
        "Single speaker pretending to be on-location. Use environment cues."
    ),
    AdFormat.LATE_NIGHT_WHISPER: (
        "Intimate, sensual, slightly cursed. ASMR energy. The product is described "
        "with inappropriate levels of tenderness. Slow pacing, dramatic pauses. "
        "Single speaker. Think late-night Italian TV shopping meets poetry."
    ),
    AdFormat.INSTITUTIONAL_PSA: (
        "Serious public-service announcement tone for something completely deranged. "
        "Official language, bureaucratic gravitas, absurd content. "
        "Single speaker. The contrast between tone and subject IS the joke."
    ),
}

SPEAKER_ROLES: dict[str, str] = {
    "hammer": "The Hammer: booming national TV voice, dramatic pauses, sells the apocalypse with a smile",
    "seductress": "The Seductress: whisper-ASMR menace, makes everything sound inappropriately intimate",
    "bureaucrat": "The Bureaucrat: dry official notice voice, reads absurd things with total sincerity",
    "maniac": "The Maniac: oversold shopping-channel energy, everything is THE GREATEST THING EVER",
    "witness": "The Witness: fake customer testimonial, suspiciously specific, clearly reading a script",
    "disclaimer_goblin": "The Disclaimer Goblin: ultra-fast legal cleanup, buries the bad news in speed",
}

SONIC_ENVIRONMENTS: dict[str, str] = {
    "cafe": "Italian cafe ambience, espresso machine hissing, distant chatter",
    "motorway": "Highway noise, car engine hum, wind rushing past",
    "beach": "Mediterranean beach, waves lapping, distant seagulls",
    "showroom": "Echoey showroom floor, polished surfaces, muzak undertone",
    "stadium": "Crowd roar, echo of announcer PA system",
    "luxury_spa": "Zen water trickling, soft chimes, hushed whispers",
    "occult_basement": "Dripping water, distant chanting, candle-flicker ambience",
    "shopping_channel": "Bright studio energy, phone ringing, audience gasps",
}

SONIC_MUSIC_BEDS: dict[str, str] = {
    "lounge": "warm mid-frequency hum, gentle modulation",
    "tarantella_pop": "fast bright rhythm, Italian folk-pop energy",
    "cheap_synth_romance": "mid frequencies, slow tremolo, warm synth pads",
    "overblown_epic": "layered low+high drones, cinematic grandiosity",
    "suspicious_jazz": "detuned intervals, slow modulation, noir vibes",
    "discount_techno": "fast pulse, rapid tremolo, budget club energy",
    # Legacy moods kept as aliases
    "dramatic": "low rumbling drone with slow LFO",
    "upbeat": "bright rhythmic pulse",
    "mysterious": "dark filtered noise with reverb feel",
    "epic": "layered low+high drones",
}

# ---------------------------------------------------------------------------
# Selection logic constants
# ---------------------------------------------------------------------------

# Default sonic palettes by brand category. Each category gets multiple variants so
# ads can shift texture between breaks instead of sounding like one recycled bed.
_CATEGORY_SONIC: dict[str, list[SonicWorld]] = {
    "tech": [
        SonicWorld(environment="shopping_channel", music_bed="discount_techno", transition_motif="startup_synth"),
        SonicWorld(environment="showroom", music_bed="upbeat", transition_motif="whoosh"),
    ],
    "food": [
        SonicWorld(environment="cafe", music_bed="tarantella_pop", transition_motif="register_hit"),
        SonicWorld(environment="shopping_channel", music_bed="cheap_synth_romance", transition_motif="ice_clink"),
        SonicWorld(environment="cafe", music_bed="upbeat", transition_motif="mandolin_sting"),
    ],
    "fashion": [
        SonicWorld(environment="showroom", music_bed="suspicious_jazz", transition_motif="whoosh"),
        SonicWorld(environment="showroom", music_bed="discount_techno", transition_motif="tape_stop"),
    ],
    "beauty": [
        SonicWorld(environment="luxury_spa", music_bed="cheap_synth_romance", transition_motif="mandolin_sting"),
        SonicWorld(environment="showroom", music_bed="lounge", transition_motif="ice_clink"),
    ],
    "services": [
        SonicWorld(environment="motorway", music_bed="lounge", transition_motif="chime"),
        SonicWorld(environment="shopping_channel", music_bed="discount_techno", transition_motif="register_hit"),
    ],
    "finance": [
        SonicWorld(environment="", music_bed="suspicious_jazz", transition_motif="hotline_beep"),
        SonicWorld(environment="showroom", music_bed="lounge", transition_motif="ding"),
    ],
    "health": [
        SonicWorld(environment="", music_bed="lounge", transition_motif="ding"),
        SonicWorld(environment="luxury_spa", music_bed="cheap_synth_romance", transition_motif="chime"),
    ],
    "fitness": [
        SonicWorld(environment="stadium", music_bed="upbeat", transition_motif="whoosh"),
        SonicWorld(environment="motorway", music_bed="discount_techno", transition_motif="startup_synth"),
    ],
    "tourism": [
        SonicWorld(environment="beach", music_bed="tarantella_pop", transition_motif="mandolin_sting"),
        SonicWorld(environment="shopping_channel", music_bed="overblown_epic", transition_motif="whoosh"),
    ],
}

# Default roles needed per format
_FORMAT_ROLES: dict[str, list[str]] = {
    AdFormat.CLASSIC_PITCH: ["hammer", "disclaimer_goblin"],
    AdFormat.TESTIMONIAL: ["witness", "hammer"],
    AdFormat.DUO_SCENE: ["hammer", "maniac"],
    AdFormat.LIVE_REMOTE: ["hammer"],
    AdFormat.LATE_NIGHT_WHISPER: ["seductress"],
    AdFormat.INSTITUTIONAL_PSA: ["bureaucrat"],
}

ALL_FORMATS = [f.value for f in AdFormat]


def compile_ad_cast(brands: list[AdBrand], voices: list[AdVoice]) -> AdCastReport:
    """Compile direct campaign identities into safe, character-owned casting.

    A direct identity is valid only when it names exactly one configured
    character, that character's role is available in *every* declared format,
    and each supporting role can be supplied by an unreserved house voice.  A
    bad campaign is excluded from rotation rather than silently clamped to a
    different role or borrowing another brand's character.

    Legacy ``CampaignSpine.spokesperson`` deliberately does not participate in
    this compiler.  A later, audition-gated migration moves those role hints
    to direct character names once every required house support exists.
    """

    voices_by_name: dict[str, list[AdVoice]] = {}
    for voice in voices:
        name = voice.name.strip()
        if name:
            voices_by_name.setdefault(name, []).append(voice)

    brand_name_counts: dict[str, int] = {}
    for brand in brands:
        if isinstance(brand.name, str) and brand.name and brand.name == brand.name.strip():
            brand_name_counts[brand.name] = brand_name_counts.get(brand.name, 0) + 1
    duplicate_brand_names = {name for name, count in brand_name_counts.items() if count > 1}
    warnings: list[str] = []
    excluded: set[str] = set()
    candidates: dict[str, tuple[AdBrand, AdVoice, tuple[str, ...]]] = {}
    # Any configured direct identity is permanently unavailable as a generic
    # or partner actor. This deliberately includes identities on invalid
    # campaigns: a bad campaign must not release its character into another
    # brand's cast.
    direct_identity_voice_names: set[str] = set()

    def exclude(brand: AdBrand, reason: str) -> None:
        """Record a listener-safe warning without exposing a provider ID."""
        if brand.name in excluded:
            return
        excluded.add(brand.name)
        warnings.append(f"campaign {brand.name!r} excluded from ad rotation: {reason}")

    for brand in brands:
        valid_brand_name = isinstance(brand.name, str) and bool(brand.name) and brand.name == brand.name.strip()
        campaign = brand.campaign
        if campaign is None:
            if not valid_brand_name:
                exclude(brand, "campaign name is malformed")
            elif brand.name in duplicate_brand_names:
                exclude(brand, "campaign name is ambiguous")
            continue
        direct_name = campaign.spokesperson_voice.strip() if isinstance(campaign.spokesperson_voice, str) else ""
        direct_declared = campaign.spokesperson_voice_declared or bool(direct_name)
        if not direct_declared:
            if not valid_brand_name:
                exclude(brand, "campaign name is malformed")
            elif brand.name in duplicate_brand_names:
                exclude(brand, "campaign name is ambiguous")
            continue
        if not direct_name:
            exclude(brand, "configured character is malformed")
            continue

        configured_matches = voices_by_name.get(direct_name, [])
        # Quarantine every matching row before deciding whether the mapping is
        # usable. Duplicate IDs must not leak through a role fallback either.
        direct_identity_voice_names.update(voice.name for voice in configured_matches)
        if not valid_brand_name:
            exclude(brand, "campaign name is malformed")
            continue
        if brand.name in duplicate_brand_names:
            exclude(brand, "campaign name is ambiguous")
            continue
        if len(configured_matches) != 1:
            exclude(
                brand,
                "configured character is ambiguous" if configured_matches else "configured character is unavailable",
            )
            continue
        configured_voice = configured_matches[0]
        if configured_voice.role not in SPEAKER_ROLES:
            exclude(brand, "configured character has no supported ad role")
            continue
        if configured_voice.secondary_only:
            exclude(brand, "configured character is supporting-only")
            continue

        if not configured_voice.airtime_approved:
            # An existing legacy role pin may continue serving this campaign
            # while a replacement is auditioned. A brand with no prior safe
            # identity is withheld rather than silently recast at random.
            if campaign.spokesperson in SPEAKER_ROLES:
                warnings.append(f"campaign {brand.name!r} keeps its existing mapping: character awaits approval")
                continue
            exclude(brand, "configured character awaits approval")
            continue

        pool = campaign.format_pool
        if not isinstance(pool, list) or not pool:
            exclude(brand, "direct character requires an explicit compatible format pool")
            continue
        if any(not isinstance(ad_format, str) or ad_format not in ALL_FORMATS for ad_format in pool):
            exclude(brand, "format pool contains an unsupported format")
            continue
        if any(configured_voice.role not in _FORMAT_ROLES[ad_format] for ad_format in pool):
            exclude(brand, "format pool is incompatible with its character")
            continue

        candidates[brand.name] = (brand, configured_voice, tuple(pool))

    # A recurring character may own several explicit campaigns (for example,
    # the same announcer can front three brands). Ownership is therefore
    # many-to-many: the selected brand must be one of those owners, never just
    # the first config claimant.
    def reservation_map() -> dict[str, frozenset[str]]:
        owners: dict[str, set[str]] = {}
        for brand_name, (_brand, voice, _pool) in candidates.items():
            if brand_name not in excluded:
                owners.setdefault(voice.name, set()).add(brand_name)
        return {voice_name: frozenset(brand_names) for voice_name, brand_names in owners.items()}

    # Supporting actors are drawn only from house voices that are not named by
    # any direct campaign, valid or invalid. That fixed pool avoids a
    # one-pass/reservation-release race and enforces the single ownership rule.
    unreserved_voices = [
        voice
        for voice in voices
        if voice.airtime_approved
        and not voice.direct_identity_quarantined
        and voice.name not in direct_identity_voice_names
    ]
    for _brand_name, (brand, voice, candidate_pool) in candidates.items():
        required_support_roles = {
            role for ad_format in candidate_pool for role in _FORMAT_ROLES[ad_format] if role != voice.role
        }
        missing_support = [
            role
            for role in required_support_roles
            if not any(candidate.role == role for candidate in unreserved_voices)
        ]
        if missing_support:
            exclude(brand, "no unreserved supporting character can perform every configured format")

    reserved_voice_owners = reservation_map()
    quarantined_voice_names = direct_identity_voice_names - set(reserved_voice_owners)
    primary_roles = {
        brand_name: voice.role
        for brand_name, (_brand, voice, _pool) in candidates.items()
        if brand_name not in excluded
    }
    return AdCastReport(
        reserved_voice_owners=reserved_voice_owners,
        quarantined_voice_names=frozenset(quarantined_voice_names),
        primary_roles=primary_roles,
        excluded_brands=frozenset(excluded),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Selection helpers
# ---------------------------------------------------------------------------


def _pick_brand(brands: list[AdBrand], ad_history: list[AdHistoryEntry]) -> AdBrand:
    """Pick a safe brand, avoiding the last 3 and weighting recurring brands."""
    safe_brands = [brand for brand in brands if brand.cast_eligible]
    if not safe_brands:
        raise ValueError("No safe ad campaigns are configured")
    recent_names = {e.brand for e in list(ad_history)[-3:]}
    eligible = [brand for brand in safe_brands if brand.name not in recent_names]
    if not eligible:
        eligible = safe_brands  # allow safe repeats if pool exhausted
    weights = [3 if b.recurring else 1 for b in eligible]
    return random.choices(eligible, weights=weights, k=1)[0]


def _select_ad_creative(
    brand: AdBrand,
    state: StationState,
    num_voices: int,
) -> tuple[str, SonicWorld, list[str]]:
    """Pick the ad format, sonic world, and needed speaker roles for this spot.

    Voice-count guard: if fewer than 2 distinct voices are available, multi-voice
    formats (duo_scene, testimonial) are excluded from candidates.
    """
    if not brand.cast_eligible:
        raise ValueError(f"Campaign {brand.name!r} is excluded from ad rotation")

    campaign = brand.campaign
    # The derived role is populated only after the character has passed config
    # validation and its human/provider audition gate.  A staged replacement
    # can therefore retain its legacy safe mapping without becoming a random
    # unapproved voice in the live rotation.
    direct_identity = bool(campaign and campaign.spokesperson_role)

    # A direct identity must keep its declared format pool intact.  Generic and
    # legacy campaigns retain the historical graceful fallback while a later
    # gated migration moves every campaign identity to ``spokesperson_voice``.
    if campaign and campaign.format_pool:
        candidates = [f for f in campaign.format_pool if f in ALL_FORMATS]
        if not candidates:
            if direct_identity:
                raise ValueError(f"Campaign {brand.name!r} has no safe configured format")
            candidates = list(ALL_FORMATS)
    else:
        if direct_identity:
            raise ValueError(f"Campaign {brand.name!r} has no explicit format pool")
        candidates = list(ALL_FORMATS)

    # Voice-count guard: exclude multi-voice formats if < 2 voices
    if num_voices < 2:
        candidates = [f for f in candidates if AdFormat(f).voice_count < 2]
        if not candidates:
            if direct_identity:
                raise ValueError(f"Campaign {brand.name!r} has no safe single-voice format")
            candidates = [f.value for f in AdFormat if f.voice_count < 2]

    # Avoid last-used format for this brand
    brand_history = [e for e in state.ad_history if e.brand == brand.name]
    if brand_history:
        last_format = brand_history[-1].format
        if last_format and len(candidates) > 1:
            candidates = [f for f in candidates if f != last_format] or candidates

    ad_format = random.choice(candidates)

    # Pick sonic world
    sonic_variants = _CATEGORY_SONIC.get(brand.category, [SonicWorld()])
    if brand_history and len(sonic_variants) > 1:
        last_sonic = brand_history[-1]
        sonic_variants = [
            variant
            for variant in sonic_variants
            if not (
                variant.environment == last_sonic.environment
                and variant.music_bed == last_sonic.music_bed
                and variant.transition_motif == last_sonic.transition_motif
            )
        ] or sonic_variants
    cat_sonic = replace(random.choice(sonic_variants))

    if campaign and campaign.sonic_signature:
        sonic = SonicWorld(
            environment=cat_sonic.environment,
            music_bed=cat_sonic.music_bed,
            transition_motif=campaign.sonic_signature.split("+")[0],
            sonic_signature=campaign.sonic_signature,
        )
    else:
        sonic = cat_sonic

    # Preserve the format's speaker order for script structure. A direct
    # character occupies its own role slot, so Il Razzo remains the disclaimer
    # goblin in [hammer, disclaimer_goblin] rather than being recast as hammer.
    if direct_identity:
        assert campaign is not None
        default_roles = _FORMAT_ROLES.get(ad_format, ["hammer"])
        primary_role = campaign.spokesperson_role
        if primary_role not in default_roles:
            raise ValueError(f"Campaign {brand.name!r} direct character is incompatible with {ad_format!r}")
        roles = list(default_roles)
    # Transitional compatibility for existing role-pinned config.  This branch
    # deliberately reserves no character and is removed by the later gated
    # migration once every required house support has been approved.
    elif campaign and campaign.spokesperson and campaign.spokesperson in SPEAKER_ROLES:
        default_roles = _FORMAT_ROLES.get(ad_format, ["hammer"])
        primary_role = campaign.spokesperson
        if primary_role not in default_roles:
            primary_role = default_roles[0]
        if AdFormat(ad_format).voice_count >= 2:
            secondary = next((r for r in default_roles if r != primary_role), default_roles[-1])
            roles = [primary_role, secondary]
        else:
            roles = [primary_role]
    else:
        roles = _FORMAT_ROLES.get(ad_format, ["hammer"])

    return ad_format, sonic, roles


def _cast_voices(
    brand: AdBrand,
    voices: list[AdVoice],
    hosts: list[HostPersonality],
    roles_needed: list[str],
) -> dict[str, AdVoice]:
    """Map roles to safe ad voices without crossing character ownership.

    A directly owned campaign always receives its configured character in its
    declared role slot. Its partners may only be unreserved house voices;
    generic/legacy campaigns
    may use only unreserved voices and retain their historical fallback path.
    """
    campaign = brand.campaign
    direct_name = (
        campaign.spokesperson_voice.strip() if campaign and isinstance(campaign.spokesperson_voice, str) else ""
    )
    direct_identity = bool(campaign and campaign.spokesperson_role)
    if direct_identity and not brand.cast_eligible:
        raise ValueError(f"Campaign {brand.name!r} is excluded from ad rotation")

    usable_voices = [
        voice
        for voice in voices
        if (
            voice.airtime_approved
            and not voice.direct_identity_quarantined
            and (not voice.reserved_for or brand.name in voice.reserved_for)
        )
    ]
    if not usable_voices:
        if direct_identity:
            raise ValueError(f"Campaign {brand.name!r} has no safe character voice")
        if not hosts:
            raise ValueError("At least one host or ad voice is required to cast ad voices")
        # No voices configured — assign the same host voice to every needed role
        host = random.choice(hosts)
        fallback = AdVoice(
            name=host.name,
            voice=host.voice,
            style=host.style,
            engine=host.engine,
            edge_fallback_voice=host.edge_fallback_voice,
        )
        return {role: fallback for role in roles_needed} if roles_needed else {"default": fallback}

    # Build role->voices index. A role may have several voices (e.g. two
    # "hammer" announcers from different engines); casting picks one at random
    # so ad breaks vary instead of always using the same timbre per role.
    role_index: dict[str, list[AdVoice]] = {}
    for v in usable_voices:
        if v.role:
            role_index.setdefault(v.role, []).append(v)

    # Generic/legacy spots never receive the house-support tier as their main
    # identity.  Direct campaigns use the complete role index below only for a
    # non-identity partner role.
    primary_usable_voices = [voice for voice in usable_voices if not voice.secondary_only]

    primary_voice: AdVoice | None = None
    primary_role = ""
    if direct_identity:
        assert campaign is not None
        matches = [voice for voice in usable_voices if voice.name.strip() == direct_name]
        if len(matches) != 1:
            raise ValueError(f"Campaign {brand.name!r} has no safe configured character")
        primary_voice = matches[0]
        primary_role = campaign.spokesperson_role or primary_voice.role
        if primary_role not in roles_needed:
            raise ValueError(f"Campaign {brand.name!r} direct character is not assigned a format role")

    result: dict[str, AdVoice] = {}
    used_voices: set[str] = set()

    for role in roles_needed:
        if direct_identity and role == primary_role:
            if primary_voice is None or primary_voice.role != role:
                raise ValueError(f"Campaign {brand.name!r} direct character does not match the requested role")
            result[role] = primary_voice
            used_voices.add(primary_voice.name)
            continue

        # Direct campaigns may use an unreserved house voice only for the
        # non-identity roles. There is intentionally no random cross-brand
        # fallback when a partner is unavailable.
        if direct_identity:
            pool = [
                voice for voice in role_index.get(role, []) if not voice.reserved_for and voice.name not in used_voices
            ]
            if not pool:
                raise ValueError(f"Campaign {brand.name!r} has no safe supporting character for role {role!r}")
            pick = random.choice(pool)
            result[role] = pick
            used_voices.add(pick.name)
            continue

        candidates = [voice for voice in role_index.get(role, []) if not voice.secondary_only]
        # Prefer a voice not already cast in this spot; fall back to reusing one
        # of the role's voices, then to any voice in the pool.
        pool = [v for v in candidates if v.name not in used_voices] or candidates
        if not pool:
            pool = [v for v in primary_usable_voices if v.name not in used_voices] or list(primary_usable_voices)
        if not pool:
            if not hosts:
                raise ValueError("At least one non-supporting host or ad voice is required to cast ad voices")
            host = random.choice(hosts)
            pick = AdVoice(
                name=host.name,
                voice=host.voice,
                style=host.style,
                engine=host.engine,
                edge_fallback_voice=host.edge_fallback_voice,
            )
            result[role] = pick
            used_voices.add(pick.name)
            continue
        pick = random.choice(pool)
        result[role] = pick
        used_voices.add(pick.name)

    return result
