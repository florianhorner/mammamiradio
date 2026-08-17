# Operations

This repo supports three deployment models: Docker container, Home Assistant add-on, and local Python dev.

## What a real deployment needs

- Python 3.11+
- `ffmpeg` on `PATH`
- writable `tmp/` and `cache/` directories
- outbound network access only for enabled optional services (Jamendo,
  Anthropic/OpenAI, Home Assistant, or standalone external media)
- working shared memory (`/dev/shm`) and one spare process slot, when Home
  Assistant context is on

With Home Assistant context enabled, the station runs one extra Python process
besides the server: the Home Assistant projection worker. It is created lazily at
the first context refresh, holds no credentials, writes nothing, and stays idle
between refreshes (default every 300s). Under `ps` inside the container it shows
up as a second `python3 ... spawn_main` alongside the resident
`multiprocessing.resource_tracker` helper. If it cannot start, the station keeps
playing and only the home colour pauses — see `docs/troubleshooting.md` →
"Home Assistant colour is paused".

Music starts from the packaged, attributed twelve-track starter catalog and
does not require network access. Approved starter derivatives play directly;
all twelve identities complete before the bag repeats. Operator `music/` files
may join the base rotation without receiving a project rights claim. Optional
Jamendo preparation runs in the background only after explicit non-commercial
enablement and never delays a due music slot. The complete source and rights
boundary is [Music sources and rights boundaries](music-sources.md).

In a standalone installation that deliberately enables the optional
`external-media` extra, live Italian charts may supplement the base through
yt-dlp. A missing or failed extractor falls back to local or starter music. On a
genuine empty-queue gap, the playback loop rescues from the packaged
`continuity_1.mp3` clip, the norm cache, then eligible bundled starter assets;
only when no eligible music exists does it repeat packaged recovery, with
`emergency_tone.mp3` as the neutral two-second cold-cache last rung. After 60
seconds without any bridge asset it requests forced banter, so the queue recovers
without crashing or stalling on silence. Quality-rejected and operator-banned
cache files remain ineligible.

The `/healthz`–`/readyz` silence gate keys on nothing airing at all, not on an
empty queue: a station bridging on packaged recovery is on air, so the add-on
watchdog is not invited to restart it mid-recovery. The required recovery files
under `mammamiradio/assets/demo/recovery/` are durable package resources, not temp
renders, and cleanup paths guard them before unlinking anything marked ephemeral.
On an empty queue the rescue ladder opens after `FIRST_BYTE_GRACE_SECONDS` (1s).
Resume, idle, and active-playback drain bridges prefer a cached song, then the
short branded continuity clip, then the emergency tone. Startup first tries the
restart handoff spool (`cache/restart_handoff/`), admitting already-normalized
music ahead of the producer/playback tasks (see `docs/architecture.md` →
"Restart handoff spool"). The producer's multi-segment delivery cushion means a
timed-out queue read signals genuine starvation rather than a normal segment
boundary; `QUEUE_FALLBACK_WAIT_SECONDS` (5s) remains only the no-content ceiling.
`scripts/ha-green-launch-smoke.py` (`make launch-smoke`, run in `pi-smoke.yml`)
denies non-loopback networking and requires first byte within two seconds for
both a warm-cache station and an empty-cache/package-only station. External chart
entries pass through the narrow non-music filter in
`mammamiradio/playlist/playlist.py::_NON_MUSIC_MARKERS` before admission.

The existing continuity ladder remains a safety path, not the primary catalog:
packaged recovery audio and eligible cached local/standalone music cover queue
drains while the producer catches up. `/healthz` and `/readyz` key on actual
audio delivery rather than queue depth. A connected listener must receive the
first accepted non-silent starter byte within two seconds on the cold,
network-disabled smoke path. Jamendo is excluded from cache rescue, continuity,
SQLite, restart handoff, and persistent reuse.

In a standalone installation with the optional `external-media` extra enabled,
external acquisition failures still use `_failed_<cache-key>.mp3` markers and a
per-process denied-key set rather than synthesized silence. Quality-gate rejects
are removed and the producer advances. This mechanism never applies to the
single-use Jamendo namespace. Representative standalone log signatures are:

```
INFO Rejecting non-music chart entry: BBC Studios - <title>
INFO Chart ingest: filtered N non-music entries
WARNING Skipping <context> track due to invalid download (<track>): <reason>
WARNING Purged rejected cache artifact <artifact>: <reason>
DEBUG No eligible music tracks remain after excluding session-rejected cache keys
```

## Required secrets and config

Environment:

- `MAMMAMIRADIO_BIND_HOST`
- `MAMMAMIRADIO_PORT`
- `MAMMAMIRADIO_ALLOW_YTDLP` (standalone only; default `false`; also requires
  the `external-media` extra. Both Home Assistant add-ons force it off and omit
  yt-dlp.)
- `JAMENDO_CLIENT_ID` (optional secret; normally managed through **Motore ->
  Setup -> Music sources** together with explicit enablement and the current
  non-commercial acknowledgement)
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD` or `ADMIN_TOKEN` — required for any non-loopback bind in standalone mode; optional for the HA add-on, which trusts its own LAN (see **Admin access model**)
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY` (optional, used for TTS and as script generation fallback)
- `HA_TOKEN` if Home Assistant integration is enabled

Static config:

- `radio.toml`
- `model_registry.toml` — AI model IDs, task routing, OpenAI TTS model, and per-model pricing. Must sit beside `radio.toml`. If missing, the station boots and plays music but degrades to stock host copy and Edge voices until it is restored.

## OpenAI fallback evaluator

`scripts/eval_openai_script_model.py` is a local operator command for comparing the
**OpenAI script-generation fallback**. It makes paid provider calls and does not evaluate
the Anthropic-first live path, listener-ready output, or runtime enforcement.

Always inspect the no-network preview first:

```bash
.venv/bin/python scripts/eval_openai_script_model.py --dry-run
```

The preview validates every fixture before a request and prints model, fixture, call,
completion-token, and registry-pricing bounds. For a real run, set `OPENAI_API_KEY`; an
explicit model without registry pricing also needs `--allow-unpriced`:

