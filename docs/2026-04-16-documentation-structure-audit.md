# Documentation And Folder Structure Audit

Date: 2026-04-16

Scope:

- top-level docs: `README.md`, `ARCHITECTURE.md`, `CONTRIBUTING.md`, `OPERATIONS.md`, `CONDUCTOR.md`, `HA_ADDON_RUNBOOK.md`, `CHANGELOG.md`, `CLAUDE.md`
- add-on docs: `ha-addon/README.md`, `ha-addon/mammamiradio/DOCS.md`, `ha-addon/mammamiradio/CHANGELOG.md`
- runtime/package structure under `mammamiradio/`, `ha-addon/`, `docs/`, `scripts/`, `tests/`

Method:

- indexed the committed directory tree
- checked repo markdown links
- cross-checked docs against `docker-compose.yml`, add-on packaging files, `conductor.json`, lifecycle scripts, and FastAPI routes in `mammamiradio/streamer.py`

## Summary

- Relative markdown links inside the repo docs are intact. The main problem is semantic drift, not broken links.
- The largest inconsistency cluster is the demo-asset story: multiple docs promise bundled audio that is not present in the package tree the code uses.
- The second largest cluster is route drift: `/`, `/dashboard`, `/admin`, and `/listen` are documented inconsistently across files.
- The third cluster is add-on release drift: docs, tests, local validation, CI, and changelogs disagree about the `ha-addon/mammamiradio/radio.toml` contract.

## Folder Structure Notes

Actual high-level structure is coherent at the top level:

- `mammamiradio/` app code, HTML, static assets
- `ha-addon/` Home Assistant packaging
- `docs/` project docs and design assets
- `scripts/` lifecycle and validation scripts
- `tests/` test suite

The main structure risks are duplicate or split trees with unclear ownership:

- top-level `demo_assets/` exists separately from `mammamiradio/demo_assets/`
- top-level `repository.yaml` exists separately from `ha-addon/repository.yaml`

## Findings

### 1. Docker quick start is not runnable as documented

Evidence:

- `README.md:29-35` says `cp .env.example .env && docker compose up`, then "the station is playing"
- `README.md:74-75` says `docker compose up` gives a working radio station in 30 seconds
- `docker-compose.yml:11-17` requires a non-empty `ADMIN_TOKEN`
- `.env.example:8-11` leaves `ADMIN_TOKEN` empty
- `docker-compose.yml` does not set `MAMMAMIRADIO_ALLOW_YTDLP=true`

Impact:

- the documented compose path fails immediately unless the operator edits `.env`
- even after adding `ADMIN_TOKEN`, compose depends on demo assets or local music because yt-dlp is disabled by default

### 2. Demo mode is documented as bundled audio, but the package tree does not contain those assets

Evidence:

- `README.md:119-130` says Demo Radio and missing-yt-dlp fallback use pre-bundled clips / a bundled demo playlist
- `CONTRIBUTING.md:13` says the app falls back to bundled demo tracks
- `OPERATIONS.md:12` says non-yt-dlp mode uses a bundled demo playlist
- `CLAUDE.md:72-74,111` says demo-first boot uses built-in demo tracks and `demo_assets/banter/`
- `ha-addon/mammamiradio/DOCS.md:14-18,81-85,289-290` says demo mode uses pre-bundled banter
- `mammamiradio/demo_assets/README.md:3-19` documents `banter/`, `ads/`, `music/`, and `jingles/`
- committed `mammamiradio/demo_assets/` content only includes `README.md` and `welcome/README.md`

Impact:

- the documented fallback inventory does not exist in the package tree the app actually reads
- operators get a materially different degraded mode than the docs promise

### 3. The documented demo-asset generator command points to a module that does not exist

Evidence:

- `mammamiradio/demo_assets/README.md:15-19` instructs `python -m mammamiradio.generate_demo_assets`
- no `mammamiradio/generate_demo_assets.py` module exists in the repo

Impact:

- the asset-generation documentation cannot be followed

### 4. Demo SFX are committed in the wrong tree for runtime lookup and packaging

Evidence:

- `mammamiradio/producer.py:97-99` resolves demo assets under `mammamiradio/demo_assets`
- `mammamiradio/producer.py:1073-1079` looks for humanity-event SFX in `mammamiradio/demo_assets/sfx/studio`
- the committed MP3 SFX live under top-level `demo_assets/sfx/studio/`, not `mammamiradio/demo_assets/sfx/studio/`
- `mammamiradio/downloader.py:143-165` looks for demo music in `mammamiradio/demo_assets/music`

