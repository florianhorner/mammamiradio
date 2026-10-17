# Release model: themes vs. versions (read this first)

This page is the **strategy** for how mammamiradio is versioned and released — the
mental model every contributor and agent must share. For the **mechanical cut steps**
(tagging, CI promotion, the config/pyproject bump, cooldown gate) see
[`runbooks/ha-addon.md`](runbooks/ha-addon.md). That runbook owns the *how*; this page
owns the *what number, and when*.

It exists because parallel work once produced conflicting "release train" plans that
invented version numbers (`2.15`, then `2.16`/`2.17`/`2.18`) for unstarted work. The
fix is one shared model, stated below.

## The one model: single trunk, cut-don't-open, promote-what-you-soaked

1. **`main` advertises the last published version.** Its
   `ha-addon/mammamiradio/config.yaml`, `pyproject.toml`, and
   `custom_components/mammamiradio/manifest.json` name a version that exists in the
   registry. The Supervisor reads `config.yaml` from this repo's default branch and
   pulls `{image}:{version}`. There is one version field, and it serves as both what
   the store offers and what the pull requests. Home Assistant documents the rule for
   a prebuilt `image:`: *"this needs to match the tag of the image that will be used."*
2. **Edge is the continuous soak.** Every push to `main` builds a `:sha` image, and the
   edge add-on points the Edge test installation at a built `main` commit (`make edge-release`). That
   image is the release artifact, running on real hardware before anything is published.
3. **A release is one cut commit.** `chore(release): cut X.Y.Z` bumps the three version
   files and folds `## [Unreleased]` into `## [X.Y.Z]` in both changelogs. Its `:sha`
   image builds, you can soak that exact commit
   (`make edge-release ARGS="--target-sha <sha>"`), then you tag it and CI promotes the
   prebuilt image. The tagged commit's `config.yaml` version must equal the tag, so the
   tagged tree also carries the release notes it ships.
4. **The next number is chosen at cut time.** Between releases it lives nowhere in the
   tree; the pending content sits in `## [Unreleased]`.
5. **A failed release gets reverted before it gets debugged.** The window where `main`
   names an unpublished version opens when the cut commit merges and closes when both
   architecture `promote` jobs finish. If any stage of `addon-release.yml` fails, land
   the revert first: `git revert --no-commit <cut-sha>`, then commit.
   Revert the complete cut: it also folded both
   changelogs, and a version-only revert is refused by `check-changelog-sync.sh` locally
   and by `pre-release-check.sh` in CI. Review-receipt admission is retired; no receipt restoration is required.
6. **Physical proof binds complete cut content.** Finalize version, changelogs, and all cut content before HA runs; its hardware-neutral digest survives squash and rejects all other drift, while `source_commit` remains provenance.

```
v2.17.0   published, and main advertises 2.17.0
   |      work accumulates under ## [Unreleased]; edge soaks :sha builds
   |
   |      chore(release): cut 2.18.0     <- window opens
   |      addon-build.yml builds :sha, optional exact-SHA edge soak
   |      tag v2.18.0: pre-flight, smoke x2 arch, promote x2 arch
   |                                     <- window closes after both promotes
   v
v2.18.0   published, and main advertises 2.18.0
```

The advertised stable version must have published images for both supported
architectures. Supervisor can display an entry before contacting the registry,
so a missing image may only become visible when an operator installs or updates.
`scripts/check-advertised-version.sh` and the advertised-version workflow check
that invariant.

## Themes are not versions

The trap is treating a *future version number* as a *lane you plan work into*. It isn't.

| | **Themes / feature buckets** | **Version numbers** |
|---|---|---|
| Examples | "HA-native maturity", "Listener UX & a11y", "Privacy docs" | 2.14.1, 2.15.0, 2.16.0 |
| How many at once | **Many, in parallel** (branches off `main`) | **One next number at a time** |
| Assigned when | At planning time (name them freely) | At **cut time** (stamped on whatever soaked) |
| Lives where | A milestone / branch name | the cut commit + the git tag |

There are **not** `2.15`/`2.16`/`2.17`/`2.18` parallel version lanes. There is one next
number, and between releases it exists **only in your head** — `config.yaml` names the
last published version, and the pending content sits in `## [Unreleased]`. Whatever
soaked clean when you cut gets the label; anything that lands after becomes the version
after. (The only exception is a short-lived `release/X.Y` branch to hotfix an
*already-published* old line.)

**The rule to remember:**

> Plan in themes; release in one number at a time. The number is assigned when the work
> soaks — not when you start it, and never before it can actually be published.

## Practical sequencing (soak hygiene)

Mechanically, feature work may merge to `main` at any time — the only *hard* constraint
is "`main` never advertises a version that has no image, outside the cut window." But
there is a real discipline on top of it:

