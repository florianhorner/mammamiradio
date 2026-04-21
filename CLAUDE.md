# mammamiradio

AI-powered Italian radio station engine. Python 3.11+, FastAPI, FFmpeg, optional Home Assistant integration.

## Leadership Principles

Every proposal — architecture, feature, fix, deployment plan — must pass both of these in order:

**1. NEVER BREAK THE ILLUSION**
The listener must always believe they are hearing a real radio station. Dead air, abrupt cuts, silence gaps, or anything that exposes the machine behind the curtain is a product failure. If a change risks breaking the illusion for a live listener, it needs a mitigation before shipping.

**2. INSTANT AUDIO**
A listener who connects must hear sound within 1–2 seconds, every time. No exceptions for cold starts, session resumes, idle wakeups, or addon restarts. Every connect path needs an immediate audio source — pre-normalized track, canned clip, anything. Build the bridge first, fix root causes second.

## Production Systems Discipline — HARD STOP

**Principle: No live surgery on the HA Green.** The only legitimate code-change path for the mammamiradio addon is:

`branch → PR → merge → CI builds image → addon updates to new image`

The restart happens once, planned, when the addon updates. Not during the day. Not as an experiment. Not to "test the fix live."

**NEVER do any of these against the running mammamiradio addon without Florian's explicit confirmation in the current message:**

1. **No live code patching.** `docker cp` into the addon container, `docker exec` with write operations (`sh -c "cat > ..."`, `tee`, `echo >`, `sed -i`, any redirection into a file), editing files inside a running container by any other means. These changes are wiped on the next restart and mask the real state of production.
2. **No process signals.** `pkill`, `kill`, `killall`, `docker kill`, `docker restart` targeting any process inside the addon. s6-rc in this container does NOT auto-restart killed services reliably — killing a process kills the container, and killing the container kills the stream.
3. **No addon restarts.** `ha apps restart`, `ha apps stop`, `docker restart addon_*_mammamiradio`, `supervisor restart`. Ask before any of these. Exception: recovery from an already-broken state (container exited, stream already dead) — restoring is correct there, but still announce it first.
4. **No live config edits.** Writing to `/config/` from a shell, editing `radio.toml` on the Pi, modifying addon options via CLI without going through the HA addon config UI.
5. **No ad-hoc Docker image changes.** Building, tagging, or pushing addon images from the Pi. Images come from CI only.
6. **No volume mounts introduced for debugging.** Mounting host paths into the container to sidestep the rebuild chain.

**Legitimate operations (no confirmation needed):**
- `docker ps`, `docker logs`, `docker exec <container> <read-only command>` (cat, ls, grep, env, ps)
- `ha apps info`, `ha apps list`, `ha apps logs`
- `ssh root@100.98.177.107 <read-only inspection>`
- `ha apps start <slug>` when the addon is already stopped/exited (recovery, not experiment)

**Why this rule exists:** On 2026-04-20, an agent live-patched via `docker cp`, the patches got wiped on addon restart, then the agent killed uvicorn assuming s6 would restart it in place. Container exited. Stream dropped. Leadership principle #1 was violated. The PR with the real fix (#213) was already open — the live patches were theater. This rule closes the loophole between "don't restart HA core" and "don't experiment on the running addon."

**When a fix is urgent:** the fastest legitimate path is usually merge → wait ~5-10min for CI → update addon. This is faster than the live-surgery path turned out to be, and it leaves a permanent fix in place instead of an ephemeral one. The legitimate path also preserves the illusion — one planned restart beats three unplanned drops.

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
- **Release cooldown self-test**: `bash tests/workflows/test_cooldown_gate.sh` (9 scenarios, runs in `quality.yml` on every PR)

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
- `MIN_COOLDOWN_HOURS`: override the release-cooldown window (default `24`, read by `scripts/check-release-cooldown.sh`)

## Runtime behavior

