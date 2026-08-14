# Contract-change proposals

Queue for changes to the frozen v1 integration surface (see `CONTRACT.md` at
the repo root). Agents park proposals here instead of editing frozen paths;
Florian reviews the queue when he opens a contract window.

## File name

`NNN-title.md` — three-digit sequence number plus a short kebab-case title,
e.g. `001-add-listener-count.md`.

## Format

```markdown
# NNN: <title>

## Field
What is added, exact key path and type, e.g. `now_playing.listener_count: int | null`.

## Why
Describe the consumer need this proposal serves. One paragraph.

## Additive proof
Why this cannot break an existing consumer: new optional key only, no
removals, no renames, no type changes, no meaning changes to existing fields.

## Fixture diffs (pre-drafted, both repos)
- Addon: diff against `tests/integrations/golden/v1_now_playing.json`
  (and the `generate_fixture.py` scenario inputs if they change).
- Music Assistant provider: diff against
  `tests/providers/mammamiradio/fixtures/v1_now_playing.json`.
Same bytes on both sides after the pinned volatile-field normalization.
```

A proposal is not a change. Nothing lands until a window is open and the PR
carries the `Contract-Change:` trailer or PR-body line — provider side first,
addon second (release ordering rule in `CONTRACT.md`).
