# mammamiradio

AI-powered Italian radio station engine. Python 3.11+, FastAPI, FFmpeg, optional Spotify and Home Assistant integration.

## Docs

- `README.md` - product overview and operator quick start
- `ARCHITECTURE.md` - runtime flow, queue model, and Spotify audio path
- `CONTRIBUTING.md` - local setup, tests, and smoke checks
- `TROUBLESHOOTING.md` - common failures and recovery paths
- `HA_ADDON_RUNBOOK.md` - addon release process, config contract, pre-merge checklist
- `OPERATIONS.md` - runtime assumptions and deploy reality
- `CHANGELOG.md` - release notes

## Commands

- Setup: `python3.11 -m venv .venv && source .venv/bin/activate && pip install -e .`
- Install: `pip install -e .`
- Run full local stack: `./start.sh`
- Run app only: `source .venv/bin/activate && python -m uvicorn mammamiradio.main:app --reload --reload-dir mammamiradio`
- Test: `pytest tests/` or `make test` (with coverage)
- Test watch: `make test-watch` (re-runs on file save)
- Test HA add-on build locally: `scripts/test-addon-local.sh`
- Lint: `ruff check .` (fix: `ruff check --fix .`)
- Format: `ruff format .` (check: `ruff format --check .`)
- Type check: `mypy mammamiradio/ tests/`
- All checks: `make check` (lint + typecheck + test)
- Pre-commit: `pip install pre-commit && pre-commit install --hook-type pre-commit --hook-type pre-push --hook-type commit-msg`
- **Validate addon before push**: `./scripts/validate-addon.sh` (add `--build` for Docker build test)

## Docker / Home Assistant

- `Dockerfile`: standalone container image with Python 3.11 + FFmpeg
- `docker-compose.yml`: one-command run for non-HA users
- `.dockerignore`: keeps builds clean
- `ha-addon/`: Home Assistant add-on scaffold
  - `ha-addon/mammamiradio/config.yaml`: add-on metadata, options schema, ingress config
  - `ha-addon/mammamiradio/Dockerfile`: HA add-on image (Alpine-based)
  - `ha-addon/mammamiradio/rootfs/run.sh`: entrypoint mapping Supervisor env vars
  - `ha-addon/mammamiradio/translations/en.yaml`: UI labels for add-on options
- `.github/workflows/docker.yml`: multi-arch Docker build CI

## Environment

- `MAMMAMIRADIO_BIND_HOST`, `MAMMAMIRADIO_PORT`: bind address and port
- `MAMMAMIRADIO_CACHE_DIR`, `MAMMAMIRADIO_TMP_DIR`: override cache/tmp directories (for Docker volumes)
- `LOG_LEVEL`: override log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`; default `INFO`)
- `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ADMIN_TOKEN`: admin auth
- `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`: Spotify Web API access
- `ANTHROPIC_API_KEY`: Claude banter/ad generation
- `HA_TOKEN`: Home Assistant API token
- `HA_URL`: Home Assistant API base URL (auto-set by HA add-on to `http://supervisor/core/api`)
- `HA_ENABLED`: force-enable HA integration (`true`/`1`/`yes`)
- `STATION_NAME`, `STATION_THEME`: override station identity from `radio.toml`
- `PLAYLIST_SPOTIFY_URL`: override playlist URL from `radio.toml`
- `CLAUDE_MODEL`: override Claude model from `radio.toml`
- `MAMMAMIRADIO_FIFO_PATH`: override go-librespot FIFO path
- `MAMMAMIRADIO_GO_LIBRESPOT_BIN`: override go-librespot binary path
- `MAMMAMIRADIO_GO_LIBRESPOT_CONFIG_DIR`: override go-librespot config directory
- `MAMMAMIRADIO_GO_LIBRESPOT_PORT`: override go-librespot API port (default `3678`)
- `MAMMAMIRADIO_ALLOW_YTDLP`: enable yt-dlp fallback for demo tracks (`true`/`1`/`yes`; default: disabled for copyright safety)

