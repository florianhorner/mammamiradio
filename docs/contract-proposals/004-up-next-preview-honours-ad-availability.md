# 004: Up-next preview honours ad availability

## Field

No key is added, removed, renamed, or retyped. This changes which **entries**
appear in an existing list for one station state:

- `up_next[]` entries with `predicted: true` and `type: "ad"`

Today the predicted tail of `up_next` can contain an `ad` entry while the
station has no ad it is able to air. This proposal omits that predicted entry
and lets the simulation continue with the segment the producer will really
make next (music).

## Why

`preview_upcoming` (`mammamiradio/scheduling/scheduler.py`) simulates pacing
for the predicted part of `up_next`, and its only caller is the v1 now-playing
surface (`mammamiradio/integrations/now_playing.py`). The live scheduler passes
`ad_available=state.ad_programme_available` when it decides the next segment;
the preview does not pass it, so it defaults to "an ad is available".

When `ad_programme_available` is false and an ad break is due, the producer
airs music while the preview still predicts an ad. A consumer rendering the
up-next list then shows a break that never happens.

The divergence is deliberate today, not an oversight:
`tests/scheduling/test_scheduler.py::test_the_v1_schedule_preview_preserves_its_frozen_ad_prediction`
pins it so that live ad suppression could not silently change the frozen
surface. That is why this is queued here and not shipped as a fix.

Reach is small. `ad_programme_available` is false only when no packaged ad is
serviceable for the active spoken mode and live ad writing is also unavailable.
The shipped pack carries ads for both spoken modes, so a stock install does not
reach this state. Only `predicted: true` entries are affected, and only while
fewer real segments are queued than the up-next limit. Audio, pacing state, and
the real schedule are untouched either way.

## Additive proof

Strictly speaking this is **not** additive, which is why it is queued: it
changes the contents of an existing list for one input class.

- No key, type, or enum value changes. `type: "ad"` remains a valid value and
  still appears whenever an ad can air.
- For every state where `ad_programme_available` is true, which includes every
  scenario in the golden fixture, the output is byte-identical.
- For the one affected state, a consumer sees a `music` prediction where it
  saw an `ad` prediction. Entries marked `predicted: true` are a forecast of
  the schedule, so a consumer that already copes with the forecast being
  wrong in this state copes with it being right.

Implementation sketch for the window, outside the frozen files:

- `scheduler.py`: pass `ad_available=state.ad_programme_available` in the
  `preview_upcoming` decision call.
- `tests/scheduling/test_scheduler.py`: invert the pinned test named above so
  it asserts no `ad` entry is predicted when ads are unavailable.

Two related simplifications in the same preview are left as they are and are
not part of this proposal: it ignores the producer's ad-break reservation
clamp, and it ignores the runway governor that turns optional speech into music
when queued audio is short.

## Fixture diffs (pre-drafted, both repos)

- Addon: no diff against `tests/integrations/golden/v1_now_playing.json`. No
  golden scenario sets `ad_programme_available` to false. If the window wants
  the new behaviour pinned on the wire, add one `generate_fixture.py` scenario
  with ads unavailable and an ad break due; its `up_next` tail then carries
  `music` where the current code would emit `ad`.
- Music Assistant provider: no diff against
  `tests/providers/mammamiradio/fixtures/v1_now_playing.json` unless the
  scenario above is added, in which case the same bytes land on both sides
  after the pinned volatile-field normalization.
