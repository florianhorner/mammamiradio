# Parallel workspaces: admission, ship path, and landing

Use this runbook to operate several Conductor workspaces with one human-owned
integration path. Worktrees isolate coding. GitHub PRs and the maintainer
integrate it.

Related:

- Human and feature landing gate: `scripts/land-pr.sh`
- Queue dashboard: `scripts/pr-queue-status.sh` (advisory, not a gate)
- Workspace lifecycle: [`docs/conductor.md`](../conductor.md)
- Listener QS train (Path B only): [`docs/listener-qs-train.md`](../listener-qs-train.md)
- Agent rules: [`docs/agents.md`](../agents.md)

Dependabot patch and minor updates use their own guarded auto-merge workflow.
See [Dependabot](#dependabot).

## Do this now (session 0)

Use Conductor and the landing scripts already in the repo.

1. From any clean checkout of this repo:

   ```bash
   git fetch origin
   bash scripts/pr-queue-status.sh
   git worktree list
   ```

2. In Conductor, count every active workspace for this repo, including an
   active train. Exclude archived workspaces. The hard cap is three active
   workspaces. If you have more, stop creating work and apply the table below
   until three remain.

3. Give each open workspace one state and action:

   | State | Action now |
   |-------|------------|
   | PR open, recommendation `land now` | Run `scripts/land-pr.sh <PR#>`. Archive only after GitHub reports `MERGED` |
   | PR open, `wait/checks` or `update + test` | Leave it. Do not start a sibling workspace to help |
   | PR open, `draft` | Return it to the owning workspace until the handoff is complete |
   | PR open, `conflict/manual` | Follow [Conflict ownership](#conflict-ownership); do not spawn a fixer workspace |
   | PR open, `commit dirty work` | Return to that workspace and reconcile its tracked changes before landing |
   | PR open, `inspect/no local worktree` or `inspect (...)` | Inspect the PR and branch before creating any replacement workspace |
   | PR merged, workspace still open | Confirm GitHub state `MERGED`, then archive |
   | No PR, work is independent and almost done | Finish, run `/ship`, and use Path A |
   | No PR, work needs an active Listener QS train | Use Path B only after the maintainer activates the train |
   | No PR, stale or overlapping work | Archive or reject it. Do not rescue it with another agent |
   | Dependabot PR | Do not count it as a workspace; follow the Dependabot batch rules |

4. Do not create an orchestrator workspace, a `safe-merge.sh`, persistent
   `.agents/eng-*` slots, or a board under `.context/`. Use
   `pr-queue-status.sh` and the Conductor workspace list as the dashboard.

5. Create the next workspace only when the total active workspace count is
   below three. Assign its path and write-set first. See
   [Admission](#admission-wip-cap).

## Roles

| Role | Who | Allowed | Forbidden |
|------|-----|---------|-----------|
| Planner | Maintainer, optional | Split the task, assign the write-set and base SHA, choose Path A or B, create an admitted workspace | Writing product code or merging during planning |
| Worker | One agent per feature workspace | Implement inside the assigned write-set and fill the handoff | Merging, spawning workspaces, or opening extra PRs to fix conflicts |
| Integrator | Maintainer | Choose merge order, integrate one slice, run `land-pr.sh`, confirm merge state, archive | Starting new feature work during the integration pass |

Agents do not create workspaces or train branches unless the current user
message assigns that role.

## Admission (WIP cap)

- The hard limit is three active workspaces for this repo. A fourth waits.
- The issue backlog does not change the cap.
- An active Listener QS train consumes one slot, leaving at most two feature
  workspaces until the train is archived.

Workspace lifecycle:

```text
active -> ready for handoff -> merged -> archived
```

`scripts/land-pr.sh` arms auto-merge and can return before GitHub merges the PR.
Before archiving a Path A workspace or the completed train, run:

```bash
gh pr view <PR#> --json state --jq .state
```

Archive only when the command prints `MERGED`.

The committed `.conductor/settings.toml` routes archive events to
`scripts/conductor-archive.sh`. Repository-local `settings.local.toml` values
have higher precedence. Remove copied lifecycle hooks from that local file
after the shared settings reach `main`, or the local copy will continue to run.

## Shipping paths

Pick one path per unit of work.

### Path A: direct to `main`

Use Path A for independent fixes, features, docs, and CI or lifecycle work with
an exclusive write-set.

```text
workspace -> branch -> /ship PR to main -> soak
          -> scripts/land-pr.sh <PR#> -> confirm MERGED -> archive
```

### Path B: temporary train for coupled slices

Use Path B only when several slices share types, APIs, config, or one behavior
that must land together. The current train name is `train/listener-qs`.

Path B is dormant unless the maintainer has created both the branch and its
dedicated Conductor workspace. Do not assign Path B work against a missing
train.

#### Activate a train (maintainer only)

1. Fetch `origin` in a clean, dedicated Conductor workspace created from the
   latest `origin/main`.
2. Create `train/listener-qs` at that exact commit and push the branch to
   `origin`.
3. Confirm the workspace is clean, its branch is `train/listener-qs`, and its
   `HEAD` equals the recorded train starting SHA.
4. Confirm the remote ref exists:

   ```bash
   git ls-remote --exit-code --heads origin refs/heads/train/listener-qs
   ```

5. Announce the train as active before assigning a Path B slice. Agents do not
   activate or retire a train without explicit authority in the current user
   message.

The train flow is:

```text
feature workspace -> clean merge into train/<name> -> local slice checks
                  -> repeat one slice at a time
                  -> one /ship PR from train to main
                  -> scripts/land-pr.sh -> confirm MERGED
                  -> archive train and recreate it from current origin/main next time
```

Direct pushes and merges into `train/**` do not trigger `quality.yml` or
`pi-smoke.yml`. Those workflows run for the final PR to `main`; they also run
for a PR whose base is `train/**`, although this runbook does not require
per-slice PRs. Run the assigned local checks after every train intake.

Feature slices targeting this train follow
[`docs/listener-qs-train.md`](../listener-qs-train.md). Do not open a Path A PR
and keep the same commits on a train.

## Shared-file ownership

Release metadata stays with the release cut or train integrator:

- `pyproject.toml`
- `CHANGELOG.md`
- `ha-addon/mammamiradio/CHANGELOG.md`
- `ha-addon/mammamiradio/config.yaml` when changing the version
- `custom_components/mammamiradio/manifest.json` when changing the version

Feature slices do not bump versions. See `docs/release-process.md`.

The following files require one exclusive owner because they affect every
workspace:

- `.github/workflows/**`
- `scripts/conductor-*.sh`
- `scripts/land-pr.sh`, `scripts/pr-queue-status.sh`, `scripts/check-merge-gate.sh`
- `.conductor/settings.toml`

For Path A, a dedicated workspace may own these files when its objective and
assigned write-set name them. No other active workspace may overlap that
write-set. For Path B, feature workers leave a manifest note and the train
integrator applies or rejects the shared-file change.

## Write-set

Assign paths per task. Do not maintain a standing directory ownership map.

At assignment, record an immutable base SHA:

- Path A: the `origin/main` commit from which the workspace starts.
- Path B: the train `HEAD` assigned to that slice.

At handoff, verify that base and list only the slice's changes:

```bash
git merge-base --is-ancestor <base-sha> HEAD
git diff --name-only <base-sha>...HEAD
```

Reject extra paths outside the write-set. A Path B shared-file change also
requires a manifest note. Return the slice to its existing workspace; do not
create a fixer workspace.

Do not admit overlapping write-sets in two active feature workspaces.

## Handoff

The worker fills this template. The integrator rejects incomplete handoffs.

```text
Path: A (PR to main) | B (train/listener-qs)
Workspace: <Conductor name>
Branch: <name>
Base SHA (assigned, immutable): <full>
Head SHA: <full>
Objective: <one line>

Write-set (assigned):
- <glob or path>

Diff vs assigned base (actual):
- <paste git diff --name-only <base-sha>...HEAD>

Shared-file manifest:
- none | changelog bullet: ... | dependency: ... | lifecycle/config: ...

Validation:
- <command> - <result>

Conflicts / residual risk:
- none | ...

Next action:
- Path A: PR #<n> ready for land-pr.sh | still soaking
- Path B: cleanly merge this head into train/listener-qs
```

## Integrator loop

Run at the start of the session and after each confirmed merge:

```bash
bash scripts/pr-queue-status.sh
```

Then:

1. Pick one ready Path A PR or one train intake.
2. For Path A, run `scripts/land-pr.sh <PR#>`. Confirm GitHub state `MERGED`
   before archiving the workspace.
3. For Path B, merge one feature branch into `train/listener-qs`. Run the
   slice's checks. When the batch is coherent, run `/ship` for the train PR,
   land it with `scripts/land-pr.sh`, and confirm state `MERGED`.
4. Archive a source feature workspace after the train owner confirms its head
   is in the train and its validation passed.
5. Re-run `pr-queue-status.sh`.

Integrate foundation changes before consumers when a dependency requires that
order.

## Conflict ownership

- Path A: the source workspace resolves conflicts with `main`, reruns its
  checks, and refreshes its review evidence.
- Path B: the train owner stops the intake and returns semantic conflicts to
  the source workspace with the current train SHA. The worker resolves against
  that SHA, records it as the new base, reruns checks, and submits a new
  handoff. The train owner verifies and integrates the clean result.

Do not resolve either path from a third workspace.

## Dependabot

Dependabot is the automated exception to the human and feature landing path.
Patch and minor Python PRs may use
`.github/workflows/dependabot-automerge.yml`. Batch handling follows
`docs/agents.md`, including nudge/recreate steps and manual major-Action
landings through `scripts/land-pr.sh`. Do not attach Dependabot branches to
Conductor feature slots.

## Explicitly out of scope

- A second human merge script that rebases and pushes `main`
- GitHub merge queue, until ready-to-merge PRs become the bottleneck
- An agent that judges integration conflicts
- Persistent worktree slots under `.agents/` beside Conductor workspaces
- A `.context/workspaces.md` source-of-truth board

`quality.yml` has no `merge_group` trigger. Adding one belongs in a separate CI
change.

## Weekly rhythm

| When | Action |
|------|--------|
| Start of session | Run `pr-queue-status.sh`, archive confirmed merges, count feature slots |
| New task | Admit only below the cap; record write-set, base SHA, and shipping path |
| Feature done | Handoff to the maintainer; archive after the path-specific merge condition |
| Train PR merged | Confirm `MERGED`, archive the train, recreate a fresh train from current `origin/main` for the next batch |
