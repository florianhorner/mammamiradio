# Cathedral Restructure — Repo Hierarchy as Mental Model

**Date:** 2026-04-28
**Status:** Plan — not yet implemented
**Mode:** Shape B (Naves first)
**Author:** /plan-devex-review session
**Leadership principle invoked:** #4 — *The README is the pitch. The folder hierarchy is the mental model. 30 seconds or we failed.*

---

## Problem

The 5-second test fails today. Hard numbers:

- **27 flat `.py` files** under `mammamiradio/` — no domain grouping
- **61 flat test files** under `tests/` — mirrors no source structure
- **14 markdown docs** at repo root — reading-order ambiguous
- `streamer.py` = **2,395 LOC, 89 def/class** (HTTP routes + auth + playback loop + admin + public status — single god module)
- `scriptwriter.py` = **1,488 LOC, 24 def/class** (banter + ads + LLM calls + fallback)
- HTML templates and `static/` live **inside the Python package** — packaging confusion
- 8 candidate files for "where does banter live?" (`scriptwriter`, `persona`, `scheduler`, `song_cues`, `context_cues`, `ad_creative`, `track_rationale`, `track_rules`) with no hierarchy hints

Prior audit `docs/2026-04-16-documentation-structure-audit.md` flagged the doc drift twelve days ago. Hierarchy was not addressed.

## Goal

A new contributor (or future-Florian / cold coding agent) lands on the repo and understands the layout in 30 seconds:

- One `README.md` first-viewport pitch (≤25 lines visible without scroll) ending in a link to the repo map
- One `REPO_MAP.md` — a single table answering "I want to fix X, where do I look?"
- A source tree where every `.py` file's location IS its mental category
- Tests that mirror source 1:1 so "find the test for X" is a deterministic path
- Docs collapsed: 4 sacred files at root, everything else under `docs/`

## Non-goals (Shape B excludes these)

- Splitting `streamer.py` (2,395 LOC) — stays in `web/` as one file with a `# TODO: split` marker. Follow-up PR.
- Splitting `scriptwriter.py` (1,488 LOC) — stays in `hosts/` as one file with the same marker. Follow-up PR.
- Renaming any module (only moves)
- Changing public import paths used by the HA addon entrypoint (`mammamiradio.main:app` stays put)
- Refactoring functions or behavior — pure structural moves

If a PR reviewer finds themselves discussing logic, they're in the wrong PR.

---

## Target tree

```
mammamiradio/
├── __init__.py
├── main.py                       # FastAPI lifecycle (kept at top — public entry)
│
├── core/
│   ├── __init__.py
│   ├── config.py                 # was: mammamiradio/config.py
│   ├── models.py                 # was: mammamiradio/models.py
│   ├── capabilities.py           # was: mammamiradio/capabilities.py
│   ├── setup_status.py           # was: mammamiradio/setup_status.py
│   └── sync.py                   # was: mammamiradio/sync.py
│
├── audio/
│   ├── __init__.py
│   ├── normalizer.py             # was: mammamiradio/normalizer.py
│   ├── audio_quality.py          # was: mammamiradio/audio_quality.py
│   ├── tts.py                    # was: mammamiradio/tts.py
│   └── voice_catalog.py          # was: mammamiradio/voice_catalog.py
│
├── playlist/
│   ├── __init__.py
│   ├── playlist.py               # was: mammamiradio/playlist.py
│   ├── downloader.py             # was: mammamiradio/downloader.py
│   ├── song_cues.py              # was: mammamiradio/song_cues.py
│   ├── track_rationale.py        # was: mammamiradio/track_rationale.py
│   └── track_rules.py            # was: mammamiradio/track_rules.py
│
├── hosts/
│   ├── __init__.py
│   ├── scriptwriter.py           # was: mammamiradio/scriptwriter.py (still 1488 LOC, # TODO: split)
│   ├── persona.py                # was: mammamiradio/persona.py
│   ├── context_cues.py           # was: mammamiradio/context_cues.py
│   └── ad_creative.py            # was: mammamiradio/ad_creative.py
│
├── home/
│   ├── __init__.py
│   ├── ha_context.py             # was: mammamiradio/ha_context.py
│   └── ha_enrichment.py          # was: mammamiradio/ha_enrichment.py
│
├── scheduling/
│   ├── __init__.py
│   ├── producer.py               # was: mammamiradio/producer.py
│   ├── scheduler.py              # was: mammamiradio/scheduler.py
│   └── clip.py                   # was: mammamiradio/clip.py
│
├── web/
│   ├── __init__.py
│   ├── streamer.py               # was: mammamiradio/streamer.py (still 2395 LOC, # TODO: split)
│   ├── og_card.py                # was: mammamiradio/og_card.py
│   ├── templates/
│   │   ├── admin.html            # was: mammamiradio/admin.html
│   │   ├── listener.html         # was: mammamiradio/listener.html
│   │   ├── live.html             # was: mammamiradio/live.html
│   │   └── regia.html            # was: mammamiradio/regia.html
│   └── static/                   # was: mammamiradio/static/  (moved as a tree)
│       ├── base.css
│       ├── tokens.css
│       ├── listener.css
│       ├── listener.js
│       ├── waveform.js
│       ├── manifest.json
│       ├── sw.js
│       ├── icon-192.svg
│       ├── icon-512.svg
│       └── icon-maskable.svg
│
├── assets/                       # was: mammamiradio/demo_assets/ + mammamiradio/logo.svg
│   ├── logo.svg
│   └── demo/
│       ├── sfx/studio/...
│       └── welcome/...
│
└── (no other top-level files inside the package)
```

