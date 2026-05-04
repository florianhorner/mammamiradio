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
- `docs/design/system.md` - Volare design system: colors, typography, components, motion
- `docs/design/admin-panel.md` - admin control-room layout, info architecture, motion rules
- `docs/conductor.md` - Conductor workspace lifecycle and `.env` discovery
- `docs/agents.md` - agent-specific notes and integration points
- `docs/stabilization-log.md` - weekly fix-hours and emergency-patch counts (release cooldown gate)
- `docs/todos.md` - deferred work items (operator-honesty pivot, etc.)

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
- `JAMENDO_CLIENT_ID`: Jamendo API client id (empty = Jamendo source disabled)
- `JAMENDO_COUNTRY`: 3-letter uppercase ISO 3166-1 alpha-3 (e.g. `ITA`, `DEU`); empty disables the country filter. radio.toml default is `ITA` for Italian-trending music.
- `JAMENDO_ORDER`: Jamendo sort order (`popularity_week` | `popularity_month` | `popularity_total` | `releasedate_desc` | empty). radio.toml default is `popularity_week`.
- `MIN_COOLDOWN_HOURS`: override the release-cooldown window (default `24`, read by `scripts/check-release-cooldown.sh`)

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
  web/                      streamer (TODO: split — routes/auth/playback loop), og_card, templates/, static/
  assets/                   demo/ MP3s + SFX, logo.svg
radio.toml                  station config
start.sh                    dev entrypoint with uvicorn and reload
tests/                      mirrors mammamiradio/ — tests/<nave>/test_*.py
```

Two god modules carry a `# TODO: split` marker: `web/streamer.py` (~2,400 LOC) and `hosts/scriptwriter.py` (~1,500 LOC). They have postal addresses now; the actual splits land in PRs 5 and 6 of the cathedral plan (`docs/2026-04-28-cathedral-restructure.md`).

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
- To tune scriptwriter behavior without a stream gap: edit `mammamiradio/hosts/scriptwriter.py`, run `make check`, then `POST /api/hot-reload` (admin auth, empty body), then `POST /api/trigger {"type": "banter"}` to generate a segment with the new code. The stream stays live throughout. If reload fails (syntax error), the endpoint returns 500 and the stream keeps running with the old code.

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

**Where this content belongs instead:** PR bodies, runbooks (`docs/runbooks/`), stabilization log (`docs/stabilization-log.md`), strategic planning docs (`docs/YYYY-MM-DD-*.md`).

**Enforcement:** `scripts/check-changelog-lint.sh` runs in CI on every PR. To extend, add a regex pattern to the `PATTERNS` array in that file.

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

**2. When you stumble on an adjacent issue not in scope:** append one entry to
`docs/todos.md` (the canonical project backlog) and continue. Do NOT fix inline.
Do NOT mention in the current PR.

Format — append under a section heading that matches the area, as a brief
`### <Title>` block in the same shape as the rest of `docs/todos.md`:

```text
### <one-line title>
**Priority:** P2
**Source:** scope-parked from <branch-name> on YYYY-MM-DD
<file:line> — one-sentence description of what was noticed.
```

This is positive action — the rule has something concrete for you to *do*
with the finding, instead of just "don't fix it." The cost of one
TODOS.md line is near zero; the cost of a 200-line adjacent fix bundled into
a fix PR is hours of review and reshaping.

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
