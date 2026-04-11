<p align="center">
  <img src="mammamiradio/logo.svg" width="128" height="128" alt="Mamma Mi Radio logo">
</p>

<h1 align="center">mammamiradio</h1>

<p align="center">AI-powered Italian radio station engine. It streams a continuous MP3 from live Italian charts or local music, layers in Claude-written host banter and absurd AI-generated ads, and exposes both a control-plane dashboard and a public listener page.</p>

The app is designed to degrade gracefully. Music comes from live Italian charts when `MAMMAMIRADIO_ALLOW_YTDLP=true`, otherwise from a bundled demo playlist or local files. If Anthropic is unavailable, banter and ads fall back to short stock lines instead of crashing the station.

## Screenshots

### Admin Dashboard

The control plane at `/admin` lets you manage the station: queue depth, host personality sliders, segment log, upcoming queue, and live banter scripts.

![Dashboard](docs/screenshots/dashboard.png)

### Listener Page

The public listener at `/listen` is an art-deco styled player with now-playing info, up-next preview, callback corner, and recently-played log.

![Listener](docs/screenshots/listener.png)

## What it does

- Streams a live MP3 station at `/stream`
- Serves a public listener page at `/` and an admin dashboard at `/admin`
- Rotates between music, host banter, and multi-spot ad breaks
- Lets hosts reference live Home Assistant state when enabled
- Supports playlist mutation from the dashboard: shuffle, skip, purge, remove, reorder, play-next
- Stop and resume sessions from the admin control room
- Remembers returning listeners across sessions with compounding persona memory

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
# Edit .env: set ADMIN_TOKEN and optionally ANTHROPIC_API_KEY, SPOTIFY_CLIENT_ID/SECRET
docker compose up
```

Open `http://localhost:8000/` for the dashboard. `ADMIN_TOKEN` must be set in `.env` (the container binds to `0.0.0.0` and requires auth).

For Spotify OAuth in local development, register `http://127.0.0.1:8000/spotify/callback` as the redirect URI in your Spotify app settings. If you want to use a stable HTTPS origin instead, set `MAMMAMIRADIO_SPOTIFY_REDIRECT_BASE_URL=https://your-domain` and register `https://your-domain/spotify/callback`.

### Run (Home Assistant add-on)

If you run Home Assistant OS or Supervised:

1. **Choose your run mode**: go to **Settings > Add-ons > Add-on Store**
2. Click the three dots menu > **Repositories**
3. Paste: `https://github.com/florianhorner/mammamiradio`
4. Find "Mamma Mi Radio" and click **Install**
5. **Connect the essentials** in Add-on Configuration:
   `spotify_client_id`, `spotify_client_secret`, `playlist_spotify_url`, and optionally `anthropic_api_key`
6. **Run preflight checks** by starting the add-on and opening the dashboard
7. **Launch your first station** once the app tells you whether you are in `Real Spotify Mode`, `Demo Mode`, or `Degraded`

