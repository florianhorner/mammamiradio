# mammamiradio

AI-powered Italian radio station engine. Python 3.11+, FastAPI, FFmpeg, optional Spotify and Home Assistant integration.

## Docs

- `README.md` - product overview and operator quick start
- `ARCHITECTURE.md` - runtime flow, queue model, and Spotify audio path
- `CONTRIBUTING.md` - local setup, tests, and smoke checks
- `TROUBLESHOOTING.md` - common failures and recovery paths
- `OPERATIONS.md` - runtime assumptions and deploy reality
- `CHANGELOG.md` - release notes

## Commands

- Setup: `python3.11 -m venv .venv && source .venv/bin/activate && pip install -e .`
- Install: `pip install -e .`
- Run full local stack: `./start.sh`
- Run app only: `source .venv/bin/activate && python -m uvicorn mammamiradio.main:app --reload --reload-dir mammamiradio`
- Test: `pytest tests/`
- Lint: `ruff check .` (fix: `ruff check --fix .`)
- Format: `ruff format .` (check: `ruff format --check .`)
- Type check: `mypy mammamiradio/ tests/`
- Pre-commit: `pip install pre-commit && pre-commit install`

## Environment

- `MAMMAMIRADIO_BIND_HOST`, `MAMMAMIRADIO_PORT`: bind address and port
- `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ADMIN_TOKEN`: admin auth
- `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`: Spotify Web API access
- `ANTHROPIC_API_KEY`: Claude banter/ad generation
- `HA_TOKEN`: Home Assistant API token

## Runtime behavior

- Startup loads `radio.toml`, validates config, fetches the playlist, starts go-librespot if possible, then launches producer and playback tasks.
- If Spotify credentials are missing, the app uses a built-in demo playlist.
- If go-librespot is unavailable or not authenticated, music falls back to local `music/` files, then `yt-dlp`, then placeholder tones.
- If Anthropic fails, banter and ad generation fall back to short stock copy.
- If Home Assistant is enabled and `HA_TOKEN` is present, banter and ads may reference current home state.
- `audio.bitrate` is the single source of truth for encoding, ICY headers, and playback throttling.
- Non-local binds require `ADMIN_PASSWORD` or `ADMIN_TOKEN`.

## Project structure

```text
mammamiradio/
  main.py             FastAPI app startup/shutdown lifecycle
  config.py           radio.toml + .env parsing, validation, runtime-json helper
  models.py           shared data models and station state
  producer.py         async segment production loop
  streamer.py         playback loop, routes, auth checks, public/admin status
  scheduler.py        segment scheduling and upcoming preview
  scriptwriter.py     Claude API calls for banter and ad JSON
  spotify_player.py   go-librespot integration, FIFO drain, auto-transfer
  spotify_auth.py     Spotipy OAuth setup
  playlist.py         playlist fetch, liked-songs fallback, demo fallback
  downloader.py       local file, yt-dlp, and placeholder audio fallback
  normalizer.py       FFmpeg helpers for normalize, mix, concat, and generated SFX
  tts.py              Edge TTS synthesis for hosts and ads
  ha_context.py       Home Assistant polling and Italian state formatting
  dashboard.html      dashboard HTML served at /
  listener.html       listener HTML served at /listen
radio.toml            station config
start.sh              dev entrypoint with reload-safe FIFO drain handling
tests/                pytest coverage
```

## Notes for future edits

- `dashboard.html` and `listener.html` are loaded as static file contents by `streamer.py`.
- `start.sh` is part of the runtime contract, not just a convenience script.
- `radio.toml` is the source of truth for hosts, pacing, ad brands, audio settings, and Home Assistant enablement. Secrets stay in `.env`.
- If you change routes, config keys, auth rules, or fallback behavior, update the matching docs in the same change.
