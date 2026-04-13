<p align="center">
  <img src="mammamiradio/logo.svg" width="128" height="128" alt="Mamma Mi Radio logo">
</p>

<h1 align="center">mammamiradio</h1>

<p align="center">AI-powered Italian radio station engine. It streams a continuous MP3 from live Italian charts or local music, layers in Claude-written host banter and absurd AI-generated ads, and exposes both a control-plane dashboard and a public listener page.</p>

The app is designed to degrade gracefully. Music comes from live Italian charts when `MAMMAMIRADIO_ALLOW_YTDLP=true`, otherwise from a bundled demo playlist or local files. If Anthropic is unavailable, banter and ads fall back to OpenAI or stock lines instead of crashing the station.

## Screenshots

### Dashboard

The listener dashboard at `/` gives you the station at a glance: now playing with animated waveform, up-next queue (with rendered vs predicted segments), pipeline status indicators, a callback corner for running jokes, and a radio dial tuning animation on first load.

![Dashboard](docs/screenshots/listener.png)

### Admin

The admin control room at `/admin` has three tabs: Music (playlist, queue, drag-drop reorder, search), Radio (hosts, pacing, triggers, banter), and Engine Room (runtime stats, segment counts, capabilities).

## What it does

- Streams a live MP3 station at `/stream`
- Serves a public listener page at `/` and an admin dashboard at `/admin`
- Rotates between music, host banter, and multi-spot ad breaks with authentic Italian brands
- Lets hosts reference live Home Assistant state when enabled
- Supports playlist mutation from the dashboard: shuffle, skip, purge, remove, reorder, play-next
- Stop and resume sessions from the admin control room
- Remembers returning listeners across sessions with compounding persona memory
- Share WTF moments: clip the last 30 seconds of audio into a shareable MP3
- Studio atmosphere: faint background voices, rare one-shot events (cough, paper rustle) for live radio feel

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) explains the runtime, component boundaries, and the audio pipeline.
- [CONTRIBUTING.md](CONTRIBUTING.md) covers local setup, test commands, and manual smoke checks.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) covers the failures you are actually likely to hit.
- [OPERATIONS.md](OPERATIONS.md) describes the current run and deploy reality.
- [CHANGELOG.md](CHANGELOG.md) tracks release notes from the current baseline forward.
- [ha-addon/README.md](ha-addon/README.md) covers Home Assistant add-on installation and usage.

## How it works

```text
Charts / local files / demo playlist -> Producer -> asyncio.Queue -> Playback loop -> /stream
                                     |                                  |
Claude -> banter/ad scripts ---------+                                  +-> /public-status, /status
Edge TTS -> dialogue + ads ----------+
FFmpeg -> normalize / mix / concat --+
Home Assistant -> optional context --+
```

- `producer.py` keeps a few segments queued ahead of playback.
- `scheduler.py` decides whether the next segment is music, banter, or an ad break.
- `streamer.py` plays one station timeline and fans out MP3 chunks to all connected listeners.

## First run in 3 steps

### 1. Choose your run mode

Pick the path you are actually using:

- Home Assistant add-on
- Docker
- macOS app
- Local dev

Config does not live in the same place for every path.

### 2. Connect the essentials

What is required vs optional:

- Anthropic API key: optional for AI banter and ads (falls back to stock copy)
- Home Assistant: optional outside add-on mode

The station plays immediately with charts or demo music. No setup is required to hear audio.

### 3. Launch your station

The dashboard shows your current tier:

- **Demo Radio**: no API key, canned banter clips
- **Full AI Radio**: Anthropic or OpenAI key configured, live AI hosts
- **Connected Home**: AI hosts + Home Assistant context

Open `/admin` and use the **Installation Onboarding** card to:

- confirm detected run mode and station tier
- run one-click preflight re-checks
- save `ANTHROPIC_API_KEY` and/or `OPENAI_API_KEY` directly from the UI
- copy a Home Assistant add-on config snippet when running in add-on mode

## Quick start

The app now treats first run as setup, not as "the dashboard happened to load". If the dashboard opens in demo or degraded mode, believe the banner.

### Prerequisites

- Python 3.11+
- FFmpeg
- Optional: Anthropic API key, for Claude-generated banter and ads (falls back to OpenAI or stock copy without it)
- Optional: OpenAI API key, for `gpt-4o-mini-tts` host voices and as a script generation fallback when Anthropic is unavailable
- Optional: Home Assistant long-lived token, for ambient home-state references in scripts

### Setup

```bash
cd /path/to/mammamiradio
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Edit `.env` as needed:

```dotenv
MAMMAMIRADIO_BIND_HOST=127.0.0.1
MAMMAMIRADIO_PORT=8000
ADMIN_USERNAME=admin
ADMIN_PASSWORD=
ADMIN_TOKEN=
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
HA_TOKEN=
```

### Run (Docker)

The easiest way to run mammamiradio on any platform (Windows, Mac, Linux):

```bash
cp .env.example .env
# Edit .env: set ADMIN_TOKEN and optionally ANTHROPIC_API_KEY
docker compose up
```

Open `http://localhost:8000/` for the dashboard. `ADMIN_TOKEN` must be set in `.env` (the container binds to `0.0.0.0` and requires auth).

