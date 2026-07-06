# Home Assistant integration (HACS)

`custom_components/mammamiradio/` is a HACS-installable Home Assistant
integration that turns the station into a first-class HA `media_player` entity:
live now-playing state plus the three transport controls the back end can
actually honor.

It complements the add-on. The add-on plays the audio and serves the
now-playing contract; this integration is the HA-native face of it.

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

## Install (HACS custom repository)

1. HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/florianhorner/mammamiradio`, category **Integration**.
3. Install **Mamma Mi Radio**, then restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → Mamma Mi Radio.**
   - **Host:** keep the default `local-mammamiradio` on Home Assistant OS; on a
     plain Docker install use the add-on's container name (for example
     `mammamiradio`).
   - **Port:** `8000`.
   - **Admin token (optional):** only needed for the play/stop/next controls.
     Use the same value as the add-on's `admin_token` option. Leave blank for
     now-playing display only.

## Turn off the add-on's media_player push (when using this integration)

The add-on pushes a `media_player.mammamiradio` "ghost" every few seconds over
the REST API — **on by default**, so an add-on-only setup gets a basic
media-player tile automatically. Once this integration owns that entity, the push
fights it (the HA state machine is last-writer-wins) and flaps the card. So when
you install this integration:

**Add-on → Configuration → turn off `On-air media player push`
(`ha_media_player_push`).**

The add-on then stops pushing `media_player.mammamiradio` (and deletes the stale
ghost once so this integration claims the id cleanly), while the
`sensor.mammamiradio_*` / `binary_sensor.mammamiradio_on_air` entities keep
flowing as before. If Home Assistant shows a Repair about a legacy media-player
conflict, reload the integration (Settings → Devices & Services → Mamma Mi Radio
→ ⋮ → **Reload**) to clear the notice.

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
