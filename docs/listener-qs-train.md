# Train/Listener QS

`Train/Listener QS` is the Path B integration train for coupled Listener QS
slices. Independent work uses Path A and goes direct to `main`.

The train is dormant unless the maintainer has created both the
`train/listener-qs` branch and its dedicated Conductor workspace. Use the
maintainer-only activation procedure in
[`docs/runbooks/parallel-workspaces.md`](runbooks/parallel-workspaces.md#activate-a-train-maintainer-only)
before assigning work. Do not open a Path A PR for commits assigned to this
train.

## Contract

- Human train name: `Train/Listener QS`
- Git branch when active: `train/listener-qs`
- Starting point: an immutable `origin/main` SHA recorded at activation
- Owner: the maintainer acting as train integrator
- Purpose: integrate named Listener QS slices in dependency order and hand off
  one train PR for review

The train owner does not define product behavior. A feature workspace owns its
behavior and resolves semantic conflicts. The train owner verifies clean
integration and may handle mechanical conflicts that do not choose behavior.

## Intake

Each feature workspace hands off:

- Branch name, assigned base SHA, and head SHA
- Short objective and user-visible behavior, if any
- Assigned write-set and actual diff against the base SHA
- Validation command and result
- Known conflicts, risks, or follow-up work
- Shared-file manifest for changelog, version, CI, or lifecycle changes
- Any manual verification needed after integration

The assigned base SHA is the train `HEAD` given to the worker before that slice
starts. Verify the slice with:

```bash
git merge-base --is-ancestor <base-sha> HEAD
git diff --name-only <base-sha>...HEAD
```

Do not compare a train-based slice to `origin/main`; that includes earlier
train slices in the worker's write-set. Park incomplete handoffs.

## Integration rules

- Keep the train's recorded starting point tied to `origin/main`.
- Integrate one slice at a time and run its assigned checks before the next.
- Do not commit `.context/` or runtime state.
- Do not invent product behavior during setup or conflict handling.
- Leave release metadata to the train integrator and the release cut.
- Follow the commit contract in `docs/agents.md`. Dependency work uses
  `chore(deps): ...`; `deps:` is not a commit type.
- Use small integration commits grouped by feature slice or mechanical
  conflict class.

Direct pushes and merges into the train do not run `quality.yml` or
`pi-smoke.yml`. Run local slice checks after each intake. The final train PR to
`main` runs both workflows.

## Conflict ownership

- The source workspace resolves product-semantic conflicts against the current
  train SHA, records that SHA as its new base, reruns checks, and submits a new
  handoff.
- The train owner verifies and integrates the clean result. Mechanical conflict
  edits are allowed only when they preserve both sides without a product choice.
- No third workspace resolves a train conflict.

## Merge gate

Before opening or landing the train PR:

- `git status --short --branch` is clean.
- The branch is `train/listener-qs` and still targets `origin/main`.
- No `.context/` files are staged.
- Every integrated slice has a base-aware handoff and passing validation.
- `git diff --check` passes.
- Runtime, route, config, auth, fallback, lifecycle, changelog, and version
  changes have the matching docs or repo-process updates required by
  `CLAUDE.md`.

Run `/ship` once for the train-to-`main` PR. The maintainer lands it through
`scripts/land-pr.sh <PR#>`, then confirms:

```bash
gh pr view <PR#> --json state --jq .state
```

Archive the train only after the command prints `MERGED`. For the next batch,
create a fresh train workspace and branch from the new `origin/main`. Never
reset or force-push a live train.

## Handoff template

```text
Train: Train/Listener QS
Branch: train/listener-qs
Train starting SHA: <origin/main SHA recorded at activation>

Integrated slices:
- <feature branch> base <assigned-base-sha> head <head-sha> - <objective>

Changed files:
- <area>: <files>

Validation:
- <command> - <result>

Conflicts resolved:
- <none | file/area - mechanical resolution>

Behavior shipped:
- <none | summary>

Shared-file changes:
- <none | summary>

Residual risks:
- <none | risk>

Next action:
- Open one train-to-main PR with /ship
```
