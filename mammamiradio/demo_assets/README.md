# Demo Assets

Pre-bundled audio clips that ship with every install. These provide station
personality on day 1 without any API keys.

## Structure

- `banter/` — Italian-accented host banter clips (~30-60s each)
- `ads/` — Fake ad breaks for imaginary Italian products
- `music/` — CC-licensed Italian-style music tracks for demo mode
- `jingles/` — Station bumpers and jingles

## Generation

Generate banter and ad clips using the existing TTS pipeline:

```bash
python -m mammamiradio.generate_demo_assets
```

## Licensing

Music tracks must be CC-licensed or original compositions.
Banter and ad clips are generated via Edge TTS and are original content.
