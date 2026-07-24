# Golden fixture for the v1 now-playing contract

`v1_now_playing.json` is the byte-level reference for the frozen v1 wire
contract (see `CONTRACT.md` at the repo root). It is the real output of
`mammamiradio.integrations.serializer.serialize_now_playing` for the pinned
representative music-segment scenario in `generate_fixture.py` — not a
hand-written sample.

## Provenance

**Provisional — to be confirmed or replaced by a payload captured from a
live addon before the drift check is made a required status check.** Until
then it is generated from pinned inputs mirroring the documented
`docs/integrations/sample-payloads/music.json` scenario.

## Pinned volatile fields

The serializer is pure, so the output is deterministic given pinned inputs.
Two fields are wall-clock values in a live capture and carry fixed pins here:

- `changed_at` = `1746500000.0`
- `now_playing.started_at` = `1746500000.0`

`generate_fixture.py --check` normalizes both sides to these pins before
byte-comparison. The ETag is an HTTP header derived from the body and never
appears in the payload.

## Swapping in a live-captured payload

Pinning the timestamps is necessary but not sufficient. `--check` compares
the whole payload against what `build_golden_snapshot()` renders, so a live
capture can only become the fixture in one of two ways:

1. Stage the live station to reproduce the pinned scenario exactly (same
   station identity, track metadata, queue contents, stream URLs), capture,
   normalize the timestamps, and confirm the bytes match; or
2. Update `build_golden_snapshot()` in `generate_fixture.py` to mirror the
   captured payload's inputs, regenerate, and confirm the generator output
   equals the normalized capture.

Either way the generator and the fixture change together in one reviewed
contract-window PR — a capture dropped in alone will fail CI.

## Regenerating (contract window only)

```sh
python tests/integrations/golden/generate_fixture.py          # rewrite
python tests/integrations/golden/generate_fixture.py --check  # verify (what CI runs)
```

A changed fixture must land through the process in `CONTRACT.md` (proposal in
`docs/contract-proposals/`, a `Contract-Change:` trailer or PR-body line, and
the matching sibling-fixture update in the Music Assistant provider).
