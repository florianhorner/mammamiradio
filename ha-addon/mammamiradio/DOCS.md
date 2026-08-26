# Mamma Mi Radio — HA Add-on Runbook

Operational guide for the Home Assistant add-on. Covers architecture, failure modes, and recovery.

## First run: hear one real speaker

This app requires **Home Assistant OS**. Home Assistant Container does not include Apps; if **Settings → Apps** is missing, use the [Docker alternative](../../README.md#docker-alternative) instead.

### 1. Add the repository

In Home Assistant: **Settings → Apps → App store → ⋮ → Repositories**.
Paste `https://github.com/florianhorner/mammamiradio`, select **Add**, open **Mamma Mi Radio** in the store, and select **Install**.

### 2. Start the add-on

Click Start. Watch the log for:
- `[mammamiradio] Starting add-on...`
- `[mammamiradio] Home Assistant API access configured via Supervisor`
- `Producer started`

The add-on starts from its attributed 12-track starter catalog: no music
provider key, download, or outbound network is required. Without an AI key, the
hosts use stock copy and fallback voices. Operator-supplied MP3s can also live
in persistent `/data/music`, and packaged recovery audio can prove the speaker
transport while a damaged music source is repaired without reporting that
source as healthy. A successful process start shows `Producer started` in the
log. `/readyz` remains HTTP `503` with `status: "starting"` until a listener
actually accepts audio; queued work and elapsed startup time do not make the
station ready by themselves.

### 3. Install the HACS integration

The HACS integration is optional. First Listen proof is hearing the station on
this device in the add-on Web UI. Install the [Mamma Mi Radio HACS
integration](../../docs/integrations/ha-integration.md#install-the-hacs-integration-for-ha-native-playback)
when you want Home Assistant speakers to play
`media-source://mammamiradio/live`, then restart Home Assistant once.

The setup strip's listener action and `/listen` remain available as the audience
surface. Once any listener accepts the first audio bytes, `/readyz` returns HTTP
`200` with `status: "ready"`.

### 4. Follow First Listen on this device

Click **Open Web UI** or use the ingress sidebar entry. A fresh unfinished
install opens directly on **First Listen** before the control room. Follow its
vertical path; completed and existing installs keep their normal control-room
landing, with review and repair under **Motore → Setup**:

1. The opening card leads with a 27-second authored mini-show: an original music
   bed, a privacy-aware Marco/Giulia welcome, then a handoff to the live stream.
   No AI key or Home context is used. Source truth for live charts, Jamendo,
   local music, bundled demo music, and recovery cover says whether primary
   music, recovery cover, or a music repair follows the opening. Recovery audio
   can keep the stream audible, but it is not a music rotation; bundled demo
   music is not a promised song library.
2. Select **Start sound check**. Confirm **Yes, I hear it** only after you hear
   the opening, or **Not yet** for
   [this-device repair](../../docs/troubleshooting.md#first-listen-does-not-play-on-this-device).
   Home Assistant speakers remain an
   [optional later route](../../docs/integrations/ha-integration.md#optional-play-it-on-a-home-assistant-speaker).

See [First-listen repair](../../docs/integrations/ha-integration.md#first-listen-repair) if a later Home Assistant speaker stays quiet.

### 5. Make the privacy choice; add AI later

On a fresh install, **Host home context** is omitted from saved add-on options
and remains off. After you confirm you heard the station on this device,
select **Keep Home private** without reading Home state, or select **See what
the hosts would receive** before **Let Marco and Giulia use these details**.
The preview is a fresh, detached Home
Assistant read: it is not published into host scripts and is not sent to an AI
provider. A preview containing only generic daylight is disclosed as
ambient-only and not meaningful personalization; keeping it private is the
recommended path. For a useful preview, inspect the filtered entities and mute
any entity locally if needed. Enabling requires that fresh preview.

If that live privacy choice applies but the setup review cannot be saved, the
choice is not rolled back. The private path offers **Save private choice again**
without reading Home state. An enabled choice remains active but requires a
fresh filtered preview before **Save shared choice again**. AI-host setup remains
locked until the review receipt is saved.

Under **Optional enhancement**, select **Set up new conversations** to save
either `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. One key unlocks generated host banter and fake ad breaks. The
admin writes the key to `/config/secrets.env`, applies it live, and checks the
provider without interrupting audio. First audio never needs it.

AI-host and premium-voice readiness are separate. Anthropic or OpenAI completes
the AI-host step; OpenAI also provides a premium voice path. Azure Speech and
ElevenLabs are voice-only and leave AI hosts optional. Azure is ready only when
both its key and region are present; one without the other is reported as
incomplete rather than ready.

When Home context is enabled, casual host breaks use at most one safe rotating
cue, while room-presence is a separate default-off **personal on-air moment**
permission. Muted entities are kept out of future prompts, public Casa moments,
reactive triggers, generated labels, and running-gag inputs; current audio
finishes normally, while an unstarted queued host break carrying that entity's
selected director fact is removed.

Premium voice keys are optional and separate from the first AI-host unlock.

The Home Assistant controls are separate too. **Enable Home Assistant Integration** is the master connection for entity publishing, optional host context, and timer interrupts. **Host home context** controls the filtered state and timer polling used for host programming; keep it off to retain the integration and entity publishing while suspending Home-state reads, timer interrupts, and Home-derived host work. The integration defaults on. On a fresh add-on install, Home context has no declared default and stays off until first audio is confirmed and the explicit First Listen choice is recorded; the prompt-context refresh interval defaults to 300 seconds once enabled.

The admin stores provider credentials in `/config/secrets.env` inside the add-on config folder. Supported keys are
`ANTHROPIC_API_KEY` (AI banter and ads), `OPENAI_API_KEY` (AI banter, ads, and OpenAI
TTS voices), `AZURE_SPEECH_KEY` plus `AZURE_SPEECH_REGION` (official Azure Italian voices), and
`ELEVENLABS_API_KEY` (custom ElevenLabs voices when configured in `radio.toml`). AI/TTS provider fields no
longer appear in the add-on Configuration tab; keys saved there by older versions are recovered from
the add-on's stored settings and moved into `/config/secrets.env` automatically the first time the
updated add-on starts.

Because provider keys are no longer add-on options, a fresh install never puts them where
`ha addons info <slug>` can print them. An install upgraded from an older version may still carry
previously saved key values in Home Assistant's stored add-on settings. The first successful start of
the updated add-on recovers those values into `/config/secrets.env`; a successful admin mode/pacing
save can perform the same migration before it replaces the stored settings with only current fields.
A Configuration-tab save cannot run the app's migration, so on an upgraded install wait for successful
startup recovery before changing Configuration. When sharing diagnostics, redact the options block:
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

Without an AI key, the station runs in Demo Mode: host writing falls back to
stock copy and fallback voices over the complete offline starter catalog. The
First Listen mini-show makes the station identity and both stock hosts audible
immediately; the bundled recovery clip covers thin-queue moments but is not a
rotation.

### Keeping a moment

Everything the station records is on a timer: a shared clip is deleted after a
day, and the written record of what was said after two weeks. **Keep this** is
the exception. The button sits on the on-air console in **Diretta**, next to
Skip, and appears while the hosts are talking and for a few seconds after a
break ends, while the music is back on. Pressing it saves that piece for good
and copies a link to it.

A song cannot be kept, and neither can a break that opens over the end of one:
those recordings belong to whoever made them, and a kept link has no expiry.
Everything you have kept is listed under **Archivio → Kept moments**, where you
can play one or remove it. The shelf holds 200; kept audio lives in
`/data/cache/keepsakes/` and survives a backup and restore.

Kept audio plays without a password, but the link the button copies is the one
you are browsing on. When you reach the control room through Home Assistant, that
link works for anyone with access to your Home Assistant. To send it further,
copy the address from the station's own port instead.

### Optional Jamendo music

Open **Motore**, then **Setup**, then **Music sources** to turn Jamendo on. It is off by
default and is not required for a complete rotation. Enable it and confirm the
current non-commercial-use acknowledgement. No signup is needed; the add-on
includes Jamendo access.

Your own client ID is optional. It takes precedence over shared access and uses
independently authorized access; the panel shows which one is active. Clearing
it returns the add-on to shared access. A legacy client ID is
migrated into the private settings file once, but remains disabled until the
acknowledgement is made in the app. The client ID is absent from both add-on
Configuration schemas.

Jamendo preparation is deliberately transient: the add-on holds at most one
lease and one audio artifact, inserts at most one prepared track after two
starter/local tracks, and deletes that artifact after play or cancellation.
Jamendo bytes and lease metadata do not enter `/data/cache`, SQLite, rescue,
handoff, clip, derivative, or restart state. License and source facts shown by
the app are provider-reported rather than a clearance verdict. Provider
confirmation for this station model remains pending; an adverse written reply
disables the integration pending reassessment. See the canonical
[music-source and rights guide](../../docs/music-sources.md).

## Architecture

```
HA Supervisor
  |
  +-- stored app options (durable admin modes and pacing)
  |     +-- materializes /data/options.json for startup
  |
  +-- nginx ingress proxy (strips /api/hassio_ingress/<token>/ prefix)
  |     |
  |     +-- uvicorn :8000 (mammamiradio FastAPI app)
  |           |
  |           +-- producer task (generates segments: music, banter, ads)
  |           +-- playback task (streams segments to listeners)
  |           +-- packaged starter catalog (read-only, attributed music)
  |
  +-- /data/ (persistent across restarts)
        +-- cache/   (eligible local/generated audio — survives restarts)
        |     +-- keepsakes/ (moments kept with "Keep this" — never expire)
        +-- music/   (operator-supplied MP3s)
        +-- tmp/     (rendered segments — ephemeral)
```

Jamendo's one prepared artifact is transient and is excluded from both
persistent paths above.

Supervisor's stored app options are the sole durable authority for Super
Italian, Chaos, Festival, AI Quality, On-Air Sound, and pacing. Control-room
saves commit there before the running station changes. `/data/options.json` is
a Supervisor-generated, read-only startup projection; the app never writes it
directly. A selection held only in memory by a pre-fix build cannot be
reconstructed after an update rematerializes an older Supervisor value.

## Backups and restores

Home Assistant can back up the Mamma Mi Radio app while the station keeps
playing.

- **Keeps playing:** the app does not stop for a backup.
- **Stays with you:** app settings, provider keys, station memory and state,
  retained history, moments you kept with **Keep this**, and files stored in
  `/data/music`.
- **Builds again:** temporary renders, downloaded and normalized cache audio,
  share clips, and restart handoff audio. The restored station may take a little
  longer to refill these caches on its first run.

Files in `/data/music` are your local music library: the station reads MP3s
from that folder as a music source, and a restore brings the library back
ready to play.

A hot backup copies retained files while the station is active, so it is not a
copy taken from one single exact moment. After a restore, confirm
your settings and key presence without sharing the keys, then let the app
rebuild its audio cache and wait for the station to become ready. If the app
cannot start or its station memory is missing, keep that restored copy stopped
and restore a known-good backup instead of repeatedly restarting it or editing
its files by hand.

## Startup sequence

1. Supervisor materializes the read-only `/data/options.json` startup projection; `run.sh` reads it, overlays provider secrets from `/config/secrets.env`, and exports env vars for the addon runtime.
2. `run.sh` maps `SUPERVISOR_TOKEN` to `HA_TOKEN`, sets `HA_URL=http://supervisor/core`, maps **Enable Home Assistant Integration** to `HA_ENABLED`, and maps the separate Host home context options to `MAMMAMIRADIO_HA_CONTEXT_ENABLED` / `MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL`. A missing `ha_context_enabled` key remains omitted so a fresh install can start with context off until Setup records the operator's choice.
3. `run.sh` forces external extraction off and starts uvicorn. Both Stable and
   Edge omit the `yt-dlp` distribution, module, and executable; legacy
   enablement settings are ignored.
4. `mammamiradio/main.py` loads `radio.toml`, validates the packaged starter
   manifest, and makes its direct pre-normalized files available.
5. Producer and playback tasks start from starter/local music. All twelve
   starter tracks complete before any starter track repeats.
6. If Jamendo was explicitly enabled and acknowledged, its bounded preparation
   may run in the background without delaying base music.

**Startup timeout**: `config.yaml` sets `timeout: 240`. Starter playback does
not wait on an external music provider. If the add-on is killed during startup,
collect the log around `Container terminated` and the starter-manifest/audio
validation messages before changing runtime files.

**Recovery**: Leave the running add-on, media gate, container filesystem, and
`/data/cache` intact while collecting diagnostics. A released starter catalog
is packaged in the image rather than repaired by a network download.

## Failure modes and recovery

### Start returns an error and the station stays paused

**Symptom**: pressing Start answers `503` and the station remains paused across
add-on restarts.

**Cause**: Stop writes a durable marker, and Start refuses to clear it until it
has reserved audio that can play immediately. A restart re-reads the marker, so a
paused station stays paused on purpose. When no recovery audio is installed at
all, the response offers **Force Start**, which is an explicit corrupt-install
escape rather than an automatic retry.

**Recovery**: full procedure, including what to inspect and when Force Start is
the right answer, is in
[docs/troubleshooting.md](https://github.com/florianhorner/mammamiradio/blob/main/docs/troubleshooting.md)
under "Stop or Resume returns 503". Inspect the installed image read-only; do not
patch or restart the running container as a test.

### `/readyz` stays at `503 starting`

**Symptom**: the add-on is running and the log shows `Producer started`, but
`/readyz` never reaches `200 ready`.

**Cause**: usually not a fault. Readiness means a listener queue actually
accepted audio, so a station nobody is tuned into stays `starting` by design.
Queued work and elapsed startup time do not make it ready.

**Recovery**: open the listener page and play the stream. If it still does not
flip after a listener is connected and audio is audible, follow the silence
checks in
[docs/troubleshooting.md](https://github.com/florianhorner/mammamiradio/blob/main/docs/troubleshooting.md).

### Stream is repeatedly playing recovery audio

**Symptom**: Ingress URL loads, but logs show repeated starter admission
failures and recovery/continuity clips rather than music.

**Causes**:
1. the packaged starter file does not match its manifest hash or evidence entry
2. FFmpeg/FFprobe is missing or rejects a packaged audio file
3. an incomplete or wrong-architecture image was installed

**Recovery**: Keep the add-on running while you collect the relevant log lines,
including the reported manifest track ID and validation failure. Install the
latest released add-on update if one is available. If the problem needs a code
fix, share the logs with the project; the supported path is `branch → PR → merge
→ CI builds image → add-on update`. When Home Assistant offers that image,
choose **Update** once at a planned moment.

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
Inputs to run.sh
  |
  +-- Supervisor stored app options (durable authority)
  |     +-- /data/options.json (generated, read-only startup projection)
  |           STATION_NAME, MAMMAMIRADIO_QUALITY (from quality_profile, default balanced),
  |           ADMIN_TOKEN (blank => LAN-trusted, no token required),
  |           HA_ENABLED (from enable_home_assistant; master HA integration switch),
  |           MAMMAMIRADIO_HA_CONTEXT_ENABLED (from ha_context_enabled;
  |             turn off to stop AI prompt-context polling while keeping HA integration),
  |           MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL (default 300 seconds),
  |           MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH, MAMMAMIRADIO_SUPER_ITALIAN,
  |           MAMMAMIRADIO_CHAOS_MODE, MAMMAMIRADIO_FESTIVAL_MODE,
  |           MAMMAMIRADIO_BROADCAST_CHAIN, MAMMAMIRADIO_GUEST_HOST,
  |           MAMMAMIRADIO_PACING_SONGS_BETWEEN_BANTER,
  |           MAMMAMIRADIO_PACING_SONGS_BETWEEN_ADS,
  |           MAMMAMIRADIO_PACING_AD_SPOTS_PER_BREAK
  |
  +-- /config/secrets.env (provider secrets and private Jamendo intent)
  |     run.sh reads supported KEY=VALUE lines and exports non-empty values:
  |     ANTHROPIC_API_KEY, OPENAI_API_KEY,
  |     AZURE_SPEECH_KEY, AZURE_SPEECH_REGION, ELEVENLABS_API_KEY,
  |     JAMENDO_CLIENT_ID, MAMMAMIRADIO_JAMENDO_ENABLED,
  |     MAMMAMIRADIO_JAMENDO_NONCOMMERCIAL_ACKNOWLEDGED,
  |     MAMMAMIRADIO_JAMENDO_ACK_REVISION
  |     Supervisor drops schema-removed provider keys from /data/options.json on start;
  |     saved by older versions are recovered once via the Supervisor API
  |     (/addons/self/info) and persisted into /config/secrets.env at first boot.
  |
  +-- SUPERVISOR_TOKEN
  |
  +-- run.sh maps Supervisor token
  |     SUPERVISOR_TOKEN -> HA_TOKEN, HA_URL=http://supervisor/core
  |
  +-- run.sh sets add-on containment and runtime defaults
  |     MAMMAMIRADIO_MUSIC_DIR=/data/music
  |     MAMMAMIRADIO_BIND_HOST=0.0.0.0, MAMMAMIRADIO_PORT=8000,
  |     MAMMAMIRADIO_CACHE_DIR=/data/cache, MAMMAMIRADIO_TMP_DIR=/data/tmp,
  |     MAMMAMIRADIO_ALLOW_YTDLP=false
  |
  +-- config.py reads the exported environment and applies add-on overrides
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
| Demo Radio | Stock host copy and fallback voices over the offline attributed starter catalog | No key or network required for music |
| Full AI Radio | Live AI banter and ads over starter/local music, with optional transient Jamendo tracks | `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` in `/config/secrets.env`; Jamendo remains optional |
| Connected Home | Above + home-aware banter | AI host key + prompt-safe Home Assistant context available |
