# Architecture

`fakeitaliradio` is a single FastAPI process that keeps a radio station timeline alive in memory. One background task produces future segments, another streams the currently selected segment to every listener at real playback speed.

## Runtime overview

```text
Spotify Web API / demo playlist
        |
        v
playlist.py --> StationState --> scheduler.py
                                |
                                v
                      producer.py builds Segment files
                                |
                                v
                      asyncio.Queue[Segment]
                                |
                                v
streamer.py playback loop --> LiveStreamHub --> /stream and /listen
        ^
        |
 dashboard + admin APIs at /
```

## Main flows

### 1. Startup

`fakeitaliradio.main` loads `radio.toml` plus `.env`, validates the config, fetches a playlist, and creates shared app state:

- `StationState` tracks playlist order, pacing counters, current scripts, logs, and ad history.
- `asyncio.Queue` holds pre-rendered segment files.
- `LiveStreamHub` fans MP3 chunks out to all active listeners.

It then launches two long-running tasks:

- `run_producer(...)` prebuilds future segments.
- `run_playback_loop(...)` reads the queue and streams one segment at a time.

### 2. Segment production

`scheduler.py` is the single source of truth for pacing:

- first segment is always music
- ad breaks trigger when `songs_since_ad >= songs_between_ads`
- banter triggers when `songs_since_banter` crosses the configured threshold, with a small random jitter

`producer.py` turns that decision into audio:

- `MUSIC`: capture from Spotify when go-librespot is authenticated, otherwise fall back to local files, yt-dlp, or a generated placeholder tone
- `BANTER`: ask Claude for host dialogue, then synthesize it with Edge TTS
- `AD`: build a multi-part ad break with host intros/outros, bumper jingles, one or more spots, and campaign-history callbacks

Every produced segment becomes a temporary MP3 on disk and is pushed into the queue.

### 3. Playback and fanout

`streamer.py` pulls one `Segment` at a time from the queue, updates `StationState.now_streaming`, and reads the MP3 in chunks.

The playback loop throttles chunk delivery to the configured bitrate. That is why the dashboard timeline stays aligned with what the listener actually hears instead of racing ahead as fast as the file can be read from disk.

`LiveStreamHub` keeps one queue per listener. Slow listeners are dropped instead of stalling the station for everyone else.

## Spotify audio path

The weird part is `spotify_player.py`.

go-librespot writes raw PCM into a named pipe. On macOS that pipe needs a reader attached all the time or go-librespot starts throwing ENXIO and skips tracks. The fix is a persistent drain thread:

1. keep the FIFO open and draining continuously
2. when a track should be captured, redirect the drain into ffmpeg stdin
3. encode the captured PCM to normalized MP3
4. return to draining into `/dev/null` when capture ends

`start.sh` also keeps a fallback `cat` process alive across uvicorn reloads so Spotify does not disconnect every time the app restarts in development.

## Surface area

Public routes:

- `/listen`
- `/stream`
- `/public-status`

Admin routes and the dashboard:

- `/`
- `/status`
- `/api/logs`
- `/api/shuffle`
- `/api/skip`
- `/api/purge`
- `/api/playlist/remove`
- `/api/playlist/move`
- `/api/playlist/move_to_next`

If the app stays on localhost, admin routes are available locally with no extra auth. If you bind to a non-local interface, config validation requires `ADMIN_PASSWORD` or `ADMIN_TOKEN`.

## File map

| Path | Responsibility |
| --- | --- |
| `fakeitaliradio/main.py` | app startup/shutdown and background task wiring |
| `fakeitaliradio/config.py` | `radio.toml` and `.env` loading plus validation |
| `fakeitaliradio/models.py` | shared dataclasses for tracks, segments, ads, and station state |
| `fakeitaliradio/playlist.py` | Spotify playlist or liked-song fetch with demo fallback |
| `fakeitaliradio/scheduler.py` | pacing rules and "up next" preview |
| `fakeitaliradio/producer.py` | segment generation pipeline |
| `fakeitaliradio/spotify_player.py` | go-librespot process management and FIFO capture |
| `fakeitaliradio/downloader.py` | local-file, yt-dlp, and placeholder music fallback |
| `fakeitaliradio/scriptwriter.py` | Claude prompts for banter and ad copy |
| `fakeitaliradio/tts.py` | Edge TTS synthesis for voices and ad parts |
| `fakeitaliradio/normalizer.py` | ffmpeg helpers for normalization, mixing, tones, and bumpers |
| `fakeitaliradio/streamer.py` | HTTP routes, auth gating, playback loop, listener fanout |
| `start.sh` | local dev entry point with reload-safe go-librespot handling |

## Operational notes

- Version metadata currently lives in `pyproject.toml`, not a dedicated `VERSION` file.
- Generated assets land in `tmp/` and `cache/`.
- This app keeps station state in memory. Restarting the process resets counters, logs, and running jokes.