```bash
OPENAI_API_KEY=... .venv/bin/python scripts/eval_openai_script_model.py
OPENAI_API_KEY=... .venv/bin/python scripts/eval_openai_script_model.py --models candidate-id --allow-unpriced
```

Each run writes schema-versioned JSONL under `tmp/evals/`. Parsed outputs include a
deterministic raw-output integrity receipt (station-name, named-banter-roster, and
spoken-text `PASS`/`FAIL`/`N/A` states); provider or JSON failures use `floor: null` and
remain separate from those checks. A completed default run is observational and exits `0`
even when a receipt fails. Use unit tests—not a paid run—as CI enforcement.

## Runtime outputs

- `tmp/` rendered segments and temp assets
- `cache/` downloaded track assets

### Music cache sizing

The normalization cache stores ready-to-play songs. A cached song starts almost
instantly. An uncached song takes a full render, about 65 seconds on HA Green
hardware, so a small cache makes listeners wait more often.

`MAMMAMIRADIO_MAX_CACHE_MB` sets the ceiling. Standalone defaults to 500 MB. The
add-on defaults to 1500 MB and exposes the **Music cache size (MB)** option
(`norm_cache_mb`). Explicit environment values outside 200-8000 are clamped; a
malformed environment value uses the applicable default. Either correction logs
a warning so config loading can complete. Supervisor validates the add-on option
as an integer in that range before the container starts. If unsupported
non-positive add-on input nevertheless reaches an internal ingestion path, both
`run.sh` and the direct fallback silently use the 1500 MB add-on default.

Size the cache for the rotation. A 200-track Jamendo rotation needs about 1 GB.
Below that, LRU eviction can remove tracks before the rotation returns, which
causes repeated cold renders. If a few songs repeat while others rarely play,
increase the cache. **On-Air Sound** roughly doubles the per-track footprint
because each song keeps a normalized file and a coloured bake.

At startup, the station combines free space, reclaimable cache bytes, and a 512 MB
reserve to choose the effective ceiling. It writes that value back to the config
object so the producer periodic eviction pass uses the same limit. A configured
ceiling above available free space would not trigger eviction and could fill the
volume. On the add-on, that volume is `/data`, shared with the database and ledger.
The station logs the configured and effective values when it lowers the ceiling.

The effective ceiling stops at 200 MB. The norm cache also supplies fallback audio
when the playback queue needs a ready track, so startup keeps that floor even when
the disk has less room. In that case, the log says the disk is nearly full, warns
that the minimum may still exceed available space, and tells the operator to free
space on the add-on data disk.

The effective ceiling appears in the startup log and in authenticated `/status`
as `cache_limit_mb`. The add-on Configuration tab continues to show the value you
entered. If songs still repeat after raising the setting, check `ha apps logs`
for a trim warning.

## Audition the Modern Night Drive imaging pack

From the repository root, run:

```bash
.venv/bin/python scripts/audition_sonic_brand.py
```

It writes a timestamped review directory under `tmp/sonic-brand-auditions/`.
The directory contains an `index.html` listening page and a manifest. Before it
writes either file, the command checks every compatibility alias. The board
covers the Neon Relay station ID and sweeper, the time check, and both
directional transitions. It also covers all three ad bumpers, Casa Notte, and
the nine fictional-ad recipes. Open the generated HTML file in a browser; the
script does not open it for you.

Use `--output-dir PATH` to choose the parent directory and `--timestamp
YYYYMMDDTHHMMSSZ` for a deterministic review location. The script validates the
pack before writing the board. It runs locally without calling TTS providers or
changing the app's playback queue. The runtime pack has 47 checksum-bound
outputs, each with its own retained project-authored source. Nine recipes define
the ad scenes. After listening on the Mac, small speaker, and Sonos, the reviewer
signs a receipt that binds the board content digest to the pack digest. The
installed runtime manifest omits the receipt and board previews. Pack layout,
provenance, selection precedence, and recovery/broadcast-chain boundaries are in
[`mammamiradio/assets/imaging/README.md`](../mammamiradio/assets/imaging/README.md).

## Startup model

The intended local startup path is:

```bash
./start.sh
```

That script launches uvicorn with `--reload`, `*.toml` reload support, and `LOG_LEVEL` from the environment.

## Operator Stop and Resume

The admin Stop button pauses the station session; it does not stop the Home
Assistant add-on process. The durable authority is
`cache_dir/session_stopped.flag`, which survives an add-on or watchdog restart.
A listener opening `/stream` cannot clear it.

Stop is persistence-first:

1. Write the stop marker. If that fails, return `503` and leave live playback,
   queue, and reservations unchanged.
2. Mark the runtime stopped, clear listener-audible truth, and advance
   `continuity_epoch` so work already rendering for the old timeline cannot be
   admitted.
3. Cut real media, purge queued work, clear pending interrupt/forced/continuity
   slots, and publish the stopped sentinel. Cleanup warnings after step 1 do not
   undo the durable pause.

Resume remains paused while it prepares the handoff:

1. Reserve readable immediate audio: eligible norm-cache music, then
   `continuity_1.mp3`, then `emergency_tone.mp3`.
2. If no playable runway exists, return `503` with `force_available: true` and
   keep the marker.
3. Remove the marker. If removal fails, return `503` and stay paused.
4. Clear the runtime stop state and wake producer/playback.

The assetless path represents a corrupt installation and never starts
automatically. After the normal `503`, the admin requires explicit operator
confirmation before sending `POST /api/resume?force=true`. That request removes
the marker first, sets `force_next=BANTER`, clears the stopped state, wakes
recovery, and returns
`{"ok":true,"recovering":true,"runway_source":"none"}`. `/readyz` remains
`503 starting` until a listener accepts the rebuilt host audio.

`/healthz` remains a runtime-health probe during an intentional pause: Stop
normally stays HTTP `200`, while prolonged silence with active listeners can
make it HTTP `503`.
`/readyz` returns HTTP `503` with `status: "stopped"` even when tasks are alive
and queued audio exists; this prevents routing new listeners to a deliberately
paused station. Every fresh or Resumed session returns HTTP `503` with
`status: "starting"` until at least one listener queue accepts audio. Producer
startup, queued work, and elapsed startup time alone are not readiness. After
listener acceptance it returns HTTP `200` with `status: "ready"`.

