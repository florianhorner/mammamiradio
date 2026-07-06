# Repo-Local Agent Rules

This file supplements the global instructions for the `mammamiradio` repository.

## Repo Profile

- Stack: Python, FastAPI, Docker, Bash lifecycle scripts, `pyproject.toml` versioning
- Product: `mammamiradio`, an AI-powered Italian radio station with a Home Assistant add-on

## Working Rules

- Conventional commits only: `feat:`, `fix:`, `chore:`, `ci:`, `deps:`
- Never modify `.context/` runtime state
- If Conductor lifecycle hooks change, update the `scripts/conductor-*.sh` files (and your Conductor `.conductor/settings.toml`) in the same change
- On version bumps, keep `CHANGELOG.md` and `ha-addon/mammamiradio/CHANGELOG.md` in sync
- In engineering reviews, when presenting multiple options, explain the tradeoffs without framing one as the choice the user should automatically take

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

- `Train/Listener QS` lives on `train/listener-qs` and uses `origin/main` as
  its base. Feature worktrees that target this train must hand off through
  `docs/listener-qs-train.md`.
