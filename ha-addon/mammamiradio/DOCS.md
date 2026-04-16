# Mamma Mi Radio — HA Add-on Runbook

Operational guide for the Home Assistant add-on. Covers architecture, failure modes, and recovery.

## First run in 4 steps

### 1. Add the repository

In HA: Settings → Add-ons → Add-on Store → overflow menu → Repositories.
Add: `https://github.com/florianhorner/mammamiradio`

### 2. Configure API key

In the add-on Configuration tab, set your `anthropic_api_key` (required for AI banter and ads).
`openai_api_key` is optional — used as TTS fallback when Anthropic is unavailable.

Without any API key, the station runs in Demo Mode: music plays, banter falls back to stock copy (the bundled-clip inventory is a TODO — `demo_assets/banter/` ships empty today).

### 3. Start the add-on

Click Start. Watch the log for:
- `[mammamiradio] Starting add-on...`
- `[mammamiradio] Home Assistant API access configured via Supervisor`
- `Producer started`
- `Station ready`

First boot is slow (30–90 seconds) — yt-dlp downloads Italian chart tracks before playback begins.

### 4. Open the listener page

Click Open Web UI or navigate to the ingress URL in the sidebar. Italian radio should start within 10 seconds.

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
        +-- music/   (local music files — optional)
```

## Startup sequence

1. `run.sh` reads `/data/options.json` and exports env vars for the addon runtime.
2. `run.sh` maps `SUPERVISOR_TOKEN` to `HA_TOKEN`, sets `HA_URL=http://supervisor/core`, `HA_ENABLED=true`.
3. `run.sh` enables yt-dlp (`MAMMAMIRADIO_ALLOW_YTDLP=true`) and starts uvicorn.
4. `mammamiradio/main.py` loads `radio.toml` and validates config.
5. `fetch_playlist()` downloads Italian chart tracks via yt-dlp (first boot: slow, cached after).
6. Producer and playback tasks start once the first segment is ready.

**Startup timeout**: `config.yaml` sets `timeout: 240`. First boot can take 60–120 seconds on slower hardware (Raspberry Pi + yt-dlp download + FFmpeg transcode). If the addon is killed during startup, check the log for `Container terminated` — usually means the download took too long.

**Recovery**: If startup times out, restart the addon. Subsequent boots are fast because tracks are cached in `/data/cache/`.

## Failure modes and recovery

### Stream plays silence indefinitely

**Symptom**: Ingress URL loads but no audio. Log shows repeated `Failed to produce segment` or `silence placeholder`.

**Causes**:
1. yt-dlp rate-limited on first boot — silence placeholder cached instead of real audio
2. FFmpeg not found on PATH
3. Network blocks outbound connections to YouTube

**Fix**: SSH to the HA host, add `export MAMMAMIRADIO_SKIP_QUALITY_GATE=1` to `/addon_configs/mammamiradio/run.sh` before the `exec uvicorn` line, then restart the addon. Once real tracks download and are cached, remove the line and restart again.

If silence is in cache from a failed run: stop the addon, SSH to the HA host, delete `/data/addon_configs/<slug>/cache/`, restart.

### TTS banter not generating

**Symptom**: Log shows `TTS synthesis failed` or `edge-tts connection error`. Banter falls back to pre-bundled demo clips.

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

**Fix**: Verify your `anthropic_api_key` is valid. Check the log for `AuthenticationError` or `RateLimitError`.

### Admin API inaccessible directly

**Symptom**: Direct access to `http://<ha-ip>:8000/admin` returns 401.

**Cause**: If you left `admin_token` blank in the Configuration tab, `run.sh` auto-generates a token on each restart and does not log it. Set a value in `admin_token` to pin it across restarts, or use HA ingress as the primary UI.

**Fix**: Access the addon via the HA sidebar (ingress). The exposed port 8000 on the host is intended for streaming clients only.

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
/data/options.json (HA UI)
  |
  +-- run.sh reads JSON, exports as env vars
  |     ANTHROPIC_API_KEY, OPENAI_API_KEY,
  |     STATION_NAME, CLAUDE_MODEL,
  |     ADMIN_TOKEN (blank => auto-generated),
  |     HA_ENABLED (from enable_home_assistant)
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
  +-- App returns listener HTML
  |     - Static attributes: src="/stream" rewritten to src="<prefix>/stream"
  |     - JS: _base = window.location.pathname
  |     - JS fetch calls: _base + '/status' -> /api/hassio_ingress/<token>/status
  |
  +-- Browser fetches <prefix>/stream
  |     -> HA proxy passes through streaming MP3 response
  |     -> Audio plays in browser
