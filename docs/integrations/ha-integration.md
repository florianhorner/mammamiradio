# Home Assistant integration (HACS)

`custom_components/mammamiradio/` is a HACS-installable Home Assistant
integration that turns the station into a first-class HA `media_player` entity:
live now-playing state plus the three transport controls the back end can
actually honor.

It complements the add-on. The add-on plays the audio and serves the
now-playing contract; this integration is the HA-native face of it.

This integration is optional for browser-only listening, but required for the
First Listen speaker path. Install it to register the Media Source that Setup
dispatches to a physical speaker. The add-on can still serve its browser player
without it, but browser audio is not accepted as First Listen proof. HACS
installation takes effect only after a Home Assistant restart.

## What you get

- `media_player.mammamiradio` — a registered entity (not the legacy pushed
  ghost), so it appears first-class in the HA dashboard card picker with no
  YAML, and automations / voice can target it.
- Live state: `playing` while on air, `idle` when stopped, `buffering` while
  the queue fills. Title, artist, and artwork (station logo when a voice or ad
  segment has no cover).
- Controls: **play** → resume, **stop** → stop, **next** → skip the current
  segment. On Home Assistant OS these work automatically (the add-on trusts the
  Supervisor network); a remote or Docker install needs the admin token. Next is
  shown only while on air. A control that can't reach the station surfaces a
  clear error instead of doing nothing.
- **Media Source:** `media-source://mammamiradio/live` resolves to a signed
  Home Assistant stream proxy (`/api/mammamiradio/stream`), so Home Assistant
  automations, Music Assistant, and Follow Me Music-style speaker handoffs can
  play the station on real media players — the speaker only needs to reach Home
  Assistant, not the add-on directly — while `media_player.mammamiradio` remains
  the station control surface.
- Repairs and diagnostics for the common recovery paths: unreachable station,
  rejected admin token, and old REST-pushed media-player conflicts. The Repairs
  clear themselves once resolved and are removed if you delete the integration;
  the unreachable notice waits for a sustained outage, not a brief blip.
- One station per install (single config entry). To change the host, port, or
  admin token later, use **Reconfigure** (Settings → Devices & Services →
  Mamma Mi Radio → ⋮ → **Reconfigure**) — no need to delete and re-add the
  entity. A failed change keeps what you typed instead of reverting.

The add-on's `station_name` option changes the entity's friendly name, media
titles, listener UI, stream metadata, and default generated imaging copy. The
integration domain, entity ID, and media-source ID stay stable:
`mammamiradio`, `media_player.mammamiradio`, and
`media-source://mammamiradio/live`.

## Install the HACS integration for speaker playback

