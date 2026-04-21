# Contributing

This repo is small, but it has real moving parts: FastAPI, FFmpeg, Edge TTS, Claude, and optional Home Assistant. The fastest way to break it is to change behavior without actually running the station.

Do the local setup, run targeted tests, then do a quick listen-through.

## Prerequisites

- Python 3.11+
- FFmpeg on your `PATH`
- Optional: Anthropic and/or OpenAI credentials for the full AI radio experience

Music source fallback chain: when `MAMMAMIRADIO_ALLOW_YTDLP=true` the app blends live Italian charts with anything in `music/`; with yt-dlp disabled it plays local `music/` only; if neither is available it falls through to silence. No external service credentials are required to run the station.

## Local setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

If you use Conductor, see [CONDUCTOR.md](CONDUCTOR.md) for workspace lifecycle details.

Then fill in whatever `.env` values you need:

- `ANTHROPIC_API_KEY` for banter and ad script generation (falls back to OpenAI if unavailable)
- `OPENAI_API_KEY` for TTS voices and as a script generation fallback
- `HA_TOKEN` for Home Assistant prompt context
- `ADMIN_PASSWORD` or `ADMIN_TOKEN` if you plan to bind outside localhost

`radio.toml` is the main station config. That is where you change hosts, pacing, ad brands, audio settings, and Home Assistant enablement.

## Run the app

Full dev workflow:

```bash
./start.sh
```

That script runs uvicorn with `--reload`.

Or use Docker (no Python/FFmpeg setup needed):

```bash
docker compose up
```

If you only need the web app and background tasks:

```bash
source .venv/bin/activate
python -m uvicorn mammamiradio.main:app --reload --reload-dir mammamiradio
```

Useful URLs:

- `http://127.0.0.1:8000/` — listener page for public callers; flips to the admin control room when the request carries a trusted HA ingress header
- `http://127.0.0.1:8000/listen` — explicit listener alias (always the public UI)
- `http://127.0.0.1:8000/admin` — admin control room (guarded by `require_admin_access`: loopback, private network including HA Supervisor ingress, admin token, or basic auth)
- `http://127.0.0.1:8000/stream` — infinite MP3 stream
- `http://127.0.0.1:8000/public-status` — public JSON
- `http://127.0.0.1:8000/status` — admin JSON

## Tests

Fast tests:

```bash
pytest tests/test_config.py tests/test_scheduler.py
```

Full suite:

```bash
pytest tests/
```

Notes:

- `tests/test_ads.py` and `tests/test_normalizer_real_ffmpeg.py` exercise audio helpers and need FFmpeg installed. The real-ffmpeg tests skip automatically when FFmpeg is absent; the pi-smoke CI job (`ubuntu-24.04-arm`) runs them on ARM hardware to catch aarch64-specific crashes.
- Home Assistant add-on changes must also pass the local add-on build check:

```bash
scripts/test-addon-local.sh
```

That command stages the add-on build context exactly like CI, then runs a local container build. If it fails locally, do not commit or push.

## Lint, format, and type check

```bash
ruff check .          # lint
ruff check --fix .    # lint + auto-fix
ruff format .         # format
ruff format --check . # format check (CI mode)
mypy mammamiradio/ tests/  # type check
```

To install pre-commit hooks locally:

```bash
pip install pre-commit
pre-commit install --hook-type pre-commit --hook-type pre-push
```

The repo wires `scripts/test-addon-local.sh` into both `pre-commit` and `pre-push` for files that can break the Home Assistant add-on build. Docker Desktop or Podman must be installed or those hooks will fail.

## Manual smoke test

After starting the app:

1. Open `http://127.0.0.1:8000/` and confirm the listener page loads.
2. Open `http://127.0.0.1:8000/admin` (with admin auth if non-loopback) and confirm the control room loads.
3. Open `http://127.0.0.1:8000/stream` in a browser or player and confirm audio starts once the first segment is queued.
4. Hit `/public-status` and confirm the upcoming list reflects the real queued segments, or returns `upcoming_mode="building"` while the producer is warming up.
5. Use the dashboard controls for skip, shuffle, purge, and playlist reorder.
6. Restart the app and verify the last selected source restores automatically.

If you are binding to `0.0.0.0`, set `ADMIN_PASSWORD` or `ADMIN_TOKEN` first or config validation will reject startup. Non-loopback admin requests with basic auth also require CSRF validation (the dashboard handles this automatically via injected tokens).

## Documentation expectations

When behavior changes, update the matching docs in the same change:

- `README.md` for user-facing setup and route changes
- `ARCHITECTURE.md` for runtime flow and system design changes
- `CLAUDE.md` for the codebase map used by coding agents
- `TROUBLESHOOTING.md` for failure modes users will actually hit
- `OPERATIONS.md` for runtime and deployment assumptions
- `CHANGELOG.md` for shipped behavior worth calling out

If you add a new config key, env var, route, auth rule, or fallback path and do not document it, the docs are wrong. Fix them in the same change.
