# AGENTS.md

Repo-wide agent guidance lives in `CLAUDE.md` (leadership principles, project
structure, commands, quality gates) and `docs/agents.md` (repo-local rules).
Read those first. This file adds only the Cursor Cloud environment notes.

## Cursor Cloud specific instructions

This section is for cloud agents running after the startup update script has
already installed dependencies. Standard commands are documented elsewhere —
`README.md`, `CONTRIBUTING.md`, the `Makefile`, and `CLAUDE.md` (Commands
section) — reference those rather than re-deriving them.

### Services

There is a single service: the FastAPI radio app (`mammamiradio.main:app`),
served by uvicorn. There is no separate frontend build, database service, or
queue — SQLite is file-based under `cache/`, and the producer/playback loops run
as in-process asyncio tasks.

- Run dev server: `./start.sh` (uvicorn with `--reload`). `caddy` is optional and
  absent here, so `start.sh` logs a NOTE and falls back to bare uvicorn — that is
  expected, not an error.
- Listener UI `/`, admin control room `/admin`, infinite MP3 `/stream`, health
  `/healthz` + `/readyz`, public JSON `/public-status`, admin JSON `/status`.
- Admin routes are open on loopback (no token needed for `127.0.0.1`), so admin
  endpoints like `POST /api/trigger {"type":"banter"}` work directly in dev.

### Environment caveats (non-obvious)

- **Python is 3.12 here, not 3.11.** The repo targets `>=3.11` and the Conductor
  bootstrap defaults to `python3.11`, which is not installed on this VM. Use
  `python3` (3.12) — `pip install -e .` and the full suite pass on it. A fresh
  venv ships setuptools < the `>=82.0.1` build requirement, so the update script
  upgrades pip/setuptools/wheel before the editable install.

- **yt-dlp YouTube downloads are bot-blocked from this datacenter IP.** Chart
  metadata fetches fine (you'll see "Using live Italian charts (75 tracks)"), but
  each track download fails with "Sign in to confirm you're not a bot" and the
  station marks that source unavailable before it can enter the audio queue. The
  continuity-recovery ladder keeps `/stream` audible; it does not present a
  synthesized silent track as music. Real chart audio needs residential network
  or yt-dlp cookies. Edge TTS (Microsoft) reaches the network fine, so banter
  voice synthesis works (e.g. `it-IT-IsabellaNeural`).

- **Test-isolation flake tied to `.env`.** `mammamiradio/core/config.py` calls
  `load_dotenv()` at import. If `.env` sets `MAMMAMIRADIO_PORT`, it leaks into
  `os.environ` and, under `pytest-randomly` ordering, breaks
  `tests/repo/test_stream_watch_server.py::test_upstream_base_url_uses_runtime_port`
  (which expects `CONDUCTOR_PORT` to win). The dev `.env` here leaves
  `MAMMAMIRADIO_PORT` (and `MAMMAMIRADIO_BIND_HOST`) commented out — they equal
  the built-in defaults (`127.0.0.1:8000`) anyway — so `pytest` is reliably
  green. Don't uncomment them unless you actually need a non-default port.

- **FFmpeg audio tests are deselected by default.** `pyproject.toml` sets
  `addopts = -m 'not requires_ffmpeg'`. FFmpeg is installed here, so run those
  explicitly with `pytest -m requires_ffmpeg` when touching the audio pipeline.

- No API keys are set by default (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `HA_TOKEN` all empty), so the station runs in the "Demo Radio" tier with stock
  banter copy. Add keys to `.env` to unlock AI hosts / home context.
