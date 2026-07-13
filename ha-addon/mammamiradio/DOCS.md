# Mamma Mi Radio — HA Add-on Runbook

Operational guide for the Home Assistant add-on. Covers architecture, failure modes, and recovery.

## First run in 4 steps

This app requires **Home Assistant OS**. Home Assistant Container does not include Apps; if **Settings → Apps** is missing, use the [Docker alternative](../../README.md#docker-alternative) instead.

### 1. Add the repository

In Home Assistant: **Settings → Apps → App store → ⋮ → Repositories**.
Paste `https://github.com/florianhorner/mammamiradio`, select **Add**, open **Mamma Mi Radio** in the store, and select **Install**.

### 2. Start the add-on

Click Start. Watch the log for:
- `[mammamiradio] Starting add-on...`
- `[mammamiradio] Home Assistant API access configured via Supervisor`
- `Producer started`

First boot can take 30-90 seconds while chart tracks are downloaded and cached. No AI key is required: without one, the hosts use stock copy and fallback voices. Music is a separate requirement — live charts need outbound access, or configure a Jamendo client ID in the app's advanced options. A successful start shows `Producer started` in the log and returns `"ready": true` from `/readyz`.

### 3. Open the Web UI and listen

Click Open Web UI or navigate to the ingress URL in the sidebar. In add-on mode, ingress opens the admin control room first. Use the setup strip's listener action, or open `/listen`, to hear the station before adding keys.

### 4. Add one AI host key, then review home context

Use **Motore → Setup → AI hosts** to save either `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. One key is enough to unlock generated host banter and fake ad breaks. The admin writes the key to `/config/secrets.env`, applies it live, and checks the provider without interrupting audio.

After an AI host key is ready, **Home context preview** shows the filtered Home Assistant entities the hosts may use. Supervisor access is automatic in the add-on; the preview is where you inspect what the AI can see and mute any entity locally. Casual host breaks use at most one safe rotating cue, while room-presence is a separate default-off **personal on-air moment** permission. Muted entities are kept out of future prompts, public Casa moments, reactive triggers, generated labels, and running-gag inputs; current audio finishes normally, while an unstarted queued host break carrying that entity's selected director fact is removed.

Premium voice keys are optional and separate from the first AI-host unlock.

The Home Assistant controls are separate too. **Enable Home Assistant Integration** is the master connection for entity publishing, optional host context, and timer interrupts. **Host home context** controls the full filtered state polling used for AI prompts; turn it off to keep the integration, entity publishing, and timer interrupts while keeping home state out of host prompts. Both default to on, and the prompt-context refresh interval defaults to 300 seconds.

The admin stores provider credentials in `/config/secrets.env` inside the add-on config folder. Supported keys are
`ANTHROPIC_API_KEY` (AI banter and ads), `OPENAI_API_KEY` (AI banter, ads, and OpenAI
TTS voices), `AZURE_SPEECH_KEY` plus `AZURE_SPEECH_REGION` (official Azure Italian voices), and
`ELEVENLABS_API_KEY` (custom ElevenLabs voices when configured in `radio.toml`). Provider fields no
longer appear in the add-on Configuration tab; keys saved there by older versions are recovered from
the add-on's stored settings and moved into `/config/secrets.env` automatically the first time the
updated add-on starts.

Because provider keys are no longer add-on options, a fresh install never puts them where
`ha addons info <slug>` can print them. An install upgraded from an older version may still carry
previously saved key values in Home Assistant's stored add-on settings; opening the add-on's
Configuration tab and pressing Save once replaces the stored settings with only the current fields,
clearing the old key values. When sharing diagnostics, redact the options block:
`ha addons info <slug> --raw-json | jq 'del(.data.options)'`.

`secrets.env` accepts `KEY=VALUE` lines, optional `export KEY=VALUE`, whitespace around keys or
values, single or double quoted values, values containing `=`, UTF-8 BOM, and CRLF endings. Full-line
comments beginning with `#` are ignored; inline comments are treated as part of unquoted values.

Before committing to a voice mix, run a local audition from the repository:

```bash
.venv/bin/python scripts/audition_tts_voices.py --include-catalog --providers all
```

The command writes clips and a `manifest.json` under `tmp/voice-auditions/`.
Providers without credentials are listed as skipped instead of being hidden by
the runtime Edge fallback.

Without an AI key, the station runs in Demo Mode: host writing falls back to stock copy and fallback voices. Demo Mode does not bundle a song library; in the Home Assistant app, music still comes from reachable charts or Jamendo. The bundled recovery clip covers thin-queue moments but is not a rotation.

## Architecture

```
HA Supervisor
  |
  +-- nginx ingress proxy (strips /api/hassio_ingress/<token>/ prefix)
  |     |
  |     +-- uvicorn :8000 (mammamiradio FastAPI app)
  |           |
  |           +-- producer task (generates segments: music, banter, ads)
  |           +-- playback task (streams segments to listeners)
  |
  +-- /data/ (persistent across restarts)
        +-- cache/   (downloaded track audio — survives restarts)
        +-- tmp/     (rendered segments — ephemeral)
```

## Startup sequence

1. `run.sh` reads `/data/options.json`, overlays provider secrets from `/config/secrets.env`, and exports env vars for the addon runtime.
2. `run.sh` maps `SUPERVISOR_TOKEN` to `HA_TOKEN`, sets `HA_URL=http://supervisor/core`, maps **Enable Home Assistant Integration** to `HA_ENABLED`, and maps the separate Host home context options to `MAMMAMIRADIO_HA_CONTEXT_ENABLED` / `MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL`.
3. `run.sh` enables yt-dlp (`MAMMAMIRADIO_ALLOW_YTDLP=true`) and starts uvicorn.
4. `mammamiradio/main.py` loads `radio.toml` and validates config.
5. `fetch_playlist()` downloads Italian chart tracks via yt-dlp (first boot: slow, cached after).
6. Producer and playback tasks start once the first segment is ready.

**Startup timeout**: `config.yaml` sets `timeout: 240`. First boot can take 60–120 seconds on slower hardware (Raspberry Pi + yt-dlp download + FFmpeg transcode). If the addon is killed during startup, check the log for `Container terminated` — usually means the download took too long.

**Recovery**: If startup times out, restart the addon. Subsequent boots are fast because tracks are cached in `/data/cache/`.

## Failure modes and recovery

### Stream is repeatedly playing recovery audio

**Symptom**: Ingress URL loads, but logs show repeated source-acquisition failures and recovery/continuity clips rather than music.

**Causes**:
1. yt-dlp rate-limited or denied by YouTube — the failed track is marked unavailable and the station uses its non-silent recovery ladder
2. FFmpeg not found on PATH
3. Network blocks outbound connections to YouTube

**Recovery**: Keep the add-on running while you collect the relevant log lines. Check that Home Assistant can reach the configured music source, and install the latest released add-on update if one is available. If the problem needs a code fix, share the logs with the project; the supported path is `branch → PR → merge → CI builds image → add-on update`. When Home Assistant offers that image, choose **Update** once at a planned moment.

Please leave the running add-on intact: do not SSH in to edit container or runtime files, bypass the audio quality gate, delete its live cache, or restart it repeatedly as an experiment. Those changes disappear on the next update and can turn a recoverable audio problem into a longer interruption.

### TTS banter not generating

**Symptom**: Log shows `TTS synthesis failed` or `edge-tts connection error`. Banter falls back to stock copy or silence.

**Cause**: `edge-tts` requires outbound websocket to Microsoft's TTS API. If your HA instance blocks outbound websockets, TTS fails silently and the producer falls back to stock copy or silence.

**Fix**: This is a network policy issue. The station still plays music. If you need live AI banter, ensure outbound websocket traffic is allowed.

### Ingress 404s (all API calls return 404)

**Symptom**: Dashboard loads but shows no data. Log floods with `GET /api/hassio_ingress/.../status 404`.

**Cause**: Double-prefixed URLs in the frontend. This was fixed in v2.2.0. If you see this, you are on an old image.

**Fix**: Update the addon to the latest version.

### "/data is not writable" warning

**Symptom**: Log shows `WARNING: /data is not writable` and falls back to `/tmp/mammamiradio-data`.

**Cause**: Supervisor permissions issue. State will not persist across restarts.

**Fix**: Fully restart the addon (stop → start, not just restart). If persistent, check that the addon has correct permissions in Supervisor.

### HA context never appears in banter

**Symptom**: Hosts never reference home state even though HA is enabled.

**Check**:
1. Log should show `Home Assistant API access configured via Supervisor`
2. Look for `Fetched HA context: N entities` — if N=0, no entities matched the filter
3. Look for `Failed to fetch HA context` — network or auth error

**Note**: `HA_URL` is set to `http://supervisor/core` by run.sh. The app appends `/api/states` itself. Do not override this.

### Producer stuck after first banter cycle

**Symptom**: Music plays, first banter completes, then silence.

**Cause**: API key is invalid or quota exceeded. The producer falls back to demo clips but they may be exhausted.

**Fix**: Verify your `ANTHROPIC_API_KEY` in `/config/secrets.env` is valid. Legacy add-on installs may still use `anthropic_api_key` in options. Check the log for `AuthenticationError` or `RateLimitError`.

### Accessing the station directly

Port 8000 serves three URLs from your home network:

| URL | Who uses it | Notes |
|-----|-------------|-------|
| `http://<ha-ip>:8000/` | Listeners (guests, family) | Public — no login needed |
| `http://<ha-ip>:8000/admin` | You (operator) | LAN-trusted — no token needed |
| `http://<ha-ip>:8000/stream` | Media players, mpv, VLC | Raw MP3 stream |

If you configured a custom `admin_token` in the add-on options, direct `/admin` access requires that token via `X-Radio-Admin-Token` header. From outside your home network, `/admin` returns 403.

## Key files

| File | Purpose |
|------|---------|
| `config.yaml` | Addon metadata, options schema, network config |
| `build.yaml` | Base images per arch, build args |
| `Dockerfile` | Image: Alpine + Python + FFmpeg + mammamiradio |
| `rootfs/run.sh` | Entrypoint: env var mapping, uvicorn launch |
| `radio.toml` | Station config defaults (hosts, pacing, ads) |

## Env var flow

```
/config/secrets.env (provider secrets, preferred)
  |
  +-- run.sh reads KEY=VALUE lines, exports non-empty values
  |     ANTHROPIC_API_KEY, OPENAI_API_KEY,
  |     AZURE_SPEECH_KEY, AZURE_SPEECH_REGION, ELEVENLABS_API_KEY
  |
  +-- /data/options.json (HA UI options; provider fields are not in the schema anymore)
  |     Supervisor drops schema-removed keys from this file on start; provider keys
  |     saved by older versions are recovered once via the Supervisor API
  |     (/addons/self/info) and persisted into /config/secrets.env at first boot.
  |     STATION_NAME, MAMMAMIRADIO_QUALITY (from quality_profile, default balanced),
  |     ADMIN_TOKEN (blank => LAN-trusted, no token required),
  |     HA_ENABLED (from enable_home_assistant; master HA integration switch),
  |     MAMMAMIRADIO_HA_CONTEXT_ENABLED (from ha_context_enabled;
  |       turn off to stop AI prompt-context polling while keeping HA integration),
  |     MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL (default 300 seconds),
  |     MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH, MAMMAMIRADIO_SUPER_ITALIAN,
  |     MAMMAMIRADIO_CHAOS_MODE, MAMMAMIRADIO_FESTIVAL_MODE,
  |     MAMMAMIRADIO_BROADCAST_CHAIN, MAMMAMIRADIO_GUEST_HOST,
  |     MAMMAMIRADIO_PACING_SONGS_BETWEEN_BANTER,
  |     MAMMAMIRADIO_PACING_SONGS_BETWEEN_ADS,
  |     MAMMAMIRADIO_PACING_AD_SPOTS_PER_BREAK,
  |     JAMENDO_CLIENT_ID
  |
  +-- run.sh maps Supervisor token
  |     SUPERVISOR_TOKEN -> HA_TOKEN, HA_URL=http://supervisor/core
  |
  +-- run.sh sets addon defaults
  |     MAMMAMIRADIO_BIND_HOST=0.0.0.0, MAMMAMIRADIO_PORT=8000,
  |     MAMMAMIRADIO_CACHE_DIR=/data/cache, MAMMAMIRADIO_TMP_DIR=/data/tmp,
  |     MAMMAMIRADIO_ALLOW_YTDLP=true
  |
  +-- config.py reads env vars, applies addon overrides
        homeassistant.url -> http://supervisor/core
        ha_token <- SUPERVISOR_TOKEN (addon mode overrides HA_TOKEN)
```

## Ingress URL flow

```
Browser: http://ha:8123/api/hassio_ingress/<token>/
  |
  +-- HA Supervisor nginx strips prefix, forwards GET / to addon:8000
  |
  +-- Trusted ingress returns the admin control room at /
  |     - The setup strip opens the listener at /listen
  |     - Static route attributes are rewritten under <prefix>
  |     - JS fetch calls stay under /api/hassio_ingress/<token>/...
  |
  +-- Browser fetches <prefix>/stream
  |     -> HA proxy passes through streaming MP3 response
  |     -> Audio plays in browser
```

**Critical rule**: `_inject_ingress_prefix` must NEVER rewrite JS string literals. Only static HTML attributes are rewritten.

## Updating and releasing the add-on

**Operators:** When Home Assistant offers an update, choose **Update** at a planned moment. The update pulls the published image and performs the one expected add-on restart; no live file edits are needed.

**Maintainers:** Follow the canonical [HA add-on release runbook](../../docs/runbooks/ha-addon.md#the-release-chain). It owns the synchronized version and changelog files, `make pre-release`, the protected branch and PR landing path, CI image promotion, and post-release verification. Do not duplicate or shortcut that contract here.

## Renaming the station

The station name is the operator-facing identity people see and hear. If you
call it "Radio Florian", the listener page, stream metadata, admin setup preview,
Home Assistant friendly labels, and the default generated station IDs and
sweepers use "Radio Florian" naturally, the way a real station would.

**To rename:**

1. In the add-on Configuration tab, set `station_name` to your chosen name (e.g. `Radio Florian`).
2. Click Save, then restart the add-on.
3. Reopen the add-on. The admin setup panel shows an **Identity** preview for
   what listeners hear, what listeners see, and what Home Assistant shows.
4. Within a few minutes of playback, new generated IDs, sweepers, and host copy
   will start using the new name.

The stable add-on slug, integration domain, entity IDs, and media-source path do
not change: `mammamiradio`, `media_player.mammamiradio`,
`sensor.mammamiradio_*`, `binary_sensor.mammamiradio_on_air`, and
`media-source://mammamiradio/live` remain the automation contract.

Custom sonic-brand copy in `radio.toml` is preserved deliberately. If you wrote
your own `full_ident` or sweeper lines, the setup Identity preview keeps them and
flags that custom copy may still mention the old name. Blank or default copy is
regenerated from the new station name.

You can also set the name via environment variable:
`STATION_NAME=Radio Florian`.

## Home Assistant entities

The add-on automatically pushes a basic `media_player.mammamiradio` plus sensor
state after each segment transition. The media-player heartbeat continues every
30 seconds for add-on-only setups; unchanged auxiliary sensor payloads are
deduped between bounded recovery heartbeats — no `configuration.yaml` changes
required, so an add-on-only setup gets a media-player tile out of the box.

For a registered, controllable `media_player.mammamiradio` and the native
`media-source://mammamiradio/live` stream source, install the HACS integration in
`custom_components/mammamiradio`. When you do, turn **On-air media player push**
off (Add-on → Configuration) so the add-on's push and the integration don't fight
over the same entity; the `sensor.mammamiradio_*` / `binary_sensor` entities keep
flowing either way.

| Entity ID | Type | State values | Key attributes |
|---|---|---|---|
| `media_player.mammamiradio` | media_player | `playing` / `idle` | `icon: mdi:radio`; pushed by the add-on by default; turn `ha_media_player_push` off when the HACS integration owns it |
| `sensor.mammamiradio_segment_type` | sensor | `music` / `banter` / `ad` / `news_flash` / `station_id` / `sweeper` / `time_check` / `off` | dynamic `icon` matching the current segment type |
| `sensor.mammamiradio_listeners` | sensor | integer | `icon: mdi:account-group`; `unit_of_measurement: listeners` |
| `binary_sensor.mammamiradio_on_air` | binary_sensor | `on` / `off` | `icon: mdi:broadcast` |

**Cold-start note:** after a HA or add-on restart, the media player reappears within 30 seconds via the heartbeat. Unchanged auxiliary sensors are republished by the bounded recovery heartbeat, or sooner when their state changes. Automations triggering on `state_changed` may miss the first segment after restart — add an `initial_state: playing` guard if needed.

**Lovelace media card** with the HACS integration:

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

**Note:** REST-pushed entities appear in Developer Tools → States but not in the HA entity registry (Integrations page). HA Assist and media-source browsing require the HACS integration for full registry visibility.

## Tiers

The dashboard shows one of three tiers based on your configuration:

| Tier | What you hear | What it needs |
|------|--------------|---------------|
| Demo Radio | Stock host copy and fallback voices over any available music source | No AI key; reachable charts or Jamendo still provide the music |
| Full AI Radio | Live AI banter and ads over the configured music source | `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` in `/config/secrets.env`, plus reachable charts or Jamendo |
| Connected Home | Above + home-aware banter | AI host key + prompt-safe Home Assistant context available |
