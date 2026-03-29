# Operations

This repo is local-first. There is no checked-in deploy target, container config, or platform-specific service definition.

That is the honest current state.

## What a real deployment needs

- Python 3.11+
- `ffmpeg` on `PATH`
- optional `go-librespot` binary at the path configured in `radio.toml`
- writable `tmp/` and `cache/` directories
- persistent access to `/tmp/fakeitaliradio.pcm`
- outbound network access for Spotify, Anthropic, and optional Home Assistant

## Required secrets and config

Environment:

- `FAKEITALIRADIO_BIND_HOST`
- `FAKEITALIRADIO_PORT`
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

## Recommended production shape

There is no blessed platform in this repo, but the sensible shape is:

1. Run the app behind a reverse proxy.
2. Bind the app on a private interface.
3. Require `ADMIN_PASSWORD` or `ADMIN_TOKEN`.
4. Persist `cache/`, `tmp/` where practical, and `.spotify_token_cache`.
5. Monitor `tmp/go-librespot.log` and app logs.

## What is still not documented because it does not exist yet

- no Dockerfile
- no systemd unit
- no launchd plist
- no nginx or Caddy config
- no Fly/Render/Vercel/Netlify config

If you want a real deploy guide, the next step is to pick one target platform and codify it in the repo.
