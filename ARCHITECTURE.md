# Architecture

This app is one live station timeline with two loops running in parallel: a producer that stays ahead, and a playback loop that streams whatever is next.

## Runtime flow

1. `fakeitaliradio.main:startup()` loads config from `radio.toml` and `.env`.
2. It fetches the playlist from Spotify or falls back to demo tracks.
3. It starts `SpotifyPlayer`, which manages go-librespot and the FIFO drain.
4. It creates shared app state: queue, skip event, stream hub, station state, uptime.
5. It launches:
   - `run_producer()` to fill the queue with segments
   - `run_playback_loop()` to stream queued audio to listeners

## Core components

### Config and state

- `config.py` parses `radio.toml`, reads secrets from `.env`, and fails fast on invalid combinations.
- `models.py` holds the station state: playlist rotation, counters, running jokes, ad history, public/admin status data.

### Producer side

`producer.py` decides what to generate next using `scheduler.py`.

Segment types:

- `MUSIC`
- `BANTER`
- `AD`

For music:

- prefers live Spotify capture when go-librespot is authenticated
- otherwise falls back to local `music/`, then `yt-dlp`, then a generated placeholder tone
- normalizes output before queueing

For banter:

- `scriptwriter.py` asks Claude for structured dialogue JSON
- `tts.py` synthesizes one line per host using Edge TTS
- running jokes are carried forward in `StationState`

For ads:

- brands are picked with recurrence weighting and recent-brand avoidance
- scripts are generated as structured multi-part ads with optional SFX and mood
- ad breaks are assembled from intro, bumpers, one or more spots, and outro

### Optional Home Assistant context

If `[homeassistant].enabled = true` and `HA_TOKEN` is set:

- `ha_context.py` polls the HA REST API
- a curated set of entities is translated into short Italian-readable context
- banter and ads may reference one ambient detail, like weather or who is home

This context is opportunistic. Failures do not stop the station.

### Spotify capture

`spotify_player.py` solves the annoying part: macOS FIFO behavior.

go-librespot writes PCM into a named pipe. If nothing is reading from that FIFO, macOS throws `ENXIO` and playback skips. The app keeps a persistent drain thread attached at all times, then redirects that stream into ffmpeg when it needs to capture a track.

`start.sh` exists mostly to make this survivable during `uvicorn --reload`.

## Playback and streaming

`streamer.py` owns playback and HTTP routes.

- `run_playback_loop()` reads the next segment, marks it live in `StationState`, and streams MP3 chunks at the configured bitrate.
- `LiveStreamHub` fans those chunks out to all connected listeners.
- Slow listeners are dropped instead of stalling the station.
- Segment temp files are deleted after playback.

The important design choice: there is one shared station timeline. Listeners tune into the current live point, not their own per-client playback state.

## Access model

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
- `/api/playlist/*`

Admin access is granted by one of:

- localhost access, unless `ADMIN_PASSWORD` is configured
- HTTP Basic auth via `ADMIN_USERNAME` and `ADMIN_PASSWORD`
- token auth via `X-Radio-Admin-Token` or `admin_token` query param for non-local requests when only `ADMIN_TOKEN` is configured

Non-local bind without admin auth is rejected at config validation time.

## Failure model

This repo is biased toward "keep the station on air."

- producer exceptions insert a short silence segment instead of crashing
- script generation failures fall back to canned copy
- missing Spotify auth falls back to demo or downloaded audio
- missing Home Assistant data is ignored
- missing ad brands disables ads rather than killing startup

That means the happy path is richer, but the failure path still produces a stream.
