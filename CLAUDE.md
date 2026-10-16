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

**5. SPEAK HUMAN, ALWAYS WITH A WAY OUT**
Every word a listener or operator reads is product copy, not a log line. No tech lingo reaches a human screen — "rate limit", "buffer empty", "429", "timeout", "rejected", "degraded" are machine words and belong in logs, never in the UI. Replace them with warm, human-readable language a non-technical person understands. And never just name a problem: every error must also tell the user how to fix it, with a concrete next step ("give the tape decks a few seconds and tap again"). A message that states a failure without a way forward is a bug, not a message. This applies to every human-facing surface. Voice differs by surface: the listener UI speaks in the station's full in-character voice (Italian-first under Super Italian Mode); the admin UI stays warm but plain English (see the admin-localization rule) — same no-lingo, always-a-fix standard, different register.

## Production Systems Discipline — HARD STOP

**Principle: No live surgery on the HA Green.** The only legitimate code-change path for the mammamiradio addon is:

`branch → PR → merge → CI builds image → addon updates to new image`

**Default HA development target:** First Listen and other Home Assistant-facing
branch work runs against `scripts/first-listen-lab.sh`, the disposable local HA
Container + VLC speaker lab. Never connect branch code to the live home, reuse
its token or backup, or attach household/cloud/MQTT devices unless the maintainer
explicitly authorizes that live action in the current message. Lab runtime and
credentials belong only under gitignored `tmp/first-listen-ha-lab/`, never in
`.context/` or tracked files. See
`docs/runbooks/first-listen-local-ha.md`.

The restart happens once, planned, when the addon updates. Not during the day. Not as an experiment. Not to "test the fix live."

**NEVER do any of these against the running mammamiradio addon without the maintainer's explicit confirmation in the current message:**

1. **No live code patching.** `docker cp` into the addon container, `docker exec` with write operations (`sh -c "cat > ..."`, `tee`, `echo >`, `sed -i`, any redirection into a file), editing files inside a running container by any other means. These changes are wiped on the next restart and mask the real state of production.
2. **No process signals.** `pkill`, `kill`, `killall`, `docker kill`, `docker restart` targeting any process inside the addon. s6-rc in this container does NOT auto-restart killed services reliably — killing a process kills the container, and killing the container kills the stream.
3. **No addon restarts.** `ha apps restart`, `ha apps stop`, `docker restart addon_*_mammamiradio`, `supervisor restart`. Ask before any of these. Exception: recovery from an already-broken state (container exited, stream already dead) — restoring is correct there, but still announce it first.
4. **No live config edits.** Writing to `/config/` from a shell, editing `radio.toml` on the Pi, modifying addon options via CLI without going through the HA addon config UI.
5. **No ad-hoc Docker image changes.** Building, tagging, or pushing addon images from the Pi. Images come from CI only.
6. **No volume mounts introduced for debugging.** Mounting host paths into the container to sidestep the rebuild chain.

**Legitimate operations (no confirmation needed):**
- `docker ps`, `docker logs`, `docker exec <container> <read-only command>` (cat, ls, grep, env, ps)
- `ha apps info`, `ha apps list`, `ha apps logs`
- `ha apps start <slug>` when the addon is already stopped/exited (recovery, not experiment)

**Why this rule exists:** Live patches (`docker cp`, in-container writes, process kills) are wiped on addon restart and can drop the stream with no durable fix. s6-rc in this container does not reliably auto-restart killed services. This rule closes the loophole between "don't restart HA core" and "don't experiment on the running addon."

**When a fix is urgent:** merge → wait ~5–10 min for CI → update addon. That path is faster than live surgery, leaves a permanent fix in place, and preserves the illusion — one planned restart beats repeated unplanned drops.

## Docs

Core documentation at the repo root (one viewport, one job each):

- `README.md` - product pitch and operator quick start
- `CONTRIBUTING.md` - local setup, tests, and smoke checks
- `CLAUDE.md` - agent rules and leadership principles (this file)
- `CHANGELOG.md` - release notes
- `CONTRACT.md` - frozen v1 integration surface and change ceremony

Everything else lives under `docs/`:

- `docs/status-quo.md` - dated product and market status
- `docs/architecture.md` - runtime flow, queue model, and audio pipeline
- `docs/operations.md` - runtime assumptions and deploy reality
- `docs/troubleshooting.md` - common failures and recovery paths
- `docs/release-process.md` - release *strategy*: themes vs versions, single-trunk cut-don't-open mental model — `main` always advertises the last **published** version, and the number changes only in the `chore(release): cut X.Y.Z` commit (the *how-to-cut* lives in the runbook below)
- `docs/runbooks/ha-addon.md` - addon release process, config contract, pre-merge checklist
- `docs/runbooks/first-listen-local-ha.md` - disposable local HA + real Mac speaker acceptance lab
- `docs/runbooks/refactor-cuts.md` - god-module split: per-cut pre-flight checklist and lessons
- `docs/runbooks/ha-upstream-watch.md` - early-warning watcher for HA upstream changes touching our HA surface
- `docs/design/system.md` - Volare design system: colors, typography, components, motion
- `docs/design/admin-panel.md` - admin control-room layout, info architecture, motion rules
- `docs/design/listener-contract.md` - listener interaction and state contract for `/` and `/listen`
- `docs/conductor.md` - Conductor workspace lifecycle and `.env` discovery
- `docs/agents.md` - agent-specific notes and integration points
- `docs/runbooks/parallel-workspaces.md` - WIP cap, Path A vs Path B, write-sets; no orchestrator agent; `land-pr.sh` is the human/feature merge path to `main`, with guarded Dependabot auto-merge as the automated exception
- `docs/listener-qs-train.md` - `Train/Listener QS` intake, merge gate, and handoff contract (Path B only)
- `docs/stabilization-log.md` - weekly fix-hours and emergency-patch counts (release cooldown gate)

There is no canonical tracked TODO file. Do not create or revive
`TODO.md`, `TODOS.md`, `docs/todos.md`, `docs/backlog.md`, or similar
catch-all backlog files. Use GitHub issues for public engineering work and a
private durable system for strategy or relationship context.

