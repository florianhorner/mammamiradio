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
   edge add-on points the soak Pi at a built `main` commit (`make edge-release`). That
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
   `git revert <cut-sha>` first. Revert the commit rather than hand-editing the version
   files back: the cut also folded both changelogs, and a version-only revert is refused
   by `check-changelog-sync.sh` locally and by `pre-release-check.sh` in CI.

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

**Why this replaced the rolling-RC model (2026-08-02).** `main` used to carry the next
number. Since version tags are only created by `addon-release.yml` on a `v*` tag push,
`main` named a nonexistent image for the whole span between opening an RC and pushing
its tag: 74.2 of the 76 days between 2026-05-18 and 2026-08-02, including one unbroken
24.7-day stretch. In that state a fresh install fails and rolls back, and an update
fails to download. Nobody sees it until someone clicks, because the Supervisor never
contacts the registry while reading the store. `scripts/check-advertised-version.sh`
and `.github/workflows/advertised-version.yml` are the guard.

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

- **Freeze image-affecting merges between the cut commit and the tag.** The window is
  short (one build plus a tag), but a merge landing inside it means the commit you soak
  and the commit you tag are not the same one. Pin the soak explicitly with
  `make edge-release ARGS="--target-sha <cut-sha>"` so the selection cannot silently
  drift to a newer commit.

  Note what `--target-sha` can and cannot do: it refuses to pin *anything but* that
  commit, but it cannot rescue a cut once an image-affecting commit has already landed
  on top. The edge branch takes its metadata from `origin/main`, so pinning an older
  image would advertise options the image does not implement — `cut-edge-release.sh`
  correctly refuses. If that happens, cut a fresh release from current `main`. The flag
  prevents drift; it does not undo it.
- **Don't merge a large off-theme PR into a cut you're about to make.** It joins that
  version's changelog whether or not it soaked. Cut first, then merge the big work so it
  soaks as the *next* version's content.
- **The changelog is folded IN the cut commit**, so the tagged tree describes exactly
  what it ships. Under the old order the fold landed after the tag and `v2.17.0`'s tree
  has no `[2.17.0]` section at all.

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
