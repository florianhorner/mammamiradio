# Operations

This repo supports three deployment models: Docker container, Home Assistant add-on, and local Python dev.

## What a real deployment needs

- Python 3.11+
- `ffmpeg` on `PATH`
- writable `tmp/` and `cache/` directories
- outbound network access for Spotify, Anthropic, and optional Home Assistant

Spotify Connect capture adds a couple more requirements:

- `go-librespot` on `PATH` or at the path configured in `radio.toml`
- persistent access to the configured FIFO path, usually `/tmp/mammamiradio.pcm`

If those are missing, the station still runs, but it drops into demo or degraded mode and falls back to local files, `yt-dlp`, or placeholder audio.

## Required secrets and config

Environment:

- `MAMMAMIRADIO_BIND_HOST`
- `MAMMAMIRADIO_PORT`
- `MAMMAMIRADIO_FIFO_PATH`
- `MAMMAMIRADIO_GO_LIBRESPOT_BIN`
- `MAMMAMIRADIO_GO_LIBRESPOT_CONFIG_DIR`
- `MAMMAMIRADIO_GO_LIBRESPOT_PORT`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD` or `ADMIN_TOKEN` for non-local access
- `SPOTIFY_CLIENT_ID`
- `SPOTIFY_CLIENT_SECRET`
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY` (optional, used for TTS and as script generation fallback)
- `MAMMAMIRADIO_SPOTIFY_REDIRECT_BASE_URL` (optional, for stable HTTPS OAuth callbacks)
- `HA_TOKEN` if Home Assistant integration is enabled

Static config:

- `radio.toml`
- local dev: `go-librespot/config.yml`
- Home Assistant add-on only: `/data/go-librespot/config.yml` inside the add-on container

## Runtime outputs

- `tmp/go-librespot.log`
- `tmp/` rendered segments and temp assets
- `cache/` downloaded track assets
- `.spotify_token_cache` OAuth token cache

## Startup model

The intended local startup path is:

```bash
./start.sh
```

That script matters because it:

- creates the FIFO if needed
- starts or reuses the owned go-librespot process when possible
- keeps a fallback FIFO drain alive across reloads
- launches uvicorn with `--reload`, `*.toml` reload support, and `LOG_LEVEL` from the environment

If you need to verify which go-librespot config directory the app resolved for the current environment, run:

```bash
.venv/bin/python -m mammamiradio.config runtime-json
```

Local dev should report `go-librespot`. Home Assistant add-on mode reports `/data/go-librespot`.

If you replace it in production, your replacement needs to preserve the FIFO + go-librespot behavior or you will reintroduce the macOS-style skip problems this repo already worked around.

## Conductor

Shared Conductor scripts live in [`conductor.json`](conductor.json):

- setup bootstraps `.venv`, installs dev tooling, and links `.env` from `$CONDUCTOR_ROOT_PATH` when present
- run exports a workspace-specific port, FIFO path, tmp/cache dirs, and go-librespot config dir before delegating to `./start.sh`
- archive kills the workspace-owned go-librespot and FIFO drain processes, removes the FIFO, and deletes `.context/conductor/`

## HTTP surface

Public routes:

- `/listen`
- `/stream`
- `/healthz`
- `/readyz`
- `/public-status`

Admin routes:

- `/`
- `/status`
- `/api/logs`
- `/api/setup/status`
- `/api/setup/recheck`
- `/api/setup/addon-snippet`
- `/api/shuffle`
- `/api/skip`
- `/api/purge`
- `/api/playlist/remove`
- `/api/playlist/move`
- `/api/playlist/move_to_next`
- `/api/search`
- `/api/playlist/add`
- `/api/playlist/load`
- `/api/capabilities`
- `/api/credentials`
- `/api/trigger`
- `/api/stop`
- `/api/resume`
- `/api/spotify/source-options`
- `/api/spotify/source/select`

## Recommended production shape

There is no blessed platform in this repo, but the sensible shape is:

1. Run the app behind a reverse proxy.
2. Bind the app on a private interface.
3. Require `ADMIN_PASSWORD` or `ADMIN_TOKEN`.
4. Persist `cache/`, `tmp/` where practical, and `.spotify_token_cache`.
5. Monitor `tmp/go-librespot.log` and app logs.

## Docker

```bash
docker compose up
```

The `Dockerfile` builds a standalone image with Python 3.11 and FFmpeg. The container runs as a non-root `radio` user. `docker-compose.yml` maps `.env` variables and mounts a persistent volume at `/data` for cache and temp files.

`ADMIN_TOKEN` is required in `.env` (the container binds to `0.0.0.0`).

## Home Assistant add-on

The `ha-addon/` directory contains a complete HA add-on scaffold. Users add the repo URL in HA Settings > Add-ons > Repositories, then install "Mamma Mi Radio" from the store.

The add-on entrypoint (`ha-addon/mammamiradio/rootfs/run.sh`) maps Supervisor-injected `$SUPERVISOR_TOKEN` to `HA_TOKEN`, auto-generates an `ADMIN_TOKEN`, reads add-on options from `/data/options.json`, syncs `/data/go-librespot/config.yml` from the shipped default, and starts uvicorn.

The dashboard is accessible via HA ingress (sidebar). The first-run flow exposes the same setup checks there as every other run mode, and the stream URL can be played on any HA media player.

## What is still not documented because it does not exist yet

- no systemd unit
- no launchd plist
- no nginx or Caddy config
- no Fly/Render/Vercel/Netlify config
