# mammamiradio

AI-powered Italian radio station engine. Python 3.11+, FastAPI, FFmpeg, optional Home Assistant integration.

## Leadership Principles

Every proposal — architecture, feature, fix, deployment plan — must pass all of these in order:

**1. NEVER BREAK THE ILLUSION**
The listener must always believe they are hearing a real radio station. Dead air, abrupt cuts, silence gaps, or anything that exposes the machine behind the curtain is a product failure. If a change risks breaking the illusion for a live listener, it needs a mitigation before shipping.

**2. INSTANT AUDIO**
A listener who connects must hear sound within 1–2 seconds, every time. No exceptions for cold starts, session resumes, idle wakeups, or addon restarts. Every connect path needs an immediate audio source — pre-normalized track, canned clip, anything. Build the bridge first, fix root causes second.

**3. NO LIVE SURGERY ON PRODUCTION SYSTEMS**
The only legitimate code-change path for the addon is `branch → PR → merge → CI builds image → addon updates`. No `docker cp`, no `pkill`, no live config edits, no addon restarts without explicit confirmation in the current message. Sleeping humans depend on the system. See **Production Systems Discipline — HARD STOP** below for the full rule.

**4. THE README IS THE PITCH**
A new reader must get it in 30 seconds or less. That is a KPI, not an aspiration. If the README needs scrolling, paragraphs of context, or a glossary before the product clicks, we failed. The first viewport carries the entire pitch: what it is, what makes it different, and what the reader does next. Same standard applies to the repo at large — when a new contributor opens the source tree, the folder hierarchy IS the mental model. If they can't find where a feature lives in 30 seconds, the structure failed.

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

Sacred files at the repo root (one viewport, one job each):

- `README.md` - product pitch and operator quick start
- `CONTRIBUTING.md` - local setup, tests, and smoke checks
- `CLAUDE.md` - agent rules and leadership principles (this file)
- `CHANGELOG.md` - release notes

Everything else lives under `docs/`:

- `docs/architecture.md` - runtime flow, queue model, and audio pipeline
- `docs/operations.md` - runtime assumptions and deploy reality
- `docs/troubleshooting.md` - common failures and recovery paths
- `docs/runbooks/ha-addon.md` - addon release process, config contract, pre-merge checklist
- `docs/runbooks/refactor-cuts.md` - god-module split: per-cut pre-flight checklist and lessons
- `docs/design/system.md` - Volare design system: colors, typography, components, motion
- `docs/design/admin-panel.md` - admin control-room layout, info architecture, motion rules
- `docs/conductor.md` - Conductor workspace lifecycle and `.env` discovery
- `docs/agents.md` - agent-specific notes and integration points
- `docs/listener-qs-train.md` - `Train/Listener QS` intake, merge gate, and handoff contract
- `docs/stabilization-log.md` - weekly fix-hours and emergency-patch counts (release cooldown gate)

There is no canonical tracked TODO file. Do not create or revive
`TODO.md`, `TODOS.md`, `docs/todos.md`, `docs/backlog.md`, or similar
catch-all backlog files. Use GitHub issues for public engineering work and a
private durable system for strategy or relationship context.

## Commands

- Setup: `python3.11 -m venv .venv && source .venv/bin/activate && pip install -e .`
- Install: `pip install -e .`
- Run full local stack: `./start.sh`
- Run app only: `source .venv/bin/activate && python -m uvicorn mammamiradio.main:app --reload --reload-dir mammamiradio`
- Test: `pytest tests/` or `make test` (with coverage)
- Test watch: `make test-watch` (re-runs on file save)
- Test HA add-on build locally: `scripts/validate-addon.sh --build`
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
  - `ha-addon/mammamiradio-edge/`: dev-release channel add-on (metadata only — pulls the same image as stable; version is the `main` short-SHA, cut manually with `make edge-release`). See `docs/runbooks/ha-addon.md` → "Edge channel".
- `.github/workflows/docker.yml`: multi-arch Docker build CI

## Environment

