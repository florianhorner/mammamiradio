# Repo-Local Agent Rules

This file supplements the global instructions for the `mammamiradio` repository.

## Repo Profile

- Stack: Python, FastAPI, Docker, Bash lifecycle scripts, `pyproject.toml` versioning
- Product: `mammamiradio`, an AI-powered Italian radio station with a Home Assistant add-on

## Working Rules

- Conventional commits only: use `type(scope): subject` with the canonical
  types `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `ci`,
  `build`, `perf`, and `revert`.
- Dependency commits use `chore(deps): ...`; `deps:` is not a commit type.
- Never modify `.context/` runtime state
- First Listen and other Home Assistant-facing development or QA use the
  disposable local HA lab by default. Treat live Home Assistant as production:
  never connect branch code to it, reuse its token or backup, reconfigure it,
  or attach its household/cloud/MQTT devices without explicit authorization in
  the current message. Keep lab state and credentials under gitignored
  `tmp/first-listen-ha-lab/`, never `.context/` or tracked files.
- Before opening any PR (ANY runtime — Claude, Codex, Cursor): run the pre-ship
  review squad, then `scripts/emit-review-evidence.sh`, and commit
  `proof/preship-review.json`. CI (`preship-evidence.yml`) verifies it against the
  PR head, report-only. The Claude-side hook cannot fire in other runtimes; the
  committed artifact is what makes the squad auditable everywhere.
- If Conductor lifecycle hooks change, update the `scripts/conductor-*.sh` files (and your Conductor `.conductor/settings.toml`) in the same change
- On version bumps, keep `CHANGELOG.md` and `ha-addon/mammamiradio/CHANGELOG.md` in sync
- In engineering reviews, present real alternatives and their trade-offs, then
  recommend one and explain why it is superior for this repository.

## Parallel workspaces

Admission, Path A vs Path B, write-sets, and the "do this now" triage live in
[`docs/runbooks/parallel-workspaces.md`](runbooks/parallel-workspaces.md).
Hard rules agents must not invent around:

- Max 3 active Conductor workspaces for this repo, including an active train.
  Do not create a fourth to "help" merge, fix conflicts, or orchestrate the
  others.
- Do not merge. The maintainer lands with `scripts/land-pr.sh`.
- Path B workers do not edit shared files listed in that runbook; put a
  manifest note in the handoff. A dedicated Path A workspace may own those
  files only when its objective and exclusive write-set name them.
- Do not spawn workspaces, trains, or extra PRs unless the current message
  assigned that role.

## PR Landing Queue

- For multi-PR or coordinator landing sessions, run `scripts/pr-queue-status.sh`
  at the start and again after each confirmed merge so the remaining queue is
  based on current GitHub/worktree state rather than chat memory.
- The single landing conductor owns the full output and merge order. Individual
  PR agents may include the relevant script output as their readiness receipt
  before handing off.
- For ordinary single-PR work, `scripts/pr-queue-status.sh` is optional and
  advisory only. It must not be treated as a merge gate; `scripts/land-pr.sh`
  remains the only required landing path.

## Dependabot Batches

- Start with `gh pr list` state, current head SHAs/checks, and a clean tracked
  worktree. After every Dependabot merge, expect the rest of the batch to become
  stale and rerun `bash scripts/nudge-dependabot-rebase.sh` instead of manually
  rebasing bot branches.
- Let pure patch/minor Python Dependabot PRs with auto-merge armed land through
  Dependabot after fresh required checks pass. If quality fails on an unrelated
  one-test timeout, verify the focused test locally before treating it as a
  rerunnable flake; stop on any deterministic dependency break.
- Treat semver-major GitHub Actions PRs as manual landings: inspect the fresh
  rebased diff, confirm required checks are green, include HA integration checks
  when workflow changes touch the Home Assistant surface, write review-log
  coverage for the exact head, then run `scripts/land-pr.sh <pr>`.
- If Dependabot says it cannot rebase a PR because the branch was edited, or a
  dependency PR becomes conflict-dirty after another dependency merge, use
  `@dependabot recreate` and re-review the recreated head. If a
  `github-actions` nudge is rejected because the actor lacks push access, post
  the `@dependabot rebase` comment from the authenticated user account.

## Integration Trains

- Default ship path, WIP cap, and write-sets:
  [`docs/runbooks/parallel-workspaces.md`](runbooks/parallel-workspaces.md).
- When active, `Train/Listener QS` uses `train/listener-qs` from a recorded
  `origin/main` SHA. Path B stays dormant until the maintainer creates that
  branch and its dedicated Conductor workspace. Feature worktrees targeting
  the active train hand off through `docs/listener-qs-train.md`.