This inverts the usual probe contract, and it is deliberate: readiness means
"delivered audio to a real listener", not "would probably deliver audio". The
consequence is a circular dependency for admission control — no listener means
never ready, never ready means no listener gets routed. **Do not use `/readyz`
to gate load-balancer or proxy admission.** Route listeners unconditionally (or
gate on `/healthz`, which stays green on a healthy idle station) and use
`/readyz` for what it is: proof of delivery and the smoke gates. The HA add-on's
own `healthcheck` in `config.yaml` points at `/healthz` for exactly this reason.

Setup and runtime pause are separate truth surfaces. `/api/setup/status` and
setup recovery endpoints continue to describe and repair configuration while
the transport is paused; a configured source stays setup-ready. Runtime status
and `/readyz` carry the pause. Do not treat `setup=ready` as proof the station is
currently on air, or a runtime pause as missing setup.

## Conductor

Shared Conductor lifecycle is defined by `scripts/conductor-*.sh`. The committed
`.conductor/settings.toml` carries shared repository behavior, including the
commit and PR writing contract; machine-specific overrides belong in
`.conductor/settings.local.toml`, which is managed by the Conductor app:

- setup bootstraps `.venv`, installs dev tooling, and links `.env` from `~/.config/mammamiradio/.env` when present, falling back to `$CONDUCTOR_ROOT_PATH/.env`
- run exports a workspace-specific port and tmp/cache dirs before delegating to
  `./start.sh`; external extraction remains off unless a standalone operator
  installs the extra and opts in
- archive deletes `.context/conductor/`

## HTTP surface

`mammamiradio/web/streamer.py` is the single source of truth. `architecture.md` (sibling) has the full route table with methods. Summary grouped by access level:

Public:

- `GET /` (listener page; HA ingress serves admin)
- `GET /listen` (alias of `/`)
- `GET /stream` (a fresh, audio-unconfirmed install receives its packaged
  client-local mini-show before joining the shared live stream)
- `GET /healthz`, `GET /readyz`, `GET /public-status`
- `GET /sw.js`, `GET /static/{filename:path}` (PWA assets)
- `POST /api/clip` (rate-limited; music sharing is available only for a
  complete single bundled-track window)
- `GET /clips/{id}.mp3` (no auth, for sharing)
- `POST /api/listener-request`, `GET /public-listener-requests` (sanitized feed for the on-page sidebar)

The read-only sidecar monitor in `scripts/stream_watch_server.py` is intentionally limited to `/public-status`, `/healthz`, and `/readyz` so it still works when admin auth is enabled.

### Listener-request forwarded identity

`POST /api/listener-request` is public and rate-limited per listener identity.
The identity is used only as input to the HMAC-backed rate-limit key; raw IP
addresses are not stored in listener-request state or returned by the API.

When the app is served through Home Assistant Supervisor ingress, Supervisor
appends the caller chain in `X-Forwarded-For`. The station trusts forwarded
identity headers only when the direct peer is loopback or the Supervisor network
(`172.30.32.0/23`). In that trusted-proxy case it reads `X-Forwarded-For` from
right to left, skips blank/invalid entries and trusted proxy hops, and buckets on
the closest non-trusted hop. If no usable forwarded hop exists, it falls back to
a valid `X-Real-IP`, then to the direct proxy peer.

Direct callers from public networks or private LANs are not trusted proxies. For
them, `X-Forwarded-For` and `X-Real-IP` are ignored and the direct peer address
is the rate-limit identity. This narrow listener-request trust boundary is
separate from the `/admin` private-network access model below.

Admin (require `ADMIN_PASSWORD` or `ADMIN_TOKEN` unless on loopback):

- `GET /admin`, `GET /dashboard`
- `GET /status`, `GET /api/capabilities`
- `PUT /api/media-sources/jamendo`, `POST /api/media-sources/jamendo/retry`
- `GET /api/setup/status`, `POST /api/setup/recheck`, `POST /api/setup/first-listen/players`, `POST /api/setup/first-listen/play`, `POST /api/setup/first-listen/receipt/retry`, `POST /api/setup/first-listen/verify`, `POST /api/setup/home-context-preview`, `PATCH /api/setup/home-context-choice`, `POST /api/setup/provider-check`, `POST /api/setup/save-keys`, `GET /api/setup/addon-snippet`
- `POST /api/shuffle`, `POST /api/skip`, `POST /api/purge`, `POST /api/stop`, `POST /api/resume`, `POST /api/trigger`
- `GET /api/pacing`, `PATCH /api/pacing`
- `GET /api/hosts`, `PATCH /api/hosts/{host_name}/personality`, `POST /api/hosts/{host_name}/personality/reset`
- `POST /api/credentials`, `POST /api/track-rules`
- `GET /api/listener-requests`, `POST /api/listener-requests/dismiss`
- `GET /api/search`, `POST /api/playlist/add`, `POST /api/playlist/remove`, `POST /api/playlist/move`, `POST /api/playlist/move_to_next`, `POST /api/playlist/load`, `POST /api/playlist/add-external`
- `POST /api/hot-reload` — reload `prompt_world.py`, `transitions.py`, `fallbacks.py`, `station_name_guard.py`, then `scriptwriter.py` (leaves-first) in-place without stopping the stream. Requires `--workers 1` (importlib reloads only the worker that handles the request; multi-worker deployments get inconsistent results). `memory_extractor.py` is deliberately excluded — it holds live in-flight task/apply-lock state a reload would reset mid-extraction.
- `POST /api/homeassistant/labels/regenerate` — force a background refresh of generated device labels; returns `{"scheduled": true}`, `{"scheduled": false, "reason": ...}` when HA context or an Anthropic key is unavailable, or 409 if a refresh is already running.
- `GET /api/homeassistant/context-candidates` — admin-only sanitized Home Assistant preview; includes additive `entities` rows plus legacy `sent_now`, `candidates`, and `muted` arrays.
- `PATCH /api/homeassistant/entity-policy` — apply exactly one idempotent `muted` or `personal_moment_enabled` property to one Home Assistant entity; the response returns effective consent, policy revision, and the count of queued host breaks removed by a mute or a personal-moment consent revocation.

### Diagnosing provider fallbacks

