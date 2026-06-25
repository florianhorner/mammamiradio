# Release model: themes vs. versions (read this first)

This page is the **strategy** for how mammamiradio is versioned and released — the
mental model every contributor and agent must share. For the **mechanical cut steps**
(tagging, CI promotion, the config/pyproject bump, cooldown gate) see
[`runbooks/ha-addon.md`](runbooks/ha-addon.md). That runbook owns the *how*; this page
owns the *what number, and when*.

It exists because parallel work once produced conflicting "release train" plans that
invented version numbers (`2.15`, then `2.16`/`2.17`/`2.18`) for unstarted work. The
fix is one shared model, stated below.

## The one model: single trunk, rolling RC, promote-don't-rebuild

1. **`main` is always exactly ONE next-version release candidate.** Its
   `ha-addon/mammamiradio/config.yaml` + `pyproject.toml` carry the *next* number you
   will publish — not "where we are." A stable tag that lags `main` by many commits is
   **correct**, not stale.
2. **Edge is the continuous soak.** Every push to `main` builds a `:sha` image; the
   edge add-on points the soak Pi at the newest *built* `main` commit
   (`make edge-release`). That is the real release artifact, running for real, before
   anything is published.
3. **A release = tag a soaked `:sha`; CI PROMOTES it (no rebuild).** Pushing `vX.Y.Z`
   re-tags the already-built, already-soaked image — the published bytes are identical
   to what ran on the Pi. The single hard rule: **the tagged commit's `config.yaml`
   version must equal the tag.**
4. **Right after tagging, open the next RC** — bump `main` to the next number and fold
   the changelog.

```
v2.13.0  ── PUBLISHED (last real tag)
   │  main bumped to 2.14.1  → baked into every :sha (rolling RC)
   ▼
2.14.1   ── soaking on edge (Pi runs :sha)
   │  tag v2.14.1 on a soaked sha → CI promotes :sha → :2.14.1  (no rebuild)
   ▼
2.14.1   ── PUBLISHED, then bump main → 2.15.0
   ▼
2.15.0   ── now the rolling RC, soaking on edge …
```

## Themes are not versions

The trap is treating a *future version number* as a *lane you plan work into*. It isn't.

| | **Themes / feature buckets** | **Version numbers** |
|---|---|---|
| Examples | "HA-native maturity", "Listener UX & a11y", "Privacy docs" | 2.14.1, 2.15.0, 2.16.0 |
| How many at once | **Many, in parallel** (branches off `main`) | **One next number at a time** |
| Assigned when | At planning time (name them freely) | At **tag time** (stamped on whatever soaked) |
| Lives where | A milestone / branch name | `config.yaml` + the git tag |

There are **not** `2.15`/`2.16`/`2.17`/`2.18` parallel version lanes. There is one next
number. Today's RC becomes the next published version; whatever soaked clean when you
cut gets that label, and anything that lands after the cut becomes the version after.
Two forward numbers cannot coexist on `main` — `config.yaml` holds one string. (The only
exception is a short-lived `release/X.Y` branch to hotfix an *already-published* old
line — never to run two forward lanes.)

**The rule to remember:**

> Plan in themes; release in one rolling number. The number is assigned when the work
> soaks — not when you start it.

## Practical sequencing (soak hygiene)

Mechanically, feature work may merge to `main` at any time — the only *hard* constraint
is "don't bump `main`'s version files to the next number before the current one is
tagged." But there is a real discipline on top of it:

- **Before the pending tag, don't merge a large off-theme PR and then re-cut edge.**
  Doing so mixes that PR into the current version's soak and changelog. The clean order
  is: **tag the current version against a soaked SHA → bump `main` to the next number →
  then merge the big off-theme work** so it soaks *as* the next version's content.
- **Changelog must match the tagged commit.** If the SHA you tag trails `HEAD`, the
  release notes include only what is actually in that SHA — never notes for commits the
  promoted image lacks (`runbooks/ha-addon.md`).

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

The lockstep is **forward-only**: because a stable release promotes an older soaked `:sha`
(the cadence model in `runbooks/ha-addon.md`), a release's published `manifest.json` reflects
that commit. A release tagged from a commit that predates this lockstep ships the
pre-lockstep manifest value — e.g. the first `2.14.x` promoted from an older edge SHA still
reads `1.0.0` in Home Assistant. That is expected and cosmetic; the next release cut after
the lockstep landed carries the synced number.

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