Impact:

- the one-shot "humanity event" SFX path is disconnected from the committed asset files
- top-level demo assets are also outside the package-data tree used by `pyproject.toml`

### 5. The code no longer uses a music placeholder tone, but the docs still say it does

Evidence:

- `ARCHITECTURE.md:54-56` says the music path falls back to a generated placeholder tone
- `CLAUDE.md:74` says music comes from local files or placeholder tones
- `mammamiradio/downloader.py:168-190` generates silence
- `mammamiradio/downloader.py:297-302` uses silence as the terminal fallback

Impact:

- the audio-fallback behavior described in docs is stale

### 6. Route documentation is inconsistent across README, CONTRIBUTING, OPERATIONS, CLAUDE, and the actual app

Evidence:

- `README.md:64` says `/` is the dashboard and `/admin` is the control room
- `CONTRIBUTING.md:114-115` says `/` is the dashboard and `/listen` is the listener page
- `OPERATIONS.md:58-59,72` says `/listen` redirects to `/` and also lists `/` as an admin route
- `CLAUDE.md:108-110` says `dashboard.html` is served at `/` and `listener.html` redirects to `/`
- `mammamiradio/streamer.py:787-821` shows:
  - `/` serves `listener.html` in normal mode
  - `/dashboard` serves `dashboard.html` behind admin auth
  - `/admin` serves `admin.html` behind admin auth
  - `/listen` serves `listener.html` as an alias, not a redirect

Impact:

- the public/admin surface is hard to reason about from the docs alone
- manual smoke-test instructions are currently aimed at the wrong pages

### 7. The route inventory docs are incomplete relative to the actual FastAPI surface

Evidence:

- `OPERATIONS.md:54-97` presents an HTTP surface that omits `/dashboard`, `/sw.js`, `/static/{filename:path}`, `PATCH /api/pacing`, `PATCH /api/hosts/{host_name}/personality`, and `POST /api/listener-requests/dismiss`
- `ARCHITECTURE.md:181-217` omits the same newer routes and update methods
- `mammamiradio/streamer.py:799-840,1174-1189,1400-1411,1638-1658` implements them

Impact:

- the docs no longer describe the full external/admin contract

### 8. `/api/logs` is documented as a real admin endpoint, but it is a stub

Evidence:

- `OPERATIONS.md:74` lists `/api/logs` as part of the admin surface
- `mammamiradio/streamer.py:862-865` returns `{}` for that route

Impact:

- the documentation overstates available diagnostics

### 9. The add-on docs are stale relative to the actual config schema

Evidence:

- `ha-addon/mammamiradio/config.yaml:26-39` defines six options: `anthropic_api_key`, `openai_api_key`, `station_name`, `claude_model`, `admin_token`, `enable_home_assistant`
- `HA_ADDON_RUNBOOK.md:52-65` documents only four options
- `ha-addon/README.md:17-23` documents only three options
- `ha-addon/mammamiradio/DOCS.md:145-160` omits `ADMIN_TOKEN` and the `enable_home_assistant` option in its env-flow diagram

Impact:

- the UI contract and docs are already out of sync

### 10. The add-on release contract for `radio.toml` is contradictory across docs, tests, local validation, changelog, and CI

Evidence:

- `HA_ADDON_RUNBOOK.md:84-85` says add-on `radio.toml` must remain byte-for-byte identical to root
- `tests/test_addon_radio_sync.py:4-7` enforces exact equality
- `scripts/test-addon-local.sh:117-122` enforces exact equality
- `CHANGELOG.md:19-22` says 2.10.3 removed the Pi-specific overrides and CI now validates strict `cmp -s`
- `.github/workflows/addon-build.yml:61-73` still applies a sed transform that expects the old Pi-specific overrides
- `ha-addon/mammamiradio/CHANGELOG.md:11-15` still says the add-on intentionally carries those overrides

Impact:

- the repo has no single trustworthy source of truth for add-on `radio.toml` sync
- CI, release notes, and local validation currently disagree on the same invariant

### 11. The changelog sync rule is documented and tested, but the repo is already out of sync

Evidence:

- `CHANGELOG.md:9-23` has a `2.10.3` entry
- `ha-addon/mammamiradio/CHANGELOG.md:1-4` stops at `2.10.2`
- `tests/test_repo_scripts.py:86-133` and `scripts/check-changelog-sync.sh` enforce that both changelogs move together on version bumps

Impact:

- release documentation is already inconsistent for the current version line

### 12. `CONDUCTOR.md` overstates how flexible `.env` discovery is

Evidence:

- `CONDUCTOR.md:7,17` says the setup script can use a `.env` "in a location of your choice" and that you can "point the setup script at it"
- `scripts/conductor-setup.sh:7-23` only checks `~/.config/mammamiradio/.env` and `$CONDUCTOR_ROOT_PATH/.env`

Impact:

- the Conductor doc implies a configuration mechanism that the script does not provide

### 13. Duplicate files exist with no documented synchronization boundary

Evidence:

- top-level `repository.yaml` and `ha-addon/repository.yaml` are both committed
- top-level `demo_assets/` and `mammamiradio/demo_assets/` are both committed but serve different, undocumented roles

Impact:

- these duplicates are drift-prone because only one side of each pair is consistently referenced by docs and validation

## Recommended Cleanup Order

1. Fix the onboarding path first: README + docker compose + demo asset reality must agree.
2. Decide the real demo-asset contract: bundled assets, generated assets, or explicit silence fallback. Then align tree layout, packaging, and docs.
3. Pick one source of truth for route docs and update `README.md`, `CONTRIBUTING.md`, `OPERATIONS.md`, and `CLAUDE.md` together.
4. Resolve the add-on `radio.toml` contract, then make CI, local validation, changelogs, and runbooks say the same thing.
5. Remove or document duplicated trees (`demo_assets`, `repository.yaml`) so future drift has an explicit boundary.

## Resolution Status

Updated after remediation work on 2026-04-16.

- #1 Docker quick start — fixed: `docker-compose.yml` now defaults `MAMMAMIRADIO_ALLOW_YTDLP=true` and passes `OPENAI_API_KEY` through, matching docs.
- #2 Demo mode packaging — partial: the `mammamiradio/demo_assets/` tree is now the single source of truth and ships as package data. SFX are populated; `banter/`, `ads/`, `music/`, `jingles/`, `welcome/` still need real MP3s. Tracked in memory `project_demo_asset_contract.md`.
- #3 Phantom generator command — fixed: `mammamiradio/demo_assets/README.md` no longer points to a non-existent module.
- #4 SFX in wrong tree — fixed: `demo_assets/sfx/studio/*.mp3` moved to `mammamiradio/demo_assets/sfx/studio/`, matching the `producer.py` lookup.
- #5 Stale placeholder-tone wording — fixed: README, CLAUDE.md, ARCHITECTURE.md now describe silence fallback.
- #6 Route drift — fixed: README, CONTRIBUTING, OPERATIONS, CLAUDE, ARCHITECTURE aligned on `/ = listener (HA ingress flips to admin)`, `/admin` and `/dashboard` admin-auth, `/listen` = listener alias. Contract locked by `tests/test_doc_audit_invariants.py`.
- #7 Route inventory gaps — fixed: ARCHITECTURE.md and OPERATIONS.md now list `/dashboard`, `/sw.js`, `/static/{filename:path}`, `PATCH /api/pacing`, `PATCH /api/hosts/{name}/personality`, `POST /api/listener-requests/dismiss`.
- #8 `/api/logs` stub — fixed: removed from admin API tables.
- #9 Add-on docs stale — fixed: `HA_ADDON_RUNBOOK.md`, `ha-addon/README.md`, `ha-addon/mammamiradio/DOCS.md` now include `admin_token` and `enable_home_assistant`.
- #10 Add-on `radio.toml` contract — fixed: CI workflow uses `cmp -s`, matching local validator and Python tests. `tests/test_addon_build_workflow.py` locks the byte-identical contract; sed-based split-brain is forbidden.
- #11 Changelog sync — fixed: `ha-addon/mammamiradio/CHANGELOG.md` now has the 2.10.3 entry.
- #12 `CONDUCTOR.md` `.env` discovery — fixed: narrowed to the two paths the script actually checks.
- #13 Duplicate trees — fixed: `demo_assets/` consolidated into `mammamiradio/demo_assets/`; `ha-addon/repository.yaml` deleted (root copy is the only one HA consumes). Both locked by `tests/test_doc_audit_invariants.py`.
