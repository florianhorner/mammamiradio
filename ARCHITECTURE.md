# Architecture

`mammamiradio` is one FastAPI process with one shared station timeline in memory.

One background task stays ahead and produces segments. Another reads the next ready segment and streams it to every connected listener at real playback speed.

## Runtime overview

```text
Spotify playlist / liked songs / demo tracks
                |
                v
           playlist.py
                |
                v
          StationState + scheduler.py
                |
                v
        producer.py renders Segment files
                |
                v
          asyncio.Queue[Segment]
                |
                v
   streamer.py playback loop -> LiveStreamHub -> /stream and /listen
                |
                +-> /public-status and /status
```

## Startup flow

`mammamiradio.main:startup()` does five things:

1. Loads `radio.toml` and `.env` through `config.py`.
2. Validates the config and applies legacy migration like `station.bitrate -> audio.bitrate`.
3. Fetches the playlist from Spotify or falls back to demo tracks.
4. Starts `SpotifyPlayer`, which owns go-librespot and the FIFO drain path.
5. Creates shared app state, then launches:
   - `run_producer()` to fill the lookahead queue
   - `run_playback_loop()` to stream queued audio

## Segment production

`scheduler.py` is the single source of truth for pacing:

- the first segment is always music
- ad breaks trigger when `songs_since_ad >= songs_between_ads`
- banter triggers when `songs_since_banter` crosses the configured threshold, with a small random jitter outside preview mode

`producer.py` turns that pacing decision into actual audio files:

- `MUSIC`
  - prefers Spotify capture when go-librespot is authenticated
  - otherwise falls back to local `music/`, then `yt-dlp`, then a generated placeholder tone
  - normalizes output before queueing
- `BANTER`
  - asks Claude for structured dialogue JSON
  - synthesizes one line per host with Edge TTS
  - preserves running jokes in `StationState`
- `AD`
  - picks brands with recurrence weighting and recent-brand avoidance
  - builds a break from host intro, bumpers, one or more ad spots, and host outro
  - records per-spot campaign history, but only increments `segments_produced` once per break

Every produced segment becomes a temporary MP3 on disk and is pushed into `asyncio.Queue[Segment]`.

## Playback and fanout

`streamer.py` owns the live station timeline.

- `run_playback_loop()` pops the next `Segment`, marks it live in `StationState`, and reads the MP3 in chunks.
- Chunk delivery is throttled to `config.audio.bitrate`, which is the single source of truth for stream pacing and ICY bitrate headers.
- `LiveStreamHub` fans each chunk out to all listeners.
- Slow listeners are dropped instead of stalling the whole station.
- Temp segment files are deleted after playback finishes.

Important design choice: there is one shared timeline. Listeners tune into the current live point, not their own private playback state.

## Spotify audio path

The strange part of this app is `spotify_player.py`.

go-librespot writes raw PCM into a named pipe. On macOS, that FIFO needs a reader attached all the time or go-librespot throws `ENXIO` and starts skipping tracks.

The fix is a persistent drain path:

1. keep the FIFO open and draining continuously
2. when a track should be captured, redirect that drain into ffmpeg stdin
3. encode the captured PCM to normalized MP3
4. return to draining without capture when the track ends

`start.sh` also keeps a fallback `cat` drain alive across uvicorn reloads so local development does not constantly break Spotify playback.

## Optional Home Assistant context

If `[homeassistant].enabled = true` and `HA_TOKEN` is present:

- `ha_context.py` polls the Home Assistant REST API
- a curated set of entities is translated into short Italian-readable context
- banter and ads may reference one ambient detail, like weather or who is home

This is opportunistic context, not a hard dependency. Failures there should not stop the station.

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
- `/api/playlist/remove`
- `/api/playlist/move`
- `/api/playlist/move_to_next`
- `/api/search`
- `/api/playlist/add`
- `/api/playlist/load`

Admin access is granted by one of:

- localhost access, unless `ADMIN_PASSWORD` is configured
- HTTP Basic auth via `ADMIN_USERNAME` and `ADMIN_PASSWORD`
- token auth via `X-Radio-Admin-Token` or `admin_token` query param for non-local requests when only `ADMIN_TOKEN` is configured

Non-local binds without admin auth are rejected during config validation.

## Failure model

This repo is biased toward "keep the station on air."

- producer exceptions insert a short silence segment instead of crashing the app
- script generation failures fall back to stock copy
- missing Spotify auth falls back to demo or downloaded audio
- missing Home Assistant context is ignored
- missing ad brands disables ads rather than killing startup

The rich path is richer, but the failure path still produces a stream.

## File map

| Path | Responsibility |
| --- | --- |
| `mammamiradio/main.py` | app startup/shutdown and background task wiring |
| `mammamiradio/config.py` | `radio.toml` and `.env` loading plus validation |
| `mammamiradio/models.py` | shared dataclasses for tracks, segments, ads, and station state |
| `mammamiradio/playlist.py` | Spotify playlist or liked-song fetch with demo fallback |
| `mammamiradio/scheduler.py` | pacing rules and upcoming preview |
| `mammamiradio/producer.py` | segment generation pipeline |
| `mammamiradio/spotify_player.py` | go-librespot process management and FIFO capture |
| `mammamiradio/downloader.py` | local-file, yt-dlp, and placeholder music fallback |
| `mammamiradio/scriptwriter.py` | Claude prompts for banter and ad copy |
| `mammamiradio/tts.py` | Edge TTS synthesis for voices and ad parts |
| `mammamiradio/normalizer.py` | ffmpeg helpers for normalization, mixing, tones, and bumpers |
| `mammamiradio/streamer.py` | HTTP routes, auth gating, playback loop, listener fanout |
| `start.sh` | local dev entry point with reload-safe go-librespot handling |

## Deployment models

The app runs in three modes:

- **Local dev** via `start.sh` (manages go-librespot + FIFO + uvicorn with --reload)
- **Docker container** via `Dockerfile` / `docker-compose.yml` (no go-librespot, runs as non-root user, persistent `/data` volume)
- **Home Assistant add-on** via `ha-addon/mammamiradio/` (Alpine-based, Supervisor injects HA token, ingress proxies the dashboard into the HA sidebar)

The HA add-on and Docker paths skip go-librespot and the FIFO. Music falls back to yt-dlp, local files, or placeholder audio. The ingress-compatible UI uses JavaScript base path detection so the dashboard works both at `/` and behind HA's ingress proxy.

## Operational notes

- Version metadata lives in `pyproject.toml`.
- Generated assets land in `tmp/` and `cache/`.
- Station state is in memory. Restarting the process resets counters, logs, and running jokes.