- **Acquire the freeze before the cut and keep it through publication.** One release
  operator owns freeze, cut and resume; follow the add-on runbook's "The cut window".
  `land-pr.sh` requires a verified freeze for stable-version changes. Pause human
  image-affecting merges until both architecture promotions succeed and the
  advertised-version check passes. A merge inside this window can change the commit
  between soak and tag. Pin the soak explicitly with
  `make edge-release ARGS="--target-sha <cut-sha>"` so the selection cannot silently
  drift to a newer commit.

  `--target-sha` pins that exact commit. It refuses the cut if newer image content
  (`ha-addon/mammamiradio/`, `ha-addon/mammamiradio-edge/`, `mammamiradio/`,
  `pyproject.toml`, `radio.toml`, `model_registry.toml`, the build workflow)
  has landed on top. Only a valid top-level edge `version:` change is exempt.
  The edge branch takes its metadata from `origin/main`, so an older image would
  advertise options it does not implement. A commit that only
  re-triggers the build (a dev-dependency bump, a validator script, a test) does not
  make the image stale. If that newer commit has an attempted main build, it needs
  a successful run; failed, cancelled or unfinished runs block the pin until a
  retry succeeds. No run, or only completed skipped runs, is allowed when image
  content is unchanged. The workflow deliberately skips edge-version cuts.
  If real content landed, cut a fresh release from current `main`.
- **Don't merge a large off-theme PR into a cut you're about to make.** It joins that
  version's changelog whether or not it soaked. Cut first, then merge the big work so it
  soaks as the *next* version's content.
- **The changelog is folded IN the cut commit**, so the tagged tree describes exactly
  what it ships. Under the old order the fold landed after the tag and `v2.17.0`'s tree
  has no `[2.17.0]` section at all.
- **Finalize the cut before HA runs.** Physical evidence binds the complete cut
  content, excluding only validated HA receipts. Review-receipt admission is
  retired; existing historical files do not need refreshing.
- **The cut asks whether the hosts' models were decided lately and are still alive.**
  `scripts/pre-release-check.sh` section 11 refuses a `model_registry.toml` whose
  `last_reviewed` stamp is missing, malformed, or older than 45 days, and refuses a
  pinned model the provider has deprecated or retired (read live from the public
  docs; unreachable docs are WAIVED and named, never passed). Refresh the stamp only
  after reading `python scripts/check_model_registry.py --providers` and deciding each
  pin; holding a legacy pin on purpose is a valid decision, say so in the comment. The
  check runs in CI only when the PR changes a version line (other PRs that touch
  `pyproject.toml` note it and skip) and always under `make pre-release`, so a stale
  registry surfaces at the cut, not as a red Dependabot PR. Between cuts,
  `.github/workflows/model-registry-watch.yml` runs `--report` every Monday and
  opens, updates, or closes a `model-registry-watch` issue to surface stale
  stamps and provider drift between cuts.
  - `scripts/check_model_registry.py` answers two questions about `model_registry.toml` without provider keys. `--age` reads `[models].last_reviewed`, the day a maintainer last *decided* the pins (bump or hold; the comment beside it says which), and fails when the stamp is missing, malformed, in the future, or older than 45 days. The limit is a constant with no override on purpose: the honest escape is the stamp, refreshed only after reading the provider report. `--providers` reads the public Anthropic models overview and per-ID lifecycle table plus OpenAI's models and deprecations pages and classifies every catalog entry as `current`, `legacy`, `unlisted`, `deprecated(<date>)` or `retired`; `--gate drift` (default) fails on anything not current, `--gate liveness` only on deprecated or retired. Exit 2 means the docs could not be read or lost the anchors the parser relies on, so a broken scraper is never green. `--report` runs both checks unconditionally with one combined exit code.
  - The pre-release section (body in `scripts/model-registry-gate.sh`) reports review age as PASS or FAIL, where a checker failure is a FAIL that blocks the cut, and liveness as PASS, FAIL, or WAIVED with the reason when the provider docs are unreachable or the liveness checker fails. Because `scripts/pre-release-check.sh` also runs in CI on any PR touching `pyproject.toml` (Dependabot included), in CI the section runs only when the diff against `origin/main` changes a version line and notes itself and skips otherwise; under `make pre-release` it always runs, and `MMR_MODEL_REGISTRY_GATE=always` forces it. It is deliberately **not** an every-PR calendar assertion: the normal pytest suite runs the gate's shell contract with committed fixtures, while only a release cut asks whether the real review stamp is still fresh.
  - The Monday watcher: exit 0 closes any open `model-registry-watch` issue, exit 1 (a stale stamp, or a pin that is no longer current at its provider: legacy, unlisted, deprecated or retired) opens one or comments the week's report on it, and a run that could not read the docs, or crashed before rendering a report, does the same under a title and body naming the watcher rather than a pin, because a broken scraper that stays silent is the one outcome the check exists to prevent.
  - Self-tests: `bash tests/workflows/test_model_registry_watch.sh`, and the cut-gate shell self-test `tests/workflows/test_model_registry_gate.sh`, which passes alternate registry and fixture paths explicitly to the sourced gate function; the production pre-release call ignores ambient overrides. Fixtures are trimmed real captures under `tests/scripts/fixtures/model_registry/`. Agents never refresh the stamp to make a gate green. Liveness at runtime is separate: a 404 on a retired model trips the provider backoff and the station keeps airing (PR #315).

### Advertised-version guard

`scripts/check-advertised-version.sh` asks GHCR whether the version `main` advertises to the HA Supervisor exists, for both arches, over the anonymous token endpoint (no scope needed). Home Assistant requires a prebuilt `image:` add-on's `version:` to name a real tag; when it does not, fresh installs fail and updates roll back.

- Verdicts: a definitive 404 fails (exit 1). An unreachable registry reports `UNKNOWN` and exits 0, since a DNS blip is not a broken release. The last output line is always `VERDICT: pass|unknown|fail` — callers that need the tri-state read that, not the exit code.
- Runs daily against `main` via `.github/workflows/advertised-version.yml`, which opens one `release-drift` issue while drift persists and closes it on a `pass`.
- `.github/workflows/dependabot-automerge.yml` reads the verdict on Dependabot PR events, config.yaml pushes to main, hourly, and on demand. `fail` disarms and labels affected PRs `cut-window-hold`; `pass` allows verified patch/minor PR events to arm; `unknown` leaves auto-merge unchanged. The sweep only disarms. Test: `bash tests/workflows/test_dependabot_automerge_gate.sh`.
- Kept out of `pre-release-check.sh` and `make pre-release` on purpose. `quality.yml` runs those on every version-file PR, so a `chore(release): cut X.Y.Z` PR names a version whose image is not built yet and could never go green.
- Self-test: `bash tests/workflows/test_advertised_version.sh`, hermetic via `MAMMAMIRADIO_TAG_PROBE` plus a `curl` PATH shim. Two cases guard the deadlock from coming back across the whole cut-PR gate closure.

## Coordinating parallel workspaces

Independent Conductor workspaces/agents can't command each other, so the shared
reference is: **this page (the model) + GitHub milestones (the target window)**. Use a
milestone per upcoming version (`v2.15`, …) to group what's aimed at the next cut. Don't
invent a second source of truth for the release model — this page and the runbook are
it. An agent must **never** auto-push a release tag or bump a version without the
maintainer's explicit go in the current message (tags publish a release HA users
auto-update to).

