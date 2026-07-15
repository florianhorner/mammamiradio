# Demo Assets

Demo asset tree for the station. `sfx/studio/` and `recovery/` are committed
runtime assets; the remaining folders stay optional pending the broader
demo-asset contract decision (see the 2026-04-16 documentation audit in
`docs/`).

## Structure

- `sfx/studio/` — committed MP3 SFX used by the producer's "humanity events" (cough, paper rustle, chair creak, pen tap). These must live inside the package tree so `mammamiradio/scheduling/producer.py` and packaging find them together.
- `recovery/` — committed, package-included continuity MP3s used before any
  generated technical fallback when producer recovery or queue-drain recovery
  needs instant audio. `continuity_1.mp3` is the normal immediate clip;
  `emergency_tone.mp3` is the required 2-second cold-cache/no-clip final rung.
  Keep both under this package tree so they are available without rendering in
  standalone and Home Assistant add-on builds.
- `spoken_assets.json` — reviewed transcript, language, role, and SHA-256 for
  every MP3 inventoried under recovery/banter/welcome. Missing, changed,
  unlisted, or listener-unsafe speech fails closed. Runtime may admit approved
  recovery and neutral banter speech; welcome discovery stays disabled.
- `welcome/` — historical generator documentation only. The runtime no longer
  discovers welcome or unmanifested banter clips from directory contents.
- `banter/`, `ads/`, `music/`, `jingles/` — not committed yet. The runtime tolerates absence: banter falls back to stock copy, ads get skipped, music falls through local files and then the recovery ladder. Any future packaged banter must also be declared in `spoken_assets.json` before runtime can use it.

## Generation

The historical welcome-clip generator now emits neutral station-continuity
lines for local review only. Its output is not runtime-discoverable.

The `banter/`, `ads/`, `music/`, and `jingles/` directories stay empty pending the demo-asset contract decision (see the 2026-04-16 documentation audit in `docs/`). If and when we ship a generator for those, it will likewise route through the existing TTS pipeline.

## Licensing

Any future music tracks must be CC-licensed or original compositions. Recovery,
banter, welcome, and ad clips generated via Edge TTS are original station copy.