### Run (Home Assistant add-on)

If you run Home Assistant OS or Supervised:

1. **Choose your run mode**: go to **Settings > Add-ons > Add-on Store**
2. Click the three dots menu > **Repositories**
3. Paste: `https://github.com/florianhorner/mammamiradio`
4. Find "Mamma Mi Radio" and click **Install**
5. **Connect the essentials** in Add-on Configuration: optionally `anthropic_api_key` for AI hosts
6. Start the add-on and open the dashboard from the sidebar

The add-on automatically connects to Home Assistant, so the radio hosts reference your actual home state (lights, temperature, who's home) without any extra configuration.

To play on speakers, use `media_player.play_media` with the stream URL, or add a button to your Lovelace dashboard.

### Run (macOS one-click)

```bash
./setup-mac.sh
```

This creates a `Mamma Mi Radio.app` you can drag to your Dock, plus `Dashboard.webloc` and `Listener.webloc` bookmark files. Double-click the app to start the radio and open the dashboard. Double-click `Stop Radio.command` to stop it.

### Run (terminal)

```bash
./start.sh
```

`start.sh` runs `uvicorn` with `--reload`.

Open:

- Listener: `http://localhost:8000/`
- Dashboard: `http://localhost:8000/admin`
- Raw stream: `http://localhost:8000/stream`

### Run (Conductor)

This repo ships a shared [`conductor.json`](conductor.json) for Conductor workspaces.

- setup creates `.venv`, installs app plus dev dependencies, and symlinks `.env` from `~/.config/mammamiradio/.env` when present, falling back to `$CONDUCTOR_ROOT_PATH/.env`
- run delegates to `./start.sh`, binds to `$CONDUCTOR_PORT`, isolates cache/tmp under `.context/conductor/`, and enables `MAMMAMIRADIO_ALLOW_YTDLP=true` by default for local workspaces
- archive removes the workspace runtime state

### Sharing with friends

To let others listen on your network, bind to all interfaces and set an admin password:

```dotenv
MAMMAMIRADIO_BIND_HOST=0.0.0.0
ADMIN_PASSWORD=your-secret-here
```

Share `http://<your-ip>:8000/listen` with listeners. The `/listen` page and `/stream` endpoint are public; the admin dashboard requires the password.

### Customizing your station

`radio.toml` is the station's identity. Change the station name, host personalities, ad brands, pacing, and audio settings to make it your own. Secrets (API keys, passwords) stay in `.env`.

## Fallback behavior

The station is intentionally resilient:

| Missing dependency | What happens |
| --- | --- |
| `MAMMAMIRADIO_ALLOW_YTDLP` not set | Uses a built-in Italian demo playlist instead of live charts |
| Anthropic API key or Claude request failure | Falls back to OpenAI `gpt-4o-mini` if `OPENAI_API_KEY` is set, then to stock copy. Authentication failures are memoized for 10 minutes to prevent repeated 401 retries. |
| OpenAI API key missing or request failure | Falls back to Edge TTS voice for that host |
| Home Assistant token or API failure | Continues without home context |
| Ad brands missing | Skips ad generation instead of failing startup |

If you keep a local `music/` directory with matching MP3s, the downloader will prefer that before trying `yt-dlp`.
Conductor and the HA addon enable `MAMMAMIRADIO_ALLOW_YTDLP=true` by default, so those runs prefer live charts over the bundled demo set.

## Configuration

Most station behavior lives in `radio.toml`.

`audio.bitrate` is the canonical bitrate setting for encoding, playback throttling, and ICY headers.

| Section | What it controls |
| --- | --- |
| `[station]` | Station name, language, theme |
| `[playlist]` | Shuffle behavior, repeat/artist cooldowns |
| `[pacing]` | Songs between banter, songs between ads, spots per ad break, lookahead |
| `[[hosts]]` | Host names, TTS engine (`edge` or `openai`), voices, style/personality |
| `[audio]` | Sample rate, channels, bitrate, Claude model |
| `[homeassistant]` | Whether HA context is enabled, base URL, refresh interval |
| `[[ads.brands]]` | Italian brand pool (Esselunga, Fiat, TIM, Barilla, etc.), categories, recurring-campaign spines |
| `[[ads.voices]]` | Dedicated commercial voices for ads |

The Home Assistant token is never stored in `radio.toml`. Set it via `HA_TOKEN` in `.env`.

## Routes

| Route | Method | Access | Description |
| --- | --- | --- | --- |
| `/` | GET | Public | Listener page |
| `/admin` | GET | Admin | Dashboard HTML |
| `/stream` | GET | Public | Infinite MP3 stream |
| `/healthz` | GET | Public | Liveness probe with process uptime |
| `/readyz` | GET | Public | Readiness probe with queue depth and startup status |
| `/public-status` | GET | Public | Current segment, recent log, and the real queued segments (`upcoming_mode` is `queued` or `building`) |
| `/status` | GET | Admin | Full admin JSON: queue depth, uptime, scripts, HA context, errors, and `provider_health` |
| `/api/setup/status` | GET | Admin | First-run setup status, detected run mode, and station mode |
| `/api/setup/recheck` | POST | Admin | Re-run setup probes |
| `/api/setup/addon-snippet` | GET | Admin | Copy-friendly Home Assistant add-on config snippet |
| `/api/shuffle` | POST | Admin | Shuffle playlist |
| `/api/skip` | POST | Admin | Skip current segment |
| `/api/purge` | POST | Admin | Remove queued segments |
| `/api/playlist/remove` | POST | Admin | Remove track by index |
| `/api/playlist/move` | POST | Admin | Move track with `{from, to}` |
| `/api/playlist/move_to_next` | POST | Admin | Move track to position 0 in upcoming |
| `/api/playlist/add` | POST | Admin | Add a track to the playlist |
| `/api/playlist/load` | POST | Admin | Load a playlist by URL |
| `/api/hosts` | GET | Admin | List hosts with personality settings |
| `/api/hosts/{name}/personality/reset` | POST | Admin | Reset host personality to defaults |
| `/api/pacing` | GET | Admin | Current pacing configuration |
| `/api/setup/save-keys` | POST | Admin | Save API keys via dashboard |
| `/api/capabilities` | GET | Admin | Capability flags, tier, next-step hint, connect status, and provider degradation telemetry |
| `/api/trigger` | POST | Admin | Trigger segment production |
| `/api/stop` | POST | Admin | Gracefully stop the session (skip + purge + pause producer until `/api/resume`) |
| `/api/resume` | POST | Admin | Resume a stopped session |
| `/api/credentials` | POST | Admin | Update credentials at runtime |
| `/api/clip` | POST | Public | Capture last 30s of audio into a shareable clip |
| `/clips/{id}.mp3` | GET | Public | Serve a saved clip (no auth, for sharing) |
| `/api/track-rules` | POST | Admin | Flag a reaction rule for the current track |
| `/api/listener-request` | POST | Public | Submit a song request or shoutout |
| `/api/listener-requests` | GET | Admin | List pending listener requests |
| `/api/search` | GET | Admin | Search playlist and external sources |
| `/api/playlist/add-external` | POST | Admin | Add external track from search results |

## Admin access

The public surface is `/listen`, `/stream`, `/public-status`, `/healthz`, and `/readyz`.

Admin routes are:

- always allowed from localhost unless `ADMIN_PASSWORD` is set
- protected everywhere by HTTP Basic auth when `ADMIN_PASSWORD` is set
- protected off-localhost by `X-Radio-Admin-Token` header when only `ADMIN_TOKEN` is set

If you bind to a non-loopback host, the app requires either `ADMIN_PASSWORD` or `ADMIN_TOKEN` at startup.

## Project layout

```text
mammamiradio/
  main.py             FastAPI app startup/shutdown
  config.py           radio.toml + env parsing and validation
  producer.py         segment generation loop
  streamer.py         routes, auth gates, playback fan-out
  scheduler.py        segment selection and upcoming preview
  playlist.py         Charts, local, and demo playlist loading
  downloader.py       local file / yt-dlp / placeholder fallback
  scriptwriter.py     Anthropic/OpenAI prompts for banter and ads
  tts.py              TTS synthesis (Edge TTS + OpenAI gpt-4o-mini-tts)
  normalizer.py       FFmpeg helpers
  ha_context.py       Home Assistant polling and formatting
  ha_enrichment.py    Pure HA event derivation (state diffing, event pruning, numeric passthrough)
  clip.py             WTF clip extraction from ring buffer
  track_rationale.py  "Why this track?" rationale for listener UI
  track_rules.py      Per-track personality rules flagged by admin
  capabilities.py     Capability flags, tier derivation, next-step hints
  persona.py          Compounding listener memory, arc phases, session tracking
  song_cues.py        Per-track machine-derived memory: anthems, skip bits, LLM reactions
  context_cues.py     Time-of-day and cultural context for prompts
  audio_quality.py    Audio quality gate for spoken segments before queuing
  setup_status.py     First-run setup status classification (legacy, kept for /status compat)
  sync.py             SQLite database initialization and schema migration
  models.py           core data models and station state
  dashboard.html      listener-facing dashboard at /
  admin.html          admin control room at /admin
radio.toml            station config
start.sh              local dev entrypoint
Dockerfile            standalone Docker image
docker-compose.yml    one-command Docker run
ha-addon/             Home Assistant add-on scaffold
tests/                pytest coverage
```

## Development

```bash
make test
```

Or run pytest directly:

```bash
pytest tests/
```

Useful direct run:

```bash
source .venv/bin/activate
python -m uvicorn mammamiradio.main:app --reload --reload-dir mammamiradio
```

Generated runtime directories:

- `cache/` for downloaded audio
- `tmp/` for normalized audio, logs, and temporary assets

See `ARCHITECTURE.md` for runtime flow, `CONTRIBUTING.md` for local development, `TROUBLESHOOTING.md` for common failures, and `OPERATIONS.md` for the current run/deploy model.