- `MAMMAMIRADIO_BIND_HOST`, `MAMMAMIRADIO_PORT`: bind address and port
- `MAMMAMIRADIO_CACHE_DIR`, `MAMMAMIRADIO_TMP_DIR`: override cache/tmp directories (for Docker volumes)
- `LOG_LEVEL`: override log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`; default `INFO`)
- `MAMMAMIRADIO_HTTP_LOG_LEVEL`: log level applied to `httpx` and `httpcore` (default `WARNING`). Successful request logs from those libraries are suppressed at default; raise to `INFO` or `DEBUG` to inspect outbound HTTP traffic. Invalid values fall back to `WARNING`.
- `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ADMIN_TOKEN`: admin auth
- `ANTHROPIC_API_KEY`: Claude banter/ad generation
- `OPENAI_API_KEY`: OpenAI gpt-4o-mini-tts voice synthesis + script generation fallback when Anthropic is unavailable
- `HA_TOKEN`: Home Assistant API token
- `HA_URL`: Home Assistant API base URL (auto-set by HA add-on to `http://supervisor/core/api`)
- `HA_ENABLED`: force-enable HA integration (`true`/`1`/`yes`)
- `STATION_NAME`, `STATION_THEME`: override station identity from `radio.toml`
- `CLAUDE_MODEL`: override Claude model from `radio.toml`
- `OPENAI_SCRIPT_MODEL`: override the OpenAI model used for script-generation fallback (banter/ads/news/transitions) when Anthropic is unavailable. Default `gpt-4o-mini`. Evaluation toggle only — does NOT affect TTS (`gpt-4o-mini-tts` stays fixed). Promotion to addon UI is a separate decision once an evaluation winner is chosen.
- `MAMMAMIRADIO_ALLOW_YTDLP`: enable yt-dlp for chart music (`true`/`1`/`yes`; default: disabled for copyright safety, but enabled by default in HA addon and Conductor)
- `JAMENDO_CLIENT_ID`: Jamendo API client id (empty = Jamendo source disabled)
- `JAMENDO_COUNTRY`: 3-letter uppercase ISO 3166-1 alpha-3 (e.g. `ITA`, `DEU`); empty disables the country filter. radio.toml default is `ITA` for Italian-trending music.
- `JAMENDO_ORDER`: Jamendo sort order (`popularity_week` | `popularity_month` | `popularity_total` | `releasedate_desc` | empty). radio.toml default is `popularity_week`.
- `JAMENDO_LIMIT`: Jamendo API result depth, integer `1`-`200`. radio.toml default is `200` to reduce short-rotation repeats.
- `MIN_COOLDOWN_HOURS`: override the release-cooldown window (default `24`, read by `scripts/check-release-cooldown.sh`)
- `MAMMAMIRADIO_SUPER_ITALIAN`: station personality dial (`true`/`1`/`yes` to enable; default on). When ON, listener UI and hosts are Italian-first. When OFF, listener UI uses English utility copy with Italian headlines and station-feel words, and AI hosts code-switch (English narrative + Italian flavor). Operator-toggleable from admin Engine Room (hot-reloadable; persisted to `.env` in standalone mode and `/data/options.json` in HA addon mode).
- `MAMMAMIRADIO_FESTIVAL_MODE`: enable Festival Mode (`true`/`1`/`yes`; default off). Hosts become theatrical music competition MCs — fictional Italian-regional delegations, dramatic scoring, drinking game triggers. Toggleable live from the admin panel without a restart; persisted to `.env` in standalone mode and `/data/options.json` in HA addon mode.
- `MAMMAMIRADIO_LEDGER_ENABLED`: enable the provenance ledger / Show Memory (`true`/`1`/`yes`; default **off**). When on, a best-effort daemon thread records how each aired moment was made — the raw LLM attempts (Tier 1), the final spoken script (Tier 2), and the true aired outcome (Tier 3) — as daily-rotated JSONL under `cache_dir/ledger` (dir `0700`, files `0600`). Off by default because the rows include home + listener context written locally in plaintext. Never raises into the audio path; a saturated queue drops the oldest row and surfaces a `ledger_heartbeat`. The ledger directory is always derived from `MAMMAMIRADIO_CACHE_DIR`, so when set it inherits the addon (`/data/cache`) vs standalone (`./cache`) resolution. **Env/standalone-only by design:** because the rows include home + listener context in plaintext, this toggle is intentionally NOT exposed in the HA addon options UI (`config.yaml`/`run.sh`) the way `MAMMAMIRADIO_FESTIVAL_MODE` and `MAMMAMIRADIO_SUPER_ITALIAN` are — an addon operator cannot enable it from the UI; it is set via env only.
- `MAMMAMIRADIO_LEDGER_RETENTION_DAYS`: days of provenance history to keep before a day-rollover gzips and prunes older files (positive integer; default `14`). The deque cap (`ledger_queue_max`, default `2000` rows) is config-only in `StationConfig` with no env override.

