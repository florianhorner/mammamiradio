# mammamiradio

AI-powered Italian radio station engine. Python 3.11+, FastAPI, FFmpeg, optional Home Assistant integration.

## Leadership Principles

Every proposal (architecture, feature, fix, deployment plan) must pass all five, in order:

**1. NEVER BREAK THE ILLUSION**
The listener must always believe they are hearing a real radio station. Dead air, abrupt cuts, silence gaps, or anything that exposes the machine behind the curtain is a product failure. A change that risks the illusion for a live listener needs a mitigation before shipping.

**2. INSTANT AUDIO**
A connecting listener hears sound within 1–2 seconds, every time: no exceptions for cold starts, session resumes, idle wakeups, or addon restarts. Every connect path needs an immediate audio source (pre-normalized track, canned clip, anything). Build the bridge first, fix root causes second.

**3. NO LIVE SURGERY ON PRODUCTION SYSTEMS**
The only legitimate addon code-change path is `branch → PR → merge → CI builds image → addon updates`. No `docker cp`, no `pkill`, no live config edits, no addon restarts without explicit confirmation in the current message. Sleeping humans depend on the system. Full rule: **Production Systems Discipline — HARD STOP** below.

**4. THE README IS THE PITCH**
A new reader must get it in 30 seconds or less (a KPI, not an aspiration); needing scrolling, paragraphs of context, or a glossary before the product clicks is failure. The first viewport carries the entire pitch: what it is, what makes it different, what the reader does next. The repo meets the same standard: the folder hierarchy IS the mental model, and a feature whose home cannot be found in 30 seconds means the structure failed.

**5. SPEAK HUMAN, ALWAYS WITH A WAY OUT**
Every word a listener or operator reads is product copy, not a log line. No tech lingo reaches a human screen: "rate limit", "buffer empty", "429", "timeout", "rejected", "degraded" are machine words for logs, never the UI; use warm language a non-technical person understands. Every error must also say how to fix it, with a concrete next step ("give the tape decks a few seconds and tap again"); a failure stated without a way forward is a bug, not a message. This applies to every human-facing surface. Register: the listener UI speaks in the station's full in-character voice (Italian-first under Super Italian Mode), the admin UI in warm plain English (admin-localization rule); same no-lingo, always-a-fix standard.

## Production Systems Discipline — HARD STOP

**No live surgery on the HA Green.** The only legitimate code-change path for the mammamiradio addon is `branch → PR → merge → CI builds image → addon updates to new image`. The restart happens once, planned, when the addon updates: not during the day, not as an experiment, not to "test the fix live".

**Default HA development target:** First Listen and other Home Assistant-facing branch work runs against `scripts/first-listen-lab.sh`, the disposable local HA Container + VLC speaker lab (`docs/runbooks/first-listen-local-ha.md`). Never connect branch code to the live home, reuse its token or backup, or attach household/cloud/MQTT devices unless the maintainer explicitly authorizes that live action in the current message. Lab runtime and credentials live only under gitignored `tmp/first-listen-ha-lab/`, never in `.context/` or tracked files.

**NEVER do any of these against the running mammamiradio addon without the maintainer's explicit confirmation in the current message:**

1. **No live code patching.** `docker cp` into the container, `docker exec` writes (`sh -c "cat > ..."`, `tee`, `echo >`, `sed -i`, any redirection into a file), or any other in-container file edit: wiped on the next restart, and it masks the real state of production.
2. **No process signals.** `pkill`, `kill`, `killall`, `docker kill`, `docker restart` on any process inside the addon. s6-rc here does NOT reliably auto-restart killed services: a killed process kills the container, which kills the stream.
3. **No addon restarts.** `ha apps restart`, `ha apps stop`, `docker restart addon_*_mammamiradio`, `supervisor restart`: ask first. Exception: recovering an already-broken state (container exited, stream dead) is correct, but announce it first.
4. **No live config edits.** Writing to `/config/` from a shell, editing `radio.toml` on the Pi, changing addon options via CLI instead of the HA addon config UI.
5. **No ad-hoc Docker image changes.** Building, tagging, or pushing addon images from the Pi; images come from CI only.
6. **No volume mounts introduced for debugging.** Mounting host paths into the container to sidestep the rebuild chain.