- Startup loads `radio.toml`, validates config, purges suspect cache files (< 10KB), restores persisted source selection from `cache/playlist_source.json`, fetches the playlist, initializes the clip ring buffer, then launches producer and playback tasks. Logs a one-line boot summary at the end.
- **Capability flags** (`anthropic`, `ha`) drive a three-tier system. The dashboard derives a tier label from them: Demo Radio, Full AI Radio, Connected Home. `GET /api/capabilities` returns flags, tier, and a `next_step` hint guiding the user toward the next setup action.
- Demo-first: the app boots immediately with whatever music source is available (yt-dlp charts, local `music/`, or bundled demo assets under `mammamiradio/demo_assets/music/`). The playback loop rescues from the norm cache, then bundled demo assets, then forces a banter segment after 60s of silence — silence is never the terminal state. No wizard, no gates.
- If no LLM key is configured (neither Anthropic nor OpenAI), banter falls back to stock copy. `mammamiradio/demo_assets/banter/` is currently empty — the bundled-clip inventory is a TODO; until it is populated, missing-LLM banter is text-to-speech over stock copy rather than pre-recorded clips.
- Music comes from live Italian charts (via yt-dlp), local `music/` files, or bundled demo assets under `mammamiradio/demo_assets/music/`. Queue starvation triggers a norm-cache rescue, then a demo-asset rescue, then forced banter — silence is never the terminal fallback.
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
  normalizer.py       FFmpeg helpers for normalize, mix, concat, generated SFX, studio bleed, and oneshot mixing
  tts.py              Edge TTS synthesis for hosts and ads (with +90% rate for pharma disclaimers)
  clip.py             WTF clip extraction from ring buffer, save, and cleanup
  ha_context.py       Home Assistant polling, Italian state formatting, mood classification, reactive triggers
  ha_enrichment.py    Pure HA event derivation (diff_states, event pruning, numeric passthrough)
  capabilities.py     Capability flags (anthropic, ha), tier derivation, and next_step hints
  persona.py          Compounding listener memory: persona, motifs, session tracking, arc phases, prompt injection filtering
  song_cues.py        Per-track machine-derived memory: anthems, skip bits, LLM reactions
  sync.py             SQLite database initialization and schema migration
  context_cues.py     Time-of-day and cultural context for banter/ad prompts
  track_rationale.py  "Why this track?" rationale generation for listener UI
  track_rules.py      Per-track personality rules flagged via /api/track-rules
  audio_quality.py    Audio quality gate: duration and silence checks before segments reach the queue
  setup_status.py     Legacy setup status classification (kept for /status endpoint compat)
  admin.html          Admin control room panel served at /admin (and at / over HA ingress)
  listener.html       Listener page served at / and /listen
  demo_assets/        Demo asset tree: sfx/studio/ SFX are committed; banter/ads/music/jingles/welcome are empty placeholders pending the demo-asset contract
radio.toml            station config
start.sh              dev entrypoint with uvicorn and reload
tests/                pytest coverage
```

## Design System

Always read `DESIGN.md` before making any visual or UI decisions.
All font choices, colors, spacing, and aesthetic direction are defined there.
Do not deviate without explicit user approval. In QA mode, flag any code that doesn't match `DESIGN.md`.

## Brand assets

- **Hero banner**: `docs/banner.png` — 1280×640 README hero. DALL-E background composited with Playfair italic typography. Source template: `docs/hero-composite.html` (contains regeneration instructions in comment header). The background image (`radio-hero-bg.png`) is generated via ChatGPT Images and not committed to git.
- **Logo SVG**: `mammamiradio/logo.svg` — canonical vector source (variant G: classic radio with Italian flag stripe and sound waves)
- **Palette**: Volare Refined — espresso dark with Italian warmth in accents. See `DESIGN.md` for the full design system.
  - Background: espresso dark (`#14110F`) with subtle warm gradient at top
  - Cards: warm brown surfaces (`#251E19`) — unified across listener and admin
  - Accent: golden sun (`#F4D048`, `#ECCC30`) — play button, active borders
  - Interactive: Lancia red (`#B82C20`) — FM dial needle
  - Text: cream (`#F5EDD8`)
  - Success/connected: blue (`#2563EB`) — never green (colorblind)
