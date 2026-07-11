<p align="center">
  <img src="docs/banner.png" width="1280" alt="Mamma Mi Radio">
</p>

# Mamma Mi Radio

A song's playing. As it winds down, one of the hosts leans in: "The coffee machine just started, someone's home early, and it's 14 degrees in here. Classic Tuesday."

Mamma Mi Radio is an AI radio station running on Home Assistant. Live charts, gloriously fake ads, and two hosts who riff on what's actually happening at home: the weather turning, the hallway light coming on again, the front door opening at the exact wrong time.

You built the sensors. You wrote the automations. Now somebody finally notices.

Native to Home Assistant.

## Start here

### Home Assistant OS app

Home Assistant Apps require **Home Assistant OS** (including Home Assistant Green and Yellow). Home Assistant Container does not include Apps; if you do not have **Settings → Apps**, use the Docker alternative below.

[![Add repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fflorianhorner%2Fmammamiradio)

Or by hand: **Settings → Apps → App store → ⋮ → Repositories**, paste `https://github.com/florianhorner/mammamiradio`, and select **Add**. Open **Mamma Mi Radio**, select **Install**, then **Start**.

No AI key is required for the first run: without one, the hosts use stock copy and fallback voices. Music is a separate requirement. The app tries live charts by default, but that needs outbound network access; for a predictable Home Assistant alternative, configure a Jamendo client ID in the app's advanced options. A successful start shows `Producer started` in the log and returns `"ready": true` from `/readyz`.

### Docker alternative

<details>
<summary>Run it without Home Assistant Apps</summary>

```bash
git clone https://github.com/florianhorner/mammamiradio.git && cd mammamiradio
cp .env.example .env
docker compose up      # ADMIN_TOKEN auto-generates if unset
```

Open `http://localhost:8000`. No AI key is required; add one when you want generated hosts. The stock Docker quickstart uses live charts for music and needs outbound access; it does not currently wire a local-music mount or Jamendo option. (Also: macOS one-click `./setup-mac.sh`, or `./start.sh` in a venv. Conductor users get `scripts/conductor-*.sh` lifecycle hooks for free.)

</details>

→ **[How it works](docs/architecture.md)** · **[Contribute](CONTRIBUTING.md)** · **[Changelog](CHANGELOG.md)**

## See it

<p align="center">
  <img src="docs/screenshots/listener.png" width="480" alt="Listener page: la radio che ascolta la tua casa">
</p>
<p align="center"><em>The listener page: la radio che ascolta la tua casa.</em></p>

<p align="center">
  <img src="docs/screenshots/admin.png" width="720" alt="The control room">
</p>
<p align="center"><em>The control room: live now-playing, the queue, and one-tap banter / ad / news.</em></p>

> *"Breaking news from the laundry room: it's done. It's been done for two hours. Nobody cares but us."*

<p align="center">
  <img src="https://img.shields.io/github/stars/florianhorner/mammamiradio?style=flat" alt="GitHub stars">
  <img src="https://img.shields.io/github/actions/workflow/status/florianhorner/mammamiradio/quality.yml?branch=main&label=CI&style=flat" alt="CI">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue?style=flat" alt="Python 3.11+">
</p>

---

> *We played this at a dinner party. Seven guests. Nobody questioned it was a real Italian radio station.*

---

## What you get

It starts in layers, and climbs from there:

| Step | You bring | What your home does |
|------|-----------|---------------------|
| **Hear it first** | No AI key; reachable live charts, or Jamendo in the Home Assistant app | Demo Radio uses stock host copy and fallback voices over that music source. |
| **Wake the hosts** | An `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` | The hosts come alive: reactive banter and the gloriously fake Italian ad breaks. |
| **Give your home a voice** | AI host key plus prompt-safe Home Assistant context | The admin shows the filtered home context first. Mute any entity locally, then the hosts can notice your house: lights, locks, who just got home. |

"Demo Radio" is the no-AI-key tier, not a bundled song library. The first step lets you hear the station before you trust it with your house. The last step is the point.

Once the station has playable audio, recovery clips and cached tracks bridge many provider hiccups and thin-queue moments. The bundled recovery clip is cover audio, not a full music rotation.

It runs on your hardware with your own AI keys: no account, no servers of ours, no telemetry. In the add-on, saved keys live in `/config/secrets.env`; the UI never echoes them. When Host home context is on and an AI host key is ready, the admin preview shows the filtered context that may go to the AI you picked for host writing and for post-air memory extraction after generated banter streams cleanly. Mute any entity there to keep it out of future host/context use. Already-rendered audio is not purged. The Home Assistant integration and Host home context are separate: turn Host home context off to stop full-state prompt polling while keeping entity publishing and timer interrupts, or run without script-provider credentials so the hosts never send home context to an AI provider.

## Make it yours

`radio.toml` is the station's identity (hosts, voices, pacing, ad brands). The `/admin` control room tunes it live: pacing, drag-and-drop queue, ban-the-song-on-air, an AI-quality dial, and personality modes (Festival, Chaos, Super Italian). Full config lives in [`.env.example`](.env.example) and [docs/operations.md](docs/operations.md).

## Docs

[Architecture](docs/architecture.md) · [Troubleshooting](docs/troubleshooting.md) · [Operations & deploy](docs/operations.md) · [Repo map](docs/REPO_MAP.md)

## Contributing

Issues and PRs welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md). First-time contributors are protected by a merge-first protocol, so your PR lands before any refactor on top.

## License

The code is [Apache-2.0](LICENSE). That does not grant rights to the music the station plays or the AI-generated banter and ads. You are responsible for whatever your station plays and says.

[![Star History Chart](https://api.star-history.com/svg?repos=florianhorner/mammamiradio&type=Date)](https://star-history.com/#florianhorner/mammamiradio&Date)