## Commands

Set up the environment before running the app or tests:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip install --force-reinstall --require-hashes -r requirements.txt
python -m pip install --no-deps -e .
python -m pip check
```

For an existing environment, activate it and repeat the four `python -m pip` commands to restore the runtime lock. See [Local setup](CONTRIBUTING.md#local-setup). uv does not manage `.venv` (`[tool.uv] managed = false` in `pyproject.toml`): `uv sync`, `uv lock` and `uv add` refuse on purpose, so install with the pip commands and never remove the setting.

- Run full local stack: `./start.sh`
- Run app only: `source .venv/bin/activate && python -m uvicorn mammamiradio.main:app --reload --reload-dir mammamiradio`
- Test: `pytest tests/` or `make test` (with coverage)
- Test watch: `make test-watch` (re-runs on file save)
- Test HA add-on build locally: `scripts/validate-addon.sh --build`
- Run First Listen against disposable local HA: `scripts/first-listen-lab.sh start`
- Lint: `ruff check .` (fix: `ruff check --fix .`)
- Format: `ruff format .` (check: `ruff format --check .`)
- Type check: `mypy mammamiradio/ tests/`
- All checks: `make check` (lint + format check + typecheck + coverage gate with per-module floors) — `format-check` mirrors CI's `ruff format --check` so a format nit can't pass locally then fail CI
- Pre-commit: `pip install pre-commit && pre-commit install --hook-type pre-commit --hook-type pre-push --hook-type commit-msg`
- **Validate addon before push**: `./scripts/validate-addon.sh` (add `--build` for Docker build test)
- **Release cooldown self-test**: `bash tests/workflows/test_cooldown_gate.sh` (9 scenarios, runs in `quality.yml` on PRs that touch `.github/`, `scripts/`, or `tests/workflows/`, and on every push to `main`)

## Docker / Home Assistant

- `Dockerfile`: standalone container image with Python 3.14 + FFmpeg
- `docker-compose.yml`: one-command run for non-HA users
- `.dockerignore`: keeps builds clean
- `ha-addon/`: Home Assistant add-on scaffold
  - `ha-addon/mammamiradio/config.yaml`: add-on metadata, options schema, ingress config
  - `ha-addon/mammamiradio/Dockerfile`: HA add-on image (Alpine-based)
  - `ha-addon/mammamiradio/rootfs/run.sh`: entrypoint mapping Supervisor env vars
  - `ha-addon/mammamiradio/translations/en.yaml`: UI labels for add-on options
  - `ha-addon/mammamiradio-edge/`: dev-release channel add-on (metadata only — pulls the same image as stable; version is the short SHA of the newest `main` commit with a built image, which may trail HEAD, cut with `make edge-release`). See `docs/runbooks/ha-addon.md` → "Edge channel".
- `.github/workflows/docker.yml`: multi-arch Docker build CI

## Environment

- The full variable catalog (defaults, clamps, fallbacks) lives in `docs/operations.md` "Environment variables (full catalog)".
- Model selection lives in `model_registry.toml`, never in code (see `docs/architecture.md` "Dynamic LLM routing").
- Secrets stay in `.env`; `radio.toml` holds station config.
- When a variable is added, renamed, or changes its default, the **Doc sync** rule applies: update the catalog in the same change.

## Runtime behavior

- Runtime invariants live in `docs/architecture.md`: Startup flow, Restart handoff, Egress FX pipeline, Queue commit, Protected continuity reservations, Source switch concurrency, Keepsakes, Operator song blocklist, Moment Receipts, Music sources, Failure model.
- Agents touching producer, streamer, playlist, or home code read the matching section first.
- Always on: station-wide toggles persist first and mutate second.
- Always on: keepsake eligibility fails closed on both segment type and music tail.
- Always on: rescue fills skip the egress colour pass.

## Project structure

The folder hierarchy IS the mental model (leadership principle #4). For a single-page "where does X live" map see `docs/REPO_MAP.md`.

```text
mammamiradio/
  main.py                   FastAPI app startup/shutdown lifecycle (kept at top — public entry)
  core/                     config, models, capabilities, guided setup, First Listen receipts/show, sync (SQLite schema)
  audio/                    normalizer (FFmpeg), audio_quality gate, tts, voice_catalog
  playlist/                 playlist source selection, downloader, song_cues, track_rationale, track_rules
  hosts/                    scriptwriter (LLM banter+ads — TODO: split), persona, context_cues, ad_creative
  home/                     HA context, preview-value classification, speaker discovery/playback, event enrichment, generated labels
  scheduling/               producer (async loop), scheduler (segment-type picker), clip (WTF ring buffer)
  web/                      streamer (TODO: split — routes/playback loop), auth (admin auth + CSRF), pages (ingress rewrite), listener_requests, og_card, templates/, static/
  assets/                   demo/ MP3s + SFX, logo.svg
  release_campaign.py       packaged release-beat manifest -> bounded on-air cold-open campaign
  restart_handoff.py        post-restart music continuity spool (producer writes, main.py admits at boot)
radio.toml                  station config
start.sh                    dev entrypoint with uvicorn and reload
tests/                      mirrors mammamiradio/ — tests/<module>/test_*.py
```

`web/streamer.py` owns routes and playback; `hosts/scriptwriter.py` assembles
host scripts. Scope any further extraction explicitly and follow
`docs/runbooks/refactor-cuts.md`.

## Design System

Always read `docs/design/system.md` before making any visual or UI decisions.
All font choices, colors, spacing, and aesthetic direction are defined there.
Do not deviate without explicit user approval. In QA mode, flag any code that doesn't match `docs/design/system.md`.

## Brand assets

- **Hero banner / social card**: `docs/banner.png` and `docs/social-card.png`, matching 1280×640 README hero and social preview. Warm Home Assistant-native room background composited with Playfair italic typography and the "La Casa Ti Ascolta" overlay. Source template: `docs/hero-composite.html` (contains regeneration instructions in comment header). The temporary background image (`banner-bg.png`) is generated externally and not committed to git.
- **Logo SVG**: `mammamiradio/assets/logo.svg` — canonical vector source (variant G: classic radio with Italian flag stripe and sound waves)
- **Palette**: Volare Refined — espresso dark with Italian warmth in accents. See `docs/design/system.md` for the full design system.
  - Background: espresso dark (`#14110F`) with subtle warm gradient at top
  - Cards: admin warm-brown `--surface` (`#251E19`); listener cards use brighter `--card` / `--card-strong` tokens for visibility on the dark page
  - Accent: golden sun (`#F4D048`, `#ECCC30`) — play button, active borders
  - Interactive: Lancia red (`#B82C20`) — FM dial needle
  - Text: cream (`#F5EDD8`)
  - Success/connected: blue (`#2563EB`) — never green (colorblind)