- **Typography**: Playfair Display italic (station name, display text) + Outfit (body) + JetBrains Mono (technical)
- **Favicon**: inline SVG data URI in `admin.html` and `listener.html` (simplified version of logo)
- **HA add-on icon**: `ha-addon/mammamiradio/icon.png` (256px) and `logo.png` (512px), rasterized from the SVG
- To regenerate PNGs from SVG: `cairosvg mammamiradio/logo.svg -o icon.png -W 256 -H 256`
- **Full design system**: `DESIGN.md` — colors, typography, components, motion, anti-patterns

## Brand safety — hard rule

**All ad brands in `radio.toml` must be fictional.** Never add a real company, product, or registered trademark to `[[ads.brands]]`. This applies to names, taglines, slogans, and campaign copy.

Why: the scriptwriter generates fake ads in the brand's voice, makes false product claims, and can produce satirical or defamatory content. Doing this with real brands creates trademark infringement and false advertising exposure — including pharma brands where false claims carry regulatory risk.

**The test:** google the brand name. If it returns a real company, it does not belong in `radio.toml`. Invented names that sound Italian and absurd are correct. Names that are one letter off a real brand (e.g. "Barella" for Barilla) also fail — the intent to deceive is implicit in the similarity.

## Notes for future edits

- `dashboard.html` and `listener.html` are loaded as static file contents by `streamer.py`.
- `start.sh` is part of the runtime contract, not just a convenience script.
- `radio.toml` is the source of truth for hosts, pacing, ad brands, audio settings, and Home Assistant enablement. Secrets stay in `.env`.
- If you change routes, config keys, auth rules, or fallback behavior, update the matching docs in the same change. (See **Doc sync** rule below.)
- `conductor.json` and `scripts/conductor-*.sh` define Conductor workspace setup/run/archive behavior. Commit those files, but keep `.context/` runtime state out of git.
- If the user has a live stream running, do not stop, restart, or reload it unless they explicitly ask. Protect the illusion first.
- Treat 60 minutes of uninterrupted runtime per live station object as the default minimum when tinkering around an active stream.
- Built-in demo music should favor current modern tracks, not nostalgic or older fallback selections.
- Advertisements need a convincing underlying sound bed. Prefer CC-free music or sound design beds under ad voiceovers instead of dry voice-only spots.
- To tune scriptwriter behavior without a stream gap: edit `mammamiradio/scriptwriter.py`, run `make check`, then `POST /api/hot-reload` (admin auth, empty body), then `POST /api/trigger {"type": "banter"}` to generate a segment with the new code. The stream stays live throughout. If reload fails (syntax error), the endpoint returns 500 and the stream keeps running with the old code.

## Quality gates

- **Pre-merge QA (mandatory)**: Every PR must pass two separate `/qa` runs before merge:
  1. **Player QA** (`/qa` on `/` dashboard) — listener-facing: stream playback, now-playing, up-next, Casa card, song requests, clip sharing, responsive layout.
  2. **Admin QA** (`/qa` on `/admin`) — operator-facing: controls (skip/stop/resume/shuffle), pacing sliders, host config, key management, engine room, playlist management.
  Splitting QA into two focused runs maximizes findings per surface. A single combined run tends to rush through one side. Both must pass before merging.
- **Coverage ratchet (automatic)**: Coverage can only go up, never down. Two layers enforce this:
  - **Aggregate floor**: `fail_under` in `pyproject.toml` — the overall minimum.
  - **Per-module floors**: `.coverage-floors.json` — every module has its own floor. A module-level regression fails CI even if the aggregate stays above threshold.
