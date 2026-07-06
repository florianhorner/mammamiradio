# Demo Assets

Demo asset tree for the station. `sfx/studio/` and `recovery/` are committed
runtime assets; the remaining folders stay optional pending the broader
demo-asset contract decision (see the 2026-04-16 documentation audit in
`docs/`).

## Structure

- `sfx/studio/` — committed MP3 SFX used by the producer's "humanity events" (cough, paper rustle, chair creak, pen tap). These must live inside the package tree so `mammamiradio/scheduling/producer.py` and packaging find them together.
- `recovery/` — committed continuity MP3s used before any generated technical fallback when producer recovery or queue-drain recovery needs instant audio.
- `welcome/` — placeholder for onboarding clips (currently a README stub).
- `banter/`, `ads/`, `music/`, `jingles/` — not committed yet. The runtime tolerates absence: banter falls back to stock copy, ads get skipped, music falls through local files and then the recovery ladder.

## Generation

Welcome clips have a generator: `scripts/generate_welcome_clips.py` renders the fixed welcome-clip contract through the existing TTS pipeline (free Edge engine by default — no API key). Run it, listen, and commit the MP3s if they sound right.

The `banter/`, `ads/`, `music/`, and `jingles/` directories stay empty pending the demo-asset contract decision (see the 2026-04-16 documentation audit in `docs/`). If and when we ship a generator for those, it will likewise route through the existing TTS pipeline.

## Licensing

Any future music tracks must be CC-licensed or original compositions. Recovery,
banter, welcome, and ad clips generated via Edge TTS are original station copy.
