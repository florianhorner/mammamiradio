<p align="center">
  <img src="docs/banner.png" alt="Mamma Mi Radio — AI-generated Italian radio station with Claude-written hosts, absurd fake ads, and Home Assistant context" width="880" />
</p>

<h1 align="center">Mamma Mi Radio</h1>

<p align="center">
  <strong>An AI-generated Italian radio station that sounds real enough to fool a dinner party.</strong><br />
  Claude-written hosts, absurd fake-brand ads, live chart music, and optional Home Assistant context — self-hosted.
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> &middot;
  <a href="#what-youll-experience">Experience</a> &middot;
  <a href="#three-tiers">Tiers</a> &middot;
  <a href="ARCHITECTURE.md">Architecture</a> &middot;
  <a href="CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <a href="https://github.com/florianhorner/mammamiradio/actions/workflows/quality.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/florianhorner/mammamiradio/quality.yml?branch=main&label=CI&color=2563eb" /></a>
  <a href="https://github.com/florianhorner/mammamiradio/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/florianhorner/mammamiradio?style=flat&color=2563eb" /></a>
  <a href="https://github.com/florianhorner/mammamiradio/commits/main"><img alt="Last commit" src="https://img.shields.io/github/last-commit/florianhorner/mammamiradio?color=2563eb" /></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11+-64748b" />
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.115-64748b" />
  <img alt="Home Assistant add-on" src="https://img.shields.io/badge/HA-add--on-64748b" />
</p>

---

## Why

**Real radio is scripted, sterile, and the ads are for brands you know. This one isn't.**

Two Italian hosts roast each other between tracks. The ads are for fictional brands with absurd claims read at double-speed by a dedicated commercial voice. The format absorbs AI imperfection — timing gaps and rough edges read as *radio character*, not failure. There is no uncanny valley in radio.

> *"We played this at a dinner party. Seven guests. Nobody questioned it was a real Italian radio station."*

## Quick Start

### Docker (any platform)

Prereqs: Docker Desktop or Docker Engine.

```bash
git clone https://github.com/florianhorner/mammamiradio.git
cd mammamiradio
cp .env.example .env
# Required: set ADMIN_TOKEN in .env (the container binds 0.0.0.0)
docker compose up
```

Open `http://localhost:8000` for the listener page. `/admin` is the control room, `/dashboard` is the authenticated dashboard. Music plays from live Italian charts by default (`MAMMAMIRADIO_ALLOW_YTDLP=true` in `docker-compose.yml`) — drop MP3s into `music/` to mix in your own.