## The HACS integration shares the release number (decided 2026-06-25 — settled)

This repo ships **two products**: the **add-on** (the station) and the **HACS integration**
(`custom_components/mammamiradio/`, the controllable `media_player` + media source). HACS
decides "is there an integration update?" by reading this repo's **GitHub releases** — which
are the add-on's `v*` tags.

**Decision: keep ONE repo. The integration's `manifest.json` version is kept in lockstep
with the release number** (bumped together with `config.yaml` + `pyproject.toml`; enforced at
every guard layer — the pre-commit hook (`scripts/check-version-sync.sh`), the PR version-sync
check (`scripts/pre-release-check.sh`), the release-tag preflight (`addon-release.yml`), and an
always-on test (`test_integration_manifest_version_matches_pyproject`) — and listed in the
runbook's "Version: three files" table). The
integration *ships with the station and carries the station's version.* On adopting this
(2026-06-25) the manifest jumped `1.0.0 → 2.14.1` to join the station's version line.

The lockstep only aligns the version *number* HACS and Home Assistant display (HACS shows
the release tag; HA shows the manifest version) — it does **not** change HACS update
behavior or reduce the update-noise described next. That noise is a separate, accepted
tradeoff, and the only real fix for it is the repo split below.

Under cut-don't-open the lockstep is exact: the tagged commit *is* the cut commit, so its
`manifest.json` always carries the release number, and `main` between releases carries the
last published one. Home Assistant therefore shows an integration version that matches a
real release at all times. (Under the old model it did not: `main` ran a number ahead, so
HA reported `2.18.0` for an integration whose newest release was `2.17.0`. Releases tagged
from an older soaked SHA could also ship a stale manifest — the first `2.14.x` shipped
`1.0.0`. Both are historical.)

**Accepted tradeoff (do not re-raise):** because HACS keys off releases, **every** station
release shows up in HACS as an "integration update" even when `custom_components/` did not
change. This is acceptable while the integration is **custom-repository-only** (manually
added by opt-in power users), so the noise reaches a small, savvy audience.

**Why not split the integration into its own repo:** that is the textbook HACS answer and it
*would* fix the update-noise, but it is real, ongoing cross-repo overhead (the integration
shares the now-playing contract and API shapes with the station), and premature for a
brand-new, opt-in integration. We chose simplicity over a second repo on purpose.

**The only triggers to revisit the split** (tracked as a GitHub issue, not re-litigated ad
hoc): (1) integration users report the update-noise as a real problem, or (2) the integration
is promoted to the **HACS default store** (which widens the audience enough that the noise
matters). Absent one of those, the answer is "single repo, version-synced" — settled.