**Legitimate operations (no confirmation needed):** `docker ps`, `docker logs`, `docker exec <container> <read-only command>` (cat, ls, grep, env, ps); `ha apps info`, `ha apps list`, `ha apps logs`; `ha apps start <slug>` when the addon is already stopped/exited (recovery, not experiment).

**When a fix is urgent:** merge → wait ~5–10 min for CI → update addon: faster than live surgery, permanent, and one planned restart beats repeated unplanned drops.

## Docs

Root docs, one job each: `README.md` (product pitch and operator quick start), `CONTRIBUTING.md` (local setup, tests, smoke checks), `CLAUDE.md` (agent rules and leadership principles, this file), `CHANGELOG.md` (release notes), `CONTRACT.md` (frozen v1 integration surface and change ceremony). Everything else, Docker and Home Assistant packaging included, is indexed in `docs/REPO_MAP.md`; the add-on release process is `docs/runbooks/ha-addon.md`.

There is no canonical tracked TODO file. Do not create or revive `TODO.md`, `TODOS.md`, `docs/todos.md`, `docs/backlog.md`, or similar catch-all backlog files. Use GitHub issues for public engineering work and a private durable system for strategy or relationship context.

## Commands

Environment: the exact pip sequence in [Local setup](CONTRIBUTING.md#local-setup) (dev tools, hash-locked runtime, `--no-deps -e .`, `pip check`). uv does not manage `.venv` (`[tool.uv] managed = false` in `pyproject.toml`; `uv sync`, `uv lock` and `uv add` refuse on purpose): never remove the setting.

- Full local stack: `./start.sh`; app only: `source .venv/bin/activate && python -m uvicorn mammamiradio.main:app --reload --reload-dir mammamiradio`
- Test: `pytest tests/` or `make test` (with coverage); `make test-watch` re-runs on save
- Lint, format, types: `ruff check .` (`--fix`), `ruff format .` (`--check`), `mypy mammamiradio/ tests/`
- All checks: `make check` (media check, lint, format check mirroring CI, typecheck, deadcode, coverage gate with per-module floors)
- Pre-commit hooks (pre-commit, pre-push, commit-msg): install per `CONTRIBUTING.md`
- **Validate addon before push**: `./scripts/validate-addon.sh` (`--build` adds the local Docker build test)
- First Listen against the disposable local HA: `scripts/first-listen-lab.sh start`
- Release cooldown self-test: `bash tests/workflows/test_cooldown_gate.sh` (9 scenarios; CI runs it on the changed-lanes `workflows` lane via `scripts/ci-changed-lanes.sh` and on every push to `main`)

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

The folder hierarchy IS the mental model (leadership principle #4); the single-page "where does X live" map is `docs/REPO_MAP.md`.

- `mammamiradio/main.py` public entry (startup/shutdown); `core/` config, models, capabilities, setup, First Listen, SQLite schema; `audio/` FFmpeg normalizer, quality gate, TTS, voices
- `playlist/` sources, downloader, song cues, track rules; `hosts/` scriptwriter, persona, context cues, ad creative; `home/` HA context, speakers, event enrichment, generated labels
- `scheduling/` producer, scheduler, clip ring buffer; `web/` streamer, auth, ingress pages, listener requests, og card, templates, static
- `media/` attribution-only starter catalog gate; `integrations/` frozen v1 now-playing contract (see `CONTRACT.md`); `assets/` demo MP3s, SFX, logo
- package-root `release_campaign.py` and `restart_handoff.py`; `radio.toml` station config; `start.sh` dev entrypoint
- `tests/` mirrors `mammamiradio/` as `tests/<module>/test_*.py`

`web/streamer.py` owns routes and playback; `hosts/scriptwriter.py` assembles host scripts. Scope any further extraction explicitly and follow `docs/runbooks/refactor-cuts.md`.

## Design System

Always read `docs/design/system.md` before making any visual or UI decision; all font choices, colors, spacing, and aesthetic direction are defined there. Do not deviate without explicit user approval. In QA mode, flag any code that does not match `docs/design/system.md`.

## Brand assets

- `mammamiradio/assets/logo.svg` is the canonical vector; every raster (HA add-on icon and logo PNGs) is regenerated from it with the commands in `docs/design/system.md` "File locations", whose table also maps the hero banner, social card, hero composite template, and favicon.
- `docs/design/system.md` owns palette, typography, components, motion, and anti-patterns.
- `mammamiradio/web/static/favicon.svg` stays byte-for-byte identical to the logo (`tests/web/test_route_smoke.py` enforces it); the browser fallback route is `/favicon.ico`.

## Brand safety — hard rule

**All ad brands in `radio.toml` must be fictional.** Never add a real company, product, or registered trademark to `[[ads.brands]]`: names, taglines, slogans, and campaign copy included. The scriptwriter writes false claims and satire in the brand's voice; with a real brand that is trademark infringement and false advertising exposure, pharma brands (regulatory risk) included.

**The test:** google the brand name. If it returns a real company, it does not belong in `radio.toml`. Invented names that sound Italian and absurd are correct. Names that are one letter off a real brand (e.g. "Barella" for Barilla) also fail — the intent to deceive is implicit in the similarity.

## Notes for future edits

- `admin.html`, `listener.html` and `clip.html` live in `mammamiradio/web/templates/`, loaded by `mammamiradio/web/streamer.py`. **Before adding a UI feature to a template, verify which route serves it** from the `@router.get` definitions in `web/streamer.py`, not the filename: `/` serves `listener.html`, except under HA ingress from hassio/loopback, where it renders the admin page; `/listen` is a listener alias; `/admin` serves `admin.html`; `/dashboard` only redirects to `/admin`.
- `start.sh` is part of the runtime contract, not just a convenience script.
- `radio.toml` is the source of truth for hosts, pacing, ad brands, audio settings, and Home Assistant enablement. Secrets stay in `.env`.
- Changing routes, config keys, auth rules, or fallback behavior means updating the matching docs in the same change (**Doc sync** below).
- `scripts/conductor-*.sh` define Conductor workspace setup/run/archive; the committed `.conductor/settings.toml` wires those hooks for new local and cloud workspaces once the change reaches the default branch. Machine-local `.conductor/settings.local.toml` takes precedence, so do not duplicate lifecycle hooks there. Keep `.context/` runtime state out of git.
- A live stream is never stopped, restarted, or reloaded unless the user explicitly asks. Protect the illusion first.
- Treat 60 minutes of uninterrupted runtime per live station object as the default minimum when tinkering around an active stream.
- Starter catalog: the canonical manifest, source evidence, human audition record, and media proof change together, never with unverified demo or fallback music, and `LOCKED_TRACKS` in `tests/media/test_starter.py` pins the exact crate so a swap is a deliberate test change. Process: `docs/music-sources.md` "Bundled starter collection".
- Bundled music is attribution-only, a hard boundary: NonCommercial, NoDerivatives, and ShareAlike are refused by name in `mammamiradio/media/starter.py`, the acquire tool, and the manifest check. Providers and why ND disqualifies otherwise perfect tracks: `docs/music-sources.md`.
- Advertisements need a convincing underlying sound bed: prefer CC-free music or sound design beds under ad voiceovers to dry voice-only spots.

## Quality gates

- **Open via `/ship`, never a bare `gh pr create`**: review is mandatory (adversarial + test-coverage + docs/config-consistency), including re-review after fixes or conflicted/hand-edited integrations. Review-receipt admission is retired (historical receipts and the standalone verifier remain, inert for ordinary PRs); the hook still denies raw merges. Physical HA evidence is a separate, unchanged release gate.
- **Landing contract**: human and feature PRs merge only through `scripts/land-pr.sh`; the full contract (behind/conflict refusal, bot-thread gates, head-pinned auto-merge, Dependabot exception, freeze/thaw) lives in docs/runbooks/ha-addon.md "Landing a PR".
- **QA gates (mandatory, risk-scoped)**: Manual `/qa` is required for the surfaces a PR can affect, and every release candidate must pass both surfaces before user-facing release.
  1. **Player QA** (`/qa` on `/` dashboard) is required for listener-facing changes: stream playback, now-playing, up-next, Casa card, song requests, clip sharing, public status, listener routes/assets, or playback-visible behavior.
  2. **Admin QA** (`/qa` on `/admin`) is required for operator-facing changes: controls, pacing sliders, host config, key management, engine room, playlist management, admin routes/assets, or operator feedback.
  3. PRs affecting both surfaces, shared auth/routing/frontend state, or uncertain user-facing behavior require both.
  4. Docs-only, tests-only, CI-only, dependency-only, and pure internal refactors may skip manual PR QA when automated checks pass and the PR states why no user-facing surface is affected.
  5. Coordinated batches, release-manager queues, edge releases, and stable releases must pass both Player QA and Admin QA on the final candidate state before shipping.
  6. QA may be reused only when the later diff cannot affect that surface; link or name the reused QA result and explain why it remains valid.
  A single combined rushed QA run is still insufficient. Do not claim QA passed unless that exact QA scope ran or was explicitly reused under this rule.
- **QA Impact**: every PR carries the `## QA Impact` block from the PR template so the gate above is auditable. Stacked PRs and release-manager queues: rebase/fix/green each PR, run only the PR-specific QA surface when that PR is risky, stage the queue into a release candidate, then run full Player QA + Admin QA once on the final candidate and ship only if both pass. Engineering reviews still apply; this rule scopes only manual `/qa`.
- **Model registry watch**: `scripts/check_model_registry.py` guards the review stamp and pin liveness (contract in docs/release-process.md). Agents never refresh the `last_reviewed` stamp to make a gate green.
- Coverage floors and ratchet, Pi aarch64 smoke canary, UI copy lint: `CONTRIBUTING.md`; advertised-version guard: `docs/release-process.md`; release invariants, version sync, release cooldown: `docs/runbooks/ha-addon.md`; shadow land queue: `docs/runbooks/parallel-workspaces.md`; explainer guards: `docs/explainer/README.md`.

## Protected UI elements

These elements have regressed in past refactors. Verify they survive any HTML edit, and grep for them before committing:

- **Token cost counter and cost split** (`admin.html` Engine Room): the backend computes `api_cost_estimate_usd` and `cost_breakdown` on every `/status` call; the UI must display the session total and category split.
- **Play button blue state** (`mammamiradio/web/static/base.css`): `.play-btn.playing` must use `var(--ok)` (blue), never `var(--sun2)` (golden). Colorblind safety.
- **Station name localStorage** (`mammamiradio/web/static/listener.js`): reads `stationName` from localStorage; admin writes it.
- **Gold "Mi" accent** (`listener.html`, `admin.html`): `<span class="mi">` in h1, styled `color: var(--sun)`. Brand signature from the hero banner.
- **Italian tricolor stripe** (`admin.html` uses `.tricolor-stripe`; `listener.html` uses `.tricolor-band`): present below h1, matching the hero banner.
- **Station name in copy** (`docs/design/system.md`): write **Mamma Mi Radio** in prose; use `mammamiradio` only in URLs, paths, package names, environment variables, entity IDs, and container names. `tests/web/test_ui_copy.py::test_station_brand_name_is_never_misspelled` scans tracked text files, changelogs included. Describe the typo without repeating it.
- **Admin espresso surface** (`mammamiradio/web/static/tokens.css`): `--surface` / `--surface-strong` / `--line-strong` must remain at the Pi-baseline values (`#251E19` / `#362B25` / `0.16`) so admin reads as espresso warm-brown, not washed-out taupe. Listener cards use the dedicated, brighter `--card` / `--card-strong` / `--card-line` tokens for visibility on the dark page; never raise the admin `--surface*` tokens for listener contrast.

## Doc sync

**Any change to a route, config key, env var, auth rule, or fallback path must update at least one of the following docs in the same commit:**

`README.md`, `docs/architecture.md`, `docs/troubleshooting.md`, `docs/operations.md`, `CLAUDE.md`, `CHANGELOG.md`

If the behavior changed and the docs didn't, the docs are wrong. Fix them in the same change, not a follow-up.

## Changelog editorial boundary

`CHANGELOG.md` and `ha-addon/mammamiradio/CHANGELOG.md` are PUBLIC release notes for users, operators, and contributors: what changed and why it matters, never an internal sprint log.

**Never write into the public changelogs:**

- Sprint / workstream labels: `WS2`, `WS3-A`, `PR-A`, `PR-B/5`, `Phase A`, `Phase 1`, `Approach B`
- Finding numbers: `finding #8`, `Item 19`, `(P0-1)`, `(M1)`, `(H2/H3)`
- AI tool provenance: `/autoplan`, `codex review`, `Claude review`, `Conductor session`, `codex independent review`
- Planning archaeology: `soak window`, `live session`, `2026-04-17 live session`, references to `docs/YYYY-MM-DD-*.md` planning files
- Architectural metaphors as labels: `cathedral`, `domain naves`, `sacred files`, `god-module`, `leadership principle`, `operator-honesty`
- Contributor archaeology: `first outside contribution`, `work was superseded`
- Workspace archaeology: git stash refs, `local-only`, un-pushed branch inventories, references to the maintainer's machine, untracked-code discoveries, review-run tallies (`N findings raised/confirmed/refuted`)

**Where it belongs instead:** runbooks (`docs/runbooks/`), the stabilization log (`docs/stabilization-log.md`), strategic planning docs (`docs/YYYY-MM-DD-*.md`); post-mortems and workspace archaeology go to the private durable system (see Scope discipline). **Not** PR bodies and **not** GitHub issues: the same boundary covers pull-request descriptions and maintainer-authored issues on the public tracker.

**Enforcement:** two pattern sets, unioned. The shared *baseline* in `florianhorner/gh-workflows` (shape-matching patterns plus a blocking em-dash rule; since gh-workflows#18 it ignores the two-line footer a cloud agent session appends to the very end of a PR or issue body, while the same words anywhere else, and in every changelog, still fail) and the *project* list `LINT_PATTERNS` in `scripts/lint-patterns.sh`, which the changelog, PR-body, and issue-body lints source alone, so overlap with the baseline is deliberate and must not be tidied away. A repo may only ADD patterns: a regex added to `LINT_PATTERNS` reaches all three lints. Patterns are POSIX ERE and must behave identically under BSD grep (local hook, macOS) and GNU grep (CI): no lookahead, no non-capturing groups, no `\d`, no PCRE classes, and a new pattern must produce **empty stderr**, not merely a tolerant exit code, since the lint checks the exit code only and a dead pattern reads as a clean pass. Full mechanics: `docs/runbooks/parallel-workspaces.md` "PR body and changelog lint mechanics".

## Scope discipline

Two narrow rules:

**1. Planning docs ship in their own PR.** Strategic planning documents (multi-week plans, design proposals, retrospectives, files matching `docs/YYYY-MM-DD-*.md` or `docs/2026-*-plan.md`, post-mortems) must NOT be bundled into fix/feat PRs. They get their own PR with their own scope.

**2. When you stumble on an adjacent issue not in scope:** classify it before writing anything down.

- Public, actionable engineering work goes into a GitHub issue or feature request with a narrow title, user-visible impact, implementation scope, acceptance criteria, and relevant file references; link it from the PR only when that helps reviewers see why the adjacent work is parked.
- Non-public strategy, outreach, relationship context, pitch framing, timing gates, or personal writing plans never go into tracked repo files or public GitHub issues: private durable system outside the repo.
- Post-mortems, abandoned-work archaeology, and local ref inventories (stashes, un-pushed branches, workspace debris) are non-public by default: private durable system, never a public issue; the issue-body lint (see Changelog editorial boundary) enforces the vocabulary.
- No private durable system in the current session: stop and ask where the note should live instead of creating a tracked TODO file or hiding durable strategy in workspace-local `.context/`.
- Already-shipped items are removed from backlog surfaces, not moved into another open list.

Do NOT create or append to `TODO.md`, `TODOS.md`, `docs/todos.md`, `docs/backlog.md`, or equivalent tracked catch-all backlog files. A scoped planning document is allowed only when it is itself the PR deliverable, never as a sidecar parking lot for unrelated work.

**Exemptions** (not scope creep): doc updates the **Doc sync** rule requires (same-commit, mandatory); test files mirroring source changes (`tests/X/**` alongside `mammamiradio/X/**`); mechanical fallout from renames (path-string updates, import fixups) in the same PR as the rename; sibling caller updates when a public function signature changes (≤3 files in other modules; beyond that the change is legitimately cross-cutting and needs its own scope statement).

Scope is checked during review. Keep each change tied to its declared objective and write-set.

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

For behavior-preserving refactors that MOVE a symbol between modules, run these checks at SCOPE time, before naming what moves, not only during execution:

- **Whole-repo symbol grep.** Grep every moved symbol across the ENTIRE repo, including `scripts/`, `.github/workflows/`, and `docs/`, not just `mammamiradio/` + `tests/`. CI guards and shell scripts hardcode symbol names; a move that ignores them red-fails the build.
- **Dependency-closure gate.** Verify every symbol the move-target calls that is SHARED with code staying behind or destined for another cut. A target that reaches a shared primitive must not move until that primitive's home is settled.
- **Read the test bodies, not just grep counts.** Counts miss pre-existing duplication.

Full per-cut checklist + cut-by-cut lessons: `docs/runbooks/refactor-cuts.md`.

## Audio delivery test coverage rule

Every PR touching audio delivery (producer, streamer, normalizer, any bridge/fallback path) must include tests for all three scenarios. Missing any one = untested blast radius, do not ship.

**Scenario 1 — Normal:** feature works as designed.

**Scenario 2 — Empty fallback:** canned clips absent or invalid, norm cache empty, no usable packaged assets in the container. Producer exception recovery must synthesize a bounded branded recovery sweeper before falling through to the emergency tone last resort. Tests that mock `_pick_canned_clip` to return a real file hide this class of bug.

**Scenario 3 — Post-restart:** flag files persisted from a prior run, `session_stopped` still set, HA watchdog has restarted. A listener connecting AFTER a restart + stopped state must still get audio.

Added after four production silence incidents from untested Scenarios 2 and 3: the suite proved features worked, not that the product worked on Pi hardware, the container filesystem, and HA watchdog restarts.

## Perceptual features ship their A/B test first

Any feature whose only output is a perceptual change (audio colour, a visual tweak, anything a human is supposed to *notice*) must ship the means to perceive and compare it (an on-demand A/B, a side-by-side, a before/after sample) in the SAME change, before or alongside the feature itself. If you cannot hear or see the difference on demand, you cannot tune it, defend it, or catch its regressions, and neither can an operator. Added after an egress colour shipped without an A/B and hid its own bugs. Corollary: when an operator-facing control changes something subtle or imperceptible, its UI copy must say so plainly (what it does, how subtle it is, where it is most noticeable); an honest "you may not hear a difference" beats a confident label nobody can verify.

Sonic identity (a station signature must stay recognizable on small speakers, with the identifying energy in the midrange; Neon Relay is the signature, Velvet Horizon a transition style only) is a design-system rule: `docs/design/system.md` "Sonic identity".

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it with the Skill tool as your FIRST action. Do NOT answer directly, do NOT use other tools first: the skill's workflow beats an ad-hoc answer.

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

**Merge-first, refactor-second.** An open, approved first-time outside contributor PR merges BEFORE any maintainer work touches overlapping files; follow-on refactoring goes in a separate PR after their merge lands. The "Merged" badge on their PR page is part of the experience we owe them. Credit channels: the Contributors list in `CONTRIBUTING.md` and the Contributing section of `README.md`.

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
- shell: CI enforces shellcheck scripts/*.sh; broader local sweep: shellcheck $(find . -name "*.sh" -not -path "./.venv/*" -not -path "./.git/*")

## No-touch zones

Two zones are mechanically enforced. Read `CONTRACT.md` before touching anything near them.

**Z-contract — the frozen v1 integration surface.** Do not edit:

- `mammamiradio/integrations/schema.py`, `serializer.py`, `now_playing.py`
- `tests/integrations/**` (contract tests AND the golden fixture `tests/integrations/golden/v1_now_playing.json`)
- `CONTRACT.md` and `.github/workflows/contract-drift.yml` (the rules and the gate are gated too)
- The `/api/integrations/v1/now-playing` endpoint path, its ETag/304 semantics, or `schema_version="1"`

A wire-visible change routed through any other file (e.g. `core/models.py`) is still a contract change; the contract-drift CI catches payload drift and the frozen pytest contract tests hold route path, ETag/304, and headers (mechanics in `CONTRACT.md`). Wanted changes go into the proposals queue: write `docs/contract-proposals/NNN-title.md` (format in that directory's README) and stop. Changes land only when the maintainer opens a contract window (the `.contract-window` marker + a review sitting) and the PR carries a `Contract-Change:` trailer or PR-body line. Never weaken a contract test to make something pass.

**Z-comms — GitHub writes to non-owned repos.** Never run `gh pr comment/review/edit/merge`, `gh issue comment`, or mutating `gh api` calls against any repo not owned by `florianhorner`, including flagless invocations inside an upstream checkout. Draft the reply to `.context/pr-replies/*.md`; the maintainer copies and sends. Always.

<!-- BEGIN: commit-message-standards (managed by bootstrap-repo.sh — do not hand-edit) -->
## Commit message standards

This repo follows the [engineering-standards commit-message spec](https://github.com/florianhorner/engineering-standards/blob/main/specs/commit-message-spec.md).

**Quick rule:** Conventional Commits (`type(scope): subject`, ≤72 chars). A `Why:` body line is REQUIRED when type is `feat` AND >50 lines changed; otherwise optional.

**Local invocation:** Use the `/commit` skill in Claude Code / Conductor. Default behavior is dry-run (drafts a message and shows the validator output without committing); pass `--commit` to actually create the commit. Manual `git commit` works too — the local `commit-msg` hook validates either path.

**Per-repo cheat sheet:** [`./CONTRIBUTING.md`](./CONTRIBUTING.md) carries the 30-second cheat sheet, good/bad examples, banned patterns, exempt subjects, bot allowlist, and bypass policy. It is self-sufficient for cloud agents (Claude Code Cloud, Codex web) that only see repo-local files.

**Machine-readable rules:** [`.config/commit-rules.json`](.config/commit-rules.json) is a SHA-pinned vendored copy of the upstream `commit-rules.json`. The validator binary, commit-msg hook, and CI workflow all read this file. Do not hand-edit — re-run `bootstrap-repo.sh` to refresh.

**Bypass:** `git commit --no-verify` requires a `Policy-Override: <reason>` trailer to pass CI. Logged to `~/.commit-bypass.log` by the pre-push hook.
<!-- END: commit-message-standards -->
