# Operations

This repo supports three deployment models: Docker container, Home Assistant add-on, and local Python dev.

## What a real deployment needs

- Python 3.11+
- `ffmpeg` on `PATH`
- writable `tmp/` and `cache/` directories
- outbound network access for Apple Music charts API, Anthropic/OpenAI, and optional Home Assistant

Music comes from live Italian charts (via yt-dlp) when `MAMMAMIRADIO_ALLOW_YTDLP=true`, otherwise from local `music/` files. If neither is available the playback loop rescues from the norm cache, then from bundled demo assets under `mammamiradio/assets/demo/music/` when present, and as a final fallback requests forced banter from the producer so the queue recovers without crashing or stalling on silence. Chart entries pass through a narrow content-hygiene filter at ingest that drops obvious non-music (podcasts, BBC comedy, audiobooks, news briefings) before they enter the candidate pool — see `mammamiradio/playlist/playlist.py::_NON_MUSIC_MARKERS`.

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
- `ADMIN_PASSWORD` or `ADMIN_TOKEN` — required for any non-loopback bind (see **Admin access model**)
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

`mammamiradio/web/streamer.py` is the single source of truth. `architecture.md` (sibling) has the full route table with methods. Summary grouped by access level:

Public:

- `GET /` (listener page; HA ingress serves admin)
- `GET /listen` (alias of `/`)
- `GET /stream`
- `GET /healthz`, `GET /readyz`, `GET /public-status`
- `GET /sw.js`, `GET /static/{filename:path}` (PWA assets)
- `POST /api/clip` (rate-limited, 1 per 10s per IP)
- `GET /clips/{id}.mp3` (no auth, for sharing)
- `POST /api/listener-request`, `GET /public-listener-requests` (sanitized feed for the on-page sidebar)

The read-only sidecar monitor in `scripts/stream_watch_server.py` is intentionally limited to `/public-status`, `/healthz`, and `/readyz` so it still works when admin auth is enabled.

Admin (require `ADMIN_PASSWORD` or `ADMIN_TOKEN` unless on loopback):

- `GET /admin`, `GET /dashboard`
- `GET /status`, `GET /api/capabilities`
- `GET /api/setup/status`, `POST /api/setup/recheck`, `POST /api/setup/provider-check`, `POST /api/setup/save-keys`, `GET /api/setup/addon-snippet`
- `POST /api/shuffle`, `POST /api/skip`, `POST /api/purge`, `POST /api/stop`, `POST /api/resume`, `POST /api/trigger`
- `GET /api/pacing`, `PATCH /api/pacing`
- `GET /api/hosts`, `PATCH /api/hosts/{host_name}/personality`, `POST /api/hosts/{host_name}/personality/reset`
- `POST /api/credentials`, `POST /api/track-rules`
- `GET /api/listener-requests`, `POST /api/listener-requests/dismiss`
- `GET /api/search`, `POST /api/playlist/add`, `POST /api/playlist/remove`, `POST /api/playlist/move`, `POST /api/playlist/move_to_next`, `POST /api/playlist/load`, `POST /api/playlist/add-external`
- `POST /api/hot-reload` — reload `scriptwriter.py` in-place without stopping the stream. Requires `--workers 1` (importlib reloads only the worker that handles the request; multi-worker deployments get inconsistent results).

### Diagnosing provider fallbacks

`GET /status` returns a `runtime_status` object under the top-level response. It contains:

- `providers` — current `audio_source`, `script_provider`, and `tts_provider` with `primary`, `active`, and `fallback_active` flags per provider.
- `recent_events` — last 10 provider switch/failover events with timestamps, reasons, and whether a fallback was active.
- `last_switch` — most recent provider change event, or `null` if no switches have occurred this session.
- `failover_events` — last 10 events where `fallback_active` was true.

The Engine Room card in `/admin` renders this live. Structured log events (`provider_switch_event`, `provider_health_state`) are also emitted so log aggregators can alert on sustained fallback states.

## Recommended production shape

There is no blessed platform in this repo, but the sensible shape is:

1. Run the app behind a reverse proxy.
2. Bind the app on a private interface.
3. Require `ADMIN_PASSWORD` or `ADMIN_TOKEN`.
4. Persist `cache/`, `tmp/` where practical.
5. Monitor app logs.

## Admin access model

Loopback (`127.0.0.1`, `localhost`) is fully trusted — no credentials needed.

For any non-loopback bind (`0.0.0.0`, a LAN/Tailscale address, or an empty
`MAMMAMIRADIO_BIND_HOST`, which listens on all interfaces):

- **Standalone startup now fails** unless `ADMIN_PASSWORD` or `ADMIN_TOKEN` is
  set. This is a behavior change: earlier versions started without credentials
  and trusted private networks at runtime. If you bind to `0.0.0.0` in
  standalone mode, set a credential or startup raises a config error.
- **When a credential is configured, private-network trust no longer bypasses
  it.** A LAN/Tailscale client must present the credential; it is no longer
  auto-trusted just for being on a private network.
- **`ADMIN_TOKEN` is a header-only API credential** (`X-Radio-Admin-Token`). A
  browser cannot send it on plain navigation, so to open `/admin` in a browser
  on a non-loopback bind you need `ADMIN_PASSWORD`. Use `ADMIN_TOKEN` for
  programmatic/API callers (HA `rest_command`, scripts).
- **Credential-less private-network deployments are unchanged** — still trusted
  for reads and CSRF-guarded on writes. The HA add-on auto-generates an
  `ADMIN_TOKEN` at startup, so it is always in the credentialed path.

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

## Home Assistant pushed entities

When the HA integration is enabled (`ha_enabled: true` in `radio.toml` or the HA add-on), mammamiradio automatically pushes its playback state to HA after each segment transition and every 30 seconds. No operator configuration required — entities appear in **Developer Tools → States** within 30 seconds of startup.

| Entity ID | Type | State values | Key attributes |
|---|---|---|---|
| `media_player.mammamiradio` | media_player | `playing` / `idle` | `media_title`, `media_artist`, `media_content_type`, `mammamiradio_segment_type`, `mammamiradio_listeners` |
| `sensor.mammamiradio_segment_type` | sensor | `music` / `banter` / `ad` / `off` | — |
| `sensor.mammamiradio_listeners` | sensor | integer | `unit_of_measurement: listeners` |
| `binary_sensor.mammamiradio_on_air` | binary_sensor | `on` / `off` | — |

**30-second cold-start note:** after a HA or addon restart, pushed entities reappear within 30 seconds via the heartbeat. Automations triggering on `state_changed` may miss the first segment after restart — add an `initial_state: playing` guard if needed.

**Lovelace media card:**

```yaml
type: media-control
entity: media_player.mammamiradio
```

**Automation example** (turn lights down when banter starts):

```yaml
trigger:
  - platform: state
    entity_id: sensor.mammamiradio_segment_type
    to: "banter"
action:
  - service: light.turn_on
    data:
      brightness_pct: 30
```

**Note:** pushed entities appear in Developer Tools → States but not in the HA entity registry (Integrations page). HA Assist requires a HACS integration for registry visibility.

## What is still not documented because it does not exist yet

- no systemd unit
- no launchd plist
- no nginx or Caddy config
- no Fly/Render/Netlify config (public preview deployment is a future idea — blocked on cost and music copyright)
