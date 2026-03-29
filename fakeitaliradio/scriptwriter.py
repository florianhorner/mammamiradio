from __future__ import annotations

import json
import logging
import random

import anthropic

from fakeitaliradio.config import StationConfig
from fakeitaliradio.models import (
    AdBrand,
    AdPart,
    AdScript,
    AdVoice,
    HostPersonality,
    StationState,
)

logger = logging.getLogger(__name__)


def _build_system_prompt(config: StationConfig) -> str:
    host_descriptions = "\n".join(
        f"- {h.name}: {h.style} (voice: {h.voice})"
        for h in config.hosts
    )
    return f"""You write scripts for a fake AI radio station called "{config.station.name}".
The station language is {config.station.language}. ALL dialogue must be in {config.station.language}.
Theme: {config.station.theme}

Hosts:
{host_descriptions}

Rules:
- Keep each line under 30 words for natural speech pacing.
- Be warm, funny, and authentic. Never break character.
- Sound like REAL Italian radio. Use natural Italian exclamations and filler words freely:
  basta, dai, ma va, figurati, mamma mia, allora, insomma, comunque, senti, guarda,
  eh niente, vabbè, cioè, tipo, no?, dico io, madonna, oddio, aspetta aspetta.
- Hosts interrupt each other, trail off, change topic mid-sentence. Real radio is messy.
- Output ONLY valid JSON, no markdown fences or extra text."""


