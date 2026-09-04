# 003: Title-only labels for tracks with no artist

## Field

No key is added, removed, renamed, or retyped. This changes the **value**
produced for two existing string fields when — and only when — a track has no
artist at all:

- `now_playing.title: str | null`
- `up_next[].title: str`

Today such a track renders as `" – Song Title"` (a leading en dash, a space, and
nothing before it). This proposal renders `"Song Title"`.

## Why

`Track.display` is `f"{artist} – {title}"` unconditionally. Since the local
music library began reading embedded tags, an operator's own MP3 with no artist
tag and no `Artist - Title` filename legitimately carries `artist == ""` — the
station deliberately refuses to invent an artist from a filename slug, because
guessing one is worse than admitting there isn't one.

`Track.display` becomes `segment.metadata["title"]` (`producer.py`), which
`_queue_shadow_entry` copies into the queued row's `label`, which
`_build_up_next` copies verbatim into `up_next[].title`. So the malformed
string is already on the v1 wire today: a consumer rendering an up-next list
for an operator with untagged local files shows a row that begins with a space
and a dangling en dash. `now_playing.title` is affected only in the narrower
case where `metadata.title_only` is absent, since `_title_for_segment` prefers
it — the producer stamps `title_only` for rotation music, so that exposure is
genuinely small.

This is a correctness fix to data the surface is already emitting, not a new
capability.

## Additive proof

Strictly speaking this is **not** additive, which is why it is queued rather
than shipped: it changes the value of an existing field for one input class.

Why it cannot break an existing consumer:

- The keyset is unchanged. No key added, removed, renamed, or retyped.
- Both fields are already free-form display strings with no documented format.
  A consumer cannot have been parsing `" – "` out of them, because the same
  fields carry `"Artist – Title"`, `"Notizie Flash"`, `"Host banter"`, and
  brand names — there is no grammar to depend on.
- The affected input class is exactly "track with an empty artist". Every track
  with an artist is byte-identical before and after.
- The current value is not a value any consumer would want to keep: a title
  prefixed with a separator and no left operand is a rendering artifact.
- Nullability is unchanged: a title-only track produced a non-empty string
  before and produces a non-empty string after.

Meaning is preserved and arguably restored — the field means "what to show the
listener", and a leading dash was never that.

## Fixture diffs (pre-drafted, both repos)

The current golden fixture contains no empty-artist track, so **no existing
fixture line changes**. The behavior is unobservable in the fixture as it
stands, which is also why the drift CI does not currently catch it.

To make the shape **observable** on both sides, the proposal adds one `up_next`
entry to the scenario inputs:

- Addon: `tests/integrations/golden/v1_now_playing.json` — append to `up_next`:

  ```json
  {
    "segment_class": "music",
    "segment_type": "music",
    "title": "Salvatore On Everything",
    "predicted": false
  }
  ```

  plus the matching queued-segment scenario input in `generate_fixture.py`.

- Music Assistant provider:
  `tests/providers/mammamiradio/fixtures/v1_now_playing.json` — the same
  appended object, same bytes after the pinned volatile-field normalization.

**Be clear about what that fixture entry does and does not do.**
`generate_fixture.py` builds queued segments as plain dicts carrying a
pre-formatted `label`; it never constructs a `Track` and never calls
`Track.display`. `_build_up_next` copies that `label` verbatim. So the fixture
entry documents the intended shape for the provider — it does **not** guard the
behavior. A `Track.display` regression would still render byte-identical
fixture output.

The real guard is a unit test on the property itself,
`tests/core/test_models.py::test_track_display_is_title_only_without_an_artist`,
which ships with this change and is not part of the frozen surface. Anyone
reviewing this proposal in a window should weigh it on that basis, not on the
fixture.

Without this proposal being accepted, the fix is confined to `core/models.py`
and the golden fixture stays untouched; the wire keeps emitting `" – Title"`.

## Release ordering

Provider side first, addon second (`CONTRACT.md`). Since no existing fixture
line changes, a provider that has not yet taken the fixture addition is
unaffected either way — the ordering rule is satisfied trivially here, but is
still followed.
