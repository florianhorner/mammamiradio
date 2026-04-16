# Demo Assets

Demo asset tree for the station. Only `sfx/studio/` is currently populated — the rest is a TODO pending the demo-asset contract decision (see the 2026-04-16 documentation audit in `docs/`).

## Structure

- `sfx/studio/` — committed MP3 SFX used by the producer's "humanity events" (cough, paper rustle, chair creak, pen tap). These must live inside the package tree so `mammamiradio/producer.py` and packaging find them together.
- `welcome/` — placeholder for onboarding clips (currently a README stub).
- `banter/`, `ads/`, `music/`, `jingles/` — not committed yet. The runtime tolerates absence: banter falls back to stock copy, ads get skipped, music falls through local files and then silence.

## Generation

There is no `generate_demo_assets` module yet. If and when we ship one, it will regenerate banter/ad clips via the existing TTS pipeline. Until then these directories stay empty.

## Licensing

Any future music tracks must be CC-licensed or original compositions. Banter and ad clips generated via Edge TTS would be original content.
