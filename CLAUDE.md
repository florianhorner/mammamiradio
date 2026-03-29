# fakeitaliradio

Fake Italian radio station engine. Python 3.11+, FastAPI, FFmpeg, optional Spotify and Home Assistant integration.

## Commands

- Install: `pip install -e .`
- Run full local stack: `./start.sh`
- Run app only: `uvicorn fakeitaliradio.main:app --reload --reload-dir fakeitaliradio`
- Test: `pytest tests/`

## Environment

- `FAKEITALIRADIO_BIND_HOST`, `FAKEITALIRADIO_PORT`: bind address and port
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
- Non-local binds require `ADMIN_PASSWORD` or `ADMIN_TOKEN`.

## Project structure

```text
fakeitaliradio/
  main.py             FastAPI app, startup/shutdown lifecycle
  config.py           Loads radio.toml + .env, validates configuration
  models.py           Track, Segment, StationState, host/ad models
  producer.py         Async segment production loop
  streamer.py         Playback loop, routes, auth checks, public/admin status
  scheduler.py        Segment scheduling and upcoming preview
  scriptwriter.py     Claude API calls for banter and ad JSON
  spotify_player.py   go-librespot integration, FIFO drain, auto-transfer
  spotify_auth.py     Spotipy OAuth setup
  playlist.py         Playlist fetch, liked-songs fallback, demo fallback
  downloader.py       Local file, yt-dlp, and placeholder audio fallback
  normalizer.py       FFmpeg helpers for normalize/mix/concat/SFX
  tts.py              Edge TTS synthesis for hosts and ads
  ha_context.py       Home Assistant polling and Italian state formatting
  dashboard.html      Dashboard HTML served at /
  listener.html       Listener HTML served at /listen
radio.toml            Station config
start.sh              Dev entrypoint with go-librespot + reload-safe FIFO drain
go-librespot/         go-librespot config directory
tests/                pytest coverage for config, scheduler, ads, preview, models
```

## Interfaces

- Public: `/listen`, `/stream`, `/public-status`
- Admin: `/`, `/status`, `/api/logs`, `/api/shuffle`, `/api/skip`, `/api/purge`, `/api/playlist/*`

## Notes for future edits

- `dashboard.html` and `listener.html` are served as static file contents loaded by `streamer.py`.
- `start.sh` exists because hot reload and go-librespot do not cooperate cleanly without an external FIFO drain.
- `radio.toml` is the source of truth for hosts, pacing, ad brands, and Home Assistant enablement. Secrets stay in `.env`.
- The repo currently has dedicated docs in `README.md`, `ARCHITECTURE.md`, `CONTRIBUTING.md`, `TROUBLESHOOTING.md`, `OPERATIONS.md`, and `CHANGELOG.md`. Keep them in sync when routes, config, or runtime behavior changes.
