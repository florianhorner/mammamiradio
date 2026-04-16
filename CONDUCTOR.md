# Conductor Workspace

This repo ships a `conductor.json` that defines workspace lifecycle for [Conductor](https://conductor.run).

## Scripts

- `scripts/conductor-setup.sh` — bootstraps the workspace venv and dev dependencies. Looks for `~/.config/mammamiradio/.env`, then falls back to `$CONDUCTOR_ROOT_PATH/.env`, and symlinks the first match into the workspace.
- `scripts/conductor-run.sh` — starts the app with workspace-scoped runtime paths under `.context/conductor/` and sets `MAMMAMIRADIO_ALLOW_YTDLP=true` by default.
- `scripts/conductor-archive.sh` — cleans up workspace runtime state when the workspace is archived.

## Runtime state

Runtime artifacts created by these scripts land under `.context/` which is gitignored. Do not commit anything from `.context/`.

## Shared credentials

The setup script expects your API keys and secrets in a `.env` file at one of two known paths: `~/.config/mammamiradio/.env` (preferred, shared across workspaces) or `$CONDUCTOR_ROOT_PATH/.env` (per-Conductor-root fallback). See `.env.example` for the required keys.
