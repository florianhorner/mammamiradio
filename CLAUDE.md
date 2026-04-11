# mammamiradio

AI-powered Italian radio station engine. Python 3.11+, FastAPI, FFmpeg, optional Home Assistant integration.

## Docs

- `README.md` - product overview and operator quick start
- `ARCHITECTURE.md` - runtime flow, queue model, and audio pipeline
- `CONTRIBUTING.md` - local setup, tests, and smoke checks
- `TROUBLESHOOTING.md` - common failures and recovery paths
- `HA_ADDON_RUNBOOK.md` - addon release process, config contract, pre-merge checklist
- `OPERATIONS.md` - runtime assumptions and deploy reality
- `CHANGELOG.md` - release notes

## Commands

- Setup: `python3.11 -m venv .venv && source .venv/bin/activate && pip install -e .`
- Install: `pip install -e .`
- Run full local stack: `./start.sh`
- Run app only: `source .venv/bin/activate && python -m uvicorn mammamiradio.main:app --reload --reload-dir mammamiradio`
- Test: `pytest tests/` or `make test` (with coverage)
- Test watch: `make test-watch` (re-runs on file save)
- Test HA add-on build locally: `scripts/test-addon-local.sh`
- Lint: `ruff check .` (fix: `ruff check --fix .`)
- Format: `ruff format .` (check: `ruff format --check .`)
- Type check: `mypy mammamiradio/ tests/`
- All checks: `make check` (lint + typecheck + coverage gate with per-module floors)
- Pre-commit: `pip install pre-commit && pre-commit install --hook-type pre-commit --hook-type pre-push --hook-type commit-msg`
- **Validate addon before push**: `./scripts/validate-addon.sh` (add `--build` for Docker build test)

## Docker / Home Assistant

- `Dockerfile`: standalone container image with Python 3.11 + FFmpeg
- `docker-compose.yml`: one-command run for non-HA users
- `.dockerignore`: keeps builds clean
- `ha-addon/`: Home Assistant add-on scaffold
  - `ha-addon/mammamiradio/config.yaml`: add-on metadata, options schema, ingress config
  - `ha-addon/mammamiradio/Dockerfile`: HA add-on image (Alpine-based)
  - `ha-addon/mammamiradio/rootfs/run.sh`: entrypoint mapping Supervisor env vars
  - `ha-addon/mammamiradio/translations/en.yaml`: UI labels for add-on options
- `.github/workflows/docker.yml`: multi-arch Docker build CI

## Environment

- `MAMMAMIRADIO_BIND_HOST`, `MAMMAMIRADIO_PORT`: bind address and port
- `MAMMAMIRADIO_CACHE_DIR`, `MAMMAMIRADIO_TMP_DIR`: override cache/tmp directories (for Docker volumes)
- `LOG_LEVEL`: override log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`; default `INFO`)
- `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ADMIN_TOKEN`: admin auth
- `ANTHROPIC_API_KEY`: Claude banter/ad generation
- `OPENAI_API_KEY`: OpenAI gpt-4o-mini-tts voice synthesis + script generation fallback when Anthropic is unavailable
- `HA_TOKEN`: Home Assistant API token
- `HA_URL`: Home Assistant API base URL (auto-set by HA add-on to `http://supervisor/core/api`)
- `HA_ENABLED`: force-enable HA integration (`true`/`1`/`yes`)
- `STATION_NAME`, `STATION_THEME`: override station identity from `radio.toml`
- `CLAUDE_MODEL`: override Claude model from `radio.toml`
- `MAMMAMIRADIO_ALLOW_YTDLP`: enable yt-dlp for chart music (`true`/`1`/`yes`; default: disabled for copyright safety, but enabled by default in HA addon and Conductor)

## Runtime behavior

- Startup loads `radio.toml`, validates config, restores persisted source selection from `cache/playlist_source.json`, fetches the playlist, then launches producer and playback tasks.
- **Capability flags** (`anthropic`, `ha`) drive a three-tier system. The dashboard derives a tier label from them: Demo Radio, Full AI Radio, Connected Home. `GET /api/capabilities` returns flags, tier, and a `next_step` hint guiding the user toward the next setup action.
- Demo-first: the app boots immediately with charts or built-in demo tracks and pre-bundled banter clips. No wizard, no gates.
- If no LLM key is configured (neither Anthropic nor OpenAI), banter uses pre-bundled clips from `demo_assets/banter/` instead of calling an API.
- Music comes from live Italian charts (via yt-dlp), local `music/` files, or placeholder tones.
- If Anthropic fails mid-session, script generation falls back to OpenAI `gpt-4o-mini` when `OPENAI_API_KEY` is set, then to short stock copy.
- If Home Assistant is enabled and `HA_TOKEN` is present, banter and ads may reference current home state.
- `audio.bitrate` is the single source of truth for encoding, ICY headers, and playback throttling.
- Source switching via `/api/playlist/load` purges the queue, skips the current segment, and begins playback from the new source immediately.
- Non-local binds require `ADMIN_PASSWORD` or `ADMIN_TOKEN`.

## Project structure

