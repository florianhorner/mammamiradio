# Home Assistant integration (HACS)

`custom_components/mammamiradio/` is a HACS-installable Home Assistant
integration that turns the station into a first-class HA `media_player` entity:
live now-playing state plus the three transport controls the back end can
actually honor.

It complements the add-on. The add-on plays the audio and serves the
now-playing contract; this integration is the HA-native face of it.

This integration is optional. Install it when you want HA-native controls or
want to prove the first listen on a physical speaker through Media Source; the
add-on can still serve its browser player without it. HACS installation takes
effect only after a Home Assistant restart.

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

## Install the optional HACS integration

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

This is the optional first-listen proof: a real Home Assistant speaker playing
the station through Media Source, rather than a browser tab. Complete the
following in order after you have installed the integration and restarted Home
Assistant. The media-player ownership choice above does not change this route.

1. Go to **Developer tools → Actions** and choose **Play specified media**.
2. Under **Target**, select one physical speaker — not
   `media_player.mammamiradio`.
3. In the **Media** picker, choose **Mamma Mi Radio → Mamma Mi Radio Live**,
   then select **Perform action**.

**Success:** the selected speaker starts playing the station. This proves the
Home Assistant media-source route (`media-source://mammamiradio/live`), not
browser playback.

If **Mamma Mi Radio Live** is missing or the speaker stays silent, reload the
integration (**Settings → Devices & Services → Mamma Mi Radio → ⋮ → Reload**)
and try the action again. If it still does not play, follow the [Home Assistant
app recovery steps](../troubleshooting.md#home-assistant-app).

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