- **Typography**: Playfair Display italic (station name, display text) + Outfit (body) + JetBrains Mono (technical)
- **Favicon**: `mammamiradio/web/static/favicon.svg` is the browser-tab copy of the canonical logo; `tests/web/test_route_smoke.py` keeps it byte-for-byte synchronized with `mammamiradio/assets/logo.svg`. The browser fallback route is `/favicon.ico`.
- **HA add-on icon**: `ha-addon/mammamiradio/icon.png` (256px) and `logo.png` (512px), rasterized from the SVG
- To regenerate PNGs from SVG: `cairosvg mammamiradio/assets/logo.svg -o ha-addon/mammamiradio/icon.png -W 256 -H 256 && cairosvg mammamiradio/assets/logo.svg -o ha-addon/mammamiradio/logo.png -W 512 -H 512`
- **Full design system**: `docs/design/system.md` — colors, typography, components, motion, anti-patterns

## Brand safety — hard rule

**All ad brands in `radio.toml` must be fictional.** Never add a real company, product, or registered trademark to `[[ads.brands]]`. This applies to names, taglines, slogans, and campaign copy.

Why: the scriptwriter generates fake ads in the brand's voice, makes false product claims, and can produce satirical or defamatory content. Doing this with real brands creates trademark infringement and false advertising exposure — including pharma brands where false claims carry regulatory risk.

**The test:** google the brand name. If it returns a real company, it does not belong in `radio.toml`. Invented names that sound Italian and absurd are correct. Names that are one letter off a real brand (e.g. "Barella" for Barilla) also fail — the intent to deceive is implicit in the similarity.

## Notes for future edits

- `admin.html`, `listener.html` and `clip.html` live in `mammamiradio/web/templates/` and are loaded by `mammamiradio/web/streamer.py`. **Before adding a UI feature to a template, verify which route serves it** — the mapping is not obvious: `/` serves `listener.html`, except under HA ingress from hassio/loopback, where it renders the admin page; `/listen` is a listener alias; `/admin` serves `admin.html`; `/dashboard` is only a redirect to `/admin`. Check the `@router.get` definitions in `web/streamer.py`, not the filename. The casa-card feature was built in the wrong file once — a full session wasted. (Moved here from the global `~/.claude/CLAUDE.md` on 2026-08-02; it was repo-specific and its paths had gone stale.)
- `start.sh` is part of the runtime contract, not just a convenience script.
- `radio.toml` is the source of truth for hosts, pacing, ad brands, audio settings, and Home Assistant enablement. Secrets stay in `.env`.
- If you change routes, config keys, auth rules, or fallback behavior, update the matching docs in the same change. (See **Doc sync** rule below.)
- `scripts/conductor-*.sh` define Conductor workspace setup/run/archive behavior. The committed `.conductor/settings.toml` wires those hooks for new local and cloud workspaces after the change reaches the default branch. Machine-local `.conductor/settings.local.toml` values take precedence, so do not duplicate lifecycle hooks there. Keep `.context/` runtime state out of git.
- If the user has a live stream running, do not stop, restart, or reload it unless they explicitly ask. Protect the illusion first.
- Treat 60 minutes of uninterrupted runtime per live station object as the default minimum when tinkering around an active stream.
- Starter-catalog changes must update the canonical manifest, source evidence,
  human audition record, and media proof together; never substitute unverified
  demo or fallback music. The exact crate is additionally pinned by
  `LOCKED_TRACKS` in `tests/media/test_starter.py`, so swapping it is a
  deliberate test change and can never happen silently.
- **Bundled music is attribution-only, and that is a hard boundary, not a
  preference.** Two providers are supported: Incompetech (CC BY 4.0, keyless)
  and Jamendo (CC BY 3.0, needs a client id to prove the licence). A row's
  `provider` field selects the acquisition path; absent means Incompetech.
  NonCommercial, NoDerivatives and ShareAlike are all refused, by name, in
  three places — `_require_attribution_only_license` in `media/starter.py`,
  `_require_jamendo_attribution_license` in the acquire tool, and the manifest
  gate in `check`. **ND is the one that silently disqualifies otherwise perfect
  tracks**: every bundled track is normalized to a fixed loudness and
  re-encoded, which is a derivative, and NoDerivatives forbids distributing
  one. That is why a track can be great, free, and still unusable.
- Advertisements need a convincing underlying sound bed. Prefer CC-free music or sound design beds under ad voiceovers instead of dry voice-only spots.
- To tune host behavior without a stream gap: edit `mammamiradio/hosts/scriptwriter.py` (generation logic) and/or the prompt-data leaves — `mammamiradio/hosts/prompt_world.py` (expression banks, host fingerprints, exchange-shape/lore data, Chaos/Festival mode blocks), `mammamiradio/hosts/relationship.py` (roster-aware shape/lore selection and recency), `mammamiradio/hosts/transitions.py` (transition rewrite openers), `mammamiradio/hosts/fallbacks.py` (chaos stock lines, ad-break bumpers), `mammamiradio/hosts/station_name_guard.py` (foreign/competitor station-name scrubbing) — run `make check`, then `POST /api/hot-reload` (admin auth, empty body) — it reloads `language_policy`, `prompt_world`, `relationship`, `transitions`, `fallbacks`, `station_name_guard`, then `scriptwriter` leaves-first, so edits to any of them take effect — then `POST /api/trigger {"type": "banter"}` to generate a segment with the new code. The stream stays live throughout. If reload fails (syntax error), the endpoint returns 500 and the stream keeps running with the old code. `mammamiradio/hosts/memory_extractor.py` (post-air memory extraction) is deliberately excluded from hot-reload — it holds live in-flight task/apply-lock state that a reload would reset mid-extraction; edit it and restart to pick up changes.