## Runtime behavior

- Startup loads `radio.toml`, validates config, starts go-librespot if possible, restores persisted source selection from `cache/playlist_source.json`, fetches the playlist, then launches producer and playback tasks.
- **Capability flags** (`spotify_connected`, `spotify_api`, `anthropic`, `ha`) replace the old 64-state mode system. Each flag is independent. The dashboard derives a tier label from them: Demo Radio, Your Music, Full AI Radio.
- Demo-first: if no Spotify credentials exist, the app boots immediately with built-in demo tracks and pre-bundled banter clips. No wizard, no gates.
- **Spotify Connect (zeroconf):** go-librespot advertises via mDNS. Users tap "MammaMiRadio" in their Spotify app to connect. This handles streaming auth without any Client ID/secret. Playlist browsing still requires Client ID/secret (Web API scopes not available via zeroconf).
- If Anthropic key is missing, banter uses pre-bundled clips from `demo_assets/banter/` instead of calling Claude.
- If go-librespot is unavailable or not authenticated, music falls back to local `music/` files, then `yt-dlp`, then placeholder tones.
- If Anthropic fails mid-session, banter and ad generation fall back to short stock copy.
- If Home Assistant is enabled and `HA_TOKEN` is present, banter and ads may reference current home state.
- `audio.bitrate` is the single source of truth for encoding, ICY headers, and playback throttling.
- Source switching via `/api/spotify/source/select` or `/api/playlist/load` purges the queue, skips the current segment, and begins playback from the new source immediately.
- The source picker (playlist/liked_songs selection) is only available in local/macOS mode; addon/Docker modes are restricted to URL loading.
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
  capabilities.py     Capability flags (spotify_connected, spotify_api, anthropic, ha) and tier derivation
  setup_status.py     Legacy setup status classification (kept for /status endpoint compat)
  dashboard.html      Capability-flag-driven dashboard served at /
  listener.html       Listener HTML served at /listen
  demo_assets/        Pre-bundled banter clips, ads, music, and jingles for demo-first boot
radio.toml            station config
start.sh              dev entrypoint with reload-safe FIFO drain handling
tests/                pytest coverage
```

## Brand assets

- **Logo SVG**: `mammamiradio/logo.svg` — canonical vector source (variant G: classic radio with Italian flag stripe and sound waves)
- **Palette**: Volare — warm Italian sunset. See `DESIGN.md` for the full design system.
  - Background: orange-red sunset gradient (`#C44020 → #D45228 → #E07038`)
  - Cards: deep sienna (`#823218`, `#924020`) — buildings in shadow
  - Accent: golden sun (`#F4D048`, `#ECCC30`) — play button, active borders
  - Interactive: Lancia red (`#B82C20`) — FM dial needle, connect border
  - Text: cream (`#F5EDD8`)
  - Success/connected: blue (`#2563EB`) — never green (colorblind)
- **Typography**: Playfair Display italic (station name, display text) + Inter (body)
- **Favicon**: inline SVG data URI in `dashboard.html` and `listener.html` (simplified version of logo)
- **HA add-on icon**: `ha-addon/mammamiradio/icon.png` (256px) and `logo.png` (512px), rasterized from the SVG
- To regenerate PNGs from SVG: `cairosvg mammamiradio/logo.svg -o icon.png -W 256 -H 256`
- **Full design system**: `DESIGN.md` — colors, typography, components, motion, anti-patterns

## Notes for future edits

- `dashboard.html` and `listener.html` are loaded as static file contents by `streamer.py`.
- `start.sh` is part of the runtime contract, not just a convenience script.
- `radio.toml` is the source of truth for hosts, pacing, ad brands, audio settings, and Home Assistant enablement. Secrets stay in `.env`.
- If you change routes, config keys, auth rules, or fallback behavior, update the matching docs in the same change.
- `conductor.json` and `scripts/conductor-*.sh` define Conductor workspace setup/run/archive behavior. Commit those files, but keep `.context/` runtime state out of git.

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
