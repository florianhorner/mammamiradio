# Operations

This repo supports three deployment models: Docker container, Home Assistant add-on, and local Python dev.

## What a real deployment needs

- Python 3.11+
- `ffmpeg` on `PATH`
- writable `tmp/` and `cache/` directories
- outbound network access for Apple Music charts API, Anthropic/OpenAI, and optional Home Assistant

Music comes from live Italian charts (via yt-dlp) when `MAMMAMIRADIO_ALLOW_YTDLP=true`, otherwise from a bundled demo playlist or local files.

## Required secrets and config

Environment:

- `MAMMAMIRADIO_BIND_HOST`
- `MAMMAMIRADIO_PORT`
- `MAMMAMIRADIO_ALLOW_YTDLP` (optional, enables live charts and yt-dlp downloads; enabled by default in HA addon and Conductor)
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD` or `ADMIN_TOKEN` for non-local access
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY` (optional, used for TTS and as script generation fallback)
- `HA_TOKEN` if Home Assistant integration is enabled

Static config:

- `radio.toml`

## Runtime outputs

- `tmp/` rendered segments and temp assets
- `cache/` downloaded track assets

## Startup model

The intended local startup path is:

```bash
./start.sh
```

That script launches uvicorn with `--reload`, `*.toml` reload support, and `LOG_LEVEL` from the environment.

## Conductor

Shared Conductor scripts live in [`conductor.json`](conductor.json):

- setup bootstraps `.venv`, installs dev tooling, and links `.env` from `~/.config/mammamiradio/.env` when present, falling back to `$CONDUCTOR_ROOT_PATH/.env`
- run exports a workspace-specific port and tmp/cache dirs before delegating to `./start.sh`, and defaults `MAMMAMIRADIO_ALLOW_YTDLP=true`
- archive deletes `.context/conductor/`

## HTTP surface

Public routes:

- `/listen`
- `/stream`
- `/healthz`
- `/readyz`
- `/public-status`

The read-only sidecar monitor in `scripts/stream_watch_server.py` is intentionally limited to `/public-status`, `/healthz`, and `/readyz` so it still works when admin auth is enabled.

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

## Recommended production shape

There is no blessed platform in this repo, but the sensible shape is:

1. Run the app behind a reverse proxy.
2. Bind the app on a private interface.
3. Require `ADMIN_PASSWORD` or `ADMIN_TOKEN`.
4. Persist `cache/`, `tmp/` where practical.
5. Monitor app logs.

## Docker

```bash
docker compose up
```

The `Dockerfile` builds a standalone image with Python 3.11 and FFmpeg. The container runs as a non-root `radio` user. `docker-compose.yml` maps `.env` variables and mounts a persistent volume at `/data` for cache and temp files.

`ADMIN_TOKEN` is required in `.env` (the container binds to `0.0.0.0`).

## Home Assistant add-on

The `ha-addon/` directory contains a complete HA add-on scaffold. Users add the repo URL in HA Settings > Add-ons > Repositories, then install "Mamma Mi Radio" from the store.

The add-on entrypoint (`ha-addon/mammamiradio/rootfs/run.sh`) maps Supervisor-injected `$SUPERVISOR_TOKEN` to `HA_TOKEN`, auto-generates an `ADMIN_TOKEN`, reads add-on options from `/data/options.json`, and starts uvicorn.

The dashboard is accessible via HA ingress (sidebar). The first-run flow exposes the same setup checks there as every other run mode, and the stream URL can be played on any HA media player.

## What is still not documented because it does not exist yet

- no systemd unit
- no launchd plist
- no nginx or Caddy config
- no Fly/Render/Vercel/Netlify config