## Quality gates

- **Open via `/ship` — never a bare `gh pr create`**: Review remains mandatory
  (adversarial + test-coverage + docs/config-consistency), including re-review
  after fixes or conflicted/hand-edited integrations. Repository-local PR and
  landing admission no longer consult review ledgers or require v2 receipts.
  The hook remains registered to deny raw merges. Historical receipts and
  the standalone verifier remain available, but do not emit/reattest for
  ordinary PRs. Physical HA evidence is a separate, unchanged release gate.



- **Landing contract — human and feature PRs merge through `scripts/land-pr.sh`, never raw `gh pr merge` or `gh api` merge calls (single source of truth; the runbook links here)**: The sole automated exception is `.github/workflows/dependabot-automerge.yml` for eligible Dependabot patch and minor updates. `/ship` opens human and feature PRs and never arms auto-merge. The PR soaks (CodeRabbit, review time) until the maintainer's explicit merge signal. On the signal, `scripts/land-pr.sh <PR#>` (1) refuses a behind or conflicted branch without mutating the PR, directing its feature workspace to integrate `origin/main`, review any changed code, and push; (2) blocks unresolved current Major/Critical/P0/P1 bot threads and fails closed if thread data cannot be read, through `scripts/land-gates.sh` shared with the report-only shadow queue, without consulting receipts or local ledgers; (3) checks release-cut admission; and (4) arms `gh pr merge --squash --auto --match-head-commit <head>` so GitHub merges only when required checks pass AND the head is still the one verified — a later push cancels the landing instead of shipping unseen code. The same hook denies raw `gh pr merge` (`--disable-auto` is allowed for disarming), REST `gh api` PUT calls to `/pulls/<n>/merge`, and GraphQL `gh api graphql` mutation payloads containing `mergePullRequest` or `enablePullRequestAutoMerge` (inline or loaded from an inspectable local file; stdin/unreadable payloads are denied because the hook cannot inspect them safely). Branch protection on `main` requires branches to be up to date before merging (strict status checks, set 2026-06-12); integrate the base in the owning workspace before landing. Dependabot auto-merge is opportunistic, not a self-landing guarantee: a behind PR parks until an authenticated maintainer handles that specific PR. During a release cut, `scripts/check-advertised-version.sh` checks GHCR for the version advertised on `main`. Missing images make `dependabot-automerge.yml` disable auto-merge and add `cut-window-hold` to affected Dependabot PRs. The sweep only disarms. After publication, a fresh PR event with verified Dependabot metadata or the landing workflow can resume held PRs. Before arming, the workflow checks that the PR head matches the metadata and `main` matches the registry check. PR events can re-arm a PR you disarmed by hand. Before a cut, the release operator runs `GH_REPO=florianhorner/mammamiradio scripts/dependabot-window-hold.sh freeze` to disable new runs, drain existing runs and disarm PRs. `land-pr.sh` requires verified freeze state for stable-version changes. Keep the human landing freeze through both architecture promotions, then resume explicitly with `GH_REPO=florianhorner/mammamiradio scripts/dependabot-window-hold.sh thaw <release-run-id>`; see the add-on runbook for proof and recovery. GitHub Actions must not post Dependabot rebase or recreate commands. Handle behind branches through an authenticated maintainer. Settings drift tripwire: `scripts/check-merge-gate.sh` (in `make pre-release`), which reads GitHub's effective rules for `main` rather than reimplementing ruleset pattern matching. Honest scope: the hook is a local guard, not a security boundary (fail-open, bypassable via the GitHub UI), and head matching does not eliminate non-conflicting staleness. The owning feature agent handles CI waits and review feedback; the landing seat reports `MERGED` only after confirming GitHub's state, never merely because arming succeeded.
- **Shadow land queue (report-only)**: `.github/workflows/land-queue.yml` runs
  `scripts/land-queue-plan.sh` every 30 minutes and writes to the job summary
  the one thing an auto-land controller *would* do next — integrate or arm a PR,
  and advance edge — plus why every other open PR is not that PR. It merges
  nothing, comments nowhere, and holds no write permission; it is **not** an
  exception to the Landing contract above. It reaches its verdict through the
  same predicates `land-pr.sh` arms on (`scripts/land-gates.sh`) and the same
  selection `cut-edge-release.sh` cuts on (`scripts/edge-select.sh`), so a
  shadow cannot drift from the thing it shadows; a test asserts the queue
  evaluates every gate `verify_head` arms on. Kill switches: delete
  `.github/land-queue.enabled`, or set repository variable `LAND_QUEUE=0` (only
  the literal `0` switches off) — both are enforced inside the script, so "off"
  means off from a local run too. Going live needs a GitHub App: commits pushed
  with `GITHUB_TOKEN` do not trigger `push`/`pull_request` workflows, so an
  integrate commit pushed with it would land with no CI. Self-test:
  `bash tests/workflows/test_land_queue.sh`. See
  `docs/runbooks/parallel-workspaces.md` → "Shadow land queue".
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
  For stacked PRs and release-manager queues: rebase/fix/green each PR, run only the PR-specific QA surface when the PR itself is risky, stage the queue into a release candidate, then run full Player QA + Admin QA once on the final candidate and ship only if both pass. The required engineering reviews still apply; this rule scopes only manual `/qa`.
- **Coverage floors**: Coverage must stay above the committed minimums. Two layers enforce this:
  - **Aggregate floor**: `fail_under` in `pyproject.toml` — the overall minimum.
  - **Per-module floors**: `.coverage-floors.json` — every module has its own floor. A module-level regression fails CI even if the aggregate stays above threshold.
