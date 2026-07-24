# Golden fixture for the v1 now-playing contract

`v1_now_playing.json` is the byte-level reference for the frozen v1 wire
contract (see `CONTRACT.md` at the repo root). It is the real output of
`mammamiradio.integrations.serializer.serialize_now_playing` for the pinned
representative music-segment scenario in `generate_fixture.py` — not a
hand-written sample.

## Provenance

**Provisional — to be confirmed/replaced by a live-capture payload (T1)
before the drift check is made a required status check.** Until then it is
generated from pinned inputs mirroring the documented
`docs/integrations/sample-payloads/music.json` scenario.

## Pinned volatile fields

The serializer is pure, so the output is deterministic given pinned inputs.
Two fields are wall-clock values in a live capture and carry fixed pins here:

- `changed_at` = `1746500000.0`
- `now_playing.started_at` = `1746500000.0`

`generate_fixture.py --check` normalizes both sides to these pins before
byte-comparison, so a future live-captured fixture only needs its timestamps
mapped onto the pins. The ETag is an HTTP header derived from the body and
never appears in the payload.

## Regenerating (contract window only)

```sh
python tests/integrations/golden/generate_fixture.py          # rewrite
python tests/integrations/golden/generate_fixture.py --check  # verify (what CI runs)
```

A changed fixture must land through the process in `CONTRACT.md` (proposal in
`docs/contract-proposals/`, a `Contract-Change:` trailer or PR-body line, and
the matching sibling-fixture update in the Music Assistant provider).
