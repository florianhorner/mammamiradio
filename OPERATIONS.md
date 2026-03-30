# Operations

This repo supports three deployment models: Docker container, Home Assistant add-on, and local Python dev.

## What a real deployment needs

- Python 3.11+
- `ffmpeg` on `PATH`
- `go-librespot` binary on `PATH` (or at the path configured in `radio.toml`)
- writable `tmp/` and `cache/` directories
- persistent access to `/tmp/mammamiradio.pcm`
- outbound network access for Spotify, Anthropic, and optional Home Assistant

## Required secrets and config

Environment:

- `MAMMAMIRADIO_BIND_HOST`
- `MAMMAMIRADIO_PORT`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD` or `ADMIN_TOKEN` for non-local access
- `SPOTIFY_CLIENT_ID`
- `SPOTIFY_CLIENT_SECRET`
- `ANTHROPIC_API_KEY`
- `HA_TOKEN` if Home Assistant integration is enabled

Static config:

- `radio.toml`
- `go-librespot/config.yml`

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
- starts go-librespot if not already running
- keeps a fallback FIFO drain alive across reloads
- launches uvicorn

If you replace it in production, your replacement needs to preserve the FIFO + go-librespot behavior or you will reintroduce the macOS-style skip problems this repo already worked around.

## HTTP surface

Public routes:

- `/listen`
- `/stream`
- `/public-status`

Admin routes:

- `/`
- `/status`
- `/api/logs`
- `/api/shuffle`
- `/api/skip`
- `/api/purge`
- `/api/playlist/remove`
- `/api/playlist/move`
- `/api/playlist/move_to_next`
- `/api/search`
- `/api/playlist/add`
- `/api/playlist/load`

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

The add-on entrypoint (`ha-addon/mammamiradio/rootfs/run.sh`) maps Supervisor-injected `$SUPERVISOR_TOKEN` to `HA_TOKEN`, auto-generates an `ADMIN_TOKEN`, reads add-on options from `/data/options.json`, and starts uvicorn.

The dashboard is accessible via HA ingress (sidebar). The stream URL can be played on any HA media player.

## What is still not documented because it does not exist yet

- no systemd unit
- no launchd plist
- no nginx or Caddy config
- no Fly/Render/Vercel/Netlify config