- **CI enforcement**: `.github/workflows/quality.yml` runs `scripts/coverage-ratchet.py`:
  - On PRs: `check` mode — fails if any module dropped below its floor.
  - On main merge: `update` mode recomputes the floors and tries to commit and push
    updates. A rejected push opens one `coverage-ratchet-stale` issue with the
    computed diff and a link to the run's diagnostic; it does not infer the cause.
    For a protected branch, run `make coverage-ratchet` locally and submit the
    resulting changes through a PR. Verify the reported increases and link that PR
    with `Fixes #<issue number>`; a later no-change run cannot prove an earlier
    increase landed, so it leaves the issue open for that verification.
    A failed/skipped computation or missing result opens a separate
    `coverage-ratchet-broken` issue, closed when computation recovers even if the
    push still fails. Cancelled workflows and obsolete main snapshots do not change
    reports. Reporting API failures fail the job. Token permissions and
    branch-protection rules are not bypassed.
  - The `tests` job checks out full git history (`fetch-depth: 0`). The imaging
    pack provenance test reads generator inputs at the revision pinned in the
    manifest, which can be older than `HEAD~1`; a shallow clone cannot prove those hashes.
- **Pi aarch64 smoke**: `.github/workflows/pi-smoke.yml` is an arm64 FFmpeg
  7.x/8.x compatibility canary, not exact add-on package parity. It uses JVS as
  primary and verifies the supported BtbN fallback against GitHub's digest.
  The required `pi-smoke` check always reports; the ARM job itself runs on
  every `main` push and on PRs that touch audio, scheduling, streamer,
  startup/core configuration, launch-smoke, Python package/runtime/development
  dependencies, radio/model configuration, the changed-lanes classifier, or
  the shared Python CI setup.
- **Explainer guards**: `.github/workflows/explainer.yml` runs the
  `docs/explainer/` test suite (`npm test`), the production build
  (`npm run build`), and the four-scenario funnel end-to-end in real Chromium
  under emulated reduced motion (`npm run test:e2e`), on any PR touching
  `docs/explainer/**` or the workflow itself. The suite carries the framing
  guards that keep the page station-first plus scenario/build validation;
  the funnel test drives an actual browser, so it does reach playback wiring
  the string assertions cannot. What stays out of CI is audio judgement —
  whether a cue lands on the beat is the manual step in
  `docs/explainer/README.md`. The workflow is in its own path filter so a
  change to the CI invocation is exercised by the job it changes.
- **Local check**: `make coverage-check` to verify locally. `make coverage-ratchet` to preview what CI would commit.
- **Adding tests**: CI computes higher floors after merge. They become enforced only
  after the updated floor files land; follow any ratchet issue to finish that step.
- **Model registry watch** (`scripts/check_model_registry.py`): two questions about `model_registry.toml`, answered without provider keys. `--age` reads `[models].last_reviewed`, the day a maintainer last *decided* the pins (bump or hold; the comment beside it says which), and fails when the stamp is missing, malformed, in the future, or older than 45 days. The limit is a constant with no override on purpose: the honest escape is the stamp, refreshed only after reading the provider report. `--providers` reads the public Anthropic models overview and per-ID lifecycle table plus OpenAI's models and deprecations pages and classifies every catalog entry as `current`, `legacy`, `unlisted`, `deprecated(<date>)` or `retired`; `--gate drift` (default) fails on anything not current, `--gate liveness` only on deprecated or retired. Exit 2 means the docs could not be read or lost the anchors the parser relies on, so a broken scraper is never green. It runs as `scripts/pre-release-check.sh` section 11 (body in `scripts/model-registry-gate.sh`): review age PASS/FAIL; liveness PASS/FAIL, or WAIVED with the reason when the docs are unreachable or the checker fails. Because that script also runs in CI on any PR touching `pyproject.toml` (Dependabot included), in CI the section runs only when the diff against `origin/main` changes a version line and notes itself and skips otherwise; under `make pre-release` it always runs, and `MMR_MODEL_REGISTRY_GATE=always` forces it. It is deliberately **not** an every-PR calendar assertion: the normal pytest suite runs the gate's shell contract with committed fixtures, while only a release cut asks whether the real review stamp is still fresh. `--report` runs both checks unconditionally with one combined exit code, and `.github/workflows/model-registry-watch.yml` runs it every Monday: exit 0 closes any open `model-registry-watch` issue, exit 1 (a stale stamp, or a pin that is no longer current at its provider: legacy, unlisted, deprecated or retired) opens one or comments the week's report on it, and a run that could not read the docs, or crashed before rendering a report, does the same under a title and body naming the watcher rather than a pin, because a broken scraper that stays silent is the one outcome the check exists to prevent. Self-test: `bash tests/workflows/test_model_registry_watch.sh`. Agents never refresh the stamp to make a gate green. The cut-gate shell self-test (`tests/workflows/test_model_registry_gate.sh`) passes alternate registry and fixture paths explicitly to the sourced gate function; the production pre-release call ignores ambient overrides. Fixtures are trimmed real captures under `tests/scripts/fixtures/model_registry/`. Liveness at runtime is separate: a 404 on a retired model trips the provider backoff and the station keeps airing (PR #315).
- **Release cooldown gate**: `.github/workflows/release-cooldown.yml` blocks any `v*` tag push if the prior published release is <24h old. Bypass by adding the `hotfix` label to the PR that introduced the tagged commit. Self-test: `bash tests/workflows/test_cooldown_gate.sh` (9 cases; also runs in `quality.yml` on PRs that touch workflow/script paths, and on every push to `main`). See `docs/runbooks/ha-addon.md` and `docs/stabilization-log.md` for the measurement plan.
- **UI copy lint** (`scripts/check-ui-copy-lint.sh`): Principle #5 guard over human-facing
  strings — listener `ui_copy`, the listener/clip/admin templates, `listener.js`/`admin.js`,
  HA add-on option descriptions, and the streamer setup errors. Three rules: machine words on
  a human screen (`tech_lingo`) and copy still phrased around picking a speaker or room
  (`stale_speaker_copy`) always fail the lint. A stated failure with no next step
  (`no_way_out`) fails the lint only for copy authored as a structured table row
  (`WAY_OUT_BLOCKING_CONTEXTS`, matched on the context group, not as a string prefix) —
  copy reviewed as a set rather than written inline at a call site. Two of those groups
  give each failure its own `action` field; the other two are single authored sentences
  carrying their own remedy. That every row passes is recomputed by
  `test_every_blocking_context_row_carries_a_way_out`, not restated as a count.
  In free-text toasts it never fails the lint, because a fixed verb list cannot enumerate
  English imperatives and would flag correct copy — it is reported by `--audit`, which
  labels every row blocking or advisory, and counted against `MAX_ADVISORY_VIOLATIONS`.
  That ceiling does fail CI: free text is bounded, not unwatched. Raising it is the
  deliberate edit that says the check misjudged a string, which keeps good copy out of
  the baseline where it would look like a real violation.
  Admin/addon/server copy is held to the same banned-word list as the listener — a warmer
  register, not a shorter list.
  - Known violations are grandfathered by fingerprint in `.config/ui-copy-baseline.json`;
    CI fails on NEW ones. Refresh with `bash scripts/check-ui-copy-lint.sh --write-baseline`,
    which prints every violation it newly accepts so a refresh cannot silently absorb a
    regression. A baselined violation that no longer reproduces is also a failure — the
    baseline may only shrink. `MAX_BASELINED_VIOLATIONS` and `MAX_ADVISORY_VIOLATIONS` in
    `tests/repo/test_ui_copy_lint.py` pin both ceilings, so advisory findings cannot pile up
    unwatched just because they do not fail the build.
  - `MIN_STRINGS_PER_GROUP` is a coverage floor per context group. The lint scrapes copy out
    of templates, so an extractor that stops matching after a reformat would otherwise
    collect nothing, find no violations, and report "clean" — the floors make that a failure
    instead. A group is one surface, enforced by
    `test_each_surface_file_has_its_own_coverage_floor`: two files sharing a group would
    share a floor, and the larger file's count alone would keep the smaller one's collapse
    invisible. The two add-on translation files are the one allowed exception, since they
    are copies of each other.
  - Runs in `quality.yml` (`lint` job) beside the changelog and docs-safety lints.
    Local: `bash scripts/check-ui-copy-lint.sh` (add `--audit` for the full report; every
    flag is forwarded to `scripts/ui_copy_lint.py`).