async def write_banter(
    state: StationState, config: StationConfig
) -> list[tuple[HostPersonality, str]]:
    client = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key)

    recent = [t.display for t in state.played_tracks[-3:]]
    jokes = state.running_jokes[-3:] if state.running_jokes else []

    host_names = {h.name: h for h in config.hosts}

    # Home Assistant context — hosts may casually reference home state
    ha_block = ""
    if state.ha_context:
        ha_block = f"""
Smart home state (you may CASUALLY reference ONE of these — like glancing out a window.
Don't force it. Only mention if it fits naturally. NEVER list multiple items.):
{state.ha_context}
"""

    prompt = f"""Write a short radio banter between the hosts. 2-4 exchanges total.

Just played: {recent if recent else "opening of the show"}
Running jokes to optionally callback: {jokes if jokes else "none yet, you may seed one"}
{ha_block}
Return JSON:
{{"lines": [{{"host": "HostName", "text": "what they say"}}], "new_joke": "brief description of any new running joke or null"}}"""

    try:
        resp = await client.messages.create(
            model=config.audio.claude_model,
            max_tokens=500,
            system=_build_system_prompt(config),
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(raw)

        result = []
        for line in data["lines"]:
            host = host_names.get(line["host"], config.hosts[0])
            result.append((host, line["text"]))

        if data.get("new_joke"):
            state.add_joke(data["new_joke"])

        logger.info("Generated banter: %d lines", len(result))
        return result

    except Exception as e:
        logger.error("Banter generation failed: %s", e)
        host = random.choice(config.hosts)
        fallback = {
            "it": "E torniamo alla musica!",
            "en": "And back to the music!",
        }
        text = fallback.get(config.station.language, fallback["en"])
        return [(host, text)]


AD_BREAK_INTROS = [
    "E ora... un messaggio dai nostri sponsor!",
    "Ma prima, una pausa pubblicitaria!",
    "Restate con noi, torniamo dopo questi messaggi!",
    "E ora, le cose importanti della vita... la pubblicità!",
    "Un attimo di pausa per i nostri amici commerciali!",
    "Ecco a voi... la pubblicità! Non cambiate stazione!",
]

AD_BREAK_OUTROS = [
    "Bene, siamo tornati!",
    "Eccoci di nuovo! Vi siete persi?",
    "E torniamo alla musica, finalmente!",
    "Siamo ancora qui! Non siamo scappati!",
    "Ok, basta pubblicità. Per ora.",
    "Torniamo a noi! Dove eravamo rimasti?",
]


async def write_ad(
    brand: AdBrand,
    voice: AdVoice,
    state: StationState,
    config: StationConfig,
) -> AdScript:
    client = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key)

    # Build context for cross-referencing
    recent_ads = [
        f"- {e.brand}: {e.summary}" for e in state.ad_history[-5:]
    ] if state.ad_history else ["(nessuna pubblicità ancora)"]

    jokes = state.running_jokes[-3:] if state.running_jokes else []
    recent_tracks = [t.display for t in state.played_tracks[-3:]]

    # Find same-brand history for campaign arcs
    same_brand_ads = [
        e.summary for e in state.ad_history if e.brand == brand.name
    ][-3:]
    # Home Assistant context for ads
    ad_ha_block = ""
    if state.ha_context:
        ad_ha_block = (
            "\nSmart home state (weave ONE detail into the ad if it fits — "
            "e.g., reference the weather, what's happening at home. "
            "Make it feel like the ad knows the listener's world.):\n"
            + state.ha_context + "\n"
        )

    campaign_context = ""
    if same_brand_ads:
        campaign_context = f"""
CAMPAIGN ARC — This brand has advertised before on this station:
{chr(10).join(f'- Previous ad: {s}' for s in same_brand_ads)}
BUILD ON THIS. Reference or contradict previous claims. Create a narrative arc:
- If first follow-up: acknowledge the previous ad ("Come promesso..." / "Dopo il successo di...")
- If ongoing campaign: escalate the absurdity, add plot twists, reveal scandals about the brand
- Think GTA radio: each ad for the same brand is an episode in a saga"""

    prompt = f"""Write a fake radio ad for the fictional brand "{brand.name}".
Tagline: "{brand.tagline}"
Category: {brand.category}

The ad is read by {voice.name}, whose style is: {voice.style}

IMPORTANT: {voice.name} is NOT one of the radio hosts. This is a separate commercial voice.

Recent ads from OTHER brands that aired (you may cleverly reference or mock these):
{chr(10).join(recent_ads)}
{campaign_context}

Running jokes from the hosts: {jokes if jokes else "none"}
Recently played music: {recent_tracks if recent_tracks else "show just started"}
{ad_ha_block}

RULES:
- Absurd but delivered with COMPLETE sincerity. The product may be insane but the pitch is 100% professional.
- Think Italian TV shopping channel meets GTA radio meets Silvio Berlusconi's fever dream.
- 15-25 seconds when read aloud. Keep each voice line under 30 words.
- Structure the ad like a REAL produced commercial: open with a hook or SFX, build tension, deliver the pitch, end with a fast disclaimer or tagline.
- You may interleave sound effect cues between voice lines for a produced feel.
- Available SFX types: "chime", "sweep", "ding", "cash_register", "whoosh"
- ALL text must be in {config.station.language}.
- You may reference what the hosts said, what other ads claimed, or current music — GTA-radio style cross-pollination.
- The mood field determines background music: "dramatic" for serious/dark products, "lounge" for luxury/lifestyle, "upbeat" for exciting offers, "mysterious" for weird products, "epic" for grandiose claims.

Return JSON:
{{
  "parts": [
    {{"type": "sfx", "sfx": "chime"}},
    {{"type": "voice", "text": "Ad copy line here"}},
    {{"type": "sfx", "sfx": "sweep"}},
    {{"type": "voice", "text": "More ad copy"}},
    {{"type": "pause", "duration": 0.5}},
    {{"type": "voice", "text": "Tagline or fast disclaimer"}}
  ],
  "mood": "lounge",
  "summary": "One sentence summary of this ad for future reference"
}}"""

    try:
        resp = await client.messages.create(
            model=config.audio.claude_model,
            max_tokens=600,
            system=_build_system_prompt(config),
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(raw)

        parts = []
        for p in data.get("parts", []):
            parts.append(AdPart(
                type=p.get("type", "voice"),
                text=p.get("text", ""),
                sfx=p.get("sfx", ""),
                duration=p.get("duration", 0.0),
            ))

        # Ensure we have at least one voice part
        if not any(p.type == "voice" for p in parts):
            parts = [AdPart(type="voice", text=data.get("text", brand.tagline))]

        summary = data.get("summary", f"Ad for {brand.name}")
        mood = data.get("mood", "lounge")
        logger.info("Generated structured ad for %s: %d parts, mood=%s", brand.name, len(parts), mood)
        return AdScript(brand=brand.name, parts=parts, summary=summary, mood=mood)

    except Exception as e:
        logger.error("Ad generation failed: %s", e)
        fallback = {
            "it": f"{brand.name}. {brand.tagline or 'Perché te lo meriti.'}",
            "en": f"{brand.name}. {brand.tagline or 'Because you deserve it.'}",
        }
        text = fallback.get(config.station.language, fallback["en"])
        return AdScript(
            brand=brand.name,
            parts=[AdPart(type="voice", text=text)],
            summary=f"Fallback ad for {brand.name}",
        )
