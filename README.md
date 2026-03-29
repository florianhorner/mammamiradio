# fakeitaliradio

AI-powered fake Italian radio station. Streams music from your Spotify library with AI-generated host banter and absurd fake ads, all in Italian.

Two hosts (configurable) riff on the tracks, keep running jokes alive across segments, and periodically cut to ad breaks for fictional brands like "Negroni as a Service." The whole thing streams as a live MP3 you can open in any browser or audio player.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) explains the runtime, component boundaries, and the FIFO/go-librespot audio path.
- [CONTRIBUTING.md](CONTRIBUTING.md) covers local setup, test commands, and manual smoke checks.
- [CHANGELOG.md](CHANGELOG.md) tracks release notes from the current `0.1.0` baseline forward.

## How it works

```
Spotify (go-librespot) ──→ Producer ──→ Queue ──→ Streamer ──→ /stream (MP3)
Claude (scripts)       ──↗               ↑
Edge TTS (voices)      ──↗          Scheduler decides:
FFmpeg (audio)         ──↗          music → banter → ads
```

The **producer** generates segments ahead of time (music, banter, or ads) and pushes them to an async queue. The **streamer** pulls segments from the queue and sends MP3 chunks to listeners, throttled to the playback bitrate so the dashboard stays in sync with what you hear. The **scheduler** decides what comes next based on pacing rules (e.g., banter every 2 songs, ads every 4).

## Quick start

### Prerequisites

- Python 3.11+
- [FFmpeg](https://ffmpeg.org/) (for audio processing)
- [go-librespot](https://github.com/devgianlu/go-librespot) (for Spotify playback)
- A Spotify account (free or premium)
- An [Anthropic API key](https://console.anthropic.com/) (for Claude, used to write scripts)

### Setup

```bash
# Clone and install
cd /path/to/fakeitaliradio
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .

# Configure
cp .env.example .env
# Edit .env with your credentials:
#   SPOTIFY_CLIENT_ID=
#   SPOTIFY_CLIENT_SECRET=
#   ANTHROPIC_API_KEY=

# Run
./start.sh
```

`start.sh` handles the FIFO pipe, launches go-librespot in the background, and starts the FastAPI server with hot reload on port 8000. By default it binds to `127.0.0.1`; override with `FAKEITALIRADIO_BIND_HOST` and `FAKEITALIRADIO_PORT`.

### Listen

- **Dashboard** (control plane): http://localhost:8000/
- **Listener** (minimal player): http://localhost:8000/listen
- **Raw stream**: http://localhost:8000/stream

On first run, open Spotify and select "fakeitaliradio" as your playback device. The station will auto-transfer playback if possible.

## Configuration

Everything lives in `radio.toml`. Key sections:

| Section | What it controls |
|---------|-----------------|
| `[station]` | Name, language, theme description |
| `[playlist]` | Spotify playlist URL (or empty for liked songs), shuffle |
| `[pacing]` | Songs between banter/ads, spots per ad break, lookahead depth |
| `[[hosts]]` | Host personalities: name, Edge TTS voice, style description |
| `[[ads.brands]]` | Fictional brands: name, tagline, category, recurring flag |
| `[[ads.voices]]` | Commercial voice actors (separate from hosts) |
| `[audio]` | Sample rate, bitrate, FIFO path, go-librespot settings, Claude model |

See `radio.toml` for a fully commented example.

## API

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Dashboard (control plane UI) |
| `/listen` | GET | Listener (minimal playback UI) |
| `/stream` | GET | Audio stream (infinite MP3) |
| `/public-status` | GET | Public listener status: now playing, recent stream log, upcoming |
| `/status` | GET | Admin JSON: queue depth, uptime, now playing, logs |
| `/api/shuffle` | POST | Shuffle the playlist |
| `/api/skip` | POST | Skip the current segment |
| `/api/purge` | POST | Drain all queued segments |
| `/api/playlist/remove` | POST | Remove a track by index |
| `/api/playlist/move` | POST | Reorder a track (`{from, to}`) |
| `/api/playlist/move_to_next` | POST | Move a track to play next |
| `/api/logs` | GET | Recent go-librespot logs |

## Segments

The producer generates three types of segments:

- **Music**: Downloads track audio via Spotify (go-librespot FIFO capture) with yt-dlp and local file fallbacks. Normalized to target loudness.
- **Banter**: Claude writes dialogue between hosts based on recent tracks and running jokes. Synthesized with Edge TTS, one voice per host.
- **Ad breaks**: Claude writes scripts for fictional brands. Multi-part audio with voice acting, SFX, bumper jingles, and music beds. Supports campaign arcs where the same brand gets callbacks across breaks.

The scheduler cycles through them: a few songs, then banter, a few more, then an ad break. Pacing is configurable in `radio.toml`.

## Project layout

Core application files live in `fakeitaliradio/`. Everything else:

| Path | What it is |
|------|-----------|
| `fakeitaliradio/` | Application runtime code |
| `tests/` | pytest tests |
| `radio.toml` | Station config (tracked, safe defaults) |
| `.claude/skills/gstack` | Vendored agent/tooling support — not application code |
| `tmp/` | Generated runtime audio and logs (gitignored) |
| `cache/` | Downloaded/cached tracks (gitignored) |
| `.context/` | Conductor agent collaboration artifacts (gitignored) |

## Dependencies

FastAPI, Uvicorn, Spotipy, Anthropic SDK, edge-tts, yt-dlp, httpx, Pydantic. Full list in `pyproject.toml`.

## Admin Access

The listener surface (`/listen`, `/stream`, `/public-status`) is public. The control plane (`/`), admin status, logs, and playlist mutation routes are restricted:

- If the app is bound to localhost, they are accessible from localhost without extra auth.
- If you bind to a non-local interface, set `ADMIN_PASSWORD` or `ADMIN_TOKEN`.
- `ADMIN_PASSWORD` uses HTTP Basic auth with `ADMIN_USERNAME` (default `admin`) and is the right choice for browser/dashboard access.
- `ADMIN_TOKEN` can be sent via the `X-Radio-Admin-Token` header for scripted access to non-local admin routes.
