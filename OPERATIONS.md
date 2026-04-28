# Operations

This repo supports three deployment models: Docker container, Home Assistant add-on, and local Python dev.

## What a real deployment needs

- Python 3.11+
- `ffmpeg` on `PATH`
- writable `tmp/` and `cache/` directories
- outbound network access for Apple Music charts API, Anthropic/OpenAI, and optional Home Assistant

Music comes from live Italian charts (via yt-dlp) when `MAMMAMIRADIO_ALLOW_YTDLP=true`, otherwise from local `music/` files. If neither is available the playback loop rescues from the norm cache, then from bundled demo assets under `mammamiradio/demo_assets/music/` when present, and as a final fallback requests forced banter from the producer so the queue recovers without crashing or stalling on silence. Chart entries pass through a narrow content-hygiene filter at ingest that drops obvious non-music (podcasts, BBC comedy, audiobooks, news briefings) before they enter the candidate pool — see `mammamiradio/playlist.py::_NON_MUSIC_MARKERS`.

Downloads that fail `validate_download` (missing file, too-short duration, corrupt) are purged from the cache directory and added to a process-local denylist so the same track is not re-selected endlessly. The main producer loop, prefetch, and prewarm all short-circuit on denylisted keys via a bounded retry around `select_next_track`. The denylist clears on restart. Music quality-gate rejections (silence, post-normalization artifacts) do NOT denylist the source track — they drop the cached normalization only and rely on the 3-consecutive-rejection circuit breaker to recover. Log signatures:

```
INFO Rejecting non-music chart entry: BBC Studios - <title>
INFO Chart ingest: filtered N non-music entries
WARNING Skipping track due to invalid download (<track>): <reason>
WARNING Purged rejected cache file <key>.mp3: <reason>
DEBUG Skipping denylisted track (already rejected this session): <track>
```

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

`mammamiradio/streamer.py` is the single source of truth. `ARCHITECTURE.md` has the full route table with methods. Summary grouped by access level:

Public:

- `GET /` (listener page; HA ingress serves admin)
- `GET /listen` (alias of `/`)
- `GET /stream`
- `GET /healthz`, `GET /readyz`, `GET /public-status`
- `GET /sw.js`, `GET /static/{filename:path}` (PWA assets)
- `POST /api/clip` (rate-limited, 1 per 10s per IP)
- `GET /clips/{id}.mp3` (no auth, for sharing)
- `POST /api/listener-request`

The read-only sidecar monitor in `scripts/stream_watch_server.py` is intentionally limited to `/public-status`, `/healthz`, and `/readyz` so it still works when admin auth is enabled.

Admin (require `ADMIN_PASSWORD` or `ADMIN_TOKEN` unless on loopback):

- `GET /admin`, `GET /dashboard`
- `GET /status`, `GET /api/capabilities`
- `GET /api/setup/status`, `POST /api/setup/recheck`, `POST /api/setup/save-keys`, `GET /api/setup/addon-snippet`
- `POST /api/shuffle`, `POST /api/skip`, `POST /api/purge`, `POST /api/stop`, `POST /api/resume`, `POST /api/trigger`
- `GET /api/pacing`, `PATCH /api/pacing`
- `GET /api/hosts`, `PATCH /api/hosts/{host_name}/personality`, `POST /api/hosts/{host_name}/personality/reset`
- `POST /api/credentials`, `POST /api/track-rules`
- `GET /api/listener-requests`, `POST /api/listener-requests/dismiss`
- `GET /api/search`, `POST /api/playlist/add`, `POST /api/playlist/remove`, `POST /api/playlist/move`, `POST /api/playlist/move_to_next`, `POST /api/playlist/load`, `POST /api/playlist/add-external`
- `POST /api/hot-reload` — reload `scriptwriter.py` in-place without stopping the stream. Requires `--workers 1` (importlib reloads only the worker that handles the request; multi-worker deployments get inconsistent results).

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
- no Fly/Render/Netlify config (public preview deployment is a future idea — blocked on cost and music copyright)
