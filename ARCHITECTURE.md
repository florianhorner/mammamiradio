# Architecture

`mammamiradio` is one FastAPI process with one shared station timeline in memory.

One background task stays ahead and produces segments. Another reads the next ready segment and streams it to every connected listener at real playback speed.

## Runtime overview

```text
Spotify playlist / liked songs / live charts / demo tracks
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
3. Restores persisted source selection from `cache/playlist_source.json`, then fetches the playlist (by source kind: playlist, liked songs, or URL) from Spotify or falls back to live Italian charts when `MAMMAMIRADIO_ALLOW_YTDLP=true`, otherwise demo tracks.
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
  - asks Claude (or OpenAI as fallback) for structured dialogue JSON
  - synthesizes one line per host via the configured TTS engine (see [TTS architecture](#tts-architecture) below)
  - preserves running jokes in `StationState`
- `AD`
  - picks brands with recurrence weighting and recent-brand avoidance
  - selects one of 6 ad formats: classic pitch, testimonial, duo scene, live remote, late-night whisper, or institutional PSA
  - resolves a sonic world (SFX, music bed mood, environment bed) per brand category
  - casts speakers by role — duo scenes and testimonials use two distinct voices with role-based resolution
  - generates a brand motif jingle for recurring brands from their sonic signature
  - builds a break from host intro, bumpers, one or more ad spots, and host outro
  - records per-spot campaign history (format, sonic signature, summary) for format rotation and campaign arc continuity

Every produced segment becomes a temporary MP3 on disk and is pushed into `asyncio.Queue[Segment]`.

Bounded state lists (`played_tracks`, `running_jokes`, `segment_log`, `stream_log`, `ad_history`, `recent_outcomes`) use `deque(maxlen=N)` for automatic memory management — no manual truncation needed.

## Playback and fanout

`streamer.py` owns the live station timeline.

- `run_playback_loop()` pops the next `Segment`, marks it live in `StationState`, and reads the MP3 in chunks.
- Chunk delivery is throttled to `config.audio.bitrate`, which is the single source of truth for stream pacing and ICY bitrate headers.
- `LiveStreamHub` fans each chunk out to all listeners.
- Slow listeners are dropped instead of stalling the whole station.
- Temp segment files are deleted after playback finishes.

Important design choice: there is one shared timeline. Listeners tune into the current live point, not their own private playback state.

## Capability flags

The old setup wizard classified the station into named modes from a combinatorial state space. The new system uses four independent boolean flags in a frozen `Capabilities` dataclass (`mammamiradio/models.py`, with detection and serialization in `mammamiradio/capabilities.py`):

| Flag | Source | What it enables |
| --- | --- | --- |
| `spotify_connected` | go-librespot zeroconf auth | Real music playback and autoplay |
| `spotify_api` | Client ID + secret present | Playlist browsing, search, metadata |
| `anthropic` | `ANTHROPIC_API_KEY` present | Live Claude-generated banter and ads |
| `ha` | `HA_TOKEN` + integration enabled | Ambient home context in banter |

The dashboard derives a tier label from these flags: Demo Radio, Your Music, Full AI Radio. Each flag is independent. `GET /api/capabilities` returns flags, tier, a guided `next_step` hint (what the user should do next), now-playing state, and connect device name.

## Autoplay flow

When a Spotify user taps "MammaMiRadio" in their device picker, the producer detects `spotify_connected` transitioning to true and triggers autoplay:

1. Read the currently playing track from go-librespot's `/status` API.
2. Purge queued demo segments and skip the current stream segment.
3. Launch two tasks in parallel:
   - **Audio capture**: `capture_current_audio()` reads PCM from the FIFO for the remaining song duration. No `play_track` call — the song continues uninterrupted.
   - **Welcome banter**: `write_banter()` generates dialogue referencing the user's current song.
4. Queue the captured song, then the personalized banter.

The listener hears their own music continue seamlessly, followed by hosts commenting on it.

## How Spotify integration works

Three separate pieces connect the station to Spotify. Each does a different job, and the station degrades gracefully when any piece is missing.

### Spotify Developer credentials (client ID + secret)

These authenticate the app against the **Spotify Web API** via Spotipy. The Web API is read-only metadata: it tells the app what music exists but never delivers audio. With credentials configured the app can:

- fetch track lists from any playlist URL
- browse the authenticated user's own playlists and Liked Songs (source picker)
- search the Spotify catalog from the dashboard
- read track names, artists, and durations so the hosts can reference what is playing

Without credentials the app falls back to live Italian charts when `MAMMAMIRADIO_ALLOW_YTDLP=true`, otherwise to a built-in demo playlist of Italian tracks. The demo playlist has hardcoded metadata so banter still references real song names.

### go-librespot (audio capture)

go-librespot is an open-source Spotify Connect receiver. It registers itself as a playback device (like a Chromecast or Sonos speaker) and streams actual audio from Spotify's servers. The app starts go-librespot at boot, and `spotify_player.py` captures PCM from its FIFO pipe, encodes it to MP3, and hands it to the producer.

Without go-librespot (or if it fails to start), the app knows what tracks to play but cannot get the audio. It falls back to downloading via `yt-dlp`, then to local files in `music/`, then to generated placeholder tones.

### Device selection in Spotify ("select mammamiradio")

Starting go-librespot creates the device, but Spotify does not automatically route audio to it. The user must open their Spotify app, tap the device picker (the speaker/devices icon), and select the mammamiradio device. This is the same gesture as picking any other Connect speaker. Until this step is done, go-librespot is running but idle and the setup status shows Spotify Connect as "Degraded."

### Playlist URL (share link)

A shortcut that bypasses the source picker entirely. The user copies a playlist share link from Spotify (Share > Copy link) and pastes it into `PLAYLIST_SPOTIFY_URL` or the dashboard's URL load field. This works in all deployment modes including the HA add-on and Docker, where the interactive picker is disabled.

### The full chain

```
Developer creds          go-librespot             Spotify app
(Web API: metadata)      (Connect: audio)         (device selection)
       |                       |                        |
       v                       v                        v
  "what to play"    +    "the actual sound"    +   "route audio here"
       |                       |                        |
       +-----------> producer.py <----------------------+
                         |
                         v
                   radio stream
