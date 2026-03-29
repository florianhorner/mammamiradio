from __future__ import annotations

import json
import logging
import random

import anthropic

from fakeitaliradio.config import StationConfig
from fakeitaliradio.models import (
    AdBrand, AdPart, AdScript, AdVoice,
    HostPersonality, StationState,
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
- Output ONLY valid JSON, no markdown fences or extra text."""


async def write_banter(
    state: StationState, config: StationConfig
) -> list[tuple[HostPersonality, str]]:
    client = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key)

    recent = [t.display for t in state.played_tracks[-3:]]
    jokes = state.running_jokes[-3:] if state.running_jokes else []

    host_names = {h.name: h for h in config.hosts}

    prompt = f"""Write a short radio banter between the hosts. 2-4 exchanges total.

Just played: {recent if recent else "opening of the show"}
Running jokes to optionally callback: {jokes if jokes else "none yet, you may seed one"}

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

    prompt = f"""Write a fake radio ad for the fictional brand "{brand.name}".
Tagline: "{brand.tagline}"
Category: {brand.category}

The ad is read by {voice.name}, whose style is: {voice.style}

IMPORTANT: {voice.name} is NOT one of the radio hosts. This is a separate commercial voice.

Recent ads that aired (you may cleverly reference these but NEVER repeat them):
{chr(10).join(recent_ads)}

Running jokes from the hosts: {jokes if jokes else "none"}
Recently played music: {recent_tracks if recent_tracks else "show just started"}

RULES:
- Absurd but delivered with complete sincerity. The product may be insane but the pitch is professional.
- 15-25 seconds when read aloud. Keep each voice line under 30 words.
- You may interleave sound effect cues between voice lines for a produced feel.
- Available SFX types: "chime", "sweep", "ding", "cash_register", "whoosh"
- ALL text must be in {config.station.language}.
- You may reference what the hosts said or what previous ads claimed, GTA-radio style.

Return JSON:
{{
  "parts": [
    {{"type": "sfx", "sfx": "chime"}},
    {{"type": "voice", "text": "Ad copy line here"}},
    {{"type": "sfx", "sfx": "sweep"}},
    {{"type": "voice", "text": "More ad copy"}},
    {{"type": "pause", "duration": 0.5}},
    {{"type": "voice", "text": "Tagline or disclaimer"}}
  ],
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
        logger.info("Generated structured ad for %s: %d parts", brand.name, len(parts))
        return AdScript(brand=brand.name, parts=parts, summary=summary)

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