- **Release invariants** (`scripts/check-release-invariants.sh`): runs on every PR. Catches (1) FFmpeg `music_eq_chain` equalizer count ≠ 2 (Pi aarch64 SIGABRT risk), (2) either required recovery asset (`continuity_1.mp3`, `emergency_tone.mp3`) missing, ≤1 KiB, or not recognized as audio by `ffprobe` — each checked independently, so validation never stops at the first playable asset — or any `generate_silence` reference in `producer.py`, (3) missing `_pick_canned_clip=None` test mock (empty-container / missing packaged recovery untested), (4) missing `session_stopped` test (post-restart silence untested). The manifest/hash boundary for those assets is no longer a per-asset call in this script: one Python-3.9-compatible `scripts/validate-spoken-assets.py` run covers the whole packaged demo inventory (and, with `--browser-assets-root`, the browser narration pack) before the per-asset reachability check. Local: `bash scripts/check-release-invariants.sh`.
- **Version sync check** (inline in `quality.yml`): runs on PRs that touch `pyproject.toml` or `ha-addon/mammamiradio/config.yaml`. Runs the full `scripts/pre-release-check.sh` (version consistency + CHANGELOG head + all invariants). No-ops on unrelated PRs. Local: `make pre-release`.
- **Advertised-version guard** (`scripts/check-advertised-version.sh`): asks GHCR whether the version `main` advertises to the HA Supervisor exists, for both arches, over the anonymous token endpoint (no scope needed). Home Assistant requires a prebuilt `image:` add-on's `version:` to name a real tag; when it does not, fresh installs fail and updates roll back.
  - Verdicts: a definitive 404 fails (exit 1). An unreachable registry reports `UNKNOWN` and exits 0, since a DNS blip is not a broken release. The last output line is always `VERDICT: pass|unknown|fail` — callers that need the tri-state read that, not the exit code.
  - Runs daily against `main` via `.github/workflows/advertised-version.yml`, which opens one `release-drift` issue while drift persists and closes it on a `pass`.
  - `.github/workflows/dependabot-automerge.yml` reads the verdict on Dependabot PR events, config.yaml pushes to main, hourly, and on demand. `fail` disarms and labels affected PRs `cut-window-hold`; `pass` allows verified patch/minor PR events to arm; `unknown` leaves auto-merge unchanged. The sweep only disarms. Test: `bash tests/workflows/test_dependabot_automerge_gate.sh`.
  - Kept out of `pre-release-check.sh` and `make pre-release` on purpose. `quality.yml` runs those on every version-file PR, so a `chore(release): cut X.Y.Z` PR names a version whose image is not built yet and could never go green.
  - Self-test: `bash tests/workflows/test_advertised_version.sh`, hermetic via `MAMMAMIRADIO_TAG_PROBE` plus a `curl` PATH shim. Two cases guard the deadlock from coming back across the whole cut-PR gate closure. See `docs/release-process.md`.

## Protected UI elements

These UI elements have regressed in past refactors. Always verify they survive after any HTML edit:

- **Token cost counter and cost split** (`admin.html` Engine Room) — backend computes `api_cost_estimate_usd` and `cost_breakdown` on every `/status` call. UI must display the session total and category split. The aggregate counter has disappeared twice in refactors.
- **Play button blue state** (`mammamiradio/web/static/base.css`) — `.play-btn.playing` must use `var(--ok)` (blue), never `var(--sun2)` (golden). Colorblind safety.
- **Station name localStorage** (`mammamiradio/web/static/listener.js`) — reads `stationName` from localStorage. Admin writes it. Broken when dashboard.html was rewritten.
- **Gold "Mi" accent** (`listener.html`, `admin.html`) — `<span class="mi">` in h1, styled `color: var(--sun)`. Brand signature from hero banner.
- **Italian tricolor stripe** (`admin.html` uses `.tricolor-stripe`; `listener.html` uses `.tricolor-band`) — present below h1. Must match hero banner.
- **Station name in copy** (`docs/design/system.md`): Write **Mamma Mi Radio** in prose. Use `mammamiradio` only in URLs, paths, package names, environment variables, entity IDs, and container names. `tests/web/test_ui_copy.py::test_station_brand_name_is_never_misspelled` scans tracked text files, including changelogs. Describe the typo without repeating it.
- **Admin espresso surface** (`mammamiradio/web/static/tokens.css`) — `--surface` / `--surface-strong` / `--line-strong` must remain at Pi-baseline values (`#251E19` / `#362B25` / `0.16`) so admin reads as espresso warm-brown, not washed-out taupe. Listener cards use the dedicated, brighter `--card` / `--card-strong` / `--card-line` tokens for visibility on the dark page; never raise the admin `--surface*` tokens for listener contrast (that washed admin to taupe in PR #298).

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
- Workspace archaeology: git stash refs, `local-only`, un-pushed branch inventories, references to the maintainer's machine, untracked-code discoveries, review-run tallies (`N findings raised/confirmed/refuted`)

**Where this content belongs instead:** runbooks (`docs/runbooks/`), stabilization log (`docs/stabilization-log.md`), strategic planning docs (`docs/YYYY-MM-DD-*.md`); post-mortems and workspace archaeology go to the private durable system (see Scope discipline). **Not** PR bodies and **not** GitHub issues — the same editorial boundary applies to pull-request descriptions and to maintainer-authored issues on the public tracker.

**Enforcement — two pattern sets, unioned.**

The *baseline* lives in `florianhorner/gh-workflows` and is shared by every repo. It matches the grammatical **shape** of a violation rather than a literal phrase — a review noun near a discovery verb, a review actor mid-action, a telemetry possessive, a numeric process tally, a review-artifact heading — so a paraphrase of a banned class does not walk through. A literal-phrase list cannot do this: the previous one held a pattern for `findings raised` and still passed "Findings from review". An em-dash rule ships alongside it and is now **blocking**, not advisory (gh-workflows v1.5, pinned in `.github/workflows/pr-body-lint.yml`). Two carve-outs keep it from contradicting the proof gate that runs on the same body in the same invocation: a line matching `n/a —` and any checkbox line (`- [ ]` / `- [x]`) are exempt, so a `## Proof` block never trips it. Only prose em-dashes fail.

The *project* list is `scripts/lint-patterns.sh` here (`LINT_PATTERNS`), passed to the reusable workflow as `extra_patterns`. It holds only what is specific to mammamiradio: sprint labels, cathedral vocabulary, this repo's contributor archaeology. **Overlap with the baseline is deliberate and must not be tidied away.** The three local lints below source this file *alone*, so deleting a pattern because the baseline also covers it silently drops changelog and issue coverage for it.

A repo may only ADD patterns. Suppressing a baseline pattern is a pull request to `gh-workflows`, not a local override.

**What this cannot catch.** Regex reaches lexical tells, not meaning. Metaphor carrying no lexical signal is out of reach, and renaming a review section to a neutral label defeats the heading rule, which matches the label and never the content. A passing lint is a tripwire on habitual violations, never evidence that a body is clean.

Three local lints consume `LINT_PATTERNS`:

- `scripts/check-changelog-lint.sh` — runs in `quality.yml` against `CHANGELOG.md` and `ha-addon/mammamiradio/CHANGELOG.md`.
- `scripts/check-pr-body-lint.sh` — runs in `.github/workflows/pr-body-lint.yml` against the PR body on every `opened/edited/synchronize/ready_for_review` event, plus a small set of PR-body-specific patterns for process narrative (`N commits ahead`, `picked up cleanly`, `auto-decided`, `soak verification`, `dual-voice review`, `🤖 Generated with`). The local PreToolUse hook (`~/.claude/hooks/verify-proof-block.sh`) chains it in at `gh pr create` time when the script is present in the project.
- `scripts/check-issue-body-lint.sh` — runs in `.github/workflows/issue-body-lint.yml` against maintainer-authored issue bodies on every `opened/edited` event. Outside contributors' issues are never linted (author guard in the workflow), and failures are quiet by design — a red X in the Actions tab, no bot comment, no label. The local PreToolUse hook (`~/.claude/hooks/verify-issue-body.sh`) blocks `gh issue create`/`gh issue edit` at author time when the script is present in the project. Self-test: `bash tests/workflows/test_issue_body_lint.sh` (runs in `quality.yml` on workflow/script PRs and on `main`).

To extend the rules, add a regex to `LINT_PATTERNS` in `scripts/lint-patterns.sh` — all three lints pick it up automatically.

**Patterns are POSIX ERE and must behave identically under BSD grep and GNU grep.** The local pre-publish hook resolves BSD grep on macOS; CI runs GNU grep on ubuntu-latest. No lookahead, no non-capturing groups, no `\d`, no PCRE classes. A `(?:...)` construct sat in this array for four months: BSD grep accepted it silently, GNU grep wrote a diagnostic and matched nothing, and because the lint checks grep's exit code but not its stderr, CI read the dead pattern as a clean pass. When adding a pattern, confirm it produces **empty stderr**, not merely a tolerant exit code.

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
- Post-mortems, abandoned-work archaeology, and local ref inventories (stashes,
  un-pushed branches, workspace debris) are non-public by default and follow
  the same rule: private durable system, never a public issue. The issue-body
  lint (see Changelog editorial boundary) enforces the vocabulary mechanically.
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
  in other modules; beyond that, the change is legitimately cross-cutting and
  needs its own scope statement)

Scope is checked during review. Keep each change tied to its declared objective
and write-set.

## Review discipline

For every bug fix or behavior change, do not stop at the first broken instance.

