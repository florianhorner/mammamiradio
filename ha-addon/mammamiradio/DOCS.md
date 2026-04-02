# Mamma Mi Radio — HA Add-on Runbook

Operational guide for the Home Assistant add-on. Covers architecture, failure modes, and recovery.

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
  |           +-- go-librespot (Spotify Connect target, writes PCM to FIFO)
  |
  +-- /data/ (persistent across restarts)
        +-- cache/       (downloaded track audio)
        +-- tmp/         (rendered segments, go-librespot.log)
        +-- music/       (local music files)
        +-- go-librespot/ (config.yml, credentials cache)
```

## Startup sequence

1. `run.sh` reads `/data/options.json`, exports env vars
2. `run.sh` starts uvicorn
3. FastAPI startup loads `radio.toml`, fetches playlist from Spotify
4. If Spotify API fails, falls back to demo playlist (10 built-in tracks)
5. Syncs `/data/go-librespot/config.yml` to ensure the shipped `device_name` is current, then starts go-librespot
6. If go-librespot fails, falls back to local files / yt-dlp / placeholder tones
7. Starts producer and playback tasks

**Startup timeout**: `config.yaml` sets `timeout: 300` (5 minutes). The first boot is slow because it fetches the playlist and renders the first segment. If the addon is killed during startup, check the Supervisor log — a timeout kill looks like `Container terminated` with no error from the app.

## Failure modes and recovery

### "No Spotify Connect target visible"

**Symptom**: The `mammamiradio` device doesn't appear in your Spotify app.

**Causes**:
1. go-librespot failed to start (check addon log for `Could not start go-librespot`)
2. Zeroconf/mDNS not reaching your network — `host_network: true` is required in `config.yaml`
3. go-librespot credentials not cached yet — it needs a first connection via zeroconf

**Fix**: Restart the addon. Check the log for `go-librespot started`. If it says `No such file or directory`, the binary is missing from the image (rebuild). If it starts but no device appears, your network may block mDNS (port 5353 UDP).

### "Playlist fetch failed — using demo playlist"

**Symptom**: Log shows `401 Valid user authentication required` and plays demo tracks.

**Causes**:
1. `spotify_client_id` / `spotify_client_secret` not set in addon options
2. Credentials are for a "client credentials" app (no user auth) — this is expected for playlists you don't own
3. Liked songs require user OAuth, which client credentials can't do

**Fix**: Set your Spotify app credentials in the addon config. For your own playlists, client credentials work. For liked songs, you need user OAuth (not yet supported in addon mode — use a playlist URL instead).

### "ffmpeg failed" / signal 15 kills

**Symptom**: Log shows `ffmpeg failed (normalize ...)` with `Exiting normally, received signal 15`.

**Causes**:
1. The addon was stopped/restarted while FFmpeg was encoding
2. The shutdown handler now awaits task cancellation, which should reduce these

**Fix**: These are harmless on restart — FFmpeg was killed because the app was shutting down. If they happen during normal operation (not a restart), check disk space in `/data/tmp/`.

### Ingress 404s (all API calls return 404)

**Symptom**: Dashboard loads but shows no data. Log floods with `GET /api/hassio_ingress/.../status 404`.

**Cause**: The `_inject_ingress_prefix` function was rewriting JavaScript string literals that the client-side `_base` variable already handles, causing double-prefixed URLs.

**Fix**: Fixed in this version. The function now only rewrites static HTML attributes (`href=`, `src=`). JavaScript API calls use the `_base` variable from `window.location.pathname`.

### HA context never appears in banter

**Symptom**: Hosts never reference home state even though HA is enabled.

**Check**:
1. Addon log should show `Home Assistant API access configured via Supervisor`
2. Look for `Fetched HA context: N entities` — if N=0, no entities matched
3. Look for `Failed to fetch HA context` — network or auth error

**Note**: `HA_URL` must be `http://supervisor/core` (without `/api`). The app appends `/api/states` itself. A double `/api` causes silent 404s from the Supervisor API.

### Producer stuck in silence loop

**Symptom**: Stream plays silence indefinitely. Log shows repeated `Failed to produce ... segment` errors.

**Cause**: FFmpeg or network is persistently broken. The producer now backs off exponentially (2s, 4s, 8s, 16s, 30s max) on consecutive failures and resets on success.

**Fix**: Check what's failing:
- FFmpeg errors: is `ffmpeg` on PATH? (`ffmpeg -version` in the container)
- Network errors: can the container reach `api.anthropic.com`? (banter/ads need Claude)
- Disk full: check `/data/tmp/` size