`GET /status` includes a redacted top-level `jamendo` object with `enabled`,
the detailed provider `state`, `client_id_configured`, current
`noncommercial_acknowledged`, `terms_scope`, `provider_confirmation`,
`ready`, `in_flight`, last-success age, a coarse last-failure code, and rejected
count. It never contains the client ID, private stream URL, or raw exception.
The source row maps that detail to the five operational UI states documented in
[music-sources.md](music-sources.md#admin-states).

The same response returns a `runtime_status` object. It contains:

- `station_on_air` — listener-centric boolean that is true only when producer/playback tasks are alive, no listener-facing silence failure is active, the session is not stopped, and either a listener accepted a chunk from the current segment or an active listener is inside the bounded three-second handoff grace after the last accepted audio.
- `health_state` — backward-compatible runtime health state for blocked tasks, listener-facing silence, paused sessions, and provider fallback summaries.
- `recovering` — true between a confirmed Force Start and whichever comes first: a listener accepting a chunk, an operator Stop, or a later successful Resume. The admin header renders it as "Starting". It is deliberately outranked by a paused session and by listener-facing silence, so it can never mask the failure Force Start exists to recover from.
- `providers` — current `audio_source`, `script_provider`, and `tts_provider` with `primary_provider`, `current_provider`, `fallback_active`, `recovery_mode`, `retry_in_seconds`, `action_guidance`, `current_reason`, and `switch_reason` fields per provider. `current_reason` says why the provider shown in *this* snapshot is selected right now; `switch_reason` and `last_switch_timestamp` describe the last listener-audible switch and are historical facts a fresh observation never rewrites. Both are operator copy — raw provider codes are translated before they reach the payload. `script_provider` populates the recovery fields so transient Anthropic errors read differently from circuit-breaker and `action_required` fallback; non-script providers keep those fields empty unless future recovery metadata is added.
- `recent_events` — last 10 provider switch/failover events with timestamps, reasons, and whether a fallback was active.
- `last_switch` — most recent provider change event, or `null` if no switches have occurred this session.
- `failover_events` — last 10 events where `fallback_active` was true.

`now_streaming` and `playback_epoch` are selected/readable truth: the file opened
and produced a non-empty chunk. They are intentionally published before delivery
so the timeline has a current selection, but they do not prove a listener heard
it. Listener-audible truth commits only when the stream hub accepts the first
chunk into at least one listener queue. That second boundary sets
`current_stream_audible`, updates `last_air_monotonic` and provider state, and
records rescue/continuity airplay. During the selected-but-not-audible window,
provider rows retain the last listener-audible source. `station_on_air` remains
true only for the bounded three-second handoff grace after prior accepted audio;
without that recent proof it stays false.

The Engine Room card in `/admin` renders this as two tiers: station health ("On Air" / "Paused" / "Error") and provider health ("Primary" / "Auto-recovering" / "Backup active"). Structured log events (`provider_switch_event`, `provider_health_state`) are also emitted so log aggregators can alert on sustained fallback states.

A dead producer or playback task outranks everything in `health_state`,
including a pause — a paused station whose runtime task also died reports
`blocked`, because that fault needs attention before Resume can work. Below
that, a deliberate pause reads as ready ("Paused"), never as an error. Below
that, prolonged silence with listeners connected outranks a Force Start
rebuild: recovery state ends when a listener accepts audio (or when the
operator presses Stop, or a later Resume succeeds normally), so a Force Start
that never produces audio would otherwise hold a calm "Starting" while the
room hears nothing. A rebuild still in progress with no one waiting stays
yellow. The admin header applies the same ranking with one difference: it
checks the pause first, since the operator who paused is looking at it.

### Reading queue-rescue health ("running on rescue")

`runtime_status.bridge_health` reports how often the producer is bridging a
starved lookahead queue with rescue audio (cached, canned, or an emergency
tone). When a bridge fires the station is briefly not the real radio — audio
keeps playing, but it is rotation/canned fallback, not fresh content. The fields:

- `session_count` / `by_type` — lifetime bridge fires this session, split across
  `drain` (queue emptied mid-playback), `resume` (waking from a stopped session),
  `idle` (a listener connected after the station went idle), and `continuity`
  (safety audio reserved by a live control — skip, ban, purge, a mode toggle, a
  rotation edit — that went on to actually air). The `continuity` bucket exists
  because that path used to be completely silent on success: the station could
  bridge on it repeatedly while this row still read "Healthy", which is how a
  repeated song reached a listener before it reached an operator. It counts at
  **air** time rather than reservation time. Every live control reserves safety
  audio before it mutates the queue, and most of that audio is never heard
  because the real queue refills first, so counting reservations would cross the
  2-per-30-minutes threshold after two ordinary admin actions and report a
  healthy station as "running on rescue".
- `window_count` — **producer** bridge fires inside the rolling window
  (`window_seconds`, default 1800s / 30 min). `continuity` is excluded here.
  Air-time counting alone did not keep the false alarm away: reserved audio does
  often air, since it sits at the head of the queue right after the control, so
  two skips or bans in half an hour would still have flipped the row. A
  continuity fire records an operator action that the safety net covered, so it
  stays out of the alarm while remaining visible in `session_count` and
  `by_type`. Any future bridge
  type that fires on ordinary operator activity rather than station failure
  belongs in `_NON_ALARMING_BRIDGE_TYPES` (`web/streamer.py`) for the same reason;
  every new type feeds the window by default.
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

`runtime_status.rescue_rotation` (authenticated only, no filesystem paths) shows
how the cached-music rescue is spreading itself across the warm cache so the same
song cannot air three times in twenty minutes when the producer stalls. A cached
song that airs as a rescue will not be picked again for a full hour of real time
(`cooldown_seconds`, 3600); rescue selection rotates through the tracks that are
outside that window, and when every candidate is still cooling it airs the one
heard longest ago rather than repeating the current song. Fields: `cooldown_seconds`
(the rest window, 3600), `tracked` (how many cached songs have aired as a rescue
this session), `cooling` (how many are still inside the cooldown), and `most_recent`
(the humanized label of the last rescue heard). The rotation is session-local and
resets on restart.

A song enters that rest window on the **first chunk a listener accepts**, not when
its segment ends. That matters because live controls (skip, ban, purge, a mode
toggle, a rotation edit) reserve safety audio at whatever moment the operator acts
— frequently mid-song. Stamping only at the end left a song that was two minutes
into a three-and-a-half-minute play looking like it had never aired, so a control
firing in that window could reserve the song already on the air. Both the
playback-gap rescue and the live-control continuity reservation now ask the same
shared question (`recent_music_identity_keys` / `is_recent_music` in
`mammamiradio/audio/norm_cache.py`) before picking a cached song, so the two paths
cannot disagree about what is currently playing.

Whether a caller may re-serve a recent song is now an explicit parameter
(`allow_recent_repeat`) rather than a property of where the code happens to live.
Each caller declares what sits below it on the ladder.

- `False` — the producer's music-first bridge rung, the first cache ask inside
  `_queue_continuity_bridge`. It has the packaged continuity clip and then the
  emergency tone underneath it, so re-airing the song currently on air is never
  its best option; it falls through to the rung below instead. The live-control
  continuity reservation reaches the same answer by a different route: it skips
  on-air and recently-heard cache files outright (`_is_recent_music`) rather than
  passing this flag.
- `True` — the second cache ask inside `_queue_continuity_bridge`, reached when
  there is no packaged clip; `_producer_error_recovery_segment`; and the
  playback-gap rescue in `run_playback_loop`. Below the first sits two seconds of
  emergency tone. Below the second sits `_blocklist_safe_last_music`, which
  recycles the last-known-good song and is therefore a guaranteed repeat. The
  third has nothing below it at all: `assets/demo/music/` is not packaged, so the
  bundled-demo rung is a no-op in every shipped container, and the packaged-clip
  branch sets `segment_ready`, which makes the 60-second forced-banter escape
  unreachable. A strict ask there means the same 4.4-second station ident on a
  loop while a playable song sits in the cache. At all three depths a song the
  listener heard recently is the better radio, so they ask permissively.

  `_queue_continuity_bridge` therefore appears in both buckets: its two cache
  asks sit at different depths and answer differently.

Permissive still prefers a non-recent candidate. `select_norm_cache_rescue`
returns a recent one only when the cache holds nothing else, matching the
pre-existing behaviour. The flag decides what happens once every candidate is
recent: decline and fall through, or serve the best available repeat.

That parameter is not cosmetic. Reaching for cached music first (above) made the
loose fallback reachable in a place it had never been: on a one-song warm cache a
drain bridge would have queued the song already playing, back-to-back, with
nothing in between — a worse repeat than the one this work exists to fix.

### Reading generated segment waste

`runtime_status.generation_waste` reports rendered audio that was discarded
before it started broadcasting — queue purges on source switch, chaos cutover,
operator stop/panic, bans, producer stale gates, and audio quality-gate rejects
(a rendered music/banter/ad segment that failed the pre-air quality check). The
fields:

- `total_segments` / `total_duration_sec` — lifetime discarded count and audio
  seconds this session.
- `recent_segments` / `recent_duration_sec` — discards inside the rolling window
  (`window_seconds`, default 900s / 15 min).
- `by_reason` / `by_type` — lifetime breakdown by discard reason and segment
  type (`stale_source` for a true source switch, `stale_playlist` for a song that
  left the rotation while it was being rendered, `quality_gate_reject`,
  `operator_stop`, etc.). `stale_continuity` is the expected companion of any
  Stop, Resume, Skip, or Panic: it counts work that was fenced off the air by a
  live control, so a burst of it right after an operator action is proof the
  fence held, not a new fault. Sustained `stale_continuity` with no operator
  activity is worth investigating. `stale_playlist` no longer fires for a pool that merely
  grew or was reordered: adding, shuffling, moving, or enriching the rotation
  leaves a finished render exactly as playable as when it started, and binning it
  cost minutes of Pi CPU while opening the gap the rescue ladder then had to
  cover. Only a removal or a ban invalidates it. Speech and rescue fills are never
  bound to a rotation row at all, so no playlist edit discards them.
- `recent_top_reason` — dominant reason in the rolling window (for "mostly …"
  copy in the admin card).
- `unproduced_segments` — discarded segments that never reached the produced
  counter, used only to keep the rough cost denominator from double-counting
  queued segments later purged.
- `estimated_waste_cost_usd` — rough proration of session API+TTS spend,
  clamped to the session total (it never exceeds what the session actually spent):
  `min(session_cost, session_cost * discarded / (produced + unproduced_discarded))`.
- `cost_basis` — plain-English explanation of the formula and its imprecision
  (count-based proration over-attributes cost to discarded music).
- `degraded` — `true` once **either** signal trips: the raw recent discard
  duration reaches `GENERATION_WASTE_DEGRADED_SECONDS` (default **120s**;
  compared before rounding, so `recent_duration_sec` in the payload is the
  rounded display value only), **or** `recent_segments` reaches
  `GENERATION_WASTE_DEGRADED_COUNT` (default **5**).

The Engine Room **Generated waste** row renders this as "Low waste" or
"Discarding often", with recent unheard segment count, duration in the window,
the dominant reason (shown with an operator-friendly label, e.g. "failed quality
check"), and the rough `estimated_waste_cost_usd` shown as an approximation. When
there are no recent discards the row drops the "mostly …" reason and shows plain
low-waste copy. Admin-only — absent from `/public-status`. Counts are
session-local and reset on restart. Observability only; does not change
scheduling or generation depth.

### Reading recent render timings

`runtime_status.render_timings` is a bounded, admin-only diagnostic trail for
completed producer attempts. It is absent from `/public-status` and never feeds
scheduling, queue admission, or playback. `retention` is the maximum retained
entry count (currently 20); `recent` is newest first. Each entry contains:

- `timestamp` — UTC completion time.
- `kind` — the attempted segment type.
- `outcome` — `produced`, `discarded`, or `failed`; non-produced attempts may
  additionally include `reason` (for example, a stale cutover or a quality-gate
  rejection).
- `total_elapsed_ms` — rounded wall-clock time for the attempt.
- `stages_ms` — rounded durations for any observed `source`, `normalize`,
  `script`, `tts`, `mix`, `quality`, `egress`, and `admission` stages.

Stage measurements are independently observed and can overlap, so their sum is
not a substitute for `total_elapsed_ms`. Every terminal producer branch closes
the current entry immediately, including a quality rejection, so the next
attempt cannot recast it as an unrelated delayed `abandoned` failure. This is
session-local observability only and resets on restart.

### Reading producer headroom

`runtime_status.producer_headroom` shows how full the lookahead queue is relative
to the configured runway target, so a starving queue is visible before it has to
bridge. The fields:

- `queue_depth` — segments currently queued (`-1` if the queue is not yet attached).
- `queue_capacity` — the queue's hard cap.
- `lookahead_target` — the runway target, `max(4, pacing.lookahead_segments)`
  (default `lookahead_segments = 4`); retained as queue-shape context, not the
  headroom decision.
- `buffered_audio_sec` — total seconds of audio already queued in the real
  playback queue, summed from segment durations (plus an active protected
  continuity slot when one exists). Only segments playback would actually
  accept count: a banned/blocklisted song, a companionship cue whose listener
  session has since moved on, or a file that is missing, empty, or not a
  regular file on disk contributes `0` seconds even while it still occupies a
  queue slot, so a queue that looks full on `queue_depth` alone can still show
  a thin `buffered_audio_sec`.
- `runway_floor_sec` — minimum ready-audio runway used by the continuity guard.
- `continuity_slot_sec` — seconds held in the capacity-exempt protected
  continuity slot (`0` when none is reserved, or when the reserved slot itself
  is no longer playback-valid); already included in `buffered_audio_sec`,
  surfaced separately so an operator can see how much of the runway is
  out-of-band safety audio rather than queued program.
- `headroom_ok` — `true` once `buffered_audio_sec >= runway_floor_sec` **and**
  the immediate head of the queue (or the continuity slot, if the queue is
  empty) is itself playback-valid. Unplayable segments never add seconds to
  `buffered_audio_sec`, but they can still sit ahead of playable ones in the
  queue — so the floor can be cleared entirely by playable audio seated
  *behind* an unplayable head, and `headroom_ok` stays `false` until that head
  clears, because playback would hit the bad segment first.
- `reason` — human-readable: `"ready runway"` or `"building runway"`.

The fields are operator-facing observability. The producer's own runway
governor makes the natural-pacing music-vs-speech call from a separate,
simpler seconds count (`_producer_buffered_seconds`) that sums raw queued
segment durations without the blocklist/companionship-session/on-disk
filtering described above — so `buffered_audio_sec` here and the count behind
a pacing decision can diverge when the queue holds an otherwise-unplayable
segment. When natural optional speech (`BANTER`, `AD`, `NEWS_FLASH`,
`STATION_ID`, `TIME_CHECK`) would run below the floor on the governor's count
while the bounded queue can still build more runway, the producer chooses
music instead. If the bounded queue is effectively saturated and still cannot
reach the seconds floor, the due speech is allowed so optional breaks do not
starve forever on short-track stations.

### Reading stream-delivery diagnostics

`runtime_status.stream_delivery` is a **private, admin-only** diagnostic surface
(authenticated `/status` only — it is never added to `/public-status`, and there
is no listener copy or operator control). It proves when the 4-second delivery
cushion (see [Delivery cushion](architecture.md#delivery-cushion-send-ahead-pacing))
absorbed a scheduling delay versus when it was exhausted, and distinguishes an
app-side segment outcome, a global pacing miss, and one lagging listener — all
without retaining any listener or Home Assistant identity.

Shape (all counters and lists are present from boot, zeroed / empty / `idle`
before anything is recorded; `slow_listener_drops.last_drop_at` is `null` until
the first overflow, because no timestamp exists yet):

- `target_lead_ms` — the send-ahead target the live pacer is running at (`4000`).
- `late_threshold_ms` — the lateness that records an event (`50`).
- `session` / `window_15m` — counts of `late`, `underrun`, `overrun_rebased`, and
  `total`, for the whole session and a rolling 15-minute window.
- `recent` — up to 20 coalesced recent pacing events. Each carries a `kind`
  (`late` = a late send the cushion still absorbed; `underrun` = the cushion was
  exhausted; `overrun_rebased` = an overlong stall was bounded to the recovery
  burst and the timeline rebased), timestamp, `lateness_ms`, `remaining_lead_ms`,
  `deficit_ms`, `segment_type`, `playback_epoch`, `listener_count`, a coarse
  generator `phase`/`kind`, and the HA refresh `in_flight` / `foreground_timed_out`
  / `stage` / `stage_elapsed_ms`.
- `recent_stream_outcomes` — up to 20 anonymous completed-send outcomes:
  timestamp, `segment_type`, classified `result`, `bytes_sent`,
  `starting_listener_count`, and `terminal_reason` (`eof`, `skip`, or
  `file_error`; `cancelled` for planned task shutdown; or `aborted` for another
  non-I/O interruption).
- `slow_listener_drops` — `session` / `window_15m` totals and `last_drop_at` for
  queue-overflow drops of a lagging listener (no identifier is retained).
- `ha_refresh` — the current coarse HA projection `stage` (`states_request`,
  `enrichment_wait`, `projection`, `idle`) and `stage_elapsed_ms`.

How to read it: a `late` event means the cushion did its job. An `underrun`
(also a rate-limited device-log warning, so it survives after the bounded status
history rolls over) means a stall exceeded the cushion and was audible; correlate
its coarse generator kind and HA `stage` to tell rendering pressure from an HA
projection. An `overrun_rebased` confirms a long stall was bounded and did not
compound into a catch-up burst. **Privacy exclusion list** — these rows never
contain raw HA states, labels, titles, segment IDs, listener IDs, IP addresses,
or prompt material; only timestamps, counts, durations, coarse kinds, and state
flags.

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

### ElevenLabs V3 host-performance gate

No host currently ships on `eleven_v3`: Marco and Giulia were reverted to V2
after their V3 host-performance audition was rejected (recorded in the tracked
receipt `proof/2026-07-16-v3-host-performance.json`). If a host opts back into
`eleven_v3`, run this gate before an edge release — never infer V3 success from a
provider-key check or an ordinary live segment:

```bash
.venv/bin/python scripts/audition_tts_voices.py --v3-host-performance --providers elevenlabs --dry-run
.venv/bin/python scripts/audition_tts_voices.py --v3-host-performance --providers elevenlabs
```

The second command writes an ignored local manifest under `tmp/voice-auditions/`.
It pairs each host's V2-clean baseline with V3-clean plus its approved cues:
Marco (`energetic`, `curious`, `playful`) and Giulia (`dry`, `curious`,
`playful`). Review the clips for intelligibility, character fit, clean spoken
copy, and artifacts. A tiny local decisions JSON may then be joined with the
manifest using `--host-performance-manifest` and
`--host-performance-decisions`; the resulting tracked receipt contains only
model/cue identifiers, clean/rendered-text hashes, audio checksum/duration,
provider outcome, and controlled human disposition. It never stores raw text,
audio paths, or credentials.

The edge release gate is:

```bash
.venv/bin/python scripts/audition_tts_voices.py --verify-host-performance-gate
```

It fails unless every paired Marco and Giulia row was generated and accepted.
The receipt is immutable by default; use the explicit overwrite flag only when
replacing reviewed evidence. This proves the release candidate, not a running
Home Assistant add-on — update the add-on through its normal image path and
confirm its one planned restart separately.

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

The active First Listen/setup surface is stricter than the general matrix:
`GET /api/setup/status` plus setup recheck, speaker playback, privacy preview,
privacy choice, entity privacy controls, provider check, and key-save actions
require the injected CSRF token and either a literal local/private IP host or
genuine Home Assistant ingress. This prevents DNS-rebinding pages from using
the token they can read from a rebound dashboard. Automation through a custom
hostname must use `X-Radio-Admin-Token`.

## Docker

```bash
docker compose up
```

The `Dockerfile` builds a standalone image with Python 3.11 and FFmpeg. The container runs as a non-root `radio` user. `docker-compose.yml` maps `.env` variables and mounts a persistent volume at `/data` for cache, temporary work, and operator-supplied music in `/data/music`.

`ADMIN_TOKEN` is required in `.env` (the container binds to `0.0.0.0`).

## Home Assistant add-on

The `ha-addon/` directory contains a complete Home Assistant app scaffold. Users add the repo URL in **Settings > Apps > App store > Repositories**, then install "Mamma Mi Radio" from the Apps catalog.

Supervisor's stored app options are the sole durable authority for add-on admin
modes and pacing. Admin saves commit there before live state changes.
`/data/options.json` is a Supervisor-generated, read-only startup projection:
the add-on entrypoint (`ha-addon/mammamiradio/rootfs/run.sh`) reads it, maps the
Supervisor-injected `$SUPERVISOR_TOKEN` to `HA_TOKEN`, overlays AI/TTS provider
secrets from `/config/secrets.env`, and starts uvicorn. Runtime code never writes
the projection directly.

Provider secrets in `/config/secrets.env` win over legacy option values per key.
AI/TTS fields and the Jamendo client ID are absent from both current add-on
schemas. Keys saved by older installs are recovered once from Supervisor's
stored settings via the Supervisor API and persisted into `secrets.env` at first
boot. A legacy Jamendo ID uses its own versioned migration marker, remains
disabled, and must be reviewed in **Motore -> Setup -> Music sources** before
acknowledged non-commercial use. Legacy extractor flags and environment
overrides are coerced off. A mode or pacing selection held only in memory by a
pre-fix build cannot be reconstructed after an update rematerializes an older
Supervisor value. The add-on binds `0.0.0.0` with no admin credential by default
and trusts its own LAN for admin access (see **Admin access model**); set
`admin_token` in the add-on options to require a credential.

The dashboard is accessible via HA ingress (sidebar). The first-run flow shows source readiness, starts the exact Live media source on one selected Home Assistant speaker, asks the operator to confirm audible sound, and only then exposes the filtered Home context preview and choice. AI-host keys are optional and come afterward.

First Listen progress is owner-only setup metadata under `/data/cache/state` in
add-on mode. Its receipt records factual milestones, not the live Home-context
policy; the privacy choice remains in the normal add-on configuration path.
Receipt and install-origin I/O runs asynchronously outside the startup audio
path. Unreadable or disagreeing evidence leaves setup incomplete and privacy
narrow instead of blocking the producer or widening Home access.

When HA context is enabled, the station reads the Home Assistant state snapshot opportunistically before banter, ad, and news-flash generation (so the weather flash grounds in a freshly refreshed forecast), with a default full-state refresh interval of 300 seconds. A normal refresh gets a 2-second foreground wait (20 seconds on the first cold label/weather warm-up); when that wait expires, audio generation immediately uses the last prompt-safe snapshot while one producer-owned HA request continues for up to 30 seconds total. `/api/states`, optional registry metadata, and optional weather enrichment begin together; the optional calls are individually bounded, best-effort, and cannot extend that same total cap. A late valid reply is adopted only before a later eligible host segment, never into rendering or queued audio. At that adoption boundary its age is checked again: a completed snapshot that became older than `max(2 × poll interval, 120 seconds)` while waiting in the mailbox remains visible to the admin as stale, but its ambient prompt details and delayed one-shots stay withheld. The next fresh reply is a resynchronization and deliberately drops delayed full-context events, directives, interrupts, ritual/radio matches, and running gags. Timer interrupts use their independent lightweight entity poll and `timer` provenance, so stale full-context suppression cannot erase a current timer alert while Home context remains enabled. The add-on exposes **Host home context** (`ha_context_enabled`) separately from HA entity publishing: turning it off suspends full-state and timer polling, cancels Home-derived label/scene/memory work, removes unstarted Home-derived breaks, and clears public Casa moments while station entities can continue publishing. Audio already on air may finish to avoid dead air, but a revoked Home-derived segment cannot write post-air memory afterward. It does not send every entity to the script prompt: telemetry/config entities, unavailable states, free-text helpers (e.g. `input_text`), and sensitive domains such as trackers, cameras, and alarms are filtered first. Resident presence (`person.*`) is kept as home/away only, with GPS and identity attributes stripped, so the empty-home mood and explicitly sourced named-resident facts can work without leaking location; stream connections never authorize arrival or return copy. The admin Home context preview shows a sanitized slice of what hosts may use; Mute for future host use stores a local policy under `cache/state/ha_entity_policy.json` and removes that entity from future prompts, public Casa moments, reactive/timer triggers, label generation candidates, and running-gag inputs. It never interrupts audio already on air; when a muted entity — or one whose room-presence personal-moment permission is turned back off — supplied a selected Home Context Director fact, its matching unstarted host break is removed from the queue. The director gives casual banter one allowlisted ambient fact at most, holds its topic for 30 minutes after stream start, and can use a room-presence binary sensor only after the explicit preview permission; no extra HA polling is performed. This holds even when a HA refresh times out and the producer airs on a last-known context (`apply_entity_mute_policy` re-applies the live policy to that stale copy, since it bypasses `fetch_home_context`'s own filtering), and muting also purges any running-gag material already tallied for that entity before the mute, so a moment observed pre-mute cannot still be offered as a callback afterward. The remaining entities are scored and capped before prompt assembly. That same filtered interaction slice can also be included in the post-air memory extractor after generated banter streams cleanly, so future host memory is based on the final station script instead of queued drafts. The practical privacy/performance levers are muting specific entities, turning Host home context off when house state should not enter prompts or timer reads, increasing `ha_context_poll_interval`, or running without script-provider credentials to avoid durable AI memory extraction. When Home context, HA access, and an Anthropic key are all active, the display names and room assignments for non-sensitive, unmuted entities can also be sent to Anthropic once to generate radio-friendly labels; no sensor values, presence, or location are included, and the results are cached locally (`cache/ha_label_catalog.json`, owner-only) so each device is only looked up once. Home mood naming stays on the local heuristic ladder unless `MAMMAMIRADIO_HA_MOOD_LLM=true`; that experimental LLM path uses only the budgeted HA context slice, refreshes the generated scene name at most once per `MAMMAMIRADIO_HA_MOOD_TTL_SECONDS` (keeping the last scene on air while a refresh runs, with bounded staleness), and falls back to the ladder on disabled config, missing keys, timeout, rejection, invalid output, or while the station's Anthropic circuit breaker is tripped. The admin Engine Room shows fact-free director diagnostics and privacy filter counts; `/public-status` exposes listener-safe Casa moments only while Home context is enabled.

## Home Assistant entities

The preferred HA surface is the HACS integration under
`custom_components/mammamiradio`: it owns the registered
`media_player.mammamiradio`, exposes native controls, provides diagnostics and
Repairs, and adds `media-source://mammamiradio/live` for casting.

The add-on also pushes a basic `media_player.mammamiradio` plus sensor state
after each segment transition. The media-player heartbeat continues every 30
seconds for add-on-only setups; unchanged auxiliary sensor payloads are deduped
between bounded recovery heartbeats to reduce HA Core REST churn. When the HACS
integration is installed, turn `ha_media_player_push` off so its registered
`media_player.mammamiradio` owns the id instead of the REST-pushed ghost; the
sensors keep flowing either way.

These entities answer a much looser question than the control room does.
`binary_sensor.mammamiradio_on_air` reports only "has the operator stopped the
station" — it derives from the persisted stop marker alone, so it stays `on`
through queue starvation, a dead playback task, and even prolonged silence with
listeners connected. `media_player.mammamiradio` is nearly as loose: its state
derives from the stop marker plus a sticky `now_streaming` row that survives the
end of a segment, republished by a 30-second heartbeat. The control room's
`station_on_air` requires a listener to have actually accepted audio (see
"Diagnosing provider fallbacks" above), so the entity and the admin header can
disagree for as long as a failure lasts, not just for a moment. For automations
that should only fire on audio a listener actually received, poll `/status` for
`station_on_air`; treat the entities as "the station is not paused", nothing
stronger.

| Entity ID | Type | State values | Key attributes |
|---|---|---|---|
| `media_player.mammamiradio` | media_player | `playing` / `idle` | `icon: mdi:radio`; pushed by the add-on by default; turn `ha_media_player_push` off when the HACS integration owns it |
| `sensor.mammamiradio_segment_type` | sensor | `music` / `banter` / `ad` / `news_flash` / `station_id` / `sweeper` / `time_check` / `off` | dynamic `icon` matching the current segment type |
| `sensor.mammamiradio_listeners` | sensor | integer | `icon: mdi:account-group`; `unit_of_measurement: listeners` |
| `binary_sensor.mammamiradio_on_air` | binary_sensor | `on` / `off` | `icon: mdi:broadcast` |

All four entities are labelled with the resolved station identity (`Mamma Mi Radio` by default, or the add-on `station_name` / `STATION_NAME` override): the media player's `friendly_name` is the station name itself (and it doubles as `media_artist` for non-music segments), while the sensors read `<station> Segment Type`, `<station> Listeners`, and `<station> On Air`. `/api/setup/status` exposes the same identity preview plus the stable IDs under `identity.stable_ids`. Entity IDs, unique IDs, media-source paths, and the `mammamiradio_*` attribute keys stay fixed regardless of the display name, so existing automations and dashboards keep working.

`entity_picture` is always an absolute image URL: the real album cover while a track plays, and the station logo for host talk, ads, and idle. The logo fallback matters because the HA media card keeps the last cover when `entity_picture` is removed — so without it the previous track's art would linger through a news flash. Override the logo per station with `artwork_url` under `[brand]` in `radio.toml` (must be an absolute `http(s)` URL; a relative path is rejected because HA resolves `entity_picture` against its own origin). Blank uses the bundled station logo.

**Cold-start note:** after a HA or addon restart, the media player reappears within 30 seconds via the heartbeat. Unchanged auxiliary sensors are republished by the bounded recovery heartbeat, or sooner when their state changes. Automations triggering on `state_changed` may miss the first segment after restart — add an `initial_state: playing` guard if needed.

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

**Note:** REST-pushed entities appear in Developer Tools → States but not in the HA entity registry (Integrations page). HA Assist, Repairs, diagnostics, and media-source browsing require the HACS integration for registry visibility.

## What is still not documented because it does not exist yet

- no systemd unit
- no launchd plist
- no nginx or Caddy config
- no Fly/Render/Netlify config (public preview deployment is a future idea — blocked on cost and music copyright)
