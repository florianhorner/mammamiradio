<p align="center">
  <a href="https://florianhorner.github.io/mammamiradio/">
    <img src="docs/banner.png" width="1280" alt="Mamma Mi Radio">
  </a>
</p>

<h1 align="center">Mamma Mi Radio</h1>
<p align="center"><em>A radio station that lives in your house and talks about it.</em></p>

## You built the sensors. You wrote the automations. Now somebody finally notices.

Marco and Giulia host a late-night Italian radio show on hardware you control.
They play music, argue like hosts who have known each other too long, and read
ads for forty companies that do not exist. If you invite the house in, its small
dramas become part of the show.

> *"Breaking news from the laundry room: it's done. It's been done for two
> hours. Nobody cares but us."*
>
## ▶ [Hear it happen: four half-minute moments](https://florianhorner.github.io/mammamiradio/)

**Sound on. No install, no account.** Each clip starts as ordinary radio, then
the house turns up as part of the show and the page reveals the invented Home
details behind the moment. The demo never touches your Home Assistant.

Another minute and a half: **[three short films from Studio B](https://florianhorner.github.io/mammamiradio/shorts/)**: Archive Receipt, Jealous
Microphone, Third Chair. Synthetic voices throughout.

<p align="center">
  <a href="https://github.com/florianhorner/mammamiradio/releases"><img alt="Release" src="https://img.shields.io/github/v/release/florianhorner/mammamiradio"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
  <img alt="Home Assistant OS app" src="https://img.shields.io/badge/Home%20Assistant-OS%20app-41BDF5?logo=homeassistant&logoColor=white">
  <a href="https://github.com/florianhorner/mammamiradio/commits/main"><img alt="Last commit" src="https://img.shields.io/github/last-commit/florianhorner/mammamiradio"></a>
</p>

I built this because a smart home is invisible to everyone who did not build
it. At dinner with seven guests, the hosts called me out for ignoring the pasta
timer. Someone stopped the conversation to ask what the radio had said. Nobody
questioned the station until the first fourth-wall break.

<p align="center">
  <img src="docs/screenshots/01-house-made-it-on-air.webp" width="960" alt="Marco and Giulia on air reacting to completed laundry, with a privacy-safe Casa receipt">
</p>
<p align="center"><em>The laundry room made it on air.</em></p>

## Why it feels like radio

Marco and Giulia have a written relationship, recurring Studio B lore, station
imaging, music, and forty fictional sponsors. Home details enter as editorial
material only when there is something worth airing. The show keeps going when
the house has nothing to say.

The four public demos are staged recordings made with invented data, so anyone
can play them without sharing a home. With the default Home-context setting, a
fresh installation starts off. After you inspect the filtered preview and opt
in, its current authorization is limited to coarse daylight and weather. The
arrival, coffee, and laundry moments show the wider Home Profile planned for a
later update; existing home-aware stations may already have broader context.

The station runs on your hardware, takes no commands, and controls nothing in
your home. There is no Mamma Mi Radio account, subscription, or project-operated
analytics upload. You add provider keys only when you want freshly written
dialogue or premium voices.

**Status:** stable, single maintainer, running daily in one household. [The full
honest assessment](docs/status-quo.md) separates engineering maturity from the
evidence that other people want this.

## First listen

Home Assistant Apps require **Home Assistant OS**, including Home Assistant
Green and Yellow. Home Assistant Container users can run the [Docker
alternative](#docker-alternative).

[![Add repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fflorianhorner%2Fmammamiradio)

You can also add the repository by hand. Open **Settings > Apps > App store >
three-dot menu > Repositories**, paste
`https://github.com/florianhorner/mammamiradio`, and select **Add**. Install and
start **Mamma Mi Radio**, then open its Web UI.

No AI key is required for your first listen. Fresh installs open **First Listen**
at the producer desk (`/admin`). Returning installs open the control room;
you can find First Listen under **Motore -> Setup**.

1. Select **Play my station**. A reviewed English opening plays
   `/stream?first_listen=1` on the current device for about 15 seconds, then
   hands off to the live stream.
   Use its speakers, headphones, Bluetooth, or AirPlay. You need no AI key or
   Home context. No HACS integration is required.
2. Select **I hear you** only after you hear Mamma Mi Radio. Select **No sound yet**
   for [repair steps](docs/troubleshooting.md#first-listen-does-not-play-on-this-device).
3. Select **Finish with Home private**, or choose **Add live host writing**.
   With default settings, Home context stays off unless you open **See what the
   hosts would receive** and then select **Let Marco and Giulia use these details**.
   You can choose **Keep Home private** from that review instead. If the preview
   contains only daylight and weather, the app labels it ambient-only and
   recommends the private path.
4. Select **Listen to the station** to enter the `/listen` station page, or
   **Open station controls** to return to `/admin`. The handoff preserves the
   audio already playing. Install the [HACS
   integration](docs/integrations/ha-integration.md#optional-play-it-on-a-home-assistant-speaker)
   later if you want native `media-source://mammamiradio/live` playback on Home
   Assistant speakers. The [Music Assistant
   provider](https://www.music-assistant.io/music-providers/mamma-mi-radio/) can
   also play it as one live station with current and up-next metadata.

<p align="center">
  <img src="docs/screenshots/02-first-listen-private.webp" width="960" alt="Completed First Listen screen showing audio heard on this device and Home staying private">
</p>
<p align="center"><em>First broadcast complete. Home stays private.</em></p>

### Docker alternative

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

## What each key adds

| You add | The station adds |
|---------|------------------|
| Nothing | Twelve packaged, credited tracks, station imaging, the 15-second First Listen opening, and 21 prerecorded Demo Radio breaks. Those assets make no provider call; other stock copy can use keyless online Edge TTS |
| `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` | Written-on-the-fly banter, news flashes, and ad breaks for forty fictional brands. Billed to you; the control room shows a running estimate |
| ElevenLabs voice credentials | The cast Marco and Giulia voices used in the public demos, with keyless online Edge TTS as the fallback |
| OpenAI or Azure Speech credentials | Alternative cloud voices where you explicitly select them |
| An AI key plus approved, filtered Home context | On fresh installs today, opted-in coarse daylight and weather. Broader household moments remain for existing home-aware stations and a later Home Profile update |

The same OpenAI API key can cover both writing and OpenAI TTS when you select
OpenAI for each. Provider usage is charged to the API account behind your key;
OpenAI API billing is separate from ChatGPT subscriptions. Azure Speech and
ElevenLabs change only the voices.

## Privacy

With the default Home-context setting, a fresh install starts off. The **Host
home context** choice is omitted and remains off until you hear the station,
inspect the filtered preview, and explicitly choose **Let Marco and Giulia use
these details**. An explicit `MAMMAMIRADIO_HA_CONTEXT_ENABLED=true` operator
setting overrides that guided default. The station does not poll Home state for
host material while context is off. You can keep Home private without fetching
a preview. If you want household details on air, mute any entity the hosts
should ignore. Previewing does not publish the snapshot into host scripts or
send it to an AI provider.

After opt-in, the active writing provider receives the authorized, filtered
prompt slice. If a Home-derived line uses online speech synthesis, that spoken
text also goes to the configured voice service or Edge. Existing legacy
installations may also use Anthropic to generate labels from sanitized entity
metadata; optional AI mood naming sends a bounded Home-state summary. Post-air
memory is written only after generated material airs and only while Home context
stays on. Without a script key, no Home context reaches an AI writing provider.

Turning Home context off stops Home-state and timer polling. It cancels
Home-derived generation and memory work, removes queued Home-derived breaks,
and clears public Casa moments. Audio on air may finish to avoid dead air, but
it cannot write Home memory afterward. Home Assistant entity publishing can
stay on while host context stays off.

The station itself runs on your hardware. Mamma Mi Radio has no account system,
central service, or project-operated analytics upload. Operational counters and
the optional provenance ledger remain local. Paid AI calls use credentials you
supply; Edge is keyless but still online. In the Home Assistant app, saved keys
live in `/config/secrets.env`; the UI never echoes them. Provider-side processing
follows each provider's terms.

## Music

Normal rotation starts with the offline, attributed twelve-track starter
collection, so no provider account or network music source is required. The
listener shows the source, license, and modification notice for each track.
The Home Assistant app scans audio under `/data/music`; use **Rotazione >
Local music > Scan now** to refresh without a restart. Standalone installs can
set `MAMMAMIRADIO_MUSIC_DIR`.

Jamendo is off by default. To enable it, acknowledge that your Jamendo API use
is non-commercial.
Provider confirmation for this station model remains pending. The station
prepares one track at a time and deletes it after play or cancellation. Jamendo
is not a recovery or restart source. Read [Music sources and rights
boundaries](docs/music-sources.md) before enabling it.

Packaged recovery clips keep the speaker path audible while the music source
stays marked unhealthy.

## Make it yours

`radio.toml` defines the hosts, voices, pacing, and ad brands. The `/admin`
control room lets you reorder the queue, ban a song on air, change AI quality,
and switch between Festival, Chaos, and Super Italian modes. The default show
is roughly 75% English and 25% Italian; Super Italian switches the hosts to 100%
Italian. See the full configuration in [`.env.example`](.env.example) and
[Operations](docs/operations.md).

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
[Pilot feedback](https://github.com/florianhorner/mammamiradio/discussions/831) |
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
