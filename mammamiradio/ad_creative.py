"""Ad creative system: format palettes, sonic worlds, brand selection, and voice casting."""

from __future__ import annotations

import random
from dataclasses import replace

from mammamiradio.config import StationConfig
from mammamiradio.models import (
    AdBrand,
    AdFormat,
    AdVoice,
    SonicWorld,
    StationState,
)

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


def _pick_brand(brands: list[AdBrand], ad_history: list) -> AdBrand:
    """Pick a brand, avoiding the last 3 aired and weighting recurring brands higher."""
    recent_names = {e.brand for e in list(ad_history)[-3:]}
    eligible = [b for b in brands if b.name not in recent_names]
    if not eligible:
        eligible = list(brands)  # allow repeats if pool exhausted
    weights = [3 if b.recurring else 1 for b in eligible]
    return random.choices(eligible, weights=weights, k=1)[0]


def _select_ad_creative(
    brand: AdBrand,
    state: StationState,
    config: StationConfig,
) -> tuple[str, SonicWorld, list[str]]:
    """Pick the ad format, sonic world, and needed speaker roles for this spot.

    Voice-count guard: if fewer than 2 distinct voices are available, multi-voice
    formats (duo_scene, testimonial) are excluded from candidates.
    """
    # Determine available distinct voices
    num_voices = len(config.ads.voices) if config.ads.voices else 1

    # Pick format
    if brand.campaign and brand.campaign.format_pool:
        candidates = list(brand.campaign.format_pool)
    else:
        candidates = list(ALL_FORMATS)

    # Voice-count guard: exclude multi-voice formats if < 2 voices
    if num_voices < 2:
        candidates = [f for f in candidates if AdFormat(f).voice_count < 2]
        if not candidates:
            candidates = [AdFormat.CLASSIC_PITCH]

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

    if brand.campaign and brand.campaign.sonic_signature:
        sonic = SonicWorld(
            environment=cat_sonic.environment,
            music_bed=cat_sonic.music_bed,
            transition_motif=brand.campaign.sonic_signature.split("+")[0],
            sonic_signature=brand.campaign.sonic_signature,
        )
    else:
        sonic = cat_sonic

    # Determine needed roles
    if brand.campaign and brand.campaign.spokesperson:
        primary_role = brand.campaign.spokesperson
        default_roles = _FORMAT_ROLES.get(ad_format, ["hammer"])
        if AdFormat(ad_format).voice_count >= 2:
            # Primary is the spokesperson, secondary is the other role
            secondary = [r for r in default_roles if r != primary_role]
            roles = [primary_role] + (secondary if secondary else [default_roles[-1]])
        else:
            roles = [primary_role]
    else:
        roles = _FORMAT_ROLES.get(ad_format, ["hammer"])

    return ad_format, sonic, roles


def _cast_voices(
    brand: AdBrand,
    config: StationConfig,
    roles_needed: list[str],
) -> dict[str, AdVoice]:
    """Map needed speaker roles to actual AdVoice instances.

    Falls back to random voice from pool if no voice matches a needed role.
    """
    voices = config.ads.voices
    if not voices:
        # No voices configured — assign the same host voice to every needed role
        host = random.choice(config.hosts)
        fallback = AdVoice(name=host.name, voice=host.voice, style=host.style)
        return {role: fallback for role in roles_needed} if roles_needed else {"default": fallback}

    # Build role->voice index
    role_index: dict[str, AdVoice] = {}
    for v in voices:
        if v.role:
            role_index[v.role] = v

    result: dict[str, AdVoice] = {}
    used_voices: set[str] = set()

    for role in roles_needed:
        if role in role_index:
            result[role] = role_index[role]
            used_voices.add(role_index[role].name)
        else:
            # Fallback: pick a random voice not already used
            available = [v for v in voices if v.name not in used_voices]
            if not available:
                available = list(voices)
            pick = random.choice(available)
            result[role] = pick
            used_voices.add(pick.name)

    return result