> Add `ANTHROPIC_API_KEY` to `.env` (get one at [console.anthropic.com](https://console.anthropic.com/)) to unlock Claude-written hosts and ads. Without a key the station plays music with stock banter.

### Home Assistant Add-on

1. In Home Assistant, go to **Settings &rarr; Add-ons &rarr; Add-on Store** &rarr; three-dot menu &rarr; **Repositories**
2. Paste: `https://github.com/florianhorner/mammamiradio`
3. Install **Mamma Mi Radio**, start it, open from the sidebar

The add-on wires Home Assistant context automatically — hosts reference your lights, temperature, presence, and weather. Full setup and config options: [ha-addon/README.md](ha-addon/README.md).

<details>
<summary>Other ways to run (macOS app, terminal, Conductor)</summary>

**macOS one-click app:**

```bash
./setup-mac.sh
```

Creates `Mamma Mi Radio.app` in your Dock. Double-click starts the station and opens the dashboard.

**Terminal (Python 3.11+, FFmpeg):**

```bash
python3.11 -m venv .venv && source .venv/bin/activate && pip install -e .
./start.sh
```

**Conductor:** ships a [`conductor.json`](conductor.json) handling `.venv`, port binding, cache isolation, and chart music by default.

</details>

## Features

- 🎙️ **Two Italian hosts written by Claude** &mdash; distinct personalities (energy / warmth / chaos axes), reactive banter, running jokes that compound across sessions.
- 📻 **Absurd fake-brand ads** &mdash; six formats (classic pitch, testimonial, duo scene, live remote, late-night whisper, PSA), brand motif jingles, pharma disclaimers read at double speed by a dedicated commercial voice.
- 🎵 **Live Italian chart music + your MP3s** &mdash; Apple Music Italy RSS top 100 via yt-dlp, blended with anything in `music/`. Auto-refreshes every 90 minutes during long sessions.
- 🏠 **Home Assistant context (optional)** &mdash; hosts reference live home state: lights, temperature, who's home, coffee machine, vacuums, terrace lights. Things real radio structurally cannot do.
- 🧠 **Compounding listener memory** &mdash; SQLite-backed persona tracks session count, arc phase (stranger &rarr; old_friend), motifs, and anthems. The station gets better the more you use it.
- ✂️ **One-tap clip sharing** &mdash; `POST /api/clip` captures the last 30 seconds from a ring buffer. Send friends the insane ad your radio just made up.
- 🛡️ **Never crashes, always plays** &mdash; producer exceptions insert silence instead of dying. Anthropic &rarr; OpenAI &rarr; stock copy. yt-dlp &rarr; local files &rarr; silence. The stream never goes dark.
- 🎛️ **Live control room** &mdash; `/admin` lets you adjust pacing, skip, shuffle, drag-and-drop queue, retune host personalities, hot-reload the scriptwriter without interrupting the stream.

## What You'll Experience

**It just plays.** `docker compose up` gives you a working radio station. No setup wizard, no API keys needed to hear sound.

**Nobody notices it's AI.** Two Italian hosts banter between tracks, roast each other, react to the music. The format absorbs imperfection — timing gaps and rough edges read as radio character.

**"How did it know?"** Connect Home Assistant and the hosts reference your actual home. *"The coffee machine says someone beat you to it."* Moments real radio structurally cannot do.

**The ads are the best part.** Fictional Italian brands with absurd claims, pharma disclaimers read at double speed, dedicated commercial voices. Entertaining enough that people actually listen.

**It remembers you.** Returning listeners get recognized. Inside jokes compound across sessions. Hosts build a persona around your listening patterns.

**Share the moment.** Hit the clip button to capture the last 30 seconds as a shareable MP3.

## Screenshots

<p align="center">
  <img src="docs/screenshots/listener.png" width="720" alt="Listener dashboard with now-playing, up-next queue, and radio dial animation">
</p>
<p align="center"><em>Listener dashboard &mdash; now playing, up-next queue, radio dial tuning animation on first load</em></p>

<p align="center">
  <img src="docs/screenshots/dashboard.png" width="720" alt="Admin control room with music, radio, and engine tabs">
</p>
<p align="center"><em>Admin control room &mdash; music queue, host config, pacing sliders, engine diagnostics</em></p>

## Three Tiers

The station plays immediately. Add keys to unlock more:

| Tier | What you get | What you need |
|------|-------------|---------------|
| **Demo Radio** | Music + stock banter + silence fallback for ads | Nothing. Works out of the box. |
| **Full AI Radio** | Claude-written hosts with distinct personalities, reactive banter, dynamic ads | `ANTHROPIC_API_KEY` (falls back to OpenAI) |
| **Connected Home** | Hosts reference live home state: lights, temperature, presence, appliances | Home Assistant + `HA_TOKEN` |

## Resources

- **[Architecture](ARCHITECTURE.md)** &mdash; the producer/playback pipeline, TTS, persona memory, HA context, route table
- **[HA Add-on Runbook](HA_ADDON_RUNBOOK.md)** &mdash; release process, config contract, pre-merge checklist
- **[Operations](OPERATIONS.md)** &mdash; deploy reality, resource needs, runtime assumptions
- **[Troubleshooting](TROUBLESHOOTING.md)** &mdash; silence, empty queue, missing banter, HA wiring
- **[Contributing](CONTRIBUTING.md)** &mdash; local setup, tests, coverage ratchet, smoke checks
- **[Design System](DESIGN.md)** &mdash; colors, typography, components, anti-patterns

## Configuration

Station behavior lives in [`radio.toml`](radio.toml). Secrets stay in `.env`.

| Section | Controls |
|---------|----------|
| `[station]` | Name, language, theme |
| `[playlist]` | Shuffle, repeat/artist cooldowns |
| `[pacing]` | Songs between banter, songs between ads, spots per break |
| `[[hosts]]` | Names, TTS engine (`edge`/`openai`), voices, personality axes |
| `[audio]` | Sample rate, channels, bitrate, Claude model |
| `[homeassistant]` | Context toggle, base URL, refresh interval |
| `[[ads.brands]]` | Fictional Italian brand pool, categories, campaign spines |
| `[[ads.voices]]` | Dedicated commercial voices for ads |

> **Brand safety:** all `[[ads.brands]]` entries must be fictional. The scriptwriter generates false product claims in the brand's voice &mdash; using real trademarks creates real exposure.

## Development

```bash
make test          # pytest with coverage
make check         # lint + typecheck + per-module coverage floor
make coverage-check  # verify no module regressed
```

Coverage can only go up &mdash; per-module floors in `.coverage-floors.json` auto-ratchet on merge to main.

## Never Crashes, Always Plays

The station degrades gracefully instead of failing:

| What's missing | What happens |
|----------------|-------------|
| `MAMMAMIRADIO_ALLOW_YTDLP` not set | Falls back to local `music/`, then silence |
| Anthropic API key | Falls back to OpenAI `gpt-4o-mini`, then stock copy |
| OpenAI API key | Falls back to Edge TTS voices |
| Home Assistant token | Continues without home context |
| Ad brands in config | Skips ads instead of crashing |

Local `music/` files are always preferred over network downloads.

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=florianhorner/mammamiradio&type=Date)](https://star-history.com/#florianhorner/mammamiradio&Date)
