<p align="center">
  <img src="docs/banner.png" width="1280" alt="Mamma Mi Radio">
</p>

# Mamma Mi Radio

A song's playing. As it winds down, one of the hosts leans in: "The coffee machine just started, someone's home early, and it's 14 degrees in here. Classic Tuesday."

Mamma Mi Radio is an AI radio station running on Home Assistant. Live charts, gloriously fake ads, and two hosts who riff on what's actually happening at home: the weather turning, the hallway light coming on again, the front door opening at the exact wrong time.

You built the sensors. You wrote the automations. Now somebody finally notices.

Native to Home Assistant.

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

## Add it to your Home Assistant

[![Add repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fflorianhorner%2Fmammamiradio)

Or by hand: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, paste `https://github.com/florianhorner/mammamiradio`, then install **Mamma Mi Radio** and start. It already has your Home Assistant. Add an AI key and the hosts start riffing on your actual home.

<details>
<summary>Just want to hear it first, without Home Assistant?</summary>

```bash
git clone https://github.com/florianhorner/mammamiradio.git && cd mammamiradio
cp .env.example .env
docker compose up      # ADMIN_TOKEN auto-generates if unset
```

Open `http://localhost:8000`. Music plays in seconds, no keys needed. Add an AI key to wake the hosts. (Also: macOS one-click `./setup-mac.sh`, or `./start.sh` in a venv. Conductor users get `scripts/conductor-*.sh` lifecycle hooks for free.)

</details>

→ **[How it works](docs/architecture.md)** · **[Contribute](CONTRIBUTING.md)** · **[Changelog](CHANGELOG.md)**

<p align="center">
  <img src="https://img.shields.io/github/stars/florianhorner/mammamiradio?style=flat" alt="GitHub stars">
  <img src="https://img.shields.io/github/actions/workflow/status/florianhorner/mammamiradio/quality.yml?branch=main&label=CI&style=flat" alt="CI">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue?style=flat" alt="Python 3.11+">
</p>

---

> *We played this at a dinner party. Seven guests. Nobody questioned it was a real Italian radio station.*

---

## What you get

It plays the moment you start it, and climbs from there:

| Step | You bring | What your home does |
|------|-----------|---------------------|
| **Hear it first** | Nothing: the add-on, or `docker compose up` | Music in seconds; between songs, a placeholder voice. Proof it runs before you wire it in. |
| **Wake the hosts** | An `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY`) | The hosts come alive: reactive banter and the gloriously fake Italian ad breaks. |
| **Give your home a voice** | Home Assistant + `HA_TOKEN` | The hosts start noticing your house: lights, locks, who just got home. **The reason it exists.** |

The first step lets you hear it before you trust it with your house. The last step is the point.

It never goes silent: if a provider hiccups or the queue runs dry, it bridges to cached audio and keeps playing, so the illusion holds.

It runs on your hardware with your own AI keys: no account, no servers of ours, no telemetry. When home context is on, a filtered snapshot of your home goes to the AI you picked for host writing and for post-air memory extraction after generated banter streams cleanly, so it's a control promise, not a privacy one. Leave it off and the hosts never mention the house.

## Make it yours

`radio.toml` is the station's identity (hosts, voices, pacing, ad brands). The `/admin` control room tunes it live: pacing, drag-and-drop queue, ban-the-song-on-air, an AI-quality dial, and personality modes (Festival, Chaos, Super Italian). Full config lives in [`.env.example`](.env.example) and [docs/operations.md](docs/operations.md).

## Docs

[Architecture](docs/architecture.md) · [Troubleshooting](docs/troubleshooting.md) · [Operations & deploy](docs/operations.md) · [Repo map](docs/REPO_MAP.md)

## Contributing

Issues and PRs welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md). First-time contributors are protected by a merge-first protocol, so your PR lands before any refactor on top.

## License

The code is [Apache-2.0](LICENSE). That does not grant rights to the music the station plays or the AI-generated banter and ads. You are responsible for whatever your station plays and says.

[![Star History Chart](https://api.star-history.com/svg?repos=florianhorner/mammamiradio&type=Date)](https://star-history.com/#florianhorner/mammamiradio&Date)
