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

If those are missing, the station still runs, but it drops into demo or degraded mode and falls back to live Italian charts when `MAMMAMIRADIO_ALLOW_YTDLP=true`, then bundled demo tracks, local files, `yt-dlp`, or placeholder audio as appropriate.

## Required secrets and config

Environment:

- `MAMMAMIRADIO_BIND_HOST`
- `MAMMAMIRADIO_PORT`
- `MAMMAMIRADIO_FIFO_PATH`
- `MAMMAMIRADIO_GO_LIBRESPOT_BIN`
- `MAMMAMIRADIO_GO_LIBRESPOT_CONFIG_DIR`
- `MAMMAMIRADIO_GO_LIBRESPOT_PORT`
- `MAMMAMIRADIO_ALLOW_YTDLP` (optional, enables live charts startup fallback and downloader fallback; enabled by default in HA addon and Conductor)
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

- setup bootstraps `.venv`, installs dev tooling, and links `.env` from `~/.config/mammamiradio/.env` when present, falling back to `$CONDUCTOR_ROOT_PATH/.env`
- run exports a workspace-specific port, FIFO path, tmp/cache dirs, and go-librespot config dir before delegating to `./start.sh`, and defaults `MAMMAMIRADIO_ALLOW_YTDLP=true`
- archive kills the workspace-owned go-librespot and FIFO drain processes, removes the FIFO, and deletes `.context/conductor/`

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

### Spotify Connect in the add-on

go-librespot is bundled in the add-on image and starts automatically. It uses mDNS/zeroconf to advertise a Spotify Connect device on the local network. However, **mDNS from inside the HA add-on container may not work** even with `host_network: true`, because:

- HA OS manages its own mDNS/Avahi daemon on the host. Container mDNS broadcasts may be filtered or not reflected to the LAN.
- The HA supervisor network stack may not forward multicast UDP (port 5353) from containers.

**yt-dlp is the primary and reliable audio source for the add-on.** It downloads tracks from YouTube and works in any container environment. Spotify Connect is best-effort: if it works on your network, great. If the device doesn't appear in Spotify, that's expected in some HA installations.

### Cache poisoning recovery

If the add-on previously ran without `MAMMAMIRADIO_ALLOW_YTDLP=true` (versions before 2.2.3), the cache directory (`/data/cache/`) may contain silence placeholders instead of real audio. Starting with v2.2.3, the app purges these automatically on startup. If silence persists, manually clear the cache: stop the add-on, delete `/data/cache/*.mp3` via SSH, and restart.

## What is still not documented because it does not exist yet

- no systemd unit
- no launchd plist
- no nginx or Caddy config
- no Fly/Render/Vercel/Netlify config
