# First Listen local Home Assistant lab

Use this disposable macOS lab for First Listen and other Home Assistant-facing
development. It keeps branch code, test credentials, and synthetic devices away
from the live home.

```text
disposable HA Container
        ↓  172.17.0.1:{8000,4212} (Docker bridge only)
owned socat relays in the dedicated Colima VM
        ↓  VM loopback :{18000,14212}
owned, non-multiplexed SSH reverse tunnel
        ↓  Mac loopback :{8000,4212}
branch radio + VLC Telnet media_player
        ↓
human audible confirmation
```

Everything persistent is under the gitignored
`tmp/first-listen-ha-lab/`. The launcher uses the dedicated Colima profile
`mmr-first-listen` and Docker context `colima-mmr-first-listen`; it never changes
your global Docker context. `stop` preserves the HA database, integrations,
credentials, music, and archived radio runs.

## Prerequisites

- macOS
- the repo virtualenv (`.venv`)
- Colima and the Docker CLI
- VLC at `/Applications/VLC.app`
- FFmpeg, `curl`, and `lsof`
- macOS OpenSSH at `/usr/bin/ssh`

The VM-side relay uses `socat`, which Colima does not guarantee in every guest
image. On the first `start`, the launcher creates the dedicated profile and then
fails closed before starting HA, VLC, or the radio if `socat` is missing. Check
the isolated guest before installing anything:

```bash
colima ssh --profile mmr-first-listen -- cat /etc/os-release
```

For a Debian or Ubuntu guest only, install the one private relay dependency:

```bash
colima ssh --profile mmr-first-listen -- \
  sudo env DEBIAN_FRONTEND=noninteractive apt-get update
colima ssh --profile mmr-first-listen -- \
  sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y \
  --no-install-recommends socat
```

For another guest OS, install its packaged `socat` rather than copying a binary
or weakening the bridge. Then rerun `scripts/first-listen-lab.sh start`.

The lab deliberately starts the radio with no Anthropic, OpenAI, Azure, or
ElevenLabs key, no Jamendo client ID, and no yt-dlp. It copies the same packaged
27-second First Listen mini-show used by a fresh install into its isolated
local-music folder, replacing any older synthetic transport-test track. The
clip has station music, Mamma Mi Radio identity, and a privacy-aware opening by
Marco and Giulia in the stock Edge voices; its reviewed transcript and SHA-256 live in
`mammamiradio/assets/demo/spoken_assets.json`.

## One-time setup

Start the isolated services:

```bash
scripts/first-listen-lab.sh start
```

On a new lab, open `http://127.0.0.1:8123` and complete local Home Assistant
onboarding. In that local profile, create a long-lived access token, then store
it without putting it in shell history:

```bash
scripts/first-listen-lab.sh set-ha-token
scripts/first-listen-lab.sh start
```

Show the two local-only integration values:

```bash
scripts/first-listen-lab.sh show-setup
```

In the disposable Home Assistant:

1. Add **VLC media player via Telnet** with host `172.17.0.1`, port
   `4212`, and the displayed VLC password. Rename it **Mac Lab Speaker** if you
   want the same label used by the acceptance walkthrough.
2. Add **Mamma Mi Radio** with host `172.17.0.1`, port `8000`, and the
   displayed admin token.
3. Under **Settings → System → Network**, set the local Home Assistant URL to
   `http://127.0.0.1:8123`. That keeps the signed Media Source URL reachable by
   VLC on the Mac.

The launcher stages `custom_components/mammamiradio/` before it creates the HA
container. After changing integration code, stage the new branch copy with:

```bash
scripts/first-listen-lab.sh sync-integration
```

That command never restarts Home Assistant. The files load on the next lab
`stop`/`start` that you choose to run.

### Existing lab migration

Labs created by the earlier launcher used `host.lima.internal`, wildcard Mac
listeners, and a Home Assistant container that published port 8123 on every Mac
interface. The new launcher recognizes those exact owned processes and proves
the legacy container's image, no-restart policy, and `/config` mount before it
takes any lifecycle action. It never removes a running container.

When you choose to migrate, run `stop`, then `start`. The explicit `stop`
authorizes replacement of that exact verified container. On the following
`start`, the launcher removes only the stopped container and recreates it as
`127.0.0.1:8123:8123` with the same disposable `/config` bind mount, so the HA
database, onboarding, integrations, and credentials survive. Docker removal is
never forced; if another actor starts the container during migration, the
operation fails closed instead of stopping it.

