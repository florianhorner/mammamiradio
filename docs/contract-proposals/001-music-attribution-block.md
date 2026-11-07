# 001: additive music_attribution block on v1 now-playing

Status: implemented in v3.0.0; retained as the additive-field record.

## Field

`now_playing.music_attribution` — optional object, emitted only for
`segment_class == "music"` segments whose metadata carries an attribution
that passes validation; omitted entirely otherwise (absent key, not `null`).
Shape (`MusicAttributionBlock` in `mammamiradio/integrations/schema.py`):

- `provider: str` (≤ 64 chars)
- `license_id: str` (≤ 64 chars)
- `license_url: str` (https URL)
- `source_url: str` (https URL)
- `credit: str` (≤ 512 chars)
- `modified: bool`
- `basis: "bundled_manifest" | "provider_reported"`

Implementation:

- `mammamiradio/integrations/schema.py`: `MusicAttributionBlock` TypedDict plus
  the optional `music_attribution` field on `NowPlayingBlock` (`total=False`).
- `mammamiradio/integrations/serializer.py`: `"music_attribution"` added to
  `SAFE_METADATA_KEYS`; `_safe_music_attribution` validates the raw metadata
  value at the serializer boundary and the block is set only when validation
  returns a value.
- Validation delegates to `safe_media_attribution_dict` in
  `mammamiradio/core/models.py`: URLs must be https with a hostname, no
  userinfo, no fragment, port 443 or none, no control characters or
  dot-segment tricks; `license_url` must be the exact Creative Commons URL
  matching `license_id` (CC-BY-3.0 or CC-BY-4.0); provider/basis pairs are
  restricted to `incompetech` + `bundled_manifest` (incompetech.com hosts,
  `/music/royalty-free/` path, optional `isrc=` query) and `jamendo` +
  `provider_reported` or `bundled_manifest` (jamendo.com hosts, `/track/` path, no query). Any
  malformed input drops the whole block instead of airing a bad link.
- `schema_version` stays `"1"`. The `/api/integrations/v1/now-playing` route
  path, ETag/304 semantics, and headers are unchanged.

## Why

The station plays bundled starter music and transient Jamendo tracks, both of which
carry license obligations (CC BY credit, source and license links, modification
notice). A Music Assistant provider or any other v1 consumer rendering
now-playing needs those credits from the same payload it already reads, so the
attribution the listener UI shows can appear wherever the stream is surfaced —
without scraping or a second endpoint.

## Compatibility

Existing fields retain their meaning and types. Payloads without validated
attribution omit the optional key; attributed music segments include it.
Consumers must tolerate this additive field. The schema version stays unchanged.
Contract tests cover the route path, ETag/304 semantics and headers. Serializer
tests cover presence, omission and malformed attribution.

The existing golden scenario has no attribution metadata and therefore omits
the optional field.

Future contract changes must follow the window, release ordering, and
`Contract-Change:` requirements in `CONTRACT.md`.
