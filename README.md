<p align="center">
  <img src="docs/banner.png" width="1280" alt="Mamma Mi Radio">
</p>

# Mamma Mi Radio

Italian radio for Home Assistant, with no AI key required. A fresh station
opens on one real speaker with an authored 27-second mini-show: station music
and a privacy-aware Marco/Giulia opening, followed by the live stream. Add more
music, Home context, and generated host conversations only when you want them.

## First listen: one real speaker

**Hear it before you choose what the hosts may use.** Install and start the Home
Assistant OS app below, then install the [HACS
integration](docs/integrations/ha-integration.md#install-the-hacs-integration-for-speaker-playback)
and restart Home Assistant once. Open the app's Web UI and follow the
[first-listen speaker path](docs/integrations/ha-integration.md#play-it-on-one-speaker)
in **First Listen**. A fresh unfinished install opens there automatically;
afterward the same tab remains available for replay and repair:

1. The opening card explains the authored 27-second mini-show: station music,
   Marco and Giulia's privacy-aware welcome, then the live stream. It needs no
   AI key and reads no Home context. Source readiness for live charts, Jamendo,
   local music, and recovery cover sits underneath as supporting detail about
   what can follow the opening.
2. Select **Find my speakers**, choose one physical Home Assistant speaker,
   then select **Start Mamma Mi Radio**. The request always uses
   `media-source://mammamiradio/live`; browser playback is not counted.
3. Home Assistant accepting the request is only delivery confirmation. Select
   **Yes — that’s Mamma Mi Radio** only after the opening reaches the room, or
   **Not yet** for
   [warm repair steps](docs/integrations/ha-integration.md#first-listen-repair).
4. Select **Keep private and continue** without reading Home state, or review
   the fresh, filtered preview before selecting **Let future hosts use this**.
   If the preview contains only generic daylight, the UI discloses it as
   ambient-only and not meaningful personalization, and recommends keeping it
   private. Add an AI key later if you want generated hosts; it is not part of
   first audio.

### Home Assistant OS app

Home Assistant Apps require **Home Assistant OS** (including Home Assistant Green and Yellow). Home Assistant Container does not include Apps; if you do not have **Settings → Apps**, use the Docker alternative below.

[![Add repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fflorianhorner%2Fmammamiradio)

Or by hand: **Settings → Apps → App store → ⋮ → Repositories**, paste `https://github.com/florianhorner/mammamiradio`, and select **Add**. Open **Mamma Mi Radio**, select **Install**, then **Start**.

### What first audio needs

No AI key is required for your first listen: without one, the hosts use stock
copy and fallback voices. The packaged 27-second First Listen mini-show makes
the station music, identity, and Marco/Giulia opening audible immediately, but
it is not a song library. A reachable music source is still required for a
normal rotation. If music is not ready yet, packaged recovery audio can keep
proving the speaker transport without
pretending that the source is healthy. The app
tries live charts by default, which need outbound network access; for a
predictable Home Assistant alternative, configure a Jamendo client ID in the
app's advanced options. Operator-supplied MP3s can also live in `/data/music`
in the add-on; standalone/Docker installs can point elsewhere with
`MAMMAMIRADIO_MUSIC_DIR`.

First audio is separate from home context. On every fresh install, the
**Host home context** choice is omitted and remains off until you hear the
speaker, request a fresh filtered preview, and explicitly choose **Let future
hosts use this**.
Previewing does not publish the snapshot into host scripts or send it to an AI
provider. If that preview contains only generic daylight, it is shown for
transparency but classified as ambient-only—not meaningful personalization—and
**Keep private and continue** is recommended. That private choice preserves
Home Assistant entity publishing while suspending Home-state polling, timer
reads/interrupts, and Home-derived host work.

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

Open `http://localhost:8000`. No AI key is required; add one when you want generated hosts. The stock Docker quickstart uses live charts for music and needs outbound access. Its persistent music path is `/data/music`; set `MAMMAMIRADIO_MUSIC_DIR` when running outside the supplied container layout. (Also: macOS one-click `./setup-mac.sh`, or `./start.sh` in a venv. Conductor users get `scripts/conductor-*.sh` lifecycle hooks for free.)

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
| **Hear it first** | No AI key; live charts, Jamendo, or local MP3s for normal rotation | Demo Radio uses stock host copy and fallback voices. Recovery cover can prove the speaker path while a music source is repaired. |
| **Wake the hosts** | An `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` | The hosts come alive: reactive banter and the gloriously fake Italian ad breaks. |
| **Give your home a voice** | AI host key plus prompt-safe Home Assistant context | The admin shows the filtered home context first. Mute any entity locally, then the hosts can notice your house: lights, locks, who just got home. |

"Demo Radio" is the no-AI-key tier, not a bundled song library. The first step lets you hear the station before you trust it with your house. The last step is the point.

Once the station has playable audio, recovery clips and cached tracks bridge many provider hiccups and thin-queue moments. The bundled recovery clip is cover audio, not a full music rotation.

It runs on your hardware with your own AI keys: no account, no servers of ours, no telemetry. In the add-on, saved keys live in `/config/secrets.env`; the UI never echoes them. When Host home context is on and an AI host key is ready, the admin preview shows the filtered context that may go to the AI you picked for host writing and for post-air memory extraction after generated banter streams cleanly. Mute any entity there to keep it out of future host/context use. Turning Host home context off cancels Home-derived generation and post-air extraction, removes unstarted Home-derived breaks, clears public Home moments, and stops Home/timer polling. Audio already on air may finish so privacy revocation does not create dead air, but it cannot publish post-air Home memory afterward. The Home Assistant integration remains separate, so entity publishing can stay on while Host home context is off; running without script-provider credentials also prevents host context from reaching an AI provider.

## Make it yours

`radio.toml` is the station's identity (hosts, voices, pacing, ad brands). The `/admin` control room tunes it live: pacing, drag-and-drop queue, ban-the-song-on-air, an AI-quality dial, and personality modes (Festival, Chaos, Super Italian). Full config lives in [`.env.example`](.env.example) and [docs/operations.md](docs/operations.md).

## Docs

[Product status](docs/status-quo.md) · [Architecture](docs/architecture.md) · [Troubleshooting](docs/troubleshooting.md) · [Operations & deploy](docs/operations.md) · [Repo map](docs/REPO_MAP.md)

## Contributing

Issues and PRs welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md). First-time contributors are protected by a merge-first protocol, so your PR lands before any refactor on top.

## License

The code is [Apache-2.0](LICENSE). That does not grant rights to the music the station plays or the AI-generated banter and ads. You are responsible for whatever your station plays and says.

[![Star History Chart](https://api.star-history.com/svg?repos=florianhorner/mammamiradio&type=Date)](https://star-history.com/#florianhorner/mammamiradio&Date)