Seven naves: `core`, `audio`, `playlist`, `hosts`, `home`, `scheduling`, `web`. One `assets/`. One `main.py`. That is the entire mammamiradio package surface.

### Tests mirror source

```
tests/
├── conftest.py
├── fixtures/
├── core/
│   ├── test_config.py
│   ├── test_config_conductor_env.py
│   ├── test_config_env_overrides.py
│   ├── test_models.py
│   ├── test_capabilities.py
│   ├── test_setup_status.py
│   ├── test_sync.py
│   └── test_sync_coverage.py
├── audio/
│   ├── test_audio_quality.py
│   ├── test_normalizer_extended.py
│   ├── test_normalizer_real_ffmpeg.py
│   ├── test_normalizer_unit.py
│   ├── test_tts.py
│   └── test_voice_validation.py
├── playlist/
│   ├── test_playlist.py
│   ├── test_playlist_fetch.py
│   ├── test_downloader.py
│   ├── test_jamendo_coverage.py
│   ├── test_song_cues.py
│   ├── test_track_rationale_coverage.py
│   └── test_track_rules.py
├── hosts/
│   ├── test_scriptwriter.py
│   ├── test_ads.py
│   ├── test_brand_config.py
│   ├── test_persona.py
│   ├── test_personality.py
│   ├── test_context_cues.py
│   └── test_new_features.py
├── home/
│   ├── test_ha_context.py
│   └── test_ha_enrichment.py
├── scheduling/
│   ├── test_producer_coverage.py
│   ├── test_producer_extended.py
│   ├── test_producer_unit.py
│   ├── test_scheduler.py
│   ├── test_clip.py
│   └── test_preview.py
├── web/
│   ├── test_streamer.py
│   ├── test_streamer_coverage.py
│   ├── test_streamer_routes.py
│   ├── test_streamer_routes_extended.py
│   ├── test_main.py
│   ├── test_admin_keyboard_shortcuts.py
│   ├── test_design_tokens.py
│   ├── test_og_card.py
│   ├── test_public_status_contract.py
│   ├── test_qa_regression_guards.py
│   ├── test_radio_immersion.py
│   ├── test_security.py
│   ├── test_shadow_queue_sync.py
│   ├── test_ui_control_contracts.py
│   └── test_xss_regression.py
├── addon/
│   ├── test_addon_build_workflow.py
│   ├── test_addon_local_build_script.py
│   └── test_addon_radio_sync.py
├── repo/
│   ├── test_repo_scripts.py
│   ├── test_run_sh_options_parser.py
│   ├── test_start_sh.py
│   ├── test_doc_audit_invariants.py
│   └── test_stream_watch_server.py
└── workflows/
    └── (existing workflow tests, no move)
```

