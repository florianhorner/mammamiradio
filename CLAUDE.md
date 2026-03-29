# fakeitaliradio

Fake Italian radio station engine. Python 3.11+, FastAPI.

## Docs

- `README.md` - product overview and operator quick start
- `ARCHITECTURE.md` - runtime flow, queue model, and Spotify audio path
- `CONTRIBUTING.md` - local setup, tests, and smoke checks
- `CHANGELOG.md` - release notes, starting from 0.1.0

## Commands

- Setup: `python3.11 -m venv .venv && source .venv/bin/activate && pip install -e .`
- Run: `./start.sh` (starts go-librespot + uvicorn with hot reload on :8000)
- Run without go-librespot: `source .venv/bin/activate && python -m uvicorn fakeitaliradio.main:app --reload --reload-dir fakeitaliradio`
- Test: `pytest tests/`
- Install: `pip install -e .`

## Project structure

```
fakeitaliradio/
  main.py           — FastAPI app, startup/shutdown lifecycle, spawns producer
  config.py         — Loads radio.toml + .env, validates config
  models.py         — Shared dataclasses: Track, Segment, StationState, HostPersonality, AdBrand
  producer.py       — Async producer loop: generates music/banter/ad segments, pushes to queue
  streamer.py       — FastAPI router: /stream, /status, /api/*, dashboard + listener HTML
  scheduler.py      — Decides next segment type based on pacing counters
  scriptwriter.py   — Claude API calls to generate banter dialogue and ad scripts
  spotify_player.py — go-librespot integration: FIFO drain, track capture, Spotify transfer
  spotify_auth.py   — Spotipy OAuth setup
  playlist.py       — Fetches playlist from Spotify (or demo tracks fallback)
  downloader.py     — Track download: Spotify FIFO → yt-dlp → local files → placeholder
  normalizer.py     — FFmpeg wrappers: normalize, concat, generate SFX/beds, mix audio
  tts.py            — Edge TTS synthesis for dialogue and ads
  dashboard.html    — Control plane UI (served at /)
  listener.html     — Minimal listener UI (served at /listen)
radio.toml          — Station config (hosts, brands, pacing, audio settings)
start.sh            — Dev startup script (FIFO + go-librespot + uvicorn)
go-librespot/       — go-librespot config
tests/              — pytest tests (config, scheduler, ads)
```

## Architecture

- **Producer/consumer queue**: producer.py fills an asyncio.Queue with Segment objects, streamer.py drains it at playback bitrate
- **Segment types**: MUSIC, BANTER, AD. Scheduler picks the next type based on counters in StationState
- **Audio pipeline**: all audio passes through normalizer.py (FFmpeg) for loudness normalization (-16 LUFS)
- **Config**: radio.toml (parsed by config.py) + .env (Spotify/Anthropic credentials)
- **Spotify**: go-librespot writes PCM to a FIFO, SpotifyPlayer has a persistent drain thread to prevent ENXIO
- **Error recovery**: failed segments insert silence and don't advance pacing counters