- Identify the user-visible promise or system invariant that failed.
- Check sibling code paths for the same failure mode before concluding the fix is done.
- Add or update at least one automated guard (test, validation, or build check) that would fail if the invariant breaks again.
- Coverage shows that code ran, not that the assembled output is right. Also test the point where the parts become one output, for example the list an ad break is concatenated from, not only the parts.
- For work that runs concurrently, assert which items were produced, which position each one takes, and the order the code guarantees. Never assert the order in which concurrent calls happened to run.
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

**Scenario 2 — Empty fallback:** simulate canned clips absent or invalid, norm cache empty, and no usable packaged assets in the container. Producer exception recovery must synthesize a bounded branded recovery sweeper before falling through to the emergency tone last resort. Tests that mock `_pick_canned_clip` to return a real file are hiding this class of bug.

**Scenario 3 — Post-restart:** flag files persisted from a prior run, `session_stopped` still set, HA watchdog has restarted. Test that a listener connecting AFTER a restart + stopped state still gets audio.

This rule was added after 4 production silence incidents caused by untested Scenarios 2 and 3. The test suite was proving features worked; it was not proving the product worked under real conditions (Pi hardware, container filesystem, HA watchdog restarts).

## Perceptual features ship their A/B test first

Any feature whose only output is a perceptual change — audio colour, a visual
tweak, anything a human is supposed to *notice* — must ship the means to perceive
and compare it (an on-demand A/B, a side-by-side, a before/after sample) in the
SAME change, before or alongside the feature itself. If you cannot hear or see the
difference on demand, you cannot tune it, defend it, or catch its regressions, and
neither can an operator.

This rule was added 2026-06-18 after the FM "broadcast chain": a subtle always-on
egress colour that was (1) inaudible on the maker's own speakers, (2) impossible to
A/B because the live toggle only affected segments past a 5-15 min lookahead queue,
and (3) only ever audible as a stereo "binaural swirl" bug. The fix's own review
then found the chain was only loudness-neutral by accident. A feature you cannot
hear-test is unfalsifiable: it hides its own bugs until a listener finds them, and
it rots because no one can tell whether it still does anything. The corollary: when
an operator-facing control changes something subtle or imperceptible, its UI copy
must say so plainly (what it does, how subtle it is, where it's most noticeable) —
an honest "you may not hear a difference" beats a confident label nobody can verify.

### Sonic identity must translate beyond full-range speakers

Keep a default station signature recognizable on phones, laptops, small smart
speakers, and ordinary car systems. Bass may reinforce the cue on full-range
equipment. It must not carry the identifying rhythm, contour, or answer by
itself. Keep the identifying energy in the midrange. Test it on a small speaker
as well as the Mac and target speaker.

Modern Night Drive uses **Neon Relay** as its station signature. **Velvet
Horizon** sets the atmosphere for separately authored transitions. Use it as a
production style. Reserve the station signature for Neon Relay, and do not
reuse the Velvet Horizon prototype as transition material.

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
- shell: CI enforces shellcheck scripts/*.sh; broader local sweep: shellcheck $(find . -name "*.sh" -not -path "./.venv/*" -not -path "./.git/*" -not -path "./.claude/skills/*")

## No-touch zones

Two zones are mechanically enforced. Read `CONTRACT.md` before touching anything near them.

**Z-contract — the frozen v1 integration surface.** Do not edit:

- `mammamiradio/integrations/schema.py`, `serializer.py`, `now_playing.py`
- `tests/integrations/**` (contract tests AND the golden fixture `tests/integrations/golden/v1_now_playing.json`)
- `CONTRACT.md` and `.github/workflows/contract-drift.yml` (the rules and the gate are gated too)
- The `/api/integrations/v1/now-playing` endpoint path, its ETag/304 semantics, or `schema_version="1"`

A wire-visible change routed through any other file (e.g. `core/models.py`) is still a contract change — the contract-drift CI renders the serializer on every PR and catches payload drift; the frozen pytest contract tests (run by the quality workflow) hold the route path, ETag/304, and header behavior. Wanted changes go into the proposals queue: write `docs/contract-proposals/NNN-title.md` (format in that directory's README) and stop. Changes land only when the maintainer opens a contract window (the `.contract-window` marker + a review sitting) and the PR carries a `Contract-Change:` trailer or PR-body line. Never weaken a contract test to make something pass.

**Z-comms — GitHub writes to non-owned repos.** Never run `gh pr comment/review/edit/merge`, `gh issue comment`, or mutating `gh api` calls against any repo not owned by `florianhorner`. This includes flagless invocations inside an upstream checkout. Draft the reply to `.context/pr-replies/*.md` instead; the maintainer copies and sends. Always.

<!-- BEGIN: commit-message-standards (managed by bootstrap-repo.sh — do not hand-edit) -->
## Commit message standards

This repo follows the [engineering-standards commit-message spec](https://github.com/florianhorner/engineering-standards/blob/main/specs/commit-message-spec.md).

**Quick rule:** Conventional Commits (`type(scope): subject`, ≤72 chars). A `Why:` body line is REQUIRED when type is `feat` AND >50 lines changed; otherwise optional.

**Local invocation:** Use the `/commit` skill in Claude Code / Conductor. Default behavior is dry-run (drafts a message and shows the validator output without committing); pass `--commit` to actually create the commit. Manual `git commit` works too — the local `commit-msg` hook validates either path.

**Per-repo cheat sheet:** [`./CONTRIBUTING.md`](./CONTRIBUTING.md) carries the 30-second cheat sheet, good/bad examples, banned patterns, exempt subjects, bot allowlist, and bypass policy. It is self-sufficient for cloud agents (Claude Code Cloud, Codex web) that only see repo-local files.

**Machine-readable rules:** [`.config/commit-rules.json`](.config/commit-rules.json) is a SHA-pinned vendored copy of the upstream `commit-rules.json`. The validator binary, commit-msg hook, and CI workflow all read this file. Do not hand-edit — re-run `bootstrap-repo.sh` to refresh.

**Bypass:** `git commit --no-verify` requires a `Policy-Override: <reason>` trailer to pass CI. Logged to `~/.commit-bypass.log` by the pre-push hook.
<!-- END: commit-message-standards -->
