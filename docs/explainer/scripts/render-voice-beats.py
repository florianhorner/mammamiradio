#!/usr/bin/env python3
"""Render the explainer's voice beats in the station's own configured voices.

An operator command, not CI: each run spends ElevenLabs characters. It reads
the beats from scenarios.mjs (the single source of truth — the dialogue is
never duplicated here), resolves Marco and Giulia from radio.toml exactly the
way the First Listen guide renderer does, and writes the files where
produce-segments.mjs expects them:

    docs/explainer/tmp/produce/voice/<scenarioId>-<beatIndex>.mp3

Run from the repo root so the .env symlink (-> ~/.config/mammamiradio/.env)
loads itself when core.config imports:

    ./.venv/bin/python docs/explainer/scripts/render-voice-beats.py [--force]

Multi-line beats render each line without loudnorm and concat with one final
loudness pass (the First Listen recipe), so a two-host exchange gets a single
consistent level instead of per-line pumping. Existing outputs are skipped
unless --force, so a re-run after adding one scenario costs one scenario.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

EXPLAINER_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = EXPLAINER_ROOT.parent.parent
sys.path.insert(0, str(REPO_ROOT))

VOICE_DIR = EXPLAINER_ROOT / "tmp" / "produce" / "voice"


def load_scenarios() -> dict:
    """Read scenarios.mjs through Node so the SSOT stays the only copy."""
    dump = subprocess.run(
        ["node", "-e", "import('./scenarios.mjs').then(m => console.log(JSON.stringify(m.default)))"],
        cwd=EXPLAINER_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(dump.stdout)


async def render(force: bool) -> None:
    os.chdir(REPO_ROOT)  # load_dotenv() in core.config resolves .env from CWD
    from mammamiradio.audio.normalizer import concat_files
    from mammamiradio.audio.tts import synthesize_elevenlabs
    from mammamiradio.core.config import load_config

    if not os.getenv("ELEVENLABS_API_KEY"):
        raise SystemExit("ELEVENLABS_API_KEY is not set — refusing to start a paid render half-configured")

    config = load_config("radio.toml")
    hosts = {host.name.lower(): host for host in config.hosts}
    for name in ("marco", "giulia"):
        host = hosts.get(name)
        if host is None or host.engine != "elevenlabs":
            raise SystemExit(f"{name} is not configured with a canonical ElevenLabs voice in radio.toml")

    async def render_line(who: str, text: str, out_path: Path, *, loudnorm: bool) -> Path:
        # Case-normalized to match the pre-flight below, which is what makes
        # this lookup safe rather than hopeful.
        host = hosts[who.strip().lower()]
        return await synthesize_elevenlabs(
            text,
            host.voice,
            out_path,
            loudnorm=loudnorm,
            voice_settings=host.voice_settings,
            elevenlabs_model=host.elevenlabs_model,
            delivery_profile=host.delivery_profile,
            host_name=host.name,
        )

    VOICE_DIR.mkdir(parents=True, exist_ok=True)

    # Every speaker in every beat, resolved before the first paid request. The
    # old pre-flight only proved marco and giulia exist, so an unknown or
    # differently-capitalized name failed mid-render with a KeyError and the
    # characters already spent were not refundable.
    unresolved: list[str] = []
    for scenario_id, scenario in load_scenarios().items():
        for index, beat in enumerate(scenario["beats"]):
            if beat.get("kind") != "voice":
                continue
            for line in beat.get("lines") or []:
                who = str(line.get("who", "")).strip().lower()
                if who not in hosts:
                    unresolved.append(f"{scenario_id} beat {index}: {line.get('who')!r}")
    if unresolved:
        raise SystemExit(
            "these beats name a speaker that is not a configured host: "
            + "; ".join(unresolved)
            + f" — configured hosts are {', '.join(sorted(hosts))}. "
            "Fix the name in scenarios.mjs, then run again."
        )

    spent_chars = 0
    for scenario_id, scenario in load_scenarios().items():
        for index, beat in enumerate(scenario["beats"]):
            if beat.get("kind") != "voice":
                continue
            lines = beat.get("lines") or []
            if not lines:
                raise SystemExit(
                    f"{scenario_id} beat {index} is a voice beat with no lines — "
                    "write the dialogue in scenarios.mjs first"
                )
            out_path = VOICE_DIR / f"{scenario_id}-{index}.mp3"
            if out_path.exists() and not force:
                print(f"skip {out_path.name} (exists — use --force to re-render)")
                continue
            if len(lines) == 1:
                await render_line(lines[0]["who"], lines[0]["text"], out_path, loudnorm=True)
            else:
                part_paths = [VOICE_DIR / f".{scenario_id}-{index}-part{n}.mp3" for n in range(len(lines))]
                await asyncio.gather(
                    *(
                        render_line(line["who"], line["text"], part, loudnorm=False)
                        for line, part in zip(lines, part_paths, strict=True)
                    )
                )
                concat_files(part_paths, out_path, silence_ms=280, loudnorm=True, strict_duration=True)
                for part in part_paths:
                    part.unlink(missing_ok=True)
            beat_chars = sum(len(line["text"]) for line in lines)
            spent_chars += beat_chars
            print(
                f"rendered {out_path.name}: {len(lines)} line(s), {beat_chars} chars, {out_path.stat().st_size} bytes"
            )
    print(f"\nDone. ~{spent_chars} paid characters this run.")


if __name__ == "__main__":
    asyncio.run(render(force="--force" in sys.argv))
