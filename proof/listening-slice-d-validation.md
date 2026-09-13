# Listening Slice D: Scaletta presentation validation

Status: `complete`

Path A feature work for `main`, owned by the `providence` workspace. No PR was
opened, no branch was pushed, and no merge or deployment was performed.

## Scope and write-set

The user-visible promise was to make the Admin Scaletta truthful and legible:
Fonte identifies the music origin, artist subtitles remain useful on every
visible music row, and the Quando heading no longer collides with Tipo.

The declared write-set was:

- `mammamiradio/web/templates/admin.html`
- `tests/web/test_admin_mobile_invariants.py`
- `tests/web/admin_browser_smoke.js`
- `proof/listening-slice-d-validation.md`
- tool-generated `proof/preship-reviews/v2/**` receipts
- ignored screenshots and temporary fixtures under `tmp/listening-slice-d-qa/`

No API, backend, queue, audio, First Listen, release metadata, deployment, or
integrator-only file changed. The changelogs remain untouched.

## Repository checkpoints

- Initial clean base and `origin/main`: `ad7a384222e8a8022ddf50086eba0b8e4e9d140f`.
- Initial branch ancestry: `origin/main...HEAD = 0/0` before implementation.
- Implementation commit: `fd5d88df87824fa45fd78d03235daf98fd2bd9fc`.
- The implementation diff is 398 additions/deletions across the three product
  and test files. The final branch check repeats ancestry, exact write-set, and
  the under-1,000-line limit after the evidence commits.
- The final v2 receipt is generated from the content head that includes this
  validation document. Its exact content-addressed path is the JSON file emitted
  under `proof/preship-reviews/v2/<reviewed-content-sha256>/`; the final
  verification command below is the authoritative path and binding check. The
  final checkout head is the receipt commit after that content head; the
  implementation content remains unchanged.

## Implementation

- Widened `.a-programme .col-time` from `54px` to `72px`, retaining the existing
  responsive table/card rules.
- Added the escaped `programmeSourceLabel()` presentation helper using the
  existing normalized `segmentSourceKind()` value. The contract is:
  `Local music`, `Jamendo`, `Starter crate`, and `Download` for known music
  origins; rendered/unknown music falls back to `Music`, planned music to
  `Planned`; rendered nonmusic is `Studio`, and planned nonmusic is `Planned`.
  Nonmusic is classified before stale source metadata can leak into its label.
- Kept `it.source` exclusively as the actionability boundary:
  `source === 'rendered_queue'`. Stable queue IDs, playlist links, filtering,
  ordering, and truncation behavior remain intact.
- Changed music subtitles to artist-only for every visible music row, including
  NEXT and later rows. Existing title parsing, `Unknown` suppression,
  nonmusic subtitles, and Station ID suppression remain in place.
- Expanded the render-cache key to cover all consumed identity, source,
  title/artist, nonmusic metadata, playlist-link, Spotify, and duration fields,
  so same-ID metadata updates re-render without manually clearing
  `_lastProgrammeHash`.
- Extended the Python invariants and executable browser smoke fixture with source
  precedence/normalization/fallbacks, escaping, actionability, filtering,
  truncation, links, stable IDs, nonmusic rows, cache invalidation, responsive
  geometry, and control-size checks.

## Baseline evidence

Before the implementation, the isolated browser probe showed the regression:

- a local music row displayed the raw `local` source suffix;
- only the NEXT music row had a subtitle, and a later music row had none;
- a same-ID title update remained the old title because the cache key did not
  consume all renderer metadata.

The baseline focused invariants passed 75 tests. Baseline screenshots were
captured from the isolated pre-change renderer:

- [before 1280px](../tmp/listening-slice-d-qa/before-1280.png) — 1280×903
- [before 900px](../tmp/listening-slice-d-qa/before-900.png) — 900×903
- [before 390px](../tmp/listening-slice-d-qa/before-390.png) — 390×1262

The old probe used one long row; the after fixture deliberately uses three
rows to exercise later-row subtitles and source/actionability states. They are
isolated before/after geometry captures, not pixel-identical data snapshots.

## Validation results

All commands below were run against the final implementation content unless
otherwise noted.

| Check | Result |
|---|---|
| `.venv/bin/python -m pytest tests/web/test_admin_mobile_invariants.py` | PASS — 79 passed |
| `ADMIN_BROWSER_SMOKE_URL=http://127.0.0.1:18765 .venv/bin/python -m pytest tests/web/test_admin_browser_smoke.py` | PASS — 2 passed |
| `PLAYER_SMOKE_URL=http://127.0.0.1:18765 ./scripts/player-smoke.sh tests/web/admin_browser_smoke.js` | PASS — `ok=true`, 87 checks; Scaletta 26 checks, 8 source rows, 2 compatibility rows, 8 fallback rows, 7 artist rows; no page/console errors |
| Browser geometry | PASS at 1280, 1024, 1023, 900, 880, 769, 768, and 390px; no document/table/row horizontal overflow |
| Browser controls and responsive visibility | PASS — visible controls are at least 44×44px; heading columns, source/duration visibility, phone card layout, and source/artist behavior match the requested breakpoints |
| `node --check tests/web/admin_browser_smoke.js` and `git diff --check` | PASS |
| `make check` | PASS — 9,425 passed, 4 skipped, 58 deselected, 1 warning; 92.63% coverage against a 92.0% floor; media proof, Ruff, formatting, mypy, vulture, and coverage ratchet all passed |

