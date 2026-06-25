# Release trains

How parallel version work is structured on a single trunk. Companion to
`docs/runbooks/ha-addon.md` (the mechanics of cutting a release) and
`docs/conductor.md` (workspace lifecycle).

## The model: single trunk, rolling RC

mammamiradio has **one long-lived branch (`main`)**. There are no `release/*`
branches. A release is a **`vX.Y.Z` tag** cut off `main`, and `main` is **always
exactly one release candidate** — its `pyproject.toml` / `config.yaml` version is
"what we release next", not "where we are". A stable tag that lags `main` by many
commits is correct, not outdated.

So a "version train" is **not a git branch**. It is a **time window on `main`**:
the set of merges that will land in the next tag. You don't run trains in
parallel in git — you run them in sequence, and you choose where the tag line
falls.

```
v2.13.0 (stable tag)        v2.14.x (next tag)         v2.15 (tag after that)
   │                           │                          │
   ●──────── main ───────────► ● ───────── main ───────► ●
 tagged        [2.14.x RC soaking on edge]      [2.15 RC opens here]
                               ▲                          ▲
                  tagging v2.14.x is what OPENS v2.15
```

Until v2.14.x is tagged, everything on `main` is v2.14.x. Tagging it is the act
that opens v2.15.

## The merge-order gate (sequential)

To keep v2.14.x a clean, separable release, **v2.15 work stays off `main` until
v2.14.x is tagged**:

1. v2.15 feature branches base on `origin/main` but **do not merge** while
   `main` is the v2.14.x RC. They mature as their own PRs (CI green, reviewed,
   QA'd) and wait behind the gate.
2. The release-manager worktree tags `v2.14.x` off the soaked edge SHA (see
   `docs/runbooks/ha-addon.md`), then lands the post-tag
   `chore(release): open 2.15.0` PR. `main` is now the v2.15.0 RC.
3. v2.15 branches rebase onto the v2.15.0 RC and merge. They are now v2.15.

**Escape hatch:** a *patch to an already-tagged v2.14.x* after v2.15 work has
merged needs a `release/2.14` branch (pre-flight requires `config.yaml` == tag).
This is the only case that justifies a release branch — see `ha-addon.md`
"Known limitations". Avoid it by tagging v2.14.x before v2.15 merges.

The gate is **convention-enforced, not a CI check** — consistent with the
scope-guard precedent in `CLAUDE.md` (a file-pattern gate can't see merge
intent). Revisit only if it gets missed in practice.

## The shared board: milestones + labels

Conductor workspaces are **independent agents**, not a command hierarchy: a
coordinator session cannot drive another workspace's agent, and worktree-isolated
agents can't write files. The durable, queryable substitute for "report into one
place" is GitHub itself:

- **Milestone per train** — `v2.14.x`, `v2.15`. The milestone *is* the train
  manifest: what's slated, what's merged, what's left.
- **Label per train** — `train:v2.14.x`, `train:v2.15`. Makes PRs filterable:
  `gh pr list --label train:v2.15`.

Every workspace assigns its PR to the right milestone + label when it opens. Any
session and the release-manager worktree read the board to know train state — no
status lives only in a chat.

## Roles

| Role | Worktree | Does |
|------|----------|------|
| **Coordinator** | the train's home workspace (e.g. `bucharest` for v2.15) | curates the milestone, merge order, and QA/soak status; may fan out subagents *in-session* for its own tightly-coupled sub-features (not `isolation: worktree` — Conductor blocks worktree writes) |
| **Release manager** | the shipping worktree (e.g. `jerusalem` / `cut-dev-release`) | the only worktree that cuts tags; tags `v2.14.x` first, then `v2.15`, on explicit signal |
| **Feature workspace** | any | branches off `origin/main`, ships PRs via `/ship`, labels + milestones each PR to its train |

## Operating rhythm

- v2.15 sub-feature tightly coupled to the coordinator's branch → subagent
  fan-out in-session.
- v2.15 feature that's independent → its own workspace off `origin/main`,
  labeled `train:v2.15`, held behind the gate.
- Routine / version-agnostic work (dependency bumps, CI) → rides the current RC
  (v2.14.x) normally; no need to hold it behind the gate.