## Runtime behavior

- Startup loads `radio.toml`, validates config, purges suspect cache files (< 10KB), restores persisted source selection from `cache/playlist_source.json`, fetches the playlist, initializes the clip ring buffer, then launches producer and playback tasks. Logs a one-line boot summary at the end.
- **Capability flags** (`anthropic`, `ha`) drive a three-tier system. The dashboard derives a tier label from them: Demo Radio, Full AI Radio, Connected Home. `GET /api/capabilities` returns flags, tier, and a `next_step` hint guiding the user toward the next setup action.
- Demo-first: the app boots immediately with whatever music source is available (yt-dlp charts, local `music/`, or bundled demo assets under `mammamiradio/assets/demo/music/`). The playback loop rescues from the norm cache, then bundled demo assets, then forces a banter segment after 60s of silence — silence is never the terminal state. No wizard, no gates.
- If no LLM key is configured (neither Anthropic nor OpenAI), banter falls back to stock copy. `mammamiradio/assets/demo/banter/` is currently empty — the bundled-clip inventory is a TODO; until it is populated, missing-LLM banter is text-to-speech over stock copy rather than pre-recorded clips.
- Music comes from live Italian charts (via yt-dlp), local `music/` files, or bundled demo assets under `mammamiradio/assets/demo/music/`. Queue starvation triggers a norm-cache rescue, then a demo-asset rescue, then forced banter — silence is never the terminal fallback.
- If Anthropic fails mid-session, script generation falls back to OpenAI `gpt-4o-mini` when `OPENAI_API_KEY` is set, then to short stock copy.
- If Home Assistant is enabled and `HA_TOKEN` is present, banter and ads may reference current home state.
- `audio.bitrate` is the single source of truth for encoding, ICY headers, and playback throttling.
- Source switching via `/api/playlist/load` purges the queue, skips the current segment, and begins playback from the new source immediately.
- Non-local binds require `ADMIN_PASSWORD` or `ADMIN_TOKEN`.

## Project structure