### Docs collapse

Root keeps **only the four sacred files**:
- `README.md` — the 30-second pitch
- `CHANGELOG.md` — release notes
- `CONTRIBUTING.md` — local setup + the REPO_MAP link
- `CLAUDE.md` — agent rules + leadership principles

Everything else moves under `docs/`:

```
docs/
├── REPO_MAP.md                   # NEW — single-page "where does X live"
├── architecture.md               # was: ARCHITECTURE.md
├── operations.md                 # was: OPERATIONS.md
├── troubleshooting.md            # was: TROUBLESHOOTING.md
├── runbooks/
│   └── ha-addon.md               # was: HA_ADDON_RUNBOOK.md
├── design/
│   ├── system.md                 # was: DESIGN.md
│   └── admin-panel.md            # was: ADMIN_PANEL_STANDARDS.md
├── conductor.md                  # was: CONDUCTOR.md
├── agents.md                     # was: AGENTS.md
├── stabilization-log.md          # was: STABILIZATION_LOG.md
├── todos.md                      # was: TODOS.md  (or stay at root — see decision below)
├── banner.png                    # unchanged
├── designs/                      # unchanged
├── screenshots/                  # unchanged
└── (existing dated audit notes stay)
```

Note: `TODOS.md` is debatable — if you actively edit it daily, leave at root. Otherwise move. Default in this plan: move.

---

## REPO_MAP.md — the new front door

The contents of `docs/REPO_MAP.md`:

```markdown
# Repo Map — Where Things Live

If you want to fix or extend X, look in Y.

| What you want to change                         | Where to look                              |
|-------------------------------------------------|--------------------------------------------|
| What hosts say (banter, jokes, callouts)        | `mammamiradio/hosts/scriptwriter.py`       |
| Host personality, listener memory, motifs       | `mammamiradio/hosts/persona.py`            |
| Time-of-day / cultural cues injected to prompts | `mammamiradio/hosts/context_cues.py`       |
| Ads (brands, voices, campaign spines)           | `mammamiradio/hosts/ad_creative.py`        |
| Music sources (charts, Jamendo, local files)    | `mammamiradio/playlist/playlist.py`        |
| yt-dlp / Jamendo / local file fetch             | `mammamiradio/playlist/downloader.py`      |
| Per-track rules ("skip the bridge", anthems)    | `mammamiradio/playlist/track_rules.py`     |
| "Why this track?" rationale generation          | `mammamiradio/playlist/track_rationale.py` |
| FFmpeg normalize / mix / concat / SFX           | `mammamiradio/audio/normalizer.py`         |
| Edge TTS / OpenAI TTS synthesis                 | `mammamiradio/audio/tts.py`                |
| Audio quality gate (duration, silence checks)   | `mammamiradio/audio/audio_quality.py`      |
| Voice catalog (Edge voice IDs)                  | `mammamiradio/audio/voice_catalog.py`      |
| Home Assistant polling / state formatting       | `mammamiradio/home/ha_context.py`          |
| HA event derivation (diffs, pruning)            | `mammamiradio/home/ha_enrichment.py`       |
| Segment scheduling (banter / ad / music)        | `mammamiradio/scheduling/scheduler.py`     |
| Producer loop (queue ahead of playback)         | `mammamiradio/scheduling/producer.py`      |
| WTF clip extraction + ring buffer               | `mammamiradio/scheduling/clip.py`          |
| HTTP routes / playback loop / auth              | `mammamiradio/web/streamer.py`             |
| Open Graph share card                           | `mammamiradio/web/og_card.py`              |
| Listener / admin / regia HTML                   | `mammamiradio/web/templates/`              |
| CSS / JS / icons / service worker               | `mammamiradio/web/static/`                 |
| `radio.toml` parsing + `.env`                   | `mammamiradio/core/config.py`              |
| Shared data models (Track, Segment, etc.)       | `mammamiradio/core/models.py`              |
| Capability flags + tier derivation              | `mammamiradio/core/capabilities.py`        |
| SQLite schema / migrations                      | `mammamiradio/core/sync.py`                |
| App startup / shutdown lifecycle                | `mammamiradio/main.py`                     |

## Tests

`tests/` mirrors the source tree exactly. To find the test for `mammamiradio/hosts/persona.py`, look in `tests/hosts/test_persona.py`.

## Docs

| Doc                              | Path                            |
|----------------------------------|----------------------------------|
| Product pitch                    | `README.md`                      |
| Local setup, conventions         | `CONTRIBUTING.md`                |
| Agent rules + leadership         | `CLAUDE.md`                      |
| Runtime flow + API routes        | `docs/architecture.md`           |
| Deploy / production reality      | `docs/operations.md`             |
| Common failures + recovery       | `docs/troubleshooting.md`        |
| HA addon release process         | `docs/runbooks/ha-addon.md`      |
| Design system (colors, fonts)    | `docs/design/system.md`          |
```

