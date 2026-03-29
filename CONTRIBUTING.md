# Contributing

This repo is small, but the moving parts are real: FastAPI, FFmpeg, Edge TTS, Spotify, Claude, and optional Home Assistant. Keep changes factual, testable, and boring in the good sense.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Minimum useful `.env`:

```dotenv
FAKEITALIRADIO_BIND_HOST=127.0.0.1
FAKEITALIRADIO_PORT=8000
```

Optional integrations:

- Spotify: `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`
- Claude: `ANTHROPIC_API_KEY`
- Home Assistant: `HA_TOKEN`
- Remote admin access: `ADMIN_PASSWORD` or `ADMIN_TOKEN`

## Running locally

Full local stack:

```bash
./start.sh
```

App only:

```bash
uvicorn fakeitaliradio.main:app --reload --reload-dir fakeitaliradio
```

Useful URLs:

- `http://localhost:8000/`
- `http://localhost:8000/listen`
- `http://localhost:8000/stream`
- `http://localhost:8000/public-status`
- `http://localhost:8000/status`

## Tests

Run the suite with:

```bash
pytest tests/
```

Current coverage is focused on:

- config validation
- scheduler behavior
- ad generation helpers and models
- playlist fetching fallbacks
- preview output

If you change scheduling, route auth, or config validation, add or update tests in `tests/`.

## Implementation notes

- `radio.toml` is checked in and holds non-secret station behavior.
- `.env` holds secrets and bind/auth config.
- `tmp/` and `cache/` are runtime output, not source.
- `dashboard.html` and `listener.html` are raw HTML templates loaded by `streamer.py`.
- `start.sh` is part of the dev workflow, not just a convenience script. It preserves the FIFO/go-librespot setup across reloads.

## Documentation expectations

When behavior changes, update:

- `README.md` for user-facing setup and route changes
- `ARCHITECTURE.md` for runtime flow and system design changes
- `CLAUDE.md` for the codebase map used by coding agents
- `TROUBLESHOOTING.md` for failure modes users will actually hit
- `OPERATIONS.md` for runtime and deployment assumptions
- `CHANGELOG.md` for shipped behavior worth calling out

If you add a new config key, env var, route, auth rule, or fallback path and do not document it, the docs are wrong. Fix them in the same change.