The folder hierarchy IS the mental model (leadership principle #4). For a single-page "where does X live" map see `docs/REPO_MAP.md`.

```text
mammamiradio/
  main.py                   FastAPI app startup/shutdown lifecycle (kept at top — public entry)
  core/                     config, models, capabilities, setup_status, sync (SQLite schema)
  audio/                    normalizer (FFmpeg), audio_quality gate, tts, voice_catalog
  playlist/                 playlist source selection, downloader, song_cues, track_rationale, track_rules
  hosts/                    scriptwriter (LLM banter+ads — TODO: split), persona, context_cues, ad_creative
  home/                     ha_context (HA polling, mood), ha_enrichment (event diff/prune)
  scheduling/               producer (async loop), scheduler (segment-type picker), clip (WTF ring buffer)
  web/                      streamer (TODO: split — routes/auth/playback loop), listener_requests, og_card, templates/, static/
  assets/                   demo/ MP3s + SFX, logo.svg
radio.toml                  station config
start.sh                    dev entrypoint with uvicorn and reload
tests/                      mirrors mammamiradio/ — tests/<nave>/test_*.py
```

Two god modules carry a `# TODO: split` marker: `web/streamer.py` (~2,300 LOC) and `hosts/scriptwriter.py` (~1,500 LOC). They have postal addresses now; the actual splits land in PRs 5 and 6 of the cathedral plan (`docs/archive/2026-04-28-cathedral-restructure.md`).

## Design System

Always read `docs/design/system.md` before making any visual or UI decisions.
All font choices, colors, spacing, and aesthetic direction are defined there.
Do not deviate without explicit user approval. In QA mode, flag any code that doesn't match `docs/design/system.md`.

## Brand assets

- **Hero banner**: `docs/banner.png` — 1280×640 README hero. DALL-E background composited with Playfair italic typography. Source template: `docs/hero-composite.html` (contains regeneration instructions in comment header). The background image (`radio-hero-bg.png`) is generated via ChatGPT Images and not committed to git.
- **Logo SVG**: `mammamiradio/assets/logo.svg` — canonical vector source (variant G: classic radio with Italian flag stripe and sound waves)
- **Palette**: Volare Refined — espresso dark with Italian warmth in accents. See `docs/design/system.md` for the full design system.
  - Background: espresso dark (`#14110F`) with subtle warm gradient at top
  - Cards: warm brown surfaces (`#251E19`) — unified across listener and admin
  - Accent: golden sun (`#F4D048`, `#ECCC30`) — play button, active borders
  - Interactive: Lancia red (`#B82C20`) — FM dial needle
  - Text: cream (`#F5EDD8`)
  - Success/connected: blue (`#2563EB`) — never green (colorblind)
- **Typography**: Playfair Display italic (station name, display text) + Outfit (body) + JetBrains Mono (technical)
- **Favicon**: inline SVG data URI in `admin.html` and `listener.html` (simplified version of logo)
- **HA add-on icon**: `ha-addon/mammamiradio/icon.png` (256px) and `logo.png` (512px), rasterized from the SVG
- To regenerate PNGs from SVG: `cairosvg mammamiradio/assets/logo.svg -o ha-addon/mammamiradio/icon.png -W 256 -H 256 && cairosvg mammamiradio/assets/logo.svg -o ha-addon/mammamiradio/logo.png -W 512 -H 512`
- **Full design system**: `docs/design/system.md` — colors, typography, components, motion, anti-patterns

## Brand safety — hard rule

**All ad brands in `radio.toml` must be fictional.** Never add a real company, product, or registered trademark to `[[ads.brands]]`. This applies to names, taglines, slogans, and campaign copy.

Why: the scriptwriter generates fake ads in the brand's voice, makes false product claims, and can produce satirical or defamatory content. Doing this with real brands creates trademark infringement and false advertising exposure — including pharma brands where false claims carry regulatory risk.

**The test:** google the brand name. If it returns a real company, it does not belong in `radio.toml`. Invented names that sound Italian and absurd are correct. Names that are one letter off a real brand (e.g. "Barella" for Barilla) also fail — the intent to deceive is implicit in the similarity.

## Notes for future edits

- `admin.html`, `listener.html`, and `live.html` live in `mammamiradio/web/templates/` and are loaded by `mammamiradio/web/streamer.py`.
- `start.sh` is part of the runtime contract, not just a convenience script.
- `radio.toml` is the source of truth for hosts, pacing, ad brands, audio settings, and Home Assistant enablement. Secrets stay in `.env`.
- If you change routes, config keys, auth rules, or fallback behavior, update the matching docs in the same change. (See **Doc sync** rule below.)
- `conductor.json` and `scripts/conductor-*.sh` define Conductor workspace setup/run/archive behavior. Commit those files, but keep `.context/` runtime state out of git.
- If the user has a live stream running, do not stop, restart, or reload it unless they explicitly ask. Protect the illusion first.
- Treat 60 minutes of uninterrupted runtime per live station object as the default minimum when tinkering around an active stream.
- Built-in demo music should favor current modern tracks, not nostalgic or older fallback selections.
- Advertisements need a convincing underlying sound bed. Prefer CC-free music or sound design beds under ad voiceovers instead of dry voice-only spots.
- To tune host behavior without a stream gap: edit `mammamiradio/hosts/scriptwriter.py` (generation logic) and/or the prompt-data leaves — `mammamiradio/hosts/prompt_world.py` (expression banks, host fingerprints, Chaos/Festival mode blocks), `mammamiradio/hosts/transitions.py` (transition rewrite openers), `mammamiradio/hosts/fallbacks.py` (chaos stock lines, ad-break bumpers) — run `make check`, then `POST /api/hot-reload` (admin auth, empty body) — it reloads `prompt_world`, `transitions`, `fallbacks` then `scriptwriter` leaves-first, so edits to any of them take effect — then `POST /api/trigger {"type": "banter"}` to generate a segment with the new code. The stream stays live throughout. If reload fails (syntax error), the endpoint returns 500 and the stream keeps running with the old code.

## Quality gates

- **Land via `/ship` — never a bare merge (enforced)**: Every PR is opened and merged through `/ship`, which runs the mandatory pre-ship review squad (adversarial + test-coverage + docs/config-consistency). A `PreToolUse` hook (`scripts/hooks/require-preship-squad.sh`, wired in `.claude/settings.json`) refuses a bare `gh pr create` / `gh pr merge` unless a `review`/`adversarial-review` entry is logged for HEAD (or a recent ancestor). Added after a refactor cut opened PRs with bare `gh pr create`, skipping the squad's docs/config-consistency check and letting a doc-sync violation reach a green PR. Fail-open and project-scoped.
- **QA gates (mandatory, risk-scoped)**: Manual `/qa` is required for the surfaces a PR can affect, and every release candidate must pass both surfaces before user-facing release.
  1. **Player QA** (`/qa` on `/` dashboard) is required for listener-facing changes: stream playback, now-playing, up-next, Casa card, song requests, clip sharing, public status, listener routes/assets, or playback-visible behavior.
  2. **Admin QA** (`/qa` on `/admin`) is required for operator-facing changes: controls, pacing sliders, host config, key management, engine room, playlist management, admin routes/assets, or operator feedback.
  3. PRs affecting both surfaces, shared auth/routing/frontend state, or uncertain user-facing behavior require both.
  4. Docs-only, tests-only, CI-only, dependency-only, and pure internal refactors may skip manual PR QA when automated checks pass and the PR states why no user-facing surface is affected.
  5. Coordinated batches, release-manager queues, edge releases, and stable releases must pass both Player QA and Admin QA on the final candidate state before shipping.
  6. QA may be reused only when the later diff cannot affect that surface; link or name the reused QA result and explain why it remains valid.
  A single combined rushed QA run is still insufficient. Do not claim QA passed unless that exact QA scope ran or was explicitly reused under this rule.
- **QA Impact (PR body / ship notes)**: every PR states its QA scope so the gate above is auditable:
  ```md
  ## QA Impact
  Classification: Player / Admin / Both / None / Deferred to release candidate
  Reason:
  - Touched surfaces:
  - Why this QA scope is sufficient:
  QA performed:
  - Player QA: run / reused / not applicable / deferred
  - Admin QA: run / reused / not applicable / deferred
  ```
  For stacked PRs and release-manager queues: rebase/fix/green each PR, run only the PR-specific QA surface when the PR itself is risky, stage the queue into a release candidate, then run full Player QA + Admin QA once on the final candidate and ship only if both pass. The pre-ship review squad is unchanged; this rule scopes only manual `/qa`.
- **Coverage ratchet (automatic)**: Coverage can only go up, never down. Two layers enforce this:
  - **Aggregate floor**: `fail_under` in `pyproject.toml` — the overall minimum.
  - **Per-module floors**: `.coverage-floors.json` — every module has its own floor. A module-level regression fails CI even if the aggregate stays above threshold.
- **CI enforcement**: `.github/workflows/quality.yml` runs `scripts/coverage-ratchet.py`:
  - On PRs: `check` mode — fails if any module dropped below its floor.
  - On main merge: `update` mode — auto-ratchets all floors up and commits the new values. Zero human intervention.
- **Local check**: `make coverage-check` to verify locally. `make coverage-ratchet` to preview what CI would commit.
- **Adding tests**: Write tests, push. CI will auto-raise the floors on merge. The next PR that drops any module will fail.
- **Release cooldown gate**: `.github/workflows/release-cooldown.yml` blocks any `v*` tag push if the prior published release is <24h old. Bypass by adding the `hotfix` label to the PR that introduced the tagged commit. Self-test: `bash tests/workflows/test_cooldown_gate.sh` (9 cases; also runs in `quality.yml` on every PR). See `docs/runbooks/ha-addon.md` and `docs/stabilization-log.md` for the measurement plan.
- **Release invariants** (`scripts/check-release-invariants.sh`): runs on every PR. Catches (1) FFmpeg `music_eq_chain` equalizer count ≠ 2 (Pi aarch64 SIGABRT risk), (2) missing `_pick_canned_clip=None` test mock (empty-container silence untested), (3) missing `session_stopped` test (post-restart silence untested). Local: `bash scripts/check-release-invariants.sh`.
- **Version sync check** (inline in `quality.yml`): runs on PRs that touch `pyproject.toml` or `ha-addon/mammamiradio/config.yaml`. Runs the full `scripts/pre-release-check.sh` (version consistency + CHANGELOG head + all invariants). No-ops on unrelated PRs. Local: `make pre-release`.

## Protected UI elements

These UI elements have regressed in past refactors. Always verify they survive after any HTML edit:

- **Token cost counter** (`admin.html` Engine Room) — backend computes `api_cost_estimate_usd` on every `/status` call. UI must display it. Has disappeared twice in refactors.
- **Play button blue state** (`mammamiradio/web/static/base.css`) — `.play-btn.playing` must use `var(--ok)` (blue), never `var(--sun2)` (golden). Colorblind safety.
- **Station name localStorage** (`mammamiradio/web/static/listener.js`) — reads `stationName` from localStorage. Admin writes it. Broken when dashboard.html was rewritten.
- **Gold "Mi" accent** (`listener.html`, `admin.html`) — `<span class="mi">` in h1, styled `color: var(--sun)`. Brand signature from hero banner.
- **Italian tricolor stripe** (`admin.html` uses `.tricolor-stripe`; `listener.html` uses `.tricolor-band`) — present below h1. Must match hero banner.
- **Admin espresso surface** (`mammamiradio/web/static/tokens.css`) — `--surface` / `--surface-strong` / `--line-strong` must remain at Pi-baseline values (`#251E19` / `#362B25` / `0.16`) so admin reads as espresso warm-brown, not washed-out taupe. Listener-card visibility fixes belong inline on `.mmr-*` classes in `listener.css` (schedule / dedica / about-card / hero-stage), never on shared tokens. Regressed once in PR #298.

When editing any HTML file, grep for these elements before committing.

## Doc sync

**Any change to a route, config key, env var, auth rule, or fallback path must update at least one of the following docs in the same commit:**

`README.md`, `docs/architecture.md`, `docs/troubleshooting.md`, `docs/operations.md`, `CLAUDE.md`, `CHANGELOG.md`

If the behavior changed and the docs didn't, the docs are wrong. Fix them in the same change, not a follow-up.

## Changelog editorial boundary

`CHANGELOG.md` and `ha-addon/mammamiradio/CHANGELOG.md` are PUBLIC release notes for users, operators, and contributors. They describe what changed and why it matters. They are NOT internal sprint logs.

**Never write into the public changelogs:**

- Sprint / workstream labels: `WS2`, `WS3-A`, `PR-A`, `PR-B/5`, `Phase A`, `Phase 1`, `Approach B`
- Finding numbers: `finding #8`, `Item 19`, `(P0-1)`, `(M1)`, `(H2/H3)`
- AI tool provenance: `/autoplan`, `codex review`, `Claude review`, `Conductor session`, `codex independent review`
- Planning archaeology: `soak window`, `live session`, `2026-04-17 live session`, references to `docs/YYYY-MM-DD-*.md` planning files
- Architectural metaphors as labels: `cathedral`, `domain naves`, `sacred files`, `god-module`, `leadership principle`, `operator-honesty`
- Contributor archaeology: `first outside contribution`, `work was superseded`

**Where this content belongs instead:** runbooks (`docs/runbooks/`), stabilization log (`docs/stabilization-log.md`), strategic planning docs (`docs/YYYY-MM-DD-*.md`). **Not** PR bodies — the same editorial boundary applies to pull-request descriptions.

**Enforcement:** the shared pattern list lives in `scripts/lint-patterns.sh` (`LINT_PATTERNS` array). Two lints consume it:

- `scripts/check-changelog-lint.sh` — runs in `quality.yml` against `CHANGELOG.md` and `ha-addon/mammamiradio/CHANGELOG.md`.
- `scripts/check-pr-body-lint.sh` — runs in `.github/workflows/pr-body-lint.yml` against the PR body on every `opened/edited/synchronize/ready_for_review` event, plus a small set of PR-body-specific patterns for process narrative (`N commits ahead`, `picked up cleanly`, `auto-decided`, `soak verification`, `dual-voice review`, `🤖 Generated with`). The local PreToolUse hook (`~/.claude/hooks/verify-proof-block.sh`) chains it in at `gh pr create` time when the script is present in the project.

To extend the rules, add a regex to `LINT_PATTERNS` in `scripts/lint-patterns.sh` — both lints pick it up automatically.

## Scope discipline

Two rules, both narrow, both targeted at patterns observed in the audit of the
last 10 merged PRs (2026-05-03):

**1. Planning docs ship in their own PR.** Strategic planning documents
(multi-week plans, design proposals, retrospectives, files matching
`docs/YYYY-MM-DD-*.md` or `docs/2026-*-plan.md`, post-mortems) must NOT be
bundled into fix/feat PRs. They get their own PR with their own scope. The
audit found exactly one major creep instance in 10 PRs — a CI fix bundled with
a 443-line strategic planning doc — and it is the dominant catchable pattern
in this repo.

**2. When you stumble on an adjacent issue not in scope:** classify it before
writing anything down.

- Public, actionable engineering work goes into a GitHub issue or feature
  request with a narrow title, user-visible impact, implementation scope,
  acceptance criteria, and relevant file references. Link the issue from the PR
  only when it helps reviewers understand why the adjacent work is parked.
- Non-public strategy, outreach, relationship context, pitch framing, timing
  gates, or personal writing plans must not be written to tracked repo files or
  public GitHub issues. Put it in a private durable system outside the repo.
- If no private durable system is available in the current session, stop and
  ask where the note should live instead of creating a tracked TODO file or
  hiding durable strategy in workspace-local `.context/`.
- Already-shipped items should be removed from backlog surfaces, not moved into
  another open list.

Do NOT create or append to `TODO.md`, `TODOS.md`, `docs/todos.md`,
`docs/backlog.md`, or equivalent tracked catch-all backlog files. A scoped
planning document is allowed only when it is itself the PR deliverable, not as a
sidecar parking lot for unrelated work.

**Exemptions** (not scope creep):
- Doc updates required by the **Doc sync** rule above (same-commit doc-sync
  is mandatory, not optional)
- Test files mirroring source changes (`tests/X/**` alongside
  `mammamiradio/X/**`)
- Mechanical fallout from renames (path-string updates, import
  fixups) within the same PR as the rename
- Sibling caller updates when a public function signature changes (≤3 files
  in other naves; beyond that, the change is legitimately cross-cutting and
  needs its own scope statement)

**Why no automated gate.** A scope-guard mechanism was designed and rejected
on 2026-05-03 after a 10-PR audit measured creep frequency at 2/10 (boundary
case) and found that the dominant creep pattern (planning-doc hitchhiking)
isn't catchable by file-pattern globs. See
`~/.gstack/projects/florianhorner-mammamiradio/florianhorner-cicd-freeze-reflection-design-20260503.md`
for the full reasoning. If creep frequency rises (>4/10 in a future audit),
revisit the mechanism path.

## Review discipline

For every bug fix or behavior change, do not stop at the first broken instance.

- Identify the user-visible promise or system invariant that failed.
- Check sibling code paths for the same failure mode before concluding the fix is done.
- Add or update at least one automated guard (test, validation, or build check) that would fail if the invariant breaks again.
- If duplicated state exists, explain what keeps it synchronized. If you cannot name the synchronization boundary, treat that as a design risk and either remove the duplication or add a guard around it.

Review question to apply before merge:

`What invariant failed here, where else could it fail the same way, and what automated check will catch the next instance before a user does?`

## Refactor discipline

For behavior-preserving refactors that MOVE a symbol between modules (the god-module
split train, and any future relocation), run these checks at SCOPE time — before naming
what moves — not only during execution:

- **Whole-repo symbol grep.** Grep every moved symbol across the ENTIRE repo, including
  `scripts/`, `.github/workflows/`, and `docs/` — not just `mammamiradio/` + `tests/`. CI
  guards and shell scripts hardcode symbol names; a move that ignores them red-fails the
  build (W3b near-miss: `scripts/validate-addon.sh` AST-scans for `_inject_ingress_prefix`).
- **Dependency-closure gate.** Verify every symbol the move-target calls that is SHARED
  with code staying behind or destined for another cut. A target that reaches a shared
  primitive must not move until that primitive's home is settled (W3b: `_render_admin_response`
  → `_get_csrf_token`, shared with the auth cut).
