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

## Turn off the add-on's media_player push (required)

The add-on has always pushed a `media_player.mammamiradio` "ghost" every few
seconds over the REST API. Once this integration owns that entity, that push
would fight it (the HA state machine is last-writer-wins) and flap the card. So:

**Add-on → Configuration → turn off `On-air media player push`
(`ha_media_player_push`).**

The add-on then stops pushing `media_player.mammamiradio` (and deletes the stale
ghost once so this integration claims the id cleanly), while the
`sensor.mammamiradio_*` / `binary_sensor.mammamiradio_on_air` entities keep
flowing as before.

> Migration note: if you have automations that read the old pushed
> `media_player.mammamiradio` state, they keep working — the registered entity
> reuses the same id.

## How it works

The integration polls the add-on's read contract
(`GET /api/integrations/v1/now-playing`) every 5 seconds and maps it to the
entity. Controls POST to `/api/resume`, `/api/stop`, `/api/skip` with the
`X-Radio-Admin-Token` header. See `docs/integrations/now-playing.md` for the
contract.

## Deferred to a later version

- A branded Lovelace card (`getEntitySuggestion`) — the built-in media-control
  card the picker already auto-suggests covers the common case.
- `media_source.py` (casting the stream to other HA speakers).
- A Music Assistant provider (a separate PR into `music-assistant/server`).