- **CI enforcement**: `.github/workflows/quality.yml` runs `scripts/coverage-ratchet.py`:
  - On PRs: `check` mode — fails if any module dropped below its floor.
  - On main merge: `update` mode — auto-ratchets all floors up and commits the new values. Zero human intervention.
- **Local check**: `make coverage-check` to verify locally. `make coverage-ratchet` to preview what CI would commit.
- **Adding tests**: Write tests, push. CI will auto-raise the floors on merge. The next PR that drops any module will fail.
- **Release cooldown gate**: `.github/workflows/release-cooldown.yml` blocks any `v*` tag push if the prior published release is <24h old. Bypass by adding the `hotfix` label to the PR that introduced the tagged commit. Self-test: `bash tests/workflows/test_cooldown_gate.sh` (9 cases; also runs in `quality.yml` on every PR). See `HA_ADDON_RUNBOOK.md` and `STABILIZATION_LOG.md` for the measurement plan.

## Protected UI elements

These UI elements have regressed in past refactors. Always verify they survive after any HTML edit:

- **Token cost counter** (`admin.html` Engine Room) — backend computes `api_cost_estimate_usd` on every `/status` call. UI must display it. Has disappeared twice in refactors.
- **Play button blue state** (`mammamiradio/static/base.css`) — `.play-btn.playing` must use `var(--ok)` (blue), never `var(--sun2)` (golden). Colorblind safety.
- **Station name localStorage** (`mammamiradio/static/listener.js`) — reads `stationName` from localStorage. Admin writes it. Broken when dashboard.html was rewritten.
- **Gold "Mi" accent** (`listener.html`, `admin.html`) — `<span class="mi">` in h1, styled `color: var(--sun)`. Brand signature from hero banner.
- **Italian tricolor stripe** (`admin.html` uses `.tricolor-stripe`; `listener.html` uses `.tricolor-band`) — present below h1. Must match hero banner.

When editing any HTML file, grep for these elements before committing.

## Doc sync

**Any change to a route, config key, env var, auth rule, or fallback path must update at least one of the following docs in the same commit:**

`README.md`, `ARCHITECTURE.md`, `TROUBLESHOOTING.md`, `OPERATIONS.md`, `CLAUDE.md`, `CHANGELOG.md`

If the behavior changed and the docs didn't, the docs are wrong. Fix them in the same change, not a follow-up.

## Review discipline

For every bug fix or behavior change, do not stop at the first broken instance.

- Identify the user-visible promise or system invariant that failed.
- Check sibling code paths for the same failure mode before concluding the fix is done.
- Add or update at least one automated guard (test, validation, or build check) that would fail if the invariant breaks again.
- If duplicated state exists, explain what keeps it synchronized. If you cannot name the synchronization boundary, treat that as a design risk and either remove the duplication or add a guard around it.

Review question to apply before merge:

`What invariant failed here, where else could it fail the same way, and what automated check will catch the next instance before a user does?`

## Audio delivery test coverage rule

Every PR touching audio delivery (producer, streamer, normalizer, any bridge/fallback path) must include tests for all three scenarios. Missing any one = untested blast radius, do not ship.

**Scenario 1 — Normal:** feature works as designed.

**Scenario 2 — Empty fallback:** canned clips absent, norm cache empty, no assets in container. The real container ships only README stubs in `demo_assets/banter/`. Tests that mock `_pick_canned_clip` to return a real file are hiding this class of bug.

**Scenario 3 — Post-restart:** flag files persisted from a prior run, `session_stopped` still set, HA watchdog has restarted. Test that a listener connecting AFTER a restart + stopped state still gets audio.

This rule was added after 4 production silence incidents caused by untested Scenarios 2 and 3. The test suite was proving features worked; it was not proving the product worked under real conditions (Pi hardware, container filesystem, HA watchdog restarts).

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