- **Read the test bodies, not just grep counts.** Refines the plan-audit rule — read the
  actual test files; counts miss pre-existing duplication (W3b: 8 sanitize tests, not 4).

Full per-cut checklist + cut-by-cut lessons: `docs/runbooks/refactor-cuts.md`.

## Audio delivery test coverage rule

Every PR touching audio delivery (producer, streamer, normalizer, any bridge/fallback path) must include tests for all three scenarios. Missing any one = untested blast radius, do not ship.

**Scenario 1 — Normal:** feature works as designed.

**Scenario 2 — Empty fallback:** canned clips absent, norm cache empty, no assets in container. The real container ships only README stubs in `mammamiradio/assets/demo/banter/`. Tests that mock `_pick_canned_clip` to return a real file are hiding this class of bug.

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

## First-time contributor protocol

**Merge-first, refactor-second.** When a first-time outside contributor PR is open and approved, it merges BEFORE any maintainer work touches overlapping files. Follow-on refactoring goes in a separate PR after their merge lands. The "Merged" badge on their PR page is part of the first-time-contributor experience we owe them.

Discovered 2026-04-23 when PR #203 (Ashika Rai N's dashboard extraction) landed as commit `2028d40` but was made unmergeable hours later by same-day design-system consolidation commits `4887876` + `598f96b` — forcing a "Closed" badge on a PR whose code had in fact shipped. See `CONTRIBUTING.md` and the Contributors section of `README.md` for credit channels; she was credited there and in `CHANGELOG.md` [Unreleased] under Refactored despite the badge.