```

**Critical rule**: `_inject_ingress_prefix` must NEVER rewrite JS string literals. Only static HTML attributes are rewritten.

## Updating the addon

1. Bump `version` in `config.yaml` and `pyproject.toml`
2. Update `CHANGELOG.md`
3. Push to main — CI builds and pushes the Docker image to GHCR automatically
4. HA Supervisor checks for updates periodically (or user clicks "Check for updates")
5. User clicks "Update" in the HA UI

**Pre-merge checklist**:
- [ ] CI builds successfully for both amd64 and aarch64
- [ ] GHCR packages are public (github.com/florianhorner → Packages → each mammamiradio-addon-* → visibility: Public)
- [ ] Install the addon from the repo URL on a test HA instance
- [ ] Verify the addon starts (check log for `Producer started`)
- [ ] Verify ingress works (listener page loads, audio plays)
- [ ] Verify stream plays for 60+ minutes without interruption

## Renaming the station

The station name is what the hosts say on air. If you call it "Radio Florian", the hosts will say "Radio Florian" — naturally, mid-conversation, the way a real DJ does.

**To rename:**

1. In the add-on Configuration tab, set `station_name` to your chosen name (e.g. `Radio Florian`).
2. Click Save, then restart the add-on.
3. Within a few minutes of playback, the hosts will start using the new name.

The name appears roughly once every 3–4 banter exchanges, never forced. You can also set it via environment variable: `STATION_NAME=Radio Florian`.

## Home Assistant media_player entity

You can expose the station as a `media_player` entity in HA — show the current track on dashboards, control play/pause/skip from automations, or cast the stream to a Sonos, Google, or Alexa speaker.

**Setup (one-time):**

1. In the add-on Configuration tab, set `admin_token` to a stable string (e.g. `my-radio-token`). This avoids log-hunting for the auto-generated token on every restart.
2. Add that token to `secrets.yaml`:
   ```yaml
   mammamiradio_admin_token: my-radio-token
   ```
3. Add these three blocks to `configuration.yaml` and reload HA:

```yaml
rest:
  - resource: "http://localhost:8000/public-status"
    scan_interval: 5
    sensor:
      - name: "mammamiradio_now_streaming"
        value_template: "{{ value_json.now_streaming.type }}"
        json_attributes:
          - now_streaming

template:
  - media_player:
      - name: "Mamma Mi Radio"
        unique_id: mammamiradio_player
        state: >
          {% set t = state_attr('sensor.mammamiradio_now_streaming', 'now_streaming') %}
          {% if t and t.type not in ['stopped', 'skipping'] %}playing{% else %}paused{% endif %}
        media_title: >
          {% set t = state_attr('sensor.mammamiradio_now_streaming', 'now_streaming') %}
          {% if t %}{{ t.get('metadata', {}).get('title_only', t.label) }}{% endif %}
        media_artist: >
          {% set t = state_attr('sensor.mammamiradio_now_streaming', 'now_streaming') %}
          {% if t and t.type == 'music' %}{{ t.get('metadata', {}).get('artist', 'Mamma Mi Radio') }}
          {% else %}Mamma Mi Radio{% endif %}
        entity_picture: >
          {% set t = state_attr('sensor.mammamiradio_now_streaming', 'now_streaming') %}
          {% if t %}{{ t.get('metadata', {}).get('album_art', '') }}{% endif %}
        media_content_type: "music"
        media_content_id: "http://localhost:8000/stream"
        supported_features:
          - pause
          - play
          - next_track
        play:
          - action: rest_command.mammamiradio_resume
        pause:
          - action: rest_command.mammamiradio_stop
        media_next_track:
          - action: rest_command.mammamiradio_skip

rest_command:
  mammamiradio_resume:
    url: "http://localhost:8000/api/resume"
    method: POST
    headers:
      X-Radio-Admin-Token: !secret mammamiradio_admin_token
  mammamiradio_stop:
    url: "http://localhost:8000/api/stop"
    method: POST
    headers:
      X-Radio-Admin-Token: !secret mammamiradio_admin_token
  mammamiradio_skip:
    url: "http://localhost:8000/api/skip"
    method: POST
    headers:
      X-Radio-Admin-Token: !secret mammamiradio_admin_token
```

The entity `media_player.mamma_mi_radio` will appear with the current track title, artist, and album art (from YouTube thumbnails). You can add it to a dashboard card or use it in automations to cast the stream to any HA-connected speaker with `media_player.play_media`.

## Tiers

The dashboard shows one of three tiers based on your configuration:

| Tier | What you hear | What it needs |
|------|--------------|---------------|
| Demo Radio | Music from yt-dlp charts; banter falls back to stock copy (bundled clips TBD) | Nothing (works out of the box) |
| Full AI Radio | Live AI banter and ads, yt-dlp charts | `anthropic_api_key` or `openai_api_key` |
| Connected Home | Above + home-aware banter | API key + HA running (automatic in addon mode) |
