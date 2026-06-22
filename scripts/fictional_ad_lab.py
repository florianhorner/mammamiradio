#!/usr/bin/env python3
"""Generate fictional ad-brand candidates for Mamma Mi Radio.

The lab is intentionally artifact-first: it proposes brands, campaign spines,
trigger hooks, and sample reads, but never edits radio.toml. A human still picks
the winners after collision checks and listening.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python <3.11 fallback
    import tomli as tomllib  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "radio.toml"
DEFAULT_OUTPUT_DIR = (
    Path("/opt/cursor/artifacts/ad-lab") if Path("/opt/cursor/artifacts").exists() else REPO_ROOT / "tmp" / "ad-lab"
)

VALID_FORMATS = {
    "classic_pitch",
    "testimonial",
    "duo_scene",
    "live_remote",
    "late_night_whisper",
    "institutional_psa",
}
VALID_SPEAKERS = {"hammer", "seductress", "bureaucrat", "maniac", "witness", "disclaimer_goblin"}

REAL_BRAND_MARKERS = {
    "amazon",
    "apple",
    "aperol",
    "armani",
    "barilla",
    "campari",
    "eni",
    "ferrari",
    "fiat",
    "google",
    "gucci",
    "illy",
    "intesa",
    "lavazza",
    "mediaset",
    "netflix",
    "nutella",
    "poste",
    "prada",
    "rai",
    "sky",
    "spotify",
    "tim",
    "unicredit",
    "versace",
    "vodafone",
}
EXTRA_REVIEW_CATEGORIES = {"finance", "health", "pharma", "insurance"}

BFF_LOAD_PERSPECTIVE = (
    "Trigger hooks are labels for the BFF/load view, not automation rules. Use "
    "them when the station is healthy, the ad break is already appropriate, and "
    "producer_headroom.headroom_ok is true; never let a spec concept force an ad "
    "during queue rescue or first-audio recovery."
)


@dataclass(frozen=True)
class CampaignDraft:
    premise: str
    sonic_signature: str
    format_pool: tuple[str, ...]
    spokesperson: str
    escalation_rule: str


@dataclass(frozen=True)
class AdLabCandidate:
    name: str
    tagline: str
    category: str
    recurring: bool
    concept: str
    why_it_fits: str
    trigger_hooks: tuple[str, ...]
    load_perspective: str
    campaign: CampaignDraft | None = None


@dataclass(frozen=True)
class SafetyReview:
    name: str
    status: str
    flags: tuple[str, ...]
    search_queries: tuple[str, ...]


def _campaign(
    premise: str,
    sonic_signature: str,
    format_pool: tuple[str, ...],
    spokesperson: str,
    escalation_rule: str,
) -> CampaignDraft:
    invalid_formats = set(format_pool) - VALID_FORMATS
    if invalid_formats:
        raise ValueError(f"Unknown ad format(s): {sorted(invalid_formats)}")
    if spokesperson not in VALID_SPEAKERS:
        raise ValueError(f"Unknown spokesperson role: {spokesperson}")
    return CampaignDraft(premise, sonic_signature, format_pool, spokesperson, escalation_rule)


def _candidate(
    name: str,
    tagline: str,
    category: str,
    recurring: bool,
    concept: str,
    why_it_fits: str,
    trigger_hooks: tuple[str, ...],
    campaign: CampaignDraft | None = None,
) -> AdLabCandidate:
    return AdLabCandidate(
        name=name,
        tagline=tagline,
        category=category,
        recurring=recurring,
        concept=concept,
        why_it_fits=why_it_fits,
        trigger_hooks=trigger_hooks,
        load_perspective=BFF_LOAD_PERSPECTIVE,
        campaign=campaign,
    )


CANDIDATE_POOL: tuple[AdLabCandidate, ...] = (
    _candidate(
        "Ombrello Preventivo",
        "Si apre prima che tu abbia torto.",
        "services",
        True,
        "A subscription umbrella service that predicts rain only after everyone is already soaked.",
        "It gives the hosts a simple physical gag and a repeatable apology campaign.",
        ("rain forecast", "listener arrives soaked", "queue is healthy before a weather bit"),
        _campaign(
            "A weather service sells umbrellas with total confidence and suspicious timing.",
            "rain_sting+chime",
            ("classic_pitch", "live_remote", "testimonial"),
            "hammer",
            "Each ad blames a more absurd authority for late umbrella delivery.",
        ),
    ),
    _candidate(
        "Ascensore Sentimentale",
        "Sale, scende, ti giudica.",
        "services",
        True,
        "Elevators with emotional commentary, relationship advice, and a memory for every awkward silence.",
        "Perfect for Home Assistant houses and apartment-building Italian comedy.",
        ("home context mentions stairs", "late-night apartment bit", "after a dramatic ballad"),
        _campaign(
            "A building elevator becomes a therapist, gossip columnist, and municipal witness.",
            "ding+cheap_synth_romance",
            ("duo_scene", "testimonial", "institutional_psa"),
            "bureaucrat",
            "Each spot reveals another floor where the elevator has taken sides.",
        ),
    ),
    _candidate(
        "Panificio Pneumatico",
        "Pane gonfio, orgoglio pieno.",
        "food",
        True,
        "A bakery inflates bread with industrial drama and sells the air as tradition.",
        "Food pride plus obviously fake engineering makes it feel native to this station.",
        ("breakfast", "kitchen HA context", "after a thin-sounding track needs warmth"),
        _campaign(
            "An artisan bakery insists compressed air is a forgotten regional recipe.",
            "register_hit+mandolin_sting",
            ("classic_pitch", "testimonial", "live_remote"),
            "witness",
            "Each ad upgrades the bread pressure until the neighborhood files complaints.",
        ),
    ),
    _candidate(
        "Notaio Espresso",
        "Contratti caldi in trenta secondi.",
        "services",
        True,
        "A notary booth that stamps documents with espresso foam and terrifying speed.",
        "It turns bureaucracy into a sonic gag without real-world legal claims.",
        ("admin/operator theme", "morning coffee", "after a bureaucratic news flash"),
        _campaign(
            "A coffee-bar notary promises fast paperwork with increasingly suspicious seals.",
            "espresso_hiss+ding",
            ("institutional_psa", "classic_pitch", "duo_scene"),
            "bureaucrat",
            "Each ad compresses a more serious legal act into a smaller coffee cup.",
        ),
    ),
    _candidate(
        "Gomme Nervose",
        "Aderenza con ansia inclusa.",
        "cars",
        True,
        "Car tires that overreact to corners, potholes, and emotional baggage.",
        "A car-category recurring saga without borrowing from real car brands.",
        ("traffic joke", "fast song", "driveway or garage context"),
        _campaign(
            "A tire company sells anxious grip as a safety innovation.",
            "whoosh+tape_stop",
            ("classic_pitch", "live_remote", "testimonial"),
            "hammer",
            "Each ad reveals a new tire fear presented as premium sensitivity.",
        ),
    ),
    _candidate(
        "Frigo Sincero",
        "Conserva il cibo, distrugge le illusioni.",
        "home",
        True,
        "A smart fridge that tells the truth about leftovers, snacks, and midnight choices.",
        "It pairs naturally with Home Assistant context and listener-house jokes.",
        ("kitchen device context", "late-night snack", "listener request after dinner"),
        _campaign(
            "A domestic appliance brand sells brutal honesty as freshness technology.",
            "startup_synth+ice_clink",
            ("duo_scene", "testimonial", "late_night_whisper"),
            "seductress",
            "Each ad gives the fridge a more invasive opinion about family life.",
        ),
    ),
    _candidate(
        "Detersivo Melodramma",
        "Le macchie confessano.",
        "services",
        True,
        "Laundry detergent that makes stains confess their emotional origin.",
        "It gives ads a soap-opera structure and recurring stain witnesses.",
        ("after messy cooking context", "dramatic host fight", "before ad break in Festival mode"),
        _campaign(
            "A detergent brand interrogates stains like suspects in a family drama.",
            "water_splash+overblown_epic",
            ("testimonial", "duo_scene", "institutional_psa"),
            "witness",
            "Each spot reveals a more embarrassing stain backstory.",
        ),
    ),
    _candidate(
        "Agenzia Scuse Rapide",
        "Arriviamo prima della verita.",
        "services",
        True,
        "On-demand excuses for missed dinners, late trains, and suspiciously silent group chats.",
        "It is broad enough to recur and specific enough for listener-context triggers.",
        ("listener dedication", "calendar/time pressure", "host caught in contradiction"),
        _campaign(
            "A professional excuse agency treats everyday lies like premium logistics.",
            "hotline_beep+chime",
            ("classic_pitch", "live_remote", "testimonial"),
            "maniac",
            "Each ad introduces faster, less plausible apology tiers.",
        ),
    ),
    _candidate(
        "Appartamento Gonfiabile",
        "Casa tua, ma con piu pressione.",
        "home",
        False,
        "Emergency inflatable apartments for guests, in-laws, and sudden lifestyle reinventions.",
        "Works as a one-shot absurd product that can appear during home-context breaks.",
        ("guest arrived", "small apartment joke", "queue healthy after listener joins"),
    ),
    _candidate(
        "Acqua Drammatica",
        "Ogni sorso ha un passato.",
        "food",
        True,
        "Bottled water with tragic regional backstories read like opera plots.",
        "A simple premium-food parody with strong sonic direction.",
        ("heat/weather", "after emotional song", "dinner-party listening"),
        _campaign(
            "A bottled water brand sells emotional provenance instead of minerals.",
            "water_drop+cheap_synth_romance",
            ("late_night_whisper", "institutional_psa", "testimonial"),
            "seductress",
            "Each ad reveals a sadder mountain and a more dramatic bottle origin.",
        ),
    ),
    _candidate(
        "Calzino Unico",
        "Il paio e un limite mentale.",
        "fashion",
        True,
        "A fashion label selling single socks as philosophical independence.",
        "Small, visual, and instantly understandable on-air.",
        ("laundry context", "fashion roast", "morning getting-ready bit"),
        _campaign(
            "A fashion house insists unmatched socks are personal liberation.",
            "whoosh+suspicious_jazz",
            ("classic_pitch", "testimonial", "duo_scene"),
            "witness",
            "Each ad makes the missing second sock sound more intentional.",
        ),
    ),
    _candidate(
        "Sveglia Vendicativa",
        "Non ti sveglia. Ti raggiunge.",
        "tech",
        True,
        "An alarm clock that escalates from ringing to personal vendetta.",
        "A strong morning recurring bit with escalating product tiers.",
        ("morning", "low energy host", "after sleepy ballad"),
        _campaign(
            "A clock company sells vengeance as reliability.",
            "alarm_beep+startup_synth",
            ("classic_pitch", "duo_scene", "institutional_psa"),
            "maniac",
            "Each ad adds a more personal wake-up enforcement method.",
        ),
    ),
    _candidate(
        "Casco Parlante",
        "Protegge la testa, commenta il resto.",
        "cars",
        False,
        "A scooter helmet that provides unwanted life coaching at every red light.",
        "Local mobility comedy without touching real vehicle brands.",
        ("traffic", "scooter joke", "after a fast track"),
    ),
    _candidate(
        "Lavanderia Segreta",
        "Puliamo tutto. Non chiediamo niente.",
        "services",
        True,
        "A laundry service with noir energy and suspicious discretion.",
        "Great fit for late-night whisper ads and mob-adjacent ad voices.",
        ("late night", "host implies scandal", "after a dramatic ad from another brand"),
        _campaign(
            "A discreet laundry chain treats every stain like a confidential matter.",
            "hotline_beep+suspicious_jazz",
            ("late_night_whisper", "institutional_psa", "testimonial"),
            "seductress",
            "Each ad hints at darker laundry without ever naming a crime.",
        ),
    ),
    _candidate(
        "Bonsai Condominiale",
        "Piccolo albero, grande assemblea.",
        "home",
        True,
        "A shared apartment-building bonsai that requires meetings, votes, and petty revenge.",
        "It is deeply Italian in scale: tiny object, huge bureaucracy.",
        ("apartment context", "bureaucracy bit", "slow Sunday programming"),
        _campaign(
            "A communal bonsai service sells neighbor conflict as urban greenery.",
            "ding+lounge",
            ("institutional_psa", "duo_scene", "testimonial"),
            "bureaucrat",
            "Each ad introduces a more procedural way to water one very small tree.",
        ),
    ),
    _candidate(
        "Tramonto in Barattolo",
        "Apri, sospira, richiudi.",
        "beauty",
        False,
        "A canned sunset for bathrooms, balconies, and emotional emergencies.",
        "Visual and romantic without needing a recurring saga.",
        ("sunset", "romantic song", "host needs a soft reset before chaos"),
    ),
    _candidate(
        "Gelateria Meteo",
        "Il gusto cambia con la pressione.",
        "food",
        True,
        "Ice cream flavors selected by barometric pressure and local gossip.",
        "Natural weather and food bridge for the station.",
        ("weather", "summer heat", "before a fake forecast"),
        _campaign(
            "A gelato shop treats meteorology as flavor science.",
            "ice_clink+mandolin_sting",
            ("live_remote", "testimonial", "classic_pitch"),
            "hammer",
            "Each ad invents a more specific weather flavor and a less convincing forecast.",
        ),
    ),
    _candidate(
        "Chiavi Filosofiche",
        "Aprono porte, chiudono certezze.",
        "services",
        False,
        "A key-cutting shop whose duplicate keys ask existential questions.",
        "Sharp one-shot premise for late-night station weirdness.",
        ("lost keys", "listener leaves home", "after an introspective track"),
    ),
    _candidate(
        "Parcheggio Immaginario",
        "Il posto c'e, se ci credi.",
        "services",
        True,
        "Parking reservations for spaces that exist emotionally, spiritually, or on alternate Tuesdays.",
        "Traffic and city frustration are endless ad fuel.",
        ("traffic", "car song", "operator wants a city-life ad"),
        _campaign(
            "A parking platform sells hope where asphalt should be.",
            "car_lock+discount_techno",
            ("classic_pitch", "testimonial", "live_remote"),
            "hammer",
            "Each spot invents a more abstract parking tier with a higher monthly fee.",
        ),
    ),
    _candidate(
        "Dentifricio Diplomatico",
        "Negozia con l'alito.",
        "beauty",
        False,
        "Toothpaste that mediates peace between coffee, garlic, and morning meetings.",
        "A clean mouth-care joke without medical claims.",
        ("morning", "coffee joke", "after food talk"),
    ),
    _candidate(
        "Piumone Strategico",
        "Conquista il letto, mantieni il territorio.",
        "home",
        True,
        "A duvet brand that frames bedtime as geopolitics.",
        "Strong with sleepy listeners and household context.",
        ("night", "temperature context", "listener returns after bedtime"),
        _campaign(
            "A bedding company treats blanket sharing like a diplomatic crisis.",
            "soft_chime+lounge",
            ("institutional_psa", "duo_scene", "testimonial"),
            "bureaucrat",
            "Each ad escalates from comfort claims to treaty negotiations.",
        ),
    ),
    _candidate(
        "Moka Ribelle",
        "Fa caffe solo quando lo rispetti.",
        "food",
        True,
        "A stovetop coffee maker that refuses service without proper ceremony.",
        "Coffee plus attitude is native to the station's world.",
        ("morning coffee", "kitchen device", "after a host insults weak espresso"),
        _campaign(
            "A coffee maker brand sells defiance as artisanal standards.",
            "espresso_hiss+mandolin_sting",
            ("classic_pitch", "testimonial", "live_remote"),
            "maniac",
            "Each ad adds another ritual the customer must perform before coffee appears.",
        ),
    ),
    _candidate(
        "Tappeto Testimone",
        "Ha visto tutto. Non parla gratis.",
        "home",
        True,
        "A rug that remembers parties, spills, and lies from the living room.",
        "Great for dinner-party listening and home-context callbacks.",
        ("party mode", "living-room context", "after a listener clip moment"),
        _campaign(
            "A rug brand sells household memory with the tone of a courtroom witness.",
            "dust_hit+suspicious_jazz",
            ("testimonial", "duo_scene", "late_night_whisper"),
            "witness",
            "Each ad reveals the rug knows more and charges more for silence.",
        ),
    ),
    _candidate(
        "Cuscino Oracolo",
        "Dormici sopra. Lui sa gia.",
        "home",
        False,
        "A pillow that gives dreams, warnings, and unhelpful financial advice.",
        "Useful as a softer late-night one-shot.",
        ("late night", "sleepy listener", "after dreamy music"),
    ),
    _candidate(
        "Sugo di Emergenza",
        "Quando la cena ha bisogno di un alibi.",
        "food",
        True,
        "A jarred sauce marketed as crisis management for surprise guests.",
        "Food, panic, and family pressure make it immediately on-brand.",
        ("dinner", "unexpected guest", "after listener request from the kitchen"),
        _campaign(
            "A sauce brand treats dinner shortcuts like emergency response.",
            "jar_pop+tarantella_pop",
            ("classic_pitch", "duo_scene", "testimonial"),
            "hammer",
            "Each ad invents a more dramatic dinner emergency and a more heroic jar.",
        ),
    ),
)


def _canonical(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _tokens(value: str) -> set[str]:
    return set(_canonical(value).split())


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def load_existing_brand_names(config_path: Path) -> set[str]:
    with config_path.open("rb") as handle:
        payload = tomllib.load(handle)
    brands = payload.get("ads", {}).get("brands", [])
    return {_canonical(str(brand.get("name", ""))) for brand in brands if isinstance(brand, dict)}


def review_candidate(candidate: AdLabCandidate, existing_names: set[str] | None = None) -> SafetyReview:
    existing_names = existing_names or set()
    flags: list[str] = []
    name_key = _canonical(candidate.name)
    text_tokens = _tokens(f"{candidate.name} {candidate.tagline}")
    marker_hits = sorted(text_tokens & REAL_BRAND_MARKERS)

    if name_key in existing_names:
        flags.append("duplicate of an existing radio.toml ad brand")
    if marker_hits:
        flags.append(f"contains real-brand marker(s): {', '.join(marker_hits)}")
    if candidate.category in EXTRA_REVIEW_CATEGORIES:
        flags.append(f"{candidate.category} category needs extra legal/taste review")
    if len(candidate.name) > 42:
        flags.append("name is long for an on-air sponsor read")
    if not candidate.tagline.endswith("."):
        flags.append("tagline should read as a finished on-air sentence")

    if any("duplicate" in flag or "real-brand" in flag for flag in flags):
        status = "reject"
    elif flags:
        status = "extra_review"
    else:
        status = "needs_web_check"

    queries = (
        f'"{candidate.name}"',
        f'"{candidate.name}" "{candidate.category}"',
        f'"{candidate.tagline}"',
    )
    return SafetyReview(candidate.name, status, tuple(flags), queries)


def select_candidates(count: int, existing_names: set[str]) -> list[tuple[AdLabCandidate, SafetyReview]]:
    selected: list[tuple[AdLabCandidate, SafetyReview]] = []
    seen: set[str] = set()
    for candidate in CANDIDATE_POOL:
        name_key = _canonical(candidate.name)
        if name_key in seen:
            continue
        seen.add(name_key)
        review = review_candidate(candidate, existing_names)
        if review.status == "reject":
            continue
        selected.append((candidate, review))
        if len(selected) >= count:
            break
    return selected


def score_candidate(candidate: AdLabCandidate, review: SafetyReview) -> int:
    score = 0
    if candidate.recurring:
        score += 3
    if candidate.campaign:
        score += 3
    if len(candidate.trigger_hooks) >= 3:
        score += 2
    if review.status == "needs_web_check":
        score += 2
    if candidate.category in {"food", "home", "services"}:
        score += 1
    return score


def finalist_candidates(
    candidates: list[tuple[AdLabCandidate, SafetyReview]],
    finalist_count: int,
) -> list[tuple[AdLabCandidate, SafetyReview]]:
    return sorted(candidates, key=lambda item: (-score_candidate(*item), item[0].name))[:finalist_count]


def sample_scripts(candidate: AdLabCandidate) -> tuple[str, str]:
    hook = candidate.trigger_hooks[0] if candidate.trigger_hooks else "ad break"
    premise = candidate.campaign.premise if candidate.campaign else candidate.concept
    return (
        (
            f"ANNUNCIATORE: {candidate.name}. {candidate.tagline} "
            f"Perche quando {hook}, non serve una soluzione normale: serve {premise.lower()} "
            "OFF: Offerta valida finche il produttore non rilegge il contratto."
        ),
        (
            f"TESTIMONE: Io pensavo fosse una pessima idea. Poi ho provato {candidate.name}. "
            f"Adesso {candidate.concept[0].lower() + candidate.concept[1:]} "
            f"ANNUNCIATORE: {candidate.tagline}"
        ),
    )


def candidate_to_toml(candidate: AdLabCandidate) -> str:
    lines = [
        "[[ads.brands]]",
        f"name = {_toml_string(candidate.name)}",
        f"tagline = {_toml_string(candidate.tagline)}",
        f"category = {_toml_string(candidate.category)}",
        f"recurring = {str(candidate.recurring).lower()}",
    ]
    if candidate.campaign:
        lines.extend(
            [
                "[ads.brands.campaign]",
                f"premise = {_toml_string(candidate.campaign.premise)}",
                f"sonic_signature = {_toml_string(candidate.campaign.sonic_signature)}",
                "format_pool = ["
                + ", ".join(_toml_string(format_name) for format_name in candidate.campaign.format_pool)
                + "]",
                f"spokesperson = {_toml_string(candidate.campaign.spokesperson)}",
                f"escalation_rule = {_toml_string(candidate.campaign.escalation_rule)}",
            ]
        )
    return "\n".join(lines) + "\n"


def _candidate_markdown(candidate: AdLabCandidate, review: SafetyReview) -> str:
    scripts = sample_scripts(candidate)
    campaign = candidate.campaign
    campaign_lines = "No recurring campaign spine."
    if campaign:
        campaign_lines = "\n".join(
            [
                f"- Premise: {campaign.premise}",
                f"- Sonic signature: `{campaign.sonic_signature}`",
                f"- Formats: {', '.join(campaign.format_pool)}",
                f"- Spokesperson: `{campaign.spokesperson}`",
                f"- Escalation: {campaign.escalation_rule}",
            ]
        )
    flags = "; ".join(review.flags) if review.flags else "No heuristic flags; still requires web collision check."
    triggers = "\n".join(f"- {trigger}" for trigger in candidate.trigger_hooks)
    return f"""## {candidate.name}

