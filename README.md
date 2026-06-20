<p align="center">
  <img src="docs/banner.png" width="1280" alt="Mamma Mi Radio">
</p>

# Mamma Mi Radio

An AI-powered Italian radio station that nobody questions is real. Two hosts banter between live Italian charts. Optional Home Assistant context lets them reference your actual home — lights, temperature, who's at the door. The format absorbs AI imperfection as authenticity.

```bash
git clone https://github.com/florianhorner/mammamiradio.git && cd mammamiradio
cp .env.example .env
# ADMIN_TOKEN is optional — auto-generated if unset. Then:
docker compose up
```

→ **[Read the code](docs/REPO_MAP.md)** · **[Ship to your home](ha-addon/README.md)** · **[Contribute](CONTRIBUTING.md)** · **[Changelog](CHANGELOG.md)**

<p align="center">
  <img src="https://img.shields.io/github/stars/florianhorner/mammamiradio?style=flat" alt="GitHub stars">
  <img src="https://img.shields.io/github/actions/workflow/status/florianhorner/mammamiradio/quality.yml?branch=main&label=CI&style=flat" alt="CI">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue?style=flat" alt="Python 3.11+">
</p>

---

> *We played this at a dinner party. Seven guests. Nobody questioned it was a real Italian radio station.*

---

## Quick Start

### Docker (any platform)

```bash
git clone https://github.com/florianhorner/mammamiradio.git
cd mammamiradio && cp .env.example .env
# ADMIN_TOKEN is optional — the container auto-generates one and saves it to
# /data/admin_token if unset (read it with: docker compose exec mammamiradio cat /data/admin_token)
docker compose up
```

Open `http://localhost:8000` for the listener page (`/admin` for the control room). Music plays from live Italian charts when `MAMMAMIRADIO_ALLOW_YTDLP=true` (enabled in `docker-compose.yml`), from CC-licensed tracks via Jamendo (set `jamendo_client_id` in `radio.toml`), or from local files dropped into `music/`.

**Verify it's working:**

```bash
curl http://localhost:8000/healthz   # → {"status":"ok","uptime_s":...}
```

Expect music within ~10s on a warm cache. First boot can take 30–60s while yt-dlp pulls the Italian chart. The dashboard's tier badge (Demo Radio / Full AI Radio / Connected Home) tells you what's active.

### Home Assistant Add-on

1. Go to **Settings > Add-ons > Add-on Store** > three-dot menu > **Repositories**
2. Paste: `https://github.com/florianhorner/mammamiradio`
3. Install "Mamma Mi Radio" and start

The add-on wires Home Assistant context automatically. Hosts can reference your lights, temperature, and who's home.

<details>
<summary>More run modes: macOS app, terminal, Conductor</summary>

#### macOS one-click

```bash
./setup-mac.sh
```

Creates a `Mamma Mi Radio.app` for your Dock. Double-click to start, opens the dashboard automatically.

#### Terminal

```bash
# Prerequisites: Python 3.11+, FFmpeg
python3.11 -m venv .venv && source .venv/bin/activate && pip install -e .
./start.sh
```

Open `http://localhost:8000` for the listener page, `/admin` for the control room.

Standalone defaults `MAMMAMIRADIO_ALLOW_YTDLP=false` (copyright-safe), so set it to `true` or drop MP3s into `music/` to hear music — Docker and Conductor enable it for you.

#### Conductor

