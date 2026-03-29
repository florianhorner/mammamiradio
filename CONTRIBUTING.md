# Contributing

This project is small enough that the fastest way to break it is to change something without running the station. Do the local setup, run targeted tests, then do a quick listen-through.

## Prerequisites

- Python 3.11+
- FFmpeg on your `PATH`
- go-librespot if you want to test real Spotify playback
- Spotify and Anthropic credentials if you want the full happy path

You can still work on config, scheduler, and most UI/API behavior without Spotify credentials. The app falls back to demo tracks when Spotify is not configured.

## Local setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Then edit `.env` as needed:

- `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` for Spotify playlist access and playback transfer
- `ANTHROPIC_API_KEY` for banter and ad script generation
- `ADMIN_PASSWORD` or `ADMIN_TOKEN` if you plan to bind outside localhost

`radio.toml` is the main station config. That is where you change hosts, pacing, playlist source, ad brands, and audio settings.

## Run the app

Full dev workflow:

```bash
./start.sh
```

That script:

- creates the FIFO if needed
- starts or reuses go-librespot
- keeps a fallback FIFO reader alive across reloads
- runs uvicorn with `--reload`

If you only need the web app and background tasks:

```bash
source .venv/bin/activate
python -m uvicorn fakeitaliradio.main:app --reload --reload-dir fakeitaliradio
```

## Tests

Fast tests with no audio generation:

```bash
pytest tests/test_config.py tests/test_scheduler.py
```

Full suite:

```bash
pytest tests/
```

Notes:

- `tests/test_ads.py` exercises audio helpers and needs FFmpeg installed.

## Lint, format, and type check

```bash
ruff check .          # lint
ruff check --fix .    # lint + auto-fix
ruff format .         # format
ruff format --check . # format check (CI mode)
mypy fakeitaliradio/ tests/  # type check
```

To install pre-commit hooks locally:

```bash
pip install pre-commit
pre-commit install
```

## Manual smoke test

After starting the app:

1. Open `http://127.0.0.1:8000/` and confirm the dashboard loads.
2. Open `http://127.0.0.1:8000/listen` and confirm the listener page loads.
3. Open `http://127.0.0.1:8000/stream` in a browser or player and confirm audio starts once the first segment is queued.
4. Hit `/public-status` and confirm the upcoming list matches the current playlist order.
5. Use the dashboard controls for skip, shuffle, purge, and playlist reorder.

If you are testing the Spotify path, also open Spotify and select the `fakeitaliradio` device. If you are binding to `0.0.0.0`, set `ADMIN_PASSWORD` or `ADMIN_TOKEN` first or config validation will reject startup.

## Docs and release notes

When behavior changes, update the matching docs in the same change:

- `README.md` for user-facing setup or product behavior
- `ARCHITECTURE.md` for runtime model or component boundary changes
- `CLAUDE.md` for command and file-map updates
- `CHANGELOG.md` for release notes

This repo does not currently have a standalone `VERSION` file. The current version source of truth is `pyproject.toml`.
