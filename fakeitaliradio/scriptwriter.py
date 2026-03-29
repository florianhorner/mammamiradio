from __future__ import annotations

import json
import logging
import random

import anthropic

from fakeitaliradio.config import StationConfig
from fakeitaliradio.models import HostPersonality, StationState

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
    brand: str, config: StationConfig
) -> tuple[HostPersonality, str]:
    client = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key)

    host = random.choice(config.hosts)
    prompt = f"""Write a short, entertaining fake radio ad read by {host.name} for the fictional brand "{brand}".
15-20 seconds when read aloud. Funny, slightly absurd, totally fictional.

Return JSON:
{{"text": "the ad copy"}}"""

    try:
        resp = await client.messages.create(
            model=config.audio.claude_model,
            max_tokens=300,
            system=_build_system_prompt(config),
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        logger.info("Generated ad for %s", brand)
        return (host, data["text"])

    except Exception as e:
        logger.error("Ad generation failed: %s", e)
        fallback = {
            "it": f"{brand}. Perché te lo meriti.",
            "en": f"{brand}. Because you deserve it.",
        }
        text = fallback.get(config.station.language, fallback["en"])
        return (host, text)