If HACS itself is not installed and configured yet, complete the [official HACS
installation](https://www.hacs.xyz/docs/use/download/download/) first. Then add
Mamma Mi Radio as a custom Integration repository:

1. HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/florianhorner/mammamiradio`, category **Integration**.
3. Install **Mamma Mi Radio**, then restart Home Assistant before adding or
   using the integration.
4. **Settings → Devices & Services → Add Integration → Mamma Mi Radio.**
   - **Host:** keep the default `local-mammamiradio` on Home Assistant OS; on a
     plain Docker install use the add-on's container name (for example
     `mammamiradio`).
   - **Port:** `8000`.
   - **Admin token (optional):** only needed for the play/stop/next controls.
     Use the same value as the add-on's `admin_token` option. Leave blank for
     now-playing display only.

## Let the HACS integration own the media player (optional)

Before the speaker test, decide which component should own
`media_player.mammamiradio`. The add-on pushes a basic "ghost" card after
segment changes and on its heartbeat — **on by default**. If you want the HACS
integration's registered entity to own that card, change the setting now. It
prevents two sources from competing to update the same player card; it is not
needed for Media Source speaker playback.

**Add-on → Configuration → turn off `On-air media player push`
(`ha_media_player_push`).**

The add-on then stops pushing `media_player.mammamiradio` (and deletes the stale
ghost once so this integration claims the id cleanly), while the
`sensor.mammamiradio_*` / `binary_sensor.mammamiradio_on_air` entities keep
flowing as before. If Home Assistant shows a Repair about a legacy media-player
conflict, reload the integration (Settings → Devices & Services → Mamma Mi Radio
→ ⋮ → **Reload**) to clear the notice. If you leave the push on, Media Source
speaker playback still works and the add-on keeps its basic media-player tile.

## Play it on one speaker

This is the First Listen proof: a real Home Assistant speaker playing the
station through Media Source, rather than a browser tab. Complete the following
in order after you have installed the integration and restarted Home Assistant.
The media-player ownership choice above does not change this route.

1. Open the Mamma Mi Radio add-on Web UI. A fresh unfinished install opens
   **First Listen** automatically; completed or existing installs can select
   the same tab explicitly.
2. The opening card puts a reviewed 27-second Mamma Mi Radio mini-show on deck:
   an original music bed plus a privacy-aware Marco/Giulia welcome, then a
   handoff to the live stream. It uses neither an AI key nor Home context.
   Source readiness for live charts, Jamendo, local music, bundled demo music,
   and recovery cover says whether primary music, recovery cover, or a music
   repair follows the opening; it never blocks the speaker controls. Bundled
   demo music is reported as unavailable when this build has no song library.
3. Select **Find my speakers**, choose one physical speaker — not
   `media_player.mammamiradio` — then select **Start Mamma Mi Radio**. The fixed
   source is `media-source://mammamiradio/live`.
4. Wait for the room. The UI saying Home Assistant accepted the request proves
   only that the service call was accepted; it does not claim the speaker was
   audible. Select **Yes — that’s Mamma Mi Radio** only after you hear the
   opening. Select **Not yet** for repair guidance.

**Success:** the selected speaker starts playing the station. This proves the
Home Assistant media-source route (`media-source://mammamiradio/live`), not
browser playback.

After confirming audio, First Listen unlocks the privacy decision. Select
**Keep private and continue** without reading Home state, or select **Show
filtered preview** before **Let future hosts use this**. The preview is a fresh,
detached read: it does not make the result available to host scripts or send it
to an AI provider. If Home Assistant offers only generic daylight, First Listen
discloses it as ambient-only and not meaningful personalization, and recommends
the private path. AI-host keys remain optional and come afterward.

For branch development, use the
[disposable local Home Assistant lab](../runbooks/first-listen-local-ha.md)
instead of a live household. It keeps a reusable local HA install and real Mac
VLC speaker while allowing the radio's first-run state to be reset independently.
HA Container covers Core, the custom integration, Media Source, and audible
speaker playback; Supervisor, ingress, and add-on packaging require a later
disposable HAOS/add-on test.

<a id="first-listen-repair"></a>

### First-listen repair

If Home Assistant accepted the show but First Listen says the listening check
was not saved, select **Save this listening check**. That action only retries
the local receipt write; it does not discover speakers, resume the station, or
send another playback request. A refresh in the same app process restores that
recovery choice. If the app restarted and the unsaved proof is gone, First
Listen says so and asks you to explicitly start the selected speaker once more.

If the privacy choice takes effect but its setup review is not saved, the live
choice remains in force. For the private choice, select **Save private review
again**; this does not fetch Home state. For an enabled choice, make the required
fresh filtered preview, then select **Save review again**. Optional AI setup
stays locked until that local review receipt is saved.

If Home Assistant accepted playback but the room is quiet:

1. Give the speaker a few seconds, then check its mute and volume in Home
   Assistant. Mamma Mi Radio does not change either setting.
2. Confirm that the selected entity is the physical speaker you intended.
3. In **Developer tools → Actions**, choose **Play specified media**, target that
   speaker, and use `media-source://mammamiradio/live` with content type
   `music`. You can also browse **Media → Mamma Mi Radio → Mamma Mi Radio
   Live**.
4. If **Live** is missing, reload the integration (**Settings → Devices &
   Services → Mamma Mi Radio → ⋮ → Reload**). If it remains missing after
   the required Home Assistant restart, recheck the HACS install and add-on
   connection.

Then return to **First Listen** and use **Retry on same speaker**, or select
**Choose another speaker** before starting again. For wider add-on connectivity
problems, follow the [Home Assistant app recovery
steps](../troubleshooting.md#home-assistant-app).

> Migration note: if you have automations that read the old pushed
> `media_player.mammamiradio` state, they keep working — the registered entity
> reuses the same id.

## How it works

The integration polls the add-on's read contract
(`GET /api/integrations/v1/now-playing`) every 5 seconds and maps it to the
entity. Controls POST to `/api/resume`, `/api/stop`, `/api/skip` with the
`X-Radio-Admin-Token` header. The media-source entry resolves to a signed
Home Assistant stream proxy, so speaker devices receive a HA-reachable URL while
the integration still pulls audio from the configured host/port plus `/stream`.
Use `media-source://mammamiradio/live` as a `media_content_id` for
`media_player.play_media`. See `docs/integrations/now-playing.md` for the
contract.

Example `media_player.play_media` usage:

```yaml
service: media_player.play_media
target:
  entity_id: media_player.your_speaker
data:
  media_content_id: media-source://mammamiradio/live
  media_content_type: music
```

**Long speaker handoffs:** the proxy URL Home Assistant hands a speaker is
signed and valid for 24 hours. A speaker streaming continuously keeps playing
past that, but if it drops and reconnects more than a day later (or after Home
Assistant restarts), that one speaker can go quiet. Start it again from the
media browser or your automation and it picks up a fresh URL. The web player and
the `media_player.mammamiradio` card are never affected.

## Deferred to a later version

- A branded Lovelace card (`getEntitySuggestion`) — the built-in media-control
  card the picker already auto-suggests covers the common case.
- A Music Assistant provider (a separate PR into `music-assistant/server`).