In the **local** HA only, change both integration hosts from
`host.lima.internal` to `172.17.0.1`; use **Reconfigure** when offered,
otherwise remove and re-add that local config entry using `show-setup`.
**Do not make these changes in the live home.**

## Personal acceptance test

For a fast repeat that keeps the radio database and normalized audio cache:

```bash
scripts/first-listen-lab.sh replay
open http://127.0.0.1:8000/admin
```

Required First Listen proof is hearing the station on this device in `/admin`.
This lab remains the way to exercise the optional Home Assistant speaker path.

Then, in **First Listen** (the automatic fresh-install landing):

1. Confirm that the opening card leads with the authored 27-second mini-show:
   station music, the privacy-aware Marco/Giulia opening, then the live stream.
   It must say that no AI key or Home context is used. Expand **What feeds the
   station after the opening** only as supporting detail. Local music should be
   ready; charts and Jamendo are intentionally unavailable; recovery/demo audio
   must be described as a limitation rather than a music source.
2. Mute the machine before confirming, select **Play the station**, then **Not
   yet**, and check that the repair guidance names this device's volume and mute
   and offers to try again here.
3. Unmute, select **Try this device again**, and listen for the music bed,
   Mamma Mi Radio identity, and the Marco/Giulia exchange.
4. Only after hearing the opening, select **Yes, I hear it**.
5. Separately, exercise the optional speaker route outside First Listen: in
   Home Assistant, **Media → Mamma Mi Radio → Mamma Mi Radio Live** to **Mac Lab
   Speaker**, and confirm the room by ear.
6. Select **Keep private and continue** without opening the preview. Confirm
   First Listen says that no Home Assistant data was requested and that AI
   hosts remain optional.
7. Reset the radio-only flow, preview Home context, and verify that generic
   daylight is disclosed as ambient-only and not meaningful personalization,
   with **Keep private and continue** recommended. Do not enable it for the
   acceptance path.

First Listen proof here is browser playback in `/admin` plus human audible
confirmation. Step 5 additionally exercises `media_player.play_media`, the
custom Media Source, the signed HA proxy, and a real `media_player`, but that
route is optional and no longer gates setup. The mini-show is client-local:
it does not enter the shared queue, change live now-playing, or reach existing
and already-completed installs. Once it finishes, that client joins the normal
live stream.

## Reset levels

`replay` archives only
`radio-cache/state/first_listen_receipt_v1.json`, then restarts the radio if it
was running. It preserves the database, install-origin witness, HA, speaker,
music, and audio cache. Use it for rapid UI, playback, repair, and privacy-flow
rechecks.

For a genuine fresh-radio classification while keeping the disposable HA and
speaker configured:

```bash
scripts/first-listen-lab.sh fresh-radio
```

This stops only the owned branch radio, timestamp-archives the whole radio
cache/database, temp directory, and isolated runtime `.env`, recreates them
empty, and starts the radio again if it was running. It never copies the old
receipt or install-origin witness into the new run. Archives are preserved
under `tmp/first-listen-ha-lab/archives/`.

Useful lifecycle commands:

```bash
scripts/first-listen-lab.sh status  # redacted; never prints token values
scripts/first-listen-lab.sh stop    # preserves all lab data
scripts/first-listen-lab.sh start
```

The launcher refuses to adopt or kill a process that merely happens to own
Mac port 8000 or 4212, either guest-loopback tunnel port, or either VM gateway
port. It validates each PID against its exact command and working directory,
uses TERM with a bounded wait, and never uses a generic process kill. It also
refuses a container with the lab name unless its image, restart policy, and
`/config` mount prove that it belongs to this lab.

The radio and VLC bind only to Mac loopback. The launcher passes VLC 3 a full
`telnet://127.0.0.1` host URL (a bare address is parsed as a path and opens a
wildcard listener) and ignores personal VLC configuration. SSH binds its high
ports only to VM loopback. The socat listeners bind only to Docker gateway
`172.17.0.1` and accept only `172.17.0.0/16` clients. Startup verifies the exact
Docker bridge and proves the VLC password prompt plus radio `/healthz` from
inside the actual HA container before it calls the bridge ready.

## What this lab does not prove

HA Container proves the Core/custom-integration/media-source/speaker path. It
does not prove Supervisor APIs, ingress rewriting, add-on installation or
update packaging, add-on options mapping, or `ha-addon/mammamiradio/rootfs/run.sh`.
Those remain a separate disposable HAOS VM/add-on acceptance gate; they should
not be tested by connecting branch code to the live home.
