# Architecture

`mammamiradio` is one FastAPI process with one shared station timeline in memory.

One background task stays ahead and produces segments. Another reads the next ready segment and streams it to every connected listener at real playback speed.

## Runtime overview

```text
Live Italian charts / local files / demo tracks
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

`mammamiradio.main:startup()` does seven things:

1. Loads `radio.toml` and `.env` through `config.py`.
2. Validates the config and applies legacy migration like `station.bitrate -> audio.bitrate`.
3. Purges suspect cache files (< 10KB, likely failed downloads) and evicts old cache entries.
4. Restores persisted source selection from `cache/playlist_source.json`, then fetches the playlist (charts or demo) with fallback to live Italian charts when `MAMMAMIRADIO_ALLOW_YTDLP=true`, otherwise demo tracks.
5. Initializes the clip ring buffer for WTF clip sharing.
6. Creates shared app state, then launches:
   - `run_producer()` to fill the lookahead queue
   - `run_playback_loop()` to stream queued audio
7. Logs a one-line boot summary with resolved config dir, audio source, API key presence, HA status, and track count.

## Segment production

`scheduler.py` is the single source of truth for pacing:

- the first segment is always music
- ad breaks trigger when `songs_since_ad >= songs_between_ads`
- banter triggers when `songs_since_banter` crosses the configured threshold, with a small random jitter outside preview mode

`producer.py` turns that pacing decision into actual audio files:

- `MUSIC`
  - uses local `music/` files, then `yt-dlp` for chart tracks, then a generated placeholder tone
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

### Studio atmosphere

Two features create the illusion of a live radio studio:

- **Studio bleed**: After producing a music segment, the producer mixes a faint (-22dB) snippet of a previously-played banter clip under ~35% of music segments. This creates the "someone left a mic on" feeling.
- **Humanity events**: A one-shot event system (cough, paper rustle, chair creak, pen tap) fires exactly once per session after 15+ segments have been produced. SFX files live in `demo_assets/sfx/studio/`.

### Clip sharing

A rolling `deque[bytes]` ring buffer on `app.state` records ~60 seconds of raw MP3 chunks during the playback loop. `POST /api/clip` extracts the last 30 seconds into a shareable file in `{cache_dir}/clips/`. Clips are served without auth at `GET /clips/{id}.mp3` and auto-expire after 24 hours. Per-IP rate limiting (1 clip per 10 seconds) and a 50-clip disk cap prevent abuse.

### Periodic chart refresh

When the playlist source is charts, the producer checks every 90 minutes and merges new chart entries into the live playlist without resetting `played_tracks` history. This prevents long sessions from looping the same track set.

## Playback and fanout

`streamer.py` owns the live station timeline.

- `run_playback_loop()` pops the next `Segment`, marks it live in `StationState`, and reads the MP3 in chunks.
- Chunk delivery is throttled to `config.audio.bitrate`, which is the single source of truth for stream pacing and ICY bitrate headers.
- `LiveStreamHub` fans each chunk out to all listeners.
- Slow listeners are dropped instead of stalling the whole station.
- Temp segment files are deleted after playback finishes.

Important design choice: there is one shared timeline. Listeners tune into the current live point, not their own private playback state.

## Capability flags

The system uses two independent boolean flags in a frozen `Capabilities` dataclass (`mammamiradio/models.py`, with detection and serialization in `mammamiradio/capabilities.py`):

| Flag | Source | What it enables |
| --- | --- | --- |
| `anthropic` | `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` present | Live AI-generated banter and ads |
| `ha` | `HA_TOKEN` + integration enabled | Ambient home context in banter |

The dashboard derives a tier label from these flags: Demo Radio, Full AI Radio, Connected Home. `GET /api/capabilities` returns flags, tier, and a guided `next_step` hint (what the user should do next).

## Music sources

Music comes from one of two source modes:

1. **Charts + local blend** (when `MAMMAMIRADIO_ALLOW_YTDLP=true`): Up to 100 tracks fetched from Apple Music Italy RSS. MP3s in `music/` are merged in automatically — deduplicated by `spotify_id`. Total catalog typically 100-300 tracks, covering 7h+ of unique content.
2. **Built-in demo playlist**: 10 hardcoded Italian tracks used when `MAMMAMIRADIO_ALLOW_YTDLP` is not set.

The station always produces a stream regardless of which source is active.

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

- `ha_context.py` polls the Home Assistant REST API for ~35 curated entities (gold/silver/bronze tiers)
- entities include room-level light groups, power sensors, weather, presence, vacuums, star projectors, terrace lights
- a 4-phase pipeline processes the data: state summary → event diffing → mood classification → weather narrative arc
- 7 reactive triggers fire on specific state changes (coffee machine, door unlock, vacuums, arrivals, terrace lights)
- banter references are tiered: 1 item by default, up to 2 when a mood scene is active (mood counts toward cap)
- weather-mood fusion allows hosts to connect outdoor conditions to indoor activity
- numeric state passthrough in `ha_enrichment.diff_states()` ensures power sensors generate events
- the listener dashboard shows a "Casa" card with mood, weather, and recent events via `ha_moments` in `/public-status`
- the admin panel shows full HA details (mood, weather arc, events summary, pending directives) via `ha_details` in `/status`
- person entity events are filtered from public API responses (privacy)

This is opportunistic context, not a hard dependency. Failures there should not stop the station.

## Access model

Public routes:

- `/` (listener dashboard)
- `/listen` (legacy, redirects to `/`)
- `/stream`
- `/public-status`
- `/healthz`, `/readyz`
- `/api/clip` (rate-limited)
- `/clips/{id}.mp3`
- `/api/listener-request`

Admin routes:

- `/admin`
- `/status`
- `/api/shuffle`
- `/api/skip`
- `/api/purge`
- `/api/playlist/remove`
- `/api/playlist/move`
- `/api/playlist/move_to_next`
- `/api/playlist/add`
- `/api/playlist/add-external`
- `/api/playlist/load`
- `/api/search`
- `/api/stop`
- `/api/resume`
- `/api/trigger`
- `/api/track-rules`
- `/api/listener-requests`
- `/api/hosts`, `/api/hosts/{name}/personality`
- `/api/pacing`
- `/api/credentials`
- `/api/capabilities`

Admin access is granted by one of:

- localhost access, unless `ADMIN_PASSWORD` is configured
- HTTP Basic auth via `ADMIN_USERNAME` and `ADMIN_PASSWORD`
- token auth via `X-Radio-Admin-Token` header for non-local requests when only `ADMIN_TOKEN` is configured

Non-local binds without admin auth are rejected during config validation.

### CSRF protection

Mutating admin requests (POST/PUT/PATCH/DELETE) over non-loopback networks must pass a CSRF check. The dashboard injects a per-session token via `__MAMMAMIRADIO_CSRF_TOKEN__` placeholder replacement. Requests are allowed if any of: the CSRF token header matches, the Origin or Referer is same-origin, the request uses token auth (`X-Radio-Admin-Token`), or the request comes through HA ingress. Loopback clients are exempt.

### Source switch concurrency

`source_switch_lock` (asyncio.Lock on `app.state`) serializes `/api/playlist/load` so only one source change runs at a time. The endpoint triggers immediate cutover: the segment queue is purged, the current segment is skipped, and playback begins from the new source. The producer uses a `playlist_revision` counter on `StationState` to detect and discard segments generated for a stale source.

## Failure model

This repo is biased toward "keep the station on air."

- producer exceptions insert a short silence segment instead of crashing the app
- script generation failures fall back to OpenAI when configured, then to stock copy
- missing yt-dlp falls back to local files or demo tracks
- missing Home Assistant context is ignored
- missing ad brands disables ads rather than killing startup

The rich path is richer, but the failure path still produces a stream.

## File map

| Path | Responsibility |
| --- | --- |
| `mammamiradio/main.py` | app startup/shutdown and background task wiring |
| `mammamiradio/config.py` | `radio.toml` and `.env` loading plus validation |
| `mammamiradio/models.py` | shared dataclasses for tracks, segments, ads, and station state |
| `mammamiradio/playlist.py` | Charts, local, and demo playlist loading |
| `mammamiradio/scheduler.py` | pacing rules and upcoming preview |
| `mammamiradio/producer.py` | segment generation pipeline |
| `mammamiradio/downloader.py` | local-file, yt-dlp, and placeholder music fallback |
| `mammamiradio/scriptwriter.py` | Anthropic/OpenAI prompts for banter and ad copy |
| `mammamiradio/tts.py` | TTS synthesis (Edge TTS + OpenAI gpt-4o-mini-tts) |
| `mammamiradio/capabilities.py` | Capability flags, tier derivation, and next-step hints |
| `mammamiradio/persona.py` | Listener persona with compounding memory, motif tracking, and session counting |
| `mammamiradio/sync.py` | SQLite database initialization |
| `mammamiradio/context_cues.py` | Time-of-day and cultural context for prompts |
| `mammamiradio/normalizer.py` | ffmpeg helpers for normalization, mixing, tones, bumpers, bleed, and SFX |
| `mammamiradio/clip.py` | WTF clip extraction from ring buffer, save, cleanup |
| `mammamiradio/track_rationale.py` | "Why this track?" rationale generation for listener UI |
| `mammamiradio/streamer.py` | HTTP routes, auth gating, playback loop, clip endpoints, listener fanout |
| `start.sh` | local dev entry point with uvicorn and reload |

## Deployment models

The app runs in three modes:

- **Local dev** via `start.sh` (uvicorn with --reload)
- **Docker container** via `Dockerfile` / `docker-compose.yml` (runs as non-root user, persistent `/data` volume)
- **Home Assistant add-on** via `ha-addon/mammamiradio/` (Alpine-based, Supervisor injects HA token, ingress proxies the dashboard into the HA sidebar)

The ingress-compatible UI uses JavaScript base path detection so the dashboard works both at `/admin` and behind HA's ingress proxy.

## Operational notes

- Version metadata lives in `pyproject.toml`.
- Generated assets land in `tmp/` and `cache/`.
- Station state is in memory. Restarting the process resets counters, logs, and running jokes.