The add-on automatically connects to Home Assistant, so the radio hosts reference your actual home state (lights, temperature, who's home) without any extra configuration.

To play on speakers, use `media_player.play_media` with the stream URL, or add a button to your Lovelace dashboard.

### Run (macOS one-click)

```bash
./setup-mac.sh
```

This creates a `Malamie Radio.app` you can drag to your Dock, plus `Dashboard.webloc` and `Listener.webloc` bookmark files. Double-click the app to start the radio and open the dashboard. Double-click `Stop Radio.command` to stop it.

### Run (terminal)

```bash
./start.sh
```

`start.sh`:

- creates the FIFO at `/tmp/mammamiradio.pcm`
- starts `go-librespot` if it is not already running
- keeps a fallback drain process alive across hot reloads
- runs `uvicorn` with `--reload`

Open:

- Dashboard: `http://localhost:8000/`
- Listener: `http://localhost:8000/listen`
- Raw stream: `http://localhost:8000/stream`

On first full Spotify run, select `mammamiradio` as the playback device in Spotify. The app also tries to auto-transfer playback when the device appears.

### Run (Conductor)

This repo ships a shared [`conductor.json`](conductor.json) for Conductor workspaces.

- setup creates `.venv`, installs app plus dev dependencies, and symlinks `.env` from `~/.config/mammamiradio/.env` when present, falling back to `$CONDUCTOR_ROOT_PATH/.env`
- run delegates to `./start.sh`, binds to `$CONDUCTOR_PORT`, isolates FIFO/cache/tmp/go-librespot state under `.context/conductor/`, and enables `MAMMAMIRADIO_ALLOW_YTDLP=true` by default for local workspaces
- archive stops workspace-owned helper processes and removes the workspace runtime state

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
| Spotify client credentials | Uses live Italian charts when `MAMMAMIRADIO_ALLOW_YTDLP=true`, otherwise a built-in Italian jazz demo playlist |
| go-librespot or Spotify device connection | Falls back to local files, then `yt-dlp`, then placeholder audio |
| Anthropic API key or Claude request failure | Falls back to OpenAI `gpt-4o-mini` if `OPENAI_API_KEY` is set, then to stock copy |
| OpenAI API key missing or request failure | Falls back to Edge TTS voice for that host |
| Home Assistant token or API failure | Continues without home context |
| Ad brands missing | Skips ad generation instead of failing startup |

If you keep a local `music/` directory with matching MP3s, the downloader will prefer that before trying `yt-dlp`.
Conductor's default run script enables `MAMMAMIRADIO_ALLOW_YTDLP=true` for local workspaces, so those runs prefer live charts over the bundled demo set when Spotify metadata is unavailable.

## Configuration

Most station behavior lives in `radio.toml`.

`audio.bitrate` is the canonical bitrate setting for encoding, playback throttling, and ICY headers.

| Section | What it controls |
| --- | --- |
| `[station]` | Station name, language, theme |
| `[playlist]` | Spotify playlist URL, source selection, shuffle behavior |
| `[pacing]` | Songs between banter, songs between ads, spots per ad break, lookahead |
| `[[hosts]]` | Host names, TTS engine (`edge` or `openai`), voices, style/personality |
| `[audio]` | Sample rate, channels, bitrate, FIFO path, go-librespot settings, Claude model |
| `[homeassistant]` | Whether HA context is enabled, base URL, refresh interval |
| `[[ads.brands]]` | Fictional brand pool, categories, recurring-campaign weighting |
| `[[ads.voices]]` | Dedicated commercial voices for ads |

The Home Assistant token is never stored in `radio.toml`. Set it via `HA_TOKEN` in `.env`.

## Routes

| Route | Method | Access | Description |
| --- | --- | --- | --- |
| `/` | GET | Admin | Dashboard HTML |
| `/listen` | GET | Public | Minimal player UI |
| `/stream` | GET | Public | Infinite MP3 stream |
| `/healthz` | GET | Public | Liveness probe with process uptime |
| `/readyz` | GET | Public | Readiness probe with queue depth and startup status |
| `/public-status` | GET | Public | Current segment, recent log, and the real queued segments (`upcoming_mode` is `queued` or `building`) |
| `/status` | GET | Admin | Full admin JSON: queue depth, uptime, scripts, HA context, errors |
| `/api/logs` | GET | Admin | Recent go-librespot logs |
| `/api/setup/status` | GET | Admin | First-run setup status, detected run mode, and station mode |
| `/api/setup/recheck` | POST | Admin | Re-run setup probes for Spotify, FFmpeg, and go-librespot |
| `/api/setup/addon-snippet` | GET | Admin | Copy-friendly Home Assistant add-on config snippet |
| `/api/shuffle` | POST | Admin | Shuffle playlist |
| `/api/skip` | POST | Admin | Skip current segment |
| `/api/purge` | POST | Admin | Remove queued segments |
| `/api/playlist/remove` | POST | Admin | Remove track by index |
| `/api/playlist/move` | POST | Admin | Move track with `{from, to}` |
| `/api/playlist/move_to_next` | POST | Admin | Move track to position 0 in upcoming |
| `/api/search` | GET | Admin | Search Spotify for tracks |
| `/api/playlist/add` | POST | Admin | Add a track to the playlist |
| `/api/playlist/load` | POST | Admin | Load a Spotify playlist by URL (legacy compatibility) |
| `/api/spotify/source-options` | GET | Admin | Available sources: user playlists, Liked Songs |
| `/api/spotify/source/select` | POST | Admin | Switch source to playlist, liked_songs, or URL |
| `/api/spotify/auth-status` | GET | Admin | Spotify OAuth connection state |
| `/api/spotify/disconnect` | POST | Admin | Revoke Spotify connection |
| `/api/hosts` | GET | Admin | List hosts with personality settings |
| `/api/hosts/{name}/personality/reset` | POST | Admin | Reset host personality to defaults |
| `/api/pacing` | GET | Admin | Current pacing configuration |
| `/api/setup/save-keys` | POST | Admin | Save API keys via dashboard |
| `/api/capabilities` | GET | Admin | Capability flags, tier, next-step hint, connect status |
| `/api/trigger` | POST | Admin | Trigger segment production |
| `/api/stop` | POST | Admin | Gracefully stop the session (skip + purge + pause producer until `/api/resume`) |
| `/api/resume` | POST | Admin | Resume a stopped session |
| `/api/credentials` | POST | Admin | Update credentials at runtime |

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
  spotify_player.py   go-librespot process + FIFO capture
  playlist.py         Spotify playlist fetch + demo fallback
  downloader.py       local file / yt-dlp / placeholder fallback
  scriptwriter.py     Anthropic/OpenAI prompts for banter and ads
  tts.py              TTS synthesis (Edge TTS + OpenAI gpt-4o-mini-tts)
  normalizer.py       FFmpeg helpers
  ha_context.py       Home Assistant polling and formatting
  models.py           core data models and station state
  dashboard.html      admin UI
  listener.html       public listener UI
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
