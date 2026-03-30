# mammamiradio

AI-powered Italian radio station engine. It streams a continuous MP3 from your Spotify library, layers in Claude-written host banter and absurd AI-generated ads, and exposes both a control-plane dashboard and a public listener page.

The app is designed to degrade gracefully. If Spotify auth is missing, it falls back to a demo playlist. If go-librespot is unavailable, it can still synthesize a station from local files, `yt-dlp`, or generated placeholder audio. If Anthropic is unavailable, banter and ads fall back to short stock lines instead of crashing the station.

## What it does

- Streams a live MP3 station at `/stream`
- Serves an admin dashboard at `/` and a public listener page at `/listen`
- Rotates between music, host banter, and multi-spot ad breaks
- Auto-transfers Spotify playback to the `mammamiradio` device when possible
- Lets hosts reference live Home Assistant state when enabled
- Supports playlist mutation from the dashboard: shuffle, skip, purge, remove, reorder, play-next

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) explains the runtime, component boundaries, and the FIFO/go-librespot audio path.
- [CONTRIBUTING.md](CONTRIBUTING.md) covers local setup, test commands, and manual smoke checks.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) covers the failures you are actually likely to hit.
- [OPERATIONS.md](OPERATIONS.md) describes the current run and deploy reality.
- [CHANGELOG.md](CHANGELOG.md) tracks release notes from the current baseline forward.
- [ha-addon/README.md](ha-addon/README.md) covers Home Assistant add-on installation and usage.

## How it works

```text
Spotify / liked songs / demo playlist -> Producer -> asyncio.Queue -> Playback loop -> /stream
                                     |                                  |
Claude -> banter/ad scripts ---------+                                  +-> /public-status, /status
Edge TTS -> dialogue + ads ----------+
FFmpeg -> normalize / mix / concat --+
Home Assistant -> optional context --+
```

- `producer.py` keeps a few segments queued ahead of playback.
- `scheduler.py` decides whether the next segment is music, banter, or an ad break.
- `streamer.py` plays one station timeline and fans out MP3 chunks to all connected listeners.
- `spotify_player.py` keeps a persistent reader on the go-librespot FIFO so macOS does not throw `ENXIO` and skip tracks.

## Quick start

### Prerequisites

- Python 3.11+
- FFmpeg
- Spotify client credentials (client ID and secret from [Spotify Developer Dashboard](https://developer.spotify.com/dashboard))
- go-librespot, for real Spotify device playback and capture
- Optional: Anthropic API key, for Claude-generated banter and ads (falls back to stock copy without it)
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
SPOTIFY_CLIENT_ID=
SPOTIFY_CLIENT_SECRET=
ANTHROPIC_API_KEY=
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

### Run (Home Assistant add-on)

If you run Home Assistant OS or Supervised:

1. Go to **Settings > Add-ons > Add-on Store**
2. Click the three dots menu > **Repositories**
3. Paste: `https://github.com/florianhorner/fakeitaliradio`
4. Find "Mamma Mi Radio" and click **Install**
5. Configure your Anthropic API key (and optionally Spotify credentials) in the add-on settings
6. Start the add-on — it appears in your sidebar

The add-on automatically connects to Home Assistant, so the radio hosts reference your actual home state (lights, temperature, who's home) without any extra configuration.

To play on speakers, use `media_player.play_media` with the stream URL, or add a button to your Lovelace dashboard.

### Run (macOS one-click)

```bash
./setup-mac.sh
```

This creates a `Radio Italì.app` you can drag to your Dock, plus `Dashboard.webloc` and `Listener.webloc` bookmark files. Double-click the app to start the radio and open the dashboard. Double-click `Stop Radio.command` to stop it.

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
| Spotify client credentials | Uses a built-in demo Italian playlist |
| go-librespot or Spotify device connection | Falls back to local files, then `yt-dlp`, then placeholder audio |
| Anthropic API key or Claude request failure | Uses simple fallback banter or ad copy |
| Home Assistant token or API failure | Continues without home context |
| Ad brands missing | Skips ad generation instead of failing startup |

If you keep a local `music/` directory with matching MP3s, the downloader will prefer that before trying `yt-dlp`.

## Configuration

Most station behavior lives in `radio.toml`.

`audio.bitrate` is the canonical bitrate setting for encoding, playback throttling, and ICY headers.

| Section | What it controls |
| --- | --- |
| `[station]` | Station name, language, theme |
| `[playlist]` | Spotify playlist URL, shuffle behavior |
| `[pacing]` | Songs between banter, songs between ads, spots per ad break, lookahead |
| `[[hosts]]` | Host names, Edge voices, style/personality |
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
| `/public-status` | GET | Public | Current segment, recent log, upcoming preview |
| `/status` | GET | Admin | Full admin JSON: queue depth, uptime, scripts, HA context, errors |
| `/api/logs` | GET | Admin | Recent go-librespot logs |
| `/api/shuffle` | POST | Admin | Shuffle playlist |
| `/api/skip` | POST | Admin | Skip current segment |
| `/api/purge` | POST | Admin | Remove queued segments |
| `/api/playlist/remove` | POST | Admin | Remove track by index |
| `/api/playlist/move` | POST | Admin | Move track with `{from, to}` |
| `/api/playlist/move_to_next` | POST | Admin | Move track to position 0 in upcoming |
| `/api/search` | GET | Admin | Search Spotify for tracks |
| `/api/playlist/add` | POST | Admin | Add a track to the playlist |
| `/api/playlist/load` | POST | Admin | Load a Spotify playlist by URL |

## Admin access

The public surface is `/listen`, `/stream`, and `/public-status`.

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
  scriptwriter.py     Claude prompts for banter and ads
  tts.py              Edge TTS synthesis
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
