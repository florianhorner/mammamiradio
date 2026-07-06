# mammamiradio integration contracts

Stable read-only APIs for third-party music controllers, Home Assistant
cards, and provider authors who want to render mammamiradio in their UI.

The contract is the door, not the URL.

## Available contracts

| Endpoint | Status | Doc |
| --- | --- | --- |
| `GET /api/integrations/v1/now-playing` | v1 | [now-playing.md](./now-playing.md) |

## Home Assistant

- [Home Assistant integration](./ha-integration.md) covers setup, station
  control, media-source casting, diagnostics, and recovery behavior.
- [HA privacy and upstream proposals](./ha-privacy-and-upstream-proposals.md)
  records shipped local privacy behavior and proposal-only upstream ideas.

## Five-line hello world

```bash
curl http://homeassistant.local:8000/api/integrations/v1/now-playing | jq
```

That returns the full v1 payload. Wire `now_playing.title`, `artist`, and
`artwork` into your UI; branch on `now_playing.segment_class` to render
voice/interstitial segments without faking music metadata.

## Versioning policy (written in stone)

- **v1 contract is additive-only.** New fields may appear within
  `schema_version: "1.*"`. Existing fields will never disappear, rename, or
  change type within v1.
- **Breaking changes ship at `/api/integrations/v2/`.** They are not allowed
  in v1.
- **Deprecation** is announced via the `Deprecation` and `Sunset` HTTP
  response headers per RFC 8594, with a minimum 6-month overlap window.
- **Sample payloads** under
  [`sample-payloads/`](./sample-payloads/) are the binding fixture. They are
  also wired into mammamiradio's own contract tests so they cannot drift.
  Third-party consumers can pull them straight from raw GitHub:
  `https://raw.githubusercontent.com/florianhorner/mammamiradio/main/docs/integrations/sample-payloads/<file>.json`.

## Auth model

- **v1:** unauthenticated. Same as `/public-status`. The endpoint returns
  the same data any listener can see via the public stream.
- **v1.1 (reserved):** an optional `X-Integration-Token` header may be
  added for rate-limit lifting and access to consumer-specific telemetry.
  Unauthenticated clients will continue to work.

## Migration

If you are an existing consumer polling `/public-status` for now-playing,
read [migration-from-public-status.md](./migration-from-public-status.md)
for the field-by-field mapping. `/public-status` will keep its legacy
top-level fields for the foreseeable future, but `upcoming` now lists only
render-ready queued audio. When it is empty with `upcoming_mode: "building"`,
inspect `session_stopped` and `golden_path.stage` to distinguish stopped,
no-source, and still-building states. `current_source`, when present, is the
loaded playlist source; the migration guide includes the copy-paste decision
table.

## Future endpoints

The `/api/integrations/v1/` prefix is reserved for further read-only
contracts. Push (SSE/WebSocket) and write endpoints have not been
specified.