```text
mammamiradio/
  main.py             FastAPI app startup/shutdown lifecycle
  config.py           radio.toml + .env parsing, validation, runtime-json helper
  models.py           shared data models and station state
  producer.py         async segment production loop
  streamer.py         playback loop, routes, auth checks, public/admin status
  scheduler.py        segment scheduling and upcoming preview
  scriptwriter.py     Anthropic/OpenAI API calls for banter and ad JSON (with automatic fallback)
  playlist.py         charts, local, and demo playlist loading
  downloader.py       local file, yt-dlp, and placeholder audio fallback
  normalizer.py       FFmpeg helpers for normalize, mix, concat, and generated SFX
  tts.py              Edge TTS synthesis for hosts and ads
  ha_context.py       Home Assistant polling and Italian state formatting
  capabilities.py     Capability flags (anthropic, ha), tier derivation, and next_step hints
  persona.py          Compounding listener memory: persona, motifs, session tracking, prompt injection filtering
  sync.py             SQLite database initialization
  context_cues.py     Time-of-day and cultural context for banter/ad prompts
  track_rationale.py  "Why this track?" rationale generation for listener UI
  setup_status.py     Legacy setup status classification (kept for /status endpoint compat)
  dashboard.html      Listener-facing dashboard served at /
  admin.html          Admin control room panel served at /admin
  listener.html       Listener HTML (legacy, redirects to /)
  demo_assets/        Pre-bundled banter clips, ads, music, and jingles for demo-first boot
radio.toml            station config
start.sh              dev entrypoint with uvicorn and reload
tests/                pytest coverage
```

## Design System

Always read `DESIGN.md` before making any visual or UI decisions.
All font choices, colors, spacing, and aesthetic direction are defined there.
Do not deviate without explicit user approval. In QA mode, flag any code that doesn't match `DESIGN.md`.

## Brand assets

- **Logo SVG**: `mammamiradio/logo.svg` — canonical vector source (variant G: classic radio with Italian flag stripe and sound waves)
- **Palette**: Volare Refined — espresso dark with Italian warmth in accents. See `DESIGN.md` for the full design system.
  - Background: espresso dark (`#14110F`) with subtle warm gradient at top
  - Cards: warm brown surfaces (`#251E19`) — unified across listener and admin
  - Accent: golden sun (`#F4D048`, `#ECCC30`) — play button, active borders
  - Interactive: Lancia red (`#B82C20`) — FM dial needle
  - Text: cream (`#F5EDD8`)
  - Success/connected: blue (`#2563EB`) — never green (colorblind)
- **Typography**: Playfair Display italic (station name, display text) + Outfit (body) + JetBrains Mono (technical)
- **Favicon**: inline SVG data URI in `dashboard.html` and `listener.html` (simplified version of logo)
- **HA add-on icon**: `ha-addon/mammamiradio/icon.png` (256px) and `logo.png` (512px), rasterized from the SVG
- To regenerate PNGs from SVG: `cairosvg mammamiradio/logo.svg -o icon.png -W 256 -H 256`
- **Full design system**: `DESIGN.md` — colors, typography, components, motion, anti-patterns

## Notes for future edits

- `dashboard.html` and `listener.html` are loaded as static file contents by `streamer.py`.
- `start.sh` is part of the runtime contract, not just a convenience script.
- `radio.toml` is the source of truth for hosts, pacing, ad brands, audio settings, and Home Assistant enablement. Secrets stay in `.env`.
- If you change routes, config keys, auth rules, or fallback behavior, update the matching docs in the same change.
- `conductor.json` and `scripts/conductor-*.sh` define Conductor workspace setup/run/archive behavior. Commit those files, but keep `.context/` runtime state out of git.
- If the user has a live stream running, do not stop, restart, or reload it unless they explicitly ask. Protect the illusion first.
- Treat 60 minutes of uninterrupted runtime per live station object as the default minimum when tinkering around an active stream.
- Built-in demo music should favor current modern tracks, not nostalgic or older fallback selections.
- Advertisements need a convincing underlying sound bed. Prefer CC-free music or sound design beds under ad voiceovers instead of dry voice-only spots.

## Quality gates

- **Coverage ratchet (automatic)**: Coverage can only go up, never down. Two layers enforce this:
  - **Aggregate floor**: `fail_under` in `pyproject.toml` — the overall minimum.
  - **Per-module floors**: `.coverage-floors.json` — every module has its own floor. A module-level regression fails CI even if the aggregate stays above threshold.
- **CI enforcement**: `.github/workflows/quality.yml` runs `scripts/coverage-ratchet.py`:
  - On PRs: `check` mode — fails if any module dropped below its floor.
  - On main merge: `update` mode — auto-ratchets all floors up and commits the new values. Zero human intervention.
- **Local check**: `make coverage-check` to verify locally. `make coverage-ratchet` to preview what CI would commit.
- **Adding tests**: Write tests, push. CI will auto-raise the floors on merge. The next PR that drops any module will fail.

## Review discipline

For every bug fix or behavior change, do not stop at the first broken instance.

- Identify the user-visible promise or system invariant that failed.
- Check sibling code paths for the same failure mode before concluding the fix is done.
- Add or update at least one automated guard (test, validation, or build check) that would fail if the invariant breaks again.
- If duplicated state exists, explain what keeps it synchronized. If you cannot name the synchronization boundary, treat that as a design risk and either remove the duplication or add a guard around it.

Review question to apply before merge:

`What invariant failed here, where else could it fail the same way, and what automated check will catch the next instance before a user does?`

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