```

Each layer is independent. Missing credentials means live Italian charts when `MAMMAMIRADIO_ALLOW_YTDLP=true`, otherwise demo tracks. Missing go-librespot means downloaded or local audio. Missing device selection means the app waits in degraded mode until the user connects. The station always produces a stream.

## Spotify audio path (FIFO details)

go-librespot writes raw PCM into a named pipe. On macOS, that FIFO needs a reader attached all the time or go-librespot throws `ENXIO` and starts skipping tracks.

The fix is a persistent drain path:

1. keep the FIFO open and draining continuously
2. when a track should be captured, redirect that drain into ffmpeg stdin
3. encode the captured PCM to normalized MP3
4. return to draining without capture when the track ends

`start.sh` also keeps a fallback `cat` drain alive across uvicorn reloads so local development does not constantly break Spotify playback.

## TTS architecture

Each host declares a TTS engine in `radio.toml`: `engine = "edge"` (default) or `engine = "openai"`.

**Edge TTS** (Microsoft): free, no API key. Each host maps to an Azure Neural voice (e.g., `it-IT-GiuseppeNeural`). SSML prosody tags (rate, pitch) are derived from the host's personality axes for voice differentiation.

**OpenAI TTS** (`gpt-4o-mini-tts`): requires `OPENAI_API_KEY`. Each host maps to an OpenAI voice (e.g., `onyx`). Personality-aware delivery instructions are generated from the host's energy, warmth, and chaos axes — the model interprets these as acting direction, not just static parameters.

Fallback chain: OpenAI failure → `edge_fallback_voice` (so the host falls back to their own Edge voice, not a stranger) → stock pre-bundled clips.

A singleton `openai.AsyncOpenAI` client is reused across all TTS calls for connection pool efficiency.

## Compounding listener memory

`persona.py` maintains a persistent listener profile in SQLite (`cache/mammamiradio.db`). The persona tracks:

- **Session count**: how many times the listener has tuned in (10-minute gap = new session)
- **Motifs**: the last 20 played tracks, so hosts can reference past music naturally
- **Theories**: LLM-generated guesses about who the listener is
- **Running jokes**: cross-session callbacks that build familiarity
- **Callbacks used**: which songs the hosts have already referenced

During banter generation, the persona is loaded into the prompt via `<listener_memory>`. Claude's response includes `persona_updates` (new theories, jokes, callbacks) which are persisted back to SQLite. First-time listeners get curiosity and intrigue. Returning listeners get inside jokes and personal references.

Instruction-like patterns in persona entries are filtered before storage (matching the `ha_context` sanitizer) to prevent stored prompt injection across sessions.

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
- `/api/stop`
- `/api/resume`
- `/api/spotify/source-options`
- `/api/spotify/source/select`

Admin access is granted by one of:

- localhost access, unless `ADMIN_PASSWORD` is configured
- HTTP Basic auth via `ADMIN_USERNAME` and `ADMIN_PASSWORD`
- token auth via `X-Radio-Admin-Token` header for non-local requests when only `ADMIN_TOKEN` is configured

Non-local binds without admin auth are rejected during config validation.

### CSRF protection

Mutating admin requests (POST/PUT/PATCH/DELETE) over non-loopback networks must pass a CSRF check. The dashboard injects a per-session token via `__MAMMAMIRADIO_CSRF_TOKEN__` placeholder replacement. Requests are allowed if any of: the CSRF token header matches, the Origin or Referer is same-origin, the request uses token auth (`X-Radio-Admin-Token`), or the request comes through HA ingress. Loopback clients are exempt.

### Source switch concurrency

`source_switch_lock` (asyncio.Lock on `app.state`) serializes `/api/spotify/source/select` and `/api/playlist/load` so only one source change runs at a time. Both endpoints trigger immediate cutover: the segment queue is purged, the current segment is skipped, and playback begins from the new source. The producer uses a `playlist_revision` counter on `StationState` to detect and discard segments generated for a stale source.

## Failure model

This repo is biased toward "keep the station on air."

- producer exceptions insert a short silence segment instead of crashing the app
- script generation failures fall back to OpenAI when configured, then to stock copy
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
| `mammamiradio/scriptwriter.py` | Anthropic/OpenAI prompts for banter and ad copy |
| `mammamiradio/tts.py` | TTS synthesis (Edge TTS + OpenAI gpt-4o-mini-tts) |
| `mammamiradio/capabilities.py` | Capability flags, tier derivation, and next-step hints |
| `mammamiradio/persona.py` | Listener persona with compounding memory, motif tracking, and session counting |
| `mammamiradio/sync.py` | go-librespot config synchronization from radio.toml |
| `mammamiradio/context_cues.py` | Time-of-day and cultural context for prompts |
| `mammamiradio/normalizer.py` | ffmpeg helpers for normalization, mixing, tones, and bumpers |
| `mammamiradio/streamer.py` | HTTP routes, auth gating, playback loop, listener fanout |
| `start.sh` | local dev entry point with reload-safe go-librespot handling |

## Deployment models

The app runs in three modes:

- **Local dev** via `start.sh` (manages go-librespot + FIFO + uvicorn with --reload)
- **Docker container** via `Dockerfile` / `docker-compose.yml` (no go-librespot, runs as non-root user, persistent `/data` volume)
- **Home Assistant add-on** via `ha-addon/mammamiradio/` (Alpine-based, Supervisor injects HA token, ingress proxies the dashboard into the HA sidebar)

The HA add-on bundles go-librespot and supports full Spotify playback. Users link their Spotify account via an OAuth flow in the dashboard (`/spotify/auth`), which stores tokens at `/data/.spotify_token_cache`. Once authenticated, the addon can auto-transfer playback to the go-librespot device and capture audio via the FIFO pipe. If Spotify is not configured or authenticated, music falls back to yt-dlp, local files, or placeholder audio. The standalone Docker image does not include go-librespot. The ingress-compatible UI uses JavaScript base path detection so the dashboard works both at `/` and behind HA's ingress proxy.

## Operational notes

- Version metadata lives in `pyproject.toml`.
- Generated assets land in `tmp/` and `cache/`.
- Station state is in memory. Restarting the process resets counters, logs, and running jokes.