### Admin API inaccessible from LAN

**Symptom**: Direct access to `http://<ha-ip>:8000/` returns 401 and you don't know the token.

**Cause**: `ADMIN_TOKEN` is auto-generated on each restart and not logged. With `host_network: true`, port 8000 is exposed on the host.

**Fix**: Use the addon via HA ingress (sidebar) — ingress bypasses admin auth. For direct access, set `ADMIN_PASSWORD` in the addon options (not yet exposed in the UI — would need adding to `config.yaml` schema).

## Key files

| File | Purpose |
|------|---------|
| `config.yaml` | Addon metadata, options schema, network config |
| `build.yaml` | Base images per arch, build args |
| `Dockerfile` | Image: Alpine + Python + FFmpeg + go-librespot |
| `rootfs/run.sh` | Entrypoint: env var mapping, uvicorn launch |
| `radio.toml` | Station config defaults (hosts, pacing, ads) |
| `go-librespot-config.yml` | Spotify Connect config (zeroconf, FIFO output) |

## Env var flow

```
/data/options.json (HA UI)
  |
  +-- run.sh reads JSON, exports as env vars
  |     ANTHROPIC_API_KEY, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET,
  |     STATION_NAME, CLAUDE_MODEL, PLAYLIST_SPOTIFY_URL
  |
  +-- run.sh maps Supervisor token
  |     SUPERVISOR_TOKEN -> HA_TOKEN, HA_URL, HA_ENABLED
  |
  +-- run.sh sets addon defaults
  |     MAMMAMIRADIO_BIND_HOST=0.0.0.0, MAMMAMIRADIO_PORT=8000,
  |     MAMMAMIRADIO_CACHE_DIR=/data/cache, MAMMAMIRADIO_TMP_DIR=/data/tmp,
  |     ADMIN_TOKEN=(auto-generated)
  |
  +-- config.py reads env vars, applies addon overrides
        go_librespot_config_dir -> /data/go-librespot  (inside the add-on container)
        homeassistant.url -> http://supervisor/core
```

## Ingress URL flow

```
Browser: http://ha:8123/api/hassio_ingress/<token>/
  |
  +-- HA Supervisor nginx strips prefix, forwards GET / to addon:8000
  |
  +-- App returns dashboard HTML
  |     - Static attributes: href="/listen" rewritten to href="<prefix>/listen"
  |     - JS: _base = window.location.pathname (= /api/hassio_ingress/<token>)
  |     - JS fetch calls: _base + '/status' -> /api/hassio_ingress/<token>/status
  |
  +-- Browser fetches /api/hassio_ingress/<token>/status
  |     -> HA proxy strips prefix -> addon sees GET /status -> 200 OK
```

**Critical rule**: `_inject_ingress_prefix` must NEVER rewrite JS string literals. The `_base` variable handles JS URLs. Only rewrite static HTML attributes.

## Updating the addon

1. Bump `version` in `config.yaml`
2. Update `CHANGELOG.md`
3. Push to main — CI builds and pushes the Docker image to GHCR
4. HA Supervisor checks for updates periodically (or user clicks "Check for updates")
5. User clicks "Update" in the HA UI

**Pre-merge checklist**:
- [ ] CI builds successfully for both amd64 and aarch64
- [ ] GHCR image is published and pullable
- [ ] Install the addon from the repo URL on a test HA instance
- [ ] Verify the addon starts (check log for `Producer started`)
- [ ] Verify ingress works (dashboard loads, status polls succeed)
- [ ] Verify stream plays audio

## Known limitations

- **No user OAuth in addon mode**: Liked songs and private playlists require user OAuth flow, which needs a browser redirect. Not practical in addon mode. Use a public playlist URL instead.
- **go-librespot credentials persist across updates**: The default config is staged at `/defaults/go-librespot-config.yml`. On boot, the addon ensures `/data/go-librespot/config.yml` exists and refreshes only the `device_name` field to match the shipped default. That path is inside the add-on container, not on the host machine. Credentials/state files beside it still persist across updates. If `/data` is wiped, the addon re-initializes the config on next start.
- **`host_network: true` is broad**: Required for mDNS/zeroconf discovery. Side effect: addon can reach any LAN device and port 8000 is exposed on the host.
- **Access logs disabled**: `--no-access-log` prevents stream listener requests from flooding the Supervisor log. If you need request debugging, remove this flag in `run.sh`.
