# Conductor Workspace

This repo's workspace lifecycle for [Conductor](https://conductor.run) is defined by the committed `scripts/conductor-*.sh` hooks below. Shared repository behavior, including the commit and PR writing contract, lives in the committed `.conductor/settings.toml`; personal machine-only overrides belong in `.conductor/settings.local.toml` and are managed by the Conductor app.

## Scripts

- `scripts/conductor-setup.sh` — bootstraps the workspace venv and dev dependencies. Looks for `~/.config/mammamiradio/.env`, then falls back to `$CONDUCTOR_ROOT_PATH/.env`, and symlinks the first match into the workspace.
- `scripts/conductor-run.sh` — starts the app with workspace-scoped runtime paths under `.context/conductor/` and sets `MAMMAMIRADIO_ALLOW_YTDLP=true` by default.
- `scripts/conductor-archive.sh` — cleans up workspace runtime state when the workspace is archived.

## Commit and PR convention contract

`.conductor/settings.toml` mirrors the repository's current GitHub writing rules
into Conductor's general, create-PR, code-review, and branch-rename prompts.
GitHub remains the enforcement authority; the Conductor prompts are an early
preflight so agents do not need a cleanup turn after generating a commit or PR.

The pinned snapshot is sourced from:

- `.config/commit-rules.json` and its SHA metadata — commit subjects, bodies,
  exemptions, and bypass policy.
- `.github/workflows/commit-lint.yml` — validation of commits and the PR title.
- `scripts/lint-patterns.sh`, `scripts/check-pr-body-lint.sh`, and
  `.github/workflows/pr-body-lint.yml` — PR editorial bans (snapshotted at
  repository commit `469caa61543573064106a93c44b6e11d31c3a489`).
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

## Integration trains

Conductor workspaces may be used as integration trains for parallel feature
worktrees. `Train/Listener QS` is the Listener QS train and should be visible as
branch `train/listener-qs`. Its intake, merge gate, and handoff contract live in
[`docs/listener-qs-train.md`](listener-qs-train.md).

## Shared credentials

The setup script expects your API keys and secrets in a `.env` file at one of two known paths: `~/.config/mammamiradio/.env` (preferred, shared across workspaces) or `$CONDUCTOR_ROOT_PATH/.env` (per-Conductor-root fallback). See `.env.example` for the required keys.