**Operational rules:**

1. When a first-time outside contributor PR opens, add a `first-time-contributor` label (or treat the PR page as a personal freeze signal).
2. Do not push maintainer commits that touch the same files while that PR is open.
3. If a rebase is needed, rebase their work onto current `main` with `Co-authored-by` preserved, then merge via "rebase and merge" or "create merge commit" (NOT squash) to keep the head SHA reachable and the "Merged" badge earned.
4. Batch your planned refactors into a branch that depends on their merge landing first.

## Health Stack

- typecheck: mypy mammamiradio/ tests/
- lint: ruff check .
- test: pytest
- deadcode: vulture mammamiradio/
- shell: shellcheck $(find . -name "*.sh" -not -path "./.venv/*" -not -path "./.git/*" -not -path "./.claude/skills/*")



<!-- BEGIN: commit-message-standards (managed by bootstrap-repo.sh — do not hand-edit) -->
## Commit message standards

This repo follows the [engineering-standards commit-message spec](https://github.com/florianhorner/engineering-standards/blob/main/specs/commit-message-spec.md).

**Quick rule:** Conventional Commits (`type(scope): subject`, ≤72 chars). A `Why:` body line is REQUIRED when type is `feat` AND >50 lines changed; otherwise optional.

**Local invocation:** Use the `/commit` skill in Claude Code / Conductor. Default behavior is dry-run (drafts a message and shows the validator output without committing); pass `--commit` to actually create the commit. Manual `git commit` works too — the local `commit-msg` hook validates either path.

**Per-repo cheat sheet:** [`./CONTRIBUTING.md`](./CONTRIBUTING.md) carries the 30-second cheat sheet, good/bad examples, banned patterns, exempt subjects, bot allowlist, and bypass policy. It is self-sufficient for cloud agents (Claude Code Cloud, Codex web) that only see repo-local files.

**Machine-readable rules:** [`.config/commit-rules.json`](.config/commit-rules.json) is a SHA-pinned vendored copy of the upstream `commit-rules.json`. The validator binary, commit-msg hook, and CI workflow all read this file. Do not hand-edit — re-run `bootstrap-repo.sh` to refresh.

**Bypass:** `git commit --no-verify` requires a `Policy-Override: <reason>` trailer to pass CI. Logged to `~/.commit-bypass.log` by the pre-push hook.
<!-- END: commit-message-standards -->
