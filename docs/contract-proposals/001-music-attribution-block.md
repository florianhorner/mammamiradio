# 001: additive music_attribution block on v1 now-playing

Status: approved-retroactively pending window.

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

Wiring, exactly as implemented on this branch:

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
  `provider_reported` (jamendo.com hosts, `/track/` path, no query). Any
  malformed input drops the whole block instead of airing a bad link.
- `schema_version` stays `"1"`. The `/api/integrations/v1/now-playing` route
  path, ETag/304 semantics, and headers are unchanged.

## Why

MA-provider era attribution on the wire. The station now plays rights-aware
bundled starter music and explicitly transient Jamendo tracks, both of which
carry license obligations (CC BY credit, source and license links, modification
notice). A Music Assistant provider or any other v1 consumer rendering
now-playing needs those credits from the same payload it already reads, so the
attribution the listener UI shows can appear wherever the stream is surfaced —
without scraping or a second endpoint.

## Additive proof

New optional key only. No removals, no renames, no type changes, no meaning
changes to existing fields. The key is absent unless a validated attribution
exists, so every existing consumer sees byte-identical payloads for every
segment it saw before; a consumer that ignores unknown keys is untouched.
`schema_version` is unchanged and the frozen pytest contract tests still hold
the route path, ETag/304, and header behavior. The new serializer tests in
`tests/integrations/` cover presence, omission, and rejection of malformed
attribution input.

## Fixture diffs (pre-drafted, both repos)

- Addon: no diff. `tests/integrations/golden/v1_now_playing.json` is untouched
  — the golden scenario carries no attribution metadata, and an absent input
  yields an absent key, so the rendered payload is byte-identical (verified:
  `git diff` of the fixture against the merge base is empty, and the
  contract-drift render on the PR shows no payload drift).
- Music Assistant provider: no diff to
  `tests/providers/mammamiradio/fixtures/v1_now_playing.json` for the same
  reason. A provider-side fixture exercising the new key becomes part of the
  provider-first release described in `CONTRACT.md` once the window opens.

A proposal is not a change; per the release-ordering rule this lands provider
side first, addon second, inside an open contract window with the
`Contract-Change:` trailer.