This repo ships `scripts/conductor-*.sh` lifecycle hooks (wired through Conductor's own workspace settings) that handle `.venv` creation, port binding, cache isolation, and `MAMMAMIRADIO_ALLOW_YTDLP=true` by default.

</details>

## What You'll Experience

**It just plays.** `docker compose up` and you have a working radio station. Music from live Italian charts (yt-dlp), CC-licensed tracks via Jamendo, or local files dropped into `music/`. No setup wizard, no API keys needed to hear sound.

**Nobody notices it's AI.** Two Italian hosts banter between tracks, roast each other, react to the music. The format absorbs AI imperfection... timing gaps and rough edges read as radio character, not failure. There is no uncanny valley in radio.

**"How did it know?"** Connect Home Assistant and the hosts reference your actual home. Lights, temperature, who's at the door. "The coffee machine says someone beat you to it." These are moments real radio structurally cannot do.

**The ads are the best part.** Fictional Italian brands with absurd claims, pharma disclaimers read at double speed, dedicated commercial voices. Guests at live tests found them uncanny in a good way... entertaining enough that people actually listen.

**It remembers you.** Returning listeners get recognized. Inside jokes compound across sessions. Hosts build a persona around your listening patterns. The station gets better the more you use it.

**Share the moment.** Hit the clip button and the link lands on your clipboard, ready to paste. Ads and host banter capture the whole bit (not just 30 seconds), so you can send friends the entire insane ad your radio just made up.

## Screenshots

<p align="center">
  <img src="docs/screenshots/listener.png" width="700" alt="Listener dashboard with now-playing, up-next queue, and radio dial animation">
</p>
<p align="center"><em>Listener dashboard: now playing, up-next queue, radio dial tuning animation on first load</em></p>

<p align="center">
  <img src="docs/screenshots/dashboard.png" width="700" alt="Admin control room with music, radio, and engine tabs">
</p>
<p align="center"><em>Admin control room: music queue, host config, pacing sliders, engine diagnostics</em></p>

## How It Works

```text
Charts / Jamendo CC / local files / demo -> Producer -> asyncio.Queue -> Playback loop -> /stream
                                         |                                  |
Claude/OpenAI -> banter/ad scripts ------+                                  +-> /public-status, /status
Edge TTS -> dialogue + ads --------------+
FFmpeg -> normalize / mix / concat ------+
Home Assistant -> optional context ------+
```

- `producer.py` keeps a few segments queued ahead of playback.
- `scheduler.py` decides whether the next segment is music, banter, or an ad break.
- `streamer.py` plays one station timeline and fans out MP3 chunks to all connected listeners.

## Three Tiers

The station plays immediately. Add keys to unlock more:

| Tier | What you get | What you need |
|------|-------------|---------------|
| **Demo Radio** | Music + silence fallback for banter and ads | Nothing. Works out of the box. |
| **Full AI Radio** | Claude-written hosts with distinct personalities, reactive banter, dynamic ads | `ANTHROPIC_API_KEY` (falls back to OpenAI) |
| **Connected Home** | Hosts reference live home state: lights, temperature, presence, appliances | Home Assistant + `HA_TOKEN` |

## Never Crashes, Always Plays

The station degrades gracefully instead of failing:

| What's missing | What happens |
|----------------|-------------|
| `MAMMAMIRADIO_ALLOW_YTDLP` not set | Skips chart downloads; falls back to Jamendo CC music, then local `music/` files, then bundled demo assets |
| `jamendo_client_id` not set | Skips Jamendo; falls back to local `music/` files, then bundled demo assets |
| Anthropic API key | Falls back to OpenAI via the active quality profile (`gpt-5.5` for creative copy in balanced/premium), then stock copy |
| OpenAI / Azure / ElevenLabs TTS key | Provider-routed voices fall back to their configured Edge voices |
| Home Assistant token | Continues without home context |
| Ad brands in config | Skips ads instead of crashing |

Local `music/` directory MP3s are always preferred over network downloads.

## Configuration

Most station behavior lives in `radio.toml`:

| Section | What it controls |
|---------|-----------------|
| `[station]` | Station name, language, theme |
| `[playlist]` | Shuffle behavior, repeat/artist cooldowns, Jamendo CC music (`jamendo_client_id`, `jamendo_tags`, `jamendo_limit`) |
| `[pacing]` | Songs between banter, songs between ads, spots per break |
| `[[hosts]]` | Host names, TTS engine (`edge`/`openai`/`azure`/`elevenlabs`), voices, personality |
| `[audio]` | Sample rate, channels, bitrate, Claude model |
| `[homeassistant]` | HA context toggle, base URL, refresh interval |
| `[[ads.brands]]` | Fictional Italian brand pool, categories, campaign spines |
| `[[ads.voices]]` | Dedicated commercial voices for ads, with optional provider engines and Edge fallbacks |

Secrets (API keys, passwords) stay in `.env`, never in `radio.toml`.

To audition the current cast plus every built-in Edge/OpenAI/Azure catalog voice
your configured keys can actually synthesize:

```bash
.venv/bin/python scripts/audition_tts_voices.py --include-catalog --providers all
```

Clips and a `manifest.json` are written under `tmp/voice-auditions/`. Providers
without credentials are skipped instead of silently falling back to Edge.

<details>
<summary>Sharing with friends</summary>

Bind to all interfaces and set an admin password:

```dotenv
MAMMAMIRADIO_BIND_HOST=0.0.0.0
ADMIN_PASSWORD=your-secret-here
```

Share `http://<your-ip>:8000` with listeners. The dashboard and stream are public; admin routes require the password.

</details>

<details>
<summary>Customizing your station</summary>

`radio.toml` is the station's identity. Change the station name, host personalities, ad brands, pacing, and audio settings to make it your own.

The admin dashboard at `/admin` lets you adjust pacing, skip tracks, shuffle the playlist, manage the queue with drag-and-drop, and configure host personalities... all live, without restarting.

</details>

## Development

```bash
make test          # run tests with coverage
make check         # lint + typecheck + coverage gate
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for full local setup, [docs/architecture.md](docs/architecture.md) for runtime flow and API routes, [docs/troubleshooting.md](docs/troubleshooting.md) for common failures, and [docs/operations.md](docs/operations.md) for deploy reality.

## Contributors

Thanks to the people who've shaped `mammamiradio`:

- [@florianhorner](https://github.com/florianhorner) — maintainer
- [@ashika-rai-n](https://github.com/ashika-rai-n) — dashboard CSS/JS extraction into `/static/` ([PR #203](https://github.com/florianhorner/mammamiradio/pull/203), [commit `2028d40`](https://github.com/florianhorner/mammamiradio/commit/2028d408499cd98b15c82a39a5cd3912cdfbb1d9))

Want to contribute? See [CONTRIBUTING.md](CONTRIBUTING.md) and pick any open issue. First-time contributors are especially welcome — and are protected by the [merge-first protocol](CLAUDE.md#first-time-contributor-protocol) so your PR lands before any refactoring on top.

## License

The **code** in this repository is licensed under [Apache-2.0](LICENSE).

That license covers the source only. It does **not** grant rights to the music the
station plays (charts pulled via yt-dlp, Jamendo tracks, or your own local files) or
to the AI-generated host banter and ads. You are responsible for the rights to
whatever your station plays and says.

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=florianhorner/mammamiradio&type=Date)](https://star-history.com/#florianhorner/mammamiradio&Date)