---

## README rewrite — the 30-second pitch

Target: **what it is + what's different + what to do next** in the first viewport (≤25 visible lines on a 1080p laptop).

Current README is 199 lines, marketing-prose-heavy, with screenshots and tier tables before any "where to start." Rewrite shape:

```markdown
<banner image>

# Mamma Mi Radio

An AI-powered Italian radio station that nobody questions is real.
Two hosts banter between live Italian charts. Optional Home Assistant
context lets them reference your actual home — lights, temperature,
who's at the door. The format absorbs AI imperfection as authenticity.

```bash
git clone https://github.com/florianhorner/mammamiradio
cd mammamiradio && cp .env.example .env && docker compose up
```

**Want to read the code?** Start at [`docs/REPO_MAP.md`](docs/REPO_MAP.md).
**Want to ship a station to your home?** See [Home Assistant add-on](ha-addon/README.md).
**Want to contribute?** See [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

<everything that's currently in the README — testimonial, three-tier table,
fallback table, screenshots, configuration, etc. — moves below this fold>
```

The above is **20 lines, fits one viewport**. Everything else stays in the README but below the fold. Marketing prose isn't deleted — just demoted.

---

## Phased PR sequence

Each PR is sized to land in one day, run all CI gates, and not collide with active workstreams.

### Phase 0 — clear the deck before any move

Three PRs are currently open against `main` that touch files the cathedral PR 3 will relocate. Landing them **before** PR 3 reduces relocation cost and avoids forcing rebases on any in-flight branch.

| PR    | Net    | Touches                                 | Why first                                                  | CI status |
|-------|--------|-----------------------------------------|------------------------------------------------------------|-----------|
| #270  | -562   | `mammamiradio/static/listener.css`      | Deletes 567 lines of dead pre-Volare CSS. Cathedral should never relocate dead code. | Only `pi-smoke` and `submit-pypi` failing — likely release-time jobs, verify and merge |
| #269  | +138   | `mammamiradio/static/listener.css`, new test | Adds mobile invariants test before structure churn. Test file picks up its mirrored home in PR 3. | Clean |
| #271  | +6     | `mammamiradio/admin.html`, `mammamiradio/static/base.css` | Tap polish + iOS zoom fix on files PR 3 will move. | Failing **Proof block** hook — needs `## Proof` section per CLAUDE.md before it'll merge |

**Order of operations:**
1. Fix #271's Proof block (3-min edit) and confirm #270's failing checks are non-gating
2. Land #269 (cleanest)
3. Land #270 (largest reduction)
4. Land #271
5. Only then begin Phase 1

**Why this matters:** every line that lands as part of #270 is a line PR 3 doesn't have to git-mv. Every file untouched in flight is a file PR 3 can move without a contributor having to rebase. The cathedral diff shrinks by ~570 lines just by sequencing right.

If new PRs open against the relocation set during Phase 0, decide per-PR whether to land before PR 3 or hold for after. Default: land before, unless the change is large and slow.

### PR 1 — `feat(structure): collapse docs to docs/ tree`  (~600 line diff, mostly moves)
- Move all non-sacred markdown to `docs/` per the table above
- Update internal links across README, CONTRIBUTING, CLAUDE.md
- Update `tests/test_doc_audit_invariants.py` to match new paths
- Update `.github/CODEOWNERS` if any path-scoped rules apply
- Update `pre-commit-config.yaml` if it references doc paths
- **CI risk:** none (no Python imports change)
- **Doc sync rule:** satisfied — every move updates at least one of README/CHANGELOG/CONTRIBUTING

