<p align="center">
  <img src="docs/banner.png" width="1280" alt="Mamma Mi Radio">
</p>

# Mamma Mi Radio

Italian radio for Home Assistant: music, stock host copy, and fallback voices
with no AI key required. Start by hearing it on one real speaker. When you add
an AI host key, review the filtered home-context preview first; mute individual
entities locally, or turn **Host home context** off to stop full-state polling.

## First listen: one real speaker

**Hear it before you choose what the hosts may use.** Install the Home
Assistant OS app below, then take the [optional HACS speaker
path](docs/integrations/ha-integration.md#play-it-on-one-speaker). It puts
**Mamma Mi Radio Live** on one physical speaker through
`media-source://mammamiradio/live`, not browser playback. HACS needs one Home
Assistant restart; the guide covers speaker recovery and the optional
media-player ownership choice. That choice is not needed for first speaker
playback.

### Home Assistant OS app

Home Assistant Apps require **Home Assistant OS** (including Home Assistant Green and Yellow). Home Assistant Container does not include Apps; if you do not have **Settings → Apps**, use the Docker alternative below.

[![Add repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fflorianhorner%2Fmammamiradio)

Or by hand: **Settings → Apps → App store → ⋮ → Repositories**, paste `https://github.com/florianhorner/mammamiradio`, and select **Add**. Open **Mamma Mi Radio**, select **Install**, then **Start**.

### What first audio needs

No AI key, provider account, or network music source is required for your first
listen. The app starts with an offline, attributed twelve-track Incompetech
starter collection; without an AI key, the hosts use stock copy and fallback
voices. Open the listener and use **Music credits** to see the exact source,
license, and modification notice for what is playing.

Jamendo is an optional, default-off transient expansion for acknowledged
non-commercial API use while provider confirmation is pending. Configure it
later in **Motore -> Setup -> Music sources**; starter music keeps playing while
it prepares one track at a time. See [Music sources and rights
boundaries](docs/music-sources.md).

First audio is separate from home context. **Host home context** is on by
default and, when the add-on has Home Assistant access, refreshes a filtered
state slice for host segments. Turn it off in the app configuration before you
start if you do not want full-state prompt-context polling. Without
script-provider credentials, that state is not sent to an AI provider.

### Check the app (operators)

`Producer started` in the app log and `"ready": true` from `/readyz` show that
the app is healthy. They are operator checks, not the first-listen proof: for
that, hear **Mamma Mi Radio Live** on the selected speaker.

### Docker alternative

<details>
<summary>Run it without Home Assistant Apps</summary>

```bash
git clone https://github.com/florianhorner/mammamiradio.git && cd mammamiradio
cp .env.example .env
docker compose up      # ADMIN_TOKEN auto-generates if unset
```

Open `http://localhost:8000`. The bundled starter collection works offline and
needs no AI or music-provider key; add an AI key when you want generated hosts.
The stock Docker quickstart keeps external extraction off. A standalone install
may deliberately add the `external-media` extra, but technical access is not a
rights claim. (Also: macOS one-click `./setup-mac.sh`, or `./start.sh` in a
venv. Conductor users get `scripts/conductor-*.sh` lifecycle hooks for free.)

</details>

→ **[How it works](docs/architecture.md)** · **[Contribute](CONTRIBUTING.md)** · **[Changelog](CHANGELOG.md)**

## When you want the house in the show

A song's playing. As it winds down, one of the hosts leans in: "The coffee
machine just started, someone's home early, and it's 14 degrees in here.
Classic Tuesday."

You built the sensors. You wrote the automations. Now somebody finally notices.

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
| **Hear it first** | Nothing | Demo Radio uses the attributed offline starter collection, stock host copy, and fallback voices. |
| **Wake the hosts** | An `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` | The hosts come alive: reactive banter and the gloriously fake Italian ad breaks. |
| **Give your home a voice** | AI host key plus prompt-safe Home Assistant context | The admin shows the filtered home context first. Mute any entity locally, then the hosts can notice your house: lights, locks, who just got home. |

"Demo Radio" is the no-AI-key host tier; music still begins with the bundled,
attributed starter collection. The first step lets you hear the station before
you trust it with your house. The last step is the point.

The starter collection is the dependable first-run rotation. Recovery clips
and eligible local cache can still bridge thin-queue moments, but Jamendo is
never a recovery or restart source.

It runs on your hardware with your own AI keys: no account, no servers of ours, no telemetry. In the add-on, saved keys live in `/config/secrets.env`; the UI never echoes them. When Host home context is on and an AI host key is ready, the admin preview shows the filtered context that may go to the AI you picked for host writing and for post-air memory extraction after generated banter streams cleanly. Mute any entity there to keep it out of future host/context use. Already-rendered audio is not purged. The Home Assistant integration and Host home context are separate: turn Host home context off to stop full-state prompt polling while keeping entity publishing and timer interrupts, or run without script-provider credentials so the hosts never send home context to an AI provider.

## Make it yours

`radio.toml` is the station's identity (hosts, voices, pacing, ad brands). The `/admin` control room tunes it live: pacing, drag-and-drop queue, ban-the-song-on-air, an AI-quality dial, and personality modes (Festival, Chaos, Super Italian). Full config lives in [`.env.example`](.env.example) and [docs/operations.md](docs/operations.md).

## Docs

[Product status](docs/status-quo.md) · [Architecture](docs/architecture.md) · [Music sources & rights boundaries](docs/music-sources.md) · [Troubleshooting](docs/troubleshooting.md) · [Operations & deploy](docs/operations.md) · [Repo map](docs/REPO_MAP.md)

## Contributing

Issues and PRs welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md). First-time contributors are protected by a merge-first protocol, so your PR lands before any refactor on top.

## License

The code is [Apache-2.0](LICENSE). Bundled audio keeps its own attributed CC BY
4.0 licenses; Jamendo facts are provider-reported; local and externally resolved
media remain the operator's responsibility. See the canonical [music-source
boundaries](docs/music-sources.md). You are also responsible for what your
station says.

[![Star History Chart](https://api.star-history.com/svg?repos=florianhorner/mammamiradio&type=Date)](https://star-history.com/#florianhorner/mammamiradio&Date)
