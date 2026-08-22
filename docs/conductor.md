# Conductor Workspace

This repo's workspace lifecycle for [Conductor](https://conductor.build) is
defined by the committed `scripts/conductor-*.sh` hooks below. The committed
`.conductor/settings.toml` wires those scripts and carries the shared workspace
role and GitHub writing contracts. Conductor reads shared settings from the
default branch on the remote, so lifecycle changes take effect for new local
workspaces after merge. Cloud workspaces read the settings from their creation
branch.

Machine-only overrides belong in `.conductor/settings.local.toml`. That file
has higher precedence than the shared settings. Do not copy setup, run, or
archive hooks or shared prompt keys into it; copied values mask later fixes to
the committed configuration. Keep existing local copies until the shared
settings reach the default branch, then remove only the duplicated keys after
verifying the shared values are active.

## Scripts

- `scripts/conductor-setup.sh` — bootstraps the workspace venv and dev dependencies. Looks for `~/.config/mammamiradio/.env`, then falls back to `$CONDUCTOR_ROOT_PATH/.env`, and symlinks the first match into the workspace.
- `scripts/conductor-run.sh` — starts the app with workspace-scoped runtime paths under `.context/conductor/` and keeps `MAMMAMIRADIO_ALLOW_YTDLP=false`. External extraction is a deliberate standalone opt-in that also requires the optional `external-media` package extra; the default Conductor run uses local-or-starter music.
- `scripts/conductor-archive.sh` — cleans up workspace runtime state when the workspace is archived. The shared archive hook invokes this file.

## Workspace and writing contracts

The shared general prompt assigns the seat first. Names containing `lander` or
`integration-manager` are landing conductors; every other workspace defaults to
a feature worker. It then applies the Path A/Path B, immutable base SHA,
write-set, shared-file ownership, single-writer, and escalation rules from the
parallel-workspace runbook.

`.conductor/settings.toml` also mirrors the repository's current GitHub writing
rules into Conductor's general, create-PR, code-review, and branch-rename
prompts. GitHub remains the enforcement authority; the Conductor prompts are an
early preflight so agents do not need a cleanup turn after generating a commit
or PR.

The pinned snapshot is sourced from:

- `.config/commit-rules.json` and its SHA metadata — commit subjects, bodies,
  exemptions, and bypass policy.
- `.github/workflows/commit-lint.yml` — validation of commits and the PR title.
- `scripts/lint-patterns.sh`, `scripts/check-pr-body-lint.sh`,
  `scripts/check-issue-body-lint.sh`, `.github/workflows/pr-body-lint.yml`,
  and `.github/workflows/issue-body-lint.yml` — PR and issue editorial bans
  (snapshotted at repository commit
  `94507d4cff9bd6d3f21dc6cdff38416857baa779`).
- `.github/pull_request_template.md` — Summary, Test plan, and conditional
  Admin Panel Standards sections.
- `.github/workflows/verify-claims.yml` — terminal `## Proof` validation.

Refresh the prompt snapshot deliberately in the same change whenever one of
these sources or its pinned external version changes. Update the source comment
and prompt text, parse the TOML, run the existing commit/PR-body checks against
fixtures, and verify a Conductor dry-run before merging. Do not add network
fetches to workspace setup, and do not treat the prompts as a replacement for
local hooks or GitHub Actions.

## Runtime state

Runtime artifacts created by these scripts land under `.context/` which is gitignored. Do not commit anything from `.context/`.

## Parallel workspaces and integration trains

Default ship path is a feature workspace, a PR to `main`,
`scripts/land-pr.sh`, confirmation that GitHub reports `MERGED`, and then
archive. Admission, write-sets, and Path A vs Path B are in
[`docs/runbooks/parallel-workspaces.md`](runbooks/parallel-workspaces.md).

A train workspace is Path B only: tightly coupled slices that must land
together. `Train/Listener QS` uses branch `train/listener-qs` when the
maintainer activates it. Until that branch and its dedicated Conductor
workspace exist, Path B is dormant. The activation check, intake, merge gate,
and handoff live in [`docs/listener-qs-train.md`](listener-qs-train.md). Do not
use a train and a direct-to-`main` PR for the same work.

## Shared credentials

The setup script expects your API keys and secrets in a `.env` file at one of two known paths: `~/.config/mammamiradio/.env` (preferred, shared across workspaces) or `$CONDUCTOR_ROOT_PATH/.env` (per-Conductor-root fallback). See `.env.example` for the required keys.