### PR 2 — `feat(structure): introduce REPO_MAP and rewrite README first viewport`  (~200 line diff)
- Create `docs/REPO_MAP.md` with the table above (paths still pointing at flat files for now — table will update in PR 3 to point at subpackage paths)
- Rewrite `README.md` first 25 lines per the pitch shape above
- Demote existing README content below the fold; do not delete
- Add a CONTRIBUTING.md link to REPO_MAP at the top
- **Wait:** can be merged before PR 3, but REPO_MAP rows pointing at subpackage paths require PR 3 to land first. Two options:
  - 2a — write REPO_MAP with current flat paths, then update in PR 3
  - 2b — hold REPO_MAP for inclusion in PR 3
- Recommend **2b** to avoid a stale REPO_MAP existing for any window

### PR 3 — `feat(structure): subpackage mammamiradio/ into seven naves`  (~1,200 line diff, 90% imports)
The big move. Per-step:

1. Create empty subpackage directories with `__init__.py` files
2. `git mv` source files to their new homes (preserve history — verify with `git log --follow`)
3. Update every `from mammamiradio.X import ...` → `from mammamiradio.<nave>.X import ...` across:
   - all moved source files
   - all test files (PR 4 mirrors tests, but imports update here)
   - `ha-addon/mammamiradio/rootfs/run.sh` if it imports anything
   - any `scripts/*.py`
4. Move HTML templates: `mammamiradio/*.html` → `mammamiradio/web/templates/`
5. Move `mammamiradio/static/` → `mammamiradio/web/static/`
6. Update `streamer.py` template loading paths (Jinja2 / FastAPI templates root)
7. Update FastAPI `StaticFiles` mount path
8. Move `mammamiradio/demo_assets/` → `mammamiradio/assets/demo/`
9. Move `mammamiradio/logo.svg` → `mammamiradio/assets/logo.svg`
10. Update `pyproject.toml` `[tool.setuptools.package-data]` to include new template/static/asset paths
11. Update `MANIFEST.in` if present
12. Update `.coverage-floors.json` keys: every floor entry currently keyed by `mammamiradio/X.py` must become `mammamiradio/<nave>/X.py`
13. Update `pyproject.toml` `[tool.coverage]` source paths
14. Update `Dockerfile` and `ha-addon/mammamiradio/Dockerfile` if they reference any moved file
15. Update `start.sh` reload-dir flag (still `mammamiradio` so subpackages auto-included)
16. Update `docs/REPO_MAP.md` rows to subpackage paths (this is when the map becomes accurate)
17. Update `docs/architecture.md` references
18. Update `CLAUDE.md` "Project structure" section (currently lists 27 flat files at lines 113-145)
19. Add a `# TODO: split` comment at the top of `streamer.py` and `scriptwriter.py` referencing this plan file

**CI risk:** high — every test changes its import path. Mitigation: do PR 3 and PR 4 as a single squash-merged PR, or land PR 3 with import updates only and PR 4 with file moves of tests immediately after.
**Decision:** combine PR 3 and PR 4 into one. The tests have to move with the source for CI to pass.

### PR 4 (combined into PR 3) — test tree mirror

(folded above)

### PR 5 — `feat(structure): split streamer.py`  (follow-up, separate PR)
- `web/streamer.py` (2395 LOC) → `web/routes_listener.py`, `web/routes_admin.py`, `web/auth.py`, `web/playback_loop.py`, `web/public_status.py`
- Pure mechanical extraction, no behavior changes
- Coverage floors per new file
- This PR can land any time after PR 3; not blocking the cathedral

### PR 6 — `feat(structure): split scriptwriter.py`  (follow-up)
- `hosts/scriptwriter.py` (1488 LOC) → `hosts/banter.py`, `hosts/ads.py`, `hosts/llm_client.py`, `hosts/fallbacks.py`
- Same shape as PR 5

PRs 5 and 6 are explicitly deferred; they are not part of "the cathedral has walls."

---

## Risks and mitigations

