# Conductor Workspace

This repo ships a `conductor.json` that defines workspace lifecycle for [Conductor](https://conductor.run).

## Scripts

- `scripts/conductor-setup.sh` — bootstraps the workspace venv and dev dependencies. Looks for a shared `.env` file in a location of your choice, or falls back to `$CONDUCTOR_ROOT_PATH/.env`, and symlinks it into the workspace.
- `scripts/conductor-run.sh` — starts the app with workspace-scoped runtime paths under `.context/conductor/` and sets `MAMMAMIRADIO_ALLOW_YTDLP=true` by default.
- `scripts/conductor-archive.sh` — cleans up workspace runtime state when the workspace is archived.

## Runtime state

Runtime artifacts created by these scripts land under `.context/` which is gitignored. Do not commit anything from `.context/`.

## Shared credentials

The setup script expects your API keys and secrets in a `.env` file. Create one at a path of your choice (e.g. `~/.config/mammamiradio/.env`) and point the setup script at it, or drop a `.env` at the workspace root. See `.env.example` for the required keys.
