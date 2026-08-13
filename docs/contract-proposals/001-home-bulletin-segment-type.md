# 001: Home bulletin segment type

## Field

Permit the existing raw-subtype fields to carry `home_bulletin: str`:

- `now_playing.segment_type` is `"home_bulletin"` while Il Bollettino di Casa
  is playing.
- The paired `now_playing.segment_class` remains the existing value `"voice"`.
- A queued or predicted Casa item uses the same pair in `up_next[]`:
  `segment_type: "home_bulletin"` and `segment_class: "voice"`.

No key, field type, endpoint, schema version, cache header, or ETag rule changes.
The current v1 serializer derives `segment_class` from `SegmentType` and copies
the raw segment type into both now-playing and up-next payloads, so adding the
core enum value is wire-visible even without editing the serializer.

## Why

Il Bollettino di Casa is a distinct, listener-earned voice programme rather
than ordinary host banter. Exposing its truthful subtype lets integrations name
it accurately while retaining the stable `voice` display bucket. The Home
Assistant segment-type sensor also changes to raw state `home_bulletin` for the
programme and uses `mdi:home`; automations that enumerate sensor states must add
that state. No entity migration or state rewriting is proposed.

## Additive proof

`segment_type` is already an unrestricted `str`, and the integration guidance
defines it as a diagnostic subtype; core rendering is based on
`segment_class`. This proposal adds one valid string value without removing or
renaming a key, changing a type, narrowing a value range, or changing the
meaning of any existing value. `segment_class: "voice"` is already part of the
v1 schema and continues to mean spoken host content.

The 2026-08-11 compatibility audit found that the current Music Assistant
development provider consumes `/api/integrations/v1/now-playing` rather than
`/public-status` and branches on `segment_class`. Its explicit Casa fixture must
therefore prove that `home_bulletin` follows the existing voice path for both
now-playing and up-next. The bundled HACS integration accepts arbitrary raw
subtypes and renders any non-music subtype as channel content. Separately, the
add-on's REST-pushed segment-type sensor gains the raw `home_bulletin` state and
`mdi:home` icon described above. These observations reduce migration risk, but
do not waive the frozen-contract process or release ordering.

The representative shared music fixture remains byte-identical. It must not be
rewritten to use Casa, because doing so would replace its existing music
coverage rather than prove an additive subtype.

## Fixture diffs (pre-drafted, both repos)

- Addon:
  - Keep `tests/integrations/golden/v1_now_playing.json` byte-for-byte
    unchanged. Regenerating the current music scenario must produce those same
    bytes.
  - During the contract window, extend the frozen v1 contract tests with both
    Casa positions: current Casa serializes as
    `{"segment_class":"voice","segment_type":"home_bulletin"}`, and queued
    or predicted Casa serializes with the same pair in `up_next[]`.
  - Exercise the real `SegmentType.HOME_BULLETIN` mapping so the test fails if
    either now-playing or up-next falls back to `unavailable`.
- Music Assistant provider:
  - Keep
    `tests/providers/mammamiradio/fixtures/v1_now_playing.json` byte-for-byte
    identical to the addon's normalized shared music fixture.
  - Add a sibling Casa fixture, proposed as
    `tests/providers/mammamiradio/fixtures/v1_home_bulletin.json`, containing a
    current and up-next `home_bulletin` case paired with `segment_class:
    "voice"`. Provider tests must prove both cases take the existing voice-host
    rendering path and do not become music or unavailable.
  - Land this provider proof on Music Assistant `dev` before the addon can ship
    or enable Casa.

## Hard prerequisite gate

This sequence is mandatory; implementation readiness is not permission to
skip or reorder it:

1. Review and approve this proposal.
2. Florian creates the gitignored `.contract-window` marker. No agent creates
   it.
3. Add and land the Music Assistant support and Casa fixture on upstream `dev`,
   while preserving the shared normalized music fixture byte-for-byte.
4. Only then, in the open window, add the frozen addon v1 fixture/tests, run the
   current fixture generator and verify its bytes remain unchanged, and carry a
   `Contract-Change:` line in the addon PR body or commit trailer.
5. Complete the reviewed addon change, then Florian deletes
   `.contract-window` to close the window.

Until all prerequisite steps complete, Casa must not merge, ship, or be enabled
for listeners. This proposal does not open the window and does not authorize an
external provider write or an addon release.