| Risk | Mitigation |
|------|-----------|
| Open PRs in flight rebase-conflict on import paths | Phase 0 clears #269/#270/#271 first. Audit open PRs again immediately before starting PR 3; if any new PR has opened against the relocation set, decide before moving. |
| HA addon entrypoint `mammamiradio.main:app` breaks | `main.py` stays at top of package. Smoke-test via `scripts/test-addon-local.sh` in PR 3. |
| Coverage floors miss the rename and CI fails | `.coverage-floors.json` rewrite is part of PR 3 acceptance criteria. Run `make coverage-check` locally before push. |
| Template-loading path breaks (Jinja2 / StaticFiles) | Add an explicit smoke test in PR 3: hit `/`, `/admin`, `/static/listener.css`, `/static/listener.js`. Already covered by `test_streamer_routes.py` — verify those tests pass post-move. |
| PyPI / wheel packaging excludes new template/static paths | Update `pyproject.toml` `package-data` section in PR 3. Test with `pip install -e .` and `python -c "from mammamiradio.web import streamer"`. |
| Pre-commit / ruff config references moved paths | Audit `.pre-commit-config.yaml` and `pyproject.toml [tool.ruff]` in PR 3. |
| `CLAUDE.md` "Project structure" section drifts | Update in PR 3, not as a follow-up. The Doc sync rule applies. |
| First-time contributor PR collision | Add `first-time-contributor` label scan before starting PR 3 (per CLAUDE.md merge-first protocol). |

---

## Acceptance criteria — how we know the cathedral has walls

After PR 3 merges:

- [ ] `find mammamiradio -maxdepth 1 -name "*.py"` returns only `__init__.py` and `main.py`
- [ ] `find mammamiradio -maxdepth 1 -name "*.html"` returns nothing
- [ ] `ls mammamiradio/` shows exactly: `__init__.py`, `main.py`, `core/`, `audio/`, `playlist/`, `hosts/`, `home/`, `scheduling/`, `web/`, `assets/`
- [ ] `find tests -maxdepth 1 -name "test_*.py"` returns nothing (all tests under subpackage dirs)
- [ ] Root-level `*.md` set is exactly: `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `CLAUDE.md`
- [ ] `docs/REPO_MAP.md` exists and every row resolves to a real path
- [ ] README's first 25 lines fit one viewport and contain: pitch, install command, three navigation links
- [ ] `make check` passes (lint + typecheck + coverage gate)
- [ ] `pytest tests/` passes
- [ ] `scripts/test-addon-local.sh` passes
- [ ] `docker compose up` produces audio within 5 seconds of stream connect

---

## Leadership principle check

| Principle | Pass? | Why |
|-----------|------|-----|
| #1 NEVER BREAK THE ILLUSION | ✅ | No runtime behavior change; structure-only |
| #2 INSTANT AUDIO | ✅ | Producer/scheduling/web modules stay together; playback loop unchanged |
| #3 PRODUCTION SYSTEMS DISCIPLINE | ✅ | All work via PR/CI; no live surgery; addon update happens once when CI ships new image |
| #4 README IS THE PITCH | ✅ | This plan IS the answer to principle #4 |

---

## What this plan does NOT include

- Function-level refactoring inside any moved file
- Renaming any module (only relocating)
- Splitting `streamer.py` or `scriptwriter.py` (deferred to PRs 5–6)
- Changing the public addon entrypoint
- Touching `radio.toml` schema
- Touching CI workflow definitions beyond updating path globs

If a PR reviewer asks "should we also clean up X while we're here?" — the answer is no. That's a separate PR after the cathedral has walls.

---

## Next action

1. **Phase 0 first.** Fix #271's Proof block, confirm #270's failing checks are non-gating, land #269 → #270 → #271 in that order. ~570 lines of code disappear before the cathedral starts moving anything.
2. **Then PR 1** (docs collapse) — lowest-risk Python-untouched move.
3. **Then PR 3** (subpackage move + REPO_MAP + README rewrite) — wait for a quiet window where no first-time contributor PRs are open against the relocation set.
4. **Then PRs 5/6** (god-module splits) on their own cadence, one per week, no rush.

Total cathedral diff after Phase 0 lands: ~1,400 lines instead of ~2,000.
