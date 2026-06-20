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
- `ADMIN_PASSWORD` or `ADMIN_TOKEN` — required for any non-loopback bind in standalone mode; optional for the HA add-on, which trusts its own LAN (see **Admin access model**)
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

Shared Conductor lifecycle is defined by `scripts/conductor-*.sh` (wired through Conductor's per-workspace `.conductor/settings.toml`, an app-managed file that is not committed):

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
- `POST /api/hot-reload` — reload `prompt_world.py`, `transitions.py`, `fallbacks.py` then `scriptwriter.py` (leaves-first) in-place without stopping the stream. Requires `--workers 1` (importlib reloads only the worker that handles the request; multi-worker deployments get inconsistent results).
- `POST /api/homeassistant/labels/regenerate` — force a background refresh of generated device labels; returns `{"scheduled": true}`, `{"scheduled": false, "reason": ...}` when HA context or an Anthropic key is unavailable, or 409 if a refresh is already running.

### Diagnosing provider fallbacks

`GET /status` returns a `runtime_status` object under the top-level response. It contains:

- `station_on_air` — listener-centric boolean that is true only when producer/playback tasks are alive, no listener-facing silence failure is active, and the session is not stopped.
- `health_state` — backward-compatible runtime health state for blocked tasks, listener-facing silence, paused sessions, and provider fallback summaries.
- `providers` — current `audio_source`, `script_provider`, and `tts_provider` with `primary_provider`, `current_provider`, `fallback_active`, `recovery_mode`, `retry_in_seconds`, and `action_guidance` fields per provider. `script_provider` populates the recovery fields so transient Anthropic errors read differently from circuit-breaker and `action_required` fallback; non-script providers keep those fields empty unless future recovery metadata is added.
- `recent_events` — last 10 provider switch/failover events with timestamps, reasons, and whether a fallback was active.
- `last_switch` — most recent provider change event, or `null` if no switches have occurred this session.
- `failover_events` — last 10 events where `fallback_active` was true.

The Engine Room card in `/admin` renders this as two tiers: station health ("On Air" / "Paused" / "Error") and provider health ("Primary" / "Auto-recovering" / "Backup active"). Structured log events (`provider_switch_event`, `provider_health_state`) are also emitted so log aggregators can alert on sustained fallback states.

### Reading queue-rescue health ("running on rescue")

`runtime_status.bridge_health` reports how often the producer is bridging a
starved lookahead queue with rescue audio (cached, canned, or an emergency
tone). When a bridge fires the station is briefly not the real radio — audio
keeps playing, but it is rotation/canned fallback, not fresh content. The fields:

- `session_count` / `by_type` — lifetime bridge fires this session, split across
  `drain` (queue emptied mid-playback), `resume` (waking from a stopped session),
  and `idle` (a listener returned after the station went idle).
- `window_count` — bridge fires inside the rolling window (`window_seconds`,
  default 1800s / 30 min).
- `last_fire` — the most recent bridge `{bridge_type, source, timestamp}`.
- `queue_empty_elapsed_s` — how long the queue has been empty right now.
- `unhealthy` — `true` once **either** signal trips: `window_count` reaches
  `threshold` (default **2 bridges in 30 minutes**), **or** `queue_empty_elapsed_s`
  passes `queue_empty_threshold_s` (default **60s of continuous queue-empty
  time**, measured over `queue_empty_window_seconds`). `unhealthy_reasons` lists
  which signal(s) fired (`bridge_frequency`, `queue_empty`, or both). That is the
  documented line for "the station is running on rescue": one startup or resume
  bridge is normal, but repeated bridging — or a queue that stays empty — means
  the queue is starving (most visibly on the Pi, where normalization latency is
  high) and needs attention even though audio plays.

The Engine Room **Queue rescue** row renders this as "Healthy" or "Running on
rescue", with the window/session counts, the last bridge, and current
queue-empty seconds. A `producer_bridge_fire` structured log event is emitted on
every fire so log aggregators can alert on sustained starvation. Counts are
session-local by design and reset on restart. This is observability only — it
does not change scheduling, prefetch depth, or rescue selection.

### Reading producer headroom

`runtime_status.producer_headroom` shows how full the lookahead queue is relative
to the configured runway target, so a starving queue is visible before it has to
bridge. The fields:

- `queue_depth` — segments currently queued (`-1` if the queue is not yet attached).
- `queue_capacity` — the queue's hard cap.
- `lookahead_target` — the runway target, `max(4, pacing.lookahead_segments)`
  (default `lookahead_segments = 4`).
- `buffered_audio_sec` — total seconds of audio already queued, summed from
  segment durations.
- `headroom_ok` — `true` once `queue_depth >= lookahead_target`.
- `reason` — human-readable: `"ready runway"` or `"building runway"`.

This is observability only; the producer's own backpressure (`lookahead_segments`)
governs how deep it prefetches.

### Detecting a not-working provider key

A key that is present but invalid is validated actively, so the operator sees it
without waiting for a banter or TTS segment to fail. The active checks cover
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `AZURE_SPEECH_KEY`/`AZURE_SPEECH_REGION`,
and `ELEVENLABS_API_KEY`.

- On startup (when any key is configured) and after a key-save, a single secret-safe
  provider probe (`check_provider_keys`) runs in the background — fire-and-forget, so it
  never delays boot or the first audio. Anthropic/OpenAI use minimal text probes; Azure Speech
  and ElevenLabs use voice-list endpoints, not billable synthesis. `POST /api/setup/provider-check`
  runs it on demand.
- The verdict is cached on the station state and exposed in `GET /api/capabilities`:
  `capabilities.anthropic_key_status` / `capabilities.openai_key_status`, and
  `provider_health.{anthropic,openai,azure_speech,elevenlabs_tts}.key_status`. Each is
  `"unverified"` (not yet checked, or a non-auth probe error such as quota/rate-limit/network),
  `"valid"`, or `"rejected"` (the provider actively refused the key with a 401).
- A `"rejected"` key reads in the Engine Room as a persistent **key not working — replace key**
  state, distinct from the transient time-based `anthropic_degraded` "suspended" fallback. When a
  rejected key is the only configured LLM key, `capabilities.next_step` steers toward replacing it.
- The listener side never surfaces key health; if OpenAI is valid the station keeps sounding live.

For voice casting specifically, run
`.venv/bin/python scripts/audition_tts_voices.py --include-catalog --providers all` to generate local clips
and a manifest under `tmp/voice-auditions/`. Missing TTS-provider credentials are shown as skipped instead
of being hidden by the runtime Edge fallback.

## Recommended production shape

There is no blessed platform in this repo, but the sensible shape is:

1. Run the app behind a reverse proxy.
2. Bind the app on a private interface.
3. Require `ADMIN_PASSWORD` or `ADMIN_TOKEN`.
4. Persist `cache/`, `tmp/` where practical.
5. Monitor app logs.

## Admin access model

This section is the single source of truth for who may reach `/admin` and the
admin API. Two layers enforce it: a **boot check** (`_validate` in
`mammamiradio/core/config.py`) decides whether the process starts at all, and a
**per-request check** (`require_admin_access` in `mammamiradio/web/auth.py`)
authorizes each call. The tables below are the contract; the code conforms to
them, and the `tests/web/test_streamer_routes.py` admin-access group plus
`tests/core/test_config.py` bind tests pin every row (helper-level unit tests
live in `tests/web/test_auth.py`). Change a row here and in those two
enforcement points together, never one without the others.

Terms: **standalone** = any non-add-on run (local, Docker). **add-on** =
the Home Assistant add-on (`is_addon` true). **Creds** = `ADMIN_PASSWORD` and/or
`ADMIN_TOKEN`. **Private network** = loopback, RFC1918 LAN, Tailscale/CGNAT
(`100.64.0.0/10`), IPv4/IPv6 link-local, IPv6 unique-local (`fc00::/7`), and the
HA Supervisor network (`172.30.32.0/23`). A non-loopback bind is `0.0.0.0`, a
LAN/Tailscale address, or an empty `MAMMAMIRADIO_BIND_HOST` (listens on all
interfaces).

### Boot: does the process start?

| Bind host | Mode | Creds set? | Result |
| --- | --- | --- | --- |
| Loopback (`127.0.0.1`, `localhost`) | any | any | Starts |
| Non-loopback | standalone | none | **Refuses to boot** (config error) |
| Non-loopback | standalone | yes | Starts |
| Non-loopback | add-on | any | Starts (the add-on trusts its own LAN) |

The add-on is the only mode that boots on a non-loopback bind without a
credential. It is the operator's own Home Assistant box, so it trusts its LAN by
design — see the per-request table for what that LAN may then do.

### Per request: may this caller reach `/admin`?

| Caller origin | Creds configured | Result |
| --- | --- | --- |
| Loopback | any | Allow (no CSRF — same machine) |
| HA Supervisor net, add-on mode | any | Allow (no CSRF — Docker-internal, used by HA automations) |
| Private network (LAN / Tailscale / IPv6 ULA+link-local) | `ADMIN_TOKEN` set | Require `X-Radio-Admin-Token` header (`401` if missing/wrong) |
| Private network | `ADMIN_PASSWORD` set | Require Basic auth + CSRF on writes (`401` if wrong) |
| Private network | none | Allow read; CSRF token or same-origin required on writes |
| Public IP | none | **`403` reject** |
| Public IP | any cred set | Require that credential (`401` if missing/wrong) |

Two invariants this table preserves:

- **A configured credential is never bypassed by private-network trust.** If you
  set `ADMIN_PASSWORD` or `ADMIN_TOKEN`, a LAN/Tailscale client must present it —
  it is not auto-trusted just for being on a private network. The credential-less
  "allow read on the LAN" row only applies when no credential is configured.
- **Public IPs never reach `/admin` without a credential.** The credential-less
  LAN fallback is scoped to private networks; a public client is rejected.

This model reads `request.client.host` raw, so the bind must not sit behind an
untrusted reverse proxy — one that rewrites the client address would make every
caller appear private and collapse the table above.

`ADMIN_TOKEN` is a header-only API credential (`X-Radio-Admin-Token`). A browser
cannot send it on plain navigation, so to open `/admin` in a browser on a
credentialed non-loopback bind you need `ADMIN_PASSWORD`; use `ADMIN_TOKEN` for
programmatic/API callers (HA `rest_command`, scripts).

The HA add-on ships with **no credential by default**: a direct LAN browser hits
`http://<ha-ip>:8000/admin` and lands in the credential-less private-network row
(read allowed, writes CSRF-guarded), while ingress and HA automations come in on
the Supervisor network. To require a credential on the add-on, set `admin_token`
in the add-on options; a configured token is then enforced even on the LAN.

## Docker

```bash
docker compose up
```

The `Dockerfile` builds a standalone image with Python 3.11 and FFmpeg. The container runs as a non-root `radio` user. `docker-compose.yml` maps `.env` variables and mounts a persistent volume at `/data` for cache and temp files.

`ADMIN_TOKEN` is required in `.env` (the container binds to `0.0.0.0`).

## Home Assistant add-on

The `ha-addon/` directory contains a complete HA add-on scaffold. Users add the repo URL in HA Settings > Add-ons > Repositories, then install "Mamma Mi Radio" from the store.

The add-on entrypoint (`ha-addon/mammamiradio/rootfs/run.sh`) maps Supervisor-injected `$SUPERVISOR_TOKEN` to `HA_TOKEN`, reads add-on options from `/data/options.json`, and starts uvicorn. It binds `0.0.0.0` with no admin credential by default and trusts its own LAN for admin access (see **Admin access model**); set `admin_token` in the add-on options to require a credential.

The dashboard is accessible via HA ingress (sidebar). The first-run flow exposes the same setup checks there as every other run mode, and the stream URL can be played on any HA media player.

When HA context is enabled, the station reads the Home Assistant state snapshot opportunistically before banter, ad, and news-flash generation (so the weather flash grounds in a freshly refreshed forecast). It does not send every entity to the script prompt: telemetry/config entities, unavailable states, free-text helpers (e.g. `input_text`), and sensitive domains such as trackers, cameras, and alarms are filtered first. Resident presence (`person.*`) is kept as home/away only, with GPS and identity attributes stripped, so arrival greetings and the empty-home mood keep working without leaking location. The remaining entities are scored and capped before prompt assembly. When label generation is active (HA enabled and an Anthropic key configured), the display names and room assignments for non-sensitive entities are also sent to Anthropic once to generate radio-friendly labels; no sensor values, presence, or location are included, and the results are cached locally (`cache/ha_label_catalog.json`, owner-only) so each device is only looked up once. A startup log line confirms when this is active. The admin Engine Room shows the scored prompt slice and privacy filter counts under Home Assistant details; `/public-status` exposes only listener-safe Casa moments.

## Home Assistant pushed entities

When the HA integration is enabled (`ha_enabled: true` in `radio.toml` or the HA add-on), mammamiradio automatically pushes its playback state to HA after each segment transition and every 30 seconds. No operator configuration required — entities appear in **Developer Tools → States** within 30 seconds of startup.

| Entity ID | Type | State values | Key attributes |
|---|---|---|---|
| `media_player.mammamiradio` | media_player | `playing` / `idle` | `media_title`, `media_artist`, `media_content_type`, `media_position`, `media_position_updated_at` (playing only), `entity_picture`, `mammamiradio_segment_type`, `mammamiradio_listeners`, `mammamiradio_queue_depth` |
| `sensor.mammamiradio_segment_type` | sensor | `music` / `banter` / `ad` / `off` | — |
| `sensor.mammamiradio_listeners` | sensor | integer | `unit_of_measurement: listeners` |
| `binary_sensor.mammamiradio_on_air` | binary_sensor | `on` / `off` | — |

All four entities are labelled with the configured station name (`Mamma Mi Radio` by default): the media player's `friendly_name` is the station name itself (and it doubles as `media_artist` for non-music segments), while the sensors read `<station> Segment Type`, `<station> Listeners`, and `<station> On Air`. Entity IDs and the `mammamiradio_*` attribute keys stay fixed regardless of the display name, so existing automations and dashboards keep working.

`entity_picture` is always an absolute image URL: the real album cover while a track plays, and the station logo for host talk, ads, and idle. The logo fallback matters because the HA media card keeps the last cover when `entity_picture` is removed — so without it the previous track's art would linger through a news flash. Override the logo per station with `artwork_url` under `[brand]` in `radio.toml` (must be an absolute `http(s)` URL; a relative path is rejected because HA resolves `entity_picture` against its own origin). Blank uses the bundled station logo.

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