The browser fixture also verified source precedence and trimming/case
normalization, explicit `download` and effective-empty source-kind compatibility
labels, unsafe/unknown values and HTML escaping, rendered versus planned
actionability, `playlist_index`/`spotify_id` links, stable remove IDs, no more
than eight rendered rows, filter-relative labels, nonmusic and Station ID
subtitles, and same-ID updates for title-only, artist, source, Spotify, title,
category, and duration metadata without clearing `_lastProgrammeHash`.

The browser smoke captured the after screenshots through the same isolated
loopback server:

- [after 1280px](../tmp/listening-slice-d-qa/slice-d-after-1280.png) — 1280×903
- [after 900px](../tmp/listening-slice-d-qa/slice-d-after-900.png) — 900×903
- [after 390px](../tmp/listening-slice-d-qa/slice-d-after-390.png) — 390×1474

Visual comparison confirms separated Quando/Tipo headings at desktop width,
the intended tablet visibility, phone card overflow containment, artist-only
subtitles on all three visible music rows, and readable `Local music`, `Jamendo`,
and `Planned` source labels. The smoke route intentionally blocks the optional
off-origin Google Fonts stylesheet; this was the only blocked off-origin
request, and it produced no console error.

## Independent review

A fresh read-only review was rerun after each in-scope correction. The review
history was:

- A classic-source mapping was corrected to the explicit Slice D generic
  `Music`/`Planned` fallback contract.
- The screenshot fixture now creates `tmp/listening-slice-d-qa/` before capture
  and returns those workspace-relative paths, keeping the proof portable across
  checkouts.
- The browser matrix now has an explicit `download` row and an effective-empty
  source-kind row, asserting `Download` and `Music` respectively.
- A reviewer suggestion to create distinct demo/classic labels was not adopted:
  the attached Slice D contract explicitly requires those values to use the
  generic rendered/non-rendered fallbacks. Dedicated demo/classic browser rows
  were added to make that contract executable.
- A stale source-kind leak on nonmusic rows was fixed with a type guard; the
  focused invariants and real browser smoke were rerun afterward.
- The final independent review reported: “No actionable defects found,” with
  the focused invariant suite passing and no unresolved findings.

The tool-generated v2 receipt is committed under
`proof/preship-reviews/v2/` and binds the final ordinary-tree content to the
clean review ledger record. It is deliberately located by the generated
content-addressed namespace rather than copied into this file: changing this
file to include its own content hash would change the hash being attested.

## QA boundaries and limitations

Admin QA is complete for this presentation slice: the source-label, subtitle,
cache, actionability, responsive layout, escaping, truncation, and geometry
contracts above were exercised in the real browser harness and static Python
invariants.

Player QA is not applicable. The final diff remains Admin-only presentation and
browser-test work; it does not alter the listener player, stream, queue, or
audio path. The loopback server and fixtures prove UI behavior only. No live
provider, Home Assistant, deployment, physical speaker, or human audibility
proof is claimed.

## Changelog and local PR draft

Release metadata was intentionally left untouched. Integrator anchors are:

- `CHANGELOG.md:7`, insert before `### Added` at line 50;
- `ha-addon/mammamiradio/CHANGELOG.md:3`, insert before `### Added` at line 38.

The exact proposed entry is:

> Scaletta's Fonte column identifies the music source, available artists appear below every song title, and the Quando heading fits without overlapping Tipo across the existing responsive layouts.

### Local PR draft (not published)

```markdown
## Summary
Clarify Scaletta's Fonte provenance labels, show available artists below every
visible song title, and give Quando enough width to remain separate from Tipo.

## QA Impact
Admin-only presentation and browser-contract coverage. Player QA is not
applicable; no player, audio, queue, API, or backend behavior changed.

## Proof
proof/listening-slice-d-validation.md
The v2 review receipt is generated under proof/preship-reviews/v2/ and the
focused invariants, real Admin browser smoke, screenshots, and make check are
recorded above.

## Integrator notes
Do not edit release files in this feature lane. Insert the exact proposed
sentence above under the current Unreleased sections at the recorded anchors.
```

Next action: `/ship` only when separately authorized. No external PR or other
publication action is pending from this workspace.