- Tagline: {candidate.tagline}
- Category: `{candidate.category}`
- Recurring: `{str(candidate.recurring).lower()}`
- Safety status: `{review.status}`
- Safety notes: {flags}

{candidate.concept}

Why it fits: {candidate.why_it_fits}

Trigger hooks:
{triggers}

BFF/load perspective: {candidate.load_perspective}

Campaign:
{campaign_lines}

Sample script A:
{scripts[0]}

Sample script B:
{scripts[1]}
"""


def write_artifacts(
    candidates: list[tuple[AdLabCandidate, SafetyReview]],
    finalists: list[tuple[AdLabCandidate, SafetyReview]],
    output_dir: Path,
    *,
    config_path: Path,
    timestamp: str,
    dry_run: bool = False,
) -> dict[str, Path]:
    if dry_run:
        return {}
    output_dir.mkdir(parents=True, exist_ok=True)

    counts = Counter(review.status for _, review in candidates)
    manifest_path = output_dir / "manifest.json"
    candidates_path = output_dir / "candidates.md"
    collision_path = output_dir / "brand_collision_checks.md"
    toml_path = output_dir / "radio_toml_candidates.toml"
    triggers_path = output_dir / "trigger_ideas.md"
    recommendation_path = output_dir / "recommendation.md"

    manifest = {
        "generated_at": timestamp,
        "config": str(config_path),
        "bff_load_perspective": BFF_LOAD_PERSPECTIVE,
        "counts": dict(counts),
        "candidates": [
            {
                **asdict(candidate),
                "review": asdict(review),
                "score": score_candidate(candidate, review),
                "sample_scripts": list(sample_scripts(candidate)),
            }
            for candidate, review in candidates
        ],
        "finalists": [candidate.name for candidate, _ in finalists],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    candidates_doc = [
        "# Fictional Ad Lab Candidates",
        "",
        f"Generated: `{timestamp}`",
        "",
        BFF_LOAD_PERSPECTIVE,
        "",
        "| Brand | Category | Recurring | Status | Score |",
        "|---|---:|---:|---:|---:|",
    ]
    for candidate, review in candidates:
        candidates_doc.append(
            f"| {candidate.name} | `{candidate.category}` | `{str(candidate.recurring).lower()}` | "
            f"`{review.status}` | {score_candidate(candidate, review)} |"
        )
    candidates_doc.append("")
    candidates_doc.extend(_candidate_markdown(candidate, review) for candidate, review in candidates)
    candidates_path.write_text("\n".join(candidates_doc).rstrip() + "\n")

    collision_doc = [
        "# Brand Collision Checks",
        "",
        "Do not commit any finalist until these searches are clean. A real company, product, "
        "registered mark, or one-letter lookalike fails the brand-safety rule.",
        "",
    ]
    for candidate, review in finalists:
        collision_doc.append(f"## {candidate.name}")
        collision_doc.append("")
        collision_doc.append(f"Status: `{review.status}`")
        if review.flags:
            collision_doc.append("Flags:")
            collision_doc.extend(f"- {flag}" for flag in review.flags)
        collision_doc.append("Search:")
        collision_doc.extend(f"- {query}" for query in review.search_queries)
        collision_doc.append("")
    collision_path.write_text("\n".join(collision_doc).rstrip() + "\n")

    toml_doc = [
        "# Paste only approved, web-checked finalists into radio.toml.",
        "# The Fictional Ad Lab never edits runtime config directly.",
        "",
    ]
    toml_doc.extend(candidate_to_toml(candidate) for candidate, _ in finalists)
    toml_path.write_text("\n".join(toml_doc).rstrip() + "\n")

    trigger_doc = [
        "# Trigger Ideas",
        "",
        BFF_LOAD_PERSPECTIVE,
        "",
        "| Brand | Hooks | Load note |",
        "|---|---|---|",
    ]
    for candidate, _review in finalists:
        trigger_doc.append(
            f"| {candidate.name} | {'; '.join(candidate.trigger_hooks)} | Do not fire during queue rescue, "
            "startup warmup, or first-audio recovery. |"
        )
    triggers_path.write_text("\n".join(trigger_doc).rstrip() + "\n")

    recommendation_doc = [
        "# Recommendation",
        "",
        "Best finalists to audition first:",
        "",
    ]
    for index, (candidate, review) in enumerate(finalists, start=1):
        recommendation_doc.append(
            f"{index}. **{candidate.name}** — {candidate.why_it_fits} "
            f"Safety: `{review.status}`. Score: {score_candidate(candidate, review)}."
        )
    recommendation_doc.append("")
    recommendation_doc.append(
        "Next step: run the searches in `brand_collision_checks.md`, listen to any spec reads, "
        "then ask an agent to add only approved brands to `radio.toml`."
    )
    recommendation_path.write_text("\n".join(recommendation_doc).rstrip() + "\n")

    return {
        "manifest": manifest_path,
        "candidates": candidates_path,
        "collision_checks": collision_path,
        "toml_candidates": toml_path,
        "trigger_ideas": triggers_path,
        "recommendation": recommendation_path,
    }


async def render_edge_spec_ads(
    finalists: list[tuple[AdLabCandidate, SafetyReview]],
    output_dir: Path,
) -> list[tuple[str, str, str]]:
    """Render single-voice spec reads for quick listening.

    These MP3s are not production ads; they are taste-review sketches.
    """

    from mammamiradio.audio.tts import synthesize

    spec_dir = output_dir / "spec-ads"
    spec_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, str, str]] = []
    for index, (candidate, _review) in enumerate(finalists, start=1):
        output_path = spec_dir / f"{index:02d}-{_canonical(candidate.name).replace(' ', '-')}.mp3"
        text = sample_scripts(candidate)[0]
        try:
            await synthesize(text, "it-IT-DiegoNeural", output_path, engine="edge")
        except Exception as exc:  # best-effort taste artifact
            results.append((candidate.name, "failed", f"{type(exc).__name__}: {exc}"))
            continue
        if not output_path.exists() or output_path.stat().st_size < 2048:
            output_path.unlink(missing_ok=True)
            results.append((candidate.name, "failed", "rendered spec read was missing or too small"))
            continue
        results.append((candidate.name, "generated", str(output_path)))
    return results


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="radio.toml path to inspect")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Directory for lab artifacts")
    parser.add_argument("--count", type=_positive_int, default=25, help="Candidate count to propose")
    parser.add_argument("--finalists", type=_positive_int, default=8, help="Finalists to include in TOML draft")
    parser.add_argument("--timestamp", help="Override run timestamp in YYYYMMDDTHHMMSSZ format")
    parser.add_argument("--dry-run", action="store_true", help="Print summary without writing artifacts")
    parser.add_argument(
        "--render-edge-specs",
        action="store_true",
        help="Render one Edge TTS MP3 spec read for each finalist",
    )
    args = parser.parse_args(argv)

    if not args.config.exists():
        print(f"Config not found: {args.config}", file=sys.stderr)
        return 2
    timestamp = args.timestamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    existing_names = load_existing_brand_names(args.config)
    candidates = select_candidates(args.count, existing_names)
    finalists = finalist_candidates(candidates, min(args.finalists, len(candidates)))

    print(f"Fictional Ad Lab: {len(candidates)} candidates, {len(finalists)} finalists, output={args.output_dir}")
    for candidate, review in finalists:
        print(f"- {candidate.name}: {review.status} (score {score_candidate(candidate, review)})")

    paths = write_artifacts(
        candidates,
        finalists,
        args.output_dir,
        config_path=args.config,
        timestamp=timestamp,
        dry_run=args.dry_run,
    )

    if args.render_edge_specs and not args.dry_run:
        render_results = asyncio.run(render_edge_spec_ads(finalists, args.output_dir))
        render_report = args.output_dir / "spec_ads.md"
        lines = ["# Spec Ad Renders", ""]
        for name, status, detail in render_results:
            lines.append(f"- `{status}` {name}: {detail}")
        render_report.write_text("\n".join(lines).rstrip() + "\n")
        paths["spec_ads"] = render_report

    if paths:
        print("Artifacts:")
        for label, path in paths.items():
            print(f"- {label}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
