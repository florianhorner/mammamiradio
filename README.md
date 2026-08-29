<p align="center">
  <a href="https://florianhorner.github.io/mammamiradio/">
    <img src="docs/banner.png" width="1280" alt="Mamma Mi Radio">
  </a>
</p>

# Mamma Mi Radio

## You built the sensors. You wrote the automations. Now somebody finally notices.

Marco and Giulia are two Italian hosts for your smart home. Their show plays
through Home Assistant, [Music Assistant 2.10](https://www.music-assistant.io/music-providers/mamma-mi-radio/),
or your browser. They bicker between songs and advertise companies that do not
exist. If you let them, they notice what is happening at home.

> *"Breaking news from the laundry room: it's done. It's been done for two
> hours. Nobody cares but us."*

**[Hear one moment in your browser](https://florianhorner.github.io/mammamiradio/).**
You do not need to install anything. The demo uses invented home data and does
not connect to your Home Assistant.

**[Watch Studio B Transmissions](https://florianhorner.github.io/mammamiradio/shorts/).**
Three short films: Archive Receipt, Jealous Microphone, Third Chair. Contains
synthetic voices.

Fresh installs keep Home context off. Hear the station, inspect the filtered
preview, then choose what Marco and Giulia may use.

## See it

<p align="center">
  <img src="docs/screenshots/01-house-made-it-on-air.webp" width="960" alt="Marco and Giulia on air reacting to completed laundry, with a privacy-safe Casa receipt">
</p>
<p align="center"><em>The laundry room made it on air.</em></p>

> *"We played this at a dinner party. Seven guests. Nobody questioned it was a
> real Italian radio station."*

## What you can add

| You add | The station adds |
|---------|------------------|
| Nothing | A 27-second First Listen, twelve offline tracks with credits, and 21 reviewed host breaks selected for your language mode |
| `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` | New host conversations and fake ad breaks |
| OpenAI, Azure Speech, or ElevenLabs TTS credentials | Premium voices, with Edge voices as the fallback |
| An AI host key plus approved, prompt-safe Home context | References to the details you allowed, such as an arrival or forgotten laundry |

OpenAI can handle both writing and voice. Azure Speech and ElevenLabs change
the voices only.

## First listen

Home Assistant Apps require **Home Assistant OS**, including Home Assistant
Green and Yellow. Home Assistant Container users can run the Docker setup
below.

[![Add repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fflorianhorner%2Fmammamiradio)

You can also add the repository by hand. Open **Settings > Apps > App store >
three-dot menu > Repositories**, paste
`https://github.com/florianhorner/mammamiradio`, and select **Add**. Install and
start **Mamma Mi Radio**, then open its Web UI.

Fresh installs open **First Listen**. Returning installs open the control room;
you can find First Listen under **Motore -> Setup**.

1. Select **Start sound check**. A 27-second opening plays on the current
   device: Marco and Giulia over an original music bed, then the live stream.
   Use its speakers, headphones, Bluetooth, or AirPlay. You need no AI key,
   Home context, or HACS integration.
2. Select **Yes, I hear it** after you hear the opening. Select **Not yet**
   for [repair steps](docs/troubleshooting.md#first-listen-does-not-play-on-this-device).
3. Select **Keep Home private**, or open **See what the hosts would receive**
   before choosing **Let Marco and Giulia use these details**. If the preview
   contains only daylight, the app labels it ambient-only and recommends
   **Keep Home private**.
4. Open the full listener. Install the [HACS
   integration](docs/integrations/ha-integration.md#optional-play-it-on-a-home-assistant-speaker)
   later if you want native `media-source://mammamiradio/live` playback on Home
   Assistant speakers.

<p align="center">
  <img src="docs/screenshots/02-first-listen-private.webp" width="960" alt="Completed First Listen screen showing audio heard on this device and Home staying private">
</p>
<p align="center"><em>First broadcast complete. Home stays private.</em></p>

### Docker

<details>
<summary>Run without Home Assistant OS</summary>

```bash
git clone https://github.com/florianhorner/mammamiradio.git && cd mammamiradio
cp .env.example .env
docker compose up      # ADMIN_TOKEN auto-generates if unset
```

Open `http://localhost:8000`. The starter music works offline. Add an AI key
when you want generated host conversations.

The stock Docker setup leaves external extraction off. Standalone installs can
add the `external-media` extra, but technical access does not grant media
rights. The Home Assistant app and supplied Docker container use `/data/music`;
source checkouts use `./music`. Set `MAMMAMIRADIO_MUSIC_DIR` to override either.
macOS users can run `./setup-mac.sh`; venv installs can run `./start.sh`.

</details>

## Music

The starter collection contains twelve attributed tracks and needs no account
or network music provider. The listener shows the source, license, and
modification notice for each track. The Home Assistant app scans audio under
`/data/music`; use **Rotazione > Local music > Scan now** to refresh without a
restart. Standalone installs can set `MAMMAMIRADIO_MUSIC_DIR`.

Jamendo is off by default. To enable it, acknowledge non-commercial use.
Provider confirmation for this station model remains pending. The station
prepares one track at a time and deletes it after play or cancellation. Jamendo
is not a recovery or restart source. Read [Music sources and rights
boundaries](docs/music-sources.md) before enabling it.

Packaged recovery clips keep the speaker path audible while the music source
stays marked unhealthy.

## Privacy

On a fresh install, Home context is off. The station does not poll Home state
for host material. You can keep Home private without fetching a preview. If you
want household details on air, inspect the filtered preview first and mute any
entity the hosts should ignore. Previewing does not add those details to a host
script or send them to an AI provider.

With Home context on and an AI host key set, the approved, filtered details may
go to that provider to write the show and extract post-air memory. Without a
script key, no Home context reaches an AI writing provider. Generated writing
and premium voices use online services; Edge TTS is online too.

Turning Home context off stops Home-state and timer polling. It cancels
Home-derived generation and memory work, removes queued Home-derived breaks,
and clears public Casa moments. Audio on air may finish to avoid dead air, but
it cannot write Home memory afterward. Home Assistant entity publishing can
stay on while host context stays off.

The station itself runs on your hardware. Mamma Mi Radio has no account system,
central service, or telemetry. Provider calls use your own keys. In the Home
Assistant app, saved keys live in `/config/secrets.env`; the UI never echoes
them.

## Make it yours

`radio.toml` defines the hosts, voices, pacing, and ad brands. The `/admin`
control room lets you reorder the queue, ban a song on air, change AI quality,
and switch between Festival, Chaos, and Super Italian modes. See the full
configuration in [`.env.example`](.env.example) and [Operations](docs/operations.md).

<p align="center">
  <img src="docs/screenshots/03-producer-desk.webp" width="960" alt="Producer desk with Marco and Giulia live, quick actions, and a short broadcast queue">
</p>
<p align="center"><em>The control room is there when you want it.</em></p>

## Operator checks

<details>
<summary>Health, readiness, and repeatable QA</summary>

`Producer started` means the engine started. `/readyz` stays at `503 starting`
until a listener accepts audio, returns `200` with `"ready": true` after that,
and reports `503 stopped` during an intentional stop. Queue depth and elapsed
time do not prove that anyone heard the station.

First Listen remains the human check: hear **Mamma Mi Radio** on the current
device. For branch development, use the [disposable Home Assistant
lab](docs/runbooks/first-listen-local-ha.md) to keep test state away from your
live home.

</details>

## Docs

[Interactive demo](https://florianhorner.github.io/mammamiradio/) |
[Studio B](https://florianhorner.github.io/mammamiradio/shorts/) |
[Product status](docs/status-quo.md) |
[Architecture](docs/architecture.md) |
[Music sources and rights](docs/music-sources.md) |
[Troubleshooting](docs/troubleshooting.md) |
[Operations](docs/operations.md) |
[Repo map](docs/REPO_MAP.md)

## Contributing

Issues and pull requests are welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md). The merge-first protocol protects a new
contributor's patch from being refactored out from under them before it lands.

## License

The code is [Apache-2.0](LICENSE). Each bundled asset keeps its own license and
attribution. The [imaging attribution
file](mammamiradio/assets/imaging/ATTRIBUTION.md) covers station imaging.
Starter music uses CC BY sources from Incompetech (4.0) and Jamendo (3.0);
Jamendo facts are provider-reported. You remain responsible for the media your
station plays and the words it puts on air. See [Music sources and rights
boundaries](docs/music-sources.md).
