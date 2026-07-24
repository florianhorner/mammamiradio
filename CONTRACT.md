# The v1 Integration Contract

External consumers — first among them the Music Assistant provider — depend on
the exact wire behavior of this addon's now-playing endpoint. That surface is
frozen. This page lists what is frozen, how it may evolve, and how a change
lands.

## Frozen surface

- `mammamiradio/integrations/schema.py`
- `mammamiradio/integrations/serializer.py`
- `mammamiradio/integrations/now_playing.py`
- `tests/integrations/**` — the contract tests are part of the contract;
  weakening a test is touching the contract
- `tests/integrations/golden/v1_now_playing.json` — the golden fixture; the
  fixture, not the code, is the contract
- `CONTRACT.md` and `.github/workflows/contract-drift.yml` — the rules and
  the gate change through the same unlock as the surface they guard
- The endpoint path `/api/integrations/v1/now-playing`
- ETag / 304 semantics: weak ETag derived from the serialized body,
  `If-None-Match` returns 304, `Cache-Control: public, max-age=2`, HEAD
  supported
- `schema_version` stays `"1"` for this surface

A wire-visible change that routes through some other file (for example
`core/models.py`) is still a contract change. The drift CI catches payload
drift by rendering the serializer on every pull request, not by watching
paths; route and header behavior is held by the frozen pytest contract tests.

## Evolution policy: additive only

Within v1, changes may only add. Never remove a key, rename a key, change a
type, narrow a value range, or change the meaning of an existing field. New
optional keys are allowed through the process below. Anything breaking ships
as a new surface at `/api/integrations/v2/` and leaves v1 running.

## Release ordering

The Music Assistant provider change lands upstream **before** the addon ships
any contract change. Consumers must already understand a payload before any
addon in the wild can send it. No exceptions.

Mechanically enforced by the cross-repo checksum job once the fixture exists
on Music Assistant `dev`; until that bootstrap moment the job skips with a
notice and the ordering rule is enforced by pull-request review.

## Changing the contract: the maintenance window

Agents never edit the frozen surface directly. The path is:

1. **Queue a proposal.** Write `docs/contract-proposals/NNN-title.md`
   (format in that directory's README): the field, why, proof it is
   additive, and pre-drafted fixture diffs for both repos.
2. **Florian opens a window.** Only Florian creates the gitignored
   `.contract-window` marker and reviews the queue in one sitting. Windows
   are opened when he has review attention, never on a schedule.
3. **Land with the unlock.** Approved changes land with a
   `Contract-Change:` trailer in a commit message or a `Contract-Change:`
   line in the PR body, together with the regenerated golden fixture here
   and the matching fixture update in the Music Assistant provider (upstream
   first, per the ordering rule).
4. **Close the window.** Florian deletes the marker.

The `Contract-Change:` marker is an audit signal, not an authorization
boundary: it makes contract changes loud and traceable in PR history. The
authorization is the repository's review gate — the require-PR ruleset and
maintainer review; nothing reaches `main` without it.

## How the drift CI enforces this

`.github/workflows/contract-drift.yml` runs on every pull request:

- **Golden check (always):** renders the serializer for the golden scenario
  via `tests/integrations/golden/generate_fixture.py --check` and
  byte-compares against the fixture after normalizing the pinned volatile
  fields (`changed_at`, `now_playing.started_at`). Any payload-visible drift
  fails, no matter which file caused it. Route and header behavior — the
  endpoint path, ETag/304, `Cache-Control` — is locked separately by the
  frozen pytest contract tests under `tests/integrations/`, which run in the
  quality workflow on every PR.
- **Frozen-path gate (only when frozen paths change):** a PR touching the
  frozen paths, the fixture, this document, or the workflow itself fails
  unless `Contract-Change:` appears at the start of a line in a commit
  message or the PR body. The PR-body line survives squash-merge, so the
  audit trail persists on the merged history.
- **Cross-repo checksum:** compares this repo's fixture (sha256) against the
  sibling fixture in `music-assistant/server` (dev branch). Skips with a
  notice while the sibling does not exist yet; once it does, divergence
  between the two repos fails.

Unrelated PRs are not gated: the golden check passes untouched code by
construction, the trailer gate only fires on frozen paths, and the cross-repo
step is informational until the sibling fixture lands.
