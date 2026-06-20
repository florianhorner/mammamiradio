# Train/Listener QS

`Train/Listener QS` is the integration train for Listener QS work. It is not an
implementation slice.

## Contract

- Human train name: `Train/Listener QS`
- Git branch: `train/listener-qs`
- Base branch: `origin/main`
- Owner role: train owner
- Purpose: receive Listener QS feature worktrees, integrate them in order,
  resolve conflicts, and hand off one coherent train state for review.

The train owner does not invent product behavior while integrating. Feature
behavior enters the train only through an explicit feature worktree handoff.

## Intake

Each feature worktree must hand off:

- Branch name and commit SHA
- Short objective and user-visible behavior, if any
- Changed files grouped by area
- Validation run and result
- Known conflicts, risks, or follow-up work
- Whether changelog, version, docs, or runtime hooks changed
- Any manual verification needed after integration

Incomplete handoffs can be parked until the missing information is supplied.
The train should stay reviewable before it absorbs another slice.

## Mapped Feature Worktrees

| Worktree | Branch | Current SHA | Status |
|----------|--------|-------------|--------|
| `havana` | `florianhorner/feat/festival-party-mode` | `194a27e24d2a40c7fcdfc3ba102a37487274c845` | Mapped to `Train/Listener QS`; no unique commits ahead of `origin/main` at mapping time. |

## Integration Rules

- Keep `train/listener-qs` based on `origin/main`.
- Do not commit `.context/` or runtime state.
- Do not change product/runtime behavior as part of train setup or conflict
  resolution unless the behavior came from a named feature worktree.
- Keep changelogs and version files unchanged unless a feature slice ships
  user-visible behavior or performs an intentional version bump.
- If Conductor lifecycle hooks change in an integrated slice, update the
  `scripts/conductor-*.sh` files in the same integration commit.
- Preserve conventional commits. Use `feat:`, `fix:`, `chore:`, `ci:`, or
  `deps:` only.
- Prefer small integration commits grouped by feature slice or conflict class.

## Merge Gate

Before handing off or merging the train:

- `git status --short --branch` is clean except for intentional staged changes.
- The branch is still `train/listener-qs`.
- The branch still targets `origin/main`.
- No `.context/` files are staged.
- Relevant tests/checks from the integrated slices have passed.
- `git diff --check` passes.
- Any runtime, route, config, auth, fallback, lifecycle, changelog, or version
  changes have matching docs or repo-process updates required by `CLAUDE.md`.

## Handoff Template

```text
Train: Train/Listener QS
Branch: train/listener-qs
Base: origin/main

Integrated slices:
- <feature branch> @ <sha> - <one-line objective>

Changed files:
- <area>: <files>

Validation:
- <command> - <result>

Conflicts resolved:
- <file or area> - <resolution>

Behavior shipped:
- <none | summary>

Changelog/version changes:
- <none | summary>

Residual risks:
- <none | risk>

Next action:
- Feature worktrees should branch/rebase against train/listener-qs and hand off
  using this template.
```
